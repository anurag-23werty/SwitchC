"""Policy Steward: a repeated, human-approved exception becomes a rule proposal; only
the owner can turn it into a new policy version; the new version is then applied.

Offline: real graph, gateway, tools and Postgres; fake LLM providers.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio

from data import fixtures as fx
from data.generate_invoices import golden_entry
from khata import steward
from khata.agents.schemas import parse_qty
from khata.approval import route_approval
from khata.models import Flag, Policy, State, Vendor
from khata.runtime import Khata

from .conftest import GOLDEN, ROOT
from .llm_fakes import FakeProviders

on_loop = pytest.mark.asyncio(loop_scope="module")
BY_ID = {g["id"]: g for g in GOLDEN}
S01 = golden_entry(99, fx.STEWARD_INVOICES[0])

MST = Vendor(
    id=3,
    code="MST",
    name="Mumbai Steel Traders",
    gstin="27AAHFM3390Q1ZL",
    state_code="27",
    phone="x",
    email="x",
    bank_name="x",
    bank_account="1",
    ifsc="MAHB0000123",
)


def policy(**kw) -> Policy:
    base = dict(
        version=2,
        auto_approve_below=Decimal("10000"),
        manager_up_to=Decimal("100000"),
        variance_pct=Decimal("2.0"),
        duplicate_window_days=7,
        arithmetic_tolerance=Decimal("1.00"),
        approvers={"finance_manager": "priya", "owner": "owner"},
        vendor_exceptions=[
            {
                "vendor_code": "MST",
                "flag": "PRICE_VARIANCE",
                "max_variance_pct": "4.00",
                "evidence": ["MST/INV/7731", "MST/INV/7802"],
            }
        ],
    )
    return Policy(**{**base, **kw})


# --- the policy engine with a vendor exception (pure) -------------------------


def test_exception_accepts_variance_within_the_agreed_percentage():
    d = route_approval(
        [Flag.PRICE_VARIANCE], Decimal("7363.20"), policy(), MST, Decimal("4.00")
    )
    assert d.gate_state == State.APPROVED and d.route == "auto"
    assert any("within the 4.00%" in r for r in d.reasons)


def test_exception_does_not_cover_a_bigger_variance_or_another_vendor():
    over = route_approval(
        [Flag.PRICE_VARIANCE], Decimal("7363.20"), policy(), MST, Decimal("4.01")
    )
    assert over.route == "manager"
    other = MST.model_copy(update={"code": "SOS"})
    assert (
        route_approval(
            [Flag.PRICE_VARIANCE], Decimal("7000"), policy(), other, Decimal("1")
        ).route
        == "manager"
    )
    unmeasured = route_approval(
        [Flag.PRICE_VARIANCE], Decimal("7000"), policy(), MST, None
    )
    assert unmeasured.route == "manager"


def test_exception_never_relaxes_other_flags_or_amount_limits():
    pct = Decimal("3")
    qty = route_approval(
        [Flag.PRICE_VARIANCE, Flag.QTY_VARIANCE], Decimal("7000"), policy(), MST, pct
    )
    assert qty.route == "manager"
    bank = route_approval(
        [Flag.PRICE_VARIANCE, Flag.BANK_CHANGE], Decimal("7000"), policy(), MST, pct
    )
    assert bank.hard_stop and bank.route == "owner"
    held = route_approval(
        [Flag.PRICE_VARIANCE, Flag.INJECTION], Decimal("7000"), policy(), MST, pct
    )
    assert held.gate_state == State.ON_HOLD
    big = route_approval([Flag.PRICE_VARIANCE], Decimal("58905.60"), policy(), MST, pct)
    assert big.route == "manager"
    huge = route_approval([Flag.PRICE_VARIANCE], Decimal("150000"), policy(), MST, pct)
    assert huge.steps == ["manager", "owner"]


def test_quantity_with_its_unit_is_read_strictly():
    assert parse_qty("5 nos", None) == (Decimal("5"), "nos")
    assert parse_qty("1,200.5", "kg") == (Decimal("1200.5"), "kg")
    for bad in ("5 x 2", "five", "5 nos extra"):
        with pytest.raises(ValueError):
            parse_qty(bad, None)


# --- end to end ---------------------------------------------------------------


@pytest.fixture(scope="module")
def opener(test_db_url, tmp_path_factory):
    fake = FakeProviders([*GOLDEN, S01], ROOT)
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


async def _approve(k: Khata, gid: str):
    g = BY_ID[gid]
    out = await k.submit(g["file"])
    for h in g["expected"]["human"]:
        out = await k.decide(out.invoice_id, h["by"], h["decision"], h["reason"])
    return out


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def flow(opener):
    async with opener() as k:
        d03 = await _approve(k, "D03")
        d04 = await _approve(k, "D04")
        yield {"k": k, "d03": d03, "d04": d04}


@on_loop
async def test_one_approval_is_not_a_pattern_two_are(flow):
    assert flow["d03"].values.get("proposal") is None
    p = flow["d04"].values["proposal"]
    assert p["status"] == "proposed" and p["proposed_by"] == "khata-steward"
    assert [e["invoice_no"] for e in p["evidence"]] == ["MST/INV/7731", "MST/INV/7802"]
    assert Decimal(p["max_variance_pct"]) == Decimal("4.00")
    assert "@owner" in p["message"] and "Only the owner" in p["message"]
    assert any(line["agent"] == "khata-steward" for line in flow["d04"].log)


@on_loop
async def test_the_steward_does_not_propose_twice(flow, pool):
    async with pool.connection() as conn, conn.transaction():
        res = await steward.propose(conn, flow["d04"].invoice_id)
    assert not res["proposed"] and "already proposed" in res["reason"]


@on_loop
async def test_the_steward_never_proposes_beyond_its_cap(flow, pool):
    async with pool.connection() as conn:
        async with conn.transaction(force_rollback=True):
            await conn.execute(
                """UPDATE rules SET policy = jsonb_set(policy, '{steward,max_variance_pct}',
                                                      '"3.0"') WHERE active"""
            )
            await conn.execute("UPDATE rule_proposals SET status = 'superseded'")
            res = await steward.propose(conn, flow["d04"].invoice_id)
    assert not res["proposed"] and "above the 3.0%" in res["reason"]


@on_loop
async def test_only_the_owner_decides_and_a_rejection_sticks(flow, pool):
    pid = flow["d04"].values["proposal"]["id"]
    async with pool.connection() as conn:
        for who in ("priya", "ravi", "stranger"):
            with pytest.raises(steward.ProposalRefused, match="only the owner"):
                async with conn.transaction():
                    await steward.decide(conn, pid, who, "approve", "looks fine")
        with pytest.raises(steward.ProposalRefused, match="reason"):
            async with conn.transaction():
                await steward.decide(conn, pid, "owner", "approve", "")
        async with conn.transaction(force_rollback=True):
            res = await steward.decide(
                conn, pid, "owner", "reject", "Not yet, ask Ravi"
            )
            assert res["status"] == "rejected" and res["new_version"] is None
            again = await steward.propose(conn, flow["d04"].invoice_id)
            assert not again["proposed"] and "rejected" in again["reason"]
            active = await (
                await conn.execute("SELECT version FROM rules WHERE active")
            ).fetchone()
            assert active["version"] == 1


@on_loop
async def test_owner_approval_writes_v2_and_s01_is_auto_approved(flow, pool):
    k = flow["k"]
    pid = flow["d04"].values["proposal"]["id"]
    res = await k.decide_rule(pid, "owner", "approve", "Freight up to 4% is agreed")
    assert res["status"] == "approved" and res["new_version"] == 2
    with pytest.raises(steward.ProposalRefused, match="already approved"):
        await k.decide_rule(pid, "owner", "approve", "twice")

    out = await k.submit(S01["file"])
    assert out.state == "PAYMENT_DRAFTED" and not out.paused
    assert out.values["route"]["route"] == "auto"
    assert out.values.get("proposal") is None  # the rule already covers it

    async with pool.connection() as conn:
        rules = await (
            await conn.execute(
                "SELECT version, active, proposed_by, approved_by FROM rules ORDER BY version"
            )
        ).fetchall()
        auto = await (
            await conn.execute(
                "SELECT decided_by, rule_version, reason FROM decisions WHERE invoice_id = %s",
                (out.invoice_id,),
            )
        ).fetchone()
        old = await (
            await conn.execute(
                "SELECT rule_version FROM decisions WHERE invoice_id = %s",
                (flow["d03"].invoice_id,),
            )
        ).fetchone()
        flags = await (
            await conn.execute(
                "SELECT flags FROM invoices WHERE id = %s", (out.invoice_id,)
            )
        ).fetchone()
        note = await (
            await conn.execute(
                "SELECT content FROM vendor_knowledge WHERE kind = 'pattern' ORDER BY id DESC"
            )
        ).fetchone()
    assert [(r["version"], r["active"]) for r in rules] == [(1, False), (2, True)]
    assert (
        rules[1]["proposed_by"] == "khata-steward"
        and rules[1]["approved_by"] == "owner"
    )
    assert auto["decided_by"] == "policy" and auto["rule_version"] == 2
    assert "MST/INV/7731" in auto["reason"]
    assert (
        old["rule_version"] == 1
    )  # past decisions keep the version they were made under
    assert flags["flags"] == ["PRICE_VARIANCE"]  # still recorded, just accepted
    assert "Policy v2" in note["content"]
