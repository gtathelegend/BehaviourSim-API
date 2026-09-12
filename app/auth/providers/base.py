"""Base abstractions and schemas for OAuth providers."""

from abc import ABC, abstractmethod
from typing import Optional
from pydantic import BaseModel, EmailStr


class ProviderIdentity(BaseModel):
    """Normalized user identity returned from an external OAuth provider."""

    provider: str
    provider_subject: str
    email: EmailStr
    display_name: Optional[str] = None


class OAuthProvider(ABC):
    """Abstract interface representing an external OAuth 2.0 identity provider."""

    @abstractmethod
    def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        """Construct external provider authorization redirect URL."""
        pass

    @abstractmethod
    async def exchange_code(self, code: str, redirect_uri: str) -> str:
        """Exchange authorization code for access token."""
        pass

    @abstractmethod
    async def get_identity(self, access_token: str) -> ProviderIdentity:
        """Fetch and normalize user identity using the access token."""
        pass
