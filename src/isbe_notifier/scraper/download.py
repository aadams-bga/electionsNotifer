"""ISBE "Download List" flow: every A-1/B-1 line across all pages as CSV.

The list pages paginate at 25 rows, but their ``lnkDownloadList`` LinkButton leads
(via two ASP.NET postbacks that require the session cookies httpx already keeps) to
a CSV of the complete list:

1. POST the list URL with ``__EVENTTARGET=...$lnkDownloadList`` and the hidden
   ASP.NET fields scraped from the page → 302 to ReportsFiledDownloadList.aspx.
2. GET that page, then POST it with ``__EVENTTARGET=...$btnCSV`` → the CSV file.

CSV columns (verified live):
- A-1: CommitteeName, ContributedBy, RcvdDate, Amount, Address1..Zip, D2Part,
  Description, VendorName, VendorAddress1..Zip
- B-1: ReceivedBy, Amount, Purpose, CandidateName, OfficeDistrict,
  SupportingOpposing — note: no date and no address columns, so lines built from a
  B-1 CSV have ``expended_date=None``.
"""

import csv
import html as html_mod
import io
import logging
import re
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin

import httpx

from .pages import A1Line, B1Line

logger = logging.getLogger(__name__)

_HIDDEN_FIELDS = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")
_EVENT_TARGET_DOWNLOAD = "ctl00$ContentPlaceHolder1$lnkDownloadList"
_EVENT_TARGET_CSV = "ctl00$ContentPlaceHolder1$btnCSV"


def _hidden_fields(html: str) -> dict[str, str]:
    fields = {}
    for name in _HIDDEN_FIELDS:
        m = re.search(rf'id="{name}" value="([^"]*)"', html)
        if m:
            # Values are HTML-attribute-escaped (&#43;, &amp;, …).
            fields[name] = html_mod.unescape(m.group(1))
    return fields


def _postback(client: httpx.Client, url: str, page_html: str, event_target: str) -> httpx.Response:
    data = {
        "__EVENTTARGET": event_target,
        "__EVENTARGUMENT": "",
        **_hidden_fields(page_html),
    }
    resp = client.post(url, data=data)
    if resp.status_code not in (200, 301, 302):
        resp.raise_for_status()
    return resp


def download_list_csv(
    client: httpx.Client, list_url: str, page_html: str, pause_seconds: float = 1.0
) -> str | None:
    """Returns the CSV text for a list page, or None if any step fails."""
    try:
        # Step 1: "Download List" postback redirects to the download page.
        # (follow_redirects on the client lands us there directly.)
        time.sleep(pause_seconds)
        resp = _postback(client, list_url, page_html, _EVENT_TARGET_DOWNLOAD)
        download_url = str(resp.url)
        if "ReportsFiledDownloadList" not in download_url:
            location = resp.headers.get("location", "")
            if "ReportsFiledDownloadList" not in location:
                logger.warning("download postback did not redirect for %s", list_url)
                return None
            download_url = urljoin(list_url, location)
            time.sleep(pause_seconds)
            resp = client.get(download_url)

        # Step 2: "CSV File" postback returns the attachment.
        time.sleep(pause_seconds)
        csv_resp = _postback(client, download_url, resp.text, _EVENT_TARGET_CSV)
        disposition = csv_resp.headers.get("content-disposition", "")
        if "attachment" not in disposition:
            logger.warning("CSV postback returned no attachment for %s", list_url)
            return None
        return csv_resp.text
    except httpx.HTTPError:
        logger.exception("download-list flow failed for %s", list_url)
        return None


def parse_csv_rows(csv_text: str) -> list[dict[str, str]]:
    """The files have a blank line after the header; DictReader plus a skip."""
    reader = csv.DictReader(io.StringIO(csv_text))
    return [row for row in reader if any((v or "").strip() for v in row.values())]


def _amount(value: str | None) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(value.replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        return None


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


def _join(row: dict, *keys: str) -> str | None:
    parts = [row.get(k, "").strip() for k in keys]
    return ", ".join(p for p in parts if p) or None


def a1_lines_from_csv(csv_text: str) -> list[A1Line]:
    lines = []
    for row in parse_csv_rows(csv_text):
        name = (row.get("ContributedBy") or "").strip()
        if not name:
            continue
        description = " — ".join(
            p for p in ((row.get("D2Part") or "").strip(), (row.get("Description") or "").strip())
            if p
        ) or None
        lines.append(
            A1Line(
                contributed_by=name,
                address=_join(row, "Address1", "Address2", "City", "State", "Zip"),
                amount=_amount(row.get("Amount")),
                received_date=_date(row.get("RcvdDate")),
                description=description,
                committee_name=(row.get("CommitteeName") or "").strip() or None,
                committee_encrypted_id=None,
                vendor_name=(row.get("VendorName") or "").strip() or None,
                vendor_address=_join(
                    row, "VendorAddress1", "VendorAddress2", "VendorCity",
                    "VendorState", "VendorZip",
                ),
            )
        )
    return lines


def b1_lines_from_csv(csv_text: str) -> list[B1Line]:
    lines = []
    for row in parse_csv_rows(csv_text):
        name = (row.get("ReceivedBy") or "").strip()
        if not name:
            continue
        lines.append(
            B1Line(
                vendor_name=name,
                vendor_address=None,  # not in the CSV
                amount=_amount(row.get("Amount")),
                expended_date=None,  # not in the CSV
                purpose=(row.get("Purpose") or "").strip() or None,
                supporting_opposing=(row.get("SupportingOpposing") or "").strip() or None,
                candidate_name=(row.get("CandidateName") or "").strip() or None,
                office_district=(row.get("OfficeDistrict") or "").strip() or None,
            )
        )
    return lines
