# BehaviorSim API — Production Deployment Runbook (Render + Neon)

This document is the authoritative operational guide for deploying the **BehaviorSim API** on **Render** using a serverless **Neon PostgreSQL** database.

---

## 1. Architecture Overview & Constraints

The production deployment connects the frontend web application, the backend API service hosted on Render, and an external serverless PostgreSQL database hosted on Neon:

```text
behaviorsim.vedaangsharma.in (Frontend Web Application)
        │
        │ HTTPS (CORS restricted)
        ▼
api.behaviorsim.vedaangsharma.in (FastAPI / Uvicorn on Render)
        │
        │ DATABASE_URL (SSL Required: sslmode=require)
        ▼
Neon PostgreSQL (Serverless PostgreSQL 16 on Neon)
```

### Critical Operational Constraints
* **Single-Instance Deployment**: The current sliding-window rate limiter (`InMemoryRateLimiter`) operates in process memory. The web service must be configured with exactly **1 instance** (no horizontal scaling or multi-worker autoscaling) until a distributed cache (such as Redis) is introduced.
* **Synchronous Compute**: Simulation runs (`POST /v1/simulations`) execute synchronously within the request cycle using the published `behaviorsim==1.0.1` package. The Free plan ceiling of 1,000 interactions ensures execution durations complete in under 50ms.
* **No Ephemeral File Storage**: The service does not write or persist state to the local disk.
* **Serverless Connection Health**: Neon automatically suspends idle compute instances; SQLAlchemy's `pool_pre_ping=True` is enabled in `app/db/session.py` to seamlessly reconnect without dropping client requests.

---

## 2. Prerequisites & Account Setup

1. **Render Account**: A standard Render account to host the API Web Service.
2. **Neon Account**: A Neon account ([neon.tech](https://neon.tech)) to provision serverless PostgreSQL.
3. **Git Repository**: A GitHub/GitLab repository containing this codebase.
4. **Domain & DNS**: Management access for `vedaangsharma.in` to create CNAME records for custom domains.
5. **OAuth Applications**:
   - Google Cloud Console: OAuth 2.0 Web Application client.
   - GitHub Developer Settings: OAuth Application.

---

## 3. Render Web Service Configuration

The web service can be provisioned automatically via the included Render Blueprint (`render.yaml`) or configured manually.

### Option A: Automatic Provisioning (Render Blueprint)
1. In the Render Dashboard, navigate to **Blueprints** $\to$ **New Blueprint Instance**.
2. Connect your repository.
3. Render automatically discovers `render.yaml` and parses the `behaviorsim-api` web service.
4. Populate the sensitive secret variables when prompted by the Blueprint wizard:
   - `DATABASE_URL`: Your Neon PostgreSQL connection string (see Section 4).
   - `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`
   - `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET`
5. Click **Apply**.

### Option B: Manual Web Service Configuration
* **Service Type**: Web Service
* **Name**: `behaviorsim-api`
* **Region**: `Oregon` (or region closest to your Neon database)
* **Runtime**: `Python`
* **Python Version**: `3.12.10` (auto-detected from `.python-version`)
* **Build Command**: `pip install -e .`
* **Pre-Deploy Command**: `alembic upgrade head`
* **Start Command**: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips "*"`
* **Health Check Path**: `/health`
* **Auto-Deploy**: `No` (recommended for controlled deployment verification)

---

## 4. Neon PostgreSQL Setup

Follow these exact steps to provision and configure the Neon database:

1. **Create Project**: In the Neon Console ([console.neon.tech](https://console.neon.tech)), click **New Project**.
2. **Name & Region**: Set project name to `behaviorsim` and select the region closest to your Render service (e.g. `US East (Ohio)` or `US West (Oregon)`).
3. **Database Version**: Select PostgreSQL 16.
4. **Connection String**: On the project dashboard under **Connection Details**:
   - Select the target database (e.g. `neondb` or `behaviorsim`).
   - Check the **Pooled connection** toggle (recommended for API workloads, utilizes PgBouncer on port 5432).
   - Copy the connection string. It will look like:
     ```text
     postgresql://USER:PASSWORD@ep-xyz-pooler.REGION.aws.neon.tech/neondb?sslmode=require
     ```
5. **SSL Requirement**: Verify that the query parameter `?sslmode=require` is present. Neon strictly requires SSL/TLS encrypted connections.
6. **URL Normalization**: The application automatically normalizes both `postgresql://` and `postgres://` prefixes to `postgresql+psycopg://` while preserving query parameters.
7. **Local Testing**: For local testing against Neon, paste the connection string into your local `.env` (which is git-ignored).
8. **Render Production**: In the Render Dashboard, add `DATABASE_URL` as a secret environment variable.
9. **Zero-Secret Rule**: NEVER commit the connection string, user, or password to Git.

---

## 5. Environment Variables & Secrets

Configure the following variables in the Render Dashboard (**Environment** tab):

| Variable | Type | Production Value |
| :--- | :---: | :--- |
| `APP_ENV` | Variable | `production` |
| `APP_NAME` | Variable | `BehaviorSim API` |
| `API_VERSION` | Variable | `0.1.0` |
| `API_BASE_URL` | Variable | `https://api.behaviorsim.vedaangsharma.in` |
| `WEB_BASE_URL` | Variable | `https://behaviorsim.vedaangsharma.in` |
| `OAUTH_REDIRECT_BASE_URL` | Variable | `https://api.behaviorsim.vedaangsharma.in` |
| `CORS_ORIGINS` | Variable | `https://behaviorsim.vedaangsharma.in` |
| `DATABASE_URL` | Secret | Real Neon PostgreSQL connection string (`...sslmode=require`) |
| `DB_POOL_SIZE` | Variable | `10` |
| `DB_MAX_OVERFLOW` | Variable | `20` |
| `DB_POOL_RECYCLE` | Variable | `1800` |
| `AUTH_SESSION_COOKIE_NAME` | Variable | `behaviorsim_session` |
| `AUTH_SESSION_MAX_AGE_SECONDS`| Variable | `604800` (7 days) |
| `AUTH_OAUTH_STATE_COOKIE_NAME`| Variable | `behaviorsim_oauth_state` |
| `AUTH_OAUTH_STATE_MAX_AGE_SECONDS`| Variable | `600` (10 minutes) |
| `API_KEY_PREFIX` | Variable | `bs_live_` |
| `SESSION_TOKEN_PREFIX` | Variable | `bs_sess_` |
| `LOG_LEVEL` | Variable | `INFO` |
| `GOOGLE_CLIENT_ID` | Secret | Real Google OAuth Client ID |
| `GOOGLE_CLIENT_SECRET` | Secret | Real Google OAuth Client Secret |
| `GITHUB_CLIENT_ID` | Secret | Real GitHub OAuth Client ID |
| `GITHUB_CLIENT_SECRET` | Secret | Real GitHub OAuth Client Secret |

> [!IMPORTANT]
> When `APP_ENV=production`, the application lifespan executes strict validation guards. Startup will fail immediately if `DATABASE_URL` uses SQLite or localhost, if URLs lack `https://`, if CORS contains `*` or `localhost`, or if OAuth credentials contain placeholder strings.

---

## 6. Database Migrations

### Pre-Deploy Migration on Render
Migrations are managed via Alembic and executed using Render's native **Pre-Deploy Command**:

```bash
alembic upgrade head
```

* **Safety Guarantee**: Render runs `alembic upgrade head` in an isolated container connected to Neon before starting the new web service container.
* If a migration fails, Render halts deployment automatically, leaving the active service untouched.

### Safe Verification Tool
A safe CLI diagnostic script is provided to verify database connectivity without printing secrets:

```bash
python scripts/verify_db_connectivity.py
```

---

## 7. Custom Domain & DNS Setup

To attach `api.behaviorsim.vedaangsharma.in`:

1. In Render Dashboard, go to **Settings** $\to$ **Custom Domains**.
2. Add `api.behaviorsim.vedaangsharma.in`.
3. Render provides a target hostname (e.g. `behaviorsim-api.onrender.com`).
4. In your DNS manager (e.g. Cloudflare, Route53, Namecheap), create a CNAME record:
   ```text
   Type:  CNAME
   Host:  api.behaviorsim
   Value: behaviorsim-api.onrender.com
   TTL:   Automatic / 300s
   ```
5. Render automatically provisions and manages an SSL/TLS certificate via Let's Encrypt.
6. Verify HTTPS resolution:
   ```bash
   curl -I https://api.behaviorsim.vedaangsharma.in/health
   ```

---

## 8. OAuth Provider Configuration

Register the exact production callback endpoints in your provider developer consoles:

### Google Cloud Console (Credentials $\to$ OAuth 2.0 Client IDs)
* **Authorized JavaScript origins**:
  - `https://behaviorsim.vedaangsharma.in`
* **Authorized redirect URIs**:
  - `https://api.behaviorsim.vedaangsharma.in/v1/auth/google/callback`

### GitHub Developer Settings (OAuth Apps)
* **Homepage URL**:
  - `https://behaviorsim.vedaangsharma.in`
* **Authorization callback URL**:
  - `https://api.behaviorsim.vedaangsharma.in/v1/auth/github/callback`

---

## 9. Health & Readiness Probes

The API exposes two distinct probe endpoints:

1. **Liveness Probe: `GET /health`**
   - **Render Health Check**: Set Render's `healthCheckPath` to `/health`.
   - Checks that the Python process and event loop are responsive.
   - Zero-dependency: performs no database queries or I/O.
   - Responds `200 OK` in < 2ms:
     ```json
     {
       "status": "ok",
       "app": "BehaviorSim API",
       "version": "0.1.0",
       "environment": "production"
     }
     ```

2. **Readiness Probe: `GET /ready`**
   - Verifies deep service readiness by executing `SELECT 1` against Neon PostgreSQL.
   - Returns `200 OK` when healthy:
     ```json
     {
       "status": "ready",
       "database": "connected"
     }
     ```
   - Returns `503 Service Unavailable` with sanitized JSON if Neon is unreachable.

---

## 10. First Deployment Walkthrough

1. **Provision Neon Database**: Create the Neon PostgreSQL project and obtain the connection string with `?sslmode=require`.
2. **Apply Blueprint on Render**: Link the repository and configure secrets in Render Dashboard.
3. **Trigger Manual Deploy**: Click **Manual Deploy** $\to$ **Deploy latest commit**.
4. **Inspect Build Logs**:
   - Verify Python runtime detection (`3.12.10`).
   - Verify dependency installation (`pip install -e .`).
5. **Inspect Pre-Deploy Logs**:
   - Verify `alembic upgrade head` runs against Neon and applies migrations 0001, 0002, 0003.
6. **Inspect Service Startup Logs**:
   - Verify startup log: `Starting BehaviorSim API (0.1.0) in production mode`.
   - Verify `BehaviorSim dependency verified: version 1.0.1`.
   - Verify `Uvicorn running on http://0.0.0.0:<PORT>`.
7. **Verify Probes**:
   ```bash
   curl -s https://api.behaviorsim.vedaangsharma.in/health
   curl -s https://api.behaviorsim.vedaangsharma.in/ready
   ```

---

## 11. Production Smoke Test

Run the following smoke test sequence against the deployed production API:

```bash
API="https://api.behaviorsim.vedaangsharma.in"

# 1. Liveness & Readiness
curl -f "$API/health"
curl -f "$API/ready"

# 2. Public Presets Discovery
curl -f "$API/v1/presets"
curl -f "$API/v1/presets/education"

# 3. Security Headers Verification
curl -I "$API/health" | grep -E "x-content-type-options|x-frame-options|strict-transport-security"

# 4. CORS Verification (Must allow frontend origin)
curl -s -I -X OPTIONS "$API/v1/presets" \
  -H "Origin: https://behaviorsim.vedaangsharma.in" \
  -H "Access-Control-Request-Method: GET" | grep -i "access-control-allow-origin"
```

---

## 12. Logs & Troubleshooting

### Log Ingestion
Render captures stdout and stderr in real-time. Application logs are formatted by `SafeFormatter` with correlation IDs:
```text
2026-09-13 00:30:00 [INFO] [behaviorsim_api] [req:c71a3962-e6fd-4100-8fae-cbeffbe0da3e]: Starting BehaviorSim API (0.1.0) in production mode
```

### Common Deployment Issues & Solutions
1. **Startup Failure: `ValueError: Production configuration validation failed`**
   - Cause: Missing or invalid production environment variables (e.g. HTTP instead of HTTPS, SQLite in `DATABASE_URL`, or default placeholder credentials).
   - Fix: Check Render Environment tab and update values to match production requirements.
2. **Pre-Deploy Failure: `psycopg.OperationalError: connection failed`**
   - Cause: Neon connection string is missing `?sslmode=require`, or host/credentials are mistyped.
   - Fix: Verify Neon connection string in Render Dashboard.
3. **CORS Rejection from Frontend**
   - Cause: Frontend URL in `CORS_ORIGINS` does not match the actual browser origin.
   - Fix: Update `CORS_ORIGINS` to `https://behaviorsim.vedaangsharma.in`.

---

## 13. Rollback Procedure

If an incident occurs post-deployment:

1. **Immediate Service Rollback**:
   - In Render Dashboard, go to **Deploys**.
   - Select the previous known-good deployment.
   - Click **Rollback to this deploy**.
   - Render instantly re-activates the previous container image without rebuilding.
2. **Database Migration Considerations**:
   - > [!CAUTION]
     > **Never run casual database downgrades in production.** Downgrading migrations drops tables (`DROP TABLE`) and causes irreversible data loss.
   - All migrations in this repository are backwards-compatible. Reverting the application code to the prior commit is safe without rolling back the database schema.
   - If schema rollback is strictly necessary, perform a manual backup in Neon console before executing `alembic downgrade -1`.

---

## 14. Rate-Limiting Single-Instance Deployment Constraint

The current application rate limiter (`InMemoryRateLimiter`) stores rolling window counters in Python process memory.

```text
┌────────────────────────────────────────────────────────┐
│                   Single Render Instance               │
│  ┌──────────────────────┐    ┌──────────────────────┐  │
│  │  FastAPI Application │    │ InMemoryRateLimiter  │  │
│  │  - Monthly Quotas    │    │ - 5 requests/min     │  │
│  │    (via Neon DB)     │    │   (in process memory)│  │
│  └──────────────────────┘    └──────────────────────┘  │
└────────────────────────────────────────────────────────┘
```

* **Single Instance**: Plan rate limits (e.g. 5 requests/minute on Free plan) are strictly and accurately enforced.
* **Monthly Quotas**: Transactionally enforced directly in Neon PostgreSQL via conditional `UPDATE` queries, remaining 100% atomic regardless of processes.
* **Horizontal Scaling Rule**: Do **not** enable Render auto-scaling or increase the instance count to $> 1$ until Redis-backed rate limiting is implemented in a future phase.

---

## 15. Security Checklist

Before releasing to end users, verify:

- [ ] `APP_ENV` is set to `production`.
- [ ] Render health check path is `/health`.
- [ ] Neon connection string uses `sslmode=require`.
- [ ] No default secrets or test credentials are used in Render Environment tab.
- [ ] SSL/TLS certificate is active for `api.behaviorsim.vedaangsharma.in`.
- [ ] Response headers include `Strict-Transport-Security: max-age=31536000; includeSubDomains`.
- [ ] Response headers include `X-Content-Type-Options: nosniff`.
- [ ] Response headers include `X-Frame-Options: DENY`.
- [ ] `CORS_ORIGINS` is strictly limited to `https://behaviorsim.vedaangsharma.in` (no wildcard `*`).
- [ ] Google & GitHub OAuth client secrets are configured as Render secrets (`sync: false`).
- [ ] Database credentials are not committed to git.

---

## 16. Future Scaling Roadmap

The following architectural components are planned for subsequent phases:

* **Redis Distributed State**: Replaces `InMemoryRateLimiter` to enable multi-instance horizontal autoscaling on Render.
* **Asynchronous Simulation Workers**: Celery/Redis worker fleet for long-running simulation jobs and Monte Carlo parameter sweeps.
* **Object Storage (S3 / Cloudflare R2)**: Storage of simulation parquet/CSV export files.
* **Stripe Billing Integration**: Automated tier upgrades from Free to Pro/Enterprise.
