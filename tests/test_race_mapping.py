"""Committee -> race auto-mapping from ISBE bulk data.

Rows use the real column names and value shapes from the ISBE files.
"""

import csv
import io
import pathlib

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import isbe_notifier.db as db
from isbe_notifier import race_mapping
from isbe_notifier.models import Base, Committee, Race, RaceCommittee
from isbe_notifier.seeds import all_races


def _candidate(cid, office, district_type, district):
    return {
        "ID": cid, "LastName": "X", "FirstName": "Y",
        "Office": office, "DistrictType": district_type, "District": district,
    }


@pytest.mark.parametrize(
    "office,district_type,district,expected",
    [
        ("Governor", "Statewide", "", "st-gov"),
        ("Lieutenant Governor", "Statewide", "", "st-ltgov"),
        ("Attorney General", "Statewide", "", "st-ag"),
        ("Secretary of State", "Statewide", "", "st-sos"),
        # ISBE has both casings of this one.
        ("Secretary Of State", "Statewide", "", "st-sos"),
        ("Comptroller", "Statewide", "", "st-comptroller"),
        ("Treasurer", "Statewide", "", "st-treasurer"),
        ("State Representative", "Representative", "12", "hd12"),
        ("State Representative", "Representative", "118", "hd118"),
        ("State Senator", "Senate", "59", "sd59"),
        ("Mayor", "City", "Chicago", "chi-mayor"),
        ("Clerk", "City", "Chicago", "chi-clerk"),
        ("Treasurer", "City", "Chicago", "chi-treasurer"),
        # Same offices, other municipalities — must not map to Chicago.
        ("Mayor", "City", "Elgin", None),
        ("Clerk", "Village", "Thornton", None),
        ("Treasurer", "County", "Cook", None),
        # Districts outside the current maps (ISBE keeps historical numbering).
        ("State Senator", "Senate", "113", None),
        ("State Representative", "Representative", "0", None),
        ("State Representative", "Representative", "", None),
        # Offices we don't track.
        ("Judge", "Subcircuit Court", "4", None),
        ("Member", "School", "Chicago 299", None),  # CPS stays on the whitelist
        ("Alderperson", "Ward", "1", None),
    ],
)
def test_race_slug_for(office, district_type, district, expected):
    row = _candidate("1", office, district_type, district)
    assert race_mapping.race_slug_for(row) == expected


class FakeClient:
    """Serves canned bulk files in place of ISBE."""

    def __init__(self, files):
        self.files = files


def _fake_fetch(files):
    def fetch(client, url, *a, **kw):
        name = url.rsplit("/", 1)[-1]
        rows = files[name]
        header = rows[0]
        buf = io.StringIO()
        buf.write("\t".join(header) + "\n")
        for row in rows[1:]:
            buf.write("\t".join(row) + "\n")

        class Resp:
            text = buf.getvalue()

        return Resp()

    return fetch


@pytest.fixture
def wired(monkeypatch, tmp_path):
    # The canned files below are a handful of rows; the real row floors exist to
    # catch truncated multi-megabyte downloads. test_truncated_file_is_rejected
    # puts a floor back to exercise that path.
    monkeypatch.setattr(race_mapping, "MIN_ROWS", {})
    engine = create_engine(f"sqlite:///{tmp_path}/m.db")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    with Session(engine) as s:
        for data in all_races():
            s.add(Race(**data))
        # Committees 100 and 200 are known; 999 deliberately is not.
        s.add(Committee(id=100, name="Friends of Gov"))
        s.add(Committee(id=200, name="Citizens for HD12"))
        s.commit()
    return engine


FILES = {
    "Candidates.txt": [
        ("ID", "FirstName", "LastName", "PartyAffiliation",
         "Office", "DistrictType", "District"),
        ("1", "Dana", "Gov", "Democrat", "Governor", "Statewide", ""),
        ("2", "Lee", "Rep", "Republican", "State Representative", "Representative", "12"),
        # not Chicago
        ("3", "Sam", "Elgin", "Non Partisan", "Mayor", "City", "Elgin"),
        # stale election year
        ("4", "Jo", "Senate", "Democrat", "State Senator", "Senate", "7"),
        # committee not in the committees table
        ("5", "Kim", "Absent", "Democrat", "Governor", "Statewide", ""),
    ],
    "CanElections.txt": [
        ("ID", "CandidateID", "ElectionYear"),
        ("1", "1", "2026"),
        ("2", "2", "2026"),
        ("3", "3", "2027"),
        ("4", "4", "2018"),  # too old
        ("5", "5", "2026"),
    ],
    "CmteCandidateLinks.txt": [
        ("ID", "CommitteeID", "CandidateID"),
        ("1", "100", "1"),
        ("2", "200", "2"),
        ("3", "200", "3"),
        ("4", "300", "4"),
        ("5", "999", "5"),  # committee not in committees table
    ],
}


def test_build_mappings_filters_by_election_year_and_office(monkeypatch, wired):
    monkeypatch.setattr(race_mapping, "fetch", _fake_fetch(FILES))
    mappings = race_mapping.build_mappings(FakeClient(FILES))

    assert mappings[100] == {"st-gov"}
    assert mappings[200] == {"hd12"}      # the Elgin mayor row is not mapped
    assert 300 not in mappings            # 2018 election is too old
    assert mappings[999] == {"st-gov"}    # built here, skipped at write time


def test_truncated_file_is_rejected(monkeypatch, wired):
    """ISBE's load balancer scrambles Content-Length, so a short body parses
    cleanly. The row floor is the only thing standing between a truncated
    download and silently-missing mappings."""
    monkeypatch.setattr(race_mapping, "fetch", _fake_fetch(FILES))
    monkeypatch.setitem(race_mapping.MIN_ROWS, "Candidates.txt", 1000)

    with pytest.raises(RuntimeError, match="truncated download"):
        race_mapping.build_mappings(FakeClient(FILES))


# --- ISBE syncs automatically; the overrides CSV has the final say ---


def _overrides(tmp_path, *rows):
    path = tmp_path / "overrides.csv"
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=race_mapping.OVERRIDE_COLUMNS)
        w.writeheader()
        for include, slug, cid, note in rows:
            w.writerow({
                "include": include, "race_slug": slug, "committee_id": cid,
                "committee_name": "", "notes": note,
            })
    return path


def _linked(engine):
    with Session(engine) as s:
        return {
            (rc.committee_id, s.get(Race, rc.race_id).slug)
            for rc in s.scalars(select(RaceCommittee))
        }


def test_sync_without_overrides_applies_everything_isbe_proposes(monkeypatch, wired, tmp_path):
    monkeypatch.setattr(race_mapping, "fetch", _fake_fetch(FILES))
    counts = race_mapping.sync_race_committees(
        FakeClient(FILES), overrides_path=tmp_path / "absent.csv"
    )
    assert counts["added"] == 2 and counts["blocked"] == 0
    assert _linked(wired) == {(100, "st-gov"), (200, "hd12")}


def test_override_no_blocks_a_link(monkeypatch, wired, tmp_path):
    monkeypatch.setattr(race_mapping, "fetch", _fake_fetch(FILES))
    path = _overrides(tmp_path, ("no", "st-gov", 100, "not really about this race"))

    counts = race_mapping.sync_race_committees(FakeClient(FILES), overrides_path=path)
    assert counts["blocked"] == 1
    assert _linked(wired) == {(200, "hd12")}


def test_override_no_removes_a_link_added_before_the_override(monkeypatch, wired, tmp_path):
    """The point of an override: a rejection the next sync would undo is useless."""
    monkeypatch.setattr(race_mapping, "fetch", _fake_fetch(FILES))
    race_mapping.sync_race_committees(
        FakeClient(FILES), overrides_path=tmp_path / "absent.csv"
    )
    assert (100, "st-gov") in _linked(wired)

    path = _overrides(tmp_path, ("no", "st-gov", 100, "changed my mind"))
    counts = race_mapping.sync_race_committees(FakeClient(FILES), overrides_path=path)
    assert counts["removed"] == 1
    assert _linked(wired) == {(200, "hd12")}

    # And it stays blocked however many times the sync runs.
    race_mapping.sync_race_committees(FakeClient(FILES), overrides_path=path)
    assert _linked(wired) == {(200, "hd12")}


def test_override_yes_forces_a_link_isbe_does_not_propose(monkeypatch, wired, tmp_path):
    monkeypatch.setattr(race_mapping, "fetch", _fake_fetch(FILES))
    path = _overrides(tmp_path, ("yes", "chi-mayor", 200, "editorially relevant"))

    counts = race_mapping.sync_race_committees(FakeClient(FILES), overrides_path=path)
    assert counts["forced"] == 1
    assert (200, "chi-mayor") in _linked(wired)


def test_sync_is_idempotent_and_dry_run_writes_nothing(monkeypatch, wired, tmp_path):
    monkeypatch.setattr(race_mapping, "fetch", _fake_fetch(FILES))
    absent = tmp_path / "absent.csv"

    assert race_mapping.sync_race_committees(
        FakeClient(FILES), dry_run=True, overrides_path=absent
    )["added"] == 2
    assert _linked(wired) == set()

    race_mapping.sync_race_committees(FakeClient(FILES), overrides_path=absent)
    assert race_mapping.sync_race_committees(
        FakeClient(FILES), overrides_path=absent
    )["added"] == 0


def test_sync_leaves_the_curated_cps_whitelist_alone(monkeypatch, wired, tmp_path):
    monkeypatch.setattr(race_mapping, "fetch", _fake_fetch(FILES))
    with Session(wired) as s:
        cps = s.scalars(select(Race).where(Race.slug == "president")).one()
        s.add(Committee(id=555, name="Curated CPS Committee"))
        s.add(RaceCommittee(race_id=cps.id, committee_id=555))
        s.commit()

    race_mapping.sync_race_committees(
        FakeClient(FILES), overrides_path=tmp_path / "absent.csv"
    )
    assert (555, "president") in _linked(wired)


def test_malformed_override_rows_are_ignored(tmp_path):
    """A typo must not take the sync down or silently block something else."""
    path = tmp_path / "overrides.csv"
    path.write_text(
        "include,race_slug,committee_id,committee_name,notes\n"
        "maybe,st-gov,100,,unrecognised verdict\n"
        "no,,100,,missing race\n"
        "no,st-gov,abc,,non-numeric committee\n"
        "no,st-gov,100,,valid\n"
    )
    assert race_mapping.read_overrides(path) == {(100, "st-gov"): "no"}


def test_missing_overrides_file_is_a_no_op(tmp_path):
    assert race_mapping.read_overrides(tmp_path / "nope.csv") == {}


def test_overrides_file_is_shipped_in_the_docker_image():
    """The image must contain the overrides CSV.

    race_mapping reads it at runtime and treats a missing file as "no
    overrides", so leaving it out of the Dockerfile wouldn't fail the build or
    the deploy — every editorial override would just quietly stop applying in
    production while working fine locally.
    """
    repo = pathlib.Path(__file__).resolve().parents[1]
    dockerfile = (repo / "Dockerfile").read_text()
    assert "raceCommitteeOverrides.csv" in dockerfile

    # And the path the code derives must match where the image puts it.
    assert race_mapping.OVERRIDES_CSV == repo / "raceCommitteeOverrides.csv"
    assert race_mapping.OVERRIDES_CSV.exists()


def test_shipped_overrides_reference_real_races():
    """A typo'd race_slug is silently ignored by read_overrides, so a blocked
    committee would quietly stay linked. This catches it at CI time instead."""
    import csv as _csv

    from isbe_notifier.seeds import all_races

    slugs = {r["slug"] for r in all_races()}
    with open(race_mapping.OVERRIDES_CSV, newline="") as fh:
        rows = list(_csv.DictReader(fh))

    assert rows, "the shipped overrides file should at least carry its examples"
    for row in rows:
        assert row["include"].strip() in ("yes", "no"), row
        assert row["race_slug"].strip() in slugs, f"unknown race_slug: {row['race_slug']}"
        assert row["committee_id"].strip().isdigit(), row
        assert row["notes"].strip(), f"every override should say why: {row}"

    # Every row must survive parsing — a dropped row is a silent no-op.
    assert len(race_mapping.read_overrides()) == len(rows)
