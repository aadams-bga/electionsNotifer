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
        lambda to, subject, body, url, sid, body_html=None:
            sent.append((to, subject, body, body_html)),
    )
    return sent


TODAY = date(2026, 6, 12)  # a Friday, used as the midnight-Central digest boundary
# 10am Central on June 11 — inside the daily window (June 11 midnight → June 12 midnight)
FILED_UTC = datetime(2026, 6, 11, 15, 0, tzinfo=UTC)


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
    # Friday boundary → the Monday-to-Monday week ending the most recent
    # Monday-midnight boundary
    start, end = digest.period_for("weekly", TODAY)
    assert start == date(2026, 6, 1) and end == date(2026, 6, 8)
    assert start.weekday() == 0 and end.weekday() == 0
    # Fired exactly on the Monday boundary (Sunday night) → the week just ended
    monday = date(2026, 6, 15)
    assert digest.period_for("weekly", monday) == (date(2026, 6, 8), monday)


def test_latest_boundary():
    central = digest.CENTRAL
    # Today's midnight has always passed → the boundary is always today's date
    assert digest.latest_boundary(datetime(2026, 6, 12, 0, 0, tzinfo=central)) == TODAY
    assert digest.latest_boundary(datetime(2026, 6, 12, 22, 59, tzinfo=central)) == TODAY


def test_window_is_midnight_to_midnight():
    lo, hi = digest._bounds_utc(TODAY - timedelta(days=1), TODAY)
    # June 11 midnight CDT = June 11 05:00 UTC; June 12 midnight CDT = June 12 05:00 UTC
    assert lo == datetime(2026, 6, 11, 5, 0, tzinfo=UTC)
    assert hi == datetime(2026, 6, 12, 5, 0, tzinfo=UTC)


def test_daily_digest_sections_and_scopes(dbsession, sent_emails):
    race, other_race, committee, (f_race, f_committee, f_stray) = _seed_world(dbsession)
    _subscriber(dbsession, "racefan@example.org", races=[race])
    _subscriber(dbsession, "committeefan@example.org", committees=[committee])
    _subscriber(dbsession, "hose@example.org", all_filings=True)
    dbsession.commit()

    assert digest.run_digest("daily", TODAY) == 3
    by_to = {to: (subject, body, html) for to, subject, body, html in sent_emails}

    subject, body, html = by_to["racefan@example.org"]
    assert "Daily filing summary" in subject
    assert "District 7" in body and "Jane Doe" in body
    assert "Big Donor" not in body  # not their committee
    assert "You're following District 7." in body
    assert "<strong>" in html and "Jane Doe" in html

    _, body, _ = by_to["committeefan@example.org"]
    assert "Big Donor" in body and "Jane Doe" not in body
    assert "You're following Friends of Example." in body

    _, body, _ = by_to["hose@example.org"]
    assert "Everything else statewide" in body
    assert "Jane Doe" in body and "Big Donor" in body
    assert "every campaign finance filing statewide" in body


def test_all_cps_digest_covers_all_races(dbsession, sent_emails):
    race, *_ = _seed_world(dbsession)
    _subscriber(dbsession, "cps@example.org", all_cps=True)
    dbsession.commit()
    assert digest.run_digest("daily", TODAY) == 1
    _, _, body, _ = sent_emails[0]
    assert "District 7" in body and "Jane Doe" in body


def test_digest_idempotent_and_nothing_to_report(dbsession, sent_emails):
    race, *_ = _seed_world(dbsession)
    _subscriber(dbsession, "racefan@example.org", races=[race])
    # Subscriber to a race with no filings → gets a "nothing to report" email
    other = dbsession.scalars(select(Race).where(Race.slug == "d8a")).one()
    _subscriber(dbsession, "quiet@example.org", races=[other])
    dbsession.commit()

    assert digest.run_digest("daily", TODAY) == 2  # both subscribers get an email
    assert digest.run_digest("daily", TODAY) == 0  # second run sends nothing (idempotent)
    assert len(sent_emails) == 2

    by_to = {to: body for to, _, body, _ in sent_emails}
    assert "No new filings" in by_to["quiet@example.org"]
    assert "You're following District 8a." in by_to["quiet@example.org"]
    assert "•" not in by_to["quiet@example.org"]  # hierarchy without bullets

    with db.session_scope() as s:
        sends = s.scalars(select(DigestSend)).all()
        assert len(sends) == 2
        assert all(send.status == "sent" for send in sends)
        assert all(send.period_start == TODAY - timedelta(days=1) for send in sends)


def test_digest_committee_grouping_and_ordering(dbsession, sent_emails):
    """Within a race: committees alphabetical; within a committee D-1 → D-2 → A-1
    → B-1, A-1s largest first; committee header carries the A-1 money total."""
    race, *_ = _seed_world(dbsession)
    zebra = Committee(id=333, name="Zebra PAC")
    alpha = Committee(id=444, name="Alpha Fund")
    dbsession.add_all([zebra, alpha])
    dbsession.flush()

    def make_filing(seq, committee_id, report_class, report_type=None, line=None):
        item = FeedItem(
            guid_seq=seq, committee_name="x", report_type=report_type or report_class,
            source="Filed electronically", url=f"https://x.test/{seq}",
            guid_url=f"https://x.test/{seq}", pub_date=FILED_UTC,
        )
        dbsession.add(item)
        dbsession.flush()
        filing = Filing(
            feed_item_seq=seq, committee_id=committee_id,
            report_type=report_type or report_class,
            report_class=report_class, created_at=FILED_UTC,
        )
        dbsession.add(filing)
        dbsession.flush()
        dbsession.add(FilingRace(filing_id=filing.id, race_id=race.id))
        if line:
            dbsession.add(FilingLine(filing=filing, **line))
        dbsession.flush()
        return filing

    # Zebra: small A-1, big A-1, and a D-1 — expect D-1 first, then A-1s by size.
    make_filing(10, 333, "A1", line={
        "kind": "contribution", "name": "Small Donor", "amount": Decimal("1000")})
    make_filing(11, 333, "A1", line={
        "kind": "contribution", "name": "Huge Donor", "amount": Decimal("50000")})
    make_filing(12, 333, "D1", report_type="Statement of Organization")
    make_filing(13, 444, "D2", report_type="Quarterly Report")

    _subscriber(dbsession, "order@example.org", races=[race])
    dbsession.commit()

    assert digest.run_digest("daily", TODAY) == 1
    _, _, body, html = sent_emails[0]

    # Committees alphabetical: Alpha Fund before Zebra PAC
    assert body.index("Alpha Fund") < body.index("Zebra PAC")
    # Committee header shows the A-1 sum
    assert "Zebra PAC — $51,000.00 in major donations" in body
    # D-1 before either A-1; A-1s in decreasing order of size
    zebra_at = body.index("Zebra PAC")
    assert body.index("Statement of Organization", zebra_at) \
        < body.index("Huge Donor") < body.index("Small Donor")
    assert "Zebra PAC — $51,000.00 in major donations" in html


def test_send_email_html_multipart(monkeypatch):
    from isbe_notifier.notify import emailer

    captured = {}
    monkeypatch.setattr(emailer, "get_email_backend",
                        lambda: type("B", (), {"send": lambda self, m: captured.update(msg=m)})())
    emailer.send_email("x@example.org", "Subj", "text body", None, 1,
                       body_html="<p><strong>bold</strong></p>")
    msg = captured["msg"]
    assert msg.get_content_type() == "multipart/alternative"
    text = msg.get_body(preferencelist=("plain",)).get_content()
    html = msg.get_body(preferencelist=("html",)).get_content()
    assert "text body" in text and "Unsubscribe" in text
    assert "<strong>bold</strong>" in html and "Unsubscribe" in html
    assert "Questions? Email aadams@bettergov.org" in text
    assert 'mailto:aadams@bettergov.org' in html


def test_send_email_without_html_gets_generated_part(monkeypatch):
    """Emails composed as plain text (real-time alerts, sign-in) still get an
    HTML alternative with clickable footer links and linkified body URLs."""
    from isbe_notifier.notify import emailer

    captured = {}
    monkeypatch.setattr(emailer, "get_email_backend",
                        lambda: type("B", (), {"send": lambda self, m: captured.update(msg=m)})())
    emailer.send_email(
        "x@example.org", "Subj",
        "Use this link:\n\nhttps://example.test/manage?token=a&b=c",
        "https://example.test/filing/1", 1,
    )
    msg = captured["msg"]
    assert msg.get_content_type() == "multipart/alternative"
    html = msg.get_body(preferencelist=("html",)).get_content()
    assert '<a href="https://example.test/manage?token=a&amp;b=c">' in html
    assert ">Unsubscribe from all alerts</a>" in html
    assert '<a href="https://example.test/filing/1">View the filing</a>' in html
    text = msg.get_body(preferencelist=("plain",)).get_content()
    assert "View the filing: https://example.test/filing/1" in text


def test_send_admin_email(monkeypatch):
    from isbe_notifier.notify import emailer

    sent = []
    monkeypatch.setattr(emailer, "get_email_backend",
                        lambda: type("B", (), {"send": lambda self, m: sent.append(m)})())
    # No-op when ADMIN_EMAIL is unset.
    emailer.send_admin_email("Subj", "body")
    assert sent == []

    monkeypatch.setattr(emailer.get_settings(), "admin_email", "admin@example.org")
    emailer.send_admin_email("New signup: x@example.org", "the body")
    (msg,) = sent
    assert msg["To"] == "admin@example.org"
    assert msg["List-Unsubscribe"] is None  # not a subscriber email; no footer/headers
    assert "the body" in msg.get_content()


def test_unverified_or_optout_excluded(dbsession, sent_emails):
    race, *_ = _seed_world(dbsession)
    _subscriber(dbsession, "optout@example.org", races=[race], daily=False)
    unverified = _subscriber(dbsession, "unverified@example.org", races=[race])
    unverified.email_verified_at = None
    dbsession.commit()
    assert digest.run_digest("daily", TODAY) == 0
    assert sent_emails == []
