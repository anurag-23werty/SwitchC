"""Shared fixtures. DB tests run against a separate `khata_test` database, never the demo one."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
import pytest_asyncio
import yaml
from psycopg import sql

from khata.config import get_settings
from khata.db import make_pool
from khata.models import InvoiceData, InvoiceLine

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = yaml.safe_load((ROOT / "evals" / "golden.yaml").read_text())["invoices"]
TEST_DB = "khata_test"


def invoice_from_truth(truth: dict) -> InvoiceData:
    """A perfect reading of an invoice: what extraction should produce."""
    return InvoiceData(
        vendor_name=truth["vendor_name"],
        supplier_gstin=truth["supplier_gstin"],
        buyer_gstin=truth["buyer_gstin"],
        invoice_no=truth["invoice_no"],
        invoice_date=date.fromisoformat(truth["invoice_date"]),
        po_number=truth["po_number"],
        taxable_value=Decimal(truth["taxable_value"]),
        cgst=Decimal(truth["cgst"]),
        sgst=Decimal(truth["sgst"]),
        igst=Decimal(truth["igst"]),
        total=Decimal(truth["total"]),
        bank_account=truth["bank_account"],
        ifsc=truth["ifsc"],
        lines=[
            InvoiceLine(
                description=ln["description"],
                hsn=ln["hsn"],
                qty=Decimal(ln["qty"]),
                unit=ln["unit"],
                rate=Decimal(ln["rate"]),
                gst_rate=Decimal(ln["gst_rate"]),
                amount=Decimal(ln["amount"]),
            )
            for ln in truth["lines"]
        ],
    )


def _test_url() -> str:
    base = get_settings().khata_database_url
    return base.rsplit("/", 1)[0] + "/" + TEST_DB


@pytest.fixture(scope="module")
def test_db_url() -> str:
    """Create (once) and freshly seed the khata_test database. Skips if Postgres is down."""
    from db.seed import seed

    admin_url = get_settings().khata_database_url
    try:
        with psycopg.connect(admin_url, autocommit=True, connect_timeout=3) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)
            ).fetchone()
            if not exists:
                conn.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB))
                )
    except psycopg.OperationalError:
        pytest.skip("khata-db not reachable; run `make up`")
    url = _test_url()
    seed(url)
    return url


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def pool(test_db_url):
    p = make_pool(test_db_url)
    await p.open()
    yield p
    await p.close()
