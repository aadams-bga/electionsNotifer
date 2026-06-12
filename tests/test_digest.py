from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import isbe_notifier.db as db
from isbe_notifier.models import (
    Base,
    Committee,
    DigestSend,
    FeedItem,
    Filing,
    FilingLine,
    FilingRace,
    Race,
    Subscriber,
    Subscription,
)
from isbe_notifier.notify import digest


@pytest.fixture
def dbsession(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/digest.db")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    with db.session_scope() as s:
        yield s


@pytest.fixture
def sent_emails(monkeypatch):
    sent = []
    monkeypatch.setattr(
        digest, "send_email",
        lambda to, subject, body, url, sid: sent.append((to, subject, body)),
    )
    return sent


TODAY = date(2026, 6, 12)  # a Friday, used as the 11pm-Central digest boundary
# 10am Central on June 12 — inside the daily window (June 11 11pm → June 12 11pm)
FILED_UTC = datetime(2026, 6, 12, 15, 0, tzinfo=UTC)


def _seed_world(s):
    race = Race(slug="d7", label="District 7", sort_order=7, office_district_patterns=[])
    other_race = Race(slug="d8a", label="District 8a", sort_order=8,
                      office_district_patterns=[])
    committee = Committee(id=111, name="Friends of Example")
    stray = Committee(id=222, name="Unrelated Committee")
    s.add_all([race, other_race, committee, stray])
    s.flush()

    def make_filing(seq, committee_id, report_class, race_id=None, line=None):
        item = FeedItem(
            guid_seq=seq, committee_name="x", report_type=report_class,
            source="Filed electronically", url=f"https://x.test/{seq}",
            guid_url=f"https://x.test/{seq}", pub_date=FILED_UTC,
        )
        s.add(item)
        s.flush()
        filing = Filing(
            feed_item_seq=seq, committee_id=committee_id, report_type=report_class,
            report_class=report_class, created_at=FILED_UTC,
        )
        s.add(filing)
        s.flush()
        if race_id:
            s.add(FilingRace(filing_id=filing.id, race_id=race_id))
        if line:
            s.add(FilingLine(filing=filing, **line))
        s.flush()
        return filing

    # B-1 matched to District 7; A-1 from the followed committee; stray D-2.
    f_race = make_filing(1, 222, "B1", race_id=race.id, line={
        "kind": "expenditure", "name": "VENDOR CO", "vendor_name": "VENDOR CO",
        "amount": Decimal("5000"), "supporting_opposing": "Supporting",
        "candidate_name": "Jane Doe", "office_district": "Chicago School Board, District 7",
    })
    f_committee = make_filing(2, 111, "A1", line={
        "kind": "contribution", "name": "Big Donor",
        "amount": Decimal("2500"), "line_date": date(2026, 6, 11),
    })
    f_stray = make_filing(3, 222, "D2")
    return race, other_race, committee, (f_race, f_committee, f_stray)


def _subscriber(s, email, *, races=(), committees=(), all_cps=False, all_filings=False,
                daily=True, weekly=False):
    sub = Subscriber(
        email=email, email_verified_at=datetime.now(UTC),
        wants_daily_digest=daily, wants_weekly_digest=weekly,
    )
    s.add(sub)
    s.flush()
    for r in races:
        s.add(Subscription(subscriber_id=sub.id, race_id=r.id, wants_email=True))
    for c in committees:
        s.add(Subscription(subscriber_id=sub.id, committee_id=c.id, wants_email=True))
    if all_cps or all_filings:
        s.add(Subscription(subscriber_id=sub.id, all_cps=all_cps,
                           all_filings=all_filings, wants_email=True))
    s.flush()
    return sub


def test_period_for():
    assert digest.period_for("daily", TODAY) == (TODAY - timedelta(days=1), TODAY)
    # Friday boundary → the week ending the most recent Monday boundary
    start, end = digest.period_for("weekly", TODAY)
    assert start == date(2026, 6, 1) and end == date(2026, 6, 8)
    assert start.weekday() == 0 and end.weekday() == 0


def test_latest_boundary():
    central = digest.CENTRAL
    # Before 11pm → yesterday's boundary; at/after 11pm → today's
    assert digest.latest_boundary(datetime(2026, 6, 12, 22, 59, tzinfo=central)) == date(
        2026, 6, 11
    )
    assert digest.latest_boundary(datetime(2026, 6, 12, 23, 0, tzinfo=central)) == TODAY


def test_window_is_11pm_to_11pm():
    lo, hi = digest._bounds_utc(TODAY - timedelta(days=1), TODAY)
    # June 11 11pm CDT = June 12 04:00 UTC; June 12 11pm CDT = June 13 04:00 UTC
    assert lo == datetime(2026, 6, 12, 4, 0, tzinfo=UTC)
    assert hi == datetime(2026, 6, 13, 4, 0, tzinfo=UTC)


def test_daily_digest_sections_and_scopes(dbsession, sent_emails):
    race, other_race, committee, (f_race, f_committee, f_stray) = _seed_world(dbsession)
    _subscriber(dbsession, "racefan@example.org", races=[race])
    _subscriber(dbsession, "committeefan@example.org", committees=[committee])
    _subscriber(dbsession, "hose@example.org", all_filings=True)
    dbsession.commit()

    assert digest.run_digest("daily", TODAY) == 3
    by_to = {to: (subject, body) for to, subject, body in sent_emails}

    subject, body = by_to["racefan@example.org"]
    assert "Daily filing summary" in subject
    assert "District 7" in body and "Jane Doe" in body
    assert "Big Donor" not in body  # not their committee

    _, body = by_to["committeefan@example.org"]
    assert "Big Donor" in body and "Jane Doe" not in body

    _, body = by_to["hose@example.org"]
    assert "Everything else statewide" in body
    assert "Jane Doe" in body and "Big Donor" in body


def test_all_cps_digest_covers_all_races(dbsession, sent_emails):
    race, *_ = _seed_world(dbsession)
    _subscriber(dbsession, "cps@example.org", all_cps=True)
    dbsession.commit()
    assert digest.run_digest("daily", TODAY) == 1
    _, _, body = sent_emails[0]
    assert "District 7" in body and "Jane Doe" in body


def test_digest_idempotent_and_skips_empty(dbsession, sent_emails):
    race, *_ = _seed_world(dbsession)
    _subscriber(dbsession, "racefan@example.org", races=[race])
    # Subscriber to a race with no filings in the period → no email at all
    other = dbsession.scalars(select(Race).where(Race.slug == "d8a")).one()
    _subscriber(dbsession, "quiet@example.org", races=[other])
    dbsession.commit()

    assert digest.run_digest("daily", TODAY) == 1
    assert digest.run_digest("daily", TODAY) == 0  # second run sends nothing
    assert len(sent_emails) == 1
    with db.session_scope() as s:
        sends = s.scalars(select(DigestSend)).all()
        assert len(sends) == 1 and sends[0].status == "sent"
        assert sends[0].period_start == TODAY - timedelta(days=1)


def test_unverified_or_optout_excluded(dbsession, sent_emails):
    race, *_ = _seed_world(dbsession)
    _subscriber(dbsession, "optout@example.org", races=[race], daily=False)
    unverified = _subscriber(dbsession, "unverified@example.org", races=[race])
    unverified.email_verified_at = None
    dbsession.commit()
    assert digest.run_digest("daily", TODAY) == 0
    assert sent_emails == []
