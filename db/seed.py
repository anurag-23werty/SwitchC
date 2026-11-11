"""Rebuild Khata's database and load the demo masters.

    uv run python -m db.seed

Drops and recreates the `khata` schema (Khata's own data only — Switch's
database is a different server), applies db/migrations, then loads the company,
people, vendors, purchase orders, goods receipts, approval policy v1, payment
history and vendor knowledge from data/fixtures.py.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import psycopg

from data import fixtures as fx
from khata.config import get_settings
from khata.migrate import migrate
from khata.money import money
from khata.payments import idempotency_key


def reset_schema(conn: psycopg.Connection) -> None:
    """Demo only: throws away everything in the `khata` schema."""
    conn.execute("DROP SCHEMA IF EXISTS khata CASCADE")
    conn.commit()
    migrate(conn)


def load_masters(conn: psycopg.Connection) -> dict[str, int]:
    c = fx.COMPANY
    assert c.bank is not None
    conn.execute(
        """INSERT INTO company (name, gstin, state_code, address, bank_name, bank_account, ifsc)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (
            c.name,
            c.gstin,
            c.state_code,
            ", ".join(c.address),
            c.bank.name,
            c.bank.account,
            c.bank.ifsc,
        ),
    )
    for p in fx.PEOPLE:
        conn.execute(
            "INSERT INTO people (username, display_name, role) VALUES (%s, %s, %s)",
            (p["username"], p["display_name"], p["role"]),
        )
    vendor_ids: dict[str, int] = {}
    for v in fx.VENDORS:
        assert v.bank is not None
        (vendor_ids[v.code],) = conn.execute(
            """INSERT INTO vendors (code, name, gstin, state_code, address, phone, email,
                                    bank_name, bank_account, ifsc, first_seen)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                v.code,
                v.name,
                v.gstin,
                v.state_code,
                ", ".join(v.address),
                v.phone,
                v.email,
                v.bank.name,
                v.bank.account,
                v.bank.ifsc,
                v.first_seen,
            ),
        ).fetchone()
    for c in fx.VENDOR_CONTACTS:
        conn.execute(
            """INSERT INTO vendor_contacts (username, vendor_id, display_name, registered_by)
               VALUES (%s, %s, %s, 'seed')""",
            (c["username"], vendor_ids[c["vendor"]], c["display_name"]),
        )
    return vendor_ids


def load_purchasing(conn: psycopg.Connection, vendor_ids: dict[str, int]) -> None:
    for po in fx.POS:
        (po_id,) = conn.execute(
            "INSERT INTO purchase_orders (po_number, vendor_id, po_date) VALUES (%s, %s, %s) RETURNING id",
            (po.number, vendor_ids[po.vendor.code], po.po_date),
        ).fetchone()
        (grn_id,) = conn.execute(
            """INSERT INTO goods_receipts (grn_number, po_id, received_date, received_by)
               VALUES (%s, %s, %s, 'stores') RETURNING id""",
            (po.grn_number, po_id, po.po_date + timedelta(days=4)),
        ).fetchone()
        received = po.received or tuple(line.qty for line in po.lines)
        for n, (line, qty_received) in enumerate(
            zip(po.lines, received, strict=True), 1
        ):
            (line_id,) = conn.execute(
                """INSERT INTO po_lines (po_id, line_no, description, hsn, qty, unit, rate, gst_rate)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (
                    po_id,
                    n,
                    line.description,
                    line.hsn,
                    line.qty,
                    line.unit,
                    money(line.rate),
                    line.gst_rate,
                ),
            ).fetchone()
            conn.execute(
                "INSERT INTO grn_lines (grn_id, po_line_id, qty_received) VALUES (%s, %s, %s)",
                (grn_id, line_id, qty_received),
            )


def load_policy(conn: psycopg.Connection) -> None:
    conn.execute(
        """INSERT INTO rules (version, policy, change_summary, proposed_by, approved_by, active)
           VALUES (1, %s, 'Initial approval policy', 'owner', 'owner', true)""",
        (json.dumps(fx.POLICY_V1),),
    )


def load_history(conn: psycopg.Connection, vendor_ids: dict[str, int]) -> None:
    """Invoices paid before the demo, so vendors have a track record."""
    for h in fx.HISTORY:
        tax = money(h.taxable * h.gst_rate / 100)
        intra = h.vendor.state_code == fx.COMPANY.state_code
        cgst = sgst = money(tax / 2) if intra else Decimal("0.00")
        igst = Decimal("0.00") if intra else tax
        total = h.taxable + cgst + sgst + igst
        assert h.vendor.bank is not None
        (inv_id,) = conn.execute(
            """INSERT INTO invoices (vendor_id, invoice_no, invoice_date, supplier_gstin,
                   buyer_gstin, taxable_value, cgst, sgst, igst, total, bank_account, ifsc,
                   state, idempotency_key, received_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       'PAYMENT_DRAFTED', %s, %s) RETURNING id""",
            (
                vendor_ids[h.vendor.code],
                h.invoice_no,
                h.invoice_date,
                h.vendor.gstin,
                fx.COMPANY.gstin,
                h.taxable,
                cgst,
                sgst,
                igst,
                total,
                h.vendor.bank.account,
                h.vendor.bank.ifsc,
                idempotency_key(h.vendor.code, h.invoice_no, total),
                h.invoice_date,
            ),
        ).fetchone()
        auto = h.approved_by == "auto"
        conn.execute(
            """INSERT INTO decisions (invoice_id, decided_by, decider_kind, decision, reason,
                                      rule_version, created_at)
               VALUES (%s, %s, %s, %s, %s, 1, %s)""",
            (
                inv_id,
                "policy" if auto else h.approved_by,
                "policy" if auto else "human",
                "auto_approve" if auto else "approve",
                "Clean match under auto-approve limit"
                if auto
                else "Matches PO and GRN.",
                h.invoice_date + timedelta(days=1),
            ),
        )
        conn.execute(
            """INSERT INTO audit_log (invoice_id, actor, actor_kind, action, to_state, reason)
               VALUES (%s, 'seed', 'system', 'history_import', 'PAYMENT_DRAFTED',
                       'Paid before Khata went live')""",
            (inv_id,),
        )


def load_knowledge(conn: psycopg.Connection, vendor_ids: dict[str, int]) -> None:
    for vendor, content in fx.KNOWLEDGE:
        conn.execute(
            "INSERT INTO vendor_knowledge (vendor_id, kind, content) VALUES (%s, 'note', %s)",
            (vendor_ids[vendor.code], content),
        )


def seed(url: str) -> dict[str, int]:
    """Rebuild the `khata` schema at `url` and load the demo masters. Returns row counts."""
    with psycopg.connect(url, options="-c search_path=khata,public") as conn:
        reset_schema(conn)
        vendor_ids = load_masters(conn)
        load_purchasing(conn, vendor_ids)
        load_policy(conn)
        load_history(conn, vendor_ids)
        load_knowledge(conn, vendor_ids)
        counts = {
            t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # noqa: S608
            for t in (
                "vendors",
                "purchase_orders",
                "po_lines",
                "goods_receipts",
                "invoices",
                "decisions",
                "rules",
                "vendor_knowledge",
                "people",
            )
        }
    return counts


def main() -> None:
    counts = seed(get_settings().khata_database_url)
    print("seeded khata:", ", ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
