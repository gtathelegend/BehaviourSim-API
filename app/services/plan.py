"""Plan management service providing plan lookups and initial free plan seeding."""

import logging
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.plan import Plan

logger = logging.getLogger("behaviorsim_api.services.plan")

FREE_PLAN_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

FREE_PLAN_DEFAULTS = {
    "name": "free",
    "monthly_requests": 100,
    "monthly_interactions": 10000,
    "max_interactions_per_request": 1000,
    "requests_per_minute": 5,
    "max_concurrent_simulations": 1,
    "max_api_keys": 1,
    "calibration_enabled": False,
    "large_exports_enabled": False,
}


def get_or_create_free_plan(db: Session) -> Plan:
    """Retrieve or seed the default 'free' plan."""
    stmt = select(Plan).where(Plan.name == "free")
    plan = db.scalars(stmt).first()
    if plan:
        return plan

    logger.info("Seeding default 'free' plan into database")
    plan = Plan(
        id=FREE_PLAN_ID,
        **FREE_PLAN_DEFAULTS,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def get_plan_by_id(db: Session, plan_id: uuid.UUID) -> Optional[Plan]:
    """Retrieve a plan by its primary key."""
    return db.get(Plan, plan_id)
