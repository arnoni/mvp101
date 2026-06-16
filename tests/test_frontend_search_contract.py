from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "public" / "static" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
APP_CSS = (ROOT / "public" / "static" / "app.css").read_text(encoding="utf-8")


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


def test_turnstile_renders_only_after_visible_and_recovers_from_widget_errors():
    assert 'const TURNSTILE_VISIBLE_WAIT_MS = 2500;' in APP_JS
    assert 'function isVisibleForRender(el)' in APP_JS
    assert 'await waitForVisible(el);' in APP_JS
    assert 'turnstile_container_not_visible' in APP_JS
    assert 'TURNSTILE_MAX_ERROR_RETRIES = 2' in APP_JS
    assert 'const willRetry = errorRetryCount < TURNSTILE_MAX_ERROR_RETRIES;' in APP_JS
    assert 'opts.onError?.(code, { willRetry, retryCount: nextRetryCount' in APP_JS
    assert 'if (!meta.willRetry) window.Sentry?.captureMessage?.("Turnstile widget error"' in APP_JS
    assert 'errorRetryTimer = window.setTimeout(() => { destroy(); init(); }' in APP_JS
    assert 'if (!meta.willRetry)' in APP_JS
    assert 'window.Sentry?.captureMessage?.("Turnstile widget error"' in APP_JS
    assert 'function onTurnstileError(code, meta = {})' in APP_JS
    assert 'captureException?.(new Error("Turnstile error")' not in APP_JS
    assert '#turnstileContainer, #unlock-turnstile-widget, #report-turnstile-widget { min-height: 65px;' in APP_CSS


def test_sentry_dsn_is_exposed_and_placeholder_sri_removed():
    assert 'data-sentry-dsn="{{ sentry_frontend_dsn|default(\'\') }}"' in INDEX_HTML
    assert 'sha384-generic-placeholder' not in INDEX_HTML
    assert 'frontend_sentry_missing' in APP_JS


def test_frontend_global_errors_and_flow_telemetry_failures_are_reported():
    assert 'function installGlobalErrorReporting()' in APP_JS
    assert 'frontend_unhandled_error' in APP_JS
    assert 'frontend_unhandled_rejection' in APP_JS
    assert 'installGlobalErrorReporting();' in APP_JS
    assert 'client_flow_event_failed' in APP_JS


def test_template_does_not_use_inline_click_handlers():
    assert "onclick=" not in INDEX_HTML


def test_search_buttons_are_tier_aware_and_emit_state_telemetry():
    assert 'const turnstileRequiredForSearch = tier === "free";' in APP_JS
    assert 'const canSearch = hasCoords && (!turnstileRequiredForSearch || hasTurnstileToken) && !busy;' in APP_JS
    assert 'if (mainBtn) mainBtn.disabled = !canSearch;' in APP_JS
    assert 'if (conBtn) conBtn.disabled = !canSearch;' in APP_JS
    assert 'if (demandBtn) demandBtn.disabled = !canSearch;' in APP_JS
    assert 'captureEvent("search_button_state_changed"' in APP_JS
    assert 'turnstile_required_for_search: turnstileRequiredForSearch' in APP_JS


def test_paid_search_dispatch_does_not_require_turnstile_token():
    assert '''function isHeroTurnstileRequired() {
    return AccessState.get().tier === "free"; }''' in APP_JS
    assert 'setSearchState(turnstileToken ? "turnstile_verified" : "ready_without_turnstile");' in APP_JS
    assert 'if (turnstileRequiredForSearch && !payload.turnstile_token)' in APP_JS
    assert 'setSearchState(token ? "turnstile_verified" : "ready_without_turnstile");' in APP_JS
    assert 'if (turnstileRequiredForSearch && !demandPayload.turnstile_token)' in APP_JS


def test_quiet_place_celebration_is_scoped_and_privacy_safe():
    assert 'id="constructionGauge" data-celebration="idle"' in INDEX_HTML
    assert 'class="gauge-band gauge-arc" id="constructionBand"' in INDEX_HTML
    assert 'class="needle gauge-needle" id="constructionNeedle"' in INDEX_HTML
    assert 'function runQuietCelebration(finalAngleDeg, reportId)' in APP_JS
    assert 'needle.animate([' in APP_JS
    assert 'QUIET_CELEBRATION_DURATION = 3400' in APP_JS
    assert 'construction_score_bucket: "under_10"' in APP_JS
    assert 'construction_score:' not in APP_JS[APP_JS.index('quiet_place_celebration_shown'):APP_JS.index('function markQuietCelebrationShown')]
    assert 'cancelQuietCelebration();' in APP_JS
    assert 'quiet-arc-pulse 3.4s ease-in-out forwards' in APP_CSS
    assert '@media (prefers-reduced-motion: reduce)' in APP_CSS
