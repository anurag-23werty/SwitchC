"""The demo data agrees with itself, with the policy, and with the printed PDFs.

These tests guard the *labels*. They don't test Khata's checks (that's Phase 2);
they catch a hand-labelling mistake before it poisons the eval scorecard.
"""

from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import pdfplumber
import pytest
import yaml

from data import fixtures as fx
from data.generate_invoices import file_name
from khata.checks.gst import is_valid_gstin
from khata.money import inr

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "evals" / "golden.yaml"
FLAGS = {
    "PRICE_VARIANCE",
    "QTY_VARIANCE",
    "DUPLICATE",
    "POSSIBLE_DUPLICATE",
    "GST_TYPE_MISMATCH",
    "GST_SPLIT_UNEQUAL",
    "GSTIN_INVALID",
    "ARITHMETIC",
    "BANK_CHANGE",
    "INJECTION",
    "FIRST_TIME_VENDOR",
    "PO_NOT_FOUND",
}
AUTO_BELOW = Decimal(fx.POLICY_V1["auto_approve_below"])
MANAGER_UP_TO = Decimal(fx.POLICY_V1["manager_up_to"])
ROLE = {p["username"]: p["role"] for p in fx.PEOPLE}


def test_master_gstins_are_valid():
    for party in [fx.COMPANY, *fx.VENDORS]:
        assert is_valid_gstin(party.gstin), party.code


def test_demo_has_nine_and_golden_has_thirty_plus():
    assert sum(s.demo for s in fx.INVOICES) == 9
    assert len(fx.INVOICES) >= 29
    assert len({s.key for s in fx.INVOICES}) == len(fx.INVOICES)


@pytest.mark.parametrize("spec", fx.INVOICES, ids=lambda s: s.key)
def test_labels_are_consistent_with_policy(spec):
    e, pr = spec.expected, fx.printed(spec)
    assert set(e.flags) <= FLAGS
    assert e.route in {"auto", "manager", "manager_then_owner", "owner", "none"}
    if e.route == "auto":
        assert not e.flags and pr.total < AUTO_BELOW and not e.human
        assert e.gate_state == e.final_state == "PAYMENT_DRAFTED"
    if e.route == "manager_then_owner":
        assert pr.total > MANAGER_UP_TO
        assert [ROLE[h.by] for h in e.human] == ["finance_manager", "owner"]
    if e.route == "owner":
        assert "BANK_CHANGE" in e.flags and [ROLE[h.by] for h in e.human] == ["owner"]
    if e.route == "none":
        assert e.gate_state == e.final_state == "REJECTED" and not e.human
    if e.gate_state in {"AWAITING_APPROVAL", "ON_HOLD"}:
        assert e.human, "a held invoice needs a human reply to finish"
        final = "PAYMENT_DRAFTED" if e.human[-1].decision == "approve" else "REJECTED"
        assert e.final_state == final


def test_bank_flag_matches_printed_bank():
    for spec in fx.INVOICES:
        changed = fx.printed(spec).bank != spec.vendor.bank
        assert changed == ("BANK_CHANGE" in spec.expected.flags), spec.key


def test_no_accidental_duplicates():
    """Same vendor + same total within 7 days must be labelled as a duplicate."""
    seen: dict[tuple[str, Decimal], list] = defaultdict(list)
    seen_no: set[tuple[str, str]] = set()
    for h in fx.HISTORY:
        seen_no.add((h.vendor.code, h.invoice_no))
    for spec in fx.INVOICES:
        total = fx.printed(spec).total
        dup_no = (spec.vendor.code, spec.invoice_no) in seen_no
        dup_total = any(
            abs((spec.invoice_date - d).days) <= 7
            for d in seen[(spec.vendor.code, total)]
        )
        flags = spec.expected.flags
        assert dup_no == ("DUPLICATE" in flags), spec.key
        if not dup_no:
            assert dup_total == ("POSSIBLE_DUPLICATE" in flags), spec.key
        seen_no.add((spec.vendor.code, spec.invoice_no))
        seen[(spec.vendor.code, total)].append(spec.invoice_date)


def test_golden_yaml_matches_fixtures():
    golden = yaml.safe_load(GOLDEN.read_text())["invoices"]
    assert [g["id"] for g in golden] == [s.key for s in fx.INVOICES]
    for g, spec in zip(golden, fx.INVOICES, strict=True):
        assert g["expected"]["flags"] == list(spec.expected.flags)
        assert Decimal(g["truth"]["total"]) == fx.printed(spec).total


@pytest.mark.parametrize("spec", fx.INVOICES, ids=lambda s: s.key)
def test_pdf_text_carries_the_truth(spec):
    path = ROOT / "data" / "invoices" / file_name(spec)
    assert path.exists(), "run `make seed` first"
    with pdfplumber.open(path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    pr = fx.printed(spec)
    assert spec.invoice_no in text
    assert pr.supplier_gstin in text
    assert inr(pr.total) in text
    assert pr.bank is not None and pr.bank.account in text
