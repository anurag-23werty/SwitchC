"""Payment drafting. Phase 1 holds only the idempotency key; drafting lands in Phase 2."""

from __future__ import annotations

import hashlib
from decimal import Decimal

from khata.money import money


def idempotency_key(vendor_code: str, invoice_no: str, total: Decimal) -> str:
    """hash(vendor, invoice number, amount). The same invoice always gets the same key."""
    raw = f"{vendor_code.strip().upper()}|{invoice_no.strip().upper()}|{money(total)}"
    return hashlib.sha256(raw.encode()).hexdigest()
