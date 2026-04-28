from pathlib import Path


def test_unlock_intent_structlog_markers_present():
    billing_py = Path("app/api/billing.py").read_text(encoding="utf-8")
    assert "unlock_intent_received" in billing_py
    assert "unlock_intent_turnstile_verified" in billing_py
    assert "unlock_intent_record_created" in billing_py
    assert "unlock_intent_magic_link_create_started" in billing_py
    assert "unlock_intent_response_ready" in billing_py
    assert "join_research_unlock_intent_request_started" in billing_py
    assert "join_research_unlock_intent_created" in billing_py
    assert "join_research_magic_link_token_created" in billing_py
    assert "join_research_email_send_started" in billing_py
    assert "join_research_email_send_succeeded" in billing_py
    assert "join_research_email_send_failed" in billing_py
    assert "join_research_flow_failed" in billing_py


def test_resend_magic_link_structlog_markers_present():
    auth_py = Path("app/api/auth.py").read_text(encoding="utf-8")
    assert "magic_link_resend_received" in auth_py
    assert "magic_link_resend_lookup_completed" in auth_py
    assert "magic_link_resend_simulated_send_started" in auth_py
    assert "magic_link_resend_real_send_finished" in auth_py
    assert "magic_link_resend_response_ready" in auth_py


def test_client_flow_telemetry_endpoint_logs_structured_events():
    routes_py = Path("app/api/routes.py").read_text(encoding="utf-8")
    assert 'class ClientFlowEventRequest(BaseModel):' in routes_py
    assert '@router.post("/telemetry/client-event")' in routes_py
    assert '"client_flow_event"' in routes_py
