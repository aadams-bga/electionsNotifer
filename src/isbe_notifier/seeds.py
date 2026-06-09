"""Idempotent seed data: the CPS Board races. Run: python -m isbe_notifier.seeds

The office_district_patterns are case-insensitive substrings matched against the
B-1 "Office - District" column. 2024 filings used "Chicago School Board, District 7";
the 2026 sub-district label format is unconfirmed, so patterns are stored in the DB
and can be adjusted from the admin page without a deploy.
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
            "office_district_patterns": ["chicago school board president"],
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
                    "office_district_patterns": [
                        f"chicago school board, district {n}{half}",
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
    return created


if __name__ == "__main__":
    print(f"created {seed_races()} races")
