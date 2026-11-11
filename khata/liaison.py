"""Vendor Liaison: Khata's agent in the vendor inbox (#ap-inbox, bridged to Telegram).

Vendors are outsiders, so this agent is deliberately plain code, with no LLM:

* Only registered vendor contacts (`vendor_contacts`) get answers, and only
  about their own company's invoices. Anyone else is told how to get registered,
  and nothing else.
* "Status of DPK/1187?" -> the invoice's stage in words a vendor should see.
  Internal reasons stay internal: a rejection for something the vendor can fix
  (wrong GST, bad arithmetic, duplicate number) is explained; any other
  rejection or hold just points to the accounts team.
* A PDF -> forwarded to #ap-desk as a new invoice, where the team takes over.
* A request to change bank details -> refused in the chat, and the owner is
  alerted in #ap-desk to call the vendor on the number on file. Bank details
  are never changed from a chat message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from khata.money import inr

LIAISON = "khata-liaison"

BANK_WORDS = re.compile(r"(?i)\b(bank|a/?c|account\s*(no|number)?|ifsc|beneficiary)\b")
CHANGE_WORDS = re.compile(
    r"(?i)\b(change[ds]?|new|update[ds]?|switch(ed)?|moved?|replace[ds]?|different)\b"
)
INVOICE_NO = re.compile(r"(?<![\w/-])[A-Za-z0-9][A-Za-z0-9/-]*[A-Za-z0-9]")

# What a vendor may be told about a rejection: only what they can fix.
VENDOR_FIXABLE = {
    "GSTIN_INVALID": "the supplier GSTIN on it isn't valid",
    "GST_TYPE_MISMATCH": "it charges the wrong type of GST (IGST vs CGST + SGST)",
    "GST_SPLIT_UNEQUAL": "CGST and SGST aren't equal halves",
    "ARITHMETIC": "the totals don't add up",
    "BUYER_GSTIN_MISMATCH": "it is billed to the wrong GSTIN for us",
    "DUPLICATE": "we already have an invoice with this number",
}
STAGE = {
    "RECEIVED": "received; our team is checking it",
    "EXTRACTED": "received; our team is checking it",
    "VALIDATED": "received; our team is checking it",
    "MATCHED": "received; our team is checking it",
    "REVIEWED": "received; our team is checking it",
    "AWAITING_APPROVAL": "checked, and waiting for approval by our finance team",
    "ON_HOLD": "under review by our team; we'll contact you if we need anything",
    "APPROVED": "approved; our accounts team will release the payment as per terms",
    "PAYMENT_DRAFTED": "approved; our accounts team will release the payment as per terms",
}


@dataclass
class Intent:
    kind: str  # bank_change | status | invoice_upload | help
    invoice_nos: list[str]


def classify(text: str, has_pdf: bool) -> Intent:
    """What a vendor's message asks for. Pure code; the text is untrusted."""
    if has_pdf:
        return Intent("invoice_upload", [])
    if BANK_WORDS.search(text) and CHANGE_WORDS.search(text):
        return Intent("bank_change", [])
    nos = [m for m in INVOICE_NO.findall(text) if re.search(r"\d", m) and len(m) >= 4]
    if nos or re.search(r"(?i)\b(status|paid|payment|invoice|update|when)\b", text):
        return Intent("status", nos[:5])
    return Intent("help", [])


def status_line(row: dict[str, Any]) -> str:
    """One invoice's status, in words fit for the vendor."""
    total = (
        f"₹{inr(Decimal(str(row['total'])))}" if row.get("total") is not None else ""
    )
    head = f"{row['invoice_no']} {total}".strip()
    if row["state"] != "REJECTED":
        return f"{head}: {STAGE.get(row['state'], 'with our team')}."
    fixable = [VENDOR_FIXABLE[f] for f in row.get("flags") or [] if f in VENDOR_FIXABLE]
    if fixable:
        again = (
            ""
            if "DUPLICATE" in (row.get("flags") or [])
            else " Please send a corrected invoice."
        )
        return f"{head}: returned, because {'; '.join(fixable)}.{again}"
    return f"{head}: not approved. Please contact our accounts team for the details."


def not_registered(company: str) -> str:
    return (
        "Hello! I can only discuss invoices with registered vendor contacts. Please ask "
        f"your account manager at {company} to register you, then try again."
    )


def help_text(contact: dict[str, Any]) -> str:
    return (
        f"Hi {contact['display_name'].split()[0]}, I'm {contact['vendor_name']}'s contact "
        "point for invoices here. Send an invoice as a PDF, or ask for the status of one "
        "by its number (for example: status of "
        f"{contact['vendor_code']}/1187)."
    )


def bank_change_reply(contact: dict[str, Any]) -> str:
    return (
        "We never change bank details from a chat message, so nothing has been changed. "
        "Our team will call your company on the phone number we already have on file to "
        "confirm. Until then, payments go only to the account in our records."
    )


def bank_change_alert(contact: dict[str, Any], owner: str, text: str) -> str:
    quoted = text.strip().replace("\n", " ")[:300]
    return (
        f"@{owner} **Bank-detail change requested in the vendor inbox.** "
        f"{contact['display_name']} (@{contact['username']}, {contact['vendor_name']}) wrote: "
        f'"{quoted}"\n'
        f"Nothing was changed. Call {contact['vendor_name']} on {contact['vendor_phone']} "
        "(the number on file, not one from the chat) before accepting any new account. "
        "Until then every invoice from them that names a different account is a hard stop."
    )


def upload_caption(contact: dict[str, Any], file_name: str) -> str:
    return (
        f"Invoice from {contact['vendor_name']}, sent by {contact['display_name']} "
        f"(@{contact['username']}) through the vendor inbox: `{file_name}`."
    )


def upload_ack(file_name: str) -> str:
    return (
        f"Thanks, `{file_name}` is with our accounts team. Ask me for its status any time "
        "using the invoice number."
    )
