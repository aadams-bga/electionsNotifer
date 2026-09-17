import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import isbe_notifier.db as db
from isbe_notifier.models import Base, Committee, PushSubscription, Subscriber, Subscription
from isbe_notifier.notify import tokens
from isbe_notifier.seeds import all_races
from isbe_notifier.web import app as webapp_module
from isbe_notifier.web.app import LANDING_SCOPES, app


@pytest.fixture
def client(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))

    from isbe_notifier.models import Race

    with db.session_scope() as s:
        for data in all_races():
            s.add(Race(**data))
        s.add(Committee(id=12345, name="Friends for a Better Chicago"))

    # No rate limiting in tests
    app.state.limiter.enabled = False
    sent = []
    monkeypatch.setattr(
        webapp_module, "send_email",
        lambda to, subject, body, link, sid: sent.append((to, subject, body)),
    )
    admin_sent = []
    monkeypatch.setattr(
        webapp_module, "send_admin_email",
        lambda subject, body: admin_sent.append((subject, body)),
    )
    c = TestClient(app)
    c.sent_emails = sent
    c.admin_emails = admin_sent
    return c


def _extract_token(text: str, purpose: str) -> str:
    m = re.search(rf"/{purpose}\?token=([\w.\-_]+)", text)
    assert m, f"no {purpose} link in: {text}"
    return m.group(1)


def test_landing_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Today's reports" in resp.text
    assert "Sign up" in resp.text  # nav CTA; the full ad lives on /about now
    assert "No reports tied to CPS Board yet today" in resp.text

    # One tab per scope, each reachable, CPS the default.
    for slug, label in [
        ("cps", "CPS Board"), ("governor", "Governor"), ("mayor", "Chicago Mayor"),
        ("ga", "General Assembly"), ("statewide", "Statewide"), ("all", "All filings"),
    ]:
        assert label in resp.text, label
        assert client.get(f"/?scope={slug}").status_code == 200
    assert "/?scope=all" in resp.text

    # An unknown scope falls back to the default rather than erroring.
    assert client.get("/?scope=nonsense").status_code == 200
    assert "Today's reports in CPS Board" in client.get("/?scope=nonsense").text


def test_landing_shows_todays_filings(client):
    from datetime import UTC, datetime

    from isbe_notifier.models import FeedItem, Filing, FilingRace, Race

    with db.session_scope() as s:
        d7 = s.scalars(select(Race).where(Race.slug == "d7a")).one()
        item = FeedItem(
            guid_seq=42, committee_name="Friends of Now", report_type="A-1",
            source="Filed electronically", url="https://x.test/42",
            guid_url="https://x.test/42", pub_date=datetime.now(UTC),
        )
        stray = FeedItem(
            guid_seq=43, committee_name="Statewide Stray", report_type="D-2",
            source="Filed electronically", url="https://x.test/43",
            guid_url="https://x.test/43", pub_date=datetime.now(UTC),
        )
        s.add_all([item, stray])
        s.flush()
        filing = Filing(feed_item_seq=42, report_type="A-1", report_class="A1")
        s.add(filing)
        s.flush()
        s.add(FilingRace(filing_id=filing.id, race_id=d7.id))

    resp = client.get("/")
    assert "Friends of Now" in resp.text
    assert "District 7a" in resp.text
    assert "Statewide Stray" not in resp.text  # CPS scope by default

    resp = client.get("/?scope=all")
    assert "Friends of Now" in resp.text
    assert "Statewide Stray" in resp.text


def test_about_page_renders(client):
    resp = client.get("/about")
    assert resp.status_code == 200
    assert "Sign up for alerts" in resp.text
    assert "How it works" in resp.text
    # the ad no longer lives on the landing page
    assert "How it works" not in client.get("/").text


def test_install_page_renders(client):
    resp = client.get("/install")
    assert resp.status_code == 200
    assert "Add to Home Screen" in resp.text
    assert "iOS 16.4" in resp.text


def test_subscribe_page_renders(client):
    resp = client.get("/subscribe")
    assert resp.status_code == 200
    assert "District 10b" in resp.text
    assert "CPS Board President" in resp.text
    assert "All CPS Board races" in resp.text
    assert "Daily summary" in resp.text


def test_embed_subscribe_page_renders(client):
    resp = client.get("/embed/subscribe")
    assert resp.status_code == 200
    assert "District 10b" in resp.text
    assert "signup-form" in resp.text
    # no site header/nav in the embed — it's meant to sit inside another page
    assert "Today's filings" not in resp.text
    # push requires a top-level browsing context (browsers block permission
    # prompts from cross-origin iframes) — not offered in the embed; the note
    # about it lives inside the box, below the submit button
    assert 'id="wants-push"' not in resp.text
    assert 'id="wants-email"' not in resp.text  # email is real-time's only channel here
    assert "filings.illinoisanswers.org" in resp.text  # points push seekers to the real site
    assert "embed-divider" not in resp.text
    assert "always arrive by email" not in resp.text
    # box (races..submit..push note) all inside one form-box div
    box_start = resp.text.index('<div class="form-box">')
    box_end = resp.text.index("</div>", resp.text.index("push notifications on your device"))
    assert box_start < resp.text.index("accept-terms") < resp.text.index("submit-btn") \
        < resp.text.index("push notifications on your device") < box_end
    # email address field is always visible, not conditionally hidden
    assert '<section>\n    <h2>What is your email address?</h2>' in resp.text


def test_frame_headers_scoped_to_embed_routes(client):
    resp = client.get("/subscribe")
    assert resp.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'self'" in resp.headers["content-security-policy"]
    assert "illinoisanswers.org" not in resp.headers["content-security-policy"]

    resp = client.get("/embed/subscribe")
    assert "x-frame-options" not in resp.headers
    csp = resp.headers["content-security-policy"]
    assert "frame-ancestors 'self' https://illinoisanswers.org" in csp
    assert "https://www.illinoisanswers.org" in csp
    assert "Real-time alerts" in resp.text
    assert "Advanced options" in resp.text  # firehose + committee search live here
    assert "firehose" in resp.text.lower()


def test_login_flow(client):
    client.post("/api/subscribe", json={
        "accepts_terms": True,
        "email": "login@example.org", "wants_email": True, "race_slugs": ["d1a"],
    })
    client.sent_emails.clear()

    assert client.get("/login").status_code == 200
    # Known address → email with manage link; response is generic
    resp = client.post("/login", data={"email": "Login@Example.org"})
    assert resp.status_code == 200
    assert "emailed it a sign-in link" in resp.text
    assert len(client.sent_emails) == 1
    to, subject, body = client.sent_emails[0]
    assert to == "login@example.org"
    token = _extract_token(body, "manage")
    assert client.get(f"/manage?token={token}").status_code == 200

    # Unknown address → identical response, no email
    client.sent_emails.clear()
    resp = client.post("/login", data={"email": "nobody@example.org"})
    assert "emailed it a sign-in link" in resp.text
    assert client.sent_emails == []


def test_all_cps_and_digest_signup(client):
    resp = client.post("/api/subscribe", json={
        "accepts_terms": True,
        "email": "cps@example.org", "wants_email": True, "all_cps": True,
        "wants_daily_digest": True, "wants_weekly_digest": True,
    })
    assert resp.status_code == 200, resp.text
    with db.session_scope() as s:
        sub = s.scalars(select(Subscription)).one()
        assert sub.all_cps is True and sub.all_filings is False
        subscriber = s.scalars(select(Subscriber)).one()
        assert subscriber.wants_daily_digest is True
        assert subscriber.wants_weekly_digest is True

    # digest flags require an email address
    assert client.post("/api/subscribe", json={
        "accepts_terms": True,
        "wants_push": True, "race_slugs": ["d1a"], "wants_daily_digest": True,
    }).status_code == 400


def test_digest_only_signup(client):
    """Daily/weekly summaries are valid without real-time alerts."""
    resp = client.post("/api/subscribe", json={
        "accepts_terms": True,
        "email": "digest-only@example.org", "race_slugs": ["d1a"],
        "wants_daily_digest": True,
    })
    assert resp.status_code == 200, resp.text
    with db.session_scope() as s:
        subscriber = s.scalars(select(Subscriber)).one()
        assert subscriber.wants_daily_digest is True
        sub = s.scalars(select(Subscription)).one()
        assert sub.wants_email is False and sub.wants_push is False

    # but picking no cadence at all is still rejected
    assert client.post("/api/subscribe", json={
        "accepts_terms": True,
        "email": "nothing@example.org", "race_slugs": ["d1a"],
    }).status_code == 400


def test_manage_updates_flags_and_digests(client):
    client.post("/api/subscribe", json={
        "accepts_terms": True,
        "email": "flags@example.org", "wants_email": True, "all_cps": True,
        "wants_daily_digest": True,
    })
    with db.session_scope() as s:
        sid = s.scalars(select(Subscriber)).one().id
    manage_token = tokens.make_token(sid, "manage")

    resp = client.get(f"/manage?token={manage_token}")
    assert "All CPS Board races" in resp.text

    resp = client.post("/api/manage", json={
        "token": manage_token, "wants_email": True, "all_filings": True,
        "wants_weekly_digest": True,
    })
    assert resp.status_code == 200
    with db.session_scope() as s:
        sub = s.scalars(select(Subscription)).one()
        assert sub.all_filings is True and sub.all_cps is False
        subscriber = s.scalars(select(Subscriber)).one()
        assert subscriber.wants_daily_digest is False  # replaced wholesale
        assert subscriber.wants_weekly_digest is True


def test_signup_verify_flow(client):
    resp = client.post("/api/subscribe", json={
        "accepts_terms": True,
        "email": "reader@example.org",
        "wants_email": True,
        "race_slugs": ["president", "d4a"],
        "committee_ids": [12345],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["needs_verification"] is True
    assert data["manage_token"]

    # Verification email was "sent" with a working link
    assert len(client.sent_emails) == 1
    to, subject, body = client.sent_emails[0]
    assert to == "reader@example.org"
    token = _extract_token(body, "verify")
    resp = client.get(f"/verify?token={token}")
    assert resp.status_code == 200
    assert "confirmed" in resp.text

    with db.session_scope() as s:
        sub = s.scalars(select(Subscriber)).one()
        assert sub.email_verified_at is not None
        assert len(sub.subscriptions) == 3


def test_terms_and_marketing(client):
    # Signup without accepting the terms is rejected
    resp = client.post("/api/subscribe", json={
        "email": "noterms@example.org", "wants_email": True, "race_slugs": ["d1a"],
    })
    assert resp.status_code == 400
    assert "terms" in resp.json()["detail"].lower()

    # Accepting terms records a timestamp; marketing opt-in is stored
    resp = client.post("/api/subscribe", json={
        "accepts_terms": True, "marketing_opt_in": True,
        "email": "consent@example.org", "wants_email": True, "race_slugs": ["d1a"],
    })
    assert resp.status_code == 200
    with db.session_scope() as s:
        subscriber = s.scalars(select(Subscriber)).one()
        assert subscriber.terms_accepted_at is not None
        assert subscriber.marketing_opt_in is True

    # Marketing defaults to off and is never un-set by a later signup
    resp = client.post("/api/subscribe", json={
        "accepts_terms": True,
        "email": "consent@example.org", "wants_email": True, "race_slugs": ["d2a"],
    })
    assert resp.status_code == 200
    with db.session_scope() as s:
        subscriber = s.scalars(select(Subscriber)).one()
        assert subscriber.marketing_opt_in is True


def test_signup_validation(client):
    assert client.post("/api/subscribe", json={
        "accepts_terms": True,
        "wants_email": True, "race_slugs": ["president"],
    }).status_code == 400  # email channel without address
    assert client.post("/api/subscribe", json={
        "accepts_terms": True,
        "email": "x@example.org", "wants_email": True, "race_slugs": [],
    }).status_code == 400  # nothing followed
    assert client.post("/api/subscribe", json={
        "accepts_terms": True,
        "email": "x@example.org", "wants_email": True, "race_slugs": ["not-a-race"],
    }).status_code == 400
    assert client.post("/api/subscribe", json={
        "accepts_terms": True,
        "email": "not-an-email", "wants_email": True, "race_slugs": ["president"],
    }).status_code == 422


def test_signup_idempotent_for_existing_email(client):
    for _ in range(2):
        resp = client.post("/api/subscribe", json={
        "accepts_terms": True,
            "email": "again@example.org", "wants_email": True, "race_slugs": ["d1a"],
        })
        assert resp.status_code == 200
    with db.session_scope() as s:
        subs = s.scalars(select(Subscription)).all()
        assert len(subs) == 1  # not duplicated


def test_admin_notified_on_signup_update_unsubscribe(client):
    client.post("/api/subscribe", json={
        "accepts_terms": True,
        "email": "watched@example.org", "wants_email": True, "all_cps": True,
        "race_slugs": ["d1a"], "wants_daily_digest": True, "marketing_opt_in": True,
    })
    subject, body = client.admin_emails[-1]
    assert subject == "New signup: watched@example.org"
    assert "All CPS Board races" in body
    assert "real-time email" in body and "daily summary" in body
    assert "Marketing opt-in: yes" in body
    assert "Needs email verification: yes" in body

    # Same email again → labeled an update, not a new signup
    client.post("/api/subscribe", json={
        "accepts_terms": True,
        "email": "watched@example.org", "wants_email": True, "race_slugs": ["d2a"],
    })
    subject, body = client.admin_emails[-1]
    assert subject == "Signup updated: watched@example.org"

    with db.session_scope() as s:
        sid = s.scalars(select(Subscriber)).one().id
    unsub_token = tokens.make_token(sid, "unsubscribe")
    client.get(f"/unsubscribe?token={unsub_token}")
    subject, body = client.admin_emails[-1]
    assert subject == "Unsubscribed: watched@example.org"


def test_manage_and_unsubscribe(client):
    client.post("/api/subscribe", json={
        "accepts_terms": True,
        "email": "m@example.org", "wants_email": True, "race_slugs": ["d2a", "d2b"],
    })
    with db.session_scope() as s:
        sid = s.scalars(select(Subscriber)).one().id
    manage_token = tokens.make_token(sid, "manage")

    resp = client.get(f"/manage?token={manage_token}")
    assert resp.status_code == 200
    assert "m@example.org" in resp.text

    resp = client.post("/api/manage", json={
        "token": manage_token, "wants_email": True,
        "race_slugs": ["d5a"], "committee_ids": [12345],
    })
    assert resp.status_code == 200
    with db.session_scope() as s:
        subs = s.scalars(select(Subscription)).all()
        assert len(subs) == 2

    unsub_token = tokens.make_token(sid, "unsubscribe")
    resp = client.get(f"/unsubscribe?token={unsub_token}")
    assert resp.status_code == 200
    with db.session_scope() as s:
        assert s.scalars(select(Subscriber)).first() is None
        assert s.scalars(select(Subscription)).first() is None


def test_bad_tokens_rejected(client):
    assert "invalid" in client.get("/verify?token=garbage").text
    assert client.post("/api/manage", json={
        "token": "garbage", "wants_email": True, "race_slugs": [],
    }).status_code == 403


def test_push_and_digest_sends_verification(client):
    """Push-only real-time + digest email: providing an email for digests must trigger
    verification even though the email channel itself isn't checked."""
    resp = client.post("/api/subscribe", json={
        "accepts_terms": True,
        "wants_push": True,
        "wants_email": False,
        "email": "pusher@example.org",
        "race_slugs": ["d1a"],
        "wants_daily_digest": True,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["needs_verification"] is True
    assert len(client.sent_emails) == 1
    to, _, body = client.sent_emails[0]
    assert to == "pusher@example.org"
    assert "verify" in body


def test_push_only_signup(client):
    resp = client.post("/api/subscribe", json={
        "accepts_terms": True,
        "wants_push": True, "race_slugs": ["d7a"],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["needs_verification"] is False

    resp = client.post("/api/push/subscribe", json={
        "token": data["manage_token"],
        "endpoint": "https://push.example/abc",
        "p256dh": "key",
        "auth": "auth",
    })
    assert resp.status_code == 200
    with db.session_scope() as s:
        push = s.scalars(select(PushSubscription)).one()
        sub = s.get(Subscriber, push.subscriber_id)
        assert sub.email is None


def test_committee_search(client):
    resp = client.get("/api/committees?q=better chicago")
    assert resp.json()["results"][0]["id"] == 12345
    resp = client.get("/api/committees?q=12345")
    assert resp.json()["results"][0]["name"] == "Friends for a Better Chicago"
    assert client.get("/api/committees?q=x").json() == {"results": []}


def test_admin_requires_token(client):
    assert client.get("/admin").status_code == 404


def test_admin_export_marketing_csv(client, monkeypatch):
    from isbe_notifier.config import get_settings

    monkeypatch.setattr(get_settings(), "admin_token", "secret")

    assert client.get("/admin/export/marketing.csv").status_code == 404
    assert client.get("/admin/export/marketing.csv?token=wrong").status_code == 404

    client.post("/api/subscribe", json={
        "accepts_terms": True, "marketing_opt_in": True, "all_cps": True,
        "email": "yes@example.org", "wants_email": True, "wants_daily_digest": True,
    })
    client.post("/api/subscribe", json={
        "accepts_terms": True, "marketing_opt_in": False, "all_cps": True,
        "email": "no@example.org", "wants_email": True, "wants_daily_digest": True,
    })

    resp = client.get("/admin/export/marketing.csv?token=secret")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    lines = resp.text.strip().splitlines()
    assert lines[0] == "email,created_at"
    assert len(lines) == 2
    assert "yes@example.org" in lines[1]


def test_firehose_signup(client):
    resp = client.post("/api/subscribe", json={
        "accepts_terms": True,
        "email": "hose@example.org", "wants_email": True, "all_filings": True,
    })
    assert resp.status_code == 200
    with db.session_scope() as s:
        sub = s.scalars(select(Subscription)).one()
        assert sub.all_filings is True
        assert sub.race_id is None and sub.committee_id is None


def test_signup_with_group_follow(client):
    resp = client.post("/api/subscribe", json={
        "accepts_terms": True, "email": "g@example.org", "wants_email": True,
        "all_groups": ["statewide", "chicago"],
    })
    assert resp.status_code == 200
    with db.session_scope() as s:
        groups = {sub.all_group for sub in s.scalars(select(Subscription))}
        assert groups == {"statewide", "chicago"}


def test_group_follow_is_enough_on_its_own(client):
    """A group follow counts as following something — no race/committee needed."""
    resp = client.post("/api/subscribe", json={
        "accepts_terms": True, "email": "only@example.org", "wants_email": True,
        "all_groups": ["statewide"],
    })
    assert resp.status_code == 200


def test_unknown_and_cps_groups_are_rejected(client):
    """Unknown slugs are dropped, and "cps" is not accepted here — it has its own
    all_cps flag, so allowing both would give one subscriber two all-CPS rows."""
    resp = client.post("/api/subscribe", json={
        "accepts_terms": True, "email": "bad@example.org", "wants_email": True,
        "all_groups": ["nonsense", "cps"], "race_slugs": ["d1a"],
    })
    assert resp.status_code == 200
    with db.session_scope() as s:
        assert {sub.all_group for sub in s.scalars(select(Subscription))} == {None}


def test_group_follow_does_not_duplicate_on_resignup(client):
    for _ in range(2):
        resp = client.post("/api/subscribe", json={
            "accepts_terms": True, "email": "dupe@example.org", "wants_email": True,
            "all_groups": ["statewide"], "race_slugs": ["d1a"],
        })
        assert resp.status_code == 200
    with db.session_scope() as s:
        subs = s.scalars(select(Subscription)).all()
        assert len(subs) == 2  # one race row + one group row, not four


def test_signup_form_has_a_section_per_group(client):
    body = client.get("/subscribe").text
    for heading in ("Chicago Board of Education", "Statewide offices",
                    "Chicago citywide offices", "Illinois House", "Illinois Senate"):
        assert heading in body, heading
    # Small groups render checkboxes; the 177 legislative districts do not.
    assert 'value="st-gov"' in body and 'value="chi-mayor"' in body
    assert 'value="hd1"' not in body and 'value="sd1"' not in body
    assert body.count('class="race-q"') == 2  # one picker per legislative chamber
    assert 'id="all-cps"' in body
    assert 'data-group="statewide"' in body


def test_race_search_api(client):
    # A bare number is the common query; it must not bury district 1 under 1x/1xx.
    results = client.get("/api/races?q=1&group=ilhouse").json()["results"]
    assert results and results[0]["slug"] == "hd1"

    labels = {r["slug"] for r in client.get("/api/races?q=12&group=ilhouse").json()["results"]}
    assert "hd12" in labels

    # group scopes the search
    senate = client.get("/api/races?q=12&group=ilsenate").json()["results"]
    assert all(r["group"] == "ilsenate" for r in senate)

    assert client.get("/api/races?q=").json() == {"results": []}


def test_manage_round_trip_with_groups(client):
    client.post("/api/subscribe", json={
        "accepts_terms": True, "email": "m2@example.org", "wants_email": True,
        "all_groups": ["statewide"],
    })
    with db.session_scope() as s:
        sid = s.scalars(select(Subscriber)).one().id
    token = tokens.make_token(sid, "manage")

    page = client.get(f"/manage?token={token}")
    assert page.status_code == 200
    # The followed group comes back checked.
    assert 'data-group="statewide"' in page.text

    resp = client.post("/api/manage", json={
        "token": token, "wants_email": True,
        "all_groups": ["chicago"], "race_slugs": ["hd12"],
    })
    assert resp.status_code == 200
    with db.session_scope() as s:
        subs = s.scalars(select(Subscription)).all()
        assert {x.all_group for x in subs if x.all_group} == {"chicago"}
        assert {x.race.slug for x in subs if x.race} == {"hd12"}


def test_landing_scope_tabs_filter_and_count(client):
    """Each tab shows only its own filings, and the counts match."""
    from datetime import UTC, datetime

    from isbe_notifier.models import FeedItem, Filing, FilingRace, Race

    # (feed seq, committee name, race slug or None for a race-less filing)
    fixtures = [
        (60, "CPS Cmte", "d3a"),
        (61, "Gov Cmte", "st-gov"),
        (62, "AG Cmte", "st-ag"),          # statewide but not the governor tab
        (63, "Mayor Cmte", "chi-mayor"),
        (64, "House Cmte", "hd12"),
        (65, "Senate Cmte", "sd7"),
        (66, "Unmatched Cmte", None),      # only ever shows under "all"
    ]
    with db.session_scope() as s:
        for seq, name, slug in fixtures:
            s.add(FeedItem(
                guid_seq=seq, committee_name=name, report_type="A-1",
                source="Filed electronically", url=f"https://x.test/{seq}",
                guid_url=f"https://x.test/{seq}", pub_date=datetime.now(UTC),
            ))
            s.flush()
            filing = Filing(feed_item_seq=seq, report_type="A-1", report_class="A1")
            s.add(filing)
            s.flush()
            if slug:
                race = s.scalars(select(Race).where(Race.slug == slug)).one()
                s.add(FilingRace(filing_id=filing.id, race_id=race.id))

    expected = {
        "cps": {"CPS Cmte"},
        "governor": {"Gov Cmte"},
        "mayor": {"Mayor Cmte"},
        "ga": {"House Cmte", "Senate Cmte"},
        "statewide": {"Gov Cmte", "AG Cmte"},  # governor is also a statewide office
        "all": {name for _, name, _ in fixtures},
    }
    all_names = {name for _, name, _ in fixtures}
    for scope, shown in expected.items():
        body = client.get(f"/?scope={scope}").text
        for name in shown:
            assert name in body, f"{name} missing from {scope}"
        for name in all_names - shown:
            assert name not in body, f"{name} leaked into {scope}"
        # The tab strip reports the same number the table shows.
        assert f"Today's reports in {LANDING_SCOPES[scope]['label']} ({len(shown)})" in body


def test_race_groups_are_collapsible_with_cps_open(client):
    body = client.get("/subscribe").text
    assert body.count('class="race-group"') == 5
    # CPS leads the form, so it starts open; the rest start collapsed.
    assert '<details class="race-group" data-group="cps" open>' in body
    for group in ("statewide", "chicago", "ilsenate", "ilhouse"):
        assert f'<details class="race-group" data-group="{group}">' in body, group


def test_manage_opens_groups_the_subscriber_already_follows(client):
    """A collapsed section must never hide an existing selection."""
    client.post("/api/subscribe", json={
        "accepts_terms": True, "email": "open@example.org", "wants_email": True,
        "race_slugs": ["hd12"], "all_groups": ["chicago"],
    })
    with db.session_scope() as s:
        sid = s.scalars(select(Subscriber)).one().id
    body = client.get(f"/manage?token={tokens.make_token(sid, 'manage')}").text

    # followed via a picked race, and via a whole-group follow
    assert '<details class="race-group" data-group="ilhouse" open>' in body
    assert '<details class="race-group" data-group="chicago" open>' in body
    # untouched groups stay collapsed; CPS still opens as the default
    assert '<details class="race-group" data-group="ilsenate">' in body
    assert '<details class="race-group" data-group="cps" open>' in body


def test_manage_opens_cps_for_an_all_cps_subscriber(client):
    client.post("/api/subscribe", json={
        "accepts_terms": True, "email": "allcps@example.org", "wants_email": True,
        "all_cps": True,
    })
    with db.session_scope() as s:
        sid = s.scalars(select(Subscriber)).one().id
    body = client.get(f"/manage?token={tokens.make_token(sid, 'manage')}").text
    assert '<details class="race-group" data-group="cps" open>' in body
    assert 'id="all-cps" checked' in body


def test_forms_use_a_single_box(client):
    """Signup, manage and the embed each keep every step in one box."""
    for path in ("/subscribe", "/embed/subscribe"):
        assert client.get(path).text.count('<div class="form-box"') == 1, path


def test_race_group_summaries_have_no_count_badge(client):
    body = client.get("/subscribe").text
    assert "group-count" not in body
    # the district pickers still name the count in their placeholder
    assert "Search 118 districts" in body


def test_embed_stays_cps_only_and_flat(client):
    """The WordPress embed is CPS-focused by design and deliberately does NOT
    share the grouped/collapsible sections the signup page uses."""
    body = client.get("/embed/subscribe").text

    # No grouped sections, no collapsible groups, no district pickers.
    assert "race-group" not in body
    assert "race-q" not in body
    assert "group-all" not in body
    assert body.count('<div class="form-box">') == 1

    # Exactly the 21 CPS races, as a flat grid, with the original select-all copy.
    assert body.count('name="race"') == 21
    assert "All CPS Board races" in body
    assert "the president's race and every district" in body
    for slug in ("st-gov", "chi-mayor", "hd1", "sd1"):
        assert f'value="{slug}"' not in body, slug


def test_all_house_and_senate_select_alls(client):
    body = client.get("/subscribe").text
    assert "All Illinois House races" in body
    assert "All Illinois Senate races" in body
    # rendered alongside the district search, not instead of it
    assert body.count('class="race-q"') == 2
    assert 'class="group-all" data-group="ilhouse"' in body
    assert 'class="group-all" data-group="ilsenate"' in body


def test_following_all_house_races(client):
    resp = client.post("/api/subscribe", json={
        "accepts_terms": True, "email": "house@example.org", "wants_email": True,
        "all_groups": ["ilhouse", "ilsenate"],
    })
    assert resp.status_code == 200
    with db.session_scope() as s:
        assert {x.all_group for x in s.scalars(select(Subscription))} == {"ilhouse", "ilsenate"}

    # comes back checked, and the section opens so it isn't hidden
    sid = None
    with db.session_scope() as s:
        sid = s.scalars(select(Subscriber)).one().id
    page = client.get(f"/manage?token={tokens.make_token(sid, 'manage')}").text
    assert '<details class="race-group" data-group="ilhouse" open>' in page
    # The checkbox itself must come back checked — the attribute sits on the
    # line after data-group, so this has to match across the newline.
    for group in ("ilhouse", "ilsenate"):
        tag = re.search(rf'<input[^>]*data-group="{group}"[^>]*>', page, re.S)
        assert tag and "checked" in tag.group(0), group
    unchecked = re.search(r'<input[^>]*data-group="statewide"[^>]*>', page, re.S)
    assert unchecked and "checked" not in unchecked.group(0)

    # Re-saving from manage without changing anything must preserve the follows.
    resp = client.post("/api/manage", json={
        "token": tokens.make_token(sid, "manage"), "wants_email": True,
        "all_groups": ["ilhouse", "ilsenate"],
    })
    assert resp.status_code == 200
    with db.session_scope() as s:
        kept = {x.all_group for x in s.scalars(select(Subscription)) if x.all_group}
        assert kept == {"ilhouse", "ilsenate"}
