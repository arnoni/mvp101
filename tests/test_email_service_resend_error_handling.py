import asyncio
import time
import sys
from pathlib import Path

import pytest

pytest.importorskip("resend")

sys.path.append(str(Path(__file__).resolve().parents[1]))

from email_service import EmailService


def test_send_magic_link_returns_false_when_provider_raises(monkeypatch):
    monkeypatch.setattr("email_service.settings.RESEND_API_KEY", "rk_test", raising=False)
    monkeypatch.setattr("email_service.settings.RESEND_FROM_EMAIL", "noreply@example.com", raising=False)

    def _boom(_params):
        raise RuntimeError("provider outage")

    monkeypatch.setattr("email_service.resend.Emails.send", _boom)

    service = EmailService()
    result = asyncio.run(service.send_magic_link("user@example.com", "https://example.com/magic"))

    assert result is False


def test_send_async_times_out(monkeypatch):
    monkeypatch.setattr("email_service.settings.RESEND_API_KEY", "rk_test", raising=False)
    monkeypatch.setattr("email_service.settings.RESEND_FROM_EMAIL", "noreply@example.com", raising=False)

    def _slow_send(_params):
        time.sleep(0.05)
        return {"id": "ok"}

    monkeypatch.setattr("email_service.resend.Emails.send", _slow_send)

    service = EmailService()

    async def _run():
        await service._send_async({"to": ["user@example.com"]}, timeout_seconds=0.01)

    with pytest.raises(TimeoutError):
        asyncio.run(_run())


def test_send_magic_link_returns_false_when_provider_response_has_no_id(monkeypatch):
    monkeypatch.setattr("email_service.settings.RESEND_API_KEY", "rk_test", raising=False)
    monkeypatch.setattr("email_service.settings.RESEND_FROM_EMAIL", "noreply@example.com", raising=False)

    def _suppressed_send(_params):
        return {"object": "email"}

    captured = {}

    def _capture_message(message, level=None):
        captured["message"] = message
        captured["level"] = level

    monkeypatch.setattr("email_service.resend.Emails.send", _suppressed_send)
    monkeypatch.setattr("email_service.sentry_sdk.capture_message", _capture_message)
    monkeypatch.setattr("email_service.sentry_sdk.capture_exception", lambda _exc: None)

    service = EmailService()
    result = asyncio.run(service.send_magic_link("user@example.com", "https://example.com/magic"))

    assert result is False
    assert captured == {"message": "magic_link_send_failed", "level": "error"}
