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


def test_index_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "District 10b" in resp.text
    assert "CPS Board President" in resp.text


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
