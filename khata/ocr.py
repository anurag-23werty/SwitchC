"""Reading scans and photos: Tesseract OCR, run locally (no API, no cost).

Vendors often send a scanned PDF or a phone photo instead of a digital PDF. Those
have no text layer, so Khata renders each page and runs Tesseract on it. OCR can
misread a digit, so an invoice read this way always goes to a person (the workflow
adds LOW_CONFIDENCE); Khata never auto-approves a scan.

Needs the `tesseract` program: `apt install tesseract-ocr` (the Docker image has
it), `brew install tesseract` on a Mac. Without it, a scan is held with a message
asking for the digital PDF. KHATA_OCR_LANG picks the language (default "eng").
"""

from __future__ import annotations

import os
import re
from pathlib import Path

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
MIN_CHARS_PER_PAGE = 40  # less text than this and the page is treated as a scan


class OcrUnavailable(RuntimeError):
    """Tesseract isn't installed or failed."""


def looks_scanned(text: str, pages: int) -> bool:
    return len(re.sub(r"\s", "", text)) < MIN_CHARS_PER_PAGE * max(pages, 1)


def engine() -> str:
    try:
        import pytesseract

        return f"tesseract {pytesseract.get_tesseract_version()}"
    except Exception as e:  # not installed
        raise OcrUnavailable(f"tesseract is not installed ({type(e).__name__})") from e


def _ocr_images(images) -> str:
    import pytesseract

    lang = os.environ.get("KHATA_OCR_LANG", "eng")
    try:
        return "\n".join(
            pytesseract.image_to_string(img, lang=lang, config="--psm 6")
            for img in images
        )
    except pytesseract.TesseractNotFoundError as e:
        raise OcrUnavailable("tesseract is not installed") from e
    except pytesseract.TesseractError as e:
        raise OcrUnavailable(f"tesseract failed: {e}") from e


def ocr_pdf(path: Path, dpi: int = 300) -> str:
    import pypdfium2 as pdfium

    engine()
    pdf = pdfium.PdfDocument(str(path))
    try:
        images = [pdf[i].render(scale=dpi / 72).to_pil() for i in range(len(pdf))]
    finally:
        pdf.close()
    return _ocr_images(images)


def ocr_image(path: Path) -> str:
    from PIL import Image, ImageOps

    engine()
    with Image.open(path) as img:
        return _ocr_images([ImageOps.exif_transpose(img).convert("L")])
