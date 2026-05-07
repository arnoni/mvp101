from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "static" / "app.js").read_text()
INDEX_HTML = (ROOT / "templates" / "index.html").read_text()


def test_main_search_button_is_explicit_button_not_native_submit():
    assert 'id="mainActionBtn" type="button"' in INDEX_HTML


def test_search_submit_telemetry_and_request_lifecycle_are_instrumented():
    for event_name in [
        "search_submit_clicked",
        "search_submit_blocked",
        "search_request_started",
        "search_request_succeeded",
        "search_request_failed",
        "search_ui_render_blocked_no_backend_response",
        "search_validation_passed",
    ]:
        assert event_name in APP_JS
    assert 'const payload = searchPayload("construction");' in APP_JS
    assert 'apiPost("/api/search", payload)' in APP_JS


def test_render_result_requires_request_success_state_first():
    success_idx = APP_JS.index('setSearchState("request_success")')
    render_idx = APP_JS.index('setSearchState("render_result")')
    message_idx = APP_JS.index('setText("constructionMessage", result.message')
    assert success_idx < render_idx < message_idx


def test_turnstile_token_logging_is_safe_length_only():
    assert 'event: "turnstile_token_received"' in APP_JS
    assert 'token_length: token?.length || 0' in APP_JS
    assert 'console.info(token)' not in APP_JS


def test_sentry_dsn_is_exposed_and_placeholder_sri_removed():
    assert 'data-sentry-dsn="{{ sentry_frontend_dsn|default(\'\') }}"' in INDEX_HTML
    assert 'sha384-generic-placeholder' not in INDEX_HTML
    assert 'frontend_sentry_missing' in APP_JS


def test_template_does_not_use_inline_click_handlers():
    assert "onclick=" not in INDEX_HTML
