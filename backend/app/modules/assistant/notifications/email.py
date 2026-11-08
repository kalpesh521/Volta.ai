"""Critical-alert email via Resend or SendGrid. No send happens without a provider key."""
from __future__ import annotations

import re

import httpx

from app.core.config import settings

_EMAIL = re.compile(r"[\w.+\-]+@[\w.\-]+\.[A-Za-z]{2,}")


class RecoverableEmailError(Exception):
    """Retryable provider or network failure."""


class PermanentEmailError(Exception):
    """The provider rejected the message. Do not retry."""


def resolved_provider() -> str:
    explicit = (settings.EMAIL_PROVIDER or "").strip().lower()
    if explicit == "resend" and settings.RESEND_API_KEY.strip():
        return "resend"
    if explicit == "sendgrid" and settings.SENDGRID_API_KEY.strip():
        return "sendgrid"
    if explicit:
        return ""
    if settings.RESEND_API_KEY.strip():
        return "resend"
    if settings.SENDGRID_API_KEY.strip():
        return "sendgrid"
    return ""


def email_configured() -> bool:
    return resolved_provider() != ""


def _from_address() -> str:
    raw = (settings.EMAIL_FROM or "").strip()
    match = _EMAIL.search(raw)
    return match.group(0) if match else raw


def deliver_email(to_email: str, subject: str, text: str) -> str:
    provider = resolved_provider()
    if not provider:
        return "skipped"
    try:
        if provider == "resend":
            _send_resend(to_email, subject, text)
        else:
            _send_sendgrid(to_email, subject, text)
    except httpx.TransportError as exc:
        raise RecoverableEmailError(str(exc)) from exc
    return "sent"


def _send_resend(to_email: str, subject: str, text: str) -> None:
    response = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {settings.RESEND_API_KEY.strip()}"},
        json={
            "from": settings.EMAIL_FROM.strip() or _from_address(),
            "to": [to_email],
            "subject": subject,
            "text": text,
        },
        timeout=10,
    )
    _raise_for_status(response)


def _send_sendgrid(to_email: str, subject: str, text: str) -> None:
    response = httpx.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {settings.SENDGRID_API_KEY.strip()}"},
        json={
            "personalizations": [{"to": [{"email": to_email}]}],
            "from": {"email": _from_address()},
            "subject": subject,
            "content": [{"type": "text/plain", "value": text}],
        },
        timeout=10,
    )
    _raise_for_status(response)


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 300:
        return
    detail = response.text[:300]
    if response.status_code in {408, 429} or response.status_code >= 500:
        raise RecoverableEmailError(f"{response.status_code} {detail}")
    raise PermanentEmailError(f"{response.status_code} {detail}")
