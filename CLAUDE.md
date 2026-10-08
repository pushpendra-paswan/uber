# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

An Uber-like ride-hailing web app, built **for learning**. Riders request trips, nearby drivers accept them, and both sides see live location, fare, and trip status.

- Three apps on one backend: **rider**, **driver**, **admin**
- Single city, single ride type, drivers are simulated by a script
- Payments are sandbox or wallet only, no real money
- **No ML layer.** Surge pricing is rule-based (demand/supply counts).
- Learning goals: ride state machine, real-time with WebSockets, Redis geo, concurrency control, idempotent payments

## Project context file (read first, update always)

`PROJECT_CONTEXT.md` in the repo root is the living status file for this project.

- **At the start of every session**, read `PROJECT_CONTEXT.md` before doing anything else. It says which milestone is done, which is next, and what decisions were made.
- **After every milestone**, update it before reporting the milestone as finished. This is part of the definition of done. Update:
  - the milestone tracker (status and date)
  - "Current status" (current phase, last completed milestone, next milestone)
  - "What exists now" (tables, endpoints, Redis keys, WebSocket messages added or changed, as short lists)
  - the decisions log (what was chosen, what was rejected, why)
  - the bugs log (anything notable that broke and how it was fixed)
  - open issues and "how to run / test" if they changed
- Keep it short and current. Rewrite sections rather than appending history. Do not paste code into it.
- Never mark a milestone Done if its tests are failing or the feature does not work end to end.
- If you notice the file is out of date with the code, fix the file.

## Tech stack

| Layer | Choice |
|---|---|
| Frontend | HTML, CSS, plain JavaScript (ES modules). No framework, no bundler, no npm. |
| Maps | Leaflet + OpenStreetMap tiles |
| Backend | Python, FastAPI (async), native WebSockets |
| Database | PostgreSQL + PostGIS, SQLAlchemy 2.0 (async), Alembic |
| Real-time / cache | Redis (GEO commands, pub/sub, locks) |
| Auth | JWT + password hashing, roles: rider, driver, admin |
| Routing / ETA | OSRM (Docker) |
| Geocoding | Nominatim, results cached in Redis |
| Payments | Internal wallet first, then Stripe test mode |
| Infra | Docker Compose |
| Tests | pytest, pytest-asyncio, httpx. Load tests with Locust. |

FastAPI serves the static frontend files, so there is one server in development.

## Project structure

Keep this structure. Files inside `services/`, `repositories/`, and `utils/` are created when a milestone needs them, not ahead of time.

```
uber-clone/
├── CLAUDE.md
├── PROJECT_CONTEXT.md
├── README.md
├── docker-compose.yml
├── .env.example
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── alembic.ini
│   ├── alembic/
│   ├── app/
│   │   ├── main.py            # app creation, router registration, static files
│   │   ├── config.py          # settings from .env
│   │   ├── database.py        # Postgres engine/session and Redis client
│   │   ├── models.py          # all SQLAlchemy models
│   │   ├── schemas.py         # all Pydantic schemas
│   │   ├── security.py        # password hashing, JWT, current-user dependencies
│   │   ├── routers/           # HTTP and WebSocket layer
│   │   │   ├── auth.py
│   │   │   ├── drivers.py
│   │   │   ├── rides.py
│   │   │   ├── payments.py
│   │   │   ├── admin.py
│   │   │   └── websocket.py
│   │   ├── services/          # business logic
│   │   │   ├── auth.py
│   │   │   ├── drivers.py
│   │   │   ├── rides.py       # ride lifecycle and state machine
│   │   │   ├── matching.py
│   │   │   ├── pricing.py     # fare estimate, final fare, surge
│   │   │   └── payments.py
│   │   ├── repositories/      # all database and Redis access
│   │   │   ├── users.py
│   │   │   ├── drivers.py     # includes driver locations in Redis GEO
│   │   │   ├── rides.py
│   │   │   ├── payments.py
│   │   │   └── ratings.py
│   │   └── utils/             # small generic pure functions (geo.py, money.py, ...)
│   └── tests/
├── frontend/
│   ├── shared/                # api.js (fetch + token), base.css
│   ├── rider/                 # index.html, rider.js, rider.css
│   ├── driver/                # index.html, driver.js, driver.css
│   └── admin/                 # index.html, admin.js, admin.css
└── simulator/
    └── simulator.py           # fake drivers and load script
```

## Architecture: routers → services → repositories

Calls only go one way: **router → service → repository**. Never skip a layer and never call upwards.

| Layer | Does | Does not |
|---|---|---|
| **Router** | Receives the request, validates it with a schema, checks the user role, calls one service function, returns the response. Handles WebSocket connect/receive/send. | Query the database. Hold business rules. |
| **Service** | Business rules: state machine, matching, fare and surge, payment logic, permission rules on a specific ride. Calls repositories (and other services when needed). Commits once at the end of a use case. | Know about FastAPI request objects. Write SQL or Redis commands directly. |
| **Repository** | All SQLAlchemy queries and all Redis commands (driver locations, locks, caches). Returns model objects or plain values. | Hold business rules. Commit transactions. |

Example: `POST /rides` → `routers/rides.py` validates the body and the rider role → `services/rides.py` gets the fare from `services/pricing.py`, creates the ride through `repositories/rides.py`, calls `change_ride_status()`, commits → router returns the ride.

Services raise `HTTPException` directly. No custom exception hierarchy.

## Coding style

The code in this project is **simple and plain**. A beginner should be able to read any file top to bottom and follow it. The layers above are the only structure; do not add more.

### Rules

- **Repositories and services are plain async functions**, not classes. No `BaseRepository`, no generic CRUD base, no abstract classes.
- **No unnecessary helper functions.** A private helper is allowed only when the same code is needed in two or more places. If a function is used once, inline it.
- **A helper must never call another helper.** If that seems necessary, inline one of them. (A service calling repository functions is the architecture, not a helper chain.)
- **Utilities in `utils/` must be generic, pure, and used in two or more places** (distance calculation, geohash cell, money conversion). Do not create a utility ahead of need. A utility never calls another utility.
- **One file per domain per layer.** Do not split a file further unless it has grown past roughly 400 lines or has a clearly separate responsibility. Ask before doing it.
- **No extra abstractions.** No dependency-injection containers, factories, event buses, or plugin systems.
- **No classes** except SQLAlchemy models and Pydantic schemas.
- **No wrappers** around libraries that already have a simple API. Repositories call SQLAlchemy and Redis directly.
- **No speculative code.** No unused parameters, no config for things we do not need yet, no "future-proofing".
- **Minimal error handling.** Validate at the edges (request schemas, permission checks, state transitions), raise a clear HTTP error, and move on. No defensive try/except around code that should not fail.
- **Clear names over comments.** Use short comments only to explain *why*, never to repeat *what*.
- **Constants at the top of the file** (radius, timeouts, base fare) with plain names, not a settings framework.

### Allowed shared functions (used in many places)

- `ALLOWED_TRANSITIONS` dict and one `change_ride_status()` function in `services/rides.py`. Every ride status change goes through it, and it writes to `ride_events`.
- `get_current_user` and role-check dependencies in `security.py`.
- `api.js` in `frontend/shared/`, since all three frontends call the backend.

### Backend conventions

- Async everywhere: async routes, async SQLAlchemy sessions, async Redis client.
- Use type hints on function signatures. Use Pydantic schemas for request and response bodies.
- Schema changes go through Alembic migrations. Never edit the database by hand.
- Driver live locations live in **Redis GEO**, not Postgres. Postgres stores durable data only.
- Money is stored as integer minor units (paise/cents), never floats.
- Payment and webhook handlers must be **idempotent** (idempotency keys, dedupe on gateway reference).
- Ride status changes only through `change_ride_status()`. Never set `ride.status` directly elsewhere.

### Ride state machine

```
REQUESTED → DRIVER_ASSIGNED → DRIVER_ARRIVED → IN_PROGRESS → COMPLETED
REQUESTED / DRIVER_ASSIGNED / DRIVER_ARRIVED → CANCELLED
REQUESTED → NO_DRIVER_FOUND
```

### Frontend conventions

- Plain HTML, CSS, and JavaScript. Use `<script type="module">`. No libraries except Leaflet.
- One JS file and one CSS file per app. The only shared JS module is `shared/api.js`.
- One `state` object per page and one `render()` function that updates the DOM from it.
- Call `fetch` through `shared/api.js` (it adds the JWT header). Use the browser `WebSocket` directly.
- On WebSocket reconnect, re-fetch the current ride state from the REST API.
- Keep HTML semantic and CSS simple. No CSS frameworks.

## Commands

```bash
docker compose up --build                 # start everything (api, postgres, redis, osrm)
docker compose exec backend alembic upgrade head
docker compose exec backend alembic revision --autogenerate -m "message"
docker compose exec backend pytest
python simulator/simulator.py --drivers 50
```

## Milestones

29 milestones across 8 phases. Phases 0 to 3 are the MVP. Work one milestone at a time, in order.

### Phase 0: Foundations
- **M0.1 Repo and infra:** folder structure, Docker Compose (Postgres+PostGIS, Redis, backend), `.env` config, `/health` endpoint
- **M0.2 Database layer:** async SQLAlchemy setup, Alembic, initial migration for `users`, `drivers`, `vehicles`, `rides`, `ride_events`, `payments`, `ratings`, `pricing_rules`

Done when: `docker compose up` gives a running API with migrated tables.

### Phase 1: Auth, Roles, Ride State Machine
- **M1.1 Authentication:** register/login, password hashing, JWT, role guards (rider, driver, admin)
- **M1.2 Driver onboarding:** driver profile and vehicle, admin approve endpoint (no real document checks)
- **M1.3 Ride state machine:** all states and transitions enforced in `change_ride_status()`, every change logged to `ride_events`
- **M1.4 Frontend skeleton:** `shared/api.js`, login pages, bare rider, driver, and minimal admin pages that call the endpoints

Done when: tests prove illegal transitions are rejected, and a ride can be clicked through each state manually.

### Phase 2: Maps, Geo, Fare Estimate, Matching (REST)
- **M2.1 Map UI:** Leaflet map, Nominatim place search with Redis caching, pickup and drop-off selection
- **M2.2 Routing and estimate:** OSRM in Docker, route polyline, distance and duration, fare estimate (`base + per_km + per_min`, minimum fare)
- **M2.3 Driver presence:** online/offline toggle, location updates stored in Redis GEO
- **M2.4 Matching v1:** `GEOSEARCH` within a radius, rank by distance, assign the nearest available driver
- **M2.5 Driver simulator:** script that spawns N fake drivers moving along OSRM routes and pinging locations

Done when: a rider picks two points, sees the estimate, requests, and gets matched to a simulated driver.

### Phase 3: Real-Time with WebSockets
- **M3.1 WS infrastructure:** authenticated connections, a connection manager, Redis pub/sub fan-out
- **M3.2 Live tracking:** driver locations pushed to the rider's map, with smooth marker movement
- **M3.3 Offer flow:** driver gets a popup with an accept/reject countdown, and a timeout or reject triggers the next candidate
- **M3.4 Resilience:** reconnect with backoff, and re-fetch current ride state on reconnect
- **M3.5 Full trip flow:** arrived, OTP verification (fake `1234`), start, complete, and cancel from either side

Done when: an entire trip completes across two browser windows with live movement and no page refreshes.

### Phase 4: Concurrency Hardening
- **M4.1 Reproduce the bug:** fire many simultaneous ride requests at the simulator fleet and observe double assignments
- **M4.2 Fix and compare:** Redis lock (`SET NX PX`) vs Postgres `FOR UPDATE SKIP LOCKED`, and keep the better one
- **M4.3 Edge cases:** driver disconnects mid-offer, rider cancels during assignment, accept-after-timeout, duplicate accepts, all covered by tests

Done when: a stress test shows zero double assignments and no stuck rides.

### Phase 5: Pricing and Payments
- **M5.1 Final fare:** computed from actual trip distance and time, plus cancellation fees
- **M5.2 Surge pricing:** geohash zones, multiplier from the open-requests-to-available-drivers ratio, with a cap
- **M5.3 Payments:** internal wallet first, then Stripe test mode, with idempotency keys and a webhook handler
- **M5.4 Money views:** driver earnings, platform commission, rider receipts

Done when: retrying a payment or replaying a webhook never charges twice.

### Phase 6: Admin, Ratings, History
- **M6.1 Ratings:** two-way ratings after a trip, with running averages
- **M6.2 Admin dashboard:** driver approvals, pricing rule editor, live rides and drivers map, revenue and ride stats
- **M6.3 Rider and driver history:** past trips, saved places, receipts

Done when: an admin can run the platform without touching the database.

### Phase 7: Observability, Load Testing, Polish
- **M7.1 Logging and metrics:** structured logs with ride IDs, plus basic Prometheus/Grafana (optional)
- **M7.2 Load test:** Locust scenarios, find the first bottleneck, fix one, and re-measure
- **M7.3 Documentation:** README, architecture diagram, and a short demo recording

Done when: you can state how many concurrent rides it handles and what broke first.

## Working agreement

- Read `PROJECT_CONTEXT.md` first. Work on the milestone it lists as next, unless told otherwise.
- Before writing code for a milestone, state in a few lines what you will build and which files you will touch.
- Prefer the smallest change that works. Do not refactor code that is not part of the task.
- Write tests for the state machine, matching, concurrency cases, and payment idempotency. Skip tests for trivial CRUD.
- A milestone is done only when: the feature works end to end, relevant tests pass, and `PROJECT_CONTEXT.md` is updated. Update `README.md` too if run or setup instructions changed.
- After finishing a milestone, give a short summary and stop. Wait for the next milestone prompt.
- If a request conflicts with the coding style above, follow the style and mention the conflict.
- Ask before adding a new dependency, a new folder, or splitting a file.
