from posixpath import normpath
from urllib.parse import unquote


def is_public_asset(path: str) -> bool:
    """Return True for approved state-independent public assets.

    Approved paths:
    - /dilldrill_new_logo_2026.png: root icon fallback, no auth or tier logic.
    - /dd_icon.png: legacy root icon fallback, no auth or tier logic.
    - /favicon.ico: browser icon fallback, no auth or tier logic.
    - /static/: static mount assets, no auth or tier logic.
    - /sw.js: service worker route backed by static file, no auth or tier logic.
    - /offline.html: offline fallback route backed by static file, no auth or tier logic.

    TODO(Q7): /robots.txt was not present as a static file; leave it out.
    TODO(Q8): /sitemap.xml was not present as a static file; leave it out.
    """
    decoded = unquote(path or "")
    normalized = decoded.rstrip("/") or "/"

    if "//" in normalized:
        return False
    if ".." in normalized.split("/"):
        return False

    normalized = normpath(normalized)
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"

    exact_paths = frozenset({
        "/dd_icon.png",
        "/dilldrill_new_logo_2026.png",
        "/favicon.ico",
        "/sw.js",
        "/offline.html",
    })

    return normalized in exact_paths or normalized.startswith("/static/")
