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
    assert 'modals: { active: null, history: [] }' in app_js
    assert "supportModal?.addEventListener('modal:close'" in app_js
    assert "state.unlock.checkoutSubmitting = false;" in app_js
