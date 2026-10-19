from app.main import app


def test_openapi_is_versioned_and_market_routes_are_secured():
    schema = app.openapi()
    assert "/api/v1/health" in schema["paths"]
    market = schema["paths"]["/api/v1/market/status"]["get"]
    assert market["security"]
