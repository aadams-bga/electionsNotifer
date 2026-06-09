"""Web push via VAPID (pywebpush). Run ``python -m isbe_notifier.notify.push genkeys``
to generate the VAPID keypair for .env / Railway variables."""

import json
import logging

from pywebpush import WebPushException, webpush

from ..config import get_settings

logger = logging.getLogger(__name__)


class PushGone(Exception):
    """The subscription is dead (410/404) and should be removed."""


def send_push(endpoint: str, p256dh: str, auth: str, title: str, body: str, url: str) -> None:
    settings = get_settings()
    if not settings.vapid_private_key:
        logger.info("PUSH (no VAPID key configured): %s — %s", title, body)
        return
    try:
        webpush(
            subscription_info={
                "endpoint": endpoint,
                "keys": {"p256dh": p256dh, "auth": auth},
            },
            data=json.dumps({"title": title, "body": body, "url": url}),
            vapid_private_key=settings.vapid_private_key,
            vapid_claims={"sub": settings.vapid_claims_email},
            ttl=3600,
        )
    except WebPushException as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in (404, 410):
            raise PushGone from exc
        raise


def _genkeys() -> None:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from py_vapid import Vapid02, b64urlencode

    v = Vapid02()
    v.generate_keys()
    raw_private = v.private_key.private_numbers().private_value.to_bytes(32, "big")
    raw_public = v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    print("VAPID_PRIVATE_KEY=" + b64urlencode(raw_private))
    print("VAPID_PUBLIC_KEY=" + b64urlencode(raw_public))


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "genkeys":
        _genkeys()
