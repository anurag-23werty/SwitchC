"""The deterministic spine of the invoice workflow.

Runs every code check through the ToolGateway, as the agent that owns it, and
moves the invoice through the state machine. A failed gate stops the invoice
early. Phase 3's LangGraph workflow calls the same tools in the same order and
adds the LLM steps (extraction, Analyst, Auditor) between them.
"""

from __future__ import annotations

import asyncio
from typing import Any

from khata.approval import REJECT
from khata.models import State
from khata.policy import ToolGateway
from khata.state import transition

REJECT_FLAGS = {f.value for f in REJECT}


def _rejecting(*reports: dict[str, Any]) -> bool:
    return any(set(r["flags"]) & REJECT_FLAGS for r in reports)


async def _advance(
    gw: ToolGateway, invoice_id: int, to: State, actor: str, reason: str
) -> None:
    async with gw.pool.connection() as conn, conn.transaction():
        await transition(conn, invoice_id, to, actor, "agent", reason)


async def run_checks(gw: ToolGateway, invoice_id: int) -> dict[str, Any]:
    """EXTRACTED -> (VALIDATED -> MATCHED -> REVIEWED) -> the approval gate."""
    gst = await gw.call("intake", "validate_gst", invoice_id=invoice_id)
    injection = await gw.call("intake", "detect_injection", invoice_id=invoice_id)
    reports = {"validate_gst": gst, "detect_injection": injection}
    if _rejecting(gst):
        route = await gw.call("router", "route_approval", invoice_id=invoice_id)
        return {"reports": reports, "route": route}
    await _advance(
        gw, invoice_id, State.VALIDATED, "khata-intake", "GST and document checks done"
    )

    # Sectioning: the three verifier checks are independent, so they run together.
    match, dup, bank = await asyncio.gather(
        gw.call("verifier", "three_way_match", invoice_id=invoice_id),
        gw.call("verifier", "check_duplicate", invoice_id=invoice_id),
        gw.call("verifier", "check_bank_change", invoice_id=invoice_id),
    )
    reports |= {
        "three_way_match": match,
        "check_duplicate": dup,
        "check_bank_change": bank,
    }
    await _advance(
        gw,
        invoice_id,
        State.MATCHED,
        "khata-verifier",
        "Match, duplicate and bank checks done",
    )
    await _advance(
        gw, invoice_id, State.REVIEWED, "khata-verifier", "Ready for routing"
    )
    route = await gw.call("router", "route_approval", invoice_id=invoice_id)
    return {"reports": reports, "route": route}
