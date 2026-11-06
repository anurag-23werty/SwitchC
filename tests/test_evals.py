"""The eval's exact-match metrics: they must catch what they claim to catch."""

from __future__ import annotations

import copy

from evals.run import compute, field_correct

from .conftest import GOLDEN


def perfect() -> list[dict]:
    """Results that match every label exactly."""
    out = []
    for g in GOLDEN:
        e = g["expected"]
        gate = "APPROVED" if e["gate_state"] == "PAYMENT_DRAFTED" else e["gate_state"]
        reading = {"fields": g["truth"], "model": "m", "instructions_in_document": []}
        out.append(
            {
                "id": g["id"],
                "gate": gate,
                "state": e["final_state"],
                "flags": sorted(e["flags"]),
                "route": e["route"],
                "human_decisions": len(e["human"]),
                "extracted": {
                    "agreed": g["truth"],
                    "readings": {"a": reading, "b": reading, "disagreements": {}},
                },
                "notes": [
                    {
                        "kind": "analysis",
                        "content": {
                            "suggested_action": "investigate",
                            "precedents": [{"invoice_no": "MST/INV/7731"}],
                        },
                    }
                ],
                "llm_calls": [],
                "seconds": 1.0,
                "error": None,
            }
        )
    return out


def test_perfect_results_score_100():
    m = compute(GOLDEN, perfect())
    assert (
        m["decision_accuracy"] == m["routing_accuracy"] == m["flag_accuracy"] == 100.0
    )
    assert m["fraud_recall"]["pct"] == m["injection_resistance"]["pct"] == 100.0
    assert m["duplicate_recall"]["pct"] == 100.0 and m["false_hold_rate"]["pct"] == 0.0
    assert m["mismatches"] == []


def test_auto_paid_bank_change_fails_fraud_recall():
    res = perfect()
    d08 = next(r for r in res if r["id"] == "D08")
    d08.update(gate="APPROVED", state="PAYMENT_DRAFTED", flags=[], human_decisions=0)
    m = compute(GOLDEN, res)
    assert m["fraud_recall"]["pct"] < 100 and "D08" in m["fraud_recall"]["missed"]


def test_injection_that_changed_the_reading_is_not_resisted():
    res = perfect()
    d09 = next(r for r in res if r["id"] == "D09")
    d09["extracted"] = copy.deepcopy(d09["extracted"])
    d09["extracted"]["agreed"]["bank_account"] = "11112222333"
    m = compute(GOLDEN, res)
    assert "D09" in m["injection_resistance"]["missed"]


def test_analyst_suggesting_approval_of_an_injection_is_not_resisted():
    res = perfect()
    g20 = next(r for r in res if r["id"] == "G20")
    g20["notes"] = [
        {
            "kind": "analysis",
            "content": {"suggested_action": "approve", "precedents": []},
        }
    ]
    assert "G20" in compute(GOLDEN, res)["injection_resistance"]["missed"]


def test_false_hold_is_counted():
    res = perfect()
    d01 = next(r for r in res if r["id"] == "D01")
    d01.update(gate="ON_HOLD", state="ON_HOLD", flags=["LOW_CONFIDENCE"])
    m = compute(GOLDEN, res)
    assert m["false_hold_rate"]["ids"] == ["D01"] and m["decision_accuracy"] < 100


def test_field_comparison_normalises_format_not_meaning():
    truth = GOLDEN[0]["truth"]
    same = {
        **truth,
        "supplier_gstin": " " + truth["supplier_gstin"].lower(),
        "total": truth["total"] + "0",
    }
    assert field_correct("supplier_gstin", same, truth) and field_correct(
        "total", same, truth
    )
    off = {**truth, "total": str(float(truth["total"]) + 1)}
    assert not field_correct("total", off, truth)
    fewer = {**truth, "lines": truth["lines"][:-1]}
    assert not field_correct("lines", fewer, truth) or len(truth["lines"]) == 0
