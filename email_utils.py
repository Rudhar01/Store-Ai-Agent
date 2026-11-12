"""
email_utils.py
--------------
Sends emails via SMTP using support@dripire.com as the sender address,
with Reply-To dynamically routed to the store owner's email address retrieved
from the database.

Supports:
  1. Password reset emails (dashboard users).
  2. Restock notification alerts.
  3. Price drop alerts.
  4. Standing instruction reorder reminders.
  5. Abandoned cart recovery emails.
  6. Order cancellation & edit receipts.
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

DEFAULT_SENDER = "support@dripire.com"


def _get_smtp_config():
    host = os.environ.get("SMTP_HOST", "").strip()
    port_str = os.environ.get("SMTP_PORT", "").strip() or "587"
    try:
        port = int(port_str)
    except ValueError:
        port = 587
    user = (os.environ.get("SMTP_USER") or os.environ.get("EMAIL_HOST_USER") or DEFAULT_SENDER).strip()
    password = (
        os.environ.get("SMTP_PASSWORD")
        or os.environ.get("SMTP_PASS")
        or os.environ.get("EMAIL_HOST_PASSWORD")
        or ""
    ).strip()
    from_addr = (
        os.environ.get("SMTP_FROM")
        or os.environ.get("EMAIL_FROM")
        or DEFAULT_SENDER
    ).strip()
    return host, port, user, password, from_addr


def is_smtp_configured() -> bool:
    host, _, user, password, _ = _get_smtp_config()
    return bool(host and user and password)


def send_email(to_email: str, subject: str, body: str, reply_to: str = None, from_name: str = None) -> bool:
    host, port, user, password, from_addr = _get_smtp_config()
    if not (host and user and password):
        safe_subj = str(subject).encode("ascii", "replace").decode("ascii")
        print(f"[email_utils] SMTP not configured - would have sent to {to_email!r}: Subject: {safe_subj}")
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    msg["To"] = to_email
    if reply_to:
        msg["Reply-To"] = reply_to.strip()

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=12) as server:
                server.login(user, password)
                server.sendmail(from_addr, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=12) as server:
                server.starttls()
                server.login(user, password)
                server.sendmail(from_addr, [to_email], msg.as_string())
        print(f"[email_utils] Email sent successfully to {to_email!r} (Subject: {subject}, Reply-To: {reply_to})")
        return True
    except Exception as e:
        print(f"[email_utils] Failed to send email to {to_email!r}: {e}")
        return False


def send_html_email(to_email: str, subject: str, html_body: str, text_body: str = "", reply_to: str = None, from_name: str = None) -> bool:
    host, port, user, password, from_addr = _get_smtp_config()
    if not (host and user and password):
        safe_subj = str(subject).encode("ascii", "replace").decode("ascii")
        print(f"[email_utils] SMTP not configured - would have sent HTML email to {to_email!r}: Subject: {safe_subj}")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    msg["To"] = to_email
    if reply_to:
        msg["Reply-To"] = reply_to.strip()

    if text_body:
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=12) as server:
                server.login(user, password)
                server.sendmail(from_addr, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=12) as server:
                server.starttls()
                server.login(user, password)
                server.sendmail(from_addr, [to_email], msg.as_string())
        print(f"[email_utils] HTML email sent successfully to {to_email!r} (Subject: {subject}, Reply-To: {reply_to})")
        return True
    except Exception as e:
        print(f"[email_utils] Failed to send HTML email to {to_email!r}: {e}")
        return False


def send_password_reset_email(to_email: str, reset_link: str) -> bool:
    subject = "Reset your RenderLink dashboard password"
    text_body = (
        f"We received a request to reset your RenderLink dashboard password.\n\n"
        f"Click the link below to choose a new password. This link expires in 1 hour:\n\n"
        f"{reset_link}\n\n"
        f"If you didn't request this, you can safely ignore this email."
    )
    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#F9FAFB;padding:24px;color:#1F2937;margin:0;">
      <div style="max-width:520px;margin:0 auto;background:#FFFFFF;border-radius:16px;padding:32px;border:1px solid #E5E7EB;box-shadow:0 4px 20px rgba(0,0,0,0.04);">
        <div style="display:inline-block;background:#F3F4F6;color:#111827;font-size:12px;font-weight:700;padding:5px 12px;border-radius:999px;margin-bottom:14px;">
          Account Security
        </div>
        <h2 style="font-size:22px;font-weight:800;color:#111827;margin-top:0;margin-bottom:10px;">Reset your password</h2>
        <p style="font-size:14px;color:#4B5563;line-height:1.6;margin-bottom:24px;">
          We received a request to reset your dashboard password. Click the button below to choose a new password. This link expires in <strong>1 hour</strong>.
        </p>
        <div style="text-align:left;margin-bottom:28px;">
          <a href="{reset_link}" style="display:inline-block;background:#111827;color:#ffffff;text-decoration:none;padding:12px 28px;border-radius:10px;font-weight:600;font-size:14px;box-shadow:0 2px 6px rgba(0,0,0,0.15);">Reset Password &rarr;</a>
        </div>
        <p style="font-size:12px;color:#6B7280;line-height:1.5;margin-bottom:20px;">
          If the button doesn't work, copy and paste this link into your browser:<br>
          <a href="{reset_link}" style="color:#2563EB;word-break:break-all;">{reset_link}</a>
        </p>
        <hr style="border:none;border-top:1px solid #E5E7EB;margin:24px 0 16px;">
        <p style="font-size:11px;color:#9CA3AF;margin:0;">If you didn't request a password reset, you can safely ignore this email.</p>
      </div>
    </body>
    </html>
    """
    return send_html_email(to_email, subject, html_body, text_body, from_name="Dripire Security")


def send_restock_notification_email(
    to_email: str,
    store_name: str,
    product_title: str,
    product_url: str = "",
    product_image: str = "",
    price: str = "",
    reply_to: str = None,
) -> bool:
    if not product_title or not str(product_title).strip():
        print(f"[email_utils] Aborting restock email to {to_email}: missing product_title.")
        return False

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
        <p style="font-size:11px;color:#9CA3AF;margin:0;">Reply to this email to contact {store_name} directly.</p>
      </div>
    </body>
    </html>
    """
    return send_html_email(to_email, subject, html_body, text_body, reply_to=reply_to, from_name=f"{store_name} via Dripire")


def send_price_drop_email(
    to_email: str,
    store_name: str,
    product_title: str,
    new_price: str,
    old_price: str = "",
    product_url: str = "",
    product_image: str = "",
    reply_to: str = None,
) -> bool:
    if not product_title or not new_price:
        print(f"[email_utils] Aborting price drop email to {to_email}: missing product_title or new_price.")
        return False

    subject = f"📉 Price Drop Alert: '{product_title}' is now {new_price} at {store_name}!"
    text_body = (
        f"Great news!\n\n"
        f"You set a price drop alert for '{product_title}'.\n"
        f"It just dropped to {new_price} (was {old_price or 'higher'})!\n\n"
        f"Shop now: {product_url or 'Visit store'}\n\n"
        f"Best,\n{store_name}"
    )

    img_tag = f'<img src="{product_image}" alt="{product_title}" style="max-width:280px;height:auto;border-radius:12px;margin:16px 0;box-shadow:0 4px 12px rgba(0,0,0,0.08);">' if product_image else ""
    cta_btn = f'<a href="{product_url}" style="display:inline-block;background:#111827;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:8px;font-weight:600;font-size:14px;margin-top:12px;">Get It at {new_price} &rarr;</a>' if product_url else ""

    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#F9FAFB;padding:24px;color:#1F2937;">
      <div style="max-width:520px;margin:0 auto;background:#FFFFFF;border-radius:16px;padding:32px;border:1px solid #E5E7EB;box-shadow:0 4px 20px rgba(0,0,0,0.04);">
        <div style="display:inline-block;background:#FEF3C7;color:#92400E;font-size:12px;font-weight:700;padding:4px 10px;border-radius:999px;margin-bottom:12px;border:1px solid #FCD34D;">
          Price Drop Alert
        </div>
        <h2 style="font-size:22px;font-weight:800;color:#111827;margin-top:0;margin-bottom:8px;">The price just dropped!</h2>
        <p style="font-size:14px;color:#4B5563;line-height:1.5;">
          You asked us to track <strong>{product_title}</strong> at <strong>{store_name}</strong>. The price has reached your target!
        </p>
        {img_tag}
        <div style="margin:16px 0;">
          <span style="font-size:22px;font-weight:800;color:#059669;">{new_price}</span>
          {'<span style="font-size:14px;color:#9CA3AF;text-decoration:line-through;margin-left:8px;">' + old_price + '</span>' if old_price else ''}
        </div>
        {cta_btn}
        <hr style="border:none;border-top:1px solid #E5E7EB;margin:24px 0 16px;">
        <p style="font-size:11px;color:#9CA3AF;margin:0;">Reply to this email to contact {store_name} directly.</p>
      </div>
    </body>
    </html>
    """
    return send_html_email(to_email, subject, html_body, text_body, reply_to=reply_to, from_name=f"{store_name} via Dripire")


def send_reorder_reminder_email(
    to_email: str,
    store_name: str,
    product_title: str,
    reorder_url: str = "",
    product_image: str = "",
    reply_to: str = None,
) -> bool:
    if not product_title or not str(product_title).strip():
        print(f"[email_utils] Aborting reorder email to {to_email}: missing product_title.")
        return False

    subject = f"⏰ Time to reorder '{product_title}' from {store_name}?"
    text_body = (
        f"Hi there,\n\n"
        f"You previously asked us to remind you to reorder '{product_title}'.\n"
        f"To restock before you run out, click here: {reorder_url or 'Visit store'}\n\n"
        f"Best,\n{store_name}"
    )

    img_tag = f'<img src="{product_image}" alt="{product_title}" style="max-width:280px;height:auto;border-radius:12px;margin:16px 0;box-shadow:0 4px 12px rgba(0,0,0,0.08);">' if product_image else ""
    cta_btn = f'<a href="{reorder_url}" style="display:inline-block;background:#111827;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:8px;font-weight:600;font-size:14px;margin-top:12px;">Quick Reorder Now &rarr;</a>' if reorder_url else ""

    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#F9FAFB;padding:24px;color:#1F2937;">
      <div style="max-width:520px;margin:0 auto;background:#FFFFFF;border-radius:16px;padding:32px;border:1px solid #E5E7EB;box-shadow:0 4px 20px rgba(0,0,0,0.04);">
        <div style="display:inline-block;background:#EEF2FF;color:#3730A3;font-size:12px;font-weight:700;padding:4px 10px;border-radius:999px;margin-bottom:12px;border:1px solid #C7D2FE;">
          Reorder Reminder
        </div>
        <h2 style="font-size:22px;font-weight:800;color:#111827;margin-top:0;margin-bottom:8px;">Running low on '{product_title}'?</h2>
        <p style="font-size:14px;color:#4B5563;line-height:1.5;">
          Per your standing instruction, here is your friendly reminder to reorder <strong>{product_title}</strong> from <strong>{store_name}</strong> so you never run out!
        </p>
        {img_tag}
        {cta_btn}
        <hr style="border:none;border-top:1px solid #E5E7EB;margin:24px 0 16px;">
        <p style="font-size:11px;color:#9CA3AF;margin:0;">Reply to this email to contact {store_name} directly.</p>
      </div>
    </body>
    </html>
    """
    return send_html_email(to_email, subject, html_body, text_body, reply_to=reply_to, from_name=f"{store_name} via Dripire")


def send_abandoned_cart_email(
    to_email: str,
    store_name: str,
    cart_items: list,
    checkout_url: str = "",
    total_price: str = "",
    reply_to: str = None,
) -> bool:
    if not cart_items or not isinstance(cart_items, list):
        print(f"[email_utils] Aborting abandoned cart email to {to_email}: cart_items is empty. Refusing to send dummy or empty data.")
        return False

    subject = f"🛒 Did you leave something behind at {store_name}?"
    item_rows = ""
    for it in cart_items[:10]:
        title = it.get("title") or it.get("name") or "Product"
        qty = it.get("quantity") or 1
        raw_price = str(it.get("price") or "").strip()
        if raw_price and not any(raw_price.startswith(sym) for sym in ("₹", "$", "€", "£")):
            raw_price = f"₹{raw_price}"
        img = it.get("image") or ""
        img_html = f'<img src="{img}" alt="{title}" style="width:52px;height:52px;border-radius:8px;object-fit:cover;margin-right:14px;border:1px solid #E5E7EB;">' if img else ""
        item_rows += f"""
        <div style="display:flex;align-items:center;padding:12px 0;border-bottom:1px solid #F3F4F6;">
          {img_html}
          <div style="flex:1;">
            <div style="font-size:14px;font-weight:600;color:#111827;">{title}</div>
            <div style="font-size:12px;color:#6B7280;margin-top:2px;">Qty: {qty}</div>
          </div>
          <div style="font-size:14px;font-weight:700;color:#111827;">{raw_price}</div>
        </div>
        """

    cta_btn = f'<a href="{checkout_url}" style="display:inline-block;background:#111827;color:#ffffff;text-decoration:none;padding:12px 28px;border-radius:8px;font-weight:600;font-size:14px;margin-top:16px;">Complete Your Order &rarr;</a>' if checkout_url else ""

    text_body = (
        f"Hi,\n\n"
        f"We noticed you left items in your cart at {store_name}.\n"
        f"We've reserved them for a limited time. Complete your order here: {checkout_url}\n\n"
        f"Best,\n{store_name}"
    )

    formatted_total = str(total_price or "").strip()
    if formatted_total and not any(formatted_total.startswith(sym) for sym in ("₹", "$", "€", "£")):
        formatted_total = f"₹{formatted_total}"

    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#F9FAFB;padding:24px;color:#1F2937;">
      <div style="max-width:520px;margin:0 auto;background:#FFFFFF;border-radius:16px;padding:32px;border:1px solid #E5E7EB;box-shadow:0 4px 20px rgba(0,0,0,0.04);">
        <div style="display:inline-block;background:#EFF6FF;color:#1E40AF;font-size:12px;font-weight:700;padding:4px 10px;border-radius:999px;margin-bottom:12px;border:1px solid #BFDBFE;">
          Cart Saved
        </div>
        <h2 style="font-size:22px;font-weight:800;color:#111827;margin-top:0;margin-bottom:8px;">You left items in your cart</h2>
        <p style="font-size:14px;color:#4B5563;line-height:1.5;">
          We noticed you didn't finish checking out at <strong>{store_name}</strong>. Don't worry — we've held your cart so you can pick up right where you left off!
        </p>
        <div style="margin:20px 0;">
          {item_rows}
        </div>
        {'<div style="font-size:15px;font-weight:700;text-align:right;color:#111827;margin-bottom:10px;">Total: ' + formatted_total + '</div>' if formatted_total else ''}
        {cta_btn}
        <hr style="border:none;border-top:1px solid #E5E7EB;margin:24px 0 16px;">
        <p style="font-size:11px;color:#9CA3AF;margin:0;">Reply to this email to contact {store_name} directly.</p>
      </div>
    </body>
    </html>
    """
    return send_html_email(to_email, subject, html_body, text_body, reply_to=reply_to, from_name=f"{store_name} via Dripire")
    return send_html_email(to_email, subject, html_body, text_body, reply_to=reply_to, from_name=f"{store_name} via Dripire")


def send_order_cancellation_email(
    to_email: str,
    store_name: str,
    order_number: str,
    status: str = "cancelled",
    details: str = "",
    reply_to: str = None,
) -> bool:
    subject = f"Order #{order_number} Update from {store_name}"
    is_cancelled = status.lower() == "cancelled"
    badge_bg = "#FEE2E2" if is_cancelled else "#FEF3C7"
    badge_color = "#991B1B" if is_cancelled else "#92400E"
    badge_border = "#FCA5A5" if is_cancelled else "#FCD34D"
    badge_label = "Order Cancelled" if is_cancelled else "Cancellation Requested"

    text_body = (
        f"Hello,\n\n"
        f"This is an update regarding order #{order_number} at {store_name}.\n"
        f"Status: {badge_label}\n\n"
        f"{details}\n\n"
        f"If you have questions, please reply directly to this email.\n\n"
        f"Best regards,\n{store_name}"
    )

    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#F9FAFB;padding:24px;color:#1F2937;">
      <div style="max-width:520px;margin:0 auto;background:#FFFFFF;border-radius:16px;padding:32px;border:1px solid #E5E7EB;box-shadow:0 4px 20px rgba(0,0,0,0.04);">
        <div style="display:inline-block;background:{badge_bg};color:{badge_color};font-size:12px;font-weight:700;padding:4px 10px;border-radius:999px;margin-bottom:12px;border:1px solid {badge_border};">
          {badge_label}
        </div>
        <h2 style="font-size:22px;font-weight:800;color:#111827;margin-top:0;margin-bottom:8px;">Order #{order_number}</h2>
        <p style="font-size:14px;color:#4B5563;line-height:1.6;">
          {details or 'Your order has been updated as requested.'}
        </p>
        <hr style="border:none;border-top:1px solid #E5E7EB;margin:24px 0 16px;">
        <p style="font-size:11px;color:#9CA3AF;margin:0;">Reply to this email to contact {store_name} directly.</p>
      </div>
    </body>
    </html>
    """
    return send_html_email(to_email, subject, html_body, text_body, reply_to=reply_to, from_name=f"{store_name} Support via Dripire")
