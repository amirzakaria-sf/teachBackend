from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _render_template(template_name: str, context: dict[str, str]) -> str:
    path = _TEMPLATE_DIR / template_name
    if not path.exists():
        raise FileNotFoundError(f"Email template not found: {template_name}")
    html = path.read_text(encoding="utf-8")
    for key, value in context.items():
        html = html.replace(f"{{{{{key}}}}}", value)
    return html


def _otp_digits_html(otp: str) -> str:
    digit_spans = "".join(
        f'<span style="display:inline-block;width:44px;height:52px;line-height:52px;'
        f'border:2px solid #cbd5e1;border-radius:8px;background:#f8fafc;'
        f'text-align:center;font-size:24px;font-family:monospace;'
        f'font-weight:700;color:#1e293b;margin:0 3px;">{ch}</span>'
        for ch in otp
    )
    return f'<div style="margin:24px 0;text-align:center;">{digit_spans}</div>'


def _send_email(to_email: str, subject: str, html_body: str) -> bool:
    host = getattr(settings, "BREVO_SMTP_HOST", "smtp-relay.brevo.com")
    port = int(getattr(settings, "BREVO_SMTP_PORT", "587"))
    user = getattr(settings, "BREVO_SMTP_USER", "")
    key = getattr(settings, "BREVO_SMTP_KEY", "")

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    from_address = getattr(settings, "EMAIL_FROM_ADDRESS", "") or user or "noreply@digitalclassroom.ai"
    message["From"] = from_address
    message["To"] = to_email
    message.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            if user and key:
                server.login(user, key)
            server.sendmail(from_address, [to_email], message.as_string())
        logger.info("Email sent to %s — subject: %s", to_email, subject)
        return True
    except Exception:
        logger.exception("Failed to send email to %s", to_email)
        return False


def send_verification_email(email: str, name: str, otp_code: str) -> bool:
    context = {
        "FIRST_NAME": name or email.split("@")[0],
        "EMAIL": email,
        "OTP_DIGITS_HTML": _otp_digits_html(otp_code),
    }
    html = _render_template("verification_email.html", context)
    return _send_email(email, "Verify your email — AI Digital Classroom", html)


def send_password_reset_otp(email: str, name: str, otp_code: str) -> bool:
    context = {
        "FIRST_NAME": name or email.split("@")[0],
        "EMAIL": email,
        "OTP_DIGITS_HTML": _otp_digits_html(otp_code),
    }
    html = _render_template("password_reset_email.html", context)
    return _send_email(email, "Reset your password — AI Digital Classroom", html)


def send_reassignment_notification(email: str, student_name: str, teacher_name: str, grade: str, section: str) -> bool:
    context = {
        "FIRST_NAME": student_name or email.split("@")[0],
        "TEACHER_NAME": teacher_name,
        "GRADE": grade or "N/A",
        "SECTION": section or "N/A",
    }
    html = _render_template("reassignment_email.html", context)
    return _send_email(email, "You've been enrolled in a new class — AI Digital Classroom", html)
