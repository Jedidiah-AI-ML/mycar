import pytest

from app.services.vin import check_digit_ok, normalise_vin, vin_warnings


def test_valid_check_digit():
    # Standard worked example (check digit 'X').
    assert check_digit_ok("1M8GDM9AXKP042788")
    assert vin_warnings("1M8GDM9AXKP042788") == []


def test_bad_check_digit_is_only_a_warning():
    vin = normalise_vin("1M8GDM9A1KP042788")  # accepted...
    assert vin_warnings(vin)  # ...but flagged


def test_normalises_case_spaces_dashes():
    assert normalise_vin(" 1m8gdm9a-xkp 042788 ") == "1M8GDM9AXKP042788"


@pytest.mark.parametrize("bad", ["", "SHORT", "1M8GDM9AXKP0427880", "1M8GDM9AXKPO42788"])
def test_rejects_bad_vins(bad):
    with pytest.raises(ValueError):
        normalise_vin(bad)
