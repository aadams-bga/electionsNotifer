"""The polling engine: fetch feed → store new items → scrape details →
resolve committee → match subscriptions → send notifications.

Run with: python -m isbe_notifier.poller
"""

import datetime as dt
import logging
import time

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import get_settings
from .db import session_scope
from .matching import matched_race_ids, recipients_for
from .models import (
    Committee,
    FeedItem,
    Filing,
    FilingLine,
    FilingRace,
    Notification,
    PollerState,
    PushSubscription,
    utcnow,
)
from .notify.content import build_content
from .notify.emailer import send_email
from .notify.push import PushGone, send_push
from .scraper import download, pages, rss
from .scraper.client import fetch, make_client

logger = logging.getLogger(__name__)

# Be polite to ISBE: small pause between page fetches within one cycle.
FETCH_PAUSE_SECONDS = 1.0


def store_new_items(session: Session, items: list[rss.RssItem]) -> list[int]:
    """Insert feed items we haven't seen; returns their guid_seqs (oldest first).

    The feed is not strictly ordered by sequence, so each item is checked
    individually rather than against a high-water mark.
    """
    seen = set(
        session.scalars(
            select(FeedItem.guid_seq).where(FeedItem.guid_seq.in_([i.guid_seq for i in items]))
        )
    )
    new_seqs = []
    for item in sorted(items, key=lambda i: i.guid_seq):
        if item.guid_seq in seen:
            continue
        session.add(
            FeedItem(
                guid_seq=item.guid_seq,
                committee_name=item.committee_name,
                report_type=item.report_type,
                source=item.source,
                url=item.url,
                guid_url=item.guid_url,
                pub_date=item.pub_date,
            )
        )
        new_seqs.append(item.guid_seq)
    session.flush()
    return new_seqs


def resolve_committee(
    session: Session, client: httpx.Client, encrypted_id: str | None, fallback_name: str
) -> Committee | None:
    """Map an encrypted CommitteeDetail ID to the plain ISBE committee ID, cached."""
    if not encrypted_id:
        return None
    committee = session.scalars(
        select(Committee).where(Committee.encrypted_id == encrypted_id)
    ).first()
    if committee is not None:
        committee.last_seen_at = utcnow()
        return committee

    base = get_settings().isbe_base_url
    url = f"{base}/CampaignDisclosure/CommitteeDetail.aspx?ID={encrypted_id}"
    time.sleep(FETCH_PAUSE_SECONDS)
    detail = pages.parse_committee_detail(fetch(client, url).text)
    if detail is None:
        logger.warning("could not parse CommitteeDetail for %s", fallback_name)
        return None

    committee = session.get(Committee, detail.committee_id)
    if committee is None:
        committee = Committee(id=detail.committee_id, name=detail.name)
        session.add(committee)
    committee.name = detail.name or fallback_name
    committee.encrypted_id = encrypted_id
    committee.committee_type = detail.committee_type
    committee.status = detail.status
    committee.purpose = detail.purpose
    committee.last_seen_at = utcnow()
    session.flush()
    return committee


def process_feed_item(session: Session, client: httpx.Client, feed_item: FeedItem) -> Filing:
    """Scrape details (A-1/B-1) and create the Filing + lines."""
    report_class, is_amendment = rss.classify(feed_item.report_type)
    filing = Filing(
        feed_item_seq=feed_item.guid_seq,
        report_type=feed_item.report_type,
        report_class=report_class,
        is_amendment=is_amendment,
        filed_at=feed_item.pub_date,
    )
    session.add(filing)

    committee: Committee | None = None
    if feed_item.url and report_class in ("A1", "B1"):
        time.sleep(FETCH_PAUSE_SECONDS)
        html = fetch(client, feed_item.url).text
        page = pages.parse_a1_list(html) if report_class == "A1" else pages.parse_b1_list(html)
        lines = page.lines
        if page.has_more_pages:
            # Paginated list: pull the complete CSV so every line is captured.
            csv_text = download.download_list_csv(
                client, feed_item.url, html, FETCH_PAUSE_SECONDS
            )
            if csv_text is not None:
                lines = (
                    download.a1_lines_from_csv(csv_text)
                    if report_class == "A1"
                    else download.b1_lines_from_csv(csv_text)
                )
                logger.info(
                    "filing %s: downloaded all %d lines via CSV", feed_item.guid_seq, len(lines)
                )
            else:
                logger.warning(
                    "filing %s has multiple pages and the CSV download failed; "
                    "only first page captured",
                    feed_item.guid_seq,
                )
        committee = resolve_committee(
            session, client, page.committee_encrypted_id, feed_item.committee_name
        )
        for ln in lines:
            if report_class == "A1":
                session.add(
                    FilingLine(
                        filing=filing,
                        kind="contribution",
                        name=ln.contributed_by,
                        address=ln.address,
                        amount=ln.amount,
                        line_date=ln.received_date,
                        description=ln.description,
                        vendor_name=ln.vendor_name,
                        vendor_address=ln.vendor_address,
                    )
                )
            else:
                session.add(
                    FilingLine(
                        filing=filing,
                        kind="expenditure",
                        name=ln.vendor_name,
                        address=ln.vendor_address,
                        amount=ln.amount,
                        line_date=ln.expended_date,
                        purpose=ln.purpose,
                        supporting_opposing=ln.supporting_opposing,
                        candidate_name=ln.candidate_name,
                        office_district=ln.office_district,
                        vendor_name=ln.vendor_name,
                        vendor_address=ln.vendor_address,
                    )
                )
    elif feed_item.url:
        # D-1/D-2/other electronic filings still carry a committee link we can resolve
        # from the list page; cheapest reliable source is the page itself. Skip the
        # extra fetch for MVP: match by previously-cached committee name if unique.
        committee = session.scalars(
            select(Committee).where(Committee.name == feed_item.committee_name)
        ).first()
    else:
        committee = session.scalars(
            select(Committee).where(Committee.name == feed_item.committee_name)
        ).first()

    if committee is not None:
        filing.committee_id = committee.id
    session.flush()
    # Persist race matches: the landing page and digests query these instead of
    # re-running the matching logic.
    for race_id in matched_race_ids(session, filing):
        session.add(FilingRace(filing_id=filing.id, race_id=race_id))
    session.flush()
    return filing


def notify_filing(session: Session, filing: Filing) -> int:
    """Send notifications for a filing. Idempotent via the notifications unique key."""
    recipients = recipients_for(session, filing)
    if not recipients:
        return 0
    content = build_content(filing)
    sent = 0
    for rec in recipients:
        if rec.wants_email and rec.email and rec.email_verified:
            sent += _send_one(session, filing.id, rec.subscriber_id, "email", lambda r=rec: (
                send_email(r.email, content.subject, content.body_text, content.url,
                           r.subscriber_id)
            ))
        if rec.wants_push:
            pushes = session.scalars(
                select(PushSubscription).where(PushSubscription.subscriber_id == rec.subscriber_id)
            ).all()
            if pushes:
                def _push_all(pushes=pushes):
                    for p in pushes:
                        try:
                            send_push(
                                p.endpoint, p.p256dh, p.auth,
                                content.push_title, content.push_body, content.url,
                            )
                        except PushGone:
                            session.delete(p)
                sent += _send_one(session, filing.id, rec.subscriber_id, "push", _push_all)
    return sent


def _send_one(session: Session, filing_id: int, subscriber_id: int, channel: str, fn) -> int:
    """Record-then-send with idempotency: the unique constraint stops double-sends."""
    notification = Notification(
        subscriber_id=subscriber_id, filing_id=filing_id, channel=channel
    )
    try:
        # Savepoint so a duplicate (already sent before a crash/restart) only
        # rolls back this insert, not the whole filing transaction.
        with session.begin_nested():
            session.add(notification)
    except IntegrityError:
        return 0
    try:
        fn()
        notification.status = "sent"
        notification.sent_at = utcnow()
        return 1
    except Exception as exc:  # noqa: BLE001 — one bad address must not stop the rest
        logger.exception("notification %s/%s failed", channel, subscriber_id)
        notification.status = "failed"
        notification.detail = str(exc)[:1000]
        return 0


def bootstrap_if_first_run(session: Session, items: list[rss.RssItem]) -> bool:
    """On the very first poll the feed holds ~1,000 historical items. Store them as
    already-processed so we don't scrape the entire backlog at startup; notifications
    begin with the next genuinely new filing."""
    if session.scalars(select(FeedItem.guid_seq).limit(1)).first() is not None:
        return False
    logger.info("first run: marking %d historical feed items as seen", len(items))
    now = utcnow()
    for item in items:
        session.add(
            FeedItem(
                guid_seq=item.guid_seq,
                committee_name=item.committee_name,
                report_type=item.report_type,
                source=item.source,
                url=item.url,
                guid_url=item.guid_url,
                pub_date=item.pub_date,
                processed_at=now,
            )
        )
    return True


def poll_once(client: httpx.Client) -> int:
    """One poll cycle. Returns number of new feed items processed."""
    settings = get_settings()
    feed_text = fetch(client, settings.rss_url).text
    items = rss.parse_feed(feed_text, settings.isbe_base_url)

    with session_scope() as session:
        if bootstrap_if_first_run(session, items):
            return 0
        new_seqs = store_new_items(session, items)
        state = session.get(PollerState, 1) or PollerState(id=1)
        session.add(state)
        state.last_poll_at = utcnow()
        if new_seqs:
            state.max_guid_seq = max(state.max_guid_seq or 0, *new_seqs)

    for seq in new_seqs:
        # Each item in its own transaction: one bad page can't poison the batch.
        try:
            with session_scope() as session:
                feed_item = session.get(FeedItem, seq)
                filing = process_feed_item(session, client, feed_item)
                notify_filing(session, filing)
                feed_item.processed_at = utcnow()
        except Exception as exc:  # noqa: BLE001
            logger.exception("processing item %s failed", seq)
            with session_scope() as session:
                feed_item = session.get(FeedItem, seq)
                feed_item.error = str(exc)[:1000]

    with session_scope() as session:
        state = session.get(PollerState, 1)
        state.last_success_at = utcnow()
        state.consecutive_errors = 0
    return len(new_seqs)


COMMITTEE_SYNC_INTERVAL = 60 * 60 * 24
# If the poller was down at the 11pm boundary, still send the digest as long as
# we come back within this many hours (i.e. until 7am Central).
DIGEST_CATCHUP_HOURS = 8


def maybe_run_digests(last_run: dict) -> None:
    """Fire digest runs once per 11pm-Central boundary. run_digest itself is
    idempotent (digest_sends unique key), so a restart can't double-send."""
    from .notify.digest import BOUNDARY_HOUR_CENTRAL, CENTRAL, latest_boundary, run_digest

    now_ct = dt.datetime.now(CENTRAL)
    boundary = latest_boundary(now_ct)
    boundary_dt = dt.datetime.combine(
        boundary, dt.time(hour=BOUNDARY_HOUR_CENTRAL), tzinfo=CENTRAL
    )
    if now_ct - boundary_dt > dt.timedelta(hours=DIGEST_CATCHUP_HOURS):
        return  # too stale to be worth sending (also skips fresh deploys mid-day)
    if last_run.get("daily") != boundary:
        last_run["daily"] = boundary
        try:
            run_digest("daily", boundary)
        except Exception:  # noqa: BLE001 — digests must not stop polling
            logger.exception("daily digest run failed")
    if boundary.weekday() == 6 and last_run.get("weekly") != boundary:  # Sunday 11pm
        last_run["weekly"] = boundary
        try:
            run_digest("weekly", boundary)
        except Exception:  # noqa: BLE001
            logger.exception("weekly digest run failed")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = get_settings()
    client = make_client()
    logger.info("poller starting; interval %ss", settings.poll_interval_seconds)

    from .committee_sync import sync_committees

    last_committee_sync = 0.0
    last_digest_run: dict = {}
    while True:
        if time.monotonic() - last_committee_sync > COMMITTEE_SYNC_INTERVAL or (
            last_committee_sync == 0.0
        ):
            try:
                sync_committees(client)
                last_committee_sync = time.monotonic()
            except Exception:  # noqa: BLE001 — sync failure must not stop polling
                logger.exception("committee sync failed; will retry next cycle")
                last_committee_sync = time.monotonic() - COMMITTEE_SYNC_INTERVAL + 3600
        started = time.monotonic()
        try:
            n = poll_once(client)
            if n:
                logger.info("processed %d new filings", n)
        except Exception:  # noqa: BLE001 — the loop must survive anything
            logger.exception("poll cycle failed")
            try:
                with session_scope() as session:
                    state = session.get(PollerState, 1) or PollerState(id=1)
                    session.add(state)
                    state.consecutive_errors = (state.consecutive_errors or 0) + 1
            except Exception:  # noqa: BLE001
                logger.exception("could not record poller error state")
        maybe_run_digests(last_digest_run)
        elapsed = time.monotonic() - started
        time.sleep(max(5.0, settings.poll_interval_seconds - elapsed))


if __name__ == "__main__":
    main()
