# BehaviourSim — Production Operations, Monitoring & SLO Runbook

**Phase 23 Operational Architecture & Incident Management Guide**  
**Target Systems**: `BehaviourSim API` (Render) & `PostgreSQL` (Neon)  
**Classification**: Production Engineering Runbook  

---

## 1. Executive Summary & Purpose

This document defines the operational monitoring framework, Service Level Indicators (SLIs), Service Level Objectives (SLOs), alerting thresholds, and incident runbooks for the BehaviourSim production platform.

Its primary goal is to empower operators to answer critical operational questions rapidly:
- Is the API healthy and responsive?
- Are simulations completing successfully?
- Is the async queue backing up or are jobs getting stuck?
- Is the daily retention cleanup running as scheduled?
- Is the Neon database approaching storage or connection saturation?
- What actions should an operator take during an incident?

---

## 2. Service Level Objectives (SLO) & Indicators (SLI)

> [!NOTE]
> All targets below are **internal application operational objectives**, not external infrastructure provider contractual SLAs.

### 2.1 Core SLI / SLO Definitions

| Objective Area | Service Level Indicator (SLI) | SLO Target | Measurement Window | Operational Impact |
| :--- | :--- | :--- | :--- | :--- |
| **API Availability** | $\frac{\text{Total Successful Requests (Non-5xx)}}{\text{Total Valid Requests}} \times 100\%$ | **$\ge 99.0\%$** | Monthly rolling | Core API reachability and stability. |
| **Submission Latency** | `POST /v1/simulations` HTTP latency (queue ingestion) | **$\text{p50} < 50\text{ms}$**<br>**$\text{p95} < 150\text{ms}$** | 1-hour rolling | Immediate acceptance of simulation requests. |
| **Status Polling Latency** | `GET /v1/simulations/{id}` HTTP latency | **$\text{p50} < 25\text{ms}$**<br>**$\text{p95} < 100\text{ms}$** | 1-hour rolling | Responsive frontend job tracking. |
| **Diagnostics Latency** | `GET /v1/diagnostics` HTTP latency | **$\text{p50} < 5\text{ms}$**<br>**$\text{p95} < 25\text{ms}$** | 1-hour rolling | Zero-overhead operational health checks (5s TTL cache). |
| **Simulation Success Rate** | $\frac{\text{Completed Simulations}}{\text{Completed} + \text{Internal Failed}} \times 100\%$ | **$\ge 99.0\%$** | 24-hour rolling | Simulator algorithmic correctness and engine reliability. |
| **Queue Backlog Age** | Maximum age of oldest pending job | **$< 60\text{ seconds}$** | Real-time | Queue processing timeliness and worker capacity. |
| **Retention Freshness** | Elapsed time since last successful retention cleanup | **$< 26\text{ hours}$** | Daily (02:00 UTC) | Steady-state database storage equilibrium. |

---

## 3. Operational Telemetry & Diagnostics Catalog

Real-time telemetry is exposed through `GET /v1/diagnostics` and structured JSON logs.

### 3.1 Diagnostics Endpoint Schema (`GET /v1/diagnostics`)

```json
{
  "status": "ok",
  "version": "0.1.0",
  "queue": {
    "pending": 0,
    "running": 0,
    "depth": 0,
    "oldest_pending_age_seconds": null,
    "oldest_running_age_seconds": null
  },
  "health": {
    "queue_healthy": true,
    "cleanup_healthy": true,
    "error_rate_pct": 0.0
  },
  "metrics": {
    "uptime_seconds": 86400,
    "api": {
      "requests_total": 12540,
      "requests_by_method": {"GET": 9800, "POST": 2740},
      "requests_by_status_class": {"2xx": 12200, "3xx": 0, "4xx": 335, "5xx": 5},
      "requests_by_route_category": {"simulations": 8500, "presets": 1200, "auth": 450, "diagnostics": 2390},
      "errors_total": 340,
      "errors_by_status": {"401": 120, "404": 180, "429": 35, "500": 5},
      "auth_failures_total": 120,
      "rate_limit_rejections_total": 35,
      "quota_exhausted_total": 12,
      "latency_ms": {
        "count": 12540,
        "min": 1.2,
        "max": 142.5,
        "avg": 8.45,
        "histogram": {
          "lt_10ms": 9420,
          "lt_25ms": 2100,
          "lt_50ms": 820,
          "lt_100ms": 180,
          "lt_250ms": 20,
          "lt_500ms": 0,
          "lt_1s": 0,
          "lt_2s": 0,
          "lt_5s": 0,
          "gt_5s": 0
        }
      }
    },
    "simulations": {
      "accepted": 2740,
      "accepted_by_preset": {"education": 1800, "mobile": 940},
      "started": 2740,
      "completed": 2738,
      "failed": 2,
      "failed_by_code": {"execution_timeout": 2},
      "recovered": 1,
      "failed_after_max_attempts": 0,
      "interactions_processed_total": 2740000,
      "worker_throughput": {
        "interactions_per_second": 27850.4,
        "jobs_per_second": 27.85,
        "total_compute_ms": 98380
      },
      "execution_duration_ms": {"count": 2738, "min": 18, "max": 85, "avg": 35.9},
      "queue_wait_ms": {"count": 2738, "min": 2, "max": 140, "avg": 12.4}
    },
    "retention": {
      "cleanup_runs_total": 14,
      "simulations_deleted_total": 2840,
      "cleanup_failures_total": 0,
      "last_run_at": 1726308000.0,
      "last_deleted_count": 195,
      "total_duration_ms": 48.2,
      "hours_since_last_cleanup": 4.25,
      "stale_warning": false
    }
  }
}
```

### 3.2 Low-Cardinality Metric Label Guarantees
To prevent memory leaks and high-cardinality memory expansion in Python process memory:
- **Route Categories**: Strictly mapped to a static finite set (`simulations`, `presets`, `auth`, `account`, `diagnostics`, `health`, `other`).
- **Status Classes**: Grouped into `2xx`, `3xx`, `4xx`, `5xx`.
- **Latency Histogram**: Partitioned into 10 fixed empirical buckets.
- **Strictly Excluded**: `user_id`, `simulation_id`, `request_id`, client IP addresses, API key tokens, query strings, and raw URL paths are NEVER used as metric labels.

---

## 4. Alerting Thresholds: Warning vs. Critical

| Condition | Warning Threshold | Critical Threshold | Rationale & Action |
| :--- | :--- | :--- | :--- |
| **API Error Rate (5xx)** | $> 1.0\%$ of requests | $> 5.0\%$ of requests | Indicates unhandled internal server exceptions or database disconnection. |
| **Queue Depth** | $> 50$ queued jobs | $> 200$ queued jobs | Worker throughput lagging behind incoming burst submissions. |
| **Oldest Pending Job Age** | $> 60\text{ seconds}$ | $> 300\text{ seconds}$ (5 min) | Pending jobs failing to be claimed; worker thread may be stalled or hung. |
| **Oldest Running Job Age** | $> 300\text{ seconds}$ (5 min) | $> 600\text{ seconds}$ (10 min) | Worker lease expired; job should be recovered or marked timed out. |
| **Retention Cleanup Freshness** | $> 26\text{ hours}$ without run | $> 48\text{ hours}$ without run | Render Cron failed to trigger or cleanup process crashed. |
| **Retention Failures** | $\ge 1$ failure in last run | Repeated failures ($> 2$) | Database lock contention, transaction failure, or connection drop during prune. |
| **Rate Limit / Quota Pressure** | $> 50$ rejections / hour | $> 200$ rejections / hour | Potential client polling bug, quota exhaustion, or abusive traffic surge. |
| **Neon Storage Utilization** | $> 70\%$ of plan limit ($350\text{MB}$) | $> 85\%$ of plan limit ($425\text{MB}$) | Approaching free tier ceiling; investigate retention settings or upgrade plan. |

---

## 5. Incident Runbooks

### Runbook 1: API Unavailable / 5xx Error Surge

**Trigger**: Critical Alert — HTTP 5xx responses $> 5\%$, or `/health` endpoint failing.

1. **Verify Live Reachability**:
   ```bash
   curl -i https://api.behavioursim.vedaangsharma.in/health
   curl -i https://api.behavioursim.vedaangsharma.in/ready
   ```
2. **Inspect Render Dashboard**:
   - Check service status in the Render Console for `behaviorsim-api`.
   - Review live application logs for unhandled traceback logs.
   - Filter logs by `[ERROR]` or search for specific `req:<request_id>` identifiers from user reports.
3. **Verify Neon Database Health**:
   - Log into [console.neon.tech](https://console.neon.tech).
   - Check compute status (active vs suspended).
   - Verify connection pool exhaustion (`active connections` near limit).
4. **Mitigation Actions**:
   - If Neon suspended: issue a dummy query or trigger restart.
   - If bad deploy: rollback instantly via Render Dashboard $\to$ **Deploys** $\to$ **Rollback to this deploy**.
   - If database credentials revoked: update `DATABASE_URL` in Render Environment tab.

---

### Runbook 2: Simulation Queue Backlog & Worker Stalling

**Trigger**: Warning Alert — `queue.depth > 50` or `queue.oldest_pending_age_seconds > 60`.

1. **Inspect Queue Diagnostics**:
   ```bash
   curl -s https://api.behavioursim.vedaangsharma.in/v1/diagnostics | jq .queue
   ```
2. **Analyze Worker Activity**:
   - Check `metrics.simulations.worker_throughput`:
     - If `jobs_per_second == 0` while `queue.depth > 0`, the background worker is not claiming jobs.
     - Review logs for `Worker claimed job` messages.
3. **Investigate Concurrency Blockers**:
   - Check if the backlog belongs to a single user who exhausted `max_concurrent_simulations` (Free plan limit = 1).
   - Other users' jobs should continue processing unless all candidates are blocked.
4. **Mitigation Actions**:
   - If worker is hung on engine compute: restart the API container via Render Dashboard.
   - The lease-recovery mechanism automatically reclaims orphaned `running` jobs after 300 seconds.
   - If sustained high volume: scale worker processes (e.g. increase worker instances or threads).

---

### Runbook 3: Elevated Simulation Failures

**Trigger**: Warning Alert — `metrics.simulations.failed` increasing or `failed_by_code` shows error spikes.

1. **Identify Error Code Distribution**:
   ```bash
   curl -s https://api.behavioursim.vedaangsharma.in/v1/diagnostics | jq .metrics.simulations.failed_by_code
   ```
   Common error codes:
   - `execution_timeout`: Simulation compute exceeded execution deadline or abandoned worker lease.
   - `queue_timeout`: Job waited in queue longer than `max_pending_seconds` (3600s).
   - `simulation_generation_failed`: Core engine threw an unhandled exception during `simulate()`.
2. **Search Logs for Relevant Simulation IDs**:
   ```text
   # In Render Log Search:
   "sim:" AND "[ERROR]"
   ```
3. **Mitigation Actions**:
   - If `execution_timeout` on large interaction counts: verify whether client requested 100k interactions under constrained CPU.
   - Quota invariant: all failed simulations automatically have interaction quota refunded to the user.

---

### Runbook 4: Stale / Failed Retention Cleanup

**Trigger**: Warning Alert — `health.cleanup_healthy == false` or `retention.hours_since_last_cleanup > 26`.

1. **Check Render Cron Execution Logs**:
   - In Render Dashboard, navigate to **Cron Jobs** $\to$ `behaviorsim-cleanup`.
   - Inspect the last execution timestamp and exit code.
   - An exit code of `1` indicates an unhandled error during cleanup.
2. **Run Manual Dry-Run Probe**:
   ```bash
   poetry run python -m app.cleanup --dry-run
   ```
   Verify:
   - Cutoff calculation succeeds.
   - Candidate count is reported without error.
   - Database connection is functional.
3. **Execute Manual Catch-Up Cleanup**:
   ```bash
   poetry run python -m app.cleanup
   ```
4. **Mitigation Actions**:
   - If Render Cron was not synced from Blueprint: create the cron job manually in Render Console as detailed in `DEPLOYMENT.md` Section 17.2.
   - If database lock timeout: decrease `SIMULATION_CLEANUP_BATCH_SIZE` from 100 to 50 via environment variable.

---

### Runbook 5: Neon Database Pressure & Connection Exhaustion

**Trigger**: Warning Alert — Neon storage $> 70\%$ or connection pool latency spiking.

1. **Inspect Neon Dashboard Metrics**:
   - Log into [console.neon.tech](https://console.neon.tech).
   - Review:
     - **Compute CPU & RAM utilization**.
     - **Active client connections**.
     - **Logical storage vs physical disk allocation**.
2. **Analyze Application Metrics**:
   - Check `metrics.api.latency_ms.avg` for latency inflation.
   - Check `metrics.retention.simulations_deleted_total`.
3. **Storage Lifecycle Check**:
   - Review autovacuum dead tuple reclamation:
     $$\text{New Simulations} \longrightarrow \text{7-Day Retention} \longrightarrow \text{DELETE} \longrightarrow \text{Dead Tuples} \longrightarrow \text{Autovacuum}$$
   - If storage is high but daily volume is steady: dead tuples are awaiting autovacuum.
4. **Mitigation Actions**:
   - If connection pool exhausted: verify Render web service is running single instance (`DB_POOL_SIZE=10`, `DB_MAX_OVERFLOW=20`).
   - If storage exceeds $85\%$: reduce retention window from 7 days to 3 days (`SIMULATION_RETENTION_DAYS=3`) and run manual cleanup.

---

## 6. Neon Monitoring Boundary

| Telemetry Domain | Observable from Application (`GET /v1/diagnostics`) | Observable ONLY from Neon Dashboard |
| :--- | :--- | :--- |
| **Query Latency** | HTTP round-trip latency, engine compute ms, queue wait ms. | Internal PostgreSQL engine execution time, planner cost. |
| **Queue Health** | Pending count, running count, oldest pending age. | N/A (Application construct). |
| **Connection Pooling** | Connection checkout errors, transaction failures. | Total active connections, PgBouncer pooler utilization. |
| **Database Storage** | Row counts, deleted row counts, logical retention cutoff. | Physical relation disk bytes, WAL disk space, TOAST tables. |
| **Engine Compute** | N/A | Compute CPU cores, memory utilization, cold-start resume time. |
| **Maintenance** | Cleanup execution timestamp, duration, batch counts. | Autovacuum frequency, dead tuple counts, checkpoint activity. |

---

## 7. Production Smoke Check Procedure

Operators can verify platform health in under 30 seconds using these curl commands:

```bash
# 1. Probe API liveness & readiness
curl -f -s https://api.behavioursim.vedaangsharma.in/health | jq .
curl -f -s https://api.behavioursim.vedaangsharma.in/ready | jq .

# 2. Probe Queue Backlog & Health Indicators
curl -f -s https://api.behavioursim.vedaangsharma.in/v1/diagnostics | jq '{health, queue}'

# 3. Probe Request Latency Histogram
curl -f -s https://api.behavioursim.vedaangsharma.in/v1/diagnostics | jq .metrics.api.latency_ms

# 4. Probe Retention Cleanup Freshness
curl -f -s https://api.behavioursim.vedaangsharma.in/v1/diagnostics | jq .metrics.retention

# 5. Verify Request Correlation ID
curl -I -s https://api.behavioursim.vedaangsharma.in/health | grep -i x-request-id
```
