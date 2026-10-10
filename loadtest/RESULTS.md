# Load test results (M7.2, stopped at the baseline)

M7.2 was stopped after the baseline: **no bottleneck experiment, no fix, no after-fix runs and no revert run were done.** What is here is the harness, the baseline and the evidence collected so far. The bottleneck below is a hypothesis, not a confirmed cause.

## Method in short

One backend process (uvicorn with `--reload`, uvloop, pool of 5 + 10) on a 12 CPU, 7.2 GB machine that also ran the generator, the 80-driver simulator fleet, Postgres (defaults, `pg_stat_statements`), Redis and OSRM. Load comes from Locust users that send the request mix of the real pages every 3 s (30 percent of the riders request rides; rider WebSockets, geocoding and Stripe are not simulated; the simulator opens no WebSocket). Steps of 10, 25, 50, 100, 150, 200, 300, 400 users, 30 s warm-up and 90 s hold, spawn rate 5 per second. SLOs on the hold period: core `GET`s p95 < 300 ms and p99 < 1000 ms, `POST /rides/estimate` and `POST /rides` p95 < 800 ms, error rate < 0.5 percent. Every run starts from a reset of the load database (template) and ends with the correctness checks (I1 to I24, metrics against `ride_events`, `POST /rides` counts, `observability_errors_total`, no active ride): **all three baseline runs passed all checks.** No step was INVALID (the Locust process used 2 to 26 percent of one core).

## Baseline: three runs

**Capacity: 100 users in all three runs. Knee: 150 users in all three runs.** (The steps are coarse: the true limit is somewhere between 100 and 150 users.)

| users | req/s of the Locust users (min-max) | core p95 ms | core p99 ms | loop lag p95 ms | backend CPU % |
|---|---|---|---|---|---|
| 10 | 13 | 20-28 | 35-57 | 4-5 | 53-54 |
| 25 | 34 | 26-41 | 55-58 | 5 | 55-60 |
| 50 | 70 | 49-83 | 76-125 | 6-13 | 70-73 |
| 100 | 143 | 112-130 | 169-203 | 23 | 94-97 |
| 150 | 202-207 | 1847-2156 | 3029-3483 | 25 | 102 |
| 200 | 212-215 | 3875-3943 | 6326-6504 | 25 | 102 |
| 300 | 223-225 | 6355-6376 | 10694-10987 | 25-28 | 102 |
| 400 | 221-228 | 8318-8634 | 14078-14376 | 25-31 | 102-105 |

From 150 users the throughput stops growing (about 225 req/s from the users, about 268 req/s counting the fleet) while latency grows with the number of users: no error rate to speak of (0.0 to 0.24 percent), only waiting.

## Probes (single endpoints, 20 users, fleet running, one run)

| endpoint | req/s | client p50 / p95 ms | db ms mean | non-db ms mean |
|---|---|---|---|---|
| `GET /health` | 449 | 39 / 79 | - | - |
| `POST /auth/login` | 6 | 1775 / 7238 | 85.9 | 2670 |
| `GET /rides/active` | 236 | 59 / 200 | 22.8 | 56 |
| `GET /wallet` | 197 | 44 / 318 | 26.9 | 90 |
| `GET /saved-places` | 235 | 34 / 278 | 20.6 | 80 |
| `GET /rides/history` | 150 | 46 / 458 | 26.8 | 128 |
| `POST /rides/estimate` | 41 | 436 / 1200 | 43.1 | 458 |
| `GET /admin/live` | 64 | 115 / 1044 | 61.4 | 272 |
| `GET /admin/stats` | 45 | 362 / 1137 | 114.1 | 338 |
| `GET /admin/surge` | 272 | 36 / 209 | 13.1 | 72 |

Most expensive per request: login (password hashing on the event loop, about 110 ms of CPU each), then the estimate (OSRM call) and the admin stats. The health endpoint, the floor of the framework with one `SELECT 1` and one Redis ping, reaches 449 req/s.

## The four signals behind the hypothesis "the single event loop (one core of Python) is the limit"

1. **CPU and throughput.** From 150 users the backend container sits at 102 percent of one core while total server throughput stays at 264-270 req/s: about 3.8 ms of backend CPU per request, constant from 150 to 400 users (1 / 3.8 ms is about 263 req/s).
2. **The database is idle.** Postgres CPU falls from 60 to 28 percent when the knee is passed, costs about 1 ms per request, mean statement time in `pg_stat_statements` is 0.02 ms, and `pg_stat_activity` shows 0.3 connections active but 10.7 "idle in transaction": connections held by requests that wait for the app, not for the database.
3. **The time is outside the database.** At 200 users a `GET /wallet` takes 775 ms on the server against 57 ms at 100 users, but its database time is 30 ms against 26 ms and its query count is the same (3).
4. **Event loop lag.** 0.6 percent of the lag monitor's wake-ups are more than 10 ms late at 10 users, 20 percent at 100, 58 to 65 percent from 150.

**Not ruled out, and the reason this is only a hypothesis:** the connection pool (15 connections at most) is full from 100 users and 236 to 576 requests are in flight above the knee, so a request may wait for a pool connection instead of for the CPU. The CPU at 102 percent argues for the CPU, but a one-variable experiment (a larger pool; `LOG_LEVEL=WARNING` to price the access log; a throwaway cProfile to see where the 3.8 ms go) was not run. The next step would be those three experiments, then a written prediction, one fix and a revert run.

## What the harness found on the way (details in `PROJECT_CONTEXT.md`)

- Two bugs of the generator, found by the harness validation and fixed: a failed login ended the whole run; a crashed task restarted without a wait.
- A Redis outage of 10 s made unrelated reads 2 to 6 s slow (the fleet's location pings hold a database connection while they wait for Redis). A Postgres outage of 10 s showed up as 99 percent fast errors and stopped the run through the collapse rule.
- Validation: request counts per route equal the server's counters (0.00 percent difference on 15 routes); percentiles equal Locust's within 3 percent on the aggregate (one rank apart on small samples); the same seed gives the same trips; the lag monitor costs nothing measurable (median `/health` 3.28 ms before, 3.04 ms after, run-to-run noise 2.6 to 4.3 ms).

## What limits how far these numbers generalize

One machine (the generator, fleet, Postgres, Redis, OSRM and the backend share 12 cores), Docker networking of this host, `--reload` mode, one backend process, an empty database at the start (history and admin queries get slower as it grows), a fixed 80-driver fleet that sends fewer requests than the real driver page, no WebSocket traffic, Postgres at its defaults, fake riders with fixed behavior.
