"""Scans and photos: read by OCR when Tesseract is there (and always sent to a person);
held with a clear message when they can't be read. Never auto-approved."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from khata import ocr
from khata.runtime import Khata

from .conftest import GOLDEN, ROOT
from .llm_fakes import FakeProviders

BY_ID = {g["id"]: g for g in GOLDEN}
SCANS = ROOT / "data" / "inbox" / "_test_scans"


def scan_of(gid: str, kind: str = "pdf") -> Path:
    """An image-only copy of a golden invoice: what a scanner or a phone produces."""
    SCANS.mkdir(parents=True, exist_ok=True)
    pdf = pdfium.PdfDocument(str(ROOT / BY_ID[gid]["file"]))
    img = pdf[0].render(scale=200 / 72).to_pil().convert("RGB")
    pdf.close()
    out = SCANS / f"{gid}_scan.{kind}"
    img.save(out, "PDF" if kind == "pdf" else "PNG", resolution=200)
    return out


def has_tesseract() -> bool:
    try:
        ocr.engine()
        return True
    except ocr.OcrUnavailable:
        return False


def test_a_page_without_text_is_recognised_as_a_scan():
    assert ocr.looks_scanned("", 1) and ocr.looks_scanned("  \n p. 1 ", 1)
    assert not ocr.looks_scanned("Invoice No: SOS/26-27/0412 " * 3, 1)


@pytest.mark.asyncio
async def test_an_unreadable_scan_is_held_and_no_model_is_asked(
    test_db_url, tmp_path, monkeypatch
):
    def unavailable(*_a, **_k):
        raise ocr.OcrUnavailable("tesseract is not installed")

    monkeypatch.setattr(ocr, "engine", unavailable)
    async with Khata.open(
        database_url=test_db_url,
        llm=FakeProviders(GOLDEN, ROOT).llm(),
        guard=False,
        payments_dir=tmp_path,
        today=date(2026, 11, 6),
    ) as k:
        out = await k.submit(scan_of("D01"))
        async with k.pool.connection() as conn:
            calls = await (
                await conn.execute(
                    "SELECT count(*) AS n FROM llm_calls WHERE invoice_id = %s",
                    (out.invoice_id,),
                )
            ).fetchone()
    assert out.state == "ON_HOLD" and not out.paused
    text = out.log[-1]["text"]
    assert "couldn't read it" in text and "digital PDF" in text
    assert calls["n"] == 0


@pytest.mark.skipif(not has_tesseract(), reason="tesseract not installed")
@pytest.mark.parametrize("kind", ["pdf", "png"])
def test_ocr_reads_the_invoice_number_and_total(kind):
    path = scan_of("D01", kind)
    text = ocr.ocr_pdf(path) if kind == "pdf" else ocr.ocr_image(path)
    squashed = text.replace(" ", "")
    assert "SOS/26-27/0412" in squashed and "6,395.60" in squashed


@pytest.mark.asyncio
async def test_a_scan_read_by_ocr_always_goes_to_a_person(
    test_db_url, tmp_path, monkeypatch
):
    """D01 is normally auto-approved; as a scan it must wait for a person."""
    import pdfplumber

    with pdfplumber.open(ROOT / BY_ID["G10"]["file"]) as pdf:
        real_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    monkeypatch.setattr(ocr, "engine", lambda: "tesseract (test)")
    monkeypatch.setattr(ocr, "ocr_pdf", lambda path: real_text)
    async with Khata.open(
        database_url=test_db_url,
        llm=FakeProviders(GOLDEN, ROOT).llm(),
        guard=False,
        payments_dir=tmp_path,
        today=date(2026, 11, 6),
    ) as k:
        out = await k.submit(scan_of("G10"))
    assert (
        BY_ID["G10"]["expected"]["route"] == "auto"
    )  # clean, small: auto when digital
    assert out.paused and out.state == "ON_HOLD"
    assert "LOW_CONFIDENCE" in out.request["flags"]
    assert "read it with OCR" in out.log[0]["text"]
