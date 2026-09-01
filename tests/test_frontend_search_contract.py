from pathlib import Path

import pytest

# Construction outcome production preflight:
# uv run pytest tests/test_frontend_search_contract.py -k construction_outcome -q

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "public" / "static" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
APP_CSS = (ROOT / "public" / "static" / "app.css").read_text(encoding="utf-8")


def test_main_search_button_is_explicit_button_not_native_submit():
    assert 'id="mainActionBtn" type="button"' in INDEX_HTML


@pytest.mark.parametrize(
    ("viewport_width", "usable_width"),
    [
        (320, 199),
        (360, 239),
        (375, 255),
        (390, 269),
        (412, 291),
    ],
)
def test_location_placeholder_mobile_font_size_fits_measured_widths(
    viewport_width, usable_width
):
    rule = """@media (max-width: 430px) {
  #locationInput::placeholder {
    font-size: clamp(11px, calc(5.75vw - 7.2px), 17px);
  }
}"""
    assert rule in APP_CSS
    assert "input::placeholder" not in APP_CSS
    assert ".field input::placeholder" not in APP_CSS
    assert "font-size: 17px;" in APP_CSS[APP_CSS.index("input {"):APP_CSS.index("input:focus")]

    baseline_placeholder_width = 296.0
    baseline_font_size = 17.0
    font_size = min(17.0, max(11.0, viewport_width * 0.0575 - 7.2))
    estimated_width = baseline_placeholder_width * font_size / baseline_font_size

    assert estimated_width <= usable_width - 2


def test_location_placeholder_mobile_rule_does_not_apply_to_tablet_or_desktop():
    assert "@media (max-width: 430px)" in APP_CSS
    assert "@media (min-width: 431px)" not in APP_CSS
    assert "#locationInput::placeholder" in APP_CSS
    assert "768px" not in APP_CSS[APP_CSS.index("#locationInput::placeholder"):]
    assert "1440px" not in APP_CSS[APP_CSS.index("#locationInput::placeholder"):]


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


def test_construction_outcome_effects_are_scoped_sequenced_and_privacy_safe():
    assert (
        'id="constructionGauge" data-outcome="neutral" data-effect="idle"'
        in INDEX_HTML
    )
    assert 'class="gauge-band gauge-arc" id="constructionBand"' in INDEX_HTML
    assert 'class="needle gauge-needle" id="constructionNeedle"' in INDEX_HTML
    for overlay_class in [
        "gauge-outcome-sweep--positive",
        "gauge-outcome-sweep--warning",
        "gauge-outcome-ring--positive",
        "gauge-outcome-ring--warning",
        "gauge-outcome-indicator--positive",
        "gauge-outcome-indicator--warning",
    ]:
        assert overlay_class in INDEX_HTML

    assert "CONSTRUCTION_POSITIVE_MAX = 15" in APP_JS
    assert "CONSTRUCTION_WARNING_MIN = 86" in APP_JS
    assert "CONSTRUCTION_EFFECT_DURATION = 10000" in APP_JS
    assert 'if (value <= CONSTRUCTION_POSITIVE_MAX) return "positive";' in APP_JS
    assert 'if (value >= CONSTRUCTION_WARNING_MIN) return "warning";' in APP_JS
    assert 'return "neutral";' in APP_JS
    assert "shownConstructionEffectReportIds.has(reportId)" in APP_JS
    assert "result.partial || result.cached" in APP_JS
    assert "setConstructionOutcomeState(classifyConstructionOutcome(restoredScore));" in APP_JS

    arrival_call = (
        'animateGauge($("constructionBand"), $("constructionNeedle"), score, '
        "settleOutcomeAfterArrival);"
    )
    assert arrival_call in APP_JS
    assert "const settleOutcomeAfterArrival = () =>" in APP_JS
    assert "maybeRunConstructionResultEffect(result, score, attemptId" in APP_JS

    telemetry_start = APP_JS.index('captureEvent("construction_outcome_effect_shown"')
    telemetry_end = APP_JS.index("function markConstructionEffectShown")
    telemetry = APP_JS[telemetry_start:telemetry_end]
    assert 'construction_score_bucket: outcome === "positive" ? "0_15" : "86_100"' in telemetry
    assert "construction_score:" not in telemetry

    assert '.construction-gauge[data-effect="positive"]' in APP_CSS
    assert '.construction-gauge[data-effect="warning"]' in APP_CSS
    assert "construction-positive-arc-travel 10s" in APP_CSS
    assert "construction-warning-arc-travel 10s" in APP_CSS
    assert '@media (prefers-reduced-motion: reduce)' in APP_CSS


@pytest.mark.parametrize(
    ("score", "expected_outcome"),
    [(0, "positive"), (15, "positive"), (16, "neutral"), (85, "neutral"),
     (86, "warning"), (100, "warning")],
)
def test_construction_outcome_exact_score_bands(score, expected_outcome):
    positive_max = 15
    warning_min = 86
    outcome = "positive" if score <= positive_max else "warning" if score >= warning_min else "neutral"
    assert outcome == expected_outcome
    assert f"CONSTRUCTION_POSITIVE_MAX = {positive_max}" in APP_JS
    assert f"CONSTRUCTION_WARNING_MIN = {warning_min}" in APP_JS


def test_construction_outcome_effect_has_strict_fresh_complete_report_gate():
    gate_start = APP_JS.index("function isPlayableConstructionEffectResult")
    gate_end = APP_JS.index("function captureConstructionEffectShown")
    gate = APP_JS[gate_start:gate_end]
    assert "options.restored" in gate
    assert "!Number.isFinite(score) || score < 0 || score > 100" in gate
    assert "attemptId !== state.hero.searchAttemptId" in gate
    assert "!resultIsCurrent(result)" in gate
    assert 'result.message_code !== "CONSTRUCTION_READY"' in gate
    for excluded_state in [
        "result.success === false",
        "result.error",
        "result.degraded",
        "result.partial",
        "result.cached",
    ]:
        assert excluded_state in gate

    identity_start = APP_JS.index("function getConstructionEffectReportId")
    identity = APP_JS[identity_start:gate_start]
    assert "if (explicitId) return String(explicitId);" in identity
    assert '!attemptId || !coordKey || result?.message_code !== "CONSTRUCTION_READY"' in identity
    assert "if (!reportId) return;" in APP_JS

    result_flow_start = APP_JS.index("async function fetchConstruction")
    result_flow_end = APP_JS.index("async function fetchDemand")
    result_flow = APP_JS[result_flow_start:result_flow_end]
    assert "animateGauge($(\"constructionBand\"), $(\"constructionNeedle\"), score, settleOutcomeAfterArrival)" in result_flow
    assert "{ restored: Number.isFinite(restored) }" in result_flow
    assert "cancelConstructionResultEffect({ attemptId });" in result_flow
    assert result_flow.count("maybeRunConstructionResultEffect") == 1

    demand_flow = APP_JS[result_flow_end:APP_JS.index("// ── 7. Modal system")]
    assert "maybeRunConstructionResultEffect" not in demand_flow


def test_construction_outcome_needle_pattern_is_bounded_and_error_reported():
    pattern_start = APP_JS.index("function constructionNeedlePattern")
    pattern_end = APP_JS.index("function startConstructionNeedlePattern")
    pattern = APP_JS[pattern_start:pattern_end]
    assert "clampScore(finalScore + 3, 0, 15)" in pattern
    assert "clampScore(finalScore - 5, 86, 100)" in pattern
    assert "[0, 0.16, 0.35, 0.53, 0.72, 0.88, 1]" in pattern

    runtime_start = APP_JS.index("function reportConstructionEffectError")
    runtime_end = APP_JS.index("async function apiPost")
    runtime = APP_JS[runtime_start:runtime_end]
    assert 'rotate(${angle} ${PIVOT_X} ${PIVOT_Y})' in runtime
    assert "translate(" not in runtime
    assert "construction_effect_start_failed" in runtime
    assert "construction_effect_frame_failed" in runtime
    assert "construction_effect_cancel_failed" in runtime
    assert "catch (_) {}" not in runtime
    assert "catch (err) {}" not in runtime

    reporter_start = APP_JS.index("function reportConstructionEffectError")
    reporter_end = APP_JS.index("function animateGauge")
    reporter = APP_JS[reporter_start:reporter_end]
    assert "logClientException(eventName, err" in reporter
    assert 'feature: "construction_outcome_effect"' in reporter
    assert "console.error(eventName, err, loggingErr);" in reporter
    assert "score" not in reporter
    assert "coord" not in reporter

    helper_start = APP_JS.index("function logClientException")
    helper_end = APP_JS.index("function installGlobalErrorReporting")
    helper = APP_JS[helper_start:helper_end]
    assert "window.Sentry?.captureException?." in helper
    assert 'window.posthog?.capture?.("client_exception", payload);' in helper

    cleanup_start = APP_JS.index("function cleanupConstructionResultEffect")
    cleanup_end = APP_JS.index("function getConstructionEffectReportId")
    cleanup = APP_JS[cleanup_start:cleanup_end]
    assert 'setConstructionEffectState("idle");' in cleanup

    needle_start = APP_JS.index("function startConstructionNeedlePattern")
    needle_end = APP_JS.index("function runConstructionResultEffect")
    needle_runtime = APP_JS[needle_start:needle_end]
    assert needle_runtime.count("setNeedleAngle(finalAngleDeg);") == 2
    assert "constructionEffectAnimation = null;" in needle_runtime
    assert "cleanupConstructionResultEffect();" in needle_runtime


def test_construction_outcome_effects_cancel_at_lifecycle_boundaries():
    assert "cancelConstructionResultEffect({ clearOutcome: true });" in APP_JS
    assert "cancelConstructionResultEffect({ attemptId });" in APP_JS
    assert 'document.visibilityState === "hidden") cancelConstructionResultEffect();' in APP_JS
    assert 'window.addEventListener("pagehide", cancelConstructionResultEffect);' in APP_JS


def test_construction_outcome_reduced_motion_and_css_are_strictly_scoped():
    reduced_start = APP_CSS.index("@media (prefers-reduced-motion: reduce)")
    reduced = APP_CSS[reduced_start:]
    assert '.construction-gauge[data-effect="positive"]' in reduced
    assert '.construction-gauge[data-effect="warning"]' in reduced
    assert "animation: none !important;" in reduced
    assert ".construction-gauge .gauge-outcome-indicator" in reduced
    assert "transition: none !important;" in reduced
    assert 'if (reduceMotion) { cleanupConstructionResultEffect(); return; }' in APP_JS

    outcome_tokens = (
        ".gauge-outcome",
        ".gauge-positive-particles",
        ".gauge-warning-lights",
    )
    for line in APP_CSS.splitlines():
        stripped = line.strip()
        if stripped.startswith(outcome_tokens):
            pytest.fail(f"Unscoped construction outcome selector: {stripped}")
