"""Account profile and management endpoints."""

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import AuthenticatedPrincipal, get_current_principal
from app.db.models.api_key import APIKey
from app.db.models.auth_identity import AuthIdentity
from app.db.session import get_db

router = APIRouter(tags=["account"])


class AccountResponse(BaseModel):
    """Sanitized account profile response schema."""

    id: uuid.UUID
    email: EmailStr
    display_name: Optional[str] = None
    is_active: bool
    created_at: datetime
    authentication_methods: List[str]
    plan: str


@router.get("/account", response_model=AccountResponse)
async def get_account(
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> AccountResponse:
    """Retrieve profile details and linked authentication methods for the authenticated user."""
    user = principal.user

    # Discover linked identity providers
    ident_stmt = select(AuthIdentity.provider).where(AuthIdentity.user_id == user.id)
    providers = list(db.scalars(ident_stmt).all())

    # Check if user has active API keys
    key_stmt = select(APIKey.id).where(APIKey.user_id == user.id, APIKey.is_active.is_(True))
    has_api_keys = db.scalars(key_stmt).first() is not None

    methods = set(providers)
    if has_api_keys or principal.authentication_method == "api_key":
        methods.add("api_key")

    plan_name = user.plan.name if user.plan else "free"

    return AccountResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_active=user.is_active,
        created_at=user.created_at,
        authentication_methods=sorted(list(methods)),
        plan=plan_name,
    )
