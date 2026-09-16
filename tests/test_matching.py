"""B-1 "Office - District" race matching.

The sample strings below are the real distinct values in production as of
2026-09-16, including the casing chaos and the filer's "Chciago" typo.
"""

import pytest

from isbe_notifier.matching import _line_matches_race, _normalize
from isbe_notifier.models import Race
from isbe_notifier.seeds import cps_races

RACES = [Race(**data) for data in cps_races()]


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
        # Other races' filings must not match CPS.
        ("Will County Board", set()),
        ("Will County Board Candidate", set()),
        ("County Clerk", set()),
        ("Secretary of State", set()),
        ("Sheriff", set()),
        ("State Representative", set()),
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
