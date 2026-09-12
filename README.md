# BehaviorSim API

Production-quality hosted REST API for the **BehaviorSim** simulation engine.

## 1. Overview

BehaviorSim API provides a scalable backend service layer for running behavioral simulations, managing simulation profiles, executing distributed runs, and serving results to web clients and external integrations.

## 2. Relationship to BehaviorSim Core Package

BehaviorSim API is a separate application that acts as a consumer of the published [`behaviorsim`](https://pypi.org/project/behaviorsim/) package (`1.0.1`).

* **BehaviorSim Engine (`behaviorsim`)**: Contains the core algorithmic logic (`Simulator`, `State`, `Profile`, `FeatureDistribution`, `TransitionRule`, `SimulationConfig`).
* **BehaviorSim API (`behaviorsim-api`)**: Manages HTTP endpoints, authentication, configuration, scheduling, job queues, persistence, and external client interaction.

The API repository consumes `behaviorsim` as an external dependency and does not re-implement or modify simulation logic.

## 3. Installation

### Prerequisites

* Python `>= 3.9` (Python 3.10+ recommended)
* `pip` or [`uv`](https://github.com/astral-sh/uv)

### Install Dependencies

Using `pip`:

```bash
pip install -e ".[dev]"
```

Or using `uv`:

```bash
uv pip install -e ".[dev]"
```

## 4. Environment Configuration

Copy `.env.example` to create your local `.env`:

```bash
cp .env.example .env
```

### Configurable Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `APP_ENV` | `development` | Environment mode (`development`, `testing`, `production`) |
| `APP_NAME` | `BehaviorSim API` | Human-readable service title |
| `API_VERSION` | `0.1.0` | Semantic version of the API |
| `API_BASE_URL` | `http://localhost:8000` | Base URL where the API is hosted |
| `WEB_BASE_URL` | `http://localhost:3000` | Base URL of the frontend web application |
| `DATABASE_URL` | `postgresql+psycopg://...` | Connection URI for the database (PostgreSQL with psycopg3 driver) |
| `API_KEY_PREFIX` | `bs_live_` | Standard prefix for developer API keys |
| `SESSION_TOKEN_PREFIX` | `bs_sess_` | Standard prefix for user session tokens |
| `GOOGLE_CLIENT_ID` | `""` | Google Cloud OAuth 2.0 Web Client ID |
| `GOOGLE_CLIENT_SECRET` | `""` | Google Cloud OAuth 2.0 Client Secret |
| `GITHUB_CLIENT_ID` | `""` | GitHub OAuth App Client ID |
| `GITHUB_CLIENT_SECRET` | `""` | GitHub OAuth App Client Secret |
| `OAUTH_REDIRECT_BASE_URL` | `http://localhost:8000` | Base URL for OAuth callback redirects |
| `AUTH_SESSION_COOKIE_NAME` | `behaviorsim_session` | Name of the authenticated session cookie |
| `AUTH_SESSION_MAX_AGE_SECONDS` | `604800` (7 days) | Session cookie and token lifespan |
| `AUTH_OAUTH_STATE_COOKIE_NAME` | `behaviorsim_oauth_state` | Cookie name for OAuth CSRF state verification |
| `AUTH_OAUTH_STATE_MAX_AGE_SECONDS` | `600` (10 minutes) | OAuth state cookie lifespan |
| `CORS_ORIGINS` | `http://localhost:3000,http://localhost:8000` | Comma-separated list of allowed CORS origins |
| `LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

No external cloud secrets or third-party credentials are required for local development and testing.

## 5. Running the API Locally

Start the local development server with Uvicorn:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

When running in development mode, interactive API documentation is available at:
* Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)
* ReDoc: [http://localhost:8000/redoc](http://localhost:8000/redoc)

## 6. Running Tests

Run the test suite with Pytest:

```bash
pytest
```

To run with verbose output:

```bash
pytest -v
```

All tests execute self-contained against an isolated in-memory test database and do not require external PostgreSQL or cloud infrastructure. All external OAuth provider HTTP calls are mocked.

## 7. Health Check Endpoint

An infrastructure-level health check endpoint is provided for container orchestrators, load balancers, and monitoring systems:

```http
GET /health
```

**Response (HTTP 200 OK):**

```json
{
  "status": "ok",
  "version": "0.1.0"
}
```

The health check does not require authentication or an active database connection.

## 8. Database Architecture & Migrations

The persistence layer uses **SQLAlchemy 2.0** declarative models managed by **Alembic** migrations.

### Schema Models

1. **`users`**:
   - `id`: UUID (v4) primary key.
   - `email`: Unique indexed user email.
   - `display_name`: Optional user display name.
   - `is_active`: Status flag for account authorization.
   - `created_at`, `updated_at`: UTC timestamps.

2. **`auth_identities`**:
   - Represents external authentication providers (`google`, `github`).
   - Compound unique constraint on `(provider, provider_subject)`.
   - Explicit foreign key relationship to `users` (`ondelete="CASCADE"`).
   - No access tokens or refresh tokens are persisted.

3. **`api_keys`**:
   - `id`: UUID primary key.
   - `user_id`: Foreign key to owning user (`ondelete="CASCADE"`).
   - `name`: Human-readable identifier (e.g., "Production Backend").
   - `key_prefix`: Short 16-character public prefix (`bs_live_xxxxxxxx`) for fast indexed lookup and audit logging.
   - `key_hash`: SHA-256 cryptographic digest of the raw secret.
   - `is_active`, `created_at`, `last_used_at`, `revoked_at`: Key lifecycle tracking.

4. **`user_sessions`**:
   - `id`: UUID primary key.
   - `user_id`: Foreign key to owning user (`ondelete="CASCADE"`).
   - `token_hash`: SHA-256 cryptographic digest of session token (`bs_sess_...`).
   - `created_at`, `expires_at`, `revoked_at`: Session expiration and revocation tracking.

### Database Migrations

Run database migrations to latest revision:

```bash
alembic upgrade head
```

Roll back migrations:

```bash
alembic downgrade base
```

## 9. Authentication & Security Architecture

The API supports dual authentication mechanisms resolving to a unified `AuthenticatedPrincipal`:

```text
Browser User (OAuth)                      Developer Client
       │                                         │
       ▼                                         ▼
HttpOnly Cookie / Bearer bs_sess_...       Authorization: Bearer bs_live_...
       │                                         │
       ▼                                         ▼
Session Validation                        API Key Validation
       └───────────────────┬─────────────────────┘
                           ▼
                AuthenticatedPrincipal
                           │
                 /v1/account Endpoint
```

### OAuth 2.0 Flow (Google & GitHub)

1. **Initiation (`GET /v1/auth/{provider}`)**:
   - Generates a 256-bit cryptographically secure state parameter.
   - Sets a short-lived `HttpOnly`, `SameSite=Lax` state cookie (`behaviorsim_oauth_state`).
   - Redirects to provider's authorization screen.
2. **Callback (`GET /v1/auth/{provider}/callback`)**:
   - Validates `state` query parameter against state cookie (timing-attack safe).
   - Deletes state cookie immediately (single-use guarantee).
   - Exchanges `code` for an access token via provider's token endpoint.
   - Retrieves verified user identity (sub, verified email, display name).
   - Resolves or creates user and external `AuthIdentity`.
   - Issues a high-entropy session token (`bs_sess_<secret>`) and stores its SHA-256 digest in `user_sessions`.
   - Sets `behaviorsim_session` cookie (`HttpOnly`, `SameSite=Lax`, `Secure` in production) and redirects to configured frontend.

### Account Resolution & Anti-Takeover Policy

- **Existing Identity**: If `(provider, provider_subject)` exists, resolves existing user.
- **New Identity**: If no matching identity or email exists, creates `User` + `AuthIdentity` atomically.
- **Email Collision (Strict Anti-Takeover)**: If an incoming OAuth identity provides an email matching an existing account not linked to this provider, automatic merging is **strictly rejected** (`409 Conflict`). Users must sign in via their original identity to link accounts.

### Session Management & Logout

- **Logout (`POST /v1/auth/logout`)**: Marks the session revoked in the database and clears client cookie.
- **API Key Independence**: Logging out of a browser session does not revoke developer API keys.

### Account Profile Endpoint

```http
GET /v1/account
```

Requires authentication (via cookie, Bearer session token, or developer API key).

**Response (HTTP 200 OK):**

```json
{
  "id": "123e4567-e89b-12d3-a456-426614174000",
  "email": "developer@example.com",
  "display_name": "Developer User",
  "is_active": true,
  "created_at": "2026-09-12T22:00:00Z",
  "authentication_methods": ["github", "api_key"]
}
```

## 10. What is Implemented

* **Phase 0 Foundation**: FastAPI application factory, logging, settings, `/health` endpoint, `behaviorsim==1.0.1` startup check.
* **Phase 1 Database Foundation**: SQLAlchemy 2.0 declarative models (`Base`), lazy engine creation, request-scoped sessions (`get_db`), and Alembic migrations (`0001_initial_auth_tables`, `0002_user_sessions`).
* **Phase 2 API Key Foundation**: Developer API-key generation/hashing/verification, API-key lifecycle service (`create`, `list`, `revoke`, `validate`).
* **Phase 3 OAuth & Account Foundation**: Google & GitHub OAuth 2.0 flows, CSRF state protection, anti-takeover account linking, server-side session management (`user_sessions`), `/v1/auth/logout`, `/v1/account`, and unified `AuthenticatedPrincipal`.

## 11. Intentionally Not Implemented in Phase 3

The following capabilities are reserved for subsequent phases:
* Rate limiting and quota management
* Simulation execution and parameter validation endpoints (`/v1/simulations`)
* Usage accounting and billing integration
* Background task workers (Celery, Redis)
* Production cloud deployment
