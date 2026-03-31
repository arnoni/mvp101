def resolve_checkout_base(raw_origin: str | None) -> str:
    """
    Resolve checkout origin from APP_ORIGIN.

    Supports accidental comma-separated env values and falls back safely.
    """
    if not raw_origin:
        return 'http://localhost:8000'

    for candidate in (part.strip().rstrip('/') for part in raw_origin.split(',')):
        if candidate.startswith(('https://', 'http://')):
            return candidate

    return 'http://localhost:8000'
