"""Signed, expiring tokens for verify / manage / unsubscribe links. No passwords."""

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..config import get_settings

VERIFY_MAX_AGE = 60 * 60 * 48  # 48h to click the verification link
MANAGE_MAX_AGE = 60 * 60 * 24 * 30  # manage links last 30 days
# Unsubscribe links must keep working indefinitely (they live in old emails).
UNSUBSCRIBE_MAX_AGE = None


def _serializer(purpose: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt=f"isbe-notifier:{purpose}")


def make_token(email: str, purpose: str) -> str:
    return _serializer(purpose).dumps(email.lower())


def read_token(token: str, purpose: str, max_age: int | None) -> str | None:
    try:
        return _serializer(purpose).loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


def verify_url(email: str) -> str:
    base = get_settings().base_url.rstrip("/")
    return f"{base}/verify?token={make_token(email, 'verify')}"


def manage_url(email: str) -> str:
    base = get_settings().base_url.rstrip("/")
    return f"{base}/manage?token={make_token(email, 'manage')}"


def unsubscribe_url(email: str) -> str:
    base = get_settings().base_url.rstrip("/")
    return f"{base}/unsubscribe?token={make_token(email, 'unsubscribe')}"
