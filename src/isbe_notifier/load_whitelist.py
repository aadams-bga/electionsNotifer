"""Loads the CPS race ↔ committee whitelist from a CSV.

CSV format (header required):
    race_slug,committee_id
    president,12345
    d4a,40616

Usage: python -m isbe_notifier.load_whitelist whitelist.csv
Idempotent; rows already present are skipped. Unknown committee IDs are
created as name-pending stubs and filled in by the next committee sync.
"""

import csv
import sys

from sqlalchemy import select

from .db import session_scope
from .models import Committee, Race, RaceCommittee


def load(path: str) -> int:
    created = 0
    with open(path, newline="") as f, session_scope() as session:
        races = {r.slug: r.id for r in session.scalars(select(Race))}
        for row in csv.DictReader(f):
            slug = row["race_slug"].strip()
            committee_id = int(row["committee_id"])
            if slug not in races:
                raise SystemExit(f"unknown race slug: {slug!r}")
            if session.get(Committee, committee_id) is None:
                session.add(Committee(id=committee_id, name=f"Committee #{committee_id}"))
                session.flush()
            exists = session.scalars(
                select(RaceCommittee).where(
                    RaceCommittee.race_id == races[slug],
                    RaceCommittee.committee_id == committee_id,
                )
            ).first()
            if exists is None:
                session.add(RaceCommittee(race_id=races[slug], committee_id=committee_id))
                created += 1
    return created


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(f"added {load(sys.argv[1])} whitelist entries")
