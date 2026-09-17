"""Maps candidate committees to races from ISBE's bulk data files.

Hand-curating a committee whitelist is fine for 21 CPS races; it doesn't scale to
118 Illinois House districts. ISBE publishes the link we need across three files:

    CmteCandidateLinks.txt   CommitteeID <-> CandidateID
    Candidates.txt           CandidateID -> Office / DistrictType / District
    CanElections.txt         CandidateID -> ElectionYear

Joining them gives committee -> race directly, so a filing by any candidate
committee is attributed without anyone maintaining a list.

Two deliberate limits:

* CPS is excluded. Its candidates share the generic Office="Member",
  DistrictType="School" shape with every other Illinois school district, so
  there's no reliable way to tell a Chicago Board of Education candidate from a
  downstate one. CPS keeps using the curated committeeWhitelist.csv.
* Only candidates with an election in MIN_ELECTION_YEAR or later are mapped.
  Committees outlive candidacies and district numbers get redrawn — ISBE still
  lists Senate districts up to 113 from earlier maps — so without this filter a
  committee would be attributed to a district it last ran in decades ago.

The sync runs itself: the poller refreshes links from ISBE daily, so new
candidates are picked up without anyone doing anything.

raceCommitteeOverrides.csv is the editorial escape hatch on top of that — an
exceptions list, not a copy of every link. Only rows you actually want to
override belong in it:

    no    never link this committee to this race; remove it if already linked
    yes   always link them, even when ISBE doesn't propose it

Anything absent from the file is simply whatever ISBE says. A blocked link stays
blocked no matter how many times the sync runs, which is the whole point — a
rejection that the next sync undid would be worthless.

    python -m isbe_notifier.race_mapping review    # what ISBE proposes (--suspect, --group)
    python -m isbe_notifier.race_mapping sync      # apply it (--dry-run to preview)

Links are only ever added, never removed, except where an override says `no`.
ISBE's load balancer serves truncated and stale copies, so pruning on the basis
of one download would erase good links. CPS is not covered here at all — it keeps
the curated committeeWhitelist.csv.
"""

import argparse
import csv
import io
import logging
import re
from pathlib import Path

import httpx
from sqlalchemy import select

from .db import session_scope
from .models import Committee, Race, RaceCommittee
from .scraper.client import fetch, make_client

logger = logging.getLogger(__name__)

BASE = "https://elections.il.gov/CampaignDisclosureDataFiles"

# Editorial overrides, checked into the repo next to committeeWhitelist.csv.
# Exceptions only — everything not listed follows ISBE.
OVERRIDES_CSV = Path(__file__).resolve().parents[2] / "raceCommitteeOverrides.csv"

OVERRIDE_COLUMNS = [
    "include", "race_slug", "committee_id", "committee_name", "notes",
]

# Candidates whose most recent election is older than this are ignored.
MIN_ELECTION_YEAR = 2026

# DistrictType="Statewide", keyed by lowercased Office.
STATEWIDE_OFFICES = {
    "governor": "st-gov",
    "lieutenant governor": "st-ltgov",
    "attorney general": "st-ag",
    "secretary of state": "st-sos",
    "comptroller": "st-comptroller",
    "treasurer": "st-treasurer",
}

# DistrictType="City" and District="Chicago", keyed by lowercased Office.
CHICAGO_OFFICES = {
    "mayor": "chi-mayor",
    "clerk": "chi-clerk",
    "treasurer": "chi-treasurer",
}

IL_HOUSE_DISTRICTS = 118
IL_SENATE_DISTRICTS = 59


# Smallest row count we'll trust per file, well under the real sizes (roughly
# 32.5k / 66.7k / 36.8k as of 2026-09). See _load for why this guard exists.
MIN_ROWS = {
    "Candidates.txt": 25_000,
    "CanElections.txt": 50_000,
    "CmteCandidateLinks.txt": 30_000,
}


def _load(client: httpx.Client, filename: str) -> list[dict]:
    """Fetch one tab-delimited ISBE bulk file.

    ISBE's load balancer makes short reads undetectable and repeat fetches
    inconsistent, so this is defensive on purpose:

    * It returns the Content-Length header with its name scrambled
      ("cteonnt-length"), an F5 BIG-IP tell. No client can length-check the body,
      so a truncated download parses fine and silently loses the last rows.
    * Its origins serve different file versions — two requests seconds apart
      returned Last-Modified values a day apart — so row counts vary run to run.

    A short file therefore can't be distinguished from a genuinely smaller one,
    and the row floor below is the backstop. Callers must only ever ADD rows from
    this data: deleting on the basis of one download would erase good mappings
    every time a stale or truncated copy came back.
    """
    text = fetch(client, f"{BASE}/{filename}").text
    rows = list(csv.DictReader(io.StringIO(text), delimiter="\t"))
    floor = MIN_ROWS.get(filename)
    if floor is not None and len(rows) < floor:
        raise RuntimeError(
            f"{filename} came back with {len(rows)} rows, under the {floor} floor — "
            "treating as a truncated download rather than trusting it"
        )
    return rows


def race_slug_for(candidate: dict) -> str | None:
    """The race a candidate row belongs to, or None if it isn't one we track."""
    office = (candidate.get("Office") or "").strip().casefold()
    district_type = (candidate.get("DistrictType") or "").strip().casefold()
    district = (candidate.get("District") or "").strip()

    if district_type == "statewide":
        return STATEWIDE_OFFICES.get(office)
    if district_type == "city" and district.casefold() == "chicago":
        return CHICAGO_OFFICES.get(office)
    if district_type == "representative" and office == "state representative":
        if district.isdigit() and 1 <= int(district) <= IL_HOUSE_DISTRICTS:
            return f"hd{int(district)}"
    if district_type == "senate" and office == "state senator":
        if district.isdigit() and 1 <= int(district) <= IL_SENATE_DISTRICTS:
            return f"sd{int(district)}"
    return None


def _mapping_rows(
    client: httpx.Client, min_election_year: int = MIN_ELECTION_YEAR
) -> list[dict]:
    """One row per committee->race link, carrying why it was made.

    The provenance (which candidate, which office, which election year) is the
    only practical way to review these by hand — a bare "41276 -> hd97" tells a
    reviewer nothing about whether it is right.
    """
    candidates = _load(client, "Candidates.txt")
    elections = _load(client, "CanElections.txt")
    links = _load(client, "CmteCandidateLinks.txt")

    years: dict[str, set[int]] = {}
    for row in elections:
        year = (row.get("ElectionYear") or "").strip()
        if year.isdigit():
            years.setdefault((row.get("CandidateID") or "").strip(), set()).add(int(year))

    tracked: dict[str, dict] = {}
    for row in candidates:
        candidate_id = (row.get("ID") or "").strip()
        candidate_years = years.get(candidate_id, set())
        if not any(y >= min_election_year for y in candidate_years):
            continue
        slug = race_slug_for(row)
        if slug:
            tracked[candidate_id] = {
                "slug": slug,
                "candidate": " ".join(
                    x for x in (
                        (row.get("FirstName") or "").strip(),
                        (row.get("LastName") or "").strip(),
                    ) if x
                ),
                "office": (row.get("Office") or "").strip(),
                "district": (row.get("District") or "").strip(),
                "party": (row.get("PartyAffiliation") or "").strip(),
                "election_years": ",".join(
                    str(y) for y in sorted(y for y in candidate_years if y >= min_election_year)
                ),
            }

    rows = []
    for row in links:
        committee_id = (row.get("CommitteeID") or "").strip()
        info = tracked.get((row.get("CandidateID") or "").strip())
        if info and committee_id.isdigit():
            rows.append({"committee_id": int(committee_id), **info})
    return rows


def build_mappings(
    client: httpx.Client, min_election_year: int = MIN_ELECTION_YEAR
) -> dict[int, set[str]]:
    """committee_id -> race slugs, from the three bulk files."""
    mappings: dict[int, set[str]] = {}
    for row in _mapping_rows(client, min_election_year):
        mappings.setdefault(row["committee_id"], set()).add(row["slug"])

    logger.info(
        "race mapping: %d committees -> %d distinct races",
        len(mappings),
        len({s for slugs in mappings.values() for s in slugs}),
    )
    return mappings


def read_overrides(path: Path = OVERRIDES_CSV) -> dict[tuple[int, str], str]:
    """(committee_id, race_slug) -> "yes" | "no". Missing file means no overrides."""
    if not Path(path).exists():
        return {}
    overrides: dict[tuple[int, str], str] = {}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            include = (row.get("include") or "").strip().casefold()
            slug = (row.get("race_slug") or "").strip()
            raw_id = (row.get("committee_id") or "").strip()
            if include in ("yes", "no") and slug and raw_id.isdigit():
                overrides[(int(raw_id), slug)] = include
    return overrides


def sync_race_committees(
    client: httpx.Client | None = None,
    min_election_year: int = MIN_ELECTION_YEAR,
    dry_run: bool = False,
    overrides_path: Path = OVERRIDES_CSV,
) -> dict[str, int]:
    """Refresh links from ISBE, with raceCommitteeOverrides.csv having final say.

    Committees absent from the committees table are skipped rather than stubbed
    in: committee_sync covers all ~34k of them and runs first in the poller, so a
    miss means something is off and shouldn't create junk rows.
    """
    client = client or make_client()
    mappings = build_mappings(client, min_election_year)
    overrides = read_overrides(overrides_path)

    # Forced links are added even when ISBE doesn't propose them.
    for (committee_id, slug), verdict in overrides.items():
        if verdict == "yes":
            mappings.setdefault(committee_id, set()).add(slug)

    counts = {"added": 0, "blocked": 0, "removed": 0, "forced": 0, "unknown_committee": 0}
    with session_scope() as session:
        race_ids = {r.slug: r.id for r in session.scalars(select(Race))}
        known = set(session.scalars(select(Committee.id)))
        existing = {
            (rc.committee_id, rc.race_id): rc
            for rc in session.scalars(select(RaceCommittee))
        }

        for committee_id, slugs in sorted(mappings.items()):
            for slug in sorted(slugs):
                race_id = race_ids.get(slug)
                if race_id is None:
                    continue
                if overrides.get((committee_id, slug)) == "no":
                    counts["blocked"] += 1
                    continue
                if (committee_id, race_id) in existing:
                    continue
                if committee_id not in known:
                    counts["unknown_committee"] += 1
                    continue
                counts["added"] += 1
                if overrides.get((committee_id, slug)) == "yes":
                    counts["forced"] += 1
                if not dry_run:
                    session.add(
                        RaceCommittee(race_id=race_id, committee_id=committee_id)
                    )

        # A "no" must also undo a link added before the override existed.
        for (committee_id, slug), verdict in overrides.items():
            race_id = race_ids.get(slug)
            if verdict != "no" or race_id is None:
                continue
            row = existing.get((committee_id, race_id))
            if row is not None:
                counts["removed"] += 1
                if not dry_run:
                    session.delete(row)

        if dry_run:
            session.rollback()

    logger.info(
        "race mapping: +%d links (%d forced), %d blocked, %d removed by override, "
        "%d committees not in the database%s",
        counts["added"], counts["forced"], counts["blocked"], counts["removed"],
        counts["unknown_committee"],
        " (dry run, nothing written)" if dry_run else "",
    )
    return counts


REPORT_COLUMNS = [
    "race_label", "race_slug", "committee_id", "committee_name", "committee_type",
    "in_database", "override", "name_obvious", "candidate", "office", "district",
    "party", "election_years",
]


def _name_obvious(candidate: str, committee_name: str) -> bool:
    """Whether the committee is self-evidently this candidate's.

    "Raoul for Illinois" via Kwame Raoul needs no thought; "JCUA Votes" via
    Brandon Johnson does. Matching on any name part (not just the surname)
    keeps first-name committees like "JB for Governor" out of the review pile.
    """
    haystack = committee_name.casefold()
    return any(
        len(part.strip('."')) > 2 and part.strip('."').casefold() in haystack
        for part in re.split(r"[\s,]+", candidate)
    )


def _group_slugs(group: str) -> set[str]:
    """Race slugs belonging to one race group."""
    with session_scope() as session:
        return {
            r.slug for r in session.scalars(select(Race).where(Race.race_group == group))
        }


def mapping_report(
    client: httpx.Client | None = None,
    min_election_year: int = MIN_ELECTION_YEAR,
    overrides_path: Path = OVERRIDES_CSV,
) -> list[dict]:
    """Every proposed link with the committee name, race label and the candidacy
    that justifies it — the reviewable form of what the sync would write."""
    client = client or make_client()
    rows = _mapping_rows(client, min_election_year)
    overrides = read_overrides(overrides_path)

    with session_scope() as session:
        races = {
            r.slug: r.label for r in session.scalars(select(Race))
        }
        committees = {
            c.id: (c.name, c.committee_type)
            for c in session.scalars(select(Committee))
        }
        existing = {
            (rc.committee_id, rc.race_id) for rc in session.scalars(select(RaceCommittee))
        }
        race_ids = {r.slug: r.id for r in session.scalars(select(Race))}

        report = []
        for row in rows:
            name, ctype = committees.get(row["committee_id"], ("(not in committees table)", ""))
            race_id = race_ids.get(row["slug"])
            report.append({
                "race_label": races.get(row["slug"], row["slug"]),
                "race_slug": row["slug"],
                "committee_id": row["committee_id"],
                "committee_name": name,
                "committee_type": ctype or "",
                "in_database": "yes" if (row["committee_id"], race_id) in existing else "no",
                "name_obvious": "yes" if _name_obvious(row["candidate"], name) else "no",
                "override": overrides.get((row["committee_id"], row["slug"]), ""),
                "candidate": row["candidate"],
                "office": row["office"],
                "district": row["district"],
                "party": row["party"],
                "election_years": row["election_years"],
            })
    report.sort(key=lambda r: (r["race_label"], r["committee_name"]))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--since", type=int, default=MIN_ELECTION_YEAR)
    common.add_argument(
        "--overrides", default=str(OVERRIDES_CSV), help="path to the overrides CSV"
    )

    p_review = sub.add_parser(
        "review", parents=[common],
        help="what ISBE currently proposes, with any override applied",
    )
    p_review.add_argument(
        "--suspect", action="store_true",
        help="only links whose committee name does not contain the candidate's name",
    )
    p_review.add_argument("--group", metavar="SLUG", help="limit to one race group")
    p_review.add_argument("--csv", metavar="PATH", help="write the table to a CSV")

    p_sync = sub.add_parser(
        "sync", parents=[common], help="apply ISBE's links, honouring overrides"
    )
    p_sync.add_argument(
        "--dry-run", action="store_true", help="report changes, write nothing"
    )

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    if args.command == "review":
        report = mapping_report(
            min_election_year=args.since, overrides_path=Path(args.overrides)
        )
        if args.suspect:
            report = [r for r in report if r["name_obvious"] == "no"]
        if args.group:
            slugs = _group_slugs(args.group)
            report = [r for r in report if r["race_slug"] in slugs]
        if args.csv:
            with open(args.csv, "w", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=REPORT_COLUMNS)
                writer.writeheader()
                writer.writerows(report)
            print(f"wrote {len(report)} links to {args.csv}")
            return
        for r in report:
            mark = {"no": "BLOCKED", "yes": "forced "}.get(r["override"], "       ")
            print(
                f"  {mark} {r['race_label']:<30} {r['committee_name'][:38]:<40} "
                f"{r['committee_type'][:11]:<13} via {r['candidate']} "
                f"({r['office']} {r['district']}, {r['election_years']})"
            )
        blocked = sum(1 for r in report if r["override"] == "no")
        print(
            f"\n{len(report)} links shown, {blocked} blocked by override.\n"
            f"To override one, add a row to {args.overrides}:\n"
            f"  no,<race_slug>,<committee_id>,<committee name>,<why>"
        )
        return

    counts = sync_race_committees(
        min_election_year=args.since,
        dry_run=args.dry_run,
        overrides_path=Path(args.overrides),
    )
    verb = "would apply" if args.dry_run else "applied"
    print(
        f"{verb}: +{counts['added']} links ({counts['forced']} forced by override), "
        f"{counts['blocked']} blocked, {counts['removed']} removed by override, "
        f"{counts['unknown_committee']} committees not in the database"
    )


if __name__ == "__main__":
    main()
