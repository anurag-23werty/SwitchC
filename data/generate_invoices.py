"""Print every invoice in data/fixtures.py as a PDF and write evals/golden.yaml.

    uv run python -m data.generate_invoices

Three layouts (classic / modern / plain) so extraction cannot lean on one
template. Amounts use "Rs." because the PDF base fonts have no rupee glyph,
which is also what many small Indian businesses print.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from data import fixtures as fx
from khata.checks.gst import STATE_NAMES
from khata.money import amount_in_words, inr

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "invoices"
GOLDEN = ROOT / "evals" / "golden.yaml"

_base = getSampleStyleSheet()
BODY = ParagraphStyle("body", parent=_base["Normal"], fontSize=8.5, leading=11)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=7.5, leading=9.5)
RIGHT = ParagraphStyle("right", parent=BODY, alignment=TA_RIGHT)
TITLE = ParagraphStyle("title", parent=_base["Title"], fontSize=15, spaceAfter=2)


def file_name(spec: fx.InvoiceSpec) -> str:
    safe = spec.invoice_no.replace("/", "-")
    return f"{spec.key}_{spec.vendor.code}_{safe}.pdf"


def _qty(q: Decimal) -> str:
    return f"{q.normalize():f}"


def _party_block(p: fx.Party, gstin: str, heading: str | None) -> Paragraph:
    lines = [f"<b>{heading}</b>"] if heading else []
    lines += [
        f"<b>{p.name}</b>",
        *p.address,
        f"GSTIN: {gstin}",
        f"State: {STATE_NAMES[p.state_code]} ({p.state_code})",
    ]
    if heading is None:
        lines.append(f"Ph: {p.phone} | {p.email}")
    return Paragraph("<br/>".join(lines), BODY)


def _tax_rows(pr: fx.Printed) -> list[tuple[str, Decimal]]:
    rates = {line["gst_rate"] for line in pr.lines}
    single = rates.pop() if len(rates) == 1 else None
    if pr.tax_mode == "IGST":
        label = f"IGST @ {single.normalize():f}%" if single is not None else "IGST"
        return [(label, pr.igst)]
    half = f" @ {(single / 2).normalize():f}%" if single is not None else ""
    return [(f"CGST{half}", pr.cgst), (f"SGST{half}", pr.sgst)]


def build_pdf(spec: fx.InvoiceSpec, path: Path) -> None:
    v, pr = spec.vendor, fx.printed(spec)
    accent = colors.HexColor(v.accent)
    story = []

    meta = Paragraph(
        "<br/>".join(
            [
                f"<b>Invoice No:</b> {spec.invoice_no}",
                f"<b>Invoice Date:</b> {spec.invoice_date.strftime('%d-%m-%Y')}",
                f"<b>PO Ref:</b> {pr.po_ref or '-'}",
                f"<b>Place of Supply:</b> {STATE_NAMES[fx.COMPANY.state_code]} ({fx.COMPANY.state_code})",
            ]
        ),
        BODY,
    )
    supplier = _party_block(v, pr.supplier_gstin, None)
    buyer = _party_block(fx.COMPANY, fx.COMPANY.gstin, "Bill To:")

    if v.style == "modern":
        band = Table(
            [
                [
                    Paragraph(
                        f"<font color='white' size='16'><b>{v.name}</b></font>", BODY
                    ),
                    Paragraph(
                        "<font color='white' size='13'><b>TAX INVOICE</b></font>", RIGHT
                    ),
                ]
            ],
            colWidths=[120 * mm, 60 * mm],
        )
        band.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), accent),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story += [band, Spacer(1, 4 * mm)]
        head = [[supplier, meta], [buyer, ""]]
    elif v.style == "plain":
        story += [Paragraph("TAX INVOICE", TITLE), Spacer(1, 2 * mm)]
        head = [[supplier, meta], [buyer, ""]]
    else:
        story += [
            Paragraph(f"<font color='{v.accent}'>{v.name}</font>", TITLE),
            Paragraph("<b>TAX INVOICE</b> - Original for Recipient", BODY),
            Spacer(1, 3 * mm),
        ]
        head = [[supplier, buyer], [meta, ""]]

    head_tbl = Table(head, colWidths=[95 * mm, 85 * mm])
    head_style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]
    if v.style == "classic":
        head_style += [
            ("BOX", (0, 0), (-1, -1), 0.6, colors.grey),
            ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.lightgrey),
        ]
    head_tbl.setStyle(TableStyle(head_style))
    story += [head_tbl, Spacer(1, 4 * mm)]

    rows = [
        [
            "#",
            "Description",
            "HSN/SAC",
            "Qty",
            "Unit",
            "Rate (Rs.)",
            "GST %",
            "Amount (Rs.)",
        ]
    ]
    for line in pr.lines:
        rows.append(
            [
                str(line["line_no"]),
                Paragraph(line["description"], SMALL),
                line["hsn"],
                _qty(line["qty"]),
                line["unit"],
                inr(line["rate"]),
                f"{line['gst_rate'].normalize():f}",
                inr(line["amount"]),
            ]
        )
    items = Table(
        rows,
        colWidths=[
            8 * mm,
            62 * mm,
            18 * mm,
            14 * mm,
            13 * mm,
            22 * mm,
            13 * mm,
            30 * mm,
        ],
        repeatRows=1,
    )
    item_style = [
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
    if v.style == "plain":
        item_style += [
            ("LINEABOVE", (0, 0), (-1, 0), 0.8, colors.black),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black),
            ("LINEBELOW", (0, -1), (-1, -1), 0.8, colors.black),
        ]
    else:
        item_style += [
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            (
                "BACKGROUND",
                (0, 0),
                (-1, 0),
                accent if v.style == "modern" else colors.whitesmoke,
            ),
            (
                "TEXTCOLOR",
                (0, 0),
                (-1, 0),
                colors.white if v.style == "modern" else colors.black,
            ),
        ]
    items.setStyle(TableStyle(item_style))
    story += [items, Spacer(1, 3 * mm)]

    totals = [["Taxable Value", inr(pr.taxable)]]
    totals += [[label, inr(amount)] for label, amount in _tax_rows(pr)]
    totals.append(["Invoice Total (Rs.)", inr(pr.total)])
    tot = Table(totals, colWidths=[45 * mm, 32 * mm], hAlign="RIGHT")
    tot.setStyle(
        TableStyle(
            [
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black),
            ]
        )
    )
    story += [
        tot,
        Spacer(1, 2 * mm),
        Paragraph(f"<b>Amount in words:</b> {amount_in_words(pr.total)}", BODY),
        Spacer(1, 4 * mm),
    ]

    assert pr.bank is not None
    story.append(
        Paragraph(
            "<b>Bank details for payment</b><br/>"
            f"Account name: {v.name}<br/>Bank: {pr.bank.name}<br/>"
            f"A/c No: {pr.bank.account}<br/>IFSC: {pr.bank.ifsc}",
            BODY,
        )
    )
    if spec.notes:
        story += [
            Spacer(1, 3 * mm),
            Paragraph("<b>Notes:</b> " + " ".join(spec.notes), SMALL),
        ]
    story += [
        Spacer(1, 10 * mm),
        Paragraph(f"For <b>{v.name}</b><br/><br/>Authorised Signatory", RIGHT),
        Spacer(1, 4 * mm),
        Paragraph(
            "This is a computer generated invoice. Subject to Bengaluru jurisdiction.",
            SMALL,
        ),
    ]

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=f"Invoice {spec.invoice_no}",
        author=v.name,
    )
    doc.build(story)


def _s(d: Decimal) -> str:
    return f"{d:.2f}"


def golden_entry(seq: int, spec: fx.InvoiceSpec) -> dict:
    pr = fx.printed(spec)
    assert pr.bank is not None
    e = spec.expected
    return {
        "id": spec.key,
        "sequence": seq,
        "demo": spec.demo,
        "title": spec.title,
        "file": f"data/invoices/{file_name(spec)}",
        "vendor_code": spec.vendor.code,
        "truth": {
            "vendor_name": spec.vendor.name,
            "supplier_gstin": pr.supplier_gstin,
            "buyer_gstin": fx.COMPANY.gstin,
            "invoice_no": spec.invoice_no,
            "invoice_date": spec.invoice_date.isoformat(),
            "po_number": pr.po_ref,
            "taxable_value": _s(pr.taxable),
            "cgst": _s(pr.cgst),
            "sgst": _s(pr.sgst),
            "igst": _s(pr.igst),
            "total": _s(pr.total),
            "bank_account": pr.bank.account,
            "ifsc": pr.bank.ifsc,
            "lines": [
                {
                    "description": ln["description"],
                    "hsn": ln["hsn"],
                    "qty": _qty(ln["qty"]),
                    "unit": ln["unit"],
                    "rate": _s(ln["rate"]),
                    "gst_rate": _qty(ln["gst_rate"]),
                    "amount": _s(ln["amount"]),
                }
                for ln in pr.lines
            ],
        },
        "expected": {
            "flags": list(e.flags),
            "route": e.route,
            "gate_state": e.gate_state,
            "human": [
                {"by": h.by, "decision": h.decision, "reason": h.reason}
                for h in e.human
            ],
            "final_state": e.final_state,
            **(
                {"analyst_should_cite": spec.analyst_should_cite}
                if spec.analyst_should_cite
                else {}
            ),
        },
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*.pdf"):
        old.unlink()
    entries = []
    for seq, spec in enumerate(fx.INVOICES, start=1):
        build_pdf(spec, OUT_DIR / file_name(spec))
        entries.append(golden_entry(seq, spec))
    header = (
        "# Khata golden set: generated by `make seed` from data/fixtures.py. Do not edit.\n"
        "# `truth` is what each PDF prints (extraction targets). `expected` is the\n"
        "# hand-labelled outcome. Process invoices in `sequence` order.\n"
    )
    GOLDEN.write_text(
        header
        + yaml.safe_dump(
            {"as_of": fx.TODAY.isoformat(), "invoices": entries},
            sort_keys=False,
            allow_unicode=True,
        )
    )
    demo = sum(1 for s in fx.INVOICES if s.demo)
    print(
        f"wrote {len(entries)} invoices ({demo} demo, {len(entries) - demo} eval-only) to "
        f"{OUT_DIR.relative_to(ROOT)} and {GOLDEN.relative_to(ROOT)}"
    )


if __name__ == "__main__":
    main()
