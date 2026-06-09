"""Signed, expiring tokens for verify / manage / unsubscribe links. No passwords.

Tokens carry the subscriber ID (not the email) so they keep working if the
subscriber has no email (push-only) and never leak the address in URLs.
"""

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..config import get_settings

VERIFY_MAX_AGE = 60 * 60 * 48  # 48h to click the verification link
MANAGE_MAX_AGE = 60 * 60 * 24 * 90  # manage links last 90 days
# Unsubscribe links must keep working indefinitely (they live in old emails).
UNSUBSCRIBE_MAX_AGE = None


def _serializer(purpose: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt=f"isbe-notifier:{purpose}")


def make_token(subscriber_id: int, purpose: str) -> str:
    return _serializer(purpose).dumps(subscriber_id)


def read_token(token: str, purpose: str, max_age: int | None) -> int | None:
    """Returns the subscriber ID, or None if invalid/expired."""
    try:
        value = _serializer(purpose).loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    return int(value) if isinstance(value, int) else None


def _url(path: str, subscriber_id: int, purpose: str) -> str:
    base = get_settings().base_url.rstrip("/")
    return f"{base}/{path}?token={make_token(subscriber_id, purpose)}"


def verify_url(subscriber_id: int) -> str:
    return _url("verify", subscriber_id, "verify")


def manage_url(subscriber_id: int) -> str:
    return _url("manage", subscriber_id, "manage")


def unsubscribe_url(subscriber_id: int) -> str:
    return _url("unsubscribe", subscriber_id, "unsubscribe")
