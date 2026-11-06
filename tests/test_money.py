from decimal import Decimal

import pytest

from khata.money import amount_in_words, inr, money
from khata.payments import idempotency_key


def test_money_rounds_half_up_to_paise():
    assert money("10.005") == Decimal("10.01")
    assert money("10.004") == Decimal("10.00")


def test_money_refuses_floats():
    with pytest.raises(TypeError):
        money(0.1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("240720", "2,40,720.00"),
        ("6395.6", "6,395.60"),
        ("999", "999.00"),
        ("12345678.9", "1,23,45,678.90"),
    ],
)
def test_indian_grouping(value, text):
    assert inr(Decimal(value)) == text


def test_amount_in_words():
    assert (
        amount_in_words(Decimal("240720"))
        == "Rupees Two Lakh Forty Thousand Seven Hundred Twenty Only"
    )
    assert amount_in_words(Decimal("58905.60")) == (
        "Rupees Fifty Eight Thousand Nine Hundred Five and Paise Sixty Only"
    )


def test_idempotency_key_is_stable_and_normalised():
    a = idempotency_key("DPK", "DPK/1187", Decimal("48144"))
    assert a == idempotency_key("dpk", " dpk/1187 ", Decimal("48144.00"))
    assert a != idempotency_key("DPK", "DPK/1187", Decimal("48144.01"))
