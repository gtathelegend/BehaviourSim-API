"""Developer API key management endpoints."""

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import AuthenticatedPrincipal, get_current_principal
from app.db.session import get_db
from app.services.api_key import (
    APIKeyCreateResult,
    create_api_key,
    list_api_keys,
    revoke_api_key,
)

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


class APIKeyMetadataResponse(BaseModel):
    """API key metadata response excluding raw secret."""

    id: uuid.UUID
    name: str
    key_prefix: str
    is_active: bool
    created_at: datetime
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


class CreateAPIKeyRequest(BaseModel):
    """Request payload for creating a new developer API key."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Descriptive identifier for the API key (e.g. 'Production Client', 'CI/CD Pipeline').",
    )


class RevokeAPIKeyResponse(BaseModel):
    """Confirmation response upon revoking an API key."""

    status: str = "revoked"
    id: uuid.UUID


@router.get("", response_model=List[APIKeyMetadataResponse])
async def list_user_api_keys(
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> List[APIKeyMetadataResponse]:
    """List all API keys belonging to the authenticated user.
    
    Returns key metadata only. Raw secret keys are never returned after initial creation.
    """
    keys = list_api_keys(db=db, user=principal.user)
    return [
        APIKeyMetadataResponse(
            id=k.id,
            name=k.name,
            key_prefix=k.key_prefix,
            is_active=k.is_active,
            created_at=k.created_at,
            last_used_at=k.last_used_at,
            revoked_at=k.revoked_at,
        )
        for k in keys
    ]


@router.post("", response_model=APIKeyCreateResult, status_code=status.HTTP_201_CREATED)
async def create_user_api_key(
    payload: CreateAPIKeyRequest,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> APIKeyCreateResult:
    """Create a new developer API key for the authenticated user.
    
    The raw API key string is returned in the response exactly once and is never stored in plaintext.
    Enforces the active API key quota defined by the user's plan.
    """
    return create_api_key(db=db, user=principal.user, name=payload.name)


@router.delete("/{key_id}", response_model=RevokeAPIKeyResponse)
async def revoke_user_api_key(
    key_id: uuid.UUID,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> RevokeAPIKeyResponse:
    """Revoke an existing API key owned by the authenticated user.
    
    Revocation immediately disables the key from authenticating future requests.
    """
    revoked = revoke_api_key(db=db, user=principal.user, key_id=key_id)
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"API key '{key_id}' not found or does not belong to the authenticated user.",
        )
    return RevokeAPIKeyResponse(id=revoked.id)
