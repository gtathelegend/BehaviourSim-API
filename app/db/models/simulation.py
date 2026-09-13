"""Simulation database model for durable run metadata and result persistence."""

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.user import User


def utc_now() -> datetime:
    """Return current UTC datetime with timezone awareness."""
    return datetime.now(timezone.utc)


class Simulation(Base):
    """Persisted simulation run entity containing execution provenance and bounded result data."""

    __tablename__ = "simulations"

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
    preset: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    num_interactions: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    seed: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )
    profile: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
    )
    initial_state: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False,
        index=True,
    )
    result_storage: Mapped[str] = mapped_column(
        String(20),
        default="database",
        nullable=False,
    )
    result_location: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )
    # Uses JSON type which maps to native JSONB on PostgreSQL and JSON/TEXT on SQLite
    data: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=True,
    )
    behaviorsim_version: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    api_version: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    compute_ms: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )
    reproducible: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    error_code: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
    )
    error_message: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
        index=True,
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    # Relationship back to User
    user: Mapped["User"] = relationship(
        "User",
        back_populates="simulations",
    )
