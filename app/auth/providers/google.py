"""Google OAuth 2.0 provider implementation."""

import logging
from urllib.parse import urlencode
import httpx

from app.auth.providers.base import OAuthProvider, ProviderIdentity
from app.core.config import get_settings
from app.core.errors import BehaviorSimAPIError

logger = logging.getLogger("behaviorsim_api.auth.google")


class GoogleOAuthProvider(OAuthProvider):
    """Google OAuth 2.0 implementation."""

    AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

    def __init__(self) -> None:
        settings = get_settings()
        self.client_id = settings.GOOGLE_CLIENT_ID
        self.client_secret = settings.GOOGLE_CLIENT_SECRET

    def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "online",
            "prompt": "select_account",
        }
        return f"{self.AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: str) -> str:
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(self.TOKEN_URL, data=data)
                if response.status_code != 200:
                    logger.error("Google code exchange failed with status %s", response.status_code)
                    raise BehaviorSimAPIError(
                        message="Failed to exchange authorization code with Google.",
                        status_code=400,
                        details={"code": "oauth_exchange_failed"},
                    )
                payload = response.json()
                access_token = payload.get("access_token")
                if not access_token:
                    raise BehaviorSimAPIError(
                        message="Google token response missing access_token.",
                        status_code=400,
                        details={"code": "oauth_exchange_failed"},
                    )
                return access_token
        except httpx.RequestError as exc:
            logger.error("Network error contacting Google token endpoint: %s", exc)
            raise BehaviorSimAPIError(
                message="Network error communicating with Google authentication service.",
                status_code=502,
                details={"code": "oauth_exchange_failed"},
            ) from exc

    async def get_identity(self, access_token: str) -> ProviderIdentity:
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(self.USERINFO_URL, headers=headers)
                if response.status_code != 200:
                    logger.error("Google userinfo request failed with status %s", response.status_code)
                    raise BehaviorSimAPIError(
                        message="Failed to retrieve user profile from Google.",
                        status_code=400,
                        details={"code": "oauth_identity_failed"},
                    )
                data = response.json()
                sub = data.get("sub")
                email = data.get("email")
                email_verified = data.get("email_verified", False)

                if not sub or not email:
                    raise BehaviorSimAPIError(
                        message="Google identity response missing required subject or email.",
                        status_code=400,
                        details={"code": "oauth_identity_failed"},
                    )
                if not email_verified:
                    raise BehaviorSimAPIError(
                        message="Google email address is not verified.",
                        status_code=400,
                        details={"code": "oauth_identity_failed"},
                    )

                return ProviderIdentity(
                    provider="google",
                    provider_subject=sub,
                    email=email,
                    display_name=data.get("name"),
                )
        except httpx.RequestError as exc:
            logger.error("Network error contacting Google userinfo: %s", exc)
            raise BehaviorSimAPIError(
                message="Network error communicating with Google userinfo service.",
                status_code=502,
                details={"code": "oauth_identity_failed"},
            ) from exc
