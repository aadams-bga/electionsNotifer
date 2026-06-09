"""Email backends: "console" (dev — logs instead of sending) and "ses" (Amazon SES).

Every email carries List-Unsubscribe headers and a footer unsubscribe link.
"""

import logging
from email.message import EmailMessage

from ..config import get_settings
from .tokens import manage_url, unsubscribe_url

logger = logging.getLogger(__name__)


def _build_message(
    to_email: str, subject: str, body_text: str, link_url: str | None, subscriber_id: int
) -> EmailMessage:
    settings = get_settings()
    unsub = unsubscribe_url(subscriber_id)
    manage = manage_url(subscriber_id)

    msg = EmailMessage()
    msg["From"] = settings.email_from
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["List-Unsubscribe"] = f"<{unsub}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    footer = "\n\n—\n"
    if link_url:
        footer += f"View the filing: {link_url}\n"
    footer += (
        f"Manage your alerts: {manage}\n"
        f"Unsubscribe from all alerts: {unsub}\n"
        f"{settings.site_name}"
    )
    msg.set_content(body_text + footer)
    return msg


class ConsoleEmailBackend:
    def send(self, msg: EmailMessage) -> None:
        logger.info("EMAIL (console backend)\n%s", msg.as_string())


class SesEmailBackend:
    def __init__(self) -> None:
        import boto3

        self._client = boto3.client("ses", region_name=get_settings().aws_region)

    def send(self, msg: EmailMessage) -> None:
        self._client.send_raw_email(
            Source=get_settings().email_from,
            Destinations=[msg["To"]],
            RawMessage={"Data": msg.as_bytes()},
        )


_backend = None


def get_email_backend():
    global _backend
    if _backend is None:
        name = get_settings().email_backend
        _backend = SesEmailBackend() if name == "ses" else ConsoleEmailBackend()
    return _backend


def send_email(
    to_email: str,
    subject: str,
    body_text: str,
    link_url: str | None,
    subscriber_id: int,
) -> None:
    msg = _build_message(to_email, subject, body_text, link_url, subscriber_id)
    get_email_backend().send(msg)
