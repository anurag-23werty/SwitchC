# Tools, checks and the policy engine

Everything that touches money, tax or fraud in Khata is deterministic Python.
LLMs read documents and write explanations; they never compute an amount,
decide a route, or approve anything. This page describes that deterministic
layer: the checks, the approval router, the state machine, payment drafting,
the per-agent permissions, and the MCP server that exposes it all.

## The checks (`khata/checks/`)

| Check | File | Flags it can raise | Rule |
|---|---|---|---|
| GST & arithmetic | `gst.py` `validate_gst` | `GSTIN_INVALID`, `GST_TYPE_MISMATCH`, `GST_SPLIT_UNEQUAL`, `ARITHMETIC`, `BUYER_GSTIN_MISMATCH` | GSTIN regex + mod-36 checksum. Same state as buyer → CGST = SGST, no IGST; different state → IGST only. Each line qty × rate = amount; lines = taxable; tax at line rates = tax charged; taxable + tax = total. ₹1 tolerance (from the policy) |
| Three-way match | `matching.py` `three_way_match` | `PRICE_VARIANCE`, `QTY_VARIANCE`, `PO_NOT_FOUND` | Lines are paired to PO lines by HSN, then by description. Price: billed vs billed qty at PO prices, with any charge not on the PO (freight) counted as variance. Qty: billed vs received on the GRN. Flag above 2% (policy) |
| Duplicates | `duplicate.py` `check_duplicate` | `DUPLICATE`, `POSSIBLE_DUPLICATE` | Same vendor + invoice number (ignoring case and spaces) → duplicate. Same vendor + same total within 7 days (policy) → possible duplicate. Rejected invoices don't count, so a vendor can reissue |
| First-time vendor | in the `check_duplicate` tool | `FIRST_TIME_VENDOR` | No approved invoice from this vendor yet |
| Bank change | `bank.py` `check_bank_change` | `BANK_CHANGE` | Account or IFSC on the invoice ≠ vendor master. Accounts are masked in messages; the callback phone comes from the master |
| Prompt injection | `injection.py` `detect_injection` | `INJECTION` | 9 pattern families aimed at *machines* (override instructions, addressing an AI, claimed approvals, "mark as approved", "release payment immediately", "skip checks", "do not flag", role-play). Text is first normalised (NFKC, zero-width characters removed) to undo disguises. Social engineering aimed at *people* ("our bank details have changed") is deliberately not matched; the bank check catches it. With an LLM key, Llama Prompt Guard 2 also scores the text (see [agent-runtime.md](agent-runtime.md)) |

Vendor identification on extraction adds `VENDOR_UNKNOWN` (not in master) or
`GSTIN_MISMATCH` (a valid GSTIN that isn't the master's). The two-model reading
adds `EXTRACTION_DISAGREEMENT` and `LOW_CONFIDENCE` (see [agent-runtime.md](agent-runtime.md)).

## The approval router (`khata/approval.py`)

Flags + total + the **active policy from the `rules` table** in, a route out.
Strongest rule first:

| # | When | Outcome | Who |
|---|---|---|---|
| 1 | Any of `GSTIN_INVALID`, `GST_TYPE_MISMATCH`, `GST_SPLIT_UNEQUAL`, `ARITHMETIC`, `DUPLICATE`, `BUYER_GSTIN_MISMATCH` | **REJECTED**, vendor must reissue | – |
| 2 | `BANK_CHANGE` | **ON_HOLD**, hard stop, with "call the vendor on the number on file" | Owner |
| 3 | `INJECTION`, `POSSIBLE_DUPLICATE`, `PO_NOT_FOUND`, `PO_VENDOR_MISMATCH`, `VENDOR_UNKNOWN`, `GSTIN_MISMATCH`, `EXTRACTION_DISAGREEMENT`, `LOW_CONFIDENCE` | **ON_HOLD** | Finance manager (+ owner above ₹1 lakh) |
| 4 | Total > ₹1,00,000 | AWAITING_APPROVAL | Finance manager, then owner |
| 5 | `PRICE_VARIANCE`, `QTY_VARIANCE`, `FIRST_TIME_VENDOR`, or total ≥ ₹10,000 | AWAITING_APPROVAL | Finance manager |
| 6 | Otherwise (clean, < ₹10,000) | **APPROVED** automatically, recorded as a `policy` decision | – |

A test asserts that every flag in `Flag` has exactly one of these outcomes.

## The state machine (`khata/state.py`)

```
RECEIVED → EXTRACTED → VALIDATED → MATCHED → REVIEWED → AWAITING_APPROVAL → APPROVED → PAYMENT_DRAFTED
   any state before the gate ↘ ON_HOLD (a person decides) or REJECTED (vendor must reissue)
```

`transition()` locks the invoice row, refuses any move not in `ALLOWED`, and
writes an `audit_log` row (actor, from, to, reason, details) in the same
transaction. A two-step approval stays in AWAITING_APPROVAL between steps.

## Recording a person's decision (`record_decision`)

Khata, not the LLM, decides whether a reply in the room counts:
- the sender must be in `people`; the **next pending step** decides who may approve
  (manager step → `finance_manager`, owner step → `owner`);
- anyone on the invoice's route may reject; a clerk can do neither;
- every decision needs a reason, and it's stored with the policy version and the
  Switch message id it came from.

## Payment drafting (`khata/payments.py`)

Khata **never pays**. Payer drafts:
- a NEFT bulk-upload CSV (`Transaction Type, Beneficiary Name, Beneficiary Account
  Number, IFSC, Amount, Debit Account Number, Value Date, Payment Reference,
  Beneficiary Email, Remarks`), always to the **vendor master** account;
- double-entry journals per invoice (purchase: Dr Purchases + Input GST / Cr
  Sundry Creditors; payment: Dr Sundry Creditors / Cr Bank), checked to balance.

Only APPROVED invoices can be drafted. It's idempotent twice over: already-drafted
invoices are reported instead of drafted again, and `payment_lines.idempotency_key`
(sha256 of vendor | invoice no | amount) is UNIQUE in the database.

## Permissions (`khata/policy.py`)

Deny by default; enforced in code by `ToolGateway.call`, which logs **every**
attempt (allowed or denied) to `tool_calls` with args, result, error and duration.

| Agent | Tools |
|---|---|
| dispatcher | `classify_input` |
| intake | `extract_invoice`, `validate_gst`, `detect_injection` |
| verifier | `three_way_match`, `check_duplicate`, `check_bank_change` |
| analyst | `search_vendor_knowledge`, `get_invoice` |
| auditor | `get_invoice`, `search_vendor_knowledge` |
| router | `route_approval`, `record_decision` |
| payer | `draft_payment` |
| vendor_liaison | `get_invoice_status` |
| policy_steward | `propose_rule_change` (stretch) |

Tools take an `invoice_id` and read the data themselves. An agent can ask for a
check; it can't hand the tool the answer.

## MCP server (`mcp_server/server.py`)

```sh
make mcp AGENT=analyst                                   # stdio
uv run python -m mcp_server.server --agent router --http 8765
```

One server per agent identity. It **registers only that agent's tools**, so an
agent can't even see a tool it may not call, and every call still goes through
the gateway. The `agent` parameter is hidden from tool schemas: identity comes
from the server, not from the caller.

## How it's tested

- `tests/test_checks.py` (46): every check and the router, including boundaries
  (2.00% passes and 2.01% flags; ₹0.99 off passes and ₹1.01 flags; ₹9,999.99 / ₹10,000 / ₹1,00,000 /
  ₹1,00,000.01), injections hidden with zero-width and full-width characters, and
  ordinary invoice text that must *not* trip the detector.
- `tests/test_golden_pipeline.py` (38): all 31 labelled invoices, in order, through
  the real database, gateway, state machine, labelled human replies and Payer.
  Flags, route, gate and final state all match the labels. Plus: payments go only to
  master accounts, a second draft is a no-op, a rejected invoice can't be paid,
  out-of-scope calls are denied and logged, a manager can't approve the owner's
  step, and D03's audit trail has every transition in order.
- `tests/test_mcp_server.py` (8): each agent's server lists exactly its tools;
  calling an unlisted tool fails.

DB tests use a separate `khata_test` database, so they never touch the demo data.
