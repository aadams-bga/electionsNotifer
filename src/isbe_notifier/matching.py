"""Decides which subscriptions a filing should notify.

Two ways a filing matches:
1. Committee follow — the subscription's committee is the filing committee
   (covers the CPS whitelist via race_committees, and arbitrary follows).
2. Race office-district match (B-1 only) — any expenditure line's
   "Office - District" value contains one of the race's configured patterns,
   case-insensitively, regardless of which committee filed the report.
"""

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from .models import Filing, Race, RaceCommittee, Subscription


def _line_matches_race(office_district: str | None, race: Race) -> bool:
    if not office_district:
        return False
    hay = office_district.casefold()
    return any(p.casefold() in hay for p in race.office_district_patterns or [])


def matched_race_ids(session: Session, filing: Filing) -> set[int]:
    race_ids: set[int] = set()

    if filing.committee_id is not None:
        race_ids.update(
            session.scalars(
                select(RaceCommittee.race_id).where(
                    RaceCommittee.committee_id == filing.committee_id
                )
            )
        )

    if filing.report_class == "B1":
        races = session.scalars(select(Race)).all()
        for race in races:
            if race.id in race_ids:
                continue
            if any(_line_matches_race(ln.office_district, race) for ln in filing.lines):
                race_ids.add(race.id)

    return race_ids


@dataclass
class MatchedRecipient:
    subscriber_id: int
    email: str
    email_verified: bool
    wants_email: bool
    wants_push: bool


def recipients_for(session: Session, filing: Filing) -> list[MatchedRecipient]:
    """All recipients who should hear about this filing, one entry per subscriber.

    A subscriber matching through several subscriptions (e.g. follows the committee
    AND a race it spends in) gets one entry with channels merged.
    """
    race_ids = matched_race_ids(session, filing)

    clauses = []
    if filing.committee_id is not None:
        clauses.append(Subscription.committee_id == filing.committee_id)
    if race_ids:
        clauses.append(Subscription.race_id.in_(race_ids))
    if not clauses:
        return []

    query = (
        select(Subscription)
        .options(joinedload(Subscription.subscriber))
        .where(or_(*clauses))
    )
    by_subscriber: dict[int, MatchedRecipient] = {}
    for sub in session.scalars(query).unique():
        rec = by_subscriber.get(sub.subscriber_id)
        if rec is None:
            by_subscriber[sub.subscriber_id] = MatchedRecipient(
                subscriber_id=sub.subscriber_id,
                email=sub.subscriber.email,
                email_verified=sub.subscriber.email_verified_at is not None,
                wants_email=sub.wants_email,
                wants_push=sub.wants_push,
            )
        else:
            rec.wants_email = rec.wants_email or sub.wants_email
            rec.wants_push = rec.wants_push or sub.wants_push
    return list(by_subscriber.values())
