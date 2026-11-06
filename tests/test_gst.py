import pytest

from khata.checks.gst import (
    gstin_check_char,
    is_valid_format,
    is_valid_gstin,
    make_gstin,
    state_code,
)

# The example GSTIN used throughout GST portal documentation.
PUBLISHED_EXAMPLE = "27AAPFU0939F1ZV"


def test_published_example_is_valid():
    assert is_valid_gstin(PUBLISHED_EXAMPLE)
    assert gstin_check_char(PUBLISHED_EXAMPLE[:14]) == "V"


@pytest.mark.parametrize(
    "wrong", "0123456789ABCDEFGHIJKLMNOPQRSTUWXYZ"
)  # every char but V
def test_any_other_check_char_fails(wrong):
    assert not is_valid_gstin(PUBLISHED_EXAMPLE[:14] + wrong)


@pytest.mark.parametrize(
    "bad",
    [
        "27AAPFU0939F1XV",  # 14th char must be Z
        "2AAAPFU0939F1ZV",  # state code must be 2 digits
        "27AAPF10939F1ZV",  # PAN letters/digits in the wrong place
        "27AAPFU0939F0ZV",  # entity number cannot be 0
        "27aapfu0939f1zv",  # lower case
        "27AAPFU0939F1Z",  # too short
    ],
)
def test_bad_format(bad):
    assert not is_valid_format(bad)
    assert not is_valid_gstin(bad)


def test_make_gstin_round_trips():
    g = make_gstin("29", "AAFCK8812L")
    assert is_valid_gstin(g)
    assert state_code(g) == "29"
