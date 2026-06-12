"""Daily and weekly summary emails: a collated version of the period's notifications.

One combined email per subscriber per cadence, sectioned:
1. CPS races they follow (every race if all_cps), via the stored filing_races matches.
2. Committees they follow directly.
3. For firehose subscribers, everything else statewide that didn't already appear.

Opt-in via Subscriber.wants_daily_digest / wants_weekly_digest; requires a verified
email. Idempotent per (subscriber, kind, period_start) through the digest_sends
unique constraint, so a crashed run can be retried safely.

Manual run / preview:
    python -m isbe_notifier.notify.digest --kind daily [--date YYYY-MM-DD] [--dry-run]
"""

import argparse
import logging
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..db import session_scope
from ..models import DigestSend, Filing, FilingRace, Race, Subscriber, utcnow
from .content import filing_total, line_rows
from .emailer import send_email

logger = logging.getLogger(__name__)

CENTRAL = ZoneInfo("America/Chicago")

# Digests go out at 11pm Central, each covering the 24h (or 7 days) ending at
# that boundary — so the daily digest summarizes the day that is just ending.
BOUNDARY_HOUR_CENTRAL = 23


def latest_boundary(now_ct: datetime) -> date:
    """The date of the most recent 11pm-Central boundary that has passed."""
    d = now_ct.date()
    return d if now_ct.hour >= BOUNDARY_HOUR_CENTRAL else d - timedelta(days=1)


def period_for(kind: str, boundary: date) -> tuple[date, date]:
    """(start, end) boundary dates; the covered window is start@11pm → end@11pm
    Central. Daily ends at `boundary`; weekly covers the week ending the most
    recent Sunday boundary."""
    if kind == "daily":
        return boundary - timedelta(days=1), boundary
    if kind == "weekly":
        last_sunday = boundary - timedelta(days=(boundary.weekday() + 1) % 7)
        return last_sunday - timedelta(days=7), last_sunday
    raise ValueError(f"unknown digest kind: {kind}")


def _bounds_utc(start: date, end: date) -> tuple[datetime, datetime]:
    boundary = time(hour=BOUNDARY_HOUR_CENTRAL)
    return (
        datetime.combine(start, boundary, tzinfo=CENTRAL).astimezone(UTC),
        datetime.combine(end, boundary, tzinfo=CENTRAL).astimezone(UTC),
    )


def _filings_in_period(session: Session, start: date, end: date) -> list[Filing]:
    lo, hi = _bounds_utc(start, end)
    return list(
        session.scalars(
            select(Filing)
            .where(Filing.created_at >= lo, Filing.created_at < hi)
            .order_by(Filing.created_at)
            .options(
                selectinload(Filing.lines),
                selectinload(Filing.feed_item),
                selectinload(Filing.committee),
            )
        )
    )


def _render_filing(filing: Filing) -> str:
    feed_item = filing.feed_item
    committee = filing.committee.name if filing.committee else feed_item.committee_name
    amendment = " (amendment)" if filing.is_amendment else ""
    head = f"• {committee} — {filing.report_type}{amendment}"
    if filing.report_class in ("A1", "B1") and filing.lines:
        n = len(filing.lines)
        noun = "contribution" if filing.report_class == "A1" else "expenditure"
        head += f": {n} {noun}{'s' if n != 1 else ''} totaling ${filing_total(filing):,.2f}"
    parts = [head]
    parts.extend(line_rows(filing))
    parts.append(f"  {feed_item.url or feed_item.guid_url}")
    return "\n".join(parts)


def build_digest(
    session: Session, subscriber: Subscriber, filings: list[Filing], kind: str,
    start: date, end: date,
) -> tuple[str, str] | None:
    """Returns (subject, body), or None when nothing relevant happened."""
    flags = {
        "all_filings": any(s.all_filings for s in subscriber.subscriptions),
        "all_cps": any(s.all_cps for s in subscriber.subscriptions),
    }
    followed_race_ids = {s.race_id for s in subscriber.subscriptions if s.race_id}
    followed_committee_ids = {s.committee_id for s in subscriber.subscriptions if s.committee_id}

    race_matches: dict[int, set[int]] = {}  # race_id -> filing ids
    filing_ids = [f.id for f in filings]
    if filing_ids:
        for fr in session.scalars(
            select(FilingRace).where(FilingRace.filing_id.in_(filing_ids))
        ):
            race_matches.setdefault(fr.race_id, set()).add(fr.filing_id)

    races = {
        r.id: r for r in session.scalars(select(Race).order_by(Race.sort_order))
    }
    digest_race_ids = (
        list(races) if flags["all_cps"]
        else [rid for rid in races if rid in followed_race_ids]
    )

    by_id = {f.id: f for f in filings}
    sections: list[str] = []
    covered: set[int] = set()

    for rid in digest_race_ids:
        matched = sorted(race_matches.get(rid, ()), key=lambda fid: by_id[fid].created_at)
        if not matched:
            continue
        body = "\n\n".join(_render_filing(by_id[fid]) for fid in matched)
        sections.append(f"## {races[rid].label}\n\n{body}")
        covered.update(matched)

    committee_filings = [
        f for f in filings
        if f.committee_id in followed_committee_ids and f.id not in covered
    ]
    if committee_filings:
        body = "\n\n".join(_render_filing(f) for f in committee_filings)
        sections.append(f"## Committees you follow\n\n{body}")
        covered.update(f.id for f in committee_filings)

    if flags["all_filings"]:
        rest = [f for f in filings if f.id not in covered]
        if rest:
            body = "\n\n".join(_render_filing(f) for f in rest)
            sections.append(f"## Everything else statewide ({len(rest)} filings)\n\n{body}")

    if not sections:
        return None

    label = "Daily" if kind == "daily" else "Weekly"
    # The window ends at 11pm on `end`, so that's the day the digest is "for".
    when = (
        end.strftime("%B %-d, %Y") if kind == "daily"
        else f"the week ending {end.strftime('%B %-d, %Y')}"
    )
    subject = f"{label} filing summary — {when}"
    intro = (
        f"Here's your {kind} summary of Illinois campaign finance filings "
        f"for {when}.\n\n"
    )
    return subject, intro + "\n\n".join(sections)


def run_digest(kind: str, boundary: date | None = None, dry_run: bool = False) -> int:
    """Build and send digests to every opted-in subscriber. Returns emails sent.

    `boundary` is the 11pm-Central boundary the period ends at (default: the
    most recent one that has passed)."""
    boundary = boundary or latest_boundary(datetime.now(CENTRAL))
    start, end = period_for(kind, boundary)
    pref = (
        Subscriber.wants_daily_digest if kind == "daily" else Subscriber.wants_weekly_digest
    )
    sent = 0
    with session_scope() as session:
        filings = _filings_in_period(session, start, end)
        subscribers = session.scalars(
            select(Subscriber)
            .where(pref.is_(True), Subscriber.email.is_not(None),
                   Subscriber.email_verified_at.is_not(None))
            .options(selectinload(Subscriber.subscriptions))
        ).all()
        logger.info(
            "%s digest for %s → %s: %d filings, %d opted-in subscribers",
            kind, start, end, len(filings), len(subscribers),
        )
        for subscriber in subscribers:
            built = build_digest(session, subscriber, filings, kind, start, end)
            if built is None:
                continue
            subject, body = built
            if dry_run:
                print(f"\n=== {subscriber.email} ===\n{subject}\n\n{body}\n")
                continue
            record = DigestSend(
                subscriber_id=subscriber.id, kind=kind, period_start=start
            )
            try:
                with session.begin_nested():
                    session.add(record)
            except IntegrityError:
                continue  # already sent for this period
            try:
                send_email(subscriber.email, subject, body, None, subscriber.id)
                record.status = "sent"
                record.sent_at = utcnow()
                sent += 1
            except Exception as exc:  # noqa: BLE001 — one bad address must not stop the rest
                logger.exception("digest email to subscriber %s failed", subscriber.id)
                record.status = "failed"
                record.detail = str(exc)[:1000]
    return sent


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("daily", "weekly"), required=True)
    parser.add_argument(
        "--date", type=date.fromisoformat, default=None,
        help="11pm-Central boundary the period ends at (default: most recent)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    args = parser.parse_args()
    get_settings()  # fail fast on bad config
    n = run_digest(args.kind, args.date, args.dry_run)
    print(f"sent {n} {args.kind} digests")


if __name__ == "__main__":
    main()
