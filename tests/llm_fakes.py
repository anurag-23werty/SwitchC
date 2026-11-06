"""A fake Groq + OpenRouter for offline tests, behind the real LLM client.

It answers over an httpx.MockTransport, so the client's request building,
parsing, validation, retries and fallbacks are all exercised. Extraction answers
are the golden truth for whichever invoice's text is in the prompt.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pdfplumber

from khata.agents.prompts import ANALYST, AUDITOR, INTAKE
from khata.llm import GROQ_URL, LLM, OPENROUTER_URL, Endpoint

ENDPOINTS = {
    "groq": Endpoint("groq", "groq", GROQ_URL, "k", "fake-groq-a"),
    "openrouter": Endpoint(
        "openrouter", "openrouter", OPENROUTER_URL, "k", "fake-or-b"
    ),
    "groq-secondary": Endpoint("groq-secondary", "groq", GROQ_URL, "k", "fake-groq-c"),
}
BOGUS_PRECEDENT = "XX/0000"


def _reply(content: dict, model: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        },
    )


def truth_as_extraction(truth: dict, total_override: str | None = None) -> dict:
    return {
        "vendor_name": truth["vendor_name"],
        "supplier_gstin": truth["supplier_gstin"],
        "buyer_gstin": truth["buyer_gstin"],
        "invoice_no": truth["invoice_no"],
        "invoice_date": truth["invoice_date"],
        "po_number": truth["po_number"],
        "taxable_value": truth["taxable_value"],
        "cgst": truth["cgst"],
        "sgst": truth["sgst"],
        "igst": truth["igst"],
        "total": total_override or truth["total"],
        "bank_account": truth["bank_account"],
        "ifsc": truth["ifsc"],
        "lines": [
            {
                k: ln[k]
                for k in (
                    "description",
                    "hsn",
                    "qty",
                    "unit",
                    "rate",
                    "gst_rate",
                    "amount",
                )
            }
            for ln in truth["lines"]
        ],
        "instructions_in_document": [],
        "confidence": 0.97,
        "reasons": ["clean digital PDF"],
    }


def _block(user: str, kind: str) -> list:
    return [
        json.loads(m)
        for m in re.findall(
            rf"<untrusted-{kind}-\w+>\n(.*?)\n</untrusted-{kind}-\w+>", user, re.S
        )
    ]


@dataclass
class FakeProviders:
    golden: list[dict]
    root: Path
    disagree_on: set[str] = field(default_factory=set)  # golden ids reader b misreads
    down: bool = False
    calls: list[tuple[str, str]] = field(default_factory=list)  # (model, agent kind)

    def __post_init__(self) -> None:
        self.texts = []
        for g in self.golden:
            with pdfplumber.open(self.root / g["file"]) as pdf:
                text = "\n".join(p.extract_text() or "" for p in pdf.pages)
            self.texts.append((text, g))

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        model = body["model"]
        system, user = body["messages"][0]["content"], body["messages"][1]["content"]
        if self.down:
            return httpx.Response(503, json={"error": "down"})
        if system.startswith(INTAKE):
            self.calls.append((model, "intake"))
            g = next(g for text, g in self.texts if text in user)
            override = None
            if g["id"] in self.disagree_on and model == "fake-or-b":
                override = "99999.00"
            return _reply(truth_as_extraction(g["truth"], override), model)
        if system.startswith(ANALYST):
            self.calls.append((model, "analyst"))
            facts = _block(user, "facts")[0]
            cited = [
                {
                    "invoice_no": k["invoice_no"],
                    "what_was_decided": k["text"][:120],
                    "why_relevant": "same flag",
                }
                for k in facts["knowledge"]
                if k["kind"] == "decision"
                and any(f in k["text"] for f in facts["flags"])
            ][:2]
            cited.append(
                {
                    "invoice_no": BOGUS_PRECEDENT,
                    "what_was_decided": "x",
                    "why_relevant": "made up",
                }
            )
            return _reply(
                {
                    "headline": f"Flags: {', '.join(facts['flags']) or 'none'}",
                    "explanation": "Fake analyst note.",
                    "precedents": cited,
                    "questions_for_approver": ["Is this expected?"],
                    "suggested_action": "investigate",
                    "confidence": 0.8,
                    "reasons": ["fake"],
                },
                model,
            )
        if system.startswith(AUDITOR):
            self.calls.append((model, "auditor"))
            return _reply(
                {
                    "consistent": True,
                    "issues": [],
                    "confidence": 0.9,
                    "reasons": ["fake"],
                },
                model,
            )
        return httpx.Response(400, json={"error": "unknown prompt"})

    def llm(self, pool=None) -> LLM:
        async def no_sleep(_: float) -> None:
            return None

        return LLM(
            ENDPOINTS,
            cache_dir=None,
            pool=pool,
            transport=httpx.MockTransport(self.handler),
            sleep=no_sleep,
        )
