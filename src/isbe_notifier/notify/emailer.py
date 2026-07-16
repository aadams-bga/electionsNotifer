"""Email backends: "console" (dev — logs instead of sending) and "ses" (Amazon SES).

Every email is multipart (text + HTML) and carries List-Unsubscribe headers plus
a footer with manage/unsubscribe links and a contact address.
"""

import html as html_mod
import logging
import re
from email.message import EmailMessage

from ..config import get_settings
from .tokens import manage_url, unsubscribe_url

logger = logging.getLogger(__name__)

CONTACT_EMAIL = "aadams@bettergov.org"

_URL_RE = re.compile(r"https?://[^\s<>\"']+")


def _text_to_html(text: str) -> str:
    """Plain text → simple HTML: escaped, paragraphs on blank lines, bare URLs
    linked. Used for emails composed as text (real-time alerts, sign-in,
    verification) so their HTML part isn't a wall of raw URLs."""
    paragraphs = []
    for block in text.split("\n\n"):
        escaped = html_mod.escape(block)
        linked = _URL_RE.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', escaped)
        paragraphs.append(f'<p style="margin:0 0 1em">{linked.replace(chr(10), "<br>")}</p>')
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;color:#111111;'
        'line-height:1.5;max-width:640px">' + "\n".join(paragraphs) + "</div>"
    )


def _build_message(
    to_email: str, subject: str, body_text: str, link_url: str | None, subscriber_id: int,
    body_html: str | None = None,
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
        f"Questions? Email {CONTACT_EMAIL}\n"
        f"{settings.site_name}"
    )
    msg.set_content(body_text + footer)

    html_footer = '<hr style="border:none;border-top:1px solid #dce1e9;margin:1.5em 0">'
    html_footer += '<p style="color:#4f5566;font-size:0.85em">'
    if link_url:
        html_footer += (
            f'<a href="{html_mod.escape(link_url, quote=True)}">View the filing</a><br>'
        )
    html_footer += (
        f'<a href="{html_mod.escape(manage, quote=True)}">Manage your alerts</a> · '
        f'<a href="{html_mod.escape(unsub, quote=True)}">Unsubscribe from all alerts</a><br>'
        f'Questions? Email <a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a><br>'
        f"{html_mod.escape(settings.site_name)}</p>"
    )
    html_body = body_html if body_html is not None else _text_to_html(body_text)
    msg.add_alternative(html_body + html_footer, subtype="html")
    return msg


class ConsoleEmailBackend:
    def send(self, msg: EmailMessage) -> None:
        # Decoded plain-text body (not raw MIME) so links can be copied from logs.
        text_part = msg.get_body(preferencelist=("plain",))
        logger.info(
            "EMAIL (console backend)\nTo: %s\nSubject: %s\n%s",
            msg["To"], msg["Subject"],
            text_part.get_content() if text_part else "(no text part)",
        )


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


def send_admin_email(subject: str, body_text: str) -> None:
    """Plain-text notification to the site operator (ADMIN_EMAIL).

    No-op when ADMIN_EMAIL is unset; failures are logged, never raised —
    admin mail must not break a signup or unsubscribe.
    """
    settings = get_settings()
    if not settings.admin_email:
        return
    msg = EmailMessage()
    msg["From"] = settings.email_from
    msg["To"] = settings.admin_email
    msg["Subject"] = subject
    msg.set_content(body_text)
    try:
        get_email_backend().send(msg)
    except Exception:
        logger.exception("admin notification failed: %s", subject)


def send_email(
    to_email: str,
    subject: str,
    body_text: str,
    link_url: str | None,
    subscriber_id: int,
    body_html: str | None = None,
) -> None:
    msg = _build_message(to_email, subject, body_text, link_url, subscriber_id, body_html)
    get_email_backend().send(msg)
