# PROJECT_CONTEXT.md

Living status file for the Uber-like ride-hailing learning project. Claude Code reads this at the start of every session and updates it after every milestone. Milestone definitions live in `CLAUDE.md`.

## Project summary

Uber-like ride-hailing web app for learning. FastAPI + PostgreSQL/PostGIS + Redis backend with layered structure (routers, services, repositories), plain HTML/CSS/JS frontend (rider, driver, admin), simulated drivers, no ML layer.

## Current status

- Current phase: 1 (Auth, Roles, Ride State Machine), in progress
- Last completed milestone: M1.2 Driver onboarding (2026-10-08)
- Next milestone: M1.3 Ride state machine
- Last updated: 2026-10-08

## Milestone tracker

Status values: Not started, In progress, Done.

| ID | Milestone | Status | Date done |
|---|---|---|---|
| M0.1 | Repo and infra | Done | 2026-10-08 |
| M0.2 | Database layer | Done | 2026-10-08 |
| M1.1 | Authentication | Done | 2026-10-08 |
| M1.2 | Driver onboarding | Done | 2026-10-08 |
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
- **Tables** (migrations `0001_initial_schema` and `0002_unique_vehicle_per_driver`; ids are integer PKs, all `created_at` are timestamptz default `now()`, money is integer paise):
  - `users`: role (rider/driver/admin), name, email (unique), phone (unique, nullable), password_hash
  - `drivers`: user_id (FK users, unique), license_number, verification_status (pending/approved/rejected, default pending)
  - `vehicles`: driver_id (FK drivers, unique `uq_vehicles_driver_id`: one vehicle per driver), plate_number (unique, stored normalized: uppercase, no spaces or hyphens), model, color, vehicle_type (default economy)
  - `rides`: rider_id (FK users), driver_id (FK drivers, nullable), pickup/dropoff lat, lng, address, status (default REQUESTED), distance_m, duration_s, fare_estimate, final_fare, otp, started_at, completed_at. Indexed: status, rider_id, driver_id
  - `ride_events`: ride_id (FK, indexed), from_status (nullable), to_status, actor_user_id (FK users, nullable)
  - `payments`: ride_id (FK, indexed), amount, method (cash/wallet/card), status (pending/succeeded/failed/refunded), idempotency_key (unique), gateway_ref (unique, nullable)
  - `ratings`: ride_id, from_user_id, to_user_id (FKs), score (check 1 to 5), comment. Unique (ride_id, from_user_id)
  - `pricing_rules`: vehicle_type (unique), base_fare, per_km, per_min, min_fare, surge_cap (float, default 2.0)
  - Relationships: `Ride.events` (ordered by id), `Driver.user` (many to one) and `Driver.vehicle` (one to one), both `lazy="raise"`, so queries must eager load them (`selectinload`). Enums are Python enums in `models.py` (`UserRole`, `VerificationStatus`, `RideStatus`, `PaymentMethod`, `PaymentStatus`)
- **Endpoints:**
  - `GET /health`: 200 `{"status","postgres","redis"}` all "ok"; 503 with the failing one "error"
  - `POST /auth/register`: JSON `{name, email, phone?, password (min 8), role: rider|driver}`; 201 `UserResponse`; 409 if email or phone exists; 422 for role `admin`, short password, bad email
  - `POST /auth/login`: JSON `{email, password}`; 200 `{access_token, token_type: "bearer", user}`; 401 `Invalid email or password` for both wrong email and wrong password
  - `GET /auth/me`: needs `Authorization: Bearer <token>`; 200 `UserResponse`; 401 otherwise
  - `GET /drivers/me`: role driver; 200 `DriverResponse`; 404 if no profile yet
  - `POST /drivers/me/profile`: role driver; JSON `{license_number}` (stripped, 5 to 30 chars); 201 `DriverResponse` with status `pending` and `vehicle: null`; 409 if the user already has a profile; 422 for a short license
  - `POST /drivers/me/vehicle`: role driver; JSON `{plate_number, model, color}` (type is always `economy`); 201 `DriverResponse`; 409 if no profile yet, a vehicle already exists, or the plate is taken (any spelling of the same plate)
  - `GET /admin/drivers?status=pending|approved|rejected`: role admin; list of `DriverResponse`, oldest first; 422 for an unknown status
  - `POST /admin/drivers/{id}/approve` and `/reject`: role admin; 200 `DriverResponse`; 404 unknown id; 409 if already in that status; 409 when approving a driver with no vehicle. Rejected to approved is allowed
  - `DriverResponse`: `id, license_number, verification_status, created_at, user (UserResponse), vehicle (id, plate_number, model, color, vehicle_type, or null)`
- **JWT:** HS256, signed with `JWT_SECRET`. Claims: `sub` (user id as string), `role`, `exp` (`ACCESS_TOKEN_EXPIRE_MINUTES`, default 60). `get_current_user` returns 401 + `WWW-Authenticate: Bearer` for a missing, malformed, tampered, or expired token, or a user that no longer exists. `require_role("rider", ...)` returns 403 for other roles.
- **Admin creation (script only):** `docker compose exec backend python create_admin.py --email ... --name ... --password ...` (prints the id; does nothing if the email exists)
- **Static files:** `frontend/` is mounted at `/` after the API routes (folders are empty placeholders)
- **Backend files:** `app/config.py` (settings from env), `app/database.py` (`Base` with constraint naming convention, async engine, `async_session`, `get_db` dependency, Redis client), `app/models.py` (all models), `app/main.py`; `app/schemas.py`, `app/security.py` (argon2 hashing, JWT, `get_current_user`, `require_role`), `app/routers/auth.py`, `app/routers/drivers.py`, `app/routers/admin.py`, `app/services/auth.py`, `app/services/drivers.py`, `app/repositories/users.py`, `app/repositories/drivers.py`, `backend/create_admin.py`; `alembic.ini` and `alembic/` (async template; `env.py` takes the URL from settings); `utils/` is still an empty package
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
| M0.2 | Lat/lng are plain `Float` columns; PostGIS extension is enabled by the migration but unused | `geometry` columns, geoalchemy2 | Driver locations live in Redis GEO and distances are computed in Python/Redis; avoids a dependency |
| M0.2 | Enums are Python enums stored as `VARCHAR` (`native_enum=False`); enum names equal values | Native Postgres enum types | Adding a value later needs no enum-type migration |
| M0.2 | Money columns are `Integer` in minor units (paise) | Float, Numeric | No rounding errors; matches the project rule |
| M0.2 | `MetaData` naming convention (`ix_`, `uq_`, `ck_`, `fk_`, `pk_` prefixes) | Postgres auto-generated names | Predictable constraint names for later migrations |
| M0.2 | Alembic `include_object` ignores any database table that is not one of our models | Default autogenerate | The PostGIS image ships `spatial_ref_sys`, `tiger.*`, `topology.*`; unfiltered, autogenerate wrote `drop_table` for ~40 of them |
| M0.2 | Migration ids are manual and readable (`0001`, file `0001_initial_schema.py`) via `--rev-id`; downgrade leaves the postgis extension | Hash-only ids | Readable ordering |
| M1.1 | JSON body for `/auth/login` | OAuth2 password form | Frontends send JSON everywhere; Swagger's Authorize still works via `HTTPBearer` (paste a token) |
| M1.1 | Admins are created only by `create_admin.py` | An admin role on `/auth/register`, a bootstrap endpoint | No API path can ever create an admin; `RegisterRequest.role` is `Literal["rider","driver"]` |
| M1.1 | Only rider and driver can self-register | Any role | Same reason as above |
| M1.1 | `get_current_user` calls `repositories/users.py` directly | Routing it through a service | Dependency with no business rule; a service would be a pass-through. Exception applies only there |
| M1.1 | Wrong email and wrong password return the same 401 body; unknown email still runs an argon2 verify against a dummy hash | Different messages, or returning early for unknown email | Attacker cannot tell which part failed, by message or by response time |
| M1.1 | `HTTPBearer(auto_error=False)` and our own 401 | Default `auto_error` | One 401 response with `WWW-Authenticate: Bearer` for every auth failure, including a missing header; Swagger still shows Authorize |
| M1.1 | Added `get_by_phone` to the users repository | Catching the unique-constraint error | Needed for the 409 on duplicate phone; explicit check keeps the error message clear |
| M1.1 | Pinned: PyJWT 2.15.1, argon2-cffi 25.1.0, email-validator 2.3.0 | Unpinned | Current stable at 2026-10-08 |
| M1.2 | One vehicle per driver, enforced by a unique constraint on `vehicles.driver_id` (`uq_vehicles_driver_id`) | Checking only in the service | The constraint is the real protection against two simultaneous requests; the service check just gives a clear message |
| M1.2 | `vehicle_type` is always set to `economy` by the server; not in `VehicleCreate` | A client-supplied type | Single ride type for now; no way to claim a type that has no pricing rule |
| M1.2 | Plates are normalized before storing and checking: uppercase, spaces and hyphens removed. `VehicleCreate` requires at least one letter or digit | Storing plates as typed | `ka 01-ab 1234` and `KA01AB1234` are the same plate; the pattern stops `---` becoming an empty plate |
| M1.2 | Approving a driver requires a vehicle (409 otherwise). Rejecting does not | Allowing approval of a profile alone | An approved driver must be able to take a ride |
| M1.2 | A rejected driver can be approved again by an admin | One-way rejection | Admin may change their mind; no re-submission flow exists yet |
| M1.2 | `lazy="raise"` on `Driver.user` and `Driver.vehicle`; driver queries eager load both with `selectinload` and `populate_existing` | Default lazy loading | Async cannot lazy load, so a forgotten eager load fails loudly instead of `MissingGreenlet`. `populate_existing` refreshes a driver already in the session after a flush or commit (new `created_at`, new `vehicle`) |
| M1.2 | `IntegrityError` on flush or commit of a profile or vehicle is rolled back and returned as 409 | Letting it surface as 500 | Two simultaneous requests can both pass the check; the second hits the unique constraint |
| M1.2 | Admin router uses a router-level `require_role("admin")` dependency; the approve/reject handlers do not need the admin user | Passing the admin into every handler | No audit of who approved yet, so the user is unused |
| M0.1 | Pinned: fastapi 0.142.4, uvicorn 0.54.0, sqlalchemy 2.1.4, asyncpg 0.32.0, redis 8.1.0, pydantic-settings 2.15.0 (M0.2 added alembic 1.20.0) | Unpinned | Current stable at 2026-10-08 |

## Bugs hit

One entry per notable bug: milestone, symptom, root cause, fix.

- **M1.2:** none. Note for later: a failed insert (lost race) still consumes an id from the sequence, so ids are not gapless (a race test skipped driver id 4).
- **M0.2:** `alembic revision --autogenerate` produced a migration that dropped ~40 PostGIS tables (`tiger.*`, `topology.*`, `spatial_ref_sys`). Cause: the `postgis/postgis` image creates them, so Alembic sees them as tables missing from our models. Fix: `include_object` filter in `alembic/env.py`.

## Open issues and TODOs

- Drivers cannot edit a profile or vehicle, or resubmit a rejected profile. A rejected driver is stuck unless an admin approves them again.
- Approval does not gate anything yet. M2.3 must require `approved` status before a driver can go online.
- argon2 hashing is synchronous and blocks the event loop (about 100 ms per login or register). Revisit during the M7.2 load test (for example `run_in_executor`).
- Register has a check-then-insert race: two simultaneous requests with the same email can both pass the check, and the second hits the unique constraint and returns 500 instead of 409. Rare; revisit if it ever matters.
- Swagger Authorize has not been clicked through in a browser in M1.1 or M1.2 (no browser tool). `/docs` serves 200 and the OpenAPI schema declares `HTTPBearer` on `/auth/me` and on all six M1.2 routes.

## How to run and test

```bash
cp .env.example .env                  # first time only
docker compose up --build             # start db, redis, backend
curl localhost:8000/health            # expect 200 {"status":"ok","postgres":"ok","redis":"ok"}
docker compose stop redis             # /health now returns 503 with redis "error"
docker compose start redis
docker compose down                   # stop
docker compose down -v                # stop and delete the database volume (full reset)

# database migrations (run after every fresh database)
docker compose exec backend alembic upgrade head                              # create/update all tables
docker compose exec backend alembic downgrade base                            # remove all tables (postgis stays)
docker compose exec --user $(id -u):$(id -g) backend alembic revision --autogenerate -m "message" --rev-id 0002
docker compose exec db psql -U uber -d uber -c '\dt'                          # inspect tables

# driver onboarding (M1.2): use a driver token and an admin token from /auth/login
curl -X POST localhost:8000/drivers/me/profile -H "Authorization: Bearer $DRIVER" -H 'Content-Type: application/json' -d '{"license_number":"DL-12345"}'
curl -X POST localhost:8000/drivers/me/vehicle -H "Authorization: Bearer $DRIVER" -H 'Content-Type: application/json' -d '{"plate_number":"KA01AB1234","model":"Swift","color":"white"}'
curl "localhost:8000/admin/drivers?status=pending" -H "Authorization: Bearer $ADMIN"
curl -X POST localhost:8000/admin/drivers/1/approve -H "Authorization: Bearer $ADMIN"

# auth (M1.1)
docker compose exec backend python create_admin.py --email admin@example.com --name "Admin" --password 'at-least-8-chars'
curl -X POST localhost:8000/auth/register -H 'Content-Type: application/json' \
  -d '{"name":"Rita","email":"rita@example.com","password":"rider-pass-1","role":"rider"}'
curl -X POST localhost:8000/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"rita@example.com","password":"rider-pass-1"}'          # returns access_token
curl localhost:8000/auth/me -H "Authorization: Bearer $TOKEN"
# Swagger: http://localhost:8000/docs -> Authorize -> paste the access_token
```

After changing `requirements.txt`, rebuild: `docker compose up -d --build backend`.

Run `alembic revision` with `--user $(id -u):$(id -g)` so the new file on the host is not owned by root. Review every autogenerated migration by hand; a "check" autogenerate right after `upgrade head` should be empty.

No automated tests yet (first ones arrive with the state machine, M1.3). M1.1 and M1.2 were verified by hand with curl and psql.
