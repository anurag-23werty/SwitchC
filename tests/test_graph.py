"""The LangGraph workflow end to end, offline: real graph, real Postgres checkpointer,
real tools and gateway; fake LLM providers behind the real LLM client.

Phase 3's acceptance: the 9 demo invoices reach their labelled states, and an
approval pauses and resumes after a simulated reply, even across a restart.
"""

from __future__ import annotations

from datetime import date

import pytest
import pytest_asyncio

from khata.graph.workflow import ExtractionFailed
from khata.runtime import Khata

from .conftest import GOLDEN, ROOT
from .llm_fakes import BOGUS_PRECEDENT, FakeProviders

pytestmark = pytest.mark.asyncio(loop_scope="module")
DEMO = [g for g in GOLDEN if g["id"].startswith("D")]
BY_ID = {g["id"]: g for g in GOLDEN}


@pytest.fixture(scope="module")
def fake():
    return FakeProviders(GOLDEN, ROOT)


@pytest.fixture(scope="module")
def opener(test_db_url, fake, tmp_path_factory):
    pay_dir = tmp_path_factory.mktemp("payments")

    def open_():
        return Khata.open(
            database_url=test_db_url,
            llm=fake.llm(),
            guard=False,
            payments_dir=pay_dir,
            today=date(2026, 11, 6),
        )

    return open_


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def demo(opener):
    """D01-D09 in order, each paused run answered with its labelled human replies."""
    results = {}
    async with opener() as k:
        for g in DEMO:
            out = await k.submit(g["file"])
            first = out
            for h in g["expected"]["human"]:
                assert out.paused, f"{g['id']} should be waiting for {h['by']}"
                out = await k.decide(
                    out.invoice_id, h["by"], h["decision"], h["reason"]
                )
            results[g["id"]] = {"first": first, "final": out}
    return results


@pytest.mark.parametrize("g", DEMO, ids=lambda g: g["id"])
async def test_demo_invoice_reaches_its_labelled_state(demo, g, pool):
    r, e = demo[g["id"]], g["expected"]
    assert not r["final"].paused
    assert r["final"].state == e["final_state"]
    gate = "APPROVED" if e["gate_state"] == "PAYMENT_DRAFTED" else e["gate_state"]
    assert r["first"].values["route"]["state"] == gate
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT flags, route FROM invoices WHERE id = %s",
                (r["final"].invoice_id,),
            )
        ).fetchone()
    assert sorted(row["flags"]) == sorted(e["flags"])
    assert row["route"] == e["route"]


async def test_people_only_see_the_gate_when_policy_needs_them(demo):
    for gid, r in demo.items():
        needs_person = bool(BY_ID[gid]["expected"]["human"])
        assert r["first"].paused is needs_person, gid


async def test_d04_analyst_cites_the_d03_decision_and_fake_precedents_are_removed(demo):
    req = demo["D04"]["first"].request
    cited = [p["invoice_no"] for p in req["analyst"]["precedents"]]
    assert "MST/INV/7731" in cited
    assert BOGUS_PRECEDENT not in cited
    review = demo["D04"]["first"].values["review"]
    assert any(BOGUS_PRECEDENT in c for c in review["corrections"])


async def test_analyst_and_auditor_run_on_different_providers(demo):
    review = demo["D03"]["first"].values["review"]
    assert review["analyst_provider"] == "groq"
    assert review["auditor_provider"] == "openrouter"
    assert review["analyst_model"] != review["auditor_model"]


async def test_bank_change_request_is_a_hard_stop_for_the_owner(demo):
    req = demo["D08"]["first"].request
    assert req["hard_stop"] and req["next_approver"] == "owner"
    assert "+91 80 2797 1180" in req["message"] and "HARD STOP" in req["message"]


async def test_rejected_and_auto_invoices_spend_no_review_calls(demo, fake):
    """Analyst/Auditor run only when a person must decide (free-tier quota)."""
    for gid in ("D01", "D05", "D06", "D07"):
        assert "review" not in demo[gid]["final"].values


async def test_agents_speak_in_order(demo):
    agents = [line["agent"] for line in demo["D03"]["first"].log]
    assert agents == [
        "khata-intake",
        "khata-intake",
        "khata-verifier",
        "khata-router",
        "khata-analyst",
        "khata-auditor",
    ]


async def test_every_llm_call_is_logged_with_the_invoice(demo, pool):
    d03 = demo["D03"]["final"].invoice_id
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT agent, ok FROM llm_calls WHERE invoice_id = %s ORDER BY id",
                (d03,),
            )
        ).fetchall()
    assert sorted(r["agent"] for r in rows) == sorted(
        ["intake-a", "intake-b", "analyst", "auditor"]
    )


async def test_approval_survives_a_restart_and_refused_replies_keep_it_paused(
    opener, demo
):
    """G12 (> Rs 1 lakh) needs Priya then the owner. Each step in a new process."""
    g = BY_ID["G12"]
    async with opener() as k:
        out = await k.submit(g["file"])
        invoice_id = out.invoice_id
    assert out.paused and out.request["next_approver"] == "priya"

    async with opener() as k:  # a fresh runtime: new pools, checkpointer, graph
        waiting = [o.invoice_id for o in await k.pending()]
        assert invoice_id in waiting
        out = await k.decide(invoice_id, "priya", "approve", "Matches PO and GRN.")
    assert out.paused and out.state == "AWAITING_APPROVAL"
    assert out.request["next_approver"] == "owner"

    async with opener() as k:
        out = await k.decide(invoice_id, "priya", "approve", "Me again.")
        assert out.paused and "can't approve the owner step" in out.request["error"]
        out = await k.decide(invoice_id, "ravi", "reject", "I'm the clerk.")
        assert out.paused and out.request["error"]
        out = await k.decide(invoice_id, "owner", "approve", "Approved.")
    assert not out.paused and out.state == "PAYMENT_DRAFTED"
    assert out.values["payment"]["drafted"]


async def test_disagreeing_readers_hold_with_both_readings_and_skip_checks(
    opener, fake, pool
):
    g = BY_ID["G13"]
    fake.disagree_on = {"G13"}
    try:
        async with opener() as k:
            out = await k.submit(g["file"])
    finally:
        fake.disagree_on = set()
    assert out.paused and out.state == "ON_HOLD"
    assert out.request["flags"] == ["EXTRACTION_DISAGREEMENT"]
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT extracted FROM invoices WHERE id = %s", (out.invoice_id,)
            )
        ).fetchone()
        checks = await (
            await conn.execute(
                "SELECT count(*) AS n FROM tool_calls WHERE invoice_id = %s "
                "AND tool IN ('validate_gst', 'three_way_match')",
                (out.invoice_id,),
            )
        ).fetchone()
    readings = row["extracted"]["readings"]
    assert readings["disagreements"]["total"]["b"] == "99999.00"
    assert readings["a"]["fields"]["total"] != readings["b"]["fields"]["total"]
    assert checks["n"] == 0


async def test_providers_down_stops_the_run_and_retry_finishes_it(opener, fake):
    g = BY_ID["G16"]
    fake.down = True
    try:
        async with opener() as k:
            with pytest.raises(ExtractionFailed):
                await k.submit(g["file"])
            async with k.pool.connection() as conn:
                row = await (
                    await conn.execute(
                        "SELECT id, state FROM invoices WHERE source_file LIKE %s",
                        (f"%{g['file'].rsplit('/', 1)[-1]}",),
                    )
                ).fetchone()
    finally:
        fake.down = False
    assert row["state"] == "RECEIVED"
    async with opener() as k:
        out = await k.retry(row["id"])
    assert out.paused and out.state == g["expected"]["gate_state"] == "ON_HOLD"
