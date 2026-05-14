import importlib
import sys
import types
from unittest.mock import MagicMock


def _install_sentry_stub():
    if "sentry_sdk" in sys.modules:
        return
    sentry_sdk = types.ModuleType("sentry_sdk")
    sentry_sdk.push_scope = MagicMock()
    sentry_sdk.capture_message = MagicMock()
    sentry_sdk.capture_exception = MagicMock()
    sentry_sdk.flush = MagicMock()
    sentry_sdk.init = MagicMock()
    sentry_sdk.configure_scope = MagicMock()

    integrations = types.ModuleType("sentry_sdk.integrations")
    fastapi = types.ModuleType("sentry_sdk.integrations.fastapi")
    logging_mod = types.ModuleType("sentry_sdk.integrations.logging")
    starlette = types.ModuleType("sentry_sdk.integrations.starlette")
    fastapi.FastApiIntegration = MagicMock()
    logging_mod.LoggingIntegration = MagicMock()
    starlette.StarletteIntegration = MagicMock()

    sys.modules["sentry_sdk"] = sentry_sdk
    sys.modules["sentry_sdk.integrations"] = integrations
    sys.modules["sentry_sdk.integrations.fastapi"] = fastapi
    sys.modules["sentry_sdk.integrations.logging"] = logging_mod
    sys.modules["sentry_sdk.integrations.starlette"] = starlette


_install_sentry_stub()
observability = importlib.import_module("app.core.observability")


def test_capture_message_sanitizes_context_and_sends_sentry_message(monkeypatch):
    pushed_scope = MagicMock()
    push_context = MagicMock()
    push_context.__enter__.return_value = pushed_scope
    push_context.__exit__.return_value = False

    monkeypatch.setattr(observability.sentry_sdk, "push_scope", lambda: push_context)
    capture_message = MagicMock()
    monkeypatch.setattr(observability.sentry_sdk, "capture_message", capture_message)

    logger = MagicMock()

    observability.capture_message(
        "silent_fallback_detected",
        level="warning",
        logger=logger,
        api_token="secret-value",
        nested={"password": "hidden", "safe": "ok"},
    )

    logger.warning.assert_called_once()
    capture_message.assert_called_once_with("silent_fallback_detected", level="warning")
    pushed_scope.set_tag.assert_any_call("event", "silent_fallback_detected")
    pushed_scope.set_tag.assert_any_call("reported_kind", "message")
    pushed_scope.set_tag.assert_any_call("api_token", "<redacted>")
    pushed_scope.set_extra.assert_any_call(
        "nested",
        {"password": "<redacted>", "safe": "ok"},
    )


def test_before_send_scrubs_sensitive_sentry_event_payload():
    event = {
        "request": {"headers": {"Authorization": "Bearer abc", "X-Safe": "ok"}},
        "extra": {"cookie": "session=secret", "value": "kept"},
    }

    scrubbed = observability._before_send(event, {})

    assert scrubbed["request"]["headers"]["Authorization"] == "<redacted>"
    assert scrubbed["request"]["headers"]["X-Safe"] == "ok"
    assert scrubbed["extra"]["cookie"] == "<redacted>"
    assert scrubbed["extra"]["value"] == "kept"
