"""A sample invoice for the example business in examples/ (Kaveri Home Products),
so a fresh install can be tried end to end without the demo data.

    uv run python scripts/sample_invoice.py      # -> examples/invoices/NOV-INV-1001.pdf

Nova Packaging bills PO-2026-0001: the boxes match, but 200 rolls of tape are billed
while the goods receipt shows 180, and the total is above the manager's limit, so
Khata should route it to the finance manager, then the owner, with QTY_VARIANCE.
All names, GSTINs and accounts are fictional.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from data import fixtures as fx
from data import generate_invoices as gen

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "examples" / "invoices"

KAVERI = fx.Party(
    "KHP",
    "Kaveri Home Products Pvt Ltd",
    "29",
    "AABCK1234M",
    ("14, 2nd Cross, Peenya Industrial Area", "Bengaluru 560058"),
    "+91 80 4000 9999",
    "accounts@kaveri.example",
    fx.Bank("HDFC Bank", "50200012345678", "HDFC0001234"),
    date(2020, 1, 1),
)
NOVA = fx.Party(
    "NOV",
    "Nova Packaging Pvt Ltd",
    "29",
    "AAFCN5678P",
    ("Plot 8, KIADB, Dobbaspet", "Karnataka 562111"),
    "+91 80 4000 1111",
    "accounts@novapack.example",
    fx.Bank("Canara Bank", "0412201005555", "CNRB0000412"),
    date(2025, 4, 1),
    style="modern",
    accent="#0b5d3b",
)
PO = fx.PO(
    "PO-2026-0001",
    NOVA,
    date(2026, 11, 1),
    (
        fx.Line("Corrugated boxes 5-ply 18x12x12 in", "4819", 2000, "nos", "62.00"),
        fx.Line("BOPP packing tape 48mm", "3919", 200, "roll", "45.00"),
    ),
)
SPEC = fx.InvoiceSpec(
    "SAMPLE",
    "Example business: tape billed beyond what was received",
    NOVA,
    "NOV/INV/1001",
    date(2026, 11, 8),
    PO,
    expected=fx.Expected(
        ("QTY_VARIANCE",),
        "manager_then_owner",
        "AWAITING_APPROVAL",
        "AWAITING_APPROVAL",
    ),
)


def main() -> None:
    fx.COMPANY = KAVERI  # the generator prints fx.COMPANY as the buyer
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "NOV-INV-1001.pdf"
    gen.build_pdf(SPEC, path)
    print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
