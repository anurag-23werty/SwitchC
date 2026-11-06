"""Intake: two independent models read the invoice; code decides whether to trust it.

The two readings are compared field by field. The fields that move money or
identify who gets it (total, supplier GSTIN, invoice number, bank account) must
match exactly, or the invoice goes ON_HOLD with both readings for a person to
look at. Agreed values must also literally appear in the document text, which
catches a model that hallucinates the same plausible value twice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from khata.agents.prompts import INTAKE, wrap_untrusted
from khata.agents.schemas import Extraction
from khata.llm import LLM, LLMResult, LLMUnavailable
from khata.models import Flag, InvoiceData

CRITICAL = ("total", "supplier_gstin", "invoice_no", "bank_account")
READER_CHAINS = {"a": ("groq",), "b": ("openrouter", "groq-secondary")}


def _norm(field_name: str, value: Any) -> Any:
    if value is None:
        return None
    if field_name == "total":
        return Decimal(value)
    if field_name == "bank_account":
        return re.sub(r"\D", "", str(value))
    return re.sub(r"\s", "", str(value)).upper()


def _squash(text: str) -> str:
    return re.sub(r"[\s,]", "", text).upper()


def grounded(field_name: str, value: Any, raw_text: str) -> bool:
    """Does this agreed value literally appear in the document?"""
    if value is None:
        return True
    hay = _squash(raw_text)
    needle = f"{value:.2f}" if field_name == "total" else _squash(str(value))
    return needle in hay


@dataclass
class Reading:
    """One reader's result: the transcription, who produced it, and whether it parsed."""

    reader: str
    endpoint: str | None = None
    model: str | None = None
    cached: bool = False
    extraction: Extraction | None = None
    data: InvoiceData | None = None
    error: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "reader": self.reader,
            "endpoint": self.endpoint,
            "model": self.model,
            "cached": self.cached,
            "error": self.error,
            "confidence": self.extraction.confidence if self.extraction else None,
            "reasons": self.extraction.reasons if self.extraction else [],
            "instructions_in_document": (
                self.extraction.instructions_in_document if self.extraction else []
            ),
            "fields": self.data.model_dump(mode="json") if self.data else None,
        }

    def dump(self) -> dict[str, Any]:
        """JSON-safe form, for the workflow's checkpointed state."""
        return {
            "reader": self.reader,
            "endpoint": self.endpoint,
            "model": self.model,
            "cached": self.cached,
            "error": self.error,
            "extraction": (
                self.extraction.model_dump(mode="json") if self.extraction else None
            ),
        }

    @classmethod
    def load(cls, d: dict[str, Any]) -> Reading:
        r = cls(d["reader"], d["endpoint"], d["model"], d["cached"], error=d["error"])
        if d["extraction"] is not None:
            r.extraction = Extraction.model_validate(d["extraction"])
            if r.error is None:
                r.data = r.extraction.to_invoice_data()
        return r


async def read_invoice(
    llm: LLM, reader: str, text: str, invoice_id: int | None
) -> Reading:
    """Ask one reader for a transcription. Never raises: failures become `error`."""
    out = Reading(reader)
    try:
        res: LLMResult[Extraction] = await llm.structured(
            Extraction,
            system=INTAKE,
            user="Transcribe this invoice.\n\n" + wrap_untrusted(text),
            chain=READER_CHAINS[reader],
            agent=f"intake-{reader}",
            invoice_id=invoice_id,
        )
    except LLMUnavailable as e:
        out.error = f"unavailable: {e}"[:500]
        return out
    out.endpoint, out.model, out.cached = res.endpoint, res.model, res.cached
    out.extraction = res.value
    try:
        out.data = res.value.to_invoice_data()
    except (ValueError, ArithmeticError) as e:
        out.error = f"unparseable transcription: {e}"[:500]
    return out


@dataclass
class Reconciled:
    data: InvoiceData | None
    flags: list[Flag] = field(default_factory=list)
    disagreements: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    confidence: Decimal = Decimal("0")

    def readings_record(self, a: Reading, b: Reading) -> dict[str, Any]:
        return {
            "a": a.summary(),
            "b": b.summary(),
            "disagreements": {
                k: {"a": str(v["a"]), "b": str(v["b"])}
                for k, v in self.disagreements.items()
            },
            "notes": self.notes,
            "flags": [f.value for f in self.flags],
        }


def reconcile(
    a: Reading, b: Reading, raw_text: str, min_confidence: Decimal
) -> Reconciled:
    """Compare two readings. Pure code: this is where trust in the extraction is decided."""
    usable = [r for r in (a, b) if r.data is not None]
    if not usable:
        return Reconciled(None, notes=[f"reader {r.reader}: {r.error}" for r in (a, b)])
    primary = usable[0]
    out = Reconciled(primary.data)
    confs = [Decimal(str(r.extraction.confidence)) for r in usable if r.extraction]
    out.confidence = min(confs).quantize(Decimal("0.001"))

    if len(usable) == 1:
        other = b if primary is a else a
        out.flags.append(Flag.LOW_CONFIDENCE)
        out.notes.append(
            f"Only one reading: reader {other.reader} failed ({other.error})."
        )
    else:
        if (a.model or "").split(":")[0] == (b.model or "").split(":")[0]:
            out.flags.append(Flag.LOW_CONFIDENCE)
            out.notes.append(f"Both readings came from the same model ({a.model}).")
        for f in CRITICAL:
            va = _norm(f, getattr(a.data, f))
            vb = _norm(f, getattr(b.data, f))
            if va != vb:
                out.disagreements[f] = {"a": va, "b": vb}
        if out.disagreements:
            out.flags.append(Flag.EXTRACTION_DISAGREEMENT)
            out.notes.append(
                "Readers disagree on "
                + ", ".join(
                    f"{k} ({v['a']} vs {v['b']})" for k, v in out.disagreements.items()
                )
            )

    if not out.disagreements:
        missing = [
            f
            for f in CRITICAL
            if not grounded(f, _norm(f, getattr(primary.data, f)), raw_text)
        ]
        if missing:
            out.flags.append(Flag.LOW_CONFIDENCE)
            out.notes.append(
                "Not found verbatim in the document: " + ", ".join(missing)
            )
    if out.confidence < min_confidence and Flag.LOW_CONFIDENCE not in out.flags:
        out.flags.append(Flag.LOW_CONFIDENCE)
        out.notes.append(
            f"Reader confidence {out.confidence} is below the policy minimum "
            f"{min_confidence}."
        )
    out.flags = list(dict.fromkeys(out.flags))
    return out
