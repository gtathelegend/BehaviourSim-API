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
| `DB_POOL_SIZE` | `10` | Database connection pool base size (PostgreSQL) |
| `DB_MAX_OVERFLOW` | `20` | Database connection pool max overflow connections |
| `DB_POOL_RECYCLE` | `1800` | Database connection pool recycle time in seconds (30 mins) |
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
  "plan": "free",
  "authentication_methods": ["github", "api_key"]
}
```

### API Key Management Endpoints

Developer API keys can be managed programmatically or via authenticated dashboard sessions:

* **List API Keys (`GET /v1/api-keys`)**: Returns metadata for all API keys owned by the user (raw secrets are omitted).
* **Create API Key (`POST /v1/api-keys`)**: Creates a key with a descriptive name, returns the raw secret `key` **exactly once**, and enforces the user's plan `max_api_keys` quota.
* **Revoke API Key (`DELETE /v1/api-keys/{key_id}`)**: Soft-revokes an active API key owned by the user, immediately invalidating future requests.

## 10. Quotas, Rate Limiting & Usage Accounting

Phase 4 establishes the usage-control and boundary layer protecting backend simulation compute:

### Plans & Entitlements

Each user is assigned a plan (defaults to `free` plan):

* **Monthly Requests**: 100 requests / month
* **Monthly Interactions**: 10,000 interactions / month
* **Single-Request Limit**: 1,000 interactions / request
* **Rate Limit**: 5 requests / minute
* **Max Concurrent Simulations**: 1
* **Max Developer API Keys**: 1 active key

### Usage Accounting Architecture

* **`monthly_usage`**: Aggregated monthly counters (`request_count`, `interaction_count`) scoped to UTC calendar month boundaries (`period_start`).
* **`usage_events`**: Append-only event audit log recording every reservation and completion with interaction counts, success flags, compute latency (`compute_ms`), and attribution (`api_key_id`, `request_id`).
* **Atomic Quota Reservation (`reserve_usage`)**: Uses conditional SQL updates (`UPDATE monthly_usage SET ... WHERE request_count + delta <= limit`) ensuring zero oversubscription under high concurrency.
* **Per-Request Interaction Boundary**: Rejects simulation requests upfront if requested interactions exceed plan maximum (`HTTP 400`).
* **Monthly Quota Boundary**: Rejects requests when monthly limits are reached (`HTTP 429` with `quota_exceeded`).
* **Key Creation Cap**: Rejects API key creation if active key count reaches plan limit (`HTTP 400`).

### Request Rate Limiting

* **Sliding-Window Limiter**: In-memory thread-safe rate limiter tracking timestamps per user over 60-second windows.
* **HTTP 429 Too Many Requests**: Returns standard error payload along with standard `Retry-After: <seconds>` HTTP header.
* **FastAPI Dependency**: `check_rate_limit` integrates cleanly on protected endpoints.

### Usage Endpoint

```http
GET /v1/usage
```

Requires authentication (cookie, session token, or API key) and enforces rate limits.

**Response (HTTP 200 OK):**

```json
{
  "plan": "free",
  "period_start": "2026-09-01T00:00:00Z",
  "period_end": "2026-10-01T00:00:00Z",
  "limits": {
    "monthly_requests": 100,
    "monthly_interactions": 10000,
    "max_interactions_per_request": 1000,
    "requests_per_minute": 5,
    "max_concurrent_simulations": 1,
    "max_api_keys": 1
  },
  "usage": {
    "requests": 12,
    "interactions": 1200
  },
  "remaining": {
    "requests": 88,
    "interactions": 8800
  }
}
```

## 11. Simulation API

Phase 5 establishes the production-facing simulation and domain discovery endpoints interfacing directly with the published `behaviorsim==1.0.1` package.

### Presets Discovery

```http
GET /v1/presets
GET /v1/presets/{preset}
```

Publicly accessible discovery endpoints describing supported domains, cohorts/profiles, and state names without triggering simulation compute.

**Available Presets:**
- `education`: Adaptive learning telemetry modeling cognitive load, accuracy, response times, and student mastery.
- `finance`: Financial trading telemetry, risk alerts, drawdowns, and portfolio volatility.
- `healthcare`: Patient monitoring telemetry tracking vital trends, alerts, and mobility trajectories.
- `mobile_app` (or alias `mobile`): User engagement tracking session duration, navigation depth, actions, and checkout flows.

### Execute Simulation

```http
POST /v1/simulations
```

Requires authentication (API key or user session token) and enforces rate limiting (5 req/min on Free plan) and monthly quota. When execution succeeds, the simulation run, execution provenance, and synthetic data are durably persisted to PostgreSQL. If execution or persistence fails, reserved quota is automatically refunded.

**Request Schema:**

```json
{
  "preset": "education",
  "num_interactions": 100,
  "seed": 42,
  "profile": "average",
  "initial_state": "Optimal"
}
```

**Response (HTTP 200 OK):**

```json
{
  "simulation_id": "2728f775-9874-4dad-8584-10b9b43eb8f4",
  "preset": "education",
  "num_interactions": 100,
  "seed": 42,
  "data": [
    {
      "profile": "average",
      "sequence_id": 1,
      "interaction_id": 1,
      "state": "Optimal",
      "difficulty": 1,
      "accuracy": 0,
      "nrt": 1.215,
      "retries": 1,
      "help_requested": 0,
      "confidence": 2
    }
  ],
  "metadata": {
    "behaviorsim_version": "1.0.1",
    "api_version": "0.1.0",
    "compute_ms": 12,
    "reproducible": true
  }
}
```

### List Simulation History

```http
GET /v1/simulations
```

Requires authentication (cookie session or API key) and enforces rate limiting. Retrieves a paginated history of simulation runs owned by the caller.

**Query Parameters:**
* `page` (integer, default: 1, min: 1): 1-indexed page number.
* `page_size` (integer, default: 20, min: 1, max: 100): Number of items per page.
* `preset` (string, optional): Filter by domain preset (`education`, `finance`, `healthcare`, `mobile_app`, or alias `mobile`). Invalid presets return `400 Bad Request`.
* `status` (string, optional): Filter by simulation run status (`completed`, `failed`, `pending`). Invalid statuses return `400 Bad Request`.

**Performance & Lightweight Projections**: The history listing returns run provenance and metadata only. The heavy interaction dataset (`data`) is omitted from list items to conserve network bandwidth and database I/O. Full datasets are retrieved via `GET /v1/simulations/{simulation_id}`.

**Ownership Isolation**: All queries and `total` counts are strictly filtered by the authenticated user (`user_id = principal.user.id`). No cross-user metadata is accessible.

**Response (HTTP 200 OK):**

```json
{
  "items": [
    {
      "simulation_id": "2728f775-9874-4dad-8584-10b9b43eb8f4",
      "preset": "education",
      "num_interactions": 100,
      "seed": 42,
      "profile": "average",
      "initial_state": "Optimal",
      "status": "completed",
      "compute_ms": 12,
      "reproducible": true,
      "behaviorsim_version": "1.0.1",
      "api_version": "0.1.0",
      "created_at": "2026-09-14T01:00:00Z",
      "completed_at": "2026-09-14T01:00:01Z"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1,
  "has_next": false
}
```

### Retrieve Simulation Run

```http
GET /v1/simulations/{simulation_id}
```

Requires authentication (cookie session or API key). Retrieves the complete stored simulation run, status, parameters, and generated data.

**Security & IDOR Protection**: A caller can only retrieve simulations they own. If the simulation does not exist or belongs to another user, a uniform `404 Not Found` error envelope is returned to prevent identifier enumeration.

**Response (HTTP 200 OK):**

```json
{
  "simulation_id": "2728f775-9874-4dad-8584-10b9b43eb8f4",
  "preset": "education",
  "num_interactions": 100,
  "seed": 42,
  "profile": "average",
  "initial_state": "Optimal",
  "status": "completed",
  "data": [
    {
      "profile": "average",
      "sequence_id": 1,
      "interaction_id": 1,
      "state": "Optimal",
      "difficulty": 1,
      "accuracy": 0,
      "nrt": 1.215,
      "retries": 1,
      "help_requested": 0,
      "confidence": 2
    }
  ],
  "metadata": {
    "behaviorsim_version": "1.0.1",
    "api_version": "0.1.0",
    "compute_ms": 12,
    "reproducible": true
  },
  "created_at": "2026-09-14T01:00:00Z",
  "completed_at": "2026-09-14T01:00:01Z"
}
```

### Delete Simulation Run

```http
DELETE /v1/simulations/{simulation_id}
```

Requires authentication (cookie session or API key) and enforces rate limiting. Permanently deletes a simulation run owned by the caller.

**Status Code**: `HTTP 204 No Content` on successful permanent deletion.

**Security & IDOR Protection**: A caller can only delete simulations they own. If the simulation does not exist or belongs to another user, a uniform `404 Not Found` error envelope is returned to prevent identifier enumeration.

**Quota Policy**: Deleting a persisted simulation run does **not** refund consumed monthly simulation quota or interactions, as the computational generation has already occurred.

### Python Client Example

```python
import requests

# Local development or production endpoint
API_URL = "http://localhost:8000/v1/simulations"
API_KEY = "bs_live_your_api_key_here"

response = requests.post(
    API_URL,
    headers={"Authorization": f"Bearer {API_KEY}"},
    json={
        "preset": "education",
        "num_interactions": 100,
        "seed": 42,
    },
)

response.raise_for_status()
result = response.json()
print("Simulation ID:", result["simulation_id"])
print("Generated Rows:", len(result["data"]))
```

## 12. Operational Readiness & Production Hardening

Phase 6 hardens the BehaviorSim API for controlled deployment with high reliability, observability, and defensive security.

### Liveness vs Readiness Probes

* **`GET /health`**: Lightweight zero-dependency process liveness check. Responds `200 OK` (`{"status": "ok", "app": "BehaviorSim API", ...}`) to verify the server process is responsive.
* **`GET /ready`**: Deep readiness probe verifying database connectivity via a live test query (`SELECT 1`). Returns `200 OK` when the database is healthy, or `503 Service Unavailable` with sanitized error details if the database cannot be reached.

### Security Headers & Middleware

All HTTP responses automatically include hardened security headers via `SecurityHeadersMiddleware`:
* `X-Content-Type-Options: nosniff`
* `X-Frame-Options: DENY`
* `Referrer-Policy: strict-origin-when-cross-origin`
* `Content-Security-Policy: default-src 'self'`
* `Strict-Transport-Security: max-age=31536000; includeSubDomains` (enforced when `APP_ENV=production`)

### Correlation IDs & Structured Logging

* `CorrelationIdMiddleware` extracts incoming `X-Request-ID` headers or generates cryptographically secure UUID4 identifiers.
* Attached to response headers and contextualized across all application log records (`[req_id=...]`).
* Sensitive headers (`Authorization`, `Cookie`), tokens, and credentials are automatically sanitized from logs.

### Error Handling & Stack Trace Sanitization

All unhandled exceptions (`HTTP 500`), Pydantic validation errors (`HTTP 422`), and domain errors return uniform, structured JSON payloads:
```json
{
  "detail": "Internal server error",
  "request_id": "c71a3962-e6fd-4100-8fae-cbeffbe0da3e"
}
```
Internal stack traces, database schema details, and secrets are strictly suppressed in client responses and logged server-side with correlation IDs.

### Production Configuration Validation

When `APP_ENV=production`, the application lifespan executes rigorous validation checks:
* Fails startup if default or wildcard CORS origins are configured (`localhost`, `127.0.0.1`, `*`).
* Fails startup if default or insecure secret keys are used.
* Fails startup if OAuth client IDs or secrets are missing.
* Fails startup if using an insecure SQLite file or memory database in production.

## 13. What is Implemented

* **Phase 0 Foundation**: FastAPI application factory, logging, settings, `/health` endpoint, `behaviorsim==1.0.1` startup check.
* **Phase 1 Database Foundation**: SQLAlchemy 2.0 declarative models (`Base`), lazy engine creation, request-scoped sessions (`get_db`), and Alembic migrations.
* **Phase 2 API Key Foundation**: Developer API-key generation/hashing/verification, API-key lifecycle service (`create`, `list`, `revoke`, `validate`).
* **Phase 3 OAuth & Account Foundation**: Google & GitHub OAuth 2.0 flows, CSRF state protection, anti-takeover account linking, server-side session management (`user_sessions`), `/v1/auth/logout`, `/v1/account`, and unified `AuthenticatedPrincipal`.
* **Phase 4 Quotas, Rate Limiting & Usage Accounting**: Plan model and seeding, monthly usage counters, audit usage events, atomic quota reservation with concurrency protection, sliding-window rate limiting (`HTTP 429` + `Retry-After`), API key creation cap, and `GET /v1/usage`.
* **Phase 5 Simulation API**: Public preset discovery (`GET /v1/presets`), synchronous simulation execution (`POST /v1/simulations`), integration with `behaviorsim==1.0.1` package, strict plan interaction limits, atomic reservation and automatic failure refund, seed reproducibility, and usage event auditing.
* **Phase 6 Production Hardening & Operational Readiness**: Production configuration validation, database connection pooling, `/ready` database readiness probe, `SecurityHeadersMiddleware`, `CorrelationIdMiddleware` with contextvar structured logging, error response sanitization (500/422/domain) concealing stack traces, non-negative transactional refund safety, and comprehensive smoke/hardening test suites.
* **Phase 7 Deployment Readiness Audit**: Complete API contract verification, `/v1/api-keys` HTTP routes (`GET`, `POST`, `DELETE`), OpenAPI 3.1 schema verification, and comprehensive 26-point production integration suite.
* **Phase 8A Render Deployment Preparation**: Python runtime pinning (`.python-version` with 3.12.10), Render Blueprint specification (`render.yaml`), PostgreSQL connection string normalization (`postgres://` & `postgresql://`), clean lifespan engine disposal, and authoritative Render deployment runbook ([DEPLOYMENT.md](DEPLOYMENT.md)).
* **Phase 11 Simulation Persistence & History**: Durable PostgreSQL simulation persistence (`simulations` table, Alembic revision `0004_add_simulations`), JSON/JSONB interaction storage, automatic quota refund on persistence failure, and secure retrieval endpoint (`GET /v1/simulations/{simulation_id}`) with strict ownership isolation (IDOR protection).
* **Phase 12 Simulation History & Result Management**: Authenticated simulation history endpoint (`GET /v1/simulations`), bounded pagination (`page`, `page_size <= 100`), domain & status filtering, deterministic ordering (`created_at DESC, id DESC`), efficient metadata projection excluding large JSONB payloads, and strict caller-scoped ownership isolation.
* **Phase 13 Simulation Deletion & Data Lifecycle Foundation**: Authenticated permanent deletion endpoint (`DELETE /v1/simulations/{simulation_id}`), HTTP 204 No Content response, strict caller-scoped ownership isolation (IDOR protection), immediate removal from history and detail endpoints, and preserved quota accounting (no quota refunds on delete).

## 14. Intentionally Deferred Capabilities

The following capabilities are intentionally deferred for subsequent phases:
* Redis distributed state and distributed rate limiting (required before horizontally scaling API instances > 1)
* Asynchronous background simulation execution and job queues (Celery)
* Object storage integration (S3) for simulation artifact exports
* Billing and subscription payment processing (Stripe)

## 15. Production Deployment (Render + Neon)

The application is prepared for production deployment with the following architecture:
* **Web Service / API**: Hosted on **Render** (`api.behaviorsim.vedaangsharma.in`), running FastAPI / Uvicorn.
* **Database**: Managed **Neon Serverless PostgreSQL** with mandatory TLS/SSL (`?sslmode=require`).

Key operational resources:
* **Render Blueprint**: Configured via [`render.yaml`](render.yaml) for web service provisioning with external database secret binding (`sync: false`).
* **Database Verification**: Safe diagnostic connectivity script available at [`scripts/verify_db_connectivity.py`](scripts/verify_db_connectivity.py).
* **Authoritative Runbook**: Step-by-step instructions for Neon project creation, environment configuration, DNS setup, database migrations, and operational maintenance are detailed in:
  * [DEPLOYMENT.md](DEPLOYMENT.md)
