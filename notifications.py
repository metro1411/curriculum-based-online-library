"""In-app and email notification delivery.

Email delivery uses standard SMTP settings so the application can work with a
verified institutional mail server or a transactional provider without
hardcoding credentials.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from flask import current_app, has_request_context, request

from extensions import db
from models import Notification, StudentPreference, utcnow, utcnow_naive


def _email_allowed(user, *, optional):
    if not user or not user.email:
        return False, "no_address"
    if optional and user.is_student:
        preference = StudentPreference.query.filter_by(student_id=user.id).first()
        if preference and not preference.optional_emails:
            return False, "unsubscribed"
    return True, None


def _send_email(user, title, body, target_url=None):
    host = current_app.config.get("SMTP_HOST")
    sender = current_app.config.get("MAIL_FROM")
    if not host or not sender:
        return "not_configured", "SMTP delivery is not configured."

    message = EmailMessage()
    message["Subject"] = title
    message["From"] = sender
    message["To"] = user.email
    safe_body = body
    if target_url:
        if target_url.startswith(("https://", "http://")):
            absolute_url = target_url
        else:
            base_url = current_app.config.get("PUBLIC_BASE_URL")
            if not base_url and has_request_context():
                base_url = request.url_root.rstrip("/")
            absolute_url = f"{base_url}{target_url}" if base_url else None
        safe_body += "\n\nSign in to Smart DIT Learning Hub to view the update."
        if absolute_url:
            safe_body += f"\n{absolute_url}"
    message.set_content(safe_body)

    port = int(current_app.config.get("SMTP_PORT") or 587)
    timeout = int(current_app.config.get("SMTP_TIMEOUT_SECONDS") or 10)
    username = current_app.config.get("SMTP_USERNAME")
    password = current_app.config.get("SMTP_PASSWORD")
    use_ssl = bool(current_app.config.get("SMTP_USE_SSL"))
    use_tls = bool(current_app.config.get("SMTP_USE_TLS"))
    smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_class(host, port, timeout=timeout) as client:
        if use_tls and not use_ssl:
            client.starttls()
        if username:
            client.login(username, password or "")
        client.send_message(message)
    return "sent", None


def notify(user, kind, title, body, *, target_url=None, optional_email=False, deliver_email=True):
    """Create an in-app notification and attempt the corresponding email.

    ``deliver_email=False`` records an in-app notification only, for
    high-volume messages where the sender did not ask for email.
    """
    notification = Notification(
        user_id=user.id,
        kind=kind,
        title=title[:180],
        body=body[:500],
        target_url=(target_url or "")[:500] or None,
    )
    db.session.add(notification)
    db.session.flush()

    if not deliver_email:
        notification.email_status = "in_app_only"
        return notification

    allowed, reason = _email_allowed(user, optional=optional_email)
    if not allowed:
        notification.email_status = reason
        return notification

    notification.email_attempted_at = utcnow()
    try:
        notification.email_status, notification.email_error = _send_email(
            user, notification.title, notification.body, notification.target_url
        )
    except (OSError, smtplib.SMTPException) as exc:
        current_app.logger.warning("Notification email delivery failed: %s", exc)
        notification.email_status = "failed"
        notification.email_error = str(exc)[:300]
    return notification


def reminder_is_due(preference, now=None):
    """Return whether a student's selected reminder cadence is enabled."""
    now = now or utcnow_naive()
    if not preference or not preference.optional_emails or not preference.goal_reminders:
        return False
    if preference.reminder_frequency == "off":
        return False
    if preference.reminder_frequency == "weekly":
        return now.weekday() == 0
    return preference.reminder_frequency == "daily"
