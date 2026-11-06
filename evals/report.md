# Khata eval scorecard

Run `20261106-211447`, 31 labelled invoices, live (6 live LLM calls, 94 from cache). Every invoice went through the same LangGraph workflow the Switch room uses, with the labelled replies as the approvers. Money decisions are graded by exact match against hand labels, never by an LLM.

## Must be 100%

| Metric | Result | |
|---|---|---|
| Fraud recall (bank change, unknown vendor, GSTIN/PO mismatch held for a person) | 100.0% (3/3) | ✅ |
| Duplicate recall (exact or possible duplicate stopped) | 100.0% (3/3) | ✅ |
| Injection resistance (flagged, held, readings unaffected, Analyst didn't suggest approving) | 100.0% (2/2) | ✅ |

## Decisions (exact match)

| Metric | Result |
|---|---|
| Decision accuracy (final state = label) | 100.0% |
| Routing accuracy (route and gate = label) | 100.0% |
| Flag accuracy (exact flag set = label) | 100.0% |
| False-hold rate (held, label says it shouldn't be) | 0.0% |
| Wrongly rejected | none |

## Extraction (exact match per field)

Dual-model agreement on the critical fields (total, supplier_gstin, invoice_no, bank_account): **100.0%**. Agreed reading has all critical fields right: **100.0%**.

| Field | Agreed | Reader A | Reader B |
|---|---|---|---|
| vendor_name | 100.0% | 100.0% | 100.0% |
| supplier_gstin | 100.0% | 100.0% | 100.0% |
| buyer_gstin | 100.0% | 96.8% | 100.0% |
| invoice_no | 100.0% | 100.0% | 100.0% |
| invoice_date | 100.0% | 100.0% | 100.0% |
| po_number | 100.0% | 100.0% | 100.0% |
| taxable_value | 100.0% | 100.0% | 100.0% |
| cgst | 100.0% | 100.0% | 100.0% |
| sgst | 100.0% | 100.0% | 100.0% |
| igst | 100.0% | 100.0% | 100.0% |
| total | 100.0% | 100.0% | 100.0% |
| bank_account | 100.0% | 100.0% | 100.0% |
| ifsc | 100.0% | 100.0% | 100.0% |
| lines | 96.8% | 80.6% | 100.0% |

Models that answered: reader a: openai/gpt-oss-120b ×31; reader b: nvidia/nemotron-3.5-super-vl:free ×24; reader b: xingchen-agi/xing4.0-29b-a4b:free ×1; reader b: qwen/qwen3.8-27b ×6

## Memory

- D04 should cite MST/INV/7731: cited ✅ (cited: MST/INV/7731)

## Analyst explanations (LLM-judged: DeepEval G-Eval)

Judge: `groq/qwen/qwen3.8-27b`, a different model from the Analyst. 19 notes.

| Criterion | Average (0–1) | Notes ≥ 0.7 |
|---|---|---|
| Faithfulness to the recorded facts | 1.0 | 19/19 |
| Usefulness to the approver | 0.89 | 16/19 |

- Lowest: D02 faithfulness 1.0: The response accurately reflects the input data: it correctly identifies the invoice amount (Rs 48,144.00) and the approval route (Finance Manager) based on the policy outcome. It correctly summarizes all automated check findings (GST, injection, bank, duplicate, three-way match) without inventing n
- Lowest: D03 faithfulness 1.0: The response accurately reflects the input facts: it correctly identifies the freight charge (Rs 2,400) not on the PO, the 4.00% price variance exceeding the 2.0% limit, and the correct status (AWAITING_APPROVAL). It correctly cites the check findings for GST, bank details, and duplicates. The sugge
- Lowest: D04 faithfulness 1.0: The response accurately reflects the input facts, correctly identifying the freight charge (Rs 1,920) and the 4.00% price variance against the 2.0% limit. It correctly cites the specific precedent (MST/INV/7731) from the knowledge base regarding freight approval. The suggested action of 'investigate

## Latency and cost

- Per invoice, end to end (no human think time): avg 2.24 s, p95 9.76 s (cached calls take ~0 s).
- Median live LLM call by agent: analyst 1899 ms, auditor 11370 ms
- LLM calls: 100 (6 live, 94 cached, 0 failed).
- **Cost: $0.00**. Groq and OpenRouter free tiers only.

| Model | Live requests | Prompt tokens | Output tokens |
|---|---|---|---|
| groq openai/gpt-oss-120b | 2 | 3048 | 924 |
| groq qwen/qwen3.8-27b | 4 | 6609 | 706 |

## How to read this

- Exact-match metrics are deterministic given the readings. LLM responses are cached, so a re-run reuses earlier readings and only re-runs the code; per-invoice latency above is near zero for cached calls. Live per-call latency is in the median-by-agent line.
- The G-Eval scores come from one LLM judge and vary between runs (the same note has scored 0.4 in one run and 1.0 in the next). Treat them as a signal, not a measurement; that is why no money decision is graded by an LLM.
- Free-tier limits decide which model answers: reader B and the Auditor fall back from OpenRouter to Groq qwen when OpenRouter's 50-a-day cap is reached. The models that actually answered are listed above.

## How to reproduce

```sh
make up && make seed
make eval            # live LLMs; responses cached in .cache/llm
uv run python -m evals.run --offline   # the harness with perfect fake readers
```
