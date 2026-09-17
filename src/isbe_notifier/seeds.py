"""Idempotent seed data: every subscribable race. Run: python -m isbe_notifier.seeds

The office_district_patterns are substrings matched against the B-1
"Office - District" column, after both sides are normalized (casefolded,
punctuation collapsed, padded) by matching._normalize — so a pattern written as
"chicago board of education 1b" also matches "Chicago Board of Education, 1B",
and "state representative 1" does NOT match "State Representative 11".

Filers name the same office many different ways. Confirmed 2026 CPS values:
"Chicago Board of Education 5A", "Chicago Board of Education, 1B",
"President of the Chicago Board of Education", "Chicago Board of Education
President", plus bare "Chicago School Board" in assorted casings. The 2024-era
"Chicago School Board, District 7" spelling is kept as a fallback.

Patterns for the non-CPS races are deliberately CONSERVATIVE. "Office - District"
is free text, and bare office names are ambiguous statewide: ISBE has 451 county
treasurers, 528 county clerks and 1,419 city mayors, so a bare "treasurer",
"clerk" or "mayor" pattern would match the wrong race constantly. Those races are
meant to be matched by committee via race_committees (see the ISBE bulk-data
auto-mapping), with these patterns as a narrow supplement. Under-matching here is
deliberate; over-matching sends wrong alerts to real people.

This module is the source of truth for patterns: the web service runs it on every
deploy and it overwrites the stored patterns, so edit them here, not in the DB.
"""

from sqlalchemy import select

from .db import session_scope
from .models import Race

# race_group -> signup-form section heading, sort_order base, and the phrase the
# digest uses when recapping a whole-group follow. The sort bases keep groups
# contiguous and let CPS keep its original 0-20 values, so nothing already in the
# DB shifts position.
RACE_GROUPS: dict[str, dict] = {
    "cps": {
        "heading": "Chicago Board of Education",
        "sort_base": 0,
        "recap": "every CPS Board race",
        # Rendered as a checkbox grid with a "follow all" box.
        "picker": False,
        "select_all": True,
        "select_all_label": "All CPS Board races",
    },
    "statewide": {
        "heading": "Statewide offices",
        "sort_base": 100,
        "recap": "every statewide race",
        "picker": False,
        "select_all": True,
        "select_all_label": "All statewide offices",
    },
    "chicago": {
        "heading": "Chicago citywide offices",
        "sort_base": 200,
        "recap": "every Chicago citywide race",
        "picker": False,
        "select_all": True,
        "select_all_label": "All Chicago citywide offices",
    },
    "ilsenate": {
        "heading": "Illinois Senate",
        "sort_base": 300,
        "recap": "every Illinois Senate race",
        # 59 districts: a search box rather than 59 checkboxes, but the
        # "follow all" box is offered alongside it.
        "picker": True,
        "select_all": True,
        "select_all_label": "All Illinois Senate races",
    },
    "ilhouse": {
        "heading": "Illinois House",
        "sort_base": 500,
        "recap": "every Illinois House race",
        "picker": True,
        "select_all": True,
        "select_all_label": "All Illinois House races",
    },
}

IL_HOUSE_DISTRICTS = 118
IL_SENATE_DISTRICTS = 59


def cps_races() -> list[dict]:
    """The 21 Chicago Board of Education races: president + 10 districts x a/b."""
    races = [
        {
            "slug": "president",
            "label": "CPS Board President (citywide)",
            "race_group": "cps",
            "sort_order": 0,
            "office_district_patterns": [
                "chicago board of education president",
                "president of the chicago board of education",
                "chicago school board president",
                "president of the chicago school board",
            ],
        }
    ]
    order = 1
    for n in range(1, 11):
        for half in ("a", "b"):
            races.append(
                {
                    "slug": f"d{n}{half}",
                    "label": f"CPS District {n}{half}",
                    "race_group": "cps",
                    "sort_order": order,
                    # Bare "<office> <district>" is how 2026 filers actually write
                    # it; the "district N" spellings are the 2024-era fallback.
                    "office_district_patterns": [
                        f"chicago board of education {n}{half}",
                        f"chicago board of education district {n}{half}",
                        f"chicago school board {n}{half}",
                        f"chicago school board district {n}{half}",
                    ],
                }
            )
            order += 1
    return races


def statewide_races() -> list[dict]:
    """The six Illinois constitutional officers."""
    base = RACE_GROUPS["statewide"]["sort_base"]
    # (slug, label, patterns)
    offices = [
        ("gov", "Governor", ["governor of illinois", "illinois governor", "governor"]),
        (
            "ltgov",
            "Lieutenant Governor",
            ["lieutenant governor", "lt governor", "lt gov"],
        ),
        ("ag", "Attorney General", ["attorney general"]),
        ("sos", "Secretary of State", ["secretary of state"]),
        # "comptroller" alone is near-unambiguous in Illinois; city comptrollers
        # are appointed, not elected, so they don't file candidate reports.
        ("comptroller", "Comptroller", ["comptroller"]),
        # NOT bare "treasurer": ISBE has hundreds of county/city/village treasurers.
        (
            "treasurer",
            "Treasurer",
            ["state treasurer", "illinois treasurer", "treasurer of illinois"],
        ),
    ]
    return [
        {
            "slug": f"st-{slug}",
            "label": label,
            "race_group": "statewide",
            "sort_order": base + i,
            "office_district_patterns": patterns,
        }
        for i, (slug, label, patterns) in enumerate(offices)
    ]


def chicago_races() -> list[dict]:
    """Chicago citywide offices. Every pattern names Chicago — a bare "mayor"
    or "clerk" would match any of the hundreds of other Illinois municipalities."""
    base = RACE_GROUPS["chicago"]["sort_base"]
    offices = [
        (
            "mayor",
            "Mayor of Chicago",
            ["mayor of chicago", "chicago mayor", "mayor city of chicago"],
        ),
        (
            "clerk",
            "Chicago City Clerk",
            ["chicago city clerk", "city clerk of chicago", "clerk city of chicago"],
        ),
        (
            "treasurer",
            "Chicago City Treasurer",
            [
                "chicago city treasurer",
                "city treasurer of chicago",
                "treasurer city of chicago",
            ],
        ),
    ]
    return [
        {
            "slug": f"chi-{slug}",
            "label": label,
            "race_group": "chicago",
            "sort_order": base + i,
            "office_district_patterns": patterns,
        }
        for i, (slug, label, patterns) in enumerate(offices)
    ]


def general_assembly_races() -> list[dict]:
    """Illinois House (118) and Senate (59) districts.

    Patterns carry the district number; _normalize's padding is what stops
    "state representative 1" from matching "State Representative 11".
    """
    races = []
    house_base = RACE_GROUPS["ilhouse"]["sort_base"]
    for n in range(1, IL_HOUSE_DISTRICTS + 1):
        races.append(
            {
                "slug": f"hd{n}",
                "label": f"Illinois House District {n}",
                "race_group": "ilhouse",
                "sort_order": house_base + n,
                "office_district_patterns": [
                    f"state representative {n}",
                    f"representative district {n}",
                    f"illinois house district {n}",
                    f"house district {n}",
                ],
            }
        )
    senate_base = RACE_GROUPS["ilsenate"]["sort_base"]
    for n in range(1, IL_SENATE_DISTRICTS + 1):
        races.append(
            {
                "slug": f"sd{n}",
                "label": f"Illinois Senate District {n}",
                "race_group": "ilsenate",
                "sort_order": senate_base + n,
                "office_district_patterns": [
                    f"state senator {n}",
                    f"senate district {n}",
                    f"illinois senate district {n}",
                ],
            }
        )
    return races


def all_races() -> list[dict]:
    return cps_races() + statewide_races() + chicago_races() + general_assembly_races()


def seed_races() -> int:
    created = 0
    with session_scope() as session:
        for data in all_races():
            race = session.scalars(select(Race).where(Race.slug == data["slug"])).first()
            if race is None:
                session.add(Race(**data))
                created += 1
            else:
                race.label = data["label"]
                race.sort_order = data["sort_order"]
                race.race_group = data["race_group"]
                race.office_district_patterns = data["office_district_patterns"]
    return created


if __name__ == "__main__":
    print(f"created {seed_races()} races")
