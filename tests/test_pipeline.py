from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from isbe_notifier import poller
from isbe_notifier.models import (
    Base,
    Committee,
    FeedItem,
    Race,
    RaceCommittee,
    Subscriber,
    Subscription,
)
from isbe_notifier.scraper import rss

FIXTURES = Path(__file__).parent / "fixtures"

A1_URL = "https://elections.il.gov/CampaignDisclosure/A1List.aspx?ID=test1"
B1_URL = "https://elections.il.gov/CampaignDisclosure/B1List.aspx?ID=test2"
DETAIL_URL_PART = "CommitteeDetail.aspx"


class FakeResponse:
    def __init__(self, text: str):
        self.text = text


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def fake_fetch(monkeypatch):
    def _fetch(client, url, attempts=3):
        if DETAIL_URL_PART in url:
            return FakeResponse((FIXTURES / "committee_detail.html").read_text())
        if url == A1_URL:
            return FakeResponse((FIXTURES / "a1_list.html").read_text())
        if url == B1_URL:
            return FakeResponse((FIXTURES / "b1_list.html").read_text())
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(poller, "fetch", _fetch)
    monkeypatch.setattr(poller, "FETCH_PAUSE_SECONDS", 0)
    return _fetch


@pytest.fixture
def sent_emails(monkeypatch):
    sent = []
    monkeypatch.setattr(
        poller, "send_email", lambda to, subject, body, url: sent.append((to, subject, body))
    )
    return sent


def _seed_subscribers(session) -> tuple[Subscriber, Subscriber]:
    race = Race(
        slug="d7",
        label="District 7",
        office_district_patterns=["chicago school board, district 7"],
    )
    follower = Subscriber(email="follower@example.org", email_verified_at=datetime.now(UTC))
    racer = Subscriber(email="racer@example.org", email_verified_at=datetime.now(UTC))
    session.add_all([race, follower, racer])
    session.flush()
    # follower follows committee 40616 directly (added when committee is resolved);
    # racer follows the District 7 race.
    session.add(Subscription(subscriber_id=racer.id, race_id=race.id, wants_email=True))
    session.flush()
    return follower, racer


def test_store_new_items_dedupes(session):
    items = rss.parse_feed((FIXTURES / "latest_reports.xml").read_text())[:10]
    new = poller.store_new_items(session, items)
    assert len(new) == 10
    assert poller.store_new_items(session, items) == []


def test_a1_pipeline_committee_follow(session, fake_fetch, sent_emails):
    follower, _ = _seed_subscribers(session)

    feed_item = FeedItem(
        guid_seq=1,
        committee_name="Citizens for Judge Christina Kye",
        report_type="A-1 ($1000+ Year Round)",
        source="Filed electronically",
        url=A1_URL,
        guid_url=A1_URL,
        pub_date=datetime.now(UTC),
    )
    session.add(feed_item)
    session.flush()

    filing = poller.process_feed_item(session, None, feed_item)

    # Committee resolved from CommitteeDetail fixture (plain ID 40616) and cached
    committee = session.get(Committee, 40616)
    assert committee is not None
    assert filing.committee_id == 40616
    assert len(filing.lines) == 1
    assert filing.lines[0].amount == Decimal("1000.00")

    # Now subscribe the follower to that committee and notify
    session.add(Subscription(subscriber_id=follower.id, committee_id=40616, wants_email=True))
    session.flush()

    assert poller.notify_filing(session, filing) == 1
    assert len(sent_emails) == 1
    to, subject, body = sent_emails[0]
    assert to == "follower@example.org"
    assert "$1,000.00" in subject
    assert "Baumert, Aggie" in body

    # Idempotent: second run sends nothing
    assert poller.notify_filing(session, filing) == 0
    assert len(sent_emails) == 1


def test_b1_pipeline_race_match(session, fake_fetch, sent_emails):
    _, racer = _seed_subscribers(session)

    feed_item = FeedItem(
        guid_seq=2,
        committee_name="INCS Action Independent Committee",
        report_type="B-1 ($1000+ Year Round)",
        source="Filed electronically",
        url=B1_URL,
        guid_url=B1_URL,
        pub_date=datetime.now(UTC),
    )
    session.add(feed_item)
    session.flush()

    filing = poller.process_feed_item(session, None, feed_item)
    assert len(filing.lines) == 4
    districts = {ln.office_district for ln in filing.lines}
    assert "Chicago School Board, District 7" in districts

    # The filing committee is NOT followed and not in any whitelist —
    # the race subscriber still gets notified via Office-District matching.
    assert poller.notify_filing(session, filing) == 1
    to, subject, body = sent_emails[0]
    assert to == "racer@example.org"
    assert "independent expenditures" in subject
    assert "Eva Villalobos" in body
    assert "Chicago School Board, District 7" in body


def test_d2_pipeline_whitelist(session, fake_fetch, sent_emails):
    follower, _ = _seed_subscribers(session)
    committee = Committee(id=99999, name="Friends of Example")
    session.add(committee)
    session.flush()
    session.add(Subscription(subscriber_id=follower.id, committee_id=99999, wants_email=True))

    feed_item = FeedItem(
        guid_seq=3,
        committee_name="Friends of Example",
        report_type="D-2 Quarterly Report",
        source="Filed electronically",
        url="https://elections.il.gov/CampaignDisclosure/D2Quarterly.aspx?ID=x",
        guid_url="https://elections.il.gov/x",
        pub_date=datetime.now(UTC),
    )
    session.add(feed_item)
    session.flush()

    filing = poller.process_feed_item(session, None, feed_item)
    assert filing.report_class == "D2"
    assert filing.committee_id == 99999  # matched by cached committee name
    assert poller.notify_filing(session, filing) == 1
    _, subject, _ = sent_emails[0]
    assert "Quarterly Report" in subject


def test_unverified_subscriber_gets_no_email(session, fake_fetch, sent_emails):
    race = Race(
        slug="d7", label="District 7",
        office_district_patterns=["chicago school board, district 7"],
    )
    unverified = Subscriber(email="unverified@example.org", email_verified_at=None)
    session.add_all([race, unverified])
    session.flush()
    session.add(Subscription(subscriber_id=unverified.id, race_id=race.id, wants_email=True))

    feed_item = FeedItem(
        guid_seq=4, committee_name="INCS Action Independent Committee",
        report_type="B-1 ($1000+ Year Round)", source="Filed electronically",
        url=B1_URL, guid_url=B1_URL, pub_date=datetime.now(UTC),
    )
    session.add(feed_item)
    session.flush()
    filing = poller.process_feed_item(session, None, feed_item)
    assert poller.notify_filing(session, filing) == 0
    assert sent_emails == []


def test_race_committee_whitelist_matches_any_report_type(session, fake_fetch, sent_emails):
    race = Race(slug="president", label="Board President", office_district_patterns=[])
    committee = Committee(id=12345, name="Friends for President")
    racer = Subscriber(email="racer@example.org", email_verified_at=datetime.now(UTC))
    session.add_all([race, committee, racer])
    session.flush()
    session.add(RaceCommittee(race_id=race.id, committee_id=committee.id))
    session.add(Subscription(subscriber_id=racer.id, race_id=race.id, wants_email=True))

    feed_item = FeedItem(
        guid_seq=5, committee_name="Friends for President",
        report_type="D-1 Statement of Organization", source="Filed on paper",
        url=None, guid_url="https://elections.il.gov/pdf", pub_date=datetime.now(UTC),
    )
    session.add(feed_item)
    session.flush()
    filing = poller.process_feed_item(session, None, feed_item)
    assert filing.committee_id == 12345
    assert poller.notify_filing(session, filing) == 1
    _, subject, _ = sent_emails[0]
    assert "Statement of Organization" in subject
