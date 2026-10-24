from math import log10


WEIGHTS = {
    "direction_momentum": 25,
    "premium_momentum": 20,
    "volume": 15,
    "oi_and_change_in_oi": 15,
    "iv": 10,
    "distance_from_spot": 10,
    "historical_10x": 5,
}


class OpportunityScoringService:
    """Deterministic 0–100 normalizers; missing evidence is omitted, not zeroed."""

    @staticmethod
    def clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
        return max(lower, min(upper, value))

    @classmethod
    def direction_momentum(cls, underlying_return_pct: float | None, option_type: str) -> float | None:
        if underlying_return_pct is None:
            return None
        alignment = underlying_return_pct if option_type == "CALL" else -underlying_return_pct
        # A side-aligned 1% five-minute underlying move maps to 100; -1% maps to 0.
        return cls.clamp(50.0 + alignment * 50.0)

    @classmethod
    def premium_momentum(cls, premium_change_pct: float | None) -> float | None:
        if premium_change_pct is None:
            return None
        # Provider previous-close change: +/-20 percentage points map to 0/100.
        return cls.clamp(50.0 + premium_change_pct * 2.5)

    @staticmethod
    def volume(volume: int | None) -> float | None:
        if volume is None or volume < 0:
            return None
        # Log scale prevents large index contracts from overwhelming other factors.
        return min(100.0, 100.0 * log10(1 + volume) / log10(10_001))

    @classmethod
    def oi_and_change(cls, oi: int | None, change_pct: float | None) -> tuple[float | None, dict]:
        parts = {}
        if oi is not None and oi >= 0:
            parts["open_interest"] = min(100.0, 100.0 * log10(1 + oi) / log10(1_000_001))
        if change_pct is not None:
            # +/-50% day-over-day OI change maps to 0/100.
            parts["change_in_oi"] = cls.clamp(50.0 + change_pct)
        return (sum(parts.values()) / len(parts) if parts else None), parts

    @classmethod
    def iv(cls, iv_value: float | None) -> float | None:
        if iv_value is None or iv_value < 0:
            return None
        # Dhan's raw IV value is used as published; 0 to 100 is the normalization range.
        return cls.clamp(100.0 - iv_value)

    @classmethod
    def distance(cls, distance_pct: float | None) -> float | None:
        if distance_pct is None or distance_pct < 0:
            return None
        # At-the-money scores 100; a 10% strike/spot distance or greater scores 0.
        return cls.clamp(100.0 - distance_pct * 10.0)

    @classmethod
    def historical_10x(cls, maximum_multiple: float | None) -> float | None:
        if maximum_multiple is None or maximum_multiple <= 0:
            return None
        return cls.clamp(maximum_multiple / 10.0 * 100.0)

    @classmethod
    def total(cls, components: dict[str, float | None]) -> dict:
        available_weight = sum(WEIGHTS[name] for name, score in components.items() if score is not None)
        if available_weight == 0:
            return {"score": None, "available_weight_pct": 0.0, "missing_components": list(WEIGHTS)}
        weighted = sum(float(score) * WEIGHTS[name] for name, score in components.items() if score is not None)
        missing = [name for name in WEIGHTS if components.get(name) is None]
        return {"score": weighted / available_weight,
                "available_weight_pct": float(available_weight), "missing_components": missing}

    @staticmethod
    def weights() -> dict[str, int]:
        return dict(WEIGHTS)
