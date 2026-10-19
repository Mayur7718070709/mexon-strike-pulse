import struct

from app.market_data import decode_ticker_packet


def test_decodes_documented_ticker_packet():
    packet = struct.pack("<BHBIfi", 2, 16, 1, 1333, 25123.5, 1_760_000_000)
    decoded = decode_ticker_packet(packet)
    assert decoded is not None
    assert decoded["security_id"] == "1333"
    assert decoded["exchange_segment"] == "NSE_EQ"
    assert decoded["last_price"] == 25123.5
    assert decoded["exchange_timestamp"].endswith("+00:00")


def test_rejects_non_ticker_and_malformed_packets():
    assert decode_ticker_packet(b"") is None
    assert decode_ticker_packet(struct.pack("<BHBIfi", 6, 16, 1, 1333, 100.0, 1_760_000_000)) is None
    assert decode_ticker_packet(struct.pack("<BHBIfi", 2, 15, 1, 1333, 100.0, 1_760_000_000)) is None
    assert decode_ticker_packet(struct.pack("<BHBIfi", 2, 16, 99, 1333, 100.0, 1_760_000_000)) is None
