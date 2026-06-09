from datetime import date
from decimal import Decimal
from pathlib import Path

from isbe_notifier.scraper.pages import parse_a1_list, parse_b1_list, parse_committee_detail
from isbe_notifier.scraper.rss import classify, parse_feed

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_feed():
    items = parse_feed((FIXTURES / "latest_reports.xml").read_text())
    assert len(items) == 1000

    # Paper-filed D-1: no link, PDF-viewer guid
    paper = items[0]
    assert paper.committee_name == "Mitchell 4 La Salle County Clerk"
    assert paper.report_type == "D-1 Statement of Organization"
    assert paper.source == "Filed on paper"
    assert paper.url is None
    assert paper.guid_seq == 1010003
    assert "CDPdfViewer.aspx" in paper.guid_url
    assert paper.guid_url.startswith("https://elections.il.gov/")

    # Electronic A-1
    a1 = items[1]
    assert a1.committee_name == "Citizens for Judge Christina Kye"
    assert a1.report_type == "A-1 ($1000+ Year Round)"
    assert a1.source == "Filed electronically"
    assert a1.url.startswith("https://elections.il.gov/CampaignDisclosure/A1List.aspx?ID=")
    assert a1.guid_seq == 1010002
    assert a1.pub_date is not None and a1.pub_date.year == 2026

    # Sequence numbers are unique and roughly newest-first, but NOT strictly ordered
    # (observed in the wild) — dedupe must check each seq against the DB, not a
    # high-water mark.
    seqs = [i.guid_seq for i in items]
    assert len(set(seqs)) == len(seqs)


def test_classify():
    assert classify("A-1 ($1000+ Year Round)") == ("A1", False)
    assert classify("B-1 ($1000+ Year Round)") == ("B1", False)
    assert classify("D-1 Statement of Organization") == ("D1", False)
    assert classify("D-1 Statement of Organization (Amendment)") == ("D1", True)
    assert classify("D-2 Quarterly Report") == ("D2", False)
    assert classify("D-2 Quarterly Report (Amendment)") == ("D2", True)
    assert classify("D-2 Final Report") == ("D2", False)
    assert classify("Letter / Correspondence") == ("OTHER", False)


def test_parse_a1_list():
    page = parse_a1_list((FIXTURES / "a1_list.html").read_text())
    assert page.committee_name == "Citizens for Judge Christina Kye"
    assert page.committee_encrypted_id
    assert not page.has_more_pages
    assert len(page.lines) == 1
    line = page.lines[0]
    assert line.contributed_by == "Baumert, Aggie"
    assert "Hinsdale" in line.address
    assert line.amount == Decimal("1000.00")
    assert line.received_date == date(2026, 6, 9)
    assert "Individual Contribution" in line.description


def test_parse_b1_list():
    page = parse_b1_list((FIXTURES / "b1_list.html").read_text())
    assert page.committee_name == "INCS Action Independent Committee"
    assert len(page.lines) == 4
    line = page.lines[0]
    assert line.vendor_name == "THE BALDUZZI GROUP"
    assert "VICTOR, NY" in line.vendor_address
    assert line.amount == Decimal("24632.00")
    assert line.expended_date == date(2024, 10, 24)
    assert line.purpose == "Mailing"
    assert line.supporting_opposing == "Supporting"
    assert line.candidate_name == "Eva Villalobos"
    assert line.office_district == "Chicago School Board, District 7"
    districts = {ln.office_district for ln in page.lines}
    assert "Chicago School Board, District 6" in districts


def test_parse_committee_detail():
    detail = parse_committee_detail((FIXTURES / "committee_detail.html").read_text())
    assert detail is not None
    assert detail.committee_id == 40616
    assert detail.name == "Citizens for Judge Christina Kye"
    assert detail.committee_type == "Candidate"
    assert detail.status == "Active"
