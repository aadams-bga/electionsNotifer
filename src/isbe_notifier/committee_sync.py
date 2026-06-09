"""Syncs the local committees table from ISBE's bulk data file so committee
search covers every Illinois committee, not just ones the poller has seen file.

Runs inside the poller (on startup when the table is sparse, then daily).
Can also be run manually: python -m isbe_notifier.committee_sync
"""

import csv
import io
import logging

import httpx
from sqlalchemy import select

from .db import session_scope
from .models import Committee
from .scraper.client import fetch, make_client

logger = logging.getLogger(__name__)

COMMITTEES_FILE_URL = "https://elections.il.gov/CampaignDisclosureDataFiles/Committees.txt"

STATUS_LABELS = {"A": "Active", "F": "Final"}


def sync_committees(client: httpx.Client | None = None) -> int:
    """Upsert all committees from the bulk file. Returns number of new rows."""
    client = client or make_client()
    text = fetch(client, COMMITTEES_FILE_URL).text
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")

    created = 0
    batch: list[dict] = []
    for row in reader:
        raw_id = (row.get("ID") or "").strip()
        if not raw_id.isdigit():
            continue
        batch.append(
            {
                "id": int(raw_id),
                "name": (row.get("Name") or "").strip(),
                "committee_type": (row.get("TypeOfCommittee") or "").strip() or None,
                "status": STATUS_LABELS.get((row.get("Status") or "").strip(), row.get("Status")),
                "purpose": (row.get("Purpose") or "").strip() or None,
            }
        )

    with session_scope() as session:
        existing_ids = set(session.scalars(select(Committee.id)))
        for data in batch:
            if data["id"] in existing_ids:
                committee = session.get(Committee, data["id"])
                committee.name = data["name"] or committee.name
                committee.committee_type = data["committee_type"] or committee.committee_type
                # Never let the bulk file blank out fields the scraper filled in.
                committee.status = data["status"] or committee.status
                committee.purpose = data["purpose"] or committee.purpose
            else:
                session.add(Committee(**data))
                created += 1
    logger.info("committee sync: %d rows, %d new", len(batch), created)
    return created


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(f"created {sync_committees()} committees")
