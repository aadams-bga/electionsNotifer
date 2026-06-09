"""Parsers for ISBE campaign-disclosure pages (A1List, B1List, CommitteeDetail).

The list pages are ASP.NET GridViews. Column identity comes from the sort keys in the
header links (``Sort$ContributedBy`` etc.), which are stabler than display text.
Amount cells pack ``$1,000.00<br/>6/9/2026`` (amount + date received) into one cell.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup, Tag

_SORT_KEY_RE = re.compile(r"Sort\$(\w+)")
_AMOUNT_RE = re.compile(r"\$?([\d,]+(?:\.\d{1,2})?)")
_PAGER_RE = re.compile(r"Page\$\d+")


@dataclass
class A1Line:
    contributed_by: str
    address: str | None
    amount: Decimal | None
    received_date: date | None
    description: str | None
    committee_name: str | None
    committee_encrypted_id: str | None
    vendor_name: str | None
    vendor_address: str | None


@dataclass
class B1Line:
    vendor_name: str
    vendor_address: str | None
    amount: Decimal | None
    expended_date: date | None
    purpose: str | None
    supporting_opposing: str | None
    candidate_name: str | None
    office_district: str | None


@dataclass
class ListPage:
    committee_name: str | None
    committee_encrypted_id: str | None
    lines: list = field(default_factory=list)
    has_more_pages: bool = False


@dataclass
class CommitteeDetail:
    committee_id: int
    name: str
    committee_type: str | None
    status: str | None
    purpose: str | None


def _cell_lines(td: Tag) -> list[str]:
    """Text content of a cell, split on <br/> boundaries, cleaned."""
    for br in td.find_all("br"):
        br.replace_with("\n")
    text = td.get_text()
    return [ln.strip() for ln in text.split("\n") if ln.strip() and ln.strip() != "\xa0"]


def _parse_amount_date(lines: list[str]) -> tuple[Decimal | None, date | None]:
    amount = None
    found_date = None
    for ln in lines:
        if amount is None and "$" in ln:
            m = _AMOUNT_RE.search(ln)
            if m:
                try:
                    amount = Decimal(m.group(1).replace(",", ""))
                except InvalidOperation:
                    pass
            continue
        if found_date is None:
            try:
                found_date = datetime.strptime(ln, "%m/%d/%Y").date()
            except ValueError:
                pass
    return amount, found_date


def _grid_table(soup: BeautifulSoup, grid_id_part: str) -> Tag | None:
    return soup.find("table", id=re.compile(grid_id_part))


def _header_keys(table: Tag) -> list[str]:
    keys = []
    for th in table.find_all("th"):
        a = th.find("a")
        m = _SORT_KEY_RE.search(a["href"]) if a and a.has_attr("href") else None
        keys.append(m.group(1) if m else th.get_text(strip=True))
    return keys


def _data_rows(table: Tag) -> tuple[list[Tag], bool]:
    rows, has_pager = [], False
    for tr in table.find_all("tr"):
        classes = tr.get("class") or []
        if "SearchListTableHeaderRow" in classes:
            continue
        if _PAGER_RE.search(str(tr)):
            has_pager = True
            continue
        if tr.find("td"):
            rows.append(tr)
    return rows, has_pager


def _committee_header(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    """Committee name + encrypted ID from the page's CommitteeDetail link, if any."""
    a = soup.find("a", href=re.compile(r"CommitteeDetail\.aspx\?ID="))
    if not a:
        return None, None
    m = re.search(r"ID=([^&]+)", a["href"])
    return a.get_text(strip=True) or None, (m.group(1) if m else None)


def parse_a1_list(html: str) -> ListPage:
    soup = BeautifulSoup(html, "lxml")
    table = _grid_table(soup, "gvA1List")
    name, enc_id = _committee_header(soup)
    page = ListPage(committee_name=name, committee_encrypted_id=enc_id)
    if table is None:
        return page
    keys = _header_keys(table)
    rows, page.has_more_pages = _data_rows(table)
    for tr in rows:
        tds = tr.find_all("td")
        if len(tds) != len(keys):
            continue
        cells = dict(zip(keys, tds, strict=False))

        amount, received = _parse_amount_date(_cell_lines(cells["Amount"]))

        # "Received By" holds the contribution type text plus the committee link.
        received_by_td = cells.get("CommitteeName")
        desc_text, cmte_name, cmte_enc = None, None, None
        if received_by_td is not None:
            link = received_by_td.find("a", href=re.compile(r"CommitteeDetail\.aspx"))
            if link:
                cmte_name = link.get_text(strip=True)
                m = re.search(r"ID=([^&]+)", link["href"])
                cmte_enc = m.group(1) if m else None
                link.extract()
            non_link = _cell_lines(received_by_td)
            desc_text = " ".join(non_link) or None
            if cmte_name and page.committee_name is None:
                page.committee_name, page.committee_encrypted_id = cmte_name, cmte_enc

        extra_desc = " ".join(_cell_lines(cells["Description"])) if "Description" in cells else ""
        description = " — ".join(x for x in (desc_text, extra_desc) if x) or None

        page.lines.append(
            A1Line(
                contributed_by=" ".join(_cell_lines(cells["ContributedBy"])),
                address=", ".join(_cell_lines(cells["Address1"])) or None
                if "Address1" in cells
                else None,
                amount=amount,
                received_date=received,
                description=description,
                committee_name=cmte_name,
                committee_encrypted_id=cmte_enc,
                vendor_name=" ".join(_cell_lines(cells["VendorName"])) or None
                if "VendorName" in cells
                else None,
                vendor_address=", ".join(_cell_lines(cells["VendorAddress1"])) or None
                if "VendorAddress1" in cells
                else None,
            )
        )
    return page


def parse_b1_list(html: str) -> ListPage:
    soup = BeautifulSoup(html, "lxml")
    table = _grid_table(soup, "gvB1List")
    name, enc_id = _committee_header(soup)
    if name is None:
        # B1 pages show the filing committee in a header span instead of a link.
        span = soup.find("span", id=re.compile(r"lblName$"))
        if span:
            name = span.get_text(strip=True) or None
    page = ListPage(committee_name=name, committee_encrypted_id=enc_id)
    if table is None:
        return page
    keys = _header_keys(table)
    rows, page.has_more_pages = _data_rows(table)
    for tr in rows:
        tds = tr.find_all("td")
        if len(tds) != len(keys):
            continue
        cells = dict(zip(keys, tds, strict=False))
        vendor_lines = _cell_lines(cells["ReceivedBy"])
        amount, expended = _parse_amount_date(_cell_lines(cells["Amount"]))
        page.lines.append(
            B1Line(
                vendor_name=vendor_lines[0] if vendor_lines else "",
                vendor_address=", ".join(vendor_lines[1:]) or None,
                amount=amount,
                expended_date=expended,
                purpose=" ".join(_cell_lines(cells["Purpose"])) or None
                if "Purpose" in cells
                else None,
                supporting_opposing=" ".join(_cell_lines(cells["SupportingOpposing"])) or None
                if "SupportingOpposing" in cells
                else None,
                candidate_name=" ".join(_cell_lines(cells["CandidateName"])) or None
                if "CandidateName" in cells
                else None,
                office_district=" ".join(_cell_lines(cells["OfficeDistrict"])) or None
                if "OfficeDistrict" in cells
                else None,
            )
        )
    return page


def parse_committee_detail(html: str) -> CommitteeDetail | None:
    soup = BeautifulSoup(html, "lxml")

    def span_text(suffix: str) -> str | None:
        el = soup.find("span", id=re.compile(rf"{suffix}$"))
        return el.get_text(strip=True) if el else None

    raw_id = span_text("lblCommitteeID")
    if not raw_id or not raw_id.isdigit():
        return None
    return CommitteeDetail(
        committee_id=int(raw_id),
        name=span_text("lblName") or "",
        committee_type=span_text("lblTypeOfCommittee"),
        status=span_text("lblStatus"),
        purpose=span_text("lblPurpose"),
    )
