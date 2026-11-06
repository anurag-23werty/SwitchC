"""Payment drafting: a NEFT bulk-upload CSV and journal entries. Khata never pays.

A person downloads the CSV from the room and uploads it to the bank's corporate
portal. Every amount and account comes from the database: the beneficiary is
always the vendor *master* account, never the one printed on the invoice.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection

from khata.models import State
from khata.money import money
from khata.state import audit, transition

NEFT_COLUMNS = [
    "Transaction Type",
    "Beneficiary Name",
    "Beneficiary Account Number",
    "IFSC",
    "Amount",
    "Debit Account Number",
    "Value Date",
    "Payment Reference",
    "Beneficiary Email",
    "Remarks",
]


def idempotency_key(vendor_code: str, invoice_no: str, total: Decimal) -> str:
    """hash(vendor, invoice number, amount). The same invoice always gets the same key."""
    raw = f"{vendor_code.strip().upper()}|{invoice_no.strip().upper()}|{money(total)}"
    return hashlib.sha256(raw.encode()).hexdigest()


class NotPayable(Exception):
    """An invoice that isn't APPROVED was asked to be paid."""


@dataclass
class DraftResult:
    batch_ref: str | None
    csv_path: str | None
    drafted: list[int] = field(default_factory=list)  # invoice ids added now
    already_drafted: list[dict] = field(default_factory=list)  # {invoice_id, batch_ref}
    total: Decimal = Decimal("0.00")
    journal: list[dict] = field(default_factory=list)


def journal_entries(inv: dict[str, Any], company_bank: str) -> list[dict]:
    """Double entry for one invoice: book the purchase, then the payment. Each balances."""
    creditor = f"Sundry Creditors - {inv['vendor_name']}"
    booking = [("Dr", "Purchases", inv["taxable_value"])]
    for tax, label in (
        ("cgst", "Input CGST"),
        ("sgst", "Input SGST"),
        ("igst", "Input IGST"),
    ):
        if inv[tax] and inv[tax] > 0:
            booking.append(("Dr", label, inv[tax]))
    booking.append(("Cr", creditor, inv["total"]))
    payment = [
        ("Dr", creditor, inv["total"]),
        ("Cr", f"Bank - {company_bank}", inv["total"]),
    ]
    entries = []
    for kind, lines in (("purchase", booking), ("payment", payment)):
        dr = sum(a for side, _, a in lines if side == "Dr")
        cr = sum(a for side, _, a in lines if side == "Cr")
        if dr != cr:
            raise ValueError(
                f"unbalanced {kind} entry for invoice {inv['id']}: Dr {dr} != Cr {cr}"
            )
        entries.append(
            {
                "invoice_id": inv["id"],
                "type": kind,
                "narration": f"{inv['vendor_name']} invoice {inv['invoice_no']}",
                "lines": [
                    {"side": s, "account": a, "amount": str(money(v))}
                    for s, a, v in lines
                ],
            }
        )
    return entries


async def draft_payment(
    conn: AsyncConnection,
    invoice_ids: list[int],
    created_by: str,
    out_dir: Path,
    value_date: date,
) -> DraftResult:
    """Draft one NEFT batch for the APPROVED invoices given. Idempotent.

    An invoice that already has a payment line is reported, not drafted again;
    the unique idempotency key on payment_lines makes a second line impossible
    even if two drafts race. Drafted invoices move to PAYMENT_DRAFTED.
    """
    company = await (
        await conn.execute("SELECT name, bank_name, bank_account FROM company")
    ).fetchone()
    rows = await (
        await conn.execute(
            """
        SELECT i.id, i.state, i.invoice_no, i.total, i.taxable_value, i.cgst, i.sgst, i.igst,
               i.idempotency_key, v.code AS vendor_code, v.name AS vendor_name,
               v.bank_account, v.ifsc, v.email, pl.batch_ref
        FROM invoices i JOIN vendors v ON v.id = i.vendor_id
        LEFT JOIN payment_lines l ON l.invoice_id = i.id
        LEFT JOIN payment_batches pl ON pl.id = l.batch_id
        WHERE i.id = ANY(%s) ORDER BY i.id FOR UPDATE OF i""",
            (invoice_ids,),
        )
    ).fetchall()
    missing = set(invoice_ids) - {r["id"] for r in rows}
    if missing:
        raise LookupError(f"invoices not found (or vendor unknown): {sorted(missing)}")

    result = DraftResult(batch_ref=None, csv_path=None)
    to_pay = []
    for r in rows:
        if r["batch_ref"]:
            result.already_drafted.append(
                {"invoice_id": r["id"], "batch_ref": r["batch_ref"]}
            )
        elif r["state"] != "APPROVED":
            raise NotPayable(
                f"invoice {r['id']} is {r['state']}; only APPROVED invoices are paid"
            )
        else:
            to_pay.append(r)
    if not to_pay:
        return result

    seq = (
        await (
            await conn.execute("SELECT count(*) AS n FROM payment_batches")
        ).fetchone()
    )["n"] + 1
    batch_ref = f"KH-{value_date:%Y%m%d}-{seq:03d}"
    batch = await (
        await conn.execute(
            "INSERT INTO payment_batches (batch_ref, created_by) VALUES (%s, %s) RETURNING id",
            (batch_ref, created_by),
        )
    ).fetchone()
    csv_rows = []
    for r in to_pay:
        key = r["idempotency_key"] or idempotency_key(
            r["vendor_code"], r["invoice_no"], r["total"]
        )
        await conn.execute(
            """
            INSERT INTO payment_lines (batch_id, invoice_id, idempotency_key, amount,
                                       beneficiary_name, beneficiary_account, ifsc)
            VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                batch["id"],
                r["id"],
                key,
                r["total"],
                r["vendor_name"],
                r["bank_account"],
                r["ifsc"],
            ),
        )
        csv_rows.append(
            [
                "NEFT",
                r["vendor_name"],
                r["bank_account"],
                r["ifsc"],
                f"{money(r['total'])}",
                company["bank_account"],
                f"{value_date:%d/%m/%Y}",
                f"{batch_ref}/{r['id']}",
                r["email"],
                f"Inv {r['invoice_no']}"[:30],
            ]
        )
        result.journal += journal_entries(r, company["bank_name"])
        result.total += r["total"]
        result.drafted.append(r["id"])

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{batch_ref}.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(NEFT_COLUMNS)
        w.writerows(csv_rows)
    await conn.execute(
        "UPDATE payment_batches SET csv_path = %s, journal = %s WHERE id = %s",
        (str(path), json.dumps(result.journal), batch["id"]),
    )
    for r in to_pay:
        await transition(
            conn,
            r["id"],
            State.PAYMENT_DRAFTED,
            created_by,
            "agent",
            f"Payment drafted in batch {batch_ref}",
            {"batch_ref": batch_ref},
        )
    await audit(
        conn,
        None,
        created_by,
        "agent",
        "payment_batch_drafted",
        f"{len(to_pay)} payment(s), Rs {money(result.total)}",
        {"batch_ref": batch_ref, "invoice_ids": result.drafted},
    )
    result.batch_ref, result.csv_path = batch_ref, str(path)
    return result
