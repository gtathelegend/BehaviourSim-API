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

All tests execute self-contained against an isolated in-memory test database and do not require external PostgreSQL or cloud infrastructure.

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

## 8. Database Architecture & Migrations (Phase 1–2)

The persistence layer uses **SQLAlchemy 2.0** declarative models with asynchronous/synchronous support via Psycopg 3, managed by **Alembic** migrations.

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

### Database Migrations

Run database migrations to latest revision:

```bash
alembic upgrade head
```

Roll back migrations:

```bash
alembic downgrade base
```

## 9. API-Key Security Model

* **High-Entropy Generation**: Generated using `secrets.token_urlsafe(32)` providing 256 bits of cryptographic entropy.
* **Format**: `bs_live_<secret>`.
* **Raw-Key-Once Guarantee**: Raw keys are returned exclusively at generation time and are never stored in plaintext, logged, or returned in subsequent list/read operations.
* **Cryptographic Hashing**: API keys are hashed with SHA-256. Because 256-bit random keys have maximal entropy ($2^{256}$ keyspace), they are immune to dictionary/brute-force attacks, avoiding CPU-blocking KDF latency (e.g. bcrypt/argon2) on API hot paths.
* **Timing-Attack Resistance**: Key verification is performed via `hmac.compare_digest`.
* **Soft Revocation**: Revoking a key marks `is_active = False` and populates `revoked_at` without deleting audit history.

## 10. What is Implemented

* **Phase 0 Foundation**: FastAPI application factory, logging, settings, `/health` endpoint, `behaviorsim==1.0.1` startup check.
* **Phase 1 Database Foundation**: SQLAlchemy 2.0 declarative models (`Base`), lazy engine creation, request-scoped sessions (`get_db`), and Alembic migrations (`0001_initial_auth_tables`).
* **Phase 2 Authentication Foundation**: External auth identity schemas (`google`, `github`), developer API-key generation/hashing/verification, API-key lifecycle service (`create`, `list`, `revoke`, `validate`), and `AuthenticatedPrincipal` dependency.

## 11. Intentionally Not Implemented in Phase 1–2

The following capabilities are reserved for subsequent phases:
* Google / GitHub OAuth callback flows and token exchange
* Rate limiting and quota management
* Simulation execution and parameter validation endpoints (`/v1/simulations`)
* Usage accounting and billing integration
* Background task workers (Celery, Redis)
