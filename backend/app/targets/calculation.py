from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OptionType = Literal["CALL", "PUT"]
TARGET_MULTIPLIERS = (2, 3, 5, 7, 10)
MONEY_QUANTUM = Decimal("0.00000001")


class TargetLevel(BaseModel):
    model_config = ConfigDict(frozen=True)

    multiplier: int = Field(description="Premium multiplier, not a strike multiplier")
    target_value: Decimal = Field(description="Reference premium multiplied by multiplier")
    status: Literal["NOT_REACHED", "REACHED", "UNVERIFIED"] = "NOT_REACHED"
    first_reached_at: datetime | None = None


class TargetCalculationService:
    """Calculate premium targets independently for a single CALL or PUT."""

    multipliers = TARGET_MULTIPLIERS

    @staticmethod
    def validate_reference_premium(value: Decimal | int | float | str) -> Decimal:
        if isinstance(value, bool):
            raise ValueError("reference premium must be a positive monetary value")
        try:
            premium = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("reference premium must be a positive monetary value") from exc
        if not premium.is_finite() or premium <= 0:
            raise ValueError("reference premium must be finite and greater than zero")
        if premium.as_tuple().exponent < -8:
            raise ValueError("reference premium supports at most 8 decimal places")
        if premium.adjusted() > 15:
            raise ValueError("reference premium exceeds the supported monetary range")
        return premium

    @classmethod
    def calculate(cls, reference_premium: Decimal | int | float | str,
                  option_type: OptionType) -> list[TargetLevel]:
        if option_type not in {"CALL", "PUT"}:
            raise ValueError("option_type must be CALL or PUT")
        reference = cls.validate_reference_premium(reference_premium)
        with localcontext() as context:
            context.prec = 40
            levels = [TargetLevel(multiplier=multiplier,
                                  target_value=(reference * Decimal(multiplier)).quantize(MONEY_QUANTUM))
                      for multiplier in cls.multipliers]
        if any(level.target_value.adjusted() > 15 for level in levels):
            raise ValueError("calculated target exceeds the supported monetary range")
        return levels

