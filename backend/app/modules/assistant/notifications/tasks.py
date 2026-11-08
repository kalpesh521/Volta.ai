"""
Celery delivery for critical alert email.

Run from backend/ after the API is up and EMAIL_PROVIDER is configured:

    celery -A app.modules.assistant.notifications.tasks:celery_app worker --loglevel=info -Q suryaa.notifications
"""
from __future__ import annotations

from celery import Celery

from app.core.config import settings
from app.modules.assistant.notifications.email import (
    PermanentEmailError,
    RecoverableEmailError,
    deliver_email,
)

celery_app = Celery(
    "suryaa",
    broker=(settings.CELERY_BROKER_URL or "").strip() or settings.RABBITMQ_URL,
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    task_acks_late=True,
    task_default_queue="suryaa.notifications",
    broker_connection_retry_on_startup=True,
    broker_connection_timeout=5,
)


@celery_app.task(
    bind=True,
    name="suryaa.send_alert_email",
    max_retries=3,
    default_retry_delay=30,
)
def send_alert_email(self, to_email: str, subject: str, text: str) -> str:
    try:
        return deliver_email(to_email, subject, text)
    except RecoverableEmailError as exc:
        raise self.retry(exc=exc)
    except PermanentEmailError:
        return "failed"
