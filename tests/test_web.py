import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import isbe_notifier.db as db
from isbe_notifier.models import Base, Committee, PushSubscription, Subscriber, Subscription
from isbe_notifier.notify import tokens
from isbe_notifier.seeds import cps_races
from isbe_notifier.web import app as webapp_module
from isbe_notifier.web.app import app


@pytest.fixture
def client(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))

    from isbe_notifier.models import Race

    with db.session_scope() as s:
        for data in cps_races():
            s.add(Race(**data))
        s.add(Committee(id=12345, name="Friends for a Better Chicago"))

    # No rate limiting in tests
    app.state.limiter.enabled = False
    sent = []
    monkeypatch.setattr(
        webapp_module, "send_email",
        lambda to, subject, body, link, sid: sent.append((to, subject, body)),
    )
    c = TestClient(app)
    c.sent_emails = sent
    return c


def _extract_token(text: str, purpose: str) -> str:
    m = re.search(rf"/{purpose}\?token=([\w.\-_]+)", text)
    assert m, f"no {purpose} link in: {text}"
    return m.group(1)


def test_landing_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Today's reports" in resp.text
    assert "Sign up for alerts" in resp.text
    # empty state links to the statewide view
    assert "No reports tied to a CPS Board race yet today" in resp.text
    assert client.get("/?scope=all").status_code == 200


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


def test_subscribe_page_renders(client):
    resp = client.get("/subscribe")
    assert resp.status_code == 200
    assert "District 10b" in resp.text
    assert "CPS Board President" in resp.text
    assert "All CPS Board races" in resp.text
    assert "Daily summary" in resp.text


def test_login_flow(client):
    client.post("/api/subscribe", json={
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
        "wants_push": True, "race_slugs": ["d1a"], "wants_daily_digest": True,
    }).status_code == 400


def test_manage_updates_flags_and_digests(client):
    client.post("/api/subscribe", json={
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


def test_signup_validation(client):
    assert client.post("/api/subscribe", json={
        "wants_email": True, "race_slugs": ["president"],
    }).status_code == 400  # email channel without address
    assert client.post("/api/subscribe", json={
        "email": "x@example.org", "wants_email": True, "race_slugs": [],
    }).status_code == 400  # nothing followed
    assert client.post("/api/subscribe", json={
        "email": "x@example.org", "wants_email": True, "race_slugs": ["not-a-race"],
    }).status_code == 400
    assert client.post("/api/subscribe", json={
        "email": "not-an-email", "wants_email": True, "race_slugs": ["president"],
    }).status_code == 422


def test_signup_idempotent_for_existing_email(client):
    for _ in range(2):
        resp = client.post("/api/subscribe", json={
            "email": "again@example.org", "wants_email": True, "race_slugs": ["d1a"],
        })
        assert resp.status_code == 200
    with db.session_scope() as s:
        subs = s.scalars(select(Subscription)).all()
        assert len(subs) == 1  # not duplicated


def test_manage_and_unsubscribe(client):
    client.post("/api/subscribe", json={
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


def test_push_only_signup(client):
    resp = client.post("/api/subscribe", json={
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


def test_firehose_signup(client):
    resp = client.post("/api/subscribe", json={
        "email": "hose@example.org", "wants_email": True, "all_filings": True,
    })
    assert resp.status_code == 200
    with db.session_scope() as s:
        sub = s.scalars(select(Subscription)).one()
        assert sub.all_filings is True
        assert sub.race_id is None and sub.committee_id is None
