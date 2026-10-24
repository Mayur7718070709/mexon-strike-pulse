import csv
import io
import math
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EXCHANGE_SEGMENTS = {"IDX_I", "NSE_EQ", "NSE_FNO", "NSE_CURRENCY", "BSE_EQ", "MCX_COMM", "BSE_CURRENCY", "BSE_FNO"}
INSTRUMENT_TYPES = {"INDEX", "FUTIDX", "OPTIDX", "EQUITY", "FUTSTK", "OPTSTK", "FUTCOM", "OPTFUT", "FUTCUR", "OPTCUR"}
INTERVALS = {"1", "5", "15", "25", "60"}
EXPIRED_FIELDS = {"open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"}


class QuoteRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    instruments: dict[str, list[int]]

    @field_validator("instruments")
    @classmethod
    def valid_instruments(cls, value):
        if not value or sum(map(len, value.values())) > 1000:
            raise ValueError("Dhan quotes require 1 to 1000 instruments")
        for segment, ids in value.items():
            if segment not in EXCHANGE_SEGMENTS or not ids or any(not isinstance(item, int) or item <= 0 for item in ids):
                raise ValueError("Use an official Dhan segment and positive security IDs from the Dhan instrument master")
        return value


class UnderlyingRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    underlying_scrip: int = Field(gt=0)
    underlying_segment: str

    @field_validator("underlying_segment")
    @classmethod
    def valid_segment(cls, value):
        if value not in EXCHANGE_SEGMENTS:
            raise ValueError("Underlying segment must be an official Dhan exchange segment")
        return value


class CandleResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    open: list[float]
    high: list[float]
    low: list[float]
    close: list[float]
    volume: list[int]
    timestamp: list[int]
    open_interest: list[int] | None = None

    @model_validator(mode="after")
    def aligned_series(self):
        lengths = {len(self.open), len(self.high), len(self.low), len(self.close), len(self.volume), len(self.timestamp)}
        if self.open_interest is not None:
            lengths.add(len(self.open_interest))
        if len(lengths) != 1 or any(not math.isfinite(x) for series in (self.open, self.high, self.low, self.close) for x in series):
            raise ValueError("Dhan candle series are malformed or misaligned")
        return self


def validate_csv_master(text: str, detailed: bool = False) -> tuple[list[str], int]:
    # Dhan's downloadable UTF-8 CSV may include a BOM and padded header cells.
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    header: list[str] = []
    for candidate in reader:
        candidate = [column.strip().lstrip("\ufeff") for column in candidate]
        if candidate and any(candidate):
            header = candidate
            break
    if not header:
        raise ValueError("Dhan instrument master CSV is empty")
    empty_columns = [index for index, column in enumerate(header) if not column]
    if empty_columns:
        raise ValueError(f"Dhan instrument master CSV has unnamed header columns at positions {empty_columns[:12]}")
    required = (
        {"EXCH_ID", "SEGMENT", "INSTRUMENT", "SYMBOL_NAME", "DISPLAY_NAME", "INSTRUMENT_TYPE", "SM_EXPIRY_DATE", "STRIKE_PRICE", "OPTION_TYPE"}
        if detailed
        else {"SEM_EXM_EXCH_ID", "SEM_SEGMENT", "SEM_INSTRUMENT_NAME", "SM_SYMBOL_NAME", "SEM_CUSTOM_SYMBOL", "SEM_EXCH_INSTRUMENT_TYPE", "SEM_EXPIRY_DATE", "SEM_STRIKE_PRICE", "SEM_OPTION_TYPE"}
    )
    if not required.issubset(set(header)):
        raise ValueError("Dhan instrument master CSV header differs from its documented format")
    row_count = 0
    for row in reader:
        if not row:
            continue
        if len(row) != len(header):
            raise ValueError("Dhan instrument master row does not match its header")
        row_count += 1
    return header, row_count


def iso_date(value: date) -> str:
    return value.isoformat()
