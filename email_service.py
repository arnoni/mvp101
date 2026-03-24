import logging 
import html 
import asyncio 
import resend 
from typing import Optional 
from app.core.config import settings 
 
logger = logging.getLogger(__name__) 
 
class EmailService: 
    def __init__(self): 
        self.api_key = settings.RESEND_API_KEY 
        self.from_email = settings.RESEND_FROM_EMAIL 
        self.reply_to = settings.RESEND_REPLY_TO
        
        if self.api_key: 
            resend.api_key = self.api_key 
        else: 
            logger.warning("RESEND_API_KEY not configured. Emails will not be sent.") 
            
        if not self.from_email: 
            logger.warning("RESEND_FROM_EMAIL not configured. Email sending will fail.") 
 
    async def _send_async(self, params: dict) -> Optional[str]: 
        """ 
        Wraps the synchronous Resend SDK call in an async thread to prevent 
        blocking the main event loop. 
        """ 
        try:
            # asyncio.to_thread is available in Python 3.9+ 
            # It runs the blocking network call in a separate thread. 
            response = await asyncio.to_thread(resend.Emails.send, params) 
            
            # Depending on the Resend SDK version, it might return a dict or an object 
            if isinstance(response, dict): 
                return response.get("id") 
            return getattr(response, "id", "Unknown ID")
        except Exception as e:
            logger.error(f"EMAIL_RESEND_SDK_CALL_FAILED: {e}")
            raise 
 
    async def send_magic_link(self, email: str, magic_link: str, expire_minutes: int = 30) -> bool: 
        """Sends the magic link email via Resend.""" 
        if not self.api_key or not self.from_email: 
            logger.error(f"EMAIL_CONFIG_MISSING: Cannot send magic link to {email}: Resend configuration missing.") 
            return False 
 
        # Escape the link to prevent HTML breaking or injection 
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
                """ 
            } 
            
            email_id = await self._send_async(params) 
            logger.info(f"EMAIL_SENT: ✅ Magic link sent to {email}. ID: {email_id}") 
            return True 
            
        except Exception as e: 
            # logger.exception automatically includes the full stack trace and the error message 
            logger.exception(f"EMAIL_SEND_MAGIC_LINK_FAILED: ❌ Failed to send magic link email to {email}: {e}") 
            return False 
 
    async def send_test_email(self, email: str) -> bool: 
        """Sends a simple test email to verify Resend configuration.""" 
        if not self.api_key or not self.from_email: 
            logger.error("EMAIL_CONFIG_MISSING: Cannot send test email: Resend configuration missing.") 
            return False 
 
        try: 
            params = { 
                "from": self.from_email, 
                "to": [email], 
                "reply_to": self.reply_to,
                "subject": "DillDrill Resend Test", 
                "html": "<strong>It works!</strong><p>This is a test email from your DillDrill app using Resend.</p>" 
            } 
            email_id = await self._send_async(params) 
            logger.info(f"EMAIL_TEST_SENT: ✅ Test email sent to {email}. ID: {email_id}") 
            return True 
        except Exception as e: 
            logger.exception(f"EMAIL_SEND_TEST_FAILED: ❌ Failed to send test email to {email}: {e}") 
            return False