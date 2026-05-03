from app.core.config import settings


def _first_valid_url(raw_value: str | None) -> str | None:
    if not raw_value:
        return None
    for candidate in (part.strip().rstrip('/') for part in raw_value.split(',')):
        if candidate.startswith(('https://', 'http://')):
            return candidate
    return None


def resolve_public_base_url() -> str:
    """Return canonical app base URL for user-facing links."""
    for key in ('NEXT_PUBLIC_SITE_URL', 'PUBLIC_BASE_URL', 'APP_BASE_URL', 'SITE_URL', 'APP_ORIGIN'):
        val = _first_valid_url(getattr(settings, key, None) if hasattr(settings, key) else None)
        if val:
            return val
    return 'http://localhost:8000'


def resolve_checkout_base(raw_origin: str | None) -> str:
    """Resolve checkout origin from APP_ORIGIN and canonical public URL fallback."""
    return _first_valid_url(raw_origin) or resolve_public_base_url()
