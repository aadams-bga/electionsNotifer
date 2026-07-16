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
import html as html_mod
import logging
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..db import session_scope
from ..models import DigestSend, Filing, FilingRace, Race, Subscriber, Subscription, utcnow
from .content import _date, _money, filing_total
from .emailer import send_email

logger = logging.getLogger(__name__)

CENTRAL = ZoneInfo("America/Chicago")

# Digests go out at midnight Central, each covering the 24h (or 7 days) ending
# at that boundary — so the daily digest summarizes the calendar day that just
# ended. A boundary date's moment is 00:00 Central at the START of that date.
BOUNDARY_HOUR_CENTRAL = 0


def latest_boundary(now_ct: datetime) -> date:
    """The date of the most recent midnight-Central boundary that has passed
    (i.e. today — today's midnight is always behind us)."""
    d = now_ct.date()
    return d if now_ct.hour >= BOUNDARY_HOUR_CENTRAL else d - timedelta(days=1)


def period_for(kind: str, boundary: date) -> tuple[date, date]:
    """(start, end) boundary dates; the covered window is start@midnight →
    end@midnight Central. Daily ends at `boundary` and covers the previous
    calendar day; weekly covers the Monday-through-Sunday week ending at the
    most recent Monday-midnight boundary."""
    if kind == "daily":
        return boundary - timedelta(days=1), boundary
    if kind == "weekly":
        last_monday = boundary - timedelta(days=boundary.weekday())
        return last_monday - timedelta(days=7), last_monday
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


# Within a committee: organizational filings first, then money filings by size.
_CLASS_ORDER = {"D1": 0, "D2": 1, "A1": 2, "B1": 3}

_esc = html_mod.escape


def _committee_name(filing: Filing) -> str:
    return filing.committee.name if filing.committee else filing.feed_item.committee_name


def _filing_sort_key(filing: Filing):
    order = _CLASS_ORDER.get(filing.report_class, 4)
    total = filing_total(filing) if filing.report_class in ("A1", "B1") else 0
    return (order, -total, filing.created_at)


def _filing_lines(filing: Filing) -> list[str]:
    """Plain lines describing one filing; the last line is always the ISBE link.
    Hierarchy comes from bolding (HTML) and newlines — no bullets, no indents."""
    amendment = " (amendment)" if filing.is_amendment else ""
    lines: list[str] = []
    if filing.report_class == "A1" and filing.lines:
        n = len(filing.lines)
        lines.append(
            f"A-1{amendment}: {n} major contribution{'s' if n != 1 else ''} "
            f"totaling {_money(filing_total(filing))}"
        )
        for ln in filing.lines:
            row = f"{_money(ln.amount)} from {ln.name}"
            if ln.line_date:
                row += f", received {_date(ln.line_date)}"
            if ln.description:
                row += f" ({ln.description})"
            lines.append(row)
    elif filing.report_class == "B1" and filing.lines:
        n = len(filing.lines)
        lines.append(
            f"B-1{amendment}: {n} independent expenditure{'s' if n != 1 else ''} "
            f"totaling {_money(filing_total(filing))}"
        )
        for ln in filing.lines:
            parts = [f"{_money(ln.amount)} to {ln.vendor_name}"]
            if ln.line_date:
                parts[0] += f" on {_date(ln.line_date)}"
            if ln.purpose:
                parts.append(f"purpose: {ln.purpose}")
            if ln.supporting_opposing and ln.candidate_name:
                parts.append(f"{ln.supporting_opposing.lower()} {ln.candidate_name}")
            if ln.office_district:
                parts.append(f"({ln.office_district})")
            lines.append(" — ".join(parts))
    else:
        lines.append(f"{filing.report_type}{amendment} filed")
    lines.append(filing.feed_item.url or filing.feed_item.guid_url)
    return lines


def _render_committee(name: str, filings: list[Filing]) -> tuple[str, str]:
    """One committee's block: bold header (with the period's A-1 money when any),
    then its filings D-1 → D-2 → A-1 → B-1 → rest, money filings largest first.
    Returns (text, html)."""
    ordered = sorted(filings, key=_filing_sort_key)
    a1_total = sum(
        (filing_total(f) for f in ordered if f.report_class == "A1"), start=0
    )
    header = f"{name} — {_money(a1_total)} in major donations" if a1_total else name

    text_blocks = [header]
    html_parts = [
        f'<p style="margin:1.25em 0 0.25em"><strong>{_esc(header)}</strong></p>'
    ]
    for filing in ordered:
        lines = _filing_lines(filing)
        text_blocks.append("\n".join(lines))
        url = lines[-1]
        rows = "<br>\n".join(_esc(line) for line in lines[:-1])
        html_parts.append(
            f'<p style="margin:0 0 0.9em">{rows}<br>\n'
            f'<a href="{_esc(url, quote=True)}">View the filing</a></p>'
        )
    return "\n\n".join(text_blocks), "\n".join(html_parts)


def _render_section(heading: str, filings: list[Filing]) -> tuple[str, str]:
    """A digest section (a race, followed committees, or the statewide rest):
    heading, then each committee alphabetically. Returns (text, html)."""
    groups: dict[str, list[Filing]] = {}
    for filing in filings:
        groups.setdefault(_committee_name(filing), []).append(filing)
    text_parts = [heading]
    html_parts = [
        f'<h2 style="font-size:1.05em;color:#003282;margin:1.75em 0 0.25em">'
        f"{_esc(heading)}</h2>"
    ]
    for name in sorted(groups, key=str.casefold):
        text, html = _render_committee(name, groups[name])
        text_parts.append(text)
        html_parts.append(html)
    return "\n\n".join(text_parts), "\n".join(html_parts)


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " and " + items[-1]


def build_digest(
    session: Session, subscriber: Subscriber, filings: list[Filing], kind: str,
    start: date, end: date,
) -> tuple[str, str, str]:
    """Returns (subject, text_body, html_body). Always sends something — even
    "nothing to report"."""
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
    sections: list[tuple[str, str]] = []  # (text, html)
    covered: set[int] = set()

    for rid in digest_race_ids:
        matched = race_matches.get(rid, set())
        if not matched:
            continue
        sections.append(_render_section(races[rid].label, [by_id[fid] for fid in matched]))
        covered.update(matched)

    committee_filings = [
        f for f in filings
        if f.committee_id in followed_committee_ids and f.id not in covered
    ]
    if committee_filings:
        sections.append(_render_section("Committees you follow", committee_filings))
        covered.update(f.id for f in committee_filings)

    if flags["all_filings"]:
        rest = [f for f in filings if f.id not in covered]
        if rest:
            heading = f"Everything else statewide ({len(rest)} filings)"
            sections.append(_render_section(heading, rest))

    label = "Daily" if kind == "daily" else "Weekly"
    # The window ends at midnight starting `end`, so the digest is "for" the
    # calendar day (or week) that just ended.
    when = (
        start.strftime("%B %-d, %Y") if kind == "daily"
        else f"the week ending {(end - timedelta(days=1)).strftime('%B %-d, %Y')}"
    )
    subject = f"{label} filing summary — {when}"

    # Recap what the subscriber follows right in the intro sentence.
    if flags["all_filings"]:
        recap = "every campaign finance filing statewide — all races and committees"
    else:
        followed = []
        if flags["all_cps"]:
            followed.append("every CPS Board race")
        else:
            followed.extend(races[rid].label for rid in digest_race_ids)
        followed.extend(sorted(
            (s.committee.name for s in subscriber.subscriptions
             if s.committee_id and s.committee),
            key=str.casefold,
        ))
        recap = _join(followed)
    intro = (
        f"Here's your {kind} summary of Illinois campaign finance filings for {when}."
    )
    if recap:
        intro += f" You're following {recap}."

    if sections:
        text_body = intro + "\n\n\n" + "\n\n\n".join(t for t, _ in sections)
    else:
        text_body = intro + "\n\nNo new filings were submitted during this period."

    html_parts = [f'<p style="margin:0 0 1em">{_esc(intro)}</p>']
    if sections:
        html_parts.extend(h for _, h in sections)
    else:
        html_parts.append(
            '<p style="margin:0 0 1em">No new filings were submitted during '
            "this period.</p>"
        )
    html_body = (
        '<div style="font-family:Arial,Helvetica,sans-serif;color:#111111;'
        'line-height:1.5;max-width:640px">' + "\n".join(html_parts) + "</div>"
    )
    return subject, text_body, html_body


def run_digest(kind: str, boundary: date | None = None, dry_run: bool = False) -> int:
    """Build and send digests to every opted-in subscriber. Returns emails sent.

    `boundary` is the midnight-Central boundary the period ends at (default:
    the most recent one that has passed)."""
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
            .options(
                selectinload(Subscriber.subscriptions).selectinload(Subscription.committee)
            )
        ).all()
        logger.info(
            "%s digest for %s → %s: %d filings, %d opted-in subscribers",
            kind, start, end, len(filings), len(subscribers),
        )
        for subscriber in subscribers:
            subject, body, body_html = build_digest(
                session, subscriber, filings, kind, start, end
            )
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
                send_email(
                    subscriber.email, subject, body, None, subscriber.id,
                    body_html=body_html,
                )
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
        help="midnight-Central boundary the period ends at (default: most recent)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    args = parser.parse_args()
    get_settings()  # fail fast on bad config
    n = run_digest(args.kind, args.date, args.dry_run)
    print(f"sent {n} {args.kind} digests")


if __name__ == "__main__":
    main()
