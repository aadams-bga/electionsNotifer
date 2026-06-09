from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class FeedItem(Base):
    """One RSS item, deduplicated by the sequence number embedded in the guid fragment."""

    __tablename__ = "feed_items"

    guid_seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    committee_name: Mapped[str] = mapped_column(String(500))
    report_type: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(100), default="")
    url: Mapped[str | None] = mapped_column(Text)  # absent for paper filings
    guid_url: Mapped[str] = mapped_column(Text)
    pub_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    filing: Mapped["Filing | None"] = relationship(back_populates="feed_item", uselist=False)


class Committee(Base):
    __tablename__ = "committees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # plain ISBE committee ID
    name: Mapped[str] = mapped_column(String(500))
    encrypted_id: Mapped[str | None] = mapped_column(String(200), index=True)
    committee_type: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str | None] = mapped_column(String(50))
    purpose: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Filing(Base):
    __tablename__ = "filings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_item_seq: Mapped[int] = mapped_column(
        ForeignKey("feed_items.guid_seq"), unique=True, index=True
    )
    committee_id: Mapped[int | None] = mapped_column(ForeignKey("committees.id"), index=True)
    report_type: Mapped[str] = mapped_column(String(200))
    report_class: Mapped[str] = mapped_column(String(20), index=True)  # A1|B1|D1|D2|OTHER
    is_amendment: Mapped[bool] = mapped_column(Boolean, default=False)
    # Future digest support: amended filings supersede originals so aggregates
    # must be able to exclude the superseded row.
    amends_filing_id: Mapped[int | None] = mapped_column(ForeignKey("filings.id"))
    filed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    feed_item: Mapped[FeedItem] = relationship(back_populates="filing")
    committee: Mapped[Committee | None] = relationship()
    lines: Mapped[list["FilingLine"]] = relationship(
        back_populates="filing", cascade="all, delete-orphan"
    )


class FilingLine(Base):
    """A single contribution (A-1) or expenditure (B-1) row."""

    __tablename__ = "filing_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # contribution | expenditure
    name: Mapped[str] = mapped_column(String(500))
    address: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    line_date: Mapped[date | None] = mapped_column(Date)
    description: Mapped[str | None] = mapped_column(Text)  # A-1 "Description" column
    purpose: Mapped[str | None] = mapped_column(Text)  # B-1 "Purpose"
    supporting_opposing: Mapped[str | None] = mapped_column(String(50))
    candidate_name: Mapped[str | None] = mapped_column(String(500))
    office_district: Mapped[str | None] = mapped_column(String(500), index=True)
    vendor_name: Mapped[str | None] = mapped_column(String(500))
    vendor_address: Mapped[str | None] = mapped_column(Text)

    filing: Mapped[Filing] = relationship(back_populates="lines")


class Race(Base):
    """A subscribable race, e.g. CPS Board President or District 4a."""

    __tablename__ = "races"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True)
    label: Mapped[str] = mapped_column(String(200))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # Case-insensitive substrings matched against B-1 "Office - District" values.
    office_district_patterns: Mapped[list] = mapped_column(JSON, default=list)


class RaceCommittee(Base):
    """Whitelist mapping a committee to a race (user-provided)."""

    __tablename__ = "race_committees"
    __table_args__ = (UniqueConstraint("race_id", "committee_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("races.id"), index=True)
    committee_id: Mapped[int] = mapped_column(ForeignKey("committees.id"), index=True)

    race: Mapped[Race] = relationship()
    committee: Mapped[Committee] = relationship()


class Subscriber(Base):
    __tablename__ = "subscribers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Nullable: push-only subscribers have no email address.
    email: Mapped[str | None] = mapped_column(String(320), unique=True, index=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    subscriptions: Mapped[list["Subscription"]] = relationship(
        back_populates="subscriber", cascade="all, delete-orphan"
    )
    push_subscriptions: Mapped[list["PushSubscription"]] = relationship(
        back_populates="subscriber", cascade="all, delete-orphan"
    )


class Subscription(Base):
    """A subscriber following either a race or a specific committee."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("subscriber_id", "race_id", "committee_id", name="uq_sub_target"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscriber_id: Mapped[int] = mapped_column(ForeignKey("subscribers.id"), index=True)
    race_id: Mapped[int | None] = mapped_column(ForeignKey("races.id"), index=True)
    committee_id: Mapped[int | None] = mapped_column(ForeignKey("committees.id"), index=True)
    wants_email: Mapped[bool] = mapped_column(Boolean, default=True)
    wants_push: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    subscriber: Mapped[Subscriber] = relationship(back_populates="subscriptions")
    race: Mapped[Race | None] = relationship()
    committee: Mapped[Committee | None] = relationship()


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscriber_id: Mapped[int] = mapped_column(ForeignKey("subscribers.id"), index=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(Text)
    auth: Mapped[str] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)

    subscriber: Mapped[Subscriber] = relationship(back_populates="push_subscriptions")


class Notification(Base):
    """Audit log and idempotency guard: one row per (subscriber, filing, channel)."""

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("subscriber_id", "filing_id", "channel", name="uq_notif_once"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscriber_id: Mapped[int] = mapped_column(ForeignKey("subscribers.id"), index=True)
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.id"), index=True)
    channel: Mapped[str] = mapped_column(String(20))  # email | push
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|sent|failed
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PollerState(Base):
    """Single-row table: poller heartbeat and high-water mark."""

    __tablename__ = "poller_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_guid_seq: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)
