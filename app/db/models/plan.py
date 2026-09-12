"""Plan and entitlement database model."""

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List

from sqlalchemy import Boolean, DateTime, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.user import User


FREE_PLAN_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def utc_now() -> datetime:
    """Return current UTC datetime with timezone awareness."""
    return datetime.now(timezone.utc)


class Plan(Base):
    """Plan entitlement tier defining usage quotas, rate limits, and feature flags."""

    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        index=True,
        nullable=False,
    )
    monthly_requests: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    monthly_interactions: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    max_interactions_per_request: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    requests_per_minute: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    max_concurrent_simulations: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    max_api_keys: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    calibration_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    large_exports_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
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
    users: Mapped[List["User"]] = relationship(
        "User",
        back_populates="plan",
    )
