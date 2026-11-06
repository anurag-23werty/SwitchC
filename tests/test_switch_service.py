"""The room service's routing, offline: fake Switch connections, real everything else.

Events are fed to the Dispatcher as Switch would deliver them; posts, hand-offs,
uploads and the audit feed are recorded by fake connections.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from khata import switch_service as svc
from khata.runtime import Khata
from khata.switch_service import Job, KhataRoom

from .conftest import GOLDEN, ROOT
from .llm_fakes import FakeProviders

pytestmark = pytest.mark.asyncio(loop_scope="module")
BY_ID = {g["id"]: g for g in GOLDEN}


class FakeConn:
    def __init__(self, name: str, log: list) -> None:
        self.name, self.log = name, log

    async def post(self, body: str, thread_id: str | None) -> dict:
        self.log.append((self.name, "post", thread_id, None, body))
        return {}

    async def address(
        self, body: str, targets: list[str], thread_id: str | None
    ) -> dict:
        self.log.append((self.name, "address", thread_id, targets, body))
        return {}

    async def op(self, operation: str, **args: Any) -> Any:
        self.log.append((self.name, "op", None, operation, args.get("name") or ""))
        if operation == "list_references":
            return {"documents": []}
        if operation == "create_room_document":
            return {"document_id": f"doc-{args['name']}"}
        return {}


class FakeAgent:
    def __init__(self, name: str, log: list) -> None:
        self.name, self.log = name, log

    async def download_media(self, room_id: str, mxc: str) -> bytes:
        return (ROOT / mxc.removeprefix("test://")).read_bytes()

    async def upload_media(self, room_id, files, caption, thread_id) -> dict:
        self.log.append(
            (self.name, "upload", thread_id, [f[0] for f in files], caption)
        )
        return {}

    async def aclose(self) -> None:
        pass


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def room(test_db_url, tmp_path_factory):
    fake = FakeProviders(GOLDEN, ROOT)
    svc.INBOX = (
        ROOT / "data" / "inbox" / "_test"
    )  # extract_invoice only reads under data/
    async with Khata.open(
        database_url=test_db_url,
        llm=fake.llm(),
        guard=False,
        payments_dir=tmp_path_factory.mktemp("pay"),
        today=date(2026, 11, 6),
    ) as k:
        r = object.__new__(KhataRoom)
        r.k, r.desk, r.audit = k, "desk-room", "audit-room"
        r.log = []
        r.conns = {n: FakeConn(n, r.log) for n in svc.AGENTS}
        r.agents = {n: FakeAgent(n, r.log) for n in svc.AGENTS}
        r.feed = FakeConn("feed", r.log)
        r.doc_ids, r._labels = {}, {}
        r.invoices, r.decisions = asyncio.Queue(), asyncio.Queue()
        yield r


async def drain(r: KhataRoom) -> None:
    for q in (r.invoices, r.decisions):
        while not q.empty():
            await r._do(q.get_nowait())
            q.task_done()


def upload(msg: str, gid: str, sender: str = "ravi") -> dict:
    return {
        "message_id": msg,
        "sender_name": sender,
        "body": "New invoice",
        "attachments": [
            {
                "filename": Path(BY_ID[gid]["file"]).name,
                "mxc": "test://" + BY_ID[gid]["file"],
            }
        ],
    }


def said(r: KhataRoom, thread: str) -> list[tuple]:
    return [e for e in r.log if e[2] == thread]


async def test_a_posted_pdf_runs_the_workflow_and_agents_hand_off_by_name(room):
    await room.on_message(upload("m-d01", "D01"))
    assert room.invoices.qsize() == 1
    await drain(room)
    thread = said(room, "m-d01")
    first = thread[0]
    assert first[:2] == ("khata-dispatcher", "address") and first[3] == ["khata-intake"]
    handoffs = [(e[0], e[3]) for e in thread if e[1] == "address"]
    assert ("khata-intake", ["khata-verifier"]) in handoffs
    assert ("khata-verifier", ["khata-router"]) in handoffs
    assert ("khata-router", ["khata-payer"]) in handoffs
    uploads = [e for e in thread if e[1] == "upload"]
    assert (
        uploads and uploads[0][3][0].endswith(".csv") and "never pays" in uploads[0][4]
    )


async def test_redelivered_event_is_handled_once(room):
    await room.on_message(upload("m-d01", "D01"))
    assert room.invoices.empty()


async def test_approval_in_the_thread_resumes_the_run(room):
    await room.on_message(upload("m-d02", "D02"))
    await drain(room)
    request = [
        e for e in said(room, "m-d02") if e[0] == "khata-router" and "@priya" in e[4]
    ]
    assert request, "Router should tag Priya"

    await room.on_message(
        {
            "message_id": "r1",
            "thread_id": "m-d02",
            "sender_name": "priya",
            "body": "Looks fine",
        }
    )
    hint = said(room, "m-d02")[-1]
    assert (
        hint[0] == "khata-dispatcher" and "start your reply with `approve`" in hint[4]
    )
    assert room.decisions.empty()

    await room.on_message(
        {
            "message_id": "r2",
            "thread_id": "m-d02",
            "sender_name": "ravi",
            "body": "approve looks right",
        }
    )
    await drain(room)
    assert "can't approve" in said(room, "m-d02")[-1][4]

    await room.on_message(
        {
            "message_id": "r3",
            "thread_id": "m-d02",
            "sender_name": "priya",
            "body": "approve Matches PO and goods received.",
        }
    )
    await drain(room)
    async with room.k.pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT i.state, d.switch_message_id FROM invoices i JOIN decisions d "
                "ON d.invoice_id = i.id WHERE i.switch_thread_id = 'm-d02'"
            )
        ).fetchone()
    assert row["state"] == "PAYMENT_DRAFTED" and row["switch_message_id"] == "r3"


async def test_decisions_and_drafts_go_to_the_audit_feed(room):
    feed = [e[4] for e in room.log if e[0] == "feed"]
    assert any("DPK DPK/1187" in f and "waiting for @priya" in f for f in feed)
    assert any("priya approved the manager step" in f for f in feed)
    assert any("Payer: Drafted NEFT batch" in f for f in feed)


async def test_room_documents_are_written_from_postgres(room):
    created = [
        e[4] for e in room.log if e[1] == "op" and e[3] == "create_room_document"
    ]
    updated = [e for e in room.log if e[1] == "op" and e[3] == "update_room_document"]
    assert "khata-decisions-log" in created and updated


async def test_non_pdf_is_declined_and_agent_posts_are_ignored(room):
    await room.on_message(
        {
            "message_id": "x1",
            "sender_name": "ravi",
            "attachments": [{"filename": "photo.jpg", "mxc": "test://x"}],
        }
    )
    assert "only take invoices as PDF" in said(room, "x1")[-1][4]
    before = len(room.log)
    await room.on_message(
        {"message_id": "x2", "sender_name": "khata-router", "body": "hi"}
    )
    assert len(room.log) == before and room.invoices.empty()


async def test_a_failed_job_says_so_in_the_thread(room):
    worker = asyncio.create_task(room._work(room.invoices))
    await room.invoices.put(
        Job("invoice", "m-bad", "ravi", "m-bad", path=Path("/nonexistent.pdf"))
    )
    await room.invoices.join()
    worker.cancel()
    last = said(room, "m-bad")[-1]
    assert last[0] == "khata-intake" and "Nothing was decided or paid" in last[4]


async def test_dispatcher_cursor_survives_a_restart(room):
    await room._save_cursor(41)
    await room._save_cursor(42)
    assert await room._load_cursor() == 42
