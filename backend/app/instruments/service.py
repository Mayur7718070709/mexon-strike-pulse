import csv
import io
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.instruments.repository import InstrumentRepository

log = structlog.get_logger(__name__)
SUPPORTED_INDEX_OPTIONS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}
REQUIRED_DETAILED_COLUMNS = {
    "EXCH_ID", "SEGMENT", "SECURITY_ID", "INSTRUMENT", "UNDERLYING_SECURITY_ID",
    "UNDERLYING_SYMBOL", "SYMBOL_NAME", "DISPLAY_NAME", "INSTRUMENT_TYPE", "LOT_SIZE",
    "SM_EXPIRY_DATE", "STRIKE_PRICE", "OPTION_TYPE", "TICK_SIZE",
}


class InstrumentMasterError(ValueError):
    pass


class InstrumentSyncService:
    def __init__(self, provider, session: AsyncSession):
        self.provider = provider
        self.session = session
        self.repository = InstrumentRepository(session)

    async def synchronize(self) -> dict:
        envelope = await self.provider.get_instrument_master(detailed=True)
        data = envelope.get("data", {})
        if data.get("format") != "csv" or not isinstance(data.get("content"), str):
            raise InstrumentMasterError("Dhan did not return the detailed CSV instrument master.")
        # Fully validate the provider payload before deactivating the last known good mapping.
        roots, options, deduplicated_rows = self._parse(data["content"])
        if not roots or not options:
            raise InstrumentMasterError("Dhan instrument master contained no supported NSE option mappings; prior mappings were retained.")

        synchronized_at = datetime.now(timezone.utc)
        try:
            provider_id = await self.repository.ensure_provider()
            await self.repository.deactivate_provider_mappings(provider_id, synchronized_at)
            root_ids: dict[tuple[str, str, str], object] = {}
            for root in roots:
                underlying_id = await self.repository.upsert_underlying(provider_id, root, synchronized_at)
                root_ids[(root["root_symbol"], root["exchange_segment"], root["security_id"])] = underlying_id

            synced_options = 0
            missing_underlying = 0
            expired_count = 0
            for option in options:
                root_key = (option["underlying_symbol"], option["underlying_segment"], option["underlying_security_id"])
                underlying_id = root_ids.get(root_key)
                if underlying_id is None:
                    # Do not submit a derivative contract request or persist an option without an official root mapping.
                    missing_underlying += 1
                    continue
                option["is_expired"] = option["expiry_date"] < synchronized_at.date()
                option["is_active"] = not option["is_expired"]
                option["inactive_at"] = synchronized_at if option["is_expired"] else None
                if option["is_expired"]:
                    expired_count += 1
                await self.repository.upsert_option(provider_id, underlying_id, option["underlying_symbol"],
                                                    option["expiry_date"], option, synchronized_at)
                synced_options += 1
            if not synced_options:
                raise InstrumentMasterError("No NSE option contracts had a matching provider-confirmed underlying; prior mappings were retained.")
            await self.repository.commit()
        except Exception:
            await self.session.rollback()
            raise
        result = {
            "status": "synchronized", "source": "DHANHQ", "synchronized_at": synchronized_at,
            "roots": len(roots), "option_contracts": synced_options, "expired_contracts": expired_count,
            "unresolved_underlying_contracts": missing_underlying,
            "deduplicated_rows": deduplicated_rows,
            "provider_timestamp": envelope.get("provenance", {}).get("provider_timestamp"),
        }
        log.info("dhan_instrument_master_synchronized", **{k: v for k, v in result.items() if k != "provider_timestamp"})
        return result

    @staticmethod
    def _parse(content: str) -> tuple[list[dict], list[dict], int]:
        reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
        if reader.fieldnames:
            reader.fieldnames = [name.strip().lstrip("\ufeff") or f"__UNNAMED_{index}"
                                 for index, name in enumerate(reader.fieldnames)]
        if not reader.fieldnames or not REQUIRED_DETAILED_COLUMNS.issubset(set(reader.fieldnames)):
            raise InstrumentMasterError("Dhan detailed master is missing required documented columns or Security ID.")
        root_candidates: dict[tuple[str, str, str], dict] = {}
        option_rows: dict[tuple[str, str, str], dict] = {}
        index_option_symbols: set[str] = set()
        stock_option_symbols: set[str] = set()
        source_count = 0
        deduplicated_rows = 0

        for row in reader:
            if not row or not any((value or "").strip() for value in row.values()):
                continue
            source_count += 1
            exch, segment = (row.get("EXCH_ID") or "").strip().upper(), (row.get("SEGMENT") or "").strip().upper()
            instrument = (row.get("INSTRUMENT") or "").strip().upper()
            inst_type = (row.get("INSTRUMENT_TYPE") or "").strip().upper()
            symbol = InstrumentSyncService._text(row, "SYMBOL_NAME").upper()
            if exch != "NSE":
                continue
            security_id = InstrumentSyncService._security_id(row.get("SECURITY_ID"))
            if segment == "I" and instrument == "INDEX" and inst_type == "INDEX" and symbol in SUPPORTED_INDEX_OPTIONS:
                key = (symbol, "IDX_I", security_id)
                if key in root_candidates:
                    deduplicated_rows += 1
                else:
                    root_candidates[key] = InstrumentSyncService._root(row, "IDX_I", "index")
            elif segment == "E" and instrument == "EQUITY" and inst_type == "EQUITY":
                key = (symbol, "NSE_EQ", security_id)
                if key in root_candidates:
                    deduplicated_rows += 1
                else:
                    root_candidates[key] = InstrumentSyncService._root(row, "NSE_EQ", "equity")
            elif segment == "D" and instrument in {"OPTIDX", "OPTSTK"} and inst_type == instrument:
                underlying_symbol = InstrumentSyncService._text(row, "UNDERLYING_SYMBOL").upper()
                underlying_security_id = InstrumentSyncService._security_id(row.get("UNDERLYING_SECURITY_ID"))
                underlying_segment = "IDX_I" if instrument == "OPTIDX" else "NSE_EQ"
                expiry = InstrumentSyncService._date(row.get("SM_EXPIRY_DATE"))
                strike = InstrumentSyncService._decimal(row.get("STRIKE_PRICE"), required=True)
                opt_type = (row.get("OPTION_TYPE") or "").strip().upper()
                if not underlying_symbol or opt_type not in {"CE", "PE"} or strike < 0:
                    continue
                if instrument == "OPTIDX":
                    if underlying_symbol not in SUPPORTED_INDEX_OPTIONS:
                        continue
                    index_option_symbols.add(underlying_symbol)
                else:
                    stock_option_symbols.add(underlying_symbol)
                item = {
                    "exchange_segment": "NSE_FNO", "security_id": security_id,
                    "symbol": symbol or InstrumentSyncService._text(row, "DISPLAY_NAME"),
                    "display_name": InstrumentSyncService._text(row, "DISPLAY_NAME") or symbol,
                    "instrument_type": instrument, "underlying_symbol": underlying_symbol,
                    "underlying_security_id": underlying_security_id,
                    "underlying_segment": underlying_segment, "expiry_date": expiry,
                    "strike_price": strike, "option_type": "call" if opt_type == "CE" else "put",
                    "lot_size": InstrumentSyncService._integer(row.get("LOT_SIZE")),
                    "tick_size": InstrumentSyncService._decimal(row.get("TICK_SIZE")),
                    "expiry_kind": {"W": "weekly", "M": "monthly"}.get((row.get("EXPIRY_FLAG") or "").strip().upper(), "regular"),
                    "is_active": True, "is_expired": False, "inactive_at": None,
                    "metadata": json.dumps({"exchange": exch, "master_segment": segment,
                                            "expiry_flag": (row.get("EXPIRY_FLAG") or "").strip().upper() or None}),
                }
                key = ("NSE_FNO", security_id, instrument)
                if key in option_rows:
                    deduplicated_rows += 1
                else:
                    option_rows[key] = item

        # Only keep underlyings that are supported and evidenced by both instrument rows.
        eligible_roots = []
        for root in root_candidates.values():
            if root["instrument_type"] == "INDEX" and root["root_symbol"] in index_option_symbols:
                eligible_roots.append(root)
            elif root["instrument_type"] == "EQUITY" and root["root_symbol"] in stock_option_symbols:
                eligible_roots.append(root)
        allowed_keys = {(r["root_symbol"], r["exchange_segment"], r["security_id"]) for r in eligible_roots}
        valid_options = [item for item in option_rows.values()
                         if (item["underlying_symbol"], item["underlying_segment"], item["underlying_security_id"]) in allowed_keys]
        if source_count == 0:
            raise InstrumentMasterError("Dhan detailed instrument master is empty.")
        return eligible_roots, valid_options, deduplicated_rows

    @staticmethod
    def _text(row: dict, key: str) -> str:
        return (row.get(key) or "").strip()

    @staticmethod
    def _security_id(value: str | None) -> str:
        raw = (value or "").strip()
        if not re.fullmatch(r"[0-9]+", raw) or int(raw) <= 0:
            raise InstrumentMasterError("Dhan master row has a missing or invalid security identifier.")
        return str(int(raw))

    @staticmethod
    def _decimal(value: str | None, *, required: bool = False):
        raw = (value or "").strip()
        if not raw:
            if required:
                raise InstrumentMasterError("Dhan option row has no documented strike price.")
            return None
        try:
            number = Decimal(raw)
        except InvalidOperation as exc:
            raise InstrumentMasterError("Dhan master contains an invalid decimal field.") from exc
        if not number.is_finite():
            raise InstrumentMasterError("Dhan master contains a non-finite decimal field.")
        return number

    @staticmethod
    def _integer(value: str | None):
        raw = (value or "").strip()
        if not raw:
            return None
        if not raw.isdigit() or int(raw) <= 0:
            raise InstrumentMasterError("Dhan master contains an invalid lot size.")
        return int(raw)

    @staticmethod
    def _date(value: str | None) -> date:
        raw = (value or "").strip()
        if not raw:
            raise InstrumentMasterError("Dhan option row has no documented expiry date.")
        # Dhan detailed master uses date strings; accept only ISO dates, never infer a format.
        try:
            return date.fromisoformat(raw[:10])
        except ValueError as exc:
            raise InstrumentMasterError("Dhan master contains an invalid expiry date.") from exc

    @classmethod
    def _root(cls, row: dict, segment: str, asset_type: str) -> dict:
        symbol = cls._text(row, "SYMBOL_NAME").upper()
        return {
            "exchange_segment": segment, "security_id": cls._security_id(row.get("SECURITY_ID")),
            "symbol": symbol, "display_name": cls._text(row, "DISPLAY_NAME") or symbol,
            "asset_type": asset_type, "instrument_type": "INDEX" if asset_type == "index" else "EQUITY",
            "underlying_symbol": symbol, "root_symbol": symbol,
            "tick_size": cls._decimal(row.get("TICK_SIZE")), "lot_size": cls._integer(row.get("LOT_SIZE")),
            "metadata": json.dumps({"exchange": "NSE", "master_segment": row.get("SEGMENT")}),
        }


class ExpiryService:
    def __init__(self, provider, repository: InstrumentRepository):
        self.provider = provider
        self.repository = repository
        self.contract_resolver = ContractResolver(repository)

    async def list_expiries(self, underlying: str) -> dict:
        resolved = await self.contract_resolver.resolve_underlying(underlying)
        if resolved is None:
            return {"status": "UNAVAILABLE", "available": False, "underlying": underlying.upper(),
                    "source": "DHANHQ", "expiries": [], "message": "No active Dhan NSE underlying mapping is available."}
        # These identifiers are resolved from the latest official master sync, never from caller input.
        envelope = await self.provider.get_expiries(int(resolved["security_id"]), resolved["exchange_segment"])
        raw_dates = envelope.get("data")
        if not isinstance(raw_dates, list):
            raise InstrumentMasterError("Dhan expiry response does not contain a date list.")
        dates = [date.fromisoformat(value) for value in raw_dates]
        await self.repository.record_expiries(resolved["underlying_id"], dates)
        await self.repository.commit()
        return {"status": "AVAILABLE" if dates else "UNAVAILABLE", "available": bool(dates),
                "underlying": resolved["root_symbol"], "source": "DHANHQ", "exchange_segment": resolved["exchange_segment"],
                "expiries": dates, "provenance": envelope.get("provenance")}


class ContractResolver:
    """Resolves only current provider-confirmed contract and underlying mappings."""

    def __init__(self, repository: InstrumentRepository):
        self.repository = repository

    async def resolve_underlying(self, symbol: str):
        return await self.repository.resolve_underlying(symbol.strip().upper())

    async def resolve_option(self, underlying: str, expiry: date, strike: Decimal, option_type: str):
        normalized_type = option_type.strip().lower()
        try:
            normalized_strike = Decimal(str(strike))
        except InvalidOperation as exc:
            raise InstrumentMasterError("Option contract parameters are invalid.") from exc
        if normalized_type not in {"call", "put"} or not normalized_strike.is_finite() or normalized_strike < 0:
            raise InstrumentMasterError("Option contract parameters are invalid.")
        # A miss is an unavailable mapping, never permission to guess a security ID.
        return await self.repository.resolve_option(underlying.strip().upper(), expiry, normalized_strike, normalized_type)
