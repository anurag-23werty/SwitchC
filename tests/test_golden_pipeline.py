"""The deterministic workflow against all 31 hand-labelled invoices, end to end.

Each invoice is read from its PDF, given a perfect extraction (golden `truth`),
run through every check via the ToolGateway as the owning agent, routed by the
policy in the database, answered by the labelled human replies, and paid by
Payer. Flags, route, gate state and final state must all match the labels.

This isolates the money logic from the LLMs: Phase 3 swaps the perfect
extraction for two models, and the eval measures what that changes.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pytest
import pytest_asyncio

from khata.pipeline import run_checks
from khata.policy import PermissionDenied, ToolGateway
from khata.tools import DecisionRefused, Toolbox, ingest, save_extraction

from .conftest import GOLDEN, ROOT, invoice_from_truth

pytestmark = pytest.mark.asyncio(loop_scope="module")
EXPECT_GATE = {
    "PAYMENT_DRAFTED": "APPROVED"
}  # labels count the Payer step; the gate is APPROVED


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def run(pool, tmp_path_factory):
    """Process all 31 invoices once; tests below inspect the results."""
    pay_dir = tmp_path_factory.mktemp("payments")
    box = Toolbox(
        pool, files_root=ROOT / "data", payments_dir=pay_dir, today=date(2026, 11, 6)
    )
    gw = ToolGateway(pool, box.as_dict())
    results = {}
    for g in GOLDEN:
        doc = await gw.call(
            "intake", "extract_invoice", file_path=str(ROOT / g["file"])
        )
        inv_id = await ingest(
            pool,
            file_path=g["file"],
            text=doc["text"],
            sha256=doc["sha256"],
            room_id=None,
            thread_id=None,
        )
        await save_extraction(
            pool,
            invoice_id=inv_id,
            data=invoice_from_truth(g["truth"]),
            readings={"truth": True},
            confidence=1,
            actor="khata-intake",
        )
        out = await run_checks(gw, inv_id)
        gate = out["route"]["state"]
        for h in g["expected"]["human"]:
            await gw.call(
                "router",
                "record_decision",
                invoice_id=inv_id,
                username=h["by"],
                decision=h["decision"],
                reason=h["reason"],
            )
        state = (await gw.call("analyst", "get_invoice", invoice_id=inv_id))["state"]
        if state == "APPROVED":
            await gw.call("payer", "draft_payment", invoice_ids=[inv_id])
        final = await gw.call("analyst", "get_invoice", invoice_id=inv_id)
        results[g["id"]] = {
            "id": inv_id,
            "route": out["route"],
            "gate": gate,
            "final": final,
        }
    return {"gw": gw, "results": results, "pay_dir": pay_dir}


@pytest.mark.parametrize("g", GOLDEN, ids=lambda g: g["id"])
async def test_outcome_matches_label(run, g):
    r, e = run["results"][g["id"]], g["expected"]
    assert sorted(r["final"]["flags"]) == sorted(e["flags"]), "flags"
    assert r["route"]["route"] == e["route"], "route"
    assert r["gate"] == EXPECT_GATE.get(e["gate_state"], e["gate_state"]), "gate state"
    assert r["final"]["state"] == e["final_state"], "final state"


async def test_bank_change_carries_callback_instruction(run):
    d08 = run["results"]["D08"]["route"]
    assert d08["hard_stop"] and "+91 80 2797 1180" in d08["callback_instruction"]


async def test_payments_go_to_master_accounts_only(run):
    rows = []
    for f in Path(run["pay_dir"]).glob("*.csv"):
        with f.open() as fh:
            rows += list(csv.DictReader(fh))
    accounts = {r["Beneficiary Account Number"] for r in rows}
    fraud = {"921020045678123", "7412589630", "029384756102"}
    assert rows and not (accounts & fraud)
    paid = sum(1 for g in GOLDEN if g["expected"]["final_state"] == "PAYMENT_DRAFTED")
    assert len(rows) == paid


async def test_payment_draft_is_idempotent(run):
    d01 = run["results"]["D01"]["id"]
    again = await run["gw"].call("payer", "draft_payment", invoice_ids=[d01])
    assert again["drafted"] == [] and again["already_drafted"][0]["invoice_id"] == d01


async def test_cannot_pay_a_rejected_invoice(run):
    with pytest.raises(DecisionRefused):
        await run["gw"].call(
            "payer", "draft_payment", invoice_ids=[run["results"]["D08"]["id"]]
        )


async def test_out_of_scope_tool_call_is_denied_and_logged(run, pool):
    gw = run["gw"]
    with pytest.raises(PermissionDenied):
        await gw.call("analyst", "draft_payment", invoice_ids=[1])
    with pytest.raises(PermissionDenied):
        await gw.call(
            "intake",
            "record_decision",
            invoice_id=1,
            username="priya",
            decision="approve",
            reason="injected",
        )
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT agent, tool FROM tool_calls WHERE NOT allowed ORDER BY id"
            )
        ).fetchall()
    assert {(r["agent"], r["tool"]) for r in rows} >= {
        ("analyst", "draft_payment"),
        ("intake", "record_decision"),
    }


async def test_manager_cannot_approve_the_owner_step(pool, run):
    """G12 needs Priya then the owner. Replaying Priya's approval for the owner step is refused."""
    box = Toolbox(
        pool,
        files_root=ROOT / "data",
        payments_dir=run["pay_dir"],
        today=date(2026, 11, 6),
    )
    gw = ToolGateway(pool, box.as_dict())
    g = next(x for x in GOLDEN if x["id"] == "G12")
    doc = await gw.call("intake", "extract_invoice", file_path=str(ROOT / g["file"]))
    inv_id = await ingest(
        pool,
        file_path=g["file"],
        text=doc["text"],
        sha256=doc["sha256"],
        room_id=None,
        thread_id=None,
    )
    data = invoice_from_truth(g["truth"]).model_copy(
        update={"invoice_no": "MST/INV/7744-B"}
    )
    await save_extraction(
        pool, invoice_id=inv_id, data=data, readings={}, confidence=1, actor="t"
    )
    await run_checks(gw, inv_id)
    ok = await gw.call(
        "router",
        "record_decision",
        invoice_id=inv_id,
        username="priya",
        decision="approve",
        reason="fine",
    )
    assert ok["remaining_steps"] == ["owner"]
    with pytest.raises(DecisionRefused):
        await gw.call(
            "router",
            "record_decision",
            invoice_id=inv_id,
            username="priya",
            decision="approve",
            reason="me again",
        )
    with pytest.raises(DecisionRefused):
        await gw.call(
            "router",
            "record_decision",
            invoice_id=inv_id,
            username="ravi",
            decision="reject",
            reason="clerk",
        )


async def test_every_transition_is_audited(run, pool):
    d03 = run["results"]["D03"]["id"]
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT to_state FROM audit_log WHERE invoice_id = %s AND action = 'transition' "
                "ORDER BY id",
                (d03,),
            )
        ).fetchall()
    assert [r["to_state"] for r in rows] == [
        "EXTRACTED",
        "VALIDATED",
        "MATCHED",
        "REVIEWED",
        "AWAITING_APPROVAL",
        "APPROVED",
        "PAYMENT_DRAFTED",
    ]
