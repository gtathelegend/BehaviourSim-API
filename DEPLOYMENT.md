# BehaviorSim API — Production Deployment Guide

This document specifies the operational requirements, infrastructure setup, security configurations, and deployment procedures for hosting the **BehaviorSim API** in production environments.

---

## 1. System Requirements & Architecture

* **Runtime**: Python `3.10+` (tested on Python `3.12.10`)
* **Framework**: FastAPI / Starlette / Uvicorn
* **Database**: PostgreSQL `14+` with standard relational tables
* **Published Core Dependency**: `behaviorsim==1.0.1`
* **Architecture Pattern**: Stateless REST API container/process with relational database persistence.

---

## 2. Production Environment Variables

Configure the following environment variables on the production container/host:

| Variable | Required | Production Value / Description | Example |
| :--- | :---: | :--- | :--- |
| `APP_ENV` | Yes | Must be set to `production` | `production` |
| `APP_NAME` | No | Service display name | `BehaviorSim API` |
| `API_VERSION` | No | SemVer release tag | `0.1.0` |
| `API_BASE_URL` | Yes | Public HTTPS URL of the hosted API | `https://api.behaviorsim.com` |
| `WEB_BASE_URL` | Yes | Public HTTPS URL of the web dashboard | `https://app.behaviorsim.com` |
| `DATABASE_URL` | Yes | PostgreSQL connection URI with `psycopg3` | `postgresql+psycopg://user:pass@db-host:5432/behaviorsim` |
| `DB_POOL_SIZE` | No | SQLAlchemy connection pool base connections (default `10`) | `20` |
| `DB_MAX_OVERFLOW` | No | Max overflow connections beyond pool size (default `20`) | `30` |
| `DB_POOL_RECYCLE` | No | Pool connection recycle duration in seconds (default `1800`) | `1800` |
| `API_KEY_PREFIX` | No | Prefix for developer API keys (default `bs_live_`) | `bs_live_` |
| `SESSION_TOKEN_PREFIX`| No | Prefix for user session tokens (default `bs_sess_`) | `bs_sess_` |
| `AUTH_SESSION_COOKIE_NAME` | No | Name for session cookie (default `behaviorsim_session`) | `behaviorsim_session` |
| `AUTH_SESSION_MAX_AGE_SECONDS` | No | Session lifespan in seconds (default `604800` = 7 days) | `604800` |
| `AUTH_OAUTH_STATE_COOKIE_NAME` | No | OAuth CSRF state cookie name (default `behaviorsim_oauth_state`) | `behaviorsim_oauth_state` |
| `AUTH_OAUTH_STATE_MAX_AGE_SECONDS` | No | OAuth CSRF cookie lifespan (default `600` = 10 mins) | `600` |
| `GOOGLE_CLIENT_ID` | Yes | Google OAuth Web Application Client ID | `123456789.apps.googleusercontent.com` |
| `GOOGLE_CLIENT_SECRET` | Yes | Google OAuth Client Secret | `GOCSPX-SecretString` |
| `GITHUB_CLIENT_ID` | Yes | GitHub OAuth Application Client ID | `Iv1.87654321` |
| `GITHUB_CLIENT_SECRET` | Yes | GitHub OAuth Application Client Secret | `github_secret_string` |
| `OAUTH_REDIRECT_BASE_URL` | Yes | Public HTTPS OAuth redirect URL | `https://api.behaviorsim.com` |
| `CORS_ORIGINS` | Yes | Comma-separated allowlist of origins (strictly no `*`) | `https://app.behaviorsim.com` |
| `LOG_LEVEL` | No | Logging verbosity (default `INFO`) | `INFO` |

> [!CAUTION]
> **Production Validation Guard**: When `APP_ENV=production`, the application lifespan executes `Settings.validate_production_configuration()`. Startup will **immediately abort** if:
> * `DATABASE_URL` contains `sqlite`, `localhost`, or `127.0.0.1`
> * `API_BASE_URL` or `WEB_BASE_URL` or `OAUTH_REDIRECT_BASE_URL` does not start with `https://`
> * `CORS_ORIGINS` contains `*`, `localhost`, or `127.0.0.1`
> * OAuth credentials contain placeholder strings

---

## 3. Database Provisioning & Migrations

### 3.1 Provisioning PostgreSQL
Ensure a dedicated PostgreSQL database with UTF-8 encoding and timezone UTC:

```sql
CREATE DATABASE behaviorsim ENCODING 'UTF8' LC_COLLATE 'en_US.UTF-8' LC_CTYPE 'en_US.UTF-8';
```

### 3.2 Executing Migrations
All schema updates are managed using Alembic. Apply all pending migrations before starting the API:

```bash
alembic upgrade head
```

To verify migration status:
```bash
alembic current
```

To rollback if needed during a rollback incident:
```bash
alembic downgrade -1
```

---

## 4. Production Process Execution

Run the application using Uvicorn with standard production process managers (such as systemd, Docker, or Kubernetes):

```bash
uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 4 \
  --proxy-headers \
  --forwarded-allow-ips "*" \
  --no-access-log
```

* `--proxy-headers`: Instructs Uvicorn to trust `X-Forwarded-Proto` and `X-Forwarded-For` from reverse proxies.
* `--workers`: Configure worker count based on available CPU cores (recommended: `2 * cores + 1`).
* `--no-access-log`: Structured application logs and correlation IDs are emitted directly by the API's logging middleware and `SafeFormatter`.

---

## 5. Reverse Proxy & HTTPS Configuration

The API must be deployed behind an SSL-terminating reverse proxy (e.g. AWS ALB, Cloudflare, Nginx, or Traefik).

### Nginx Example Configuration

```nginx
server {
    listen 443 ssl http2;
    server_name api.behaviorsim.com;

    ssl_certificate /etc/letsencrypt/live/api.behaviorsim.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.behaviorsim.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    # Security headers are injected by the application middleware,
    # but the proxy must preserve them and forward standard proxy headers:
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Request-ID $request_id;
        
        proxy_connect_timeout 10s;
        proxy_read_timeout 30s;
        proxy_send_timeout 30s;
    }
}
```

---

## 6. Health and Readiness Probes

Configure load balancer and orchestrator probes using the two dedicated endpoints:

* **Liveness Probe: `GET /health`**
  * Verifies the HTTP process is running and event loop is responsive.
  * Zero-dependency (does not touch database).
  * Interval: 10s, Timeout: 2s, Unhealthy threshold: 3.
  * Returns `200 OK`: `{"status": "ok", "app": "BehaviorSim API", ...}`.

* **Readiness Probe: `GET /ready`**
  * Verifies database connectivity via `SELECT 1`.
  * Protects traffic from reaching instances whose database pool is saturated or disconnected.
  * Interval: 5s, Timeout: 3s.
  * Returns `200 OK` on success: `{"status": "ready", "database": "connected"}`.
  * Returns `503 Service Unavailable` with sanitized JSON if database ping fails.

---

## 7. Operational Boundaries & Scaling Considerations

### 7.1 In-Memory Rate Limiting & Horizontal Scaling Caveat
* **Current Implementation**: The sliding-window rate limiter (`app/core/rate_limit.py`) tracks request timestamps in-memory.
* **Single Instance / Worker**: Strictly enforces the configured per-user RPM limits.
* **Horizontal Scaling (Multi-Worker / Multi-Pod)**:
  > [!IMPORTANT]
  > Because the current rate-limiter state is in-process memory, running multiple API workers or multi-pod clusters divides rate-limiting counters across workers. For horizontally scaled deployments requiring strict global rate-limit enforcement across distributed nodes, a shared distributed store (such as Redis) should be introduced. Quotas, however, are transactionally enforced in the PostgreSQL database and are 100% resilient across any number of workers and nodes.

### 7.2 Synchronous Simulation Compute
* Simulation runs via `POST /v1/simulations` are currently executed synchronously in the HTTP request cycle using the published `behaviorsim==1.0.1` package.
* Per-request interaction limit is bounded to `1,000` rows on the Free tier.
* Compute durations typically range from 5ms to 50ms per batch.
* Asynchronous job workers (e.g., Celery/Redis) and persistent storage (S3) are reserved for future phases.

---

## 8. Maintenance & Retention Procedures

1. **Session Cleanup**:
   * Expired sessions (`user_sessions.expires_at < NOW()`) or revoked sessions (`revoked_at IS NOT NULL`) can be periodically purged via a recurring cron or scheduled database query:
   ```sql
   DELETE FROM user_sessions WHERE expires_at < NOW() - INTERVAL '30 days';
   ```

2. **Usage Event Retention**:
   * Audit records in `usage_events` record interaction count, compute time, and request ID. For compliance, retain for 90 days or archive older events into an audit data lake.

3. **Database Backup**:
   * Perform automated point-in-time recovery (PITR) backups on PostgreSQL.
   * Verify backups regularly with staging restore drills.
