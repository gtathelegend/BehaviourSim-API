"""User database model."""

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.plan import FREE_PLAN_ID

if TYPE_CHECKING:
    from app.db.models.auth_identity import AuthIdentity
    from app.db.models.api_key import APIKey
    from app.db.models.plan import Plan
    from app.db.models.session import UserSession
    from app.db.models.usage import MonthlyUsage, UsageEvent


def utc_now() -> datetime:
    """Return current UTC datetime with timezone awareness."""
    return datetime.now(timezone.utc)


class User(Base):
    """User account entity representing a registered developer or organization."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
        nullable=False,
    )
    display_name: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("plans.id"),
        default=FREE_PLAN_ID,
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
    identities: Mapped[List["AuthIdentity"]] = relationship(
        "AuthIdentity",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    api_keys: Mapped[List["APIKey"]] = relationship(
        "APIKey",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    sessions: Mapped[List["UserSession"]] = relationship(
        "UserSession",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    plan: Mapped["Plan"] = relationship(
        "Plan",
        back_populates="users",
    )
    monthly_usages: Mapped[List["MonthlyUsage"]] = relationship(
        "MonthlyUsage",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    usage_events: Mapped[List["UsageEvent"]] = relationship(
        "UsageEvent",
        back_populates="user",
        cascade="all, delete-orphan",
    )
