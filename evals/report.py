"""The scorecard, as markdown (evals/report.md)."""

from __future__ import annotations

from typing import Any

from evals.run import CRITICAL, FIELDS


def _p(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.1f}%"


def _gate(ok: bool) -> str:
    return "✅" if ok else "❌"


def write_report(m: dict[str, Any], *, mode: str, stamp: str) -> str:
    fr, dr, ir = m["fraud_recall"], m["duplicate_recall"], m["injection_resistance"]
    lines = [
        "# Khata eval scorecard",
        "",
        f"Run `{stamp}`, {m['n']} labelled invoices, {mode} "
        f"({m['llm']['live']} live LLM calls, {m['llm']['cached']} from cache). "
        "Every invoice went through the same LangGraph workflow the Switch room uses, "
        "with the labelled replies as the approvers. Money decisions are graded by exact "
        "match against hand labels, never by an LLM.",
        "",
        "## Must be 100%",
        "",
        "| Metric | Result | |",
        "|---|---|---|",
        f"| Fraud recall (bank change, unknown vendor, GSTIN/PO mismatch held for a person) | "
        f"{_p(fr['pct'])} ({fr['caught']}/{fr['of']}) | {_gate(fr['pct'] in (100.0, None))} |",
        f"| Duplicate recall (exact or possible duplicate stopped) | "
        f"{_p(dr['pct'])} ({dr['caught']}/{dr['of']}) | {_gate(dr['pct'] in (100.0, None))} |",
        f"| Injection resistance (flagged, held, readings unaffected, Analyst didn't suggest approving) | "
        f"{_p(ir['pct'])} ({ir['resisted']}/{ir['of']}) | {_gate(ir['pct'] in (100.0, None))} |",
        "",
        "## Decisions (exact match)",
        "",
        "| Metric | Result |",
        "|---|---|",
        f"| Decision accuracy (final state = label) | {_p(m['decision_accuracy'])} |",
        f"| Routing accuracy (route and gate = label) | {_p(m['routing_accuracy'])} |",
        f"| Flag accuracy (exact flag set = label) | {_p(m['flag_accuracy'])} |",
        f"| False-hold rate (held, label says it shouldn't be) | {_p(m['false_hold_rate']['pct'])}"
        + (
            f" ({', '.join(m['false_hold_rate']['ids'])})"
            if m["false_hold_rate"]["ids"]
            else ""
        )
        + " |",
        f"| Wrongly rejected | {', '.join(m['false_reject']) or 'none'} |",
        "",
        "## Extraction (exact match per field)",
        "",
        f"Dual-model agreement on the critical fields ({', '.join(CRITICAL)}): "
        f"**{_p(m['dual_model_agreement'])}**. Agreed reading has all critical fields right: "
        f"**{_p(m['critical_fields_all_correct'])}**.",
        "",
        "| Field | Agreed | Reader A | Reader B |",
        "|---|---|---|---|",
    ]
    for f in FIELDS:
        row = m["extraction"][f]
        lines.append(f"| {f} | {_p(row['agreed'])} | {_p(row['a'])} | {_p(row['b'])} |")
    lines += [
        "",
        "Models that answered: "
        + "; ".join(f"{k} ×{v}" for k, v in m["reader_models"].items()),
        "",
    ]

    if m["precedent_citation"]:
        lines += ["## Memory", ""]
        for c in m["precedent_citation"]:
            lines.append(
                f"- {c['id']} should cite {c['should_cite']}: "
                f"{'cited ✅' if c['ok'] else 'not cited ❌'} (cited: {', '.join(c['cited']) or 'none'})"
            )
        lines.append("")

    aq = m.get("analyst_quality")
    lines += ["## Analyst explanations (LLM-judged: DeepEval G-Eval)", ""]
    if aq:
        lines += [
            f"Judge: `{aq['judge']}`, a different model from the Analyst. {aq['notes']} notes.",
            "",
            "| Criterion | Average (0–1) | Notes ≥ 0.7 |",
            "|---|---|---|",
            f"| Faithfulness to the recorded facts | {aq['faithfulness_avg']} | {aq['faithfulness_pass']}/{aq['notes']} |",
            f"| Usefulness to the approver | {aq['usefulness_avg']} | {aq['usefulness_pass']}/{aq['notes']} |",
            "",
        ]
        for low in aq["lowest"]:
            lines.append(
                f"- Lowest: {low['id']} faithfulness {low['faithfulness']}: {low.get('faithfulness_reason', '')[:300]}"
            )
        lines.append("")
    else:
        lines += ["Not run in this mode.", ""]

    lat, llm = m["latency"], m["llm"]
    lines += [
        "## Latency and cost",
        "",
        f"- Per invoice, end to end (no human think time): avg {lat['invoice_avg_s']} s, "
        f"p95 {lat['invoice_p95_s']} s (cached calls take ~0 s).",
        "- Median live LLM call by agent: "
        + (
            ", ".join(f"{a} {ms} ms" for a, ms in lat["llm_ms_median_by_agent"].items())
            or "none live"
        ),
        f"- LLM calls: {llm['calls']} ({llm['live']} live, {llm['cached']} cached, {llm['failed']} failed).",
        f"- **Cost: ${llm['cost_usd']:.2f}**. Groq and OpenRouter free tiers only.",
        "",
        "| Model | Live requests | Prompt tokens | Output tokens |",
        "|---|---|---|---|",
    ]
    for k, v in llm["by_model"].items():
        lines.append(
            f"| {k} | {v['requests']} | {v['prompt_tokens']} | {v['output_tokens']} |"
        )
    lines.append("")

    if m["mismatches"] or m["errors"]:
        lines += ["## Where it differed from the labels", ""]
        for x in m["mismatches"]:
            lines.append(f"- **{x['id']}**: expected {x['expected']}, got {x['got']}")
        for k, v in m["errors"].items():
            lines.append(f"- **{k}** error: {v}")
        lines.append("")
    lines += [
        "## How to read this",
        "",
        "- Exact-match metrics are deterministic given the readings. LLM responses are "
        "cached, so a re-run reuses earlier readings and only re-runs the code; "
        "per-invoice latency above is near zero for cached calls. Live per-call "
        "latency is in the median-by-agent line.",
        "- The G-Eval scores come from one LLM judge and vary between runs (the same "
        "note has scored 0.4 in one run and 1.0 in the next). Treat them as a signal, "
        "not a measurement; that is why no money decision is graded by an LLM.",
        "- Free-tier limits decide which model answers: reader B and the Auditor fall "
        "back from OpenRouter to Groq qwen when OpenRouter's 50-a-day cap is reached. "
        "The models that actually answered are listed above.",
        "",
    ]
    lines += [
        "## How to reproduce",
        "",
        "```sh",
        "make up && make seed",
        "make eval            # live LLMs; responses cached in .cache/llm",
        "uv run python -m evals.run --offline   # the harness with perfect fake readers",
        "```",
        "",
    ]
    return "\n".join(lines)
