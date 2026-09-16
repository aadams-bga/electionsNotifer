"""Idempotent seed data: the CPS Board races. Run: python -m isbe_notifier.seeds

The office_district_patterns are substrings matched against the B-1
"Office - District" column, after both sides are normalized (casefolded,
punctuation collapsed) by matching._normalize — so a pattern written as
"chicago board of education 1b" also matches "Chicago Board of Education, 1B".

Filers name the same office many different ways. Confirmed 2026 values:
"Chicago Board of Education 5A", "Chicago Board of Education, 1B",
"President of the Chicago Board of Education", "Chicago Board of Education
President", plus bare "Chicago School Board" in assorted casings. The 2024-era
"Chicago School Board, District 7" spelling is kept as a fallback.

This module is the source of truth for patterns: the web service runs it on every
deploy and it overwrites the stored patterns, so edit them here, not in the DB.
"""

from sqlalchemy import select

from .db import session_scope
from .models import Race


def cps_races() -> list[dict]:
    races = [
        {
            "slug": "president",
            "label": "CPS Board President (citywide)",
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
                    "label": f"District {n}{half}",
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


def seed_races() -> int:
    created = 0
    with session_scope() as session:
        for data in cps_races():
            race = session.scalars(select(Race).where(Race.slug == data["slug"])).first()
            if race is None:
                session.add(Race(**data))
                created += 1
            else:
                race.label = data["label"]
                race.sort_order = data["sort_order"]
                race.office_district_patterns = data["office_district_patterns"]
    return created


if __name__ == "__main__":
    print(f"created {seed_races()} races")
