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
| `DATABASE_URL` | `postgresql://...` | Connection URI for the database (used in Phase 1+) |
| `CORS_ORIGINS` | `http://localhost:3000,http://localhost:8000` | Comma-separated list of allowed CORS origins |
| `LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

No external secrets or cloud credentials are required for Phase 0.

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

All Phase 0 tests run locally and self-contained without requiring database or cloud dependencies.

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

## 8. What is Implemented in Phase 0

* **Application Foundation**: FastAPI application factory with lifespan-based lifecycle management.
* **Dependency Verification**: Startup check ensuring `behaviorsim==1.0.1` is available without executing compute during initialization.
* **Centralized Configuration**: Typed `pydantic-settings` schema with environment variable parsing and origin validation.
* **Infrastructure Health Endpoint**: Fast, lightweight `/health` probe.
* **API Versioning Base**: Router layout prepared for `/v1/...` routes.
* **Structured Logging**: Standardized application logging omitting sensitive headers and credentials.
* **Safe Error Handling Foundation**: Base domain exception definitions and error registration handlers.
* **Restricted CORS**: Origin-filtered CORS middleware preventing unsafe wildcard access.
* **Self-Contained Test Suite**: Health, dependency, and settings validation passing with zero external dependencies.

## 9. Intentionally Not Implemented in Phase 0

The following capabilities are reserved for subsequent phases:
* Database persistence (SQLAlchemy, Alembic migrations)
* Authentication and authorization (OAuth, API keys, JWTs)
* Simulation execution and parameter validation endpoints (`/v1/simulations`)
* Background task queues (Celery, Redis)
* Rate limiting and quota management
