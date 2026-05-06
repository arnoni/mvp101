from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_PY = (ROOT / "app" / "main.py").read_text()
INDEX_HTML = (ROOT / "templates" / "index.html").read_text()
PYPROJECT = (ROOT / "pyproject.toml").read_text()
REQUIREMENTS = (ROOT / "requirements.txt").read_text()
APP_JS = (ROOT / "static" / "app.js").read_text()


def test_csp_allows_sentry_turnstile_and_posthog_scripts_with_nonce():
    assert "Content-Security-Policy" in MAIN_PY
    assert '"script-src"' in MAIN_PY
    assert "f\"'nonce-{nonce}'\"" in MAIN_PY
    assert "https://browser.sentry-cdn.com" in MAIN_PY
    assert "https://challenges.cloudflare.com" in MAIN_PY
    assert "https://*.i.posthog.com" in MAIN_PY


def test_csp_allows_turnstile_frames_and_telemetry_connects():
    assert '"frame-src": ["https://challenges.cloudflare.com"]' in MAIN_PY
    assert "https://*.sentry.io" in MAIN_PY
    assert "https://*.ingest.sentry.io" in MAIN_PY
    assert "https://*.posthog.com" in MAIN_PY


def test_inline_and_external_scripts_have_csp_nonce():
    assert '<script nonce="{{ csp_nonce }}">' in INDEX_HTML
    assert '<script nonce="{{ csp_nonce }}" src="https://browser.sentry-cdn.com/7.120.3/bundle.min.js"' in INDEX_HTML
    assert '<script nonce="{{ csp_nonce }}" src="/static/app.js?v=1.2.2"' in INDEX_HTML
    assert '<script nonce="{{ csp_nonce }}" src="https://challenges.cloudflare.com/turnstile/v0/api.js"' in INDEX_HTML


def test_trusted_types_script_url_policy_wraps_posthog_injection():
    assert "window.__ddTrustedTypesPolicy" in INDEX_HTML
    assert "createScriptURL" in INDEX_HTML
    assert "__ddTrustedTypesPolicy?.createScriptURL" in INDEX_HTML
    for unsafe_sink in ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"]:
        assert unsafe_sink not in APP_JS


def test_runtime_dependencies_are_declared_for_pytest_contracts():
    for dependency in ['"fastapi==0.115.0"', '"pydantic-settings==2.3.4"']:
        assert dependency in PYPROJECT
    assert "fastapi==0.115.0" in REQUIREMENTS
    assert "pydantic-settings==2.3.4" in REQUIREMENTS
