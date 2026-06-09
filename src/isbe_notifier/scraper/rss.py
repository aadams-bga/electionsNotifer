"""Parser for the ISBE "Latest Reports Filed" RSS feed.

Feed quirks this handles:
- Item guids end in ``#<sequence>`` — a monotonically increasing number that is the
  only reliable dedupe key (the feed holds the last 1,000 items).
- Links/guids are app-relative (``~/CampaignDisclosure/...``).
- Paper filings have no <link>, only a PDF-viewer guid.
- <description> packs report type and source separated by an HTML <br/>.
- pubDate has no timezone; ISBE timestamps are America/Chicago.
"""

import html as htmllib
import re
from dataclasses import dataclass
from datetime import datetime
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")

_SEQ_RE = re.compile(r"#(\d+)\s*$")
_TYPE_RE = re.compile(r"Report Type:\s*(.*?)(?:<br\s*/?>|$)", re.S)
_SOURCE_RE = re.compile(r"Source:\s*(.*?)(?:<br\s*/?>|$)", re.S)


@dataclass(frozen=True)
class RssItem:
    guid_seq: int
    committee_name: str
    report_type: str
    source: str
    url: str | None  # absolute; None for paper filings with no <link>
    guid_url: str  # absolute, fragment stripped
    pub_date: datetime | None


def _absolutize(app_relative: str, base_url: str) -> str:
    url = htmllib.unescape(app_relative.strip())
    if url.startswith("~/"):
        url = base_url.rstrip("/") + url[1:]
    return url


def _parse_pub_date(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.strptime(text.strip(), "%a, %d %b %Y %H:%M:%S").replace(tzinfo=CENTRAL)
    except ValueError:
        return None


def parse_feed(xml_text: str, base_url: str = "https://elections.il.gov") -> list[RssItem]:
    root = ElementTree.fromstring(xml_text)
    items: list[RssItem] = []
    for el in root.iter("item"):
        guid_raw = el.findtext("guid") or ""
        seq_match = _SEQ_RE.search(guid_raw)
        if not seq_match:
            continue  # without a sequence number we cannot dedupe; skip
        guid_seq = int(seq_match.group(1))
        guid_url = _absolutize(_SEQ_RE.sub("", guid_raw), base_url)

        link_raw = el.findtext("link")
        url = _absolutize(link_raw, base_url) if link_raw and link_raw.strip() else None

        desc = el.findtext("description") or ""
        type_match = _TYPE_RE.search(desc)
        source_match = _SOURCE_RE.search(desc)

        items.append(
            RssItem(
                guid_seq=guid_seq,
                committee_name=(el.findtext("title") or "").strip(),
                report_type=(type_match.group(1).strip() if type_match else "Unknown"),
                source=(source_match.group(1).strip() if source_match else ""),
                url=url,
                guid_url=guid_url,
                pub_date=_parse_pub_date(el.findtext("pubDate")),
            )
        )
    return items


def classify(report_type: str) -> tuple[str, bool]:
    """Map an ISBE report-type string to (report_class, is_amendment)."""
    is_amendment = "(amendment)" in report_type.lower()
    t = report_type.strip().upper()
    for prefix, cls in (("A-1", "A1"), ("B-1", "B1"), ("D-1", "D1"), ("D-2", "D2")):
        if t.startswith(prefix):
            return cls, is_amendment
    return "OTHER", is_amendment
