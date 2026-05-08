import asyncio
import hashlib
import html
from typing import Any, Optional

import resend
import sentry_sdk
import structlog

from app.core.config import settings

logger = structlog.get_logger(__name__)
RESEND_SEND_TIMEOUT_SECONDS = 10


def _hash_email(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def _safe_provider_response(response: Any) -> Any:
    if response is None or isinstance(response, (dict, list, str, int, float, bool)):
        return response
    try:
        return repr(response)
    except Exception as exc:
        return f"<unrepresentable {exc.__class__.__name__}>"


def _extract_provider_email_id(response: Any) -> Optional[str]:
    if isinstance(response, dict):
        email_id = response.get("id")
        return str(email_id) if email_id else None
    email_id = getattr(response, "id", None)
    return str(email_id) if email_id else None


def _extract_provider_error(response: Any) -> Optional[str]:
    if isinstance(response, dict):
        error = response.get("error") or response.get("message")
        if error:
            return str(error)
    error = getattr(response, "error", None) or getattr(response, "message", None)
    return str(error) if error else None


class EmailService:
    def __init__(self):
        self.api_key = settings.RESEND_API_KEY
        self.from_email = settings.RESEND_FROM_EMAIL
        self.reply_to = settings.RESEND_REPLY_TO

        if self.api_key:
            resend.api_key = self.api_key
        else:
            logger.warning("email_resend_api_key_missing")

        if not self.from_email:
            logger.warning("email_resend_from_email_missing")

    async def _send_async(self, params: dict, *, timeout_seconds: int = RESEND_SEND_TIMEOUT_SECONDS) -> Optional[str]:
        """
        Wrap the synchronous Resend SDK call in a worker thread with an explicit timeout,
        so resend failures do not silently stall higher-level request handlers.
        """
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(resend.Emails.send, params),
                timeout=timeout_seconds,
            )
            provider_error = _extract_provider_error(response)
            if provider_error:
                logger.error(
                    "magic_link_send_failed",
                    provider="resend",
                    reason="provider_error",
                    email_hash=_hash_email(str((params.get("to") or [""])[0])),
                    resend_response=_safe_provider_response(response),
                )
                sentry_sdk.capture_message("magic_link_send_failed", level="error")
                raise RuntimeError(f"Resend send failed: {provider_error}")
            email_id = _extract_provider_email_id(response)
            if not email_id:
                logger.error(
                    "magic_link_send_failed",
                    provider="resend",
                    reason="missing_provider_email_id",
                    email_hash=_hash_email(str((params.get("to") or [""])[0])),
                    resend_response=_safe_provider_response(response),
                )
                sentry_sdk.capture_message("magic_link_send_failed", level="error")
                raise RuntimeError(f"Resend send response did not include an email id: {response!r}")
            return email_id
        except asyncio.TimeoutError:
            logger.error(
                "E_EMAIL_SEND_FAILED",
                provider="resend",
                reason="timeout",
                timeout_seconds=timeout_seconds,
            )
            raise
        except Exception as exc:
            logger.error(
                "E_EMAIL_SEND_FAILED",
                provider="resend",
                reason="sdk_call_failed",
                error_class=exc.__class__.__name__,
                error_detail=str(exc),
            )
            raise

    async def send_magic_link(self, email: str, magic_link: str, expire_minutes: int = 30) -> bool:
        """Sends the magic link email via Resend."""
        if not self.api_key or not self.from_email:
            logger.error(
                "E_EMAIL_SEND_FAILED",
                provider="resend",
                reason="magic_link_config_missing",
                has_api_key=bool(self.api_key),
                has_from_email=bool(self.from_email),
                email=email,
            )
            return False

        safe_link = html.escape(magic_link)

        try:
            params = {
                "from": self.from_email,
                "to": [email],
                "reply_to": self.reply_to,
                "subject": "Your DillDrill Access Link",
                "html": f"""
                <div style="font-family: sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #eee; border-radius: 10px;">
                    <h2 style="color: #FF6B00;">DillDrill Access</h2>
                    <p>Click the button below to log in and unlock full access to DillDrill.</p>
                    <div style="margin: 30px 0;">
                        <a href="{safe_link}"
                           style="background-color: #FF6B00; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; font-weight: bold; display: inline-block;">
                           Unlock Full Access ➔
                        </a>
                    </div>
                    <p style="color: #666; font-size: 14px;">This link will expire in {expire_minutes} minutes.</p>
                    <hr style="border: 0; border-top: 1px solid #eee; margin: 20px 0;">
                    <p style="color: #999; font-size: 12px;">If you didn't request this email, you can safely ignore it.</p>
                </div>
                """,
            }

            email_id = await self._send_async(params)
            logger.info("email_send_magic_link_success", email=email, provider_email_id=email_id)
            return True

        except Exception as exc:
            logger.error(
                "magic_link_send_failed",
                provider="resend",
                reason="magic_link_send_failed",
                email_hash=_hash_email(email),
                resend_response=None,
                error_class=exc.__class__.__name__,
                error_detail=str(exc),
            )
            sentry_sdk.capture_exception(exc)
            return False

    async def send_test_email(self, email: str) -> bool:
        """Sends a simple test email to verify Resend configuration."""
        if not self.api_key or not self.from_email:
            logger.error(
                "email_send_test_config_missing",
                has_api_key=bool(self.api_key),
                has_from_email=bool(self.from_email),
                email=email,
            )
            return False

        try:
            params = {
                "from": self.from_email,
                "to": [email],
                "reply_to": self.reply_to,
                "subject": "DillDrill Resend Test",
                "html": "<strong>It works!</strong><p>This is a test email from your DillDrill app using Resend.</p>",
            }
            email_id = await self._send_async(params)
            logger.info("email_send_test_success", email=email, provider_email_id=email_id)
            return True
        except Exception as exc:
            logger.error(
                "email_send_test_failed",
                email=email,
                error_class=exc.__class__.__name__,
                error_detail=str(exc),
            )
            return False
