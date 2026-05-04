from pathlib import Path


def test_search_turnstile_policy_matches_frontend_contract():
    app_js = Path("static/app.js").read_text(encoding="utf-8")
    routes_py = Path("app/api/routes.py").read_text(encoding="utf-8")

    assert 'fetch("/api/search"' in app_js
    assert "turnstile_token: state.verification.token" in app_js
    assert "Render hero Turnstile on page load" in app_js
    assert "gate_result = await run_gate(" in routes_py
    assert "data_turnstile_token=data.turnstile_token" in routes_py
