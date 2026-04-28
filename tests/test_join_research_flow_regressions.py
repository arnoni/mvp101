from pathlib import Path


def test_bottom_join_and_user_access_use_shared_join_modal_open():
    app_js = Path("static/app.js").read_text(encoding="utf-8")
    assert "openJoinResearchModal(surface = 'hero_unlock_button')" in app_js
    assert "modals.support.openJoinResearchModal('user_access_modal');" in app_js
    assert "ModalSystem.openJoinResearchModal('demand_level_page');" in app_js
    assert "join_research_modal_reset_failed" in app_js
    assert "formatJoinModalErrorMessage(reason, errorId)" in app_js
    assert "isCurrentOperation(opType, opId)" in app_js
    assert "join_research_checkout_watchdog_timeout" in app_js
    assert "state.unlock.resendSubmitting = true;" in app_js
    assert "join_research_modal_closed" in app_js
    assert "logFlowEvent(eventName, payload = {})" in app_js
    assert "join_research_email_submit_started" in app_js
    assert "join_research_unlock_intent_request_started" in app_js
    assert "join_research_email_send_failed" in app_js
    assert "Join Research is temporarily unavailable." in app_js
    assert "utils.notify('Could not open Join Research right now. Please try again.', 'error');" not in app_js


def test_intent_created_path_transitions_to_check_email_step():
    app_js = Path("static/app.js").read_text(encoding="utf-8")
    marker = "if (data.ok === true && data.status === 'intent_created') {"
    assert marker in app_js
    snippet = app_js[app_js.index(marker): app_js.index(marker) + 1500]
    assert "this.showStep(3);" in snippet
    assert "Request saved for ${email}." in snippet
