"""Opt-in tests against DhanHQ. No fake provider responses are used in this module."""

import asyncio
import os
from datetime import date

import pytest

from app.config import Settings
from app.dhan.client import DhanRestClient
from app.dhan.config import DhanConfig
from app.dhan.provider import DhanProvider

pytestmark = pytest.mark.skipif(os.getenv("RUN_DHAN_INTEGRATION") != "1", reason="Set RUN_DHAN_INTEGRATION=1 to call the live DhanHQ API")


def _provider() -> DhanProvider:
    config = DhanConfig.from_settings(Settings())
    if not config.is_configured:
        pytest.skip("Valid DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are required")
    return DhanProvider(config, DhanRestClient(config))


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        pytest.skip(f"Set {name} using an actual value from the official Dhan instrument master/API")
    return value


def test_live_market_quote_from_dhan():
    async def run():
        provider = _provider()
        try:
            result = await provider.get_market_quotes({_required("DHAN_TEST_EXCHANGE_SEGMENT"): [int(_required("DHAN_TEST_SECURITY_ID"))]}, "ltp")
            assert result["provenance"]["provider"] == "DHANHQ"
            assert result["data"]
        finally:
            await provider.rest.close()
    asyncio.run(run())


def test_live_expiry_information_from_dhan():
    async def run():
        provider = _provider()
        try:
            result = await provider.get_expiries(int(_required("DHAN_TEST_UNDERLYING_ID")), _required("DHAN_TEST_UNDERLYING_SEGMENT"))
            assert isinstance(result["data"], list)
        finally:
            await provider.rest.close()
    asyncio.run(run())


def test_live_option_chain_from_dhan():
    async def run():
        provider = _provider()
        try:
            result = await provider.get_option_chain(int(_required("DHAN_TEST_UNDERLYING_ID")), _required("DHAN_TEST_UNDERLYING_SEGMENT"), date.fromisoformat(_required("DHAN_TEST_OPTION_EXPIRY")))
            assert isinstance(result["data"]["oc"], dict)
        finally:
            await provider.rest.close()
    asyncio.run(run())


def test_live_historical_candles_from_dhan():
    async def run():
        provider = _provider()
        try:
            interval = os.getenv("DHAN_TEST_INTERVAL") or None
            result = await provider.get_historical_candles(
                _required("DHAN_TEST_SECURITY_ID"), _required("DHAN_TEST_EXCHANGE_SEGMENT"), _required("DHAN_TEST_INSTRUMENT"),
                date.fromisoformat(_required("DHAN_TEST_FROM_DATE")), date.fromisoformat(_required("DHAN_TEST_TO_DATE")), interval=interval,
            )
            assert isinstance(result["data"]["timestamp"], list)
        finally:
            await provider.rest.close()
    asyncio.run(run())


def test_live_expired_options_from_dhan():
    async def run():
        provider = _provider()
        try:
            result = await provider.get_expired_options(
                exchange_segment=_required("DHAN_TEST_EXCHANGE_SEGMENT"), security_id=_required("DHAN_TEST_UNDERLYING_ID"),
                instrument=_required("DHAN_TEST_OPTION_INSTRUMENT"), interval=os.getenv("DHAN_TEST_INTERVAL", "1"),
                expiry_flag=_required("DHAN_TEST_EXPIRY_FLAG"), expiry_code=int(_required("DHAN_TEST_EXPIRY_CODE")),
                strike=_required("DHAN_TEST_STRIKE"), option_type=os.getenv("DHAN_TEST_OPTION_TYPE", "CALL"),
                required_data=["open", "high", "low", "close", "volume"],
                from_date=date.fromisoformat(_required("DHAN_TEST_FROM_DATE")), to_date=date.fromisoformat(_required("DHAN_TEST_TO_DATE")),
            )
            assert isinstance(result["data"], dict)
        finally:
            await provider.rest.close()
    asyncio.run(run())


def test_live_instrument_master_from_dhan():
    async def run():
        provider = _provider()
        try:
            result = await provider.get_instrument_master(detailed=os.getenv("DHAN_TEST_MASTER_DETAILED") == "1")
            assert result["data"]["format"] == "csv"
            assert result["data"]["row_count"] > 0
        finally:
            await provider.rest.close()
    asyncio.run(run())


def test_live_websocket_feed_from_dhan():
    async def run():
        provider = _provider()
        stream = provider.stream_market_data([(_required("DHAN_TEST_EXCHANGE_SEGMENT"), _required("DHAN_TEST_SECURITY_ID"))])
        try:
            quote = await asyncio.wait_for(anext(stream), timeout=20)
            assert quote["source"] == "DHANHQ"
            assert quote["exchange_timestamp"]
            assert quote["ingested_at"]
        finally:
            await stream.aclose()
            await provider.rest.close()
    asyncio.run(run())
