from decimal import Decimal

import pytest

from app.targets.calculation import TargetCalculationService


def test_call_targets_multiply_reference_premium_only():
    levels = TargetCalculationService.calculate(Decimal("120"), "CALL")

    assert [(level.multiplier, level.target_value) for level in levels] == [
        (2, Decimal("240.00000000")),
        (3, Decimal("360.00000000")),
        (5, Decimal("600.00000000")),
        (7, Decimal("840.00000000")),
        (10, Decimal("1200.00000000")),
    ]


def test_put_targets_are_calculated_independently():
    call_levels = TargetCalculationService.calculate("120", "CALL")
    put_levels = TargetCalculationService.calculate("95", "PUT")

    assert call_levels[-1].target_value == Decimal("1200.00000000")
    assert put_levels[-1].target_value == Decimal("950.00000000")


@pytest.mark.parametrize("value", [0, -1, "NaN", "Infinity", True, "not-a-price"])
def test_reference_premium_must_be_positive_and_finite(value):
    with pytest.raises(ValueError):
        TargetCalculationService.calculate(value, "CALL")


def test_reference_premium_precision_is_bounded_to_storage_precision():
    with pytest.raises(ValueError, match="8 decimal places"):
        TargetCalculationService.calculate("1.000000001", "PUT")


def test_option_type_must_be_call_or_put():
    with pytest.raises(ValueError, match="CALL or PUT"):
        TargetCalculationService.calculate("95", "FUTURE")
