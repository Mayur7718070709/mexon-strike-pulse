from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Underlying = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class WatchlistCreateRequest(RequestModel):
    name: Name
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None
    is_default: bool = False


class WatchlistItemCreateRequest(RequestModel):
    instrument_id: UUID | None = None
    option_contract_id: UUID | None = None
    underlying: Underlying | None = None
    expiry: date | None = None
    strike: Decimal | None = Field(default=None, gt=0, max_digits=24, decimal_places=8)
    option_type: Literal["CALL", "PUT"] | None = None
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=250)] | None = None

    @model_validator(mode="after")
    def validate_subject(self):
        has_identity = any(value is not None for value in (self.underlying, self.expiry, self.strike, self.option_type))
        complete_identity = all(value is not None for value in (self.underlying, self.expiry, self.strike, self.option_type))
        groups = int(self.instrument_id is not None) + int(self.option_contract_id is not None) + int(has_identity)
        if groups != 1 or (has_identity and not complete_identity):
            raise ValueError("Provide exactly one complete Dhan instrument, option contract, or underlying/expiry/strike/option_type identity")
        return self


class TargetStartRequest(RequestModel):
    request_id: UUID
    underlying: Underlying
    expiry: date
    strike: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    option_type: Literal["CALL", "PUT"]
    confirmed_reference_premium: Decimal | None = Field(default=None, gt=0, max_digits=24, decimal_places=8)
    confirmed_reference_timestamp: datetime | None = None

    @model_validator(mode="after")
    def validate_confirmation(self):
        if (self.confirmed_reference_premium is None) != (self.confirmed_reference_timestamp is None):
            raise ValueError("Confirm both the displayed premium and its source timestamp, or neither")
        if self.confirmed_reference_timestamp is not None and self.confirmed_reference_timestamp.tzinfo is None:
            raise ValueError("confirmed_reference_timestamp must include a timezone")
        return self
