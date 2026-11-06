"""Prompt-injection detection over invoice text. Deterministic, so it can't be talked out of it.

An invoice is data from outside the company. Text in it that addresses an AI or
an automated system, or claims an approval, is an attempt to steer the agents.
This detector runs before any LLM reads the document; a model-based
classifier is added on top in Phase 3, never instead.

The patterns aim at text *addressed to machines*. Social engineering aimed at
people ("our bank details have changed, kindly remit to the new account") is
not injection; the bank-change check catches that.
"""

from __future__ import annotations

import re
import unicodedata

from khata.models import CheckReport, Flag

_PATTERNS: list[tuple[str, str]] = [
    (
        "override_instructions",
        r"\b(ignore|disregard|forget|override)\b[\w\s,]{0,30}\b(previous|prior|above|earlier|all|your|system)\b[\w\s]{0,20}\b(instructions?|prompts?|rules?|guidelines?|checks?)",
    ),
    (
        "addresses_ai",
        r"\b(ai|llm|gpt|chatbot|language model|assistant|ai agent|automated (invoice )?(processing )?(systems?|agents?)|bots?)\b\s*[:,-]",
    ),
    ("addresses_automation", r"\bnote to (automated|ai|machine|invoice processing)\b"),
    (
        "claims_approval",
        r"\b(pre-?approved|already approved|has been approved|approval (is )?granted)\b",
    ),
    (
        "commands_approval",
        r"\b(mark|set|flag|treat)\b[\w\s]{0,20}\b(as )?(approved|paid|verified|clean)\b",
    ),
    (
        "commands_payment",
        r"\b(release|schedule|process|make)\b[\w\s]{0,15}\bpayment\b[\w\s]{0,15}\b(immediately|now|today|urgently)\b",
    ),
    (
        "skip_controls",
        r"\b(skip|bypass|disable|do not run|don't run)\b[\w\s]{0,15}\b(checks?|verification|validation|review|approval)\b",
    ),
    (
        "suppress_reporting",
        r"\b(do not|don't|never)\b[\w\s]{0,10}\b(flag|report|mention|escalate)\b",
    ),
    (
        "role_play",
        r"\b(you are now|act as|new instructions|system prompt|developer mode)\b",
    ),
]
_COMPILED = [(name, re.compile(rx, re.IGNORECASE)) for name, rx in _PATTERNS]
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))


def normalise(text: str) -> str:
    """Undo the usual disguises: odd Unicode forms, zero-width characters, broken lines."""
    text = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    return re.sub(r"\s+", " ", text)


def detect_injection(text: str) -> CheckReport:
    """Flag text that tries to instruct the agents. Returns every match with context."""
    r = CheckReport(check="detect_injection")
    clean = normalise(text)
    hits = []
    for name, rx in _COMPILED:
        for m in rx.finditer(clean):
            start, end = max(m.start() - 40, 0), min(m.end() + 40, len(clean))
            hits.append(
                {"pattern": name, "match": m.group(0), "context": clean[start:end]}
            )
    if hits:
        patterns = sorted({h["pattern"] for h in hits})
        r.add(
            Flag.INJECTION,
            f"The document contains text aimed at automated systems ({', '.join(patterns)}). "
            "Treated as data, never as instructions.",
            hits=hits[:10],
        )
    else:
        r.add(None, "No instructions aimed at automated systems found")
    return r
