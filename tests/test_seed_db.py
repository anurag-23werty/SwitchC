"""Checks against the seeded database. Skipped when khata-db isn't running."""

import psycopg
import pytest

from data import fixtures as fx
from khata.config import get_settings


@pytest.fixture(scope="module")
def conn():
    try:
        c = psycopg.connect(
            get_settings().khata_database_url,
            options="-c search_path=khata,public",
            connect_timeout=3,
        )
    except psycopg.OperationalError:
        pytest.skip("khata-db not reachable; run `make up && make seed`")
    with c:
        yield c


def one(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def test_counts(conn):
    assert one(conn, "SELECT count(*) FROM vendors") == len(fx.VENDORS)
    assert one(conn, "SELECT count(*) FROM purchase_orders") == len(fx.POS)
    assert one(conn, "SELECT count(*) FROM invoices") == len(fx.HISTORY)


def test_exactly_one_active_policy(conn):
    assert one(conn, "SELECT count(*) FROM rules WHERE active") == 1
    assert (
        one(conn, "SELECT policy->>'auto_approve_below' FROM rules WHERE active")
        == "10000.00"
    )


def test_partial_delivery_recorded(conn):
    received = one(
        conn,
        """
        SELECT g.qty_received FROM grn_lines g
        JOIN po_lines l ON l.id = g.po_line_id
        JOIN purchase_orders p ON p.id = l.po_id
        WHERE p.po_number = 'PO-2026-1131'""",
    )
    assert received == 60


def test_first_time_vendor_has_no_history(conn):
    assert (
        one(
            conn,
            """SELECT count(*) FROM invoices i JOIN vendors v ON v.id = i.vendor_id
                        WHERE v.code = 'DTC'""",
        )
        == 0
    )


def test_payment_lines_reject_a_second_draft(conn):
    """The idempotency key is unique at the database level, not just in code."""
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        inv = one(conn, "SELECT id FROM invoices ORDER BY id LIMIT 1")
        batch = one(
            conn,
            "INSERT INTO payment_batches (batch_ref, created_by) "
            "VALUES ('TEST', 'test') RETURNING id",
        )
        for _ in range(2):
            conn.execute(
                "INSERT INTO payment_lines (batch_id, invoice_id, idempotency_key, amount, "
                "beneficiary_name, beneficiary_account, ifsc) "
                "VALUES (%s, %s, 'k', 1, 'x', '1', 'HDFC0000001')",
                (batch, inv),
            )
