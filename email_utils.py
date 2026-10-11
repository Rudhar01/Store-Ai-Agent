"""
email_utils.py
--------------
Sends email via generic SMTP (works with Gmail, Outlook, or any SMTP
provider — just supply credentials). If SMTP isn't configured, falls
back to printing the message to the server log instead of crashing,
so the forgot-password flow is still testable without real email set up.

Required environment variables (all optional — falls back to logging
if any are missing):
  SMTP_HOST      e.g. smtp.gmail.com
  SMTP_PORT      e.g. 587
  SMTP_USER      the account's email address
  SMTP_PASSWORD  the account's password or app-specific password
                 (Gmail requires an "App Password", not your normal
                 login password, if 2FA is enabled — normal accounts
                 need "less secure app access" enabled, which Google
                 has been phasing out, so an App Password is the
                 reliable option)
  SMTP_FROM      optional, defaults to SMTP_USER
"""

import os
import smtplib
from email.mime.text import MIMEText

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or "587")
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "") or SMTP_USER


def send_email(to_email: str, subject: str, body: str) -> bool:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD):
        print(f"[email_utils] SMTP not configured — would have sent to {to_email!r}:\nSubject: {subject}\n{body}")
        return False

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_email

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [to_email], msg.as_string())
        return True
    except Exception as e:
        print(f"[email_utils] Failed to send email to {to_email!r}: {e}")
        return False


def send_password_reset_email(to_email: str, reset_link: str) -> bool:
    subject = "Reset your RenderLink dashboard password"
    body = (
        f"We received a request to reset your RenderLink dashboard password.\n\n"
        f"Click the link below to choose a new password. This link expires in 1 hour:\n\n"
        f"{reset_link}\n\n"
        f"If you didn't request this, you can safely ignore this email."
    )
    return send_email(to_email, subject, body)
