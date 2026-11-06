"""The invoice state machine. Every transition is checked and written to audit_log."""

from __future__ import annotations

import json
from typing import Any

from psycopg import AsyncConnection

from khata.models import State

S = State
ALLOWED: dict[State, set[State]] = {
    S.RECEIVED: {S.EXTRACTED, S.ON_HOLD, S.REJECTED},
    S.EXTRACTED: {S.VALIDATED, S.ON_HOLD, S.REJECTED},
    S.VALIDATED: {S.MATCHED, S.ON_HOLD, S.REJECTED},
    S.MATCHED: {S.REVIEWED, S.ON_HOLD, S.REJECTED},
    S.REVIEWED: {S.AWAITING_APPROVAL, S.APPROVED, S.ON_HOLD, S.REJECTED},
    # A two-step approval stays in AWAITING_APPROVAL between the steps.
    S.AWAITING_APPROVAL: {S.AWAITING_APPROVAL, S.APPROVED, S.REJECTED},
    S.ON_HOLD: {S.AWAITING_APPROVAL, S.APPROVED, S.REJECTED},
    S.APPROVED: {S.PAYMENT_DRAFTED},
    S.PAYMENT_DRAFTED: set(),
    S.REJECTED: set(),
}


class IllegalTransition(Exception):
    pass


async def audit(
    conn: AsyncConnection,
    invoice_id: int | None,
    actor: str,
    actor_kind: str,
    action: str,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
    from_state: State | None = None,
    to_state: State | None = None,
) -> None:
    await conn.execute(
        """INSERT INTO audit_log (invoice_id, actor, actor_kind, action, from_state, to_state,
                                  reason, details)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            invoice_id,
            actor,
            actor_kind,
            action,
            from_state,
            to_state,
            reason,
            json.dumps(details or {}, default=str),
        ),
    )


async def transition(
    conn: AsyncConnection,
    invoice_id: int,
    to_state: State,
    actor: str,
    actor_kind: str,
    reason: str,
    details: dict[str, Any] | None = None,
) -> State:
    """Move an invoice to `to_state` if the state machine allows it. Returns the old state.

    The row is locked first, so two agents can't move the same invoice at once.
    """
    row = await (
        await conn.execute(
            "SELECT state FROM invoices WHERE id = %s FOR UPDATE", (invoice_id,)
        )
    ).fetchone()
    if row is None:
        raise LookupError(f"invoice {invoice_id} not found")
    current = State(row["state"])
    if to_state not in ALLOWED[current]:
        raise IllegalTransition(
            f"invoice {invoice_id}: {current} -> {to_state} is not allowed"
        )
    await conn.execute(
        "UPDATE invoices SET state = %s, updated_at = now() WHERE id = %s",
        (to_state, invoice_id),
    )
    await audit(
        conn,
        invoice_id,
        actor,
        actor_kind,
        "transition",
        reason,
        details,
        current,
        to_state,
    )
    return current
