"""B-1 "Office - District" race matching.

The sample strings below are the real distinct values in production as of
2026-09-16, including the casing chaos and the filer's "Chciago" typo.
"""

from collections import Counter
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from isbe_notifier.matching import _line_matches_race, _normalize, recipients_for
from isbe_notifier.models import (
    Base,
    FeedItem,
    Filing,
    FilingLine,
    Race,
    Subscriber,
    Subscription,
)
from isbe_notifier.seeds import RACE_GROUPS, all_races

RACES = [Race(**data) for data in all_races()]


def matching_slugs(office_district: str | None) -> set[str]:
    return {r.slug for r in RACES if _line_matches_race(office_district, r)}


@pytest.mark.parametrize(
    "office_district,expected",
    [
        # 2026 format: bare "<office> <district>", the one that was never matching.
        ("Chicago Board of Education 5A", {"d5a"}),
        ("Chicago Board of Education 10B", {"d10b"}),
        ("Chicago Board of Education 2B", {"d2b"}),
        ("Chicago Board of Education 3B", {"d3b"}),
        ("Chicago Board of Education 6A", {"d6a"}),
        ("Chicago Board of Education 1B", {"d1b"}),
        ("Chicago Board of Education 4A", {"d4a"}),
        # Comma variant — only matches because both sides get normalized.
        ("Chicago Board of Education, 1B", {"d1b"}),
        # Both president spellings seen in the wild.
        ("President of the Chicago Board of Education", {"president"}),
        ("Chicago Board of Education President", {"president"}),
        # 2024-era spelling, kept as a fallback.
        ("Chicago School Board, District 7a", {"d7a"}),
        ("chicago school board district 10b", {"d10b"}),
        # Statewide offices now have races of their own.
        ("Secretary of State", {"st-sos"}),
        # Offices with no race, or too ambiguous to attribute, match nothing.
        ("Will County Board", set()),
        ("Will County Board Candidate", set()),
        ("County Clerk", set()),
        ("Sheriff", set()),
        ("State Representative", set()),  # bare, no district — unattributable
        ("ALL", set()),
        (None, set()),
        ("", set()),
    ],
)
def test_real_office_district_values(office_district, expected):
    assert matching_slugs(office_district) == expected


@pytest.mark.parametrize(
    "office_district",
    [
        "Chicago School Board",
        "CHICAGO SCHOOL BOARD",
        "Chicago School BOARD",
        "ChiCAGO SCHOOL BOARD",
        "Chciago School Board",  # filer's typo
    ],
)
def test_board_wide_values_match_no_specific_district(office_district):
    """Bare board-wide references name no district, so they match no race.

    These are real independent-expenditure lines that currently reach nobody;
    attributing them needs a CPS-wide concept rather than a per-race pattern.
    """
    assert matching_slugs(office_district) == set()


def test_single_digit_district_does_not_match_double_digit():
    """"...Education 1B" must not match district 10b, and vice versa."""
    assert matching_slugs("Chicago Board of Education 1B") == {"d1b"}
    assert matching_slugs("Chicago Board of Education 10B") == {"d10b"}
    assert matching_slugs("Chicago Board of Education 1A") == {"d1a"}
    assert matching_slugs("Chicago Board of Education 10A") == {"d10a"}


def test_casing_and_punctuation_are_ignored():
    for variant in (
        "CHICAGO BOARD OF EDUCATION 5A",
        "chicago  board   of education   5a",
        "Chicago Board of Education - 5A",
        "Chicago Board of Education, 5A.",
    ):
        assert matching_slugs(variant) == {"d5a"}


def test_empty_pattern_never_matches():
    """Patterns live in the DB, so a blank one must not match every filing."""
    race = Race(slug="x", label="X", office_district_patterns=["", "   "])
    assert _line_matches_race("Chicago Board of Education 5A", race) is False


def test_normalize():
    assert _normalize("Chicago Board of Education, 1B") == "chicago board of education 1b"
    assert _normalize("  CHICAGO   SCHOOL BOARD  ") == "chicago school board"
    assert _normalize("") == ""


def test_ambiguous_bare_offices_match_nothing():
    """Statewide, a bare office name says nothing about which race it is.

    ISBE lists 451 county treasurers, 528 county clerks and 1,419 city mayors, so
    these patterns are deliberately narrow — the committee whitelist, not the
    B-1 text, is what attributes these races.
    """
    for office in ("Treasurer", "County Treasurer", "City Treasurer", "Clerk",
                   "County Clerk", "Circuit Clerk", "Mayor", "Village Clerk"):
        assert matching_slugs(office) == set(), office


def test_lieutenant_governor_also_matches_governor():
    """Known, accepted overlap: "governor" is a substring of "lieutenant
    governor", and in Illinois the two run as a joint ticket, so a Lt. Gov
    expenditure is genuinely relevant to Governor followers."""
    assert matching_slugs("Governor") == {"st-gov"}
    assert matching_slugs("Lieutenant Governor") == {"st-gov", "st-ltgov"}


def test_legislative_district_numbers_do_not_bleed():
    """Padding is what stops "state representative 1" matching district 11-118."""
    assert matching_slugs("State Representative 1") == {"hd1"}
    assert matching_slugs("State Representative 11") == {"hd11"}
    assert matching_slugs("State Representative 118") == {"hd118"}
    assert matching_slugs("State Senator 5") == {"sd5"}
    assert matching_slugs("Senate District 59") == {"sd59"}


def test_seed_data_shape():
    all_ = all_races()
    assert len(all_) == 207
    assert len({r["slug"] for r in all_}) == len(all_), "slugs must be unique"
    assert len({r["sort_order"] for r in all_}) == len(all_), "sort_order must be unique"
    assert {r["race_group"] for r in all_} == set(RACE_GROUPS)
    # CPS keeps its original positions so nothing already in the DB shifts.
    assert sorted(r["sort_order"] for r in all_ if r["race_group"] == "cps") == list(range(21))
    assert Counter(r["race_group"] for r in all_) == {
        "cps": 21, "statewide": 6, "chicago": 3, "ilsenate": 59, "ilhouse": 118,
    }


# --- group containment: the landmine that adding non-CPS races creates ---


@pytest.fixture
def seeded():
    """An in-memory DB holding every race, as seeds would create them."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for data in all_races():
            session.add(Race(**data))
        session.flush()
        yield session


def _subscriber(session, email, **subscription_kwargs):
    sub = Subscriber(email=email, email_verified_at=datetime.now(UTC))
    session.add(sub)
    session.flush()
    session.add(Subscription(subscriber_id=sub.id, wants_email=True, **subscription_kwargs))
    session.flush()
    return sub


def recipients(session, office_district):
    """Who a B-1 naming this office would notify."""
    seq = session.scalar(select(func.count()).select_from(FeedItem)) + 1
    session.add(FeedItem(
        guid_seq=seq, committee_name="Some IE Committee", report_type="B-1",
        source="Filed electronically", url="http://x", guid_url="http://x",
    ))
    session.flush()
    filing = Filing(feed_item_seq=seq, report_type="B-1", report_class="B1")
    session.add(filing)
    session.flush()
    session.add(FilingLine(
        filing_id=filing.id, kind="expenditure", name="Ad buy",
        office_district=office_district,
    ))
    session.flush()
    session.refresh(filing)
    return {r.email for r in recipients_for(session, filing)}


def test_all_cps_subscriber_is_not_notified_about_other_groups(seeded):
    """The whole reason race groups exist: adding a governor race must not
    silently start mailing every existing "All CPS Board races" subscriber."""
    _subscriber(seeded, "cps@example.org", all_cps=True)

    assert recipients(seeded, "Governor") == set()
    assert recipients(seeded, "Attorney General") == set()
    assert recipients(seeded, "State Representative 12") == set()
    assert recipients(seeded, "Mayor of Chicago") == set()


def test_all_cps_subscriber_still_gets_cps_races(seeded):
    _subscriber(seeded, "cps@example.org", all_cps=True)

    assert recipients(seeded, "Chicago Board of Education 5A") == {"cps@example.org"}
    assert recipients(seeded, "Chicago Board of Education President") == {"cps@example.org"}


def test_group_subscriber_gets_only_its_own_group(seeded):
    _subscriber(seeded, "state@example.org", all_group="statewide")
    _subscriber(seeded, "cps@example.org", all_cps=True)

    assert recipients(seeded, "Governor") == {"state@example.org"}
    assert recipients(seeded, "Chicago Board of Education 5A") == {"cps@example.org"}
    assert recipients(seeded, "State Representative 12") == set()


def test_individually_followed_race_is_unaffected_by_groups(seeded):
    gov = seeded.scalars(select(Race).where(Race.slug == "st-gov")).one()
    _subscriber(seeded, "govfan@example.org", race_id=gov.id)

    assert recipients(seeded, "Governor") == {"govfan@example.org"}
    assert recipients(seeded, "Chicago Board of Education 5A") == set()


def test_firehose_still_matches_everything(seeded):
    _subscriber(seeded, "hose@example.org", all_filings=True)

    assert recipients(seeded, "Governor") == {"hose@example.org"}
    assert recipients(seeded, "Chicago Board of Education 5A") == {"hose@example.org"}
    assert recipients(seeded, "Will County Board") == {"hose@example.org"}


def test_all_house_group_follow_matches_any_house_district(seeded):
    """"All Illinois House races" must catch every district, and nothing else."""
    _subscriber(seeded, "house@example.org", all_group="ilhouse")

    assert recipients(seeded, "State Representative 1") == {"house@example.org"}
    assert recipients(seeded, "State Representative 118") == {"house@example.org"}
    assert recipients(seeded, "State Senator 7") == set()
    assert recipients(seeded, "Governor") == set()
    assert recipients(seeded, "Chicago Board of Education 5A") == set()
