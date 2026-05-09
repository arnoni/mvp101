from pathlib import Path


def test_bottom_join_and_user_access_use_shared_join_modal_open():
    app_js = Path("static/app.js").read_text(encoding="utf-8")
    assert 'openJoinResearchModal(surface = "hero_unlock_button")' in app_js
    assert 'openJoinResearchModal("user_access_modal")' in app_js
    assert 'openJoinResearchModal("demand_level_page")' in app_js
    assert "join_research_modal_reset_failed" in app_js
    assert "productSearchErrorMessage(code)" in app_js
    assert "isCurrentOperation(type, id)" in app_js
    assert "if (!isCurrentOperation(\"checkout\", opId)) return;" in app_js
    assert "state.unlock.resendSubmitting = true;" in app_js
    assert "join_research_access_modal_closed" in app_js
    assert "logFlowEvent(eventName, payload = {})" in app_js
    assert "join_research_access_email_submit_started" in app_js
    assert "join_research_access_magic_link_succeeded" in app_js
    assert "join_research_access_flow_failed" in app_js
    assert "Join Research is temporarily unavailable." in app_js
    assert "utils.notify('Could not open Join Research right now. Please try again.', 'error');" not in app_js


def test_intent_created_path_transitions_to_check_email_step():
    app_js = Path("static/app.js").read_text(encoding="utf-8")
    marker = 'if (data?.ok !== true || data?.status !== "magic_link_sent") {'
    assert marker in app_js
    snippet = app_js[app_js.index(marker): app_js.index(marker) + 1500]
    assert "showSupportStep(3);" in snippet
    assert "We've sent a secure access link to ${email}." in snippet


def test_empty_location_is_not_sent_as_zero_or_simulated_report():
    app_js = Path("static/app.js").read_text(encoding="utf-8")
    assert 'location_input: $("locationInput")?.value || ""' in app_js
    assert "lat: parsed.lat" in app_js
    assert "lon: parsed.lng" in app_js
    assert "Demand analyzed (Fallback)" not in app_js
    assert "Simulated Analysis (Network Fallback)" not in app_js


def test_join_research_modal_open_uses_safe_exception_logger():
    app_js = Path("static/app.js").read_text(encoding="utf-8")
    assert "function safeLogJoinResearchException(eventName, err, extra = {})" in app_js
    assert 'if (typeof window.logJoinResearchException === "function")' in app_js
    assert 'safeLogJoinResearchException("join_research_modal_reset_failed"' in app_js
