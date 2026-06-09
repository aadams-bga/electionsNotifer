"""Builds notification subject/body/push text for a filing, per report class.

Content rules (per project plan):
- A-1: contributor(s), date(s) received, amount(s), total, link.
- B-1: vendor, amount(s), date(s), purpose, supporting/opposing, candidate,
  office-district, link.
- D-1 / D-2 / other: one-line "filed" notice with link.
- Paper filings: type + PDF link.
"""

from dataclasses import dataclass

from ..models import Filing


@dataclass
class NotificationContent:
    subject: str
    body_text: str  # plain text email body (also reused inside the HTML template)
    push_title: str
    push_body: str
    url: str


def _money(amount) -> str:
    return f"${amount:,.2f}" if amount is not None else "(amount unavailable)"


def _date(d) -> str:
    return d.strftime("%-m/%-d/%Y") if d else "(date unavailable)"


def build_content(filing: Filing) -> NotificationContent:
    feed_item = filing.feed_item
    committee = filing.committee.name if filing.committee else feed_item.committee_name
    url = feed_item.url or feed_item.guid_url
    report_type = filing.report_type
    amendment = " (amendment)" if filing.is_amendment else ""

    if filing.report_class == "A1" and filing.lines:
        total = sum((ln.amount for ln in filing.lines if ln.amount is not None), start=0)
        rows = [
            f"  • {ln.name} — {_money(ln.amount)} received {_date(ln.line_date)}"
            + (f" ({ln.description})" if ln.description else "")
            for ln in filing.lines
        ]
        n = len(filing.lines)
        subject = f"{committee} reported {_money(total)} in major contributions"
        body = (
            f"{committee} filed an A-1 (major contribution) report{amendment} "
            f"with {n} contribution{'s' if n != 1 else ''} totaling {_money(total)}:\n\n"
            + "\n".join(rows)
        )
        push_body = f"{_money(total)} in contributions: " + "; ".join(
            f"{ln.name} {_money(ln.amount)}" for ln in filing.lines[:3]
        ) + ("…" if n > 3 else "")
        return NotificationContent(subject, body, f"A-1: {committee}", push_body, url)

    if filing.report_class == "B1" and filing.lines:
        total = sum((ln.amount for ln in filing.lines if ln.amount is not None), start=0)
        rows = []
        for ln in filing.lines:
            parts = [f"  • {_money(ln.amount)} to {ln.vendor_name} on {_date(ln.line_date)}"]
            if ln.purpose:
                parts.append(f"purpose: {ln.purpose}")
            if ln.supporting_opposing and ln.candidate_name:
                parts.append(f"{ln.supporting_opposing.lower()} {ln.candidate_name}")
            if ln.office_district:
                parts.append(f"({ln.office_district})")
            rows.append(" — ".join(parts))
        n = len(filing.lines)
        subject = f"{committee} reported {_money(total)} in independent expenditures"
        body = (
            f"{committee} filed a B-1 (independent expenditure) report{amendment} "
            f"with {n} expenditure{'s' if n != 1 else ''} totaling {_money(total)}:\n\n"
            + "\n".join(rows)
        )
        push_body = f"{_money(total)} spent: " + "; ".join(
            f"{(ln.supporting_opposing or '').lower()} {ln.candidate_name}".strip()
            for ln in filing.lines[:3]
            if ln.candidate_name
        ) + ("…" if n > 3 else "")
        return NotificationContent(subject, body, f"B-1: {committee}", push_body or subject, url)

    if filing.report_class == "D1":
        subject = f"{committee} filed a Statement of Organization{amendment}"
    elif filing.report_class == "D2":
        label = "Final Report" if "final" in report_type.lower() else "Quarterly Report"
        subject = f"{committee} filed a {label}{amendment}"
    else:
        subject = f"{committee} filed: {report_type}"

    source_note = f" ({feed_item.source})" if feed_item.source else ""
    body = f"{committee} filed: {report_type}{source_note}."
    return NotificationContent(subject, body, subject, report_type, url)
