"""Usage accounting and event database models."""

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.user import User


def utc_now() -> datetime:
    """Return current UTC datetime with timezone awareness."""
    return datetime.now(timezone.utc)


class MonthlyUsage(Base):
    """Aggregate monthly usage record per user."""

    __tablename__ = "monthly_usage"
    __table_args__ = (
        UniqueConstraint("user_id", "period_start", name="uq_monthly_usage_user_period"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    request_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    interaction_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    # Relationships
    user: Mapped["User"] = relationship(
        "User",
        back_populates="monthly_usages",
    )


class UsageEvent(Base):
    """Append-only log of simulation and API usage events for audit and billing."""

    __tablename__ = "usage_events"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    api_key_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("api_keys.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
    )
    request_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        index=True,
    )
    interaction_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    compute_ms: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )
    success: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
        index=True,
    )
    metadata_json: Mapped[Optional[str]] = mapped_column(
        String(1000),
        nullable=True,
    )

    # Relationships
    user: Mapped["User"] = relationship(
        "User",
        back_populates="usage_events",
    )
