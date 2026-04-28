from pathlib import Path


def test_unlock_intent_structlog_markers_present():
    billing_py = Path("app/api/billing.py").read_text(encoding="utf-8")
    assert "unlock_intent_received" in billing_py
    assert "unlock_intent_turnstile_verified" in billing_py
    assert "unlock_intent_record_created" in billing_py
    assert "unlock_intent_magic_link_create_started" in billing_py
    assert "unlock_intent_response_ready" in billing_py


def test_resend_magic_link_structlog_markers_present():
    auth_py = Path("app/api/auth.py").read_text(encoding="utf-8")
    assert "magic_link_resend_received" in auth_py
    assert "magic_link_resend_lookup_completed" in auth_py
    assert "magic_link_resend_simulated_send_started" in auth_py
    assert "magic_link_resend_real_send_finished" in auth_py
    assert "magic_link_resend_response_ready" in auth_py
