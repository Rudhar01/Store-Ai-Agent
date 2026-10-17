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
        safe_subj = str(subject).encode("ascii", "replace").decode("ascii")
        print(f"[email_utils] SMTP not configured - would have sent to {to_email!r}: Subject: {safe_subj}")
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


def send_html_email(to_email: str, subject: str, html_body: str, text_body: str = "") -> bool:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD):
        safe_subj = str(subject).encode("ascii", "replace").decode("ascii")
        print(f"[email_utils] SMTP not configured - would have sent HTML email to {to_email!r}: Subject: {safe_subj}")
        return False

    from email.mime.multipart import MIMEMultipart
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_email

    if text_body:
        msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [to_email], msg.as_string())
        return True
    except Exception as e:
        print(f"[email_utils] Failed to send HTML email to {to_email!r}: {e}")
        return False


def send_restock_notification_email(
    to_email: str,
    store_name: str,
    product_title: str,
    product_url: str = "",
    product_image: str = "",
    price: str = "",
) -> bool:
    subject = f"🎉 Good news! '{product_title}' is back in stock at {store_name}"
    text_body = (
        f"Hello,\n\n"
        f"The item you were waiting for, '{product_title}', is officially back in stock at {store_name}!\n\n"
        f"Price: {price}\n"
        f"Shop now before it sells out again: {product_url or 'Visit store'}\n\n"
        f"Happy shopping,\n{store_name} AI Assistant"
    )

    img_tag = f'<img src="{product_image}" alt="{product_title}" style="max-width:280px;height:auto;border-radius:12px;margin:16px 0;box-shadow:0 4px 12px rgba(0,0,0,0.08);">' if product_image else ""
    price_tag = f'<div style="font-size:18px;font-weight:700;color:#111827;margin-bottom:12px;">Price: {price}</div>' if price else ""
    cta_btn = f'<a href="{product_url}" style="display:inline-block;background:#111827;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:8px;font-weight:600;font-size:14px;margin-top:8px;">View & Order Now &rarr;</a>' if product_url else ""

    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#F9FAFB;padding:24px;color:#1F2937;">
      <div style="max-width:520px;margin:0 auto;background:#FFFFFF;border-radius:16px;padding:32px;border:1px solid #E5E7EB;box-shadow:0 4px 20px rgba(0,0,0,0.04);">
        <div style="display:inline-block;background:#ECFDF5;color:#065F46;font-size:12px;font-weight:700;padding:4px 10px;border-radius:999px;margin-bottom:12px;border:1px solid #A7F3D0;">
          Back in Stock Alert
        </div>
        <h2 style="font-size:22px;font-weight:800;color:#111827;margin-top:0;margin-bottom:8px;">It's finally back!</h2>
        <p style="font-size:14px;color:#4B5563;line-height:1.5;">You asked us to notify you the moment <strong>{product_title}</strong> was restocked at <strong>{store_name}</strong>. Fresh stock has just arrived!</p>
        {img_tag}
        {price_tag}
        {cta_btn}
        <hr style="border:none;border-top:1px solid #E5E7EB;margin:24px 0 16px;">
        <p style="font-size:11px;color:#9CA3AF;margin:0;">You received this email because you subscribed to back-in-stock alerts via the {store_name} AI Shopping Assistant.</p>
      </div>
    </body>
    </html>
    """
    return send_html_email(to_email, subject, html_body, text_body)
