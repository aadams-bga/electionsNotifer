import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr, Field
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import func, or_, select

from ..config import get_settings
from ..db import session_scope
from ..models import (
    Committee,
    FeedItem,
    PollerState,
    PushSubscription,
    Race,
    Subscriber,
    Subscription,
    utcnow,
)
from ..notify import tokens
from ..notify.emailer import send_email

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="ISBE Filing Notifier", docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"detail": "Too many requests; try later."})


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'"
    )
    return response


def _races(session) -> list[Race]:
    return list(session.scalars(select(Race).order_by(Race.sort_order)))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    settings = get_settings()
    with session_scope() as session:
        races = _races(session)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "races": races,
                "site_name": settings.site_name,
                "vapid_public_key": settings.vapid_public_key,
            },
        )


@app.get("/healthz")
def healthz():
    with session_scope() as session:
        state = session.get(PollerState, 1)
        last = state.last_success_at if state else None
        stale = last is None or (datetime.now(UTC) - last) > timedelta(minutes=10)
        return {
            "ok": True,
            "poller_last_success": last.isoformat() if last else None,
            "poller_stale": stale,
        }


class SubscribeRequest(BaseModel):
    email: EmailStr | None = None
    wants_email: bool = False
    wants_push: bool = False
    race_slugs: list[str] = Field(default_factory=list, max_length=50)
    committee_ids: list[int] = Field(default_factory=list, max_length=100)


@app.post("/api/subscribe")
@limiter.limit("10/hour")
def subscribe(request: Request, payload: SubscribeRequest):
    if payload.wants_email and not payload.email:
        raise HTTPException(400, "Email address required for email notifications.")
    if not payload.wants_email and not payload.wants_push:
        raise HTTPException(400, "Choose email notifications, push notifications, or both.")
    if not payload.race_slugs and not payload.committee_ids:
        raise HTTPException(400, "Choose at least one race or committee to follow.")

    with session_scope() as session:
        races = session.scalars(select(Race).where(Race.slug.in_(payload.race_slugs))).all()
        committees = (
            session.scalars(select(Committee).where(Committee.id.in_(payload.committee_ids))).all()
            if payload.committee_ids
            else []
        )
        if len(races) != len(set(payload.race_slugs)):
            raise HTTPException(400, "Unknown race.")
        if len(committees) != len(set(payload.committee_ids)):
            raise HTTPException(400, "Unknown committee.")

        subscriber = None
        needs_verification = False
        if payload.email:
            email = payload.email.lower()
            subscriber = session.scalars(
                select(Subscriber).where(Subscriber.email == email)
            ).first()
            if subscriber is None:
                subscriber = Subscriber(email=email)
                session.add(subscriber)
                session.flush()
            needs_verification = payload.wants_email and subscriber.email_verified_at is None
        else:
            subscriber = Subscriber(email=None)
            session.add(subscriber)
            session.flush()

        existing = {
            (s.race_id, s.committee_id): s
            for s in session.scalars(
                select(Subscription).where(Subscription.subscriber_id == subscriber.id)
            )
        }
        for race in races:
            key = (race.id, None)
            sub = existing.get(key) or Subscription(subscriber_id=subscriber.id, race_id=race.id)
            sub.wants_email = sub.wants_email or payload.wants_email
            sub.wants_push = sub.wants_push or payload.wants_push
            session.add(sub)
        for committee in committees:
            key = (None, committee.id)
            sub = existing.get(key) or Subscription(
                subscriber_id=subscriber.id, committee_id=committee.id
            )
            sub.wants_email = sub.wants_email or payload.wants_email
            sub.wants_push = sub.wants_push or payload.wants_push
            session.add(sub)

        subscriber_id = subscriber.id
        if needs_verification:
            send_email(
                subscriber.email,
                f"Confirm your {get_settings().site_name} subscription",
                "Click the link below to confirm you want filing alerts.\n\n"
                f"{tokens.verify_url(subscriber_id)}\n\n"
                "If you didn't sign up, ignore this email and you won't hear from us again.",
                None,
                subscriber_id,
            )

    return {
        "ok": True,
        "needs_verification": needs_verification,
        "manage_token": tokens.make_token(subscriber_id, "manage"),
    }


@app.get("/verify", response_class=HTMLResponse)
def verify(request: Request, token: str):
    subscriber_id = tokens.read_token(token, "verify", tokens.VERIFY_MAX_AGE)
    with session_scope() as session:
        subscriber = session.get(Subscriber, subscriber_id) if subscriber_id else None
        if subscriber is None:
            return _message(request, "That link is invalid or has expired.", error=True)
        if subscriber.email_verified_at is None:
            subscriber.email_verified_at = utcnow()
    return _message(
        request, "You're confirmed! You'll get an alert the next time a report is filed."
    )


@app.get("/unsubscribe", response_class=HTMLResponse)
@app.post("/unsubscribe", response_class=HTMLResponse)
def unsubscribe(request: Request, token: str):
    subscriber_id = tokens.read_token(token, "unsubscribe", tokens.UNSUBSCRIBE_MAX_AGE)
    with session_scope() as session:
        subscriber = session.get(Subscriber, subscriber_id) if subscriber_id else None
        if subscriber is None:
            return _message(request, "That link is invalid.", error=True)
        session.delete(subscriber)  # cascades to subscriptions + push subscriptions
    return _message(request, "You've been unsubscribed from all alerts.")


@app.get("/manage", response_class=HTMLResponse)
def manage(request: Request, token: str):
    subscriber_id = tokens.read_token(token, "manage", tokens.MANAGE_MAX_AGE)
    with session_scope() as session:
        subscriber = session.get(Subscriber, subscriber_id) if subscriber_id else None
        if subscriber is None:
            return _message(request, "That link is invalid or has expired.", error=True)
        races = _races(session)
        subs = session.scalars(
            select(Subscription).where(Subscription.subscriber_id == subscriber.id)
        ).all()
        followed_committees = [
            {"id": s.committee.id, "name": s.committee.name} for s in subs if s.committee
        ]
        return templates.TemplateResponse(
            request,
            "manage.html",
            {
                "site_name": get_settings().site_name,
                "token": token,
                "email": subscriber.email,
                "races": races,
                "selected_race_slugs": {s.race.slug for s in subs if s.race},
                "followed_committees": followed_committees,
                "wants_email": any(s.wants_email for s in subs),
                "wants_push": any(s.wants_push for s in subs),
                "vapid_public_key": get_settings().vapid_public_key,
            },
        )


class ManageRequest(SubscribeRequest):
    token: str


@app.post("/api/manage")
@limiter.limit("30/hour")
def update_subscriptions(request: Request, payload: ManageRequest):
    subscriber_id = tokens.read_token(payload.token, "manage", tokens.MANAGE_MAX_AGE)
    if subscriber_id is None:
        raise HTTPException(403, "Invalid or expired link.")
    with session_scope() as session:
        subscriber = session.get(Subscriber, subscriber_id)
        if subscriber is None:
            raise HTTPException(403, "Invalid or expired link.")
        races = session.scalars(select(Race).where(Race.slug.in_(payload.race_slugs))).all()
        for sub in list(subscriber.subscriptions):
            session.delete(sub)
        session.flush()
        for race in races:
            session.add(
                Subscription(
                    subscriber_id=subscriber.id,
                    race_id=race.id,
                    wants_email=payload.wants_email,
                    wants_push=payload.wants_push,
                )
            )
        for cid in payload.committee_ids:
            if session.get(Committee, cid):
                session.add(
                    Subscription(
                        subscriber_id=subscriber.id,
                        committee_id=cid,
                        wants_email=payload.wants_email,
                        wants_push=payload.wants_push,
                    )
                )
    return {"ok": True}


class PushSubscribeRequest(BaseModel):
    token: str
    endpoint: str = Field(max_length=2000)
    p256dh: str = Field(max_length=500)
    auth: str = Field(max_length=500)


@app.post("/api/push/subscribe")
@limiter.limit("30/hour")
def push_subscribe(request: Request, payload: PushSubscribeRequest):
    subscriber_id = tokens.read_token(payload.token, "manage", tokens.MANAGE_MAX_AGE)
    if subscriber_id is None:
        raise HTTPException(403, "Invalid or expired token.")
    with session_scope() as session:
        if session.get(Subscriber, subscriber_id) is None:
            raise HTTPException(403, "Invalid token.")
        existing = session.scalars(
            select(PushSubscription).where(PushSubscription.endpoint == payload.endpoint)
        ).first()
        if existing:
            existing.subscriber_id = subscriber_id
            existing.p256dh = payload.p256dh
            existing.auth = payload.auth
        else:
            session.add(
                PushSubscription(
                    subscriber_id=subscriber_id,
                    endpoint=payload.endpoint,
                    p256dh=payload.p256dh,
                    auth=payload.auth,
                    user_agent=request.headers.get("user-agent", "")[:500],
                )
            )
    return {"ok": True}


@app.get("/api/committees")
@limiter.limit("60/hour")
def search_committees(request: Request, q: str = ""):
    q = q.strip()
    if len(q) < 2:
        return {"results": []}
    with session_scope() as session:
        clauses = [Committee.name.ilike(f"%{q}%")]
        if q.isdigit():
            clauses.append(Committee.id == int(q))
        rows = session.scalars(
            select(Committee).where(or_(*clauses)).order_by(Committee.name).limit(20)
        ).all()
        return {
            "results": [
                {"id": c.id, "name": c.name, "type": c.committee_type, "status": c.status}
                for c in rows
            ]
        }


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, token: str = ""):
    settings = get_settings()
    if not settings.admin_token or token != settings.admin_token:
        raise HTTPException(404)
    with session_scope() as session:
        state = session.get(PollerState, 1)
        counts = {
            "subscribers": session.scalar(select(func.count(Subscriber.id))),
            "subscriptions": session.scalar(select(func.count(Subscription.id))),
            "push_subscriptions": session.scalar(select(func.count(PushSubscription.id))),
            "committees": session.scalar(select(func.count(Committee.id))),
            "feed_items": session.scalar(select(func.count(FeedItem.guid_seq))),
        }
        recent = session.scalars(
            select(FeedItem).order_by(FeedItem.guid_seq.desc()).limit(25)
        ).all()
        return templates.TemplateResponse(
            request,
            "admin.html",
            {
                "site_name": settings.site_name,
                "state": state,
                "counts": counts,
                "recent": recent,
            },
        )


def _message(request: Request, text: str, error: bool = False) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "message.html",
        {"site_name": get_settings().site_name, "message": text, "error": error},
        status_code=400 if error else 200,
    )
