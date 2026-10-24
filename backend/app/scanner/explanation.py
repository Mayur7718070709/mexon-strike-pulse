from app.scanner.scoring import WEIGHTS


class ScannerExplanationService:
    @staticmethod
    def explain(components: dict, coverage: dict, included: bool, reasons: list[str], exclusions: list[str]) -> dict:
        available = [name for name, score in components.items() if score is not None]
        missing = [name for name in WEIGHTS if components.get(name) is None]
        return {
            "included": included,
            "inclusion_reasons": reasons,
            "exclusion_reasons": exclusions,
            "component_summary": {name: ("Unavailable: no sufficiently fresh provider evidence" if components.get(name) is None
                                           else f"{components[name]:.1f}/100 using provider evidence")
                                  for name in WEIGHTS},
            "score_coverage": {"available_components": available, "missing_components": missing,
                               "available_weight_pct": coverage["available_weight_pct"]},
            "disclaimer": "A scanner score is an observation-ranking heuristic, not a prediction, guarantee, or trade instruction.",
        }
