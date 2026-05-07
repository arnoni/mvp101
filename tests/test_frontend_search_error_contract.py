from pathlib import Path

APP_JS = Path("static/app.js").read_text(encoding="utf-8")


def test_frontend_search_errors_use_product_copy_and_observability_events():
    assert "Request failed with HTTP" not in APP_JS
    assert "productSearchErrorMessage" in APP_JS
    assert "You've used today's free checks. Try again tomorrow or join research access." in APP_JS
    assert "Too many requests from your location. Please wait and try again." in APP_JS
    assert "Verification failed. Please try again." in APP_JS
    assert "Service temporarily busy. Please try again in a moment." in APP_JS
    assert "search_unstructured_backend_error" in APP_JS
    assert "search_quota_exceeded" in APP_JS
    assert "search_rate_limited" in APP_JS
    assert "search_throttled" in APP_JS


def test_frontend_search_has_single_in_flight_guard_and_token_payload_block():
    assert "let searchRequestInFlight = false;" in APP_JS
    assert "if (searchRequestInFlight || state.hero.searchRequestInFlight)" in APP_JS
    assert "search_payload_missing_token" in APP_JS
    assert "turnstile_verified" in APP_JS
    assert "const payload = searchPayload(\"construction\");" in APP_JS
