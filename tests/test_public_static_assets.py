import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_STATIC = ROOT / "public" / "static"
MAIN_PY = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
LEGAL_BASE_HTML = (ROOT / "templates" / "legal" / "base.html").read_text(encoding="utf-8")
APP_JS = (PUBLIC_STATIC / "app.js").read_text(encoding="utf-8")
SW_JS = (PUBLIC_STATIC / "sw.js").read_text(encoding="utf-8")
VERCEL_JSON = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))


def test_required_static_assets_live_under_public_static():
    for relative in [
        "app.css",
        "app.js",
        "legal.css",
        "sw.js",
        "offline.html",
        "privacy.html",
        "graphics/DillDrill_banner_selected.svg",
        "images/site_01.jpg",
    ]:
        assert (PUBLIC_STATIC / relative).is_file()


def test_static_service_worker_and_root_offline_page_are_public_files():
    assert (ROOT / "public" / "offline.html").is_file()
    assert (PUBLIC_STATIC / "sw.js").is_file()
    assert (ROOT / "public" / "offline.html").read_bytes() == (
        PUBLIC_STATIC / "offline.html"
    ).read_bytes()


def test_browser_asset_urls_remain_unchanged():
    assert "/static/app.css" in INDEX_HTML
    assert "/static/app.js" in INDEX_HTML
    assert "/static/app.css" in LEGAL_BASE_HTML
    assert "/static/legal.css" in LEGAL_BASE_HTML
    assert "/static/graphics/DillDrill_banner_selected.svg" in SW_JS
    assert '"public", "static"' in MAIN_PY


def test_static_dir_resolution_falls_back_when_public_static_missing(tmp_path):
    from app.main import _resolve_static_dir

    legacy_static = tmp_path / "static"
    legacy_static.mkdir()

    assert _resolve_static_dir(str(tmp_path)) == str(legacy_static)


def test_static_dir_resolution_prefers_public_static(tmp_path):
    from app.main import _resolve_static_dir

    public_static = tmp_path / "public" / "static"
    legacy_static = tmp_path / "static"
    public_static.mkdir(parents=True)
    legacy_static.mkdir()

    assert _resolve_static_dir(str(tmp_path)) == str(public_static)


def test_dd_icon_is_preserved_and_copied_byte_for_byte():
    root_icon = ROOT / "dd_icon.png"
    public_icon = ROOT / "public" / "dd_icon.png"

    assert root_icon.is_file()
    assert public_icon.is_file()
    assert root_icon.read_bytes() == public_icon.read_bytes()


def test_templates_reference_public_png_icon_without_missing_favicons():
    active_templates = [INDEX_HTML, LEGAL_BASE_HTML]

    for template in active_templates:
        assert '<link rel="icon" type="image/png" href="/dd_icon.png">' in template
        assert '<link rel="apple-touch-icon" href="/dd_icon.png">' in template
        assert "/favicon.ico" not in template


def test_favicon_redirect_is_configured_before_fastapi_catch_all():
    redirects = VERCEL_JSON.get("redirects", [])
    rewrites = VERCEL_JSON.get("rewrites", [])

    assert redirects[0] == {
        "source": "/favicon.ico",
        "destination": "/dd_icon.png",
        "permanent": True,
    }
    assert rewrites[-1] == {"source": "/(.*)", "destination": "app/main.py"}


def test_fastapi_no_longer_has_204_favicon_route():
    assert '@app.get("/favicon.ico"' not in MAIN_PY
    assert "async def favicon" not in MAIN_PY
    assert "Response(status_code=204)" not in MAIN_PY


def test_service_worker_and_offline_urls_stay_stable():
    assert '@app.get("/sw.js"' in MAIN_PY
    assert '@app.get("/offline.html"' in MAIN_PY
    assert 'os.path.join(static_dir, "sw.js")' in MAIN_PY
    assert 'os.path.join(static_dir, "offline.html")' in MAIN_PY
    assert "navigator.serviceWorker.register" not in APP_JS
    assert "'/offline.html'" in SW_JS
