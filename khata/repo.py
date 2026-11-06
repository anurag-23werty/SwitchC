"""Database reads and writes. Functions take a connection so callers own the transaction."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

from psycopg import AsyncConnection

from khata.models import (
    Company,
    Flag,
    InvoiceData,
    Policy,
    POLine,
    PriorInvoice,
    PurchaseOrder,
    State,
    Vendor,
)
from khata.payments import idempotency_key

_VENDOR_COLS = (
    "id, code, name, gstin, state_code, phone, email, bank_name, bank_account, ifsc"
)


async def _one(conn: AsyncConnection, sql: str, *args: Any) -> dict | None:
    return await (await conn.execute(sql, args)).fetchone()


async def _all(conn: AsyncConnection, sql: str, *args: Any) -> list[dict]:
    return await (await conn.execute(sql, args)).fetchall()


async def get_company(conn: AsyncConnection) -> Company:
    row = await _one(conn, "SELECT name, gstin, state_code FROM company")
    if row is None:
        raise LookupError("company not seeded; run `make seed`")
    return Company(**row)


async def get_policy(conn: AsyncConnection) -> Policy:
    row = await _one(conn, "SELECT version, policy FROM rules WHERE active")
    if row is None:
        raise LookupError("no active approval policy in `rules`")
    p = row["policy"]
    return Policy(
        version=row["version"],
        auto_approve_below=Decimal(p["auto_approve_below"]),
        manager_up_to=Decimal(p["manager_up_to"]),
        variance_pct=Decimal(p["variance_pct"]),
        duplicate_window_days=int(p["duplicate_window_days"]),
        arithmetic_tolerance=Decimal(p["arithmetic_tolerance"]),
        approvers=p["approvers"],
    )


async def get_vendor(conn: AsyncConnection, vendor_id: int) -> Vendor:
    row = await _one(
        conn, f"SELECT {_VENDOR_COLS} FROM vendors WHERE id = %s", vendor_id
    )
    if row is None:
        raise LookupError(f"vendor {vendor_id} not found")
    return Vendor(**row)


def _name_key(name: str) -> str:
    name = re.sub(r"\b(pvt|private|ltd|limited|co|company|llp)\b\.?", " ", name.lower())
    return " ".join(re.findall(r"[a-z0-9]+", name))


async def find_vendor(
    conn: AsyncConnection, gstin: str, name: str
) -> tuple[Vendor | None, str]:
    """Identify the vendor: by GSTIN, else by name. Returns (vendor, how it matched)."""
    row = await _one(
        conn,
        f"SELECT {_VENDOR_COLS} FROM vendors WHERE gstin = %s",
        gstin.strip().upper(),
    )
    if row:
        return Vendor(**row), "gstin"
    key = _name_key(name)
    for row in await _all(conn, f"SELECT {_VENDOR_COLS} FROM vendors"):
        if _name_key(row["name"]) == key:
            return Vendor(**row), "name"
    return None, "none"


async def get_po(conn: AsyncConnection, po_number: str | None) -> PurchaseOrder | None:
    if not po_number:
        return None
    po = await _one(
        conn,
        "SELECT id, po_number, vendor_id FROM purchase_orders WHERE po_number = %s",
        po_number.strip().upper(),
    )
    if po is None:
        return None
    lines = await _all(
        conn,
        """
        SELECT l.id, l.line_no, l.description, l.hsn, l.qty, l.rate, l.gst_rate,
               COALESCE(SUM(g.qty_received), 0) AS qty_received
        FROM po_lines l LEFT JOIN grn_lines g ON g.po_line_id = l.id
        WHERE l.po_id = %s GROUP BY l.id ORDER BY l.line_no""",
        po["id"],
    )
    return PurchaseOrder(**po, lines=[POLine(**ln) for ln in lines])


async def prior_invoices(
    conn: AsyncConnection, vendor_id: int, exclude_id: int | None
) -> list[PriorInvoice]:
    rows = await _all(
        conn,
        """
        SELECT id, invoice_no, invoice_date, total, state FROM invoices
        WHERE vendor_id = %s AND id <> COALESCE(%s, -1)
          AND invoice_no IS NOT NULL AND total IS NOT NULL
        ORDER BY id""",
        vendor_id,
        exclude_id,
    )
    return [PriorInvoice(**r) for r in rows]


async def has_approved_history(
    conn: AsyncConnection, vendor_id: int, exclude_id: int | None
) -> bool:
    row = await _one(
        conn,
        """
        SELECT 1 FROM invoices WHERE vendor_id = %s AND id <> COALESCE(%s, -1)
          AND state IN ('APPROVED', 'PAYMENT_DRAFTED') LIMIT 1""",
        vendor_id,
        exclude_id,
    )
    return row is not None


async def create_invoice(
    conn: AsyncConnection,
    source_file: str | None,
    file_sha256: str | None,
    room_id: str | None,
    thread_id: str | None,
) -> int:
    row = await _one(
        conn,
        """
        INSERT INTO invoices (source_file, file_sha256, switch_room_id, switch_thread_id)
        VALUES (%s, %s, %s, %s) RETURNING id""",
        source_file,
        file_sha256,
        room_id,
        thread_id,
    )
    assert row is not None
    return row["id"]


async def store_extraction(
    conn: AsyncConnection,
    invoice_id: int,
    data: InvoiceData,
    vendor: Vendor | None,
    readings: dict[str, Any],
    confidence: Decimal,
) -> None:
    """Save the agreed reading of an invoice (and every raw reading) on its row."""
    key = idempotency_key(vendor.code, data.invoice_no, data.total) if vendor else None
    await conn.execute(
        """
        UPDATE invoices SET vendor_id = %s, invoice_no = %s, invoice_date = %s, po_number = %s,
            supplier_gstin = %s, buyer_gstin = %s, taxable_value = %s, cgst = %s, sgst = %s,
            igst = %s, total = %s, bank_account = %s, ifsc = %s, extracted = %s,
            extraction_confidence = %s, idempotency_key = %s, updated_at = now()
        WHERE id = %s""",
        (
            vendor.id if vendor else None,
            data.invoice_no,
            data.invoice_date,
            data.po_number,
            data.supplier_gstin,
            data.buyer_gstin,
            data.taxable_value,
            data.cgst,
            data.sgst,
            data.igst,
            data.total,
            data.bank_account,
            data.ifsc,
            json.dumps(
                {"agreed": data.model_dump(mode="json"), "readings": readings},
                default=str,
            ),
            confidence,
            key,
            invoice_id,
        ),
    )
    await conn.execute("DELETE FROM invoice_lines WHERE invoice_id = %s", (invoice_id,))
    for n, line in enumerate(data.lines, start=1):
        await conn.execute(
            """
            INSERT INTO invoice_lines (invoice_id, line_no, description, hsn, qty, unit, rate,
                                       gst_rate, amount)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                invoice_id,
                n,
                line.description,
                line.hsn,
                line.qty,
                line.unit,
                line.rate,
                line.gst_rate,
                line.amount,
            ),
        )


async def add_flags(conn: AsyncConnection, invoice_id: int, flags: list[Flag]) -> None:
    await conn.execute(
        """
        UPDATE invoices SET flags = ARRAY(SELECT DISTINCT unnest(flags || %s::text[])),
                            updated_at = now()
        WHERE id = %s""",
        ([f.value for f in flags], invoice_id),
    )


async def load_invoice_data(conn: AsyncConnection, invoice_id: int) -> InvoiceData:
    """Rebuild the stored reading as an InvoiceData."""
    row = await _one(conn, "SELECT extracted FROM invoices WHERE id = %s", invoice_id)
    if row is None or row["extracted"] is None:
        raise LookupError(f"invoice {invoice_id} has not been extracted yet")
    return InvoiceData.model_validate(row["extracted"]["agreed"])


async def get_invoice(conn: AsyncConnection, invoice_id: int) -> dict[str, Any]:
    inv = await _one(
        conn,
        """
        SELECT i.id, i.state, i.flags, i.invoice_no, i.invoice_date, i.po_number, i.total,
               i.taxable_value, i.cgst, i.sgst, i.igst, i.supplier_gstin, i.bank_account,
               i.ifsc, i.extraction_confidence, i.switch_room_id, i.switch_thread_id,
               v.code AS vendor_code, v.name AS vendor_name
        FROM invoices i LEFT JOIN vendors v ON v.id = i.vendor_id WHERE i.id = %s""",
        invoice_id,
    )
    if inv is None:
        raise LookupError(f"invoice {invoice_id} not found")
    inv["lines"] = await _all(
        conn,
        """
        SELECT line_no, description, hsn, qty, unit, rate, gst_rate, amount
        FROM invoice_lines WHERE invoice_id = %s ORDER BY line_no""",
        invoice_id,
    )
    inv["decisions"] = await _all(
        conn,
        """
        SELECT decided_by, decision, approval_step, reason, created_at
        FROM decisions WHERE invoice_id = %s ORDER BY id""",
        invoice_id,
    )
    inv["history"] = await _all(
        conn,
        """
        SELECT actor, action, from_state, to_state, reason, created_at
        FROM audit_log WHERE invoice_id = %s ORDER BY id""",
        invoice_id,
    )
    return inv


async def person(conn: AsyncConnection, username: str) -> dict | None:
    return await _one(
        conn,
        "SELECT username, display_name, role FROM people WHERE username = %s",
        username.lstrip("@").lower(),
    )


async def decisions_for(conn: AsyncConnection, invoice_id: int) -> list[dict]:
    return await _all(
        conn,
        """SELECT decided_by, decision, approval_step, reason
                               FROM decisions WHERE invoice_id = %s ORDER BY id""",
        invoice_id,
    )


async def insert_decision(
    conn: AsyncConnection,
    invoice_id: int,
    decided_by: str,
    decider_kind: str,
    decision: str,
    approval_step: str | None,
    reason: str,
    rule_version: int,
    switch_message_id: str | None,
) -> int:
    row = await _one(
        conn,
        """
        INSERT INTO decisions (invoice_id, decided_by, decider_kind, decision, approval_step,
                               reason, rule_version, switch_message_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        invoice_id,
        decided_by,
        decider_kind,
        decision,
        approval_step,
        reason,
        rule_version,
        switch_message_id,
    )
    assert row is not None
    return row["id"]


async def vendor_knowledge(
    conn: AsyncConnection, vendor_id: int, query: str | None
) -> list[dict]:
    """Notes and past human decisions (with their reasons) for a vendor.

    Ranked by word overlap with `query` when one is given. Vector search over
    the `embedding` column is a Phase 6 upgrade; this is the deterministic base.
    """
    notes = await _all(
        conn,
        """
        SELECT 'note' AS kind, content AS text, created_at, NULL::int AS invoice_id,
               NULL::text AS invoice_no
        FROM vendor_knowledge WHERE vendor_id = %s""",
        vendor_id,
    )
    past = await _all(
        conn,
        """
        SELECT 'decision' AS kind,
               d.decision || ' by ' || d.decided_by || ': ' || d.reason
                 || ' [flags: ' || COALESCE(array_to_string(i.flags, ', '), '') || ']' AS text,
               d.created_at, i.id AS invoice_id, i.invoice_no
        FROM decisions d JOIN invoices i ON i.id = d.invoice_id
        WHERE i.vendor_id = %s AND d.decider_kind = 'human'
        ORDER BY d.id DESC LIMIT 20""",
        vendor_id,
    )
    items = notes + past
    if query:
        words = set(re.findall(r"[a-z0-9]+", query.lower()))
        for item in items:
            item["score"] = len(
                words & set(re.findall(r"[a-z0-9]+", item["text"].lower()))
            )
        items.sort(key=lambda i: (-i["score"], i["kind"] != "decision"))
    return items


async def invoice_status(
    conn: AsyncConnection, vendor_code: str, invoice_no: str
) -> list[dict]:
    return await _all(
        conn,
        """
        SELECT i.id, i.invoice_no, i.state, i.total, i.updated_at
        FROM invoices i JOIN vendors v ON v.id = i.vendor_id
        WHERE v.code = %s AND upper(replace(i.invoice_no, ' ', '')) = upper(replace(%s, ' ', ''))
        ORDER BY i.id""",
        vendor_code.upper(),
        invoice_no,
    )


async def invoices_in_state(conn: AsyncConnection, state: State) -> list[int]:
    rows = await _all(
        conn, "SELECT id FROM invoices WHERE state = %s ORDER BY id", state
    )
    return [r["id"] for r in rows]
