import logging
import time

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


def make_client() -> httpx.Client:
    settings = get_settings()
    return httpx.Client(
        headers={
            # ISBE returns 403 to non-browser user agents.
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=30.0,
        follow_redirects=True,
    )


def fetch(client: httpx.Client, url: str, attempts: int = 3) -> httpx.Response:
    """GET with simple exponential backoff. Raises on final failure."""
    delay = 2.0
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = client.get(url)
            if resp.status_code in RETRY_STATUSES:
                raise httpx.HTTPStatusError(
                    f"retryable status {resp.status_code}", request=resp.request, response=resp
                )
            resp.raise_for_status()
            return resp
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            if attempt < attempts - 1:
                logger.warning("fetch failed (%s), retrying in %.0fs: %s", url, delay, exc)
                time.sleep(delay)
                delay *= 2
    raise last_exc  # type: ignore[misc]
