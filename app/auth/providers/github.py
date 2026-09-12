"""GitHub OAuth 2.0 provider implementation."""

import logging
from urllib.parse import urlencode
import httpx

from app.auth.providers.base import OAuthProvider, ProviderIdentity
from app.core.config import get_settings
from app.core.errors import BehaviorSimAPIError

logger = logging.getLogger("behaviorsim_api.auth.github")


class GitHubOAuthProvider(OAuthProvider):
    """GitHub OAuth 2.0 implementation."""

    AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
    TOKEN_URL = "https://github.com/login/oauth/access_token"
    USER_URL = "https://api.github.com/user"
    EMAILS_URL = "https://api.github.com/user/emails"

    def __init__(self) -> None:
        settings = get_settings()
        self.client_id = settings.GITHUB_CLIENT_ID
        self.client_secret = settings.GITHUB_CLIENT_SECRET

    def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": "read:user user:email",
            "state": state,
        }
        return f"{self.AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: str) -> str:
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }
        headers = {"Accept": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(self.TOKEN_URL, data=data, headers=headers)
                if response.status_code != 200:
                    logger.error("GitHub code exchange failed with status %s", response.status_code)
                    raise BehaviorSimAPIError(
                        message="Failed to exchange authorization code with GitHub.",
                        status_code=400,
                        details={"code": "oauth_exchange_failed"},
                    )
                payload = response.json()
                if "error" in payload:
                    logger.error("GitHub OAuth returned error: %s", payload.get("error_description"))
                    raise BehaviorSimAPIError(
                        message=f"GitHub OAuth error: {payload.get('error')}",
                        status_code=400,
                        details={"code": "oauth_exchange_failed"},
                    )
                access_token = payload.get("access_token")
                if not access_token:
                    raise BehaviorSimAPIError(
                        message="GitHub token response missing access_token.",
                        status_code=400,
                        details={"code": "oauth_exchange_failed"},
                    )
                return access_token
        except httpx.RequestError as exc:
            logger.error("Network error contacting GitHub token endpoint: %s", exc)
            raise BehaviorSimAPIError(
                message="Network error communicating with GitHub authentication service.",
                status_code=502,
                details={"code": "oauth_exchange_failed"},
            ) from exc

    async def get_identity(self, access_token: str) -> ProviderIdentity:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "BehaviorSim-API",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # 1. Fetch user profile
                user_res = await client.get(self.USER_URL, headers=headers)
                if user_res.status_code != 200:
                    logger.error("GitHub user request failed with status %s", user_res.status_code)
                    raise BehaviorSimAPIError(
                        message="Failed to retrieve user profile from GitHub.",
                        status_code=400,
                        details={"code": "oauth_identity_failed"},
                    )
                user_data = user_res.json()
                github_id = user_data.get("id")
                if not github_id:
                    raise BehaviorSimAPIError(
                        message="GitHub user profile missing identifier.",
                        status_code=400,
                        details={"code": "oauth_identity_failed"},
                    )
                provider_subject = str(github_id)
                display_name = user_data.get("name") or user_data.get("login")

                # 2. Fetch user emails to locate primary verified email
                emails_res = await client.get(self.EMAILS_URL, headers=headers)
                if emails_res.status_code != 200:
                    logger.error("GitHub emails request failed with status %s", emails_res.status_code)
                    raise BehaviorSimAPIError(
                        message="Failed to retrieve verified email from GitHub.",
                        status_code=400,
                        details={"code": "oauth_identity_failed"},
                    )
                emails_data = emails_res.json()

                verified_email = None
                for email_entry in emails_data:
                    if email_entry.get("verified") and email_entry.get("primary"):
                        verified_email = email_entry.get("email")
                        break
                if not verified_email:
                    # Fallback to any verified email
                    for email_entry in emails_data:
                        if email_entry.get("verified"):
                            verified_email = email_entry.get("email")
                            break

                if not verified_email:
                    raise BehaviorSimAPIError(
                        message="No verified email address found on GitHub account.",
                        status_code=400,
                        details={"code": "oauth_identity_failed"},
                    )

                return ProviderIdentity(
                    provider="github",
                    provider_subject=provider_subject,
                    email=verified_email,
                    display_name=display_name,
                )
        except httpx.RequestError as exc:
            logger.error("Network error contacting GitHub user endpoints: %s", exc)
            raise BehaviorSimAPIError(
                message="Network error communicating with GitHub API.",
                status_code=502,
                details={"code": "oauth_identity_failed"},
            ) from exc
