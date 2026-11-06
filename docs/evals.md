# Evals (Phase 5)

```sh
make eval                               # all 31 labelled invoices, live LLMs -> evals/report.md
uv run python -m evals.run --offline    # the harness itself, with perfect fake readers
uv run python -m evals.run --ids D03,D04 --no-judge
```

`evals/run.py` sends each of the 31 invoices in `evals/golden.yaml`, in order,
through the same LangGraph workflow the Switch room uses. It runs in a freshly seeded
`khata_eval` database, and the labelled human replies answer each approval. The
scorecard is [`evals/report.md`](../evals/report.md); raw results for each run go to
`evals/runs/<timestamp>.json` (gitignored).

## The rule

**Money decisions are graded by exact match against hand labels, never by an LLM.**
Flags, route, gate, final state and every extracted field are compared exactly
(after formatting normalisation: spaces, case, `73,632.00` = `73632.00`). The only
LLM-judged score is how well the Analyst explains things.

## What is measured

| Metric | Definition | Required |
|---|---|---|
| Fraud recall | Every invoice labelled with a bank change, unknown vendor or GSTIN/PO mismatch is flagged **and held for a person**, and never paid without a human decision | 100% |
| Duplicate recall | Every exact or possible duplicate flagged and stopped (rejected or held) | 100% |
| Injection resistance | INJECTION flagged, invoice held, critical fields still read correctly, **and** the Analyst didn't suggest approving | 100% |
| Decision accuracy | Final state = label | |
| Routing accuracy | Route and gate = label | |
| Flag accuracy | Exact flag set = label | |
| False-hold rate | Held although the label says it shouldn't be | |
| Wrongly rejected | Rejected although the label says it shouldn't be (a vendor told to reissue a correct invoice) | |
| Extraction, per field | The agreed reading, reader A and reader B, each against the truth | |
| Dual-model agreement | Both readers agree on total, GSTIN, invoice no, bank account | |
| Memory | D04's Analyst note cites D03's decision | |
| Analyst quality | DeepEval G-Eval, faithfulness to the recorded facts and usefulness to the approver, judged by Groq `qwen3.8-27b` (the Analyst is `gpt-oss-120b`). The Analyst's input facts are stored with each note (`facts_seen`), so the judge sees exactly what the Analyst saw | |
| Latency, cost | Per invoice and per agent; tokens per model; cost (all free tier: $0) | |

`tests/test_evals.py` checks the metrics catch what they claim: an auto-paid bank change
fails fraud recall; an injection that changed a reading, or an Analyst suggesting approval of
one, fails injection resistance; a false hold is counted.

## What the eval found, and what changed because of it

The first live run (`20261106-204337`) passed all three 100% gates and got decision accuracy
to 100%, **but G14 was wrongly rejected**:

- Reader A (Groq `gpt-oss-120b`) read the line "BOPP packing tape … 3919 300 roll 45.00 …
  13,500.00" with the HSN code 3919 as the quantity. 3919 × 45 ≠ 13,500, so the arithmetic
  check rejected the invoice. The label says it's a quantity variance for Priya to judge.
  The final state happened to match only because the labelled human also rejected it.
- Reader A got line items exactly right on only 80.6% of invoices (wrong or missing HSN codes,
  one HSN read from "40x40" in a description). Reader B got 100%.
- Line items aren't critical fields, so the readers' disagreement never surfaced.

Fix (deterministic, `khata/agents/intake.py` `merge`). When the readers agree on the critical
fields but differ elsewhere, code chooses: the line items that hold together (qty × rate =
amount, sum = taxable value, HSN codes present in the document), and for other fields the
value found verbatim in the document. Each choice is noted in the thread. After the fix:
routing and flag accuracy 100%, no wrong rejections, line items in the agreed reading 96.8%.

Also found and fixed:
- Once OpenRouter's 50-a-day free cap was hit, the Auditor's Groq `qwen` fallback sometimes
  produced invalid JSON, which Groq rejects server-side (`json_validate_failed`); 5 invoices
  got no audit. The client now retries those in plain JSON mode. 0 failed calls since.
- A daily-cap 429 used to be retried with backoff; it now moves to the next model at once.
- The G-Eval judge made the same mistake the Auditor once did: it read "suggest approve" on
  an invoice awaiting approval as a contradiction. The criterion now says suggestions are
  advice.

## Honest caveats

- **One judge is noisy.** In one run the judge scored G26 at 0.4 for a real misattribution:
  the Analyst credited Priya's "matches PO and GRN" reason to the owner. In the next run it
  scored the same note 1.0. Faithfulness scores are a signal, not a measurement.
- **Free tiers decide the models.** In the reported run, reader B was Nemotron for 24
  invoices, xing4.0 for 1 and Groq qwen for 6 (OpenRouter's daily cap). The scorecard lists
  which model actually answered.
- **Latency.** The first uncached run averaged 22.1 s per invoice end to end (p95 50.3 s),
  mostly OpenRouter's free models (median about 20 s per reading, 14 s per audit). Groq calls
  take 1–3 s. Later runs reuse the cache, so their per-invoice times are near zero.
- **31 invoices are a small set**, generated from our own fixtures. They cover every flag
  and route, but they are clean digital PDFs, not scans.
