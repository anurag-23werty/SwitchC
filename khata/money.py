"""Money helpers. Every amount is a Decimal rounded to paise; floats never touch money."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

PAISE = Decimal("0.01")


def money(value: Decimal | int | str) -> Decimal:
    """Round to paise, half-up (how Indian invoices round)."""
    if isinstance(value, float):
        raise TypeError("money() refuses floats; pass a str or Decimal")
    return Decimal(value).quantize(PAISE, rounding=ROUND_HALF_UP)


def inr(value: Decimal) -> str:
    """Indian digit grouping: 240720 -> '2,40,720.00'."""
    value = money(value)
    sign = "-" if value < 0 else ""
    whole, frac = f"{abs(value):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups) + "," + tail
    return f"{sign}{whole}.{frac}"


_ONES = (
    "Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen "
    "Fourteen Fifteen Sixteen Seventeen Eighteen Nineteen"
).split()
_TENS = "_ _ Twenty Thirty Forty Fifty Sixty Seventy Eighty Ninety".split()


def _below_thousand(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(f"{_ONES[n // 100]} Hundred")
        n %= 100
    if n >= 20:
        parts.append(_TENS[n // 10] + (f" {_ONES[n % 10]}" if n % 10 else ""))
    elif n:
        parts.append(_ONES[n])
    return " ".join(parts)


def _words(n: int) -> str:
    if n == 0:
        return "Zero"
    parts = []
    for size, name in ((10_000_000, "Crore"), (100_000, "Lakh"), (1_000, "Thousand")):
        if n >= size:
            parts.append(f"{_words(n // size)} {name}")
            n %= size
    if n:
        parts.append(_below_thousand(n))
    return " ".join(parts)


def amount_in_words(value: Decimal) -> str:
    """'Rupees Two Lakh Forty Thousand Seven Hundred Twenty and Paise Fifty Only'."""
    value = money(value)
    rupees = int(value)
    paise = int((value - rupees) * 100)
    text = f"Rupees {_words(rupees)}"
    if paise:
        text += f" and Paise {_words(paise)}"
    return text + " Only"
