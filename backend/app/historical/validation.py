import math
from datetime import datetime, timezone


class HistoricalValidationService:
    """Validate Dhan's parallel candle arrays and retain provider epoch timestamps."""

    FIELDS = ("open", "high", "low", "close", "volume", "timestamp")

    def normalize(self, data: dict, *, open_interest_supported: bool = True) -> dict:
        if not isinstance(data, dict):
            raise ValueError("Dhan historical response is not an object")
        if any(not isinstance(data.get(key), list) for key in self.FIELDS):
            raise ValueError("Dhan historical response is missing a documented candle series")
        lengths = {len(data[key]) for key in self.FIELDS}
        if open_interest_supported and data.get("open_interest") is not None:
            if not isinstance(data["open_interest"], list):
                raise ValueError("Dhan open_interest series is malformed")
            lengths.add(len(data["open_interest"]))
        if len(lengths) != 1:
            raise ValueError("Dhan historical series are not aligned")

        observations, invalid, duplicates = [], 0, 0
        seen = set()
        for index, epoch in enumerate(data["timestamp"]):
            try:
                if isinstance(epoch, bool) or not isinstance(epoch, int):
                    raise ValueError("timestamp is not an epoch integer")
                timestamp = datetime.fromtimestamp(epoch, tz=timezone.utc)
                prices = {name: float(data[name][index]) for name in ("open", "high", "low", "close")}
                if any(not math.isfinite(value) or value <= 0 for value in prices.values()):
                    raise ValueError("price must be finite and positive")
                if prices["high"] < max(prices["open"], prices["close"], prices["low"]):
                    raise ValueError("high is below another OHLC value")
                if prices["low"] > min(prices["open"], prices["close"], prices["high"]):
                    raise ValueError("low is above another OHLC value")
                volume = data["volume"][index]
                oi = data.get("open_interest", [None] * len(data["timestamp"]))[index]
                if volume is not None and (isinstance(volume, bool) or not isinstance(volume, int) or volume < 0):
                    raise ValueError("volume must be a non-negative integer")
                if oi is not None and (isinstance(oi, bool) or not isinstance(oi, int) or oi < 0):
                    raise ValueError("open_interest must be a non-negative integer")
                if timestamp in seen:
                    duplicates += 1
                    continue
                seen.add(timestamp)
                observations.append({"timestamp": timestamp, "provider_timestamp": timestamp,
                                     "ingested_at": datetime.now(timezone.utc), **prices,
                                     "volume": volume, "open_interest": oi})
            except (TypeError, ValueError, OverflowError, OSError):
                invalid += 1
        observations.sort(key=lambda row: row["timestamp"])
        return {"candles": observations, "received": len(data["timestamp"]),
                "invalid": invalid, "duplicates": duplicates}
