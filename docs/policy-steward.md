# Policy Steward (Phase 6)

The approval policy lives in the `rules` table, one row per version. People make the
exceptions; the **Policy Steward** (`khata-steward`, the 8th agent) notices when they
keep making the same one, and proposes turning it into a rule. **Only the owner can
accept a proposal**, and accepting it writes a new policy version. Nothing about this
is decided by a model: the pattern, the proposed limit and the new rule are all code.

```sh
uv run khata steward-demo      # D03, D04 -> proposal -> owner approves -> S01 auto-approved
uv run khata rules             # versions and proposals
uv run khata rule 1 --as owner approve Freight up to 4% is agreed with them
uv run python scripts/steward_room_demo.py   # the same, live in #ap-desk (needs `khata serve`)
```

## How it works

1. A person approves an invoice. After the Payer drafts it, the workflow's `steward`
   node calls `propose_rule_change` (the Steward's only tool, through the ToolGateway).
2. Code counts that vendor's invoices that a **person approved** and whose **only**
   flag was the same eligible exception. Eligible flags are listed in the policy
   (`steward.eligible_flags`: `PRICE_VARIANCE` only). Bank changes, injections,
   duplicates, unknown vendors and quantity differences can never be proposed.
3. At `steward.min_overrides` (2) or more, it re-measures each invoice's variance from
   the stored reading and the PO, and proposes accepting up to the **largest variance
   people actually approved**, never more than `steward.max_variance_pct` (10%).
4. The proposal is posted as its own thread in `#ap-desk`, tagging the owner, with
   each approval and its reason quoted; `#ap-audit` gets a line too.
5. The owner replies `approve <reason>` or `reject <reason>` in that thread.
   Anyone else is refused ("only the owner can decide a rule change").
6. On approval, one transaction: the active policy is copied, the vendor exception is
   added (`vendor_exceptions`), a new `rules` row is inserted and made active, the old
   one is kept, the proposal is marked approved, and a `pattern` note is added to the
   vendor's knowledge. The room's policy document is refreshed.
7. A rejection is remembered: the same evidence is never proposed again; only new
   approvals that add to the pattern start a new proposal.

## What an exception changes, and what it doesn't

With policy v2 in force, an invoice from that vendor whose price variance is **within
the agreed percentage** no longer needs a sign-off *for the variance*:

- Amount limits still apply. Over ₹10,000 still goes to the finance manager; over
  ₹1,00,000 also to the owner. (So D03/D04-size invoices still go to Priya, but the
  small S01 is auto-approved.)
- A bigger variance, another flag, or another vendor is routed exactly as before.
- The flag is still recorded on the invoice; the Router's reason says which policy
  version and which approvals accepted it.
- Every past decision keeps the `rule_version` it was made under.

## Verified

- `tests/test_steward.py` (9): the rule engine with an exception (within / above the
  limit, other vendors, other flags, hard stops, amount limits); one approval is not a
  pattern, two are; no duplicate proposals; the cap; only the owner decides; a
  rejection sticks; approval writes v2 and S01 is auto-approved with `rule_version` 2.
- Live in Mattermost through Switch (`scripts/steward_room_demo.py`): D03 and D04
  approved by Priya, proposal #1 posted tagging @owner, Priya's attempt refused, the
  owner's approval recorded from the room, policy v2 active, S01 auto-approved, the
  room's policy document updated.
- The eval (policy v1 throughout) is unchanged: proposals are made but never approved
  there, so no label moves.
