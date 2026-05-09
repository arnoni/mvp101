from pathlib import Path


def test_disabled_72_hour_card_is_native_disabled_button_with_label():
    template = Path("templates/index.html").read_text(encoding="utf-8")
    card_anchor = 'class="plan-card plan-card-disabled" data-plan="sim_3_day"'
    assert card_anchor in template
    snippet = template[template.index(card_anchor): template.index(card_anchor) + 220]
    assert "disabled" in snippet
    assert "aria-disabled" not in snippet
    assert "tabindex" not in snippet
    assert 't.get(\'coming_soon\', \'Coming soon\')' in template


def test_frontend_keeps_sim_1_day_submission_and_disabled_click_analytics_payload():
    app_js = Path("static/app.js").read_text(encoding="utf-8")
    assert "const plan = 'sim_1_day';" in app_js
    assert "plan,\n            turnstile_token" in app_js
    assert "disabled_access_level_clicked" in app_js
    assert "join_research_access_modal" in app_js
    assert "72_hour_preview" in app_js
    assert "disabled_not_available_in_simulated_flow" in app_js


def test_frontend_modal_state_and_support_close_reset_are_wired():
    app_js = Path("static/app.js").read_text(encoding="utf-8")
    assert 'modals: { active: null }' in app_js
    assert 'el.dispatchEvent(new CustomEvent("modal:close"' in app_js
    assert "state.unlock.submitting = false;" in app_js


def test_frontend_persists_intent_id_before_intent_created_early_return():
    app_js = Path("static/app.js").read_text(encoding="utf-8")
    persist_marker = "if (data?.intent_id) {\n        sessionStorage.setItem(\"last_payment_intent_id\", data.intent_id);\n      }"
    early_return_marker = 'if (data?.ok !== true || data?.status !== "magic_link_sent") {'
    assert persist_marker in app_js
    assert app_js.index(persist_marker) < app_js.index(early_return_marker)


def test_auth_magic_landing_accepts_request_object_for_cookie_and_state_reads():
    auth_py = Path("app/api/auth.py").read_text(encoding="utf-8")
    assert "async def magic_landing(" in auth_py
    assert "request: Request," in auth_py
