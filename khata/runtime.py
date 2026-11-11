"""Khata's agent runtime: the workflow graph with its tools, LLMs and checkpointer.

    async with Khata.open() as k:
        out = await k.submit("data/invoices/D03_MST_MST-INV-7731.pdf")   # pauses at approval
    # ... any time later, in any process:
    async with Khata.open() as k:
        out = await k.decide(out.invoice_id, "priya", "approve", "Freight agreed")

LangGraph checkpoints live in Khata's Postgres (schema `khata`), so a paused
approval survives a restart; `make seed` resets both together.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from khata import steward, tracing
from khata.config import ROOT, Settings, get_settings
from khata.db import make_pool
from khata.graph.workflow import Deps, build_graph
from khata.llm import LLM, PromptGuard, endpoints_from_settings
from khata.migrate import pending_url
from khata.policy import ToolGateway
from khata.tools import Toolbox

OnLog = Callable[[int | None, dict[str, str]], Awaitable[None]]


@dataclass
class Outcome:
    """Where an invoice's run stands after a step."""

    invoice_id: int | None
    graph_thread: str
    state: str | None
    paused: bool
    request: dict[str, Any] | None = None  # the approval request, if paused
    log: list[dict[str, str]] = field(default_factory=list)  # new agent messages
    values: dict[str, Any] = field(default_factory=dict)


class Khata:
    def __init__(self, pool, cp_pool, llm, guard, graph, gw) -> None:
        self.pool = pool
        self.gw = gw  # the ToolGateway: callers outside the graph use it too
        self._cp_pool = cp_pool
        self.llm = llm
        self.guard = guard
        self.graph = graph

    @classmethod
    @asynccontextmanager
    async def open(
        cls,
        settings: Settings | None = None,
        *,
        database_url: str | None = None,
        llm: LLM | None = None,
        guard: PromptGuard | None | bool = None,
        payments_dir: Path | None = None,
        today: date | None = None,
    ):
        """Open pools, the checkpointer and the graph. `guard=False` disables Prompt Guard."""
        s = settings or get_settings()
        url = database_url or s.khata_database_url
        behind = await asyncio.to_thread(pending_url, url)
        if behind:
            raise RuntimeError(
                f"database schema is behind ({', '.join(behind)} pending): "
                "run `khata db migrate`"
            )
        pool = make_pool(url)
        # The checkpointer wants autocommit connections without prepared statements.
        cp_pool = AsyncConnectionPool(
            url,
            min_size=1,
            max_size=5,
            open=False,
            kwargs={
                "options": "-c search_path=khata,public",
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
        )
        await pool.open()
        await cp_pool.open()
        own_llm = llm is None
        eps = endpoints_from_settings(s)
        if llm is None:
            llm = LLM(eps, cache_dir=s.llm_cache_dir, pool=pool)
        elif llm.pool is None:
            llm.pool = pool  # every LLM call is logged with its invoice id
        if guard is None:
            guard = PromptGuard(eps["guard"]) if s.groq_api_key else None
        elif guard is False:
            guard = None
        try:
            saver = AsyncPostgresSaver(cp_pool)
            await saver.setup()
            box = Toolbox(
                pool,
                files_root=ROOT / "data",
                payments_dir=payments_dir or s.payments_dir,
                today=today,
                guard=guard,
            )
            gw = ToolGateway(pool, box.as_dict())
            graph = build_graph(Deps(pool, gw, llm)).compile(checkpointer=saver)
            yield cls(pool, cp_pool, llm, guard, graph, gw)
        finally:
            if own_llm:
                await llm.aclose()
            if isinstance(guard, PromptGuard):
                await guard.aclose()
            await cp_pool.close()
            await pool.close()
            tracing.flush()

    # -- running ------------------------------------------------------------------

    async def _run(self, thread: str, payload: Any, on_log: OnLog | None) -> Outcome:
        """Run until the gate or the end. `on_log(invoice_id, line)` hears each agent
        message as its node finishes, so a room can show the work as it happens."""
        config = {"configurable": {"thread_id": thread}, **tracing.run_config()}
        before = await self.graph.aget_state(config)
        seen = len(before.values.get("log", [])) if before.values else 0
        invoice_id = before.values.get("invoice_id") if before.values else None
        with tracing.invoice_run(thread):
            async for chunk in self.graph.astream(
                payload, config, stream_mode="updates"
            ):
                for node, update in chunk.items():
                    if node.startswith("__") or not isinstance(update, dict):
                        continue
                    invoice_id = update.get("invoice_id", invoice_id)
                    if on_log is not None:
                        for line in update.get("log") or []:
                            await on_log(invoice_id, line)
        return await self.outcome(thread, since=seen)

    async def outcome(self, thread: str, since: int = 0) -> Outcome:
        snap = await self.graph.aget_state({"configurable": {"thread_id": thread}})
        v = snap.values or {}
        interrupts = [i.value for t in snap.tasks for i in t.interrupts]
        inv = v.get("invoice_id")
        state = None
        if inv is not None:
            async with self.pool.connection() as conn:
                row = await (
                    await conn.execute(
                        "SELECT state FROM invoices WHERE id = %s", (inv,)
                    )
                ).fetchone()
            state = row["state"] if row else None
        return Outcome(
            invoice_id=inv,
            graph_thread=thread,
            state=state,
            paused=bool(interrupts),
            request=interrupts[0] if interrupts else None,
            log=v.get("log", [])[since:],
            values=v,
        )

    async def submit(
        self,
        file_path: str | Path,
        *,
        room_id: str | None = None,
        thread_id: str | None = None,
        on_log: OnLog | None = None,
    ) -> Outcome:
        """Start a run for a received document. Returns at the approval gate or the end."""
        path = Path(file_path)
        if not path.is_absolute():
            path = ROOT / path
        return await self._run(
            f"inv-{uuid.uuid4()}",
            {"file_path": str(path), "room_id": room_id, "thread_id": thread_id},
            on_log,
        )

    async def thread_for(self, invoice_id: int) -> str:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT graph_thread_id FROM invoices WHERE id = %s", (invoice_id,)
                )
            ).fetchone()
        if row is None or row["graph_thread_id"] is None:
            raise LookupError(f"invoice {invoice_id} has no workflow run")
        return row["graph_thread_id"]

    async def decide(
        self,
        invoice_id: int,
        username: str,
        decision: str,
        reason: str,
        message_id: str | None = None,
        on_log: OnLog | None = None,
    ) -> Outcome:
        """Resume a paused run with a person's reply. Khata checks they may make it."""
        thread = await self.thread_for(invoice_id)
        current = await self.outcome(thread)
        if not current.paused:
            raise RuntimeError(
                f"invoice {invoice_id} isn't waiting for a decision ({current.state})"
            )
        reply = {
            "username": username,
            "decision": decision,
            "reason": reason,
            "message_id": message_id,
        }
        return await self._run(thread, Command(resume=reply), on_log)

    async def retry(self, invoice_id: int, on_log: OnLog | None = None) -> Outcome:
        """Re-run a run that stopped on an error (e.g. both LLM providers down)."""
        return await self._run(await self.thread_for(invoice_id), None, on_log)

    async def unfinished(self) -> list[int]:
        """Invoices whose run stopped before the gate or the end (a crash, an outage)."""
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, graph_thread_id FROM invoices WHERE graph_thread_id IS NOT NULL "
                    "AND state <> 'REJECTED' ORDER BY id"
                )
            ).fetchall()
        out = []
        for r in rows:
            snap = await self.graph.aget_state(
                {"configurable": {"thread_id": r["graph_thread_id"]}}
            )
            if snap.next and not any(t.interrupts for t in snap.tasks):
                out.append(r["id"])
        return out

    # -- rule proposals (Policy Steward) ------------------------------------------

    async def proposals(self, status: str | None = None) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id FROM rule_proposals WHERE %s::text IS NULL OR status = %s "
                    "ORDER BY id",
                    (status, status),
                )
            ).fetchall()
            return [await steward.describe(conn, r["id"]) for r in rows]

    async def decide_rule(
        self,
        proposal_id: int,
        username: str,
        decision: str,
        reason: str,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        """The owner's answer to a rule proposal. Approval activates a new policy version."""
        async with self.pool.connection() as conn, conn.transaction():
            return await steward.decide(
                conn, proposal_id, username, decision, reason, message_id
            )

    async def pending(self) -> list[Outcome]:
        """Every run paused at the approval gate."""
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT graph_thread_id FROM invoices WHERE graph_thread_id IS NOT NULL "
                    "AND state IN ('AWAITING_APPROVAL', 'ON_HOLD') ORDER BY id"
                )
            ).fetchall()
        outs = [await self.outcome(r["graph_thread_id"]) for r in rows]
        return [o for o in outs if o.paused]
