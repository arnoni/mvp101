from app.utils.url import resolve_checkout_base


def test_resolve_checkout_base_uses_first_valid_origin_from_csv():
    raw = "https://dilldrill.com,https//www.dilldrill.com"
    assert resolve_checkout_base(raw) == "https://dilldrill.com"


def test_resolve_checkout_base_falls_back_for_invalid_values():
    raw = "dilldrill.com,https//www.dilldrill.com"
    assert resolve_checkout_base(raw) == "http://localhost:8000"
