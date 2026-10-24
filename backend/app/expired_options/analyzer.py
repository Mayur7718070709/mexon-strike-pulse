from decimal import Decimal, InvalidOperation


class ExpiredOptionsAnalyzer:
    """Compute premium multiples from returned candles only."""

    @staticmethod
    def premium_multiple(observations: list[dict]) -> dict:
        ordered = sorted(observations, key=lambda row: row["provider_timestamp"])
        usable = [row for row in ordered if row.get("open_price") is not None and row.get("high_price") is not None]
        if not usable:
            return {"status": "NO OBSERVATIONS", "observations": len(observations),
                    "entry_premium": None, "maximum_premium": None, "maximum_multiple": None,
                    "method": "first observed candle open for one source date as entry; maximum same-date candle high divided by entry"}
        first = usable[0]
        try:
            entry = Decimal(str(first["open_price"]))
            highs = [Decimal(str(row["high_price"])) for row in usable]
        except (InvalidOperation, TypeError, ValueError):
            return {"status": "UNAVAILABLE", "reason": "Provider observations contain invalid prices."}
        if entry <= 0:
            return {"status": "UNAVAILABLE", "reason": "First observed open is not positive; no multiple is computed."}
        maximum = max(highs)
        peak_row = max(usable, key=lambda row: Decimal(str(row["high_price"])))
        return {"status": "AVAILABLE", "observations": len(usable), "entry_premium": str(entry),
                "entry_timestamp": first["provider_timestamp"], "maximum_premium": str(maximum),
                "maximum_timestamp": peak_row["provider_timestamp"],
                "maximum_multiple": str(maximum / entry),
                "method": "first stored provider candle open for one source date as entry; maximum same-date provider candle high divided by entry"}

    @classmethod
    def analyze_groups(cls, observations: list[dict]) -> list[dict]:
        groups: dict[tuple, list[dict]] = {}
        for row in observations:
            key = (row["strike_selection"], row["option_type"], row["strike"], row["provider_timestamp"].date())
            groups.setdefault(key, []).append(row)
        results = []
        for (selection, option_type, strike, source_date), rows in groups.items():
            results.append({"strike_selection": selection, "option_type": option_type.upper(),
                            "actual_strike": str(strike), "source_date": source_date,
                            **cls.premium_multiple(rows)})
        return results
