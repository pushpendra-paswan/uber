# PROJECT_CONTEXT.md

Living status file for the Uber-like ride-hailing learning project. Claude Code reads this at the start of every session and updates it after every milestone. Milestone definitions live in `CLAUDE.md`.

## Project summary

Uber-like ride-hailing web app for learning. FastAPI + PostgreSQL/PostGIS + Redis backend with layered structure (routers, services, repositories), plain HTML/CSS/JS frontend (rider, driver, admin), simulated drivers, no ML layer.

## Current status

- Current phase: 0 (Foundations)
- Last completed milestone: M0.1 Repo and infra (2026-10-08)
- Next milestone: M0.2 Database layer
- Last updated: 2026-10-08

## Milestone tracker

Status values: Not started, In progress, Done.

| ID | Milestone | Status | Date done |
|---|---|---|---|
| M0.1 | Repo and infra | Done | 2026-10-08 |
| M0.2 | Database layer | Not started | |
| M1.1 | Authentication | Not started | |
| M1.2 | Driver onboarding | Not started | |
| M1.3 | Ride state machine | Not started | |
| M1.4 | Frontend skeleton | Not started | |
| M2.1 | Map UI | Not started | |
| M2.2 | Routing and estimate | Not started | |
| M2.3 | Driver presence | Not started | |
| M2.4 | Matching v1 | Not started | |
| M2.5 | Driver simulator | Not started | |
| M3.1 | WS infrastructure | Not started | |
| M3.2 | Live tracking | Not started | |
| M3.3 | Offer flow | Not started | |
| M3.4 | Resilience | Not started | |
| M3.5 | Full trip flow | Not started | |
| M4.1 | Reproduce the bug | Not started | |
| M4.2 | Fix and compare | Not started | |
| M4.3 | Edge cases | Not started | |
| M5.1 | Final fare | Not started | |
| M5.2 | Surge pricing | Not started | |
| M5.3 | Payments | Not started | |
| M5.4 | Money views | Not started | |
| M6.1 | Ratings | Not started | |
| M6.2 | Admin dashboard | Not started | |
| M6.3 | Rider and driver history | Not started | |
| M7.1 | Logging and metrics | Not started | |
| M7.2 | Load test | Not started | |
| M7.3 | Documentation | Not started | |

## What exists now

- **Services (Docker Compose):** `db` (postgis/postgis:16-3.4, volume `postgres_data`, port 5432), `redis` (redis:7-alpine, port 6379), `backend` (FastAPI, port 8000, uvicorn --reload). Backend waits for db and redis to be healthy.
- **Tables:** none yet (M0.2)
- **Endpoints:** `GET /health` (200 `{"status","postgres","redis"}` all "ok"; 503 with the failing one "error")
- **Static files:** `frontend/` is mounted at `/` after the API routes (folders are empty placeholders)
- **Backend files:** `app/config.py` (settings from env), `app/database.py` (async engine and Redis client only), `app/main.py`; empty `routers/ services/ repositories/ utils/` packages
- **Redis keys:** none yet
- **WebSocket messages:** none yet
- **Frontend pages:** none yet

## Decisions log

One line per decision: milestone, what was chosen, what was rejected, why.

| Milestone | Chosen | Rejected | Why |
|---|---|---|---|
| Setup | Layered backend (routers, services, repositories, utils), plain functions, plain HTML/CSS/JS frontend | Frontend framework, ML layer | Learning focus: backend, real-time, concurrency |
| M0.1 | Repo root is `uber/` (not `uber-clone/`); built in place | Nested `uber-clone/` folder | The project folder already exists and holds CLAUDE.md |
| M0.1 | `postgis/postgis:16-3.4`, `redis:7-alpine`, `python:3.12-slim` | `latest` tags | Reproducible builds |
| M0.1 | Compose mounts `./backend` at `/app` and `./frontend` at `/frontend`; `main.py` serves `/frontend` | Serving `../frontend` by relative path | Container only sees mounted paths; same code works in the container |
| M0.1 | `/health` checks each dependency in its own try/except and returns `status: "error"` plus 503 if either fails | Failing on the first error | Reports both dependencies even when one is down |
| M0.1 | Pinned: fastapi 0.142.4, uvicorn 0.54.0, sqlalchemy 2.1.4, asyncpg 0.32.0, redis 8.1.0, pydantic-settings 2.15.0 | Unpinned | Current stable at 2026-10-08 |

## Bugs hit

One entry per notable bug: milestone, symptom, root cause, fix.

None yet.

## Open issues and TODOs

None yet.

## How to run and test

```bash
cp .env.example .env                  # first time only
docker compose up --build             # start db, redis, backend
curl localhost:8000/health            # expect 200 {"status":"ok","postgres":"ok","redis":"ok"}
docker compose stop redis             # /health now returns 503 with redis "error"
docker compose start redis
docker compose down                   # stop
docker compose down -v                # stop and delete the database volume (full reset)
```

No automated tests yet (first ones arrive with the state machine, M1.3).
