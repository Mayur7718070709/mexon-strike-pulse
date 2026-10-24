from typing import Any


class DhanError(Exception):
    """Provider error with a safe message; never stores/logs tokens or raw bodies."""


class DhanAuthenticationError(DhanError):
    pass


class DhanRateLimitError(DhanError):
    pass


class DhanRequestError(DhanError):
    pass


class DhanInvalidResponse(DhanError):
    pass


class DhanUnavailableError(DhanError):
    pass


class DhanErrorMapper:
    AUTH_CODES = {"DH-901", "807", "808", "809", "810"}
    RATE_CODES = {"DH-904", "805"}
    ACCESS_CODES = {"DH-902", "806"}
    INPUT_CODES = {"DH-905", "804", "811", "812", "813", "814", "DH-907"}
    SERVER_CODES = {"800", "DH-908", "DH-909"}

    @classmethod
    def code_from_payload(cls, payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        value = payload.get("errorCode", payload.get("error_code", payload.get("code")))
        if value is None and isinstance(payload.get("error"), dict):
            error = payload["error"]
            value = error.get("errorCode", error.get("error_code", error.get("code")))
        if value is None and isinstance(payload.get("remarks"), dict):
            remarks = payload["remarks"]
            value = remarks.get("errorCode", remarks.get("error_code", remarks.get("code")))
        return str(value) if value is not None else None

    @classmethod
    def map_response(cls, status_code: int, payload: Any = None) -> DhanError | None:
        code = cls.code_from_payload(payload)
        if status_code in (401, 403) or code in cls.AUTH_CODES:
            return DhanAuthenticationError("Dhan rejected the configured credentials.")
        if status_code == 429 or code in cls.RATE_CODES:
            return DhanRateLimitError("Dhan rate limit was reached.")
        if code in cls.ACCESS_CODES:
            return DhanAuthenticationError("Dhan data API access is not enabled for this account.")
        if code in cls.SERVER_CODES:
            return DhanUnavailableError("Dhan reported an internal or network data error.")
        if 400 <= status_code < 500 or code in cls.INPUT_CODES:
            return DhanRequestError("Dhan rejected the request; verify its documented parameters.")
        if isinstance(payload, dict) and str(payload.get("status", "")).lower() in {"failure", "failed", "error"}:
            return DhanRequestError("Dhan rejected the request.")
        if isinstance(payload, dict) and payload.get("errorType") and not payload.get("status"):
            return DhanRequestError("Dhan returned a provider error response.")
        if status_code >= 500:
            return DhanUnavailableError("Dhan returned a server error.")
        return None
