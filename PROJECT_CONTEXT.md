# PROJECT_CONTEXT.md

Living status file for the Uber-like ride-hailing learning project. Claude Code reads this at the start of every session and updates it after every milestone. Milestone definitions live in `CLAUDE.md`.

## Project summary

Uber-like ride-hailing web app for learning. FastAPI + PostgreSQL/PostGIS + Redis backend with layered structure (routers, services, repositories), plain HTML/CSS/JS frontend (rider, driver, admin), simulated drivers, no ML layer.

## Current status

- Current phase: 1 (Auth, Roles, Ride State Machine) is complete. Phase 2 (Maps, Geo, Fare Estimate, Matching) is next
- Last completed milestone: M1.4 Frontend skeleton (2026-10-08)
- Next milestone: M2.1 Map UI
- Last updated: 2026-10-08

## Milestone tracker

Status values: Not started, In progress, Done.

| ID | Milestone | Status | Date done |
|---|---|---|---|
| M0.1 | Repo and infra | Done | 2026-10-08 |
| M0.2 | Database layer | Done | 2026-10-08 |
| M1.1 | Authentication | Done | 2026-10-08 |
| M1.2 | Driver onboarding | Done | 2026-10-08 |
| M1.3 | Ride state machine | Done | 2026-10-08 |
| M1.4 | Frontend skeleton | Done | 2026-10-08 |
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
  - Relationships: `Ride.events` (ordered by id), `Driver.user` (many to one) and `Driver.vehicle` (one to one), all `lazy="raise"`. Driver queries eager load with `selectinload`; ride events are never loaded through the ride, only via `rides_repo.list_events`. Enums are Python enums in `models.py` (`UserRole`, `VerificationStatus`, `RideStatus`, `PaymentMethod`, `PaymentStatus`), plus the constant `ACTIVE_RIDE_STATUSES` (REQUESTED, DRIVER_ASSIGNED, DRIVER_ARRIVED, IN_PROGRESS)
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
  - `POST /rides`: role rider; JSON `{pickup_lat, pickup_lng, pickup_address, dropoff_lat, dropoff_lng, dropoff_address}`; 201 `RideResponse` with status `REQUESTED`; 409 if the rider already has an active ride; 422 for out-of-range coordinates, blank address, or identical pickup and drop-off
  - `GET /rides/active`: role rider or driver; their active ride, or 404 `No active ride`; 403 for admin. Declared before `/rides/{ride_id}`
  - `GET /rides/{ride_id}` and `GET /rides/{ride_id}/events`: any logged-in user who is the ride's rider, its assigned driver, or an admin. Everyone else, and unknown ids, get the same 404 `Ride not found`
  - `POST /rides/{ride_id}/arrive`, `/start`, `/complete`: role driver (403 otherwise), must be the assigned driver (404 otherwise); 200 `RideResponse`; 409 `Cannot change ride from X to Y` if the transition is not allowed. `/start` sets `started_at` and does not check an OTP yet; `/complete` sets `completed_at` and leaves `final_fare` null
  - `POST /rides/{ride_id}/cancel`: role rider or driver; the rider from REQUESTED / DRIVER_ASSIGNED / DRIVER_ARRIVED, the assigned driver from DRIVER_ASSIGNED / DRIVER_ARRIVED; 409 after IN_PROGRESS or when already ended. Cancelling ends the ride
  - `POST /admin/rides/{ride_id}/assign`: role admin; JSON `{driver_id}`; 200 `RideResponse` with `DRIVER_ASSIGNED`; 404 unknown driver; 409 if the driver is not approved, already has an active ride, or the ride is not REQUESTED. Temporary stand-in for matching (M2.4)
  - `RideResponse`: `id, rider_id, driver_id, pickup_*/dropoff_* (lat, lng, address), status, distance_m, duration_s, fare_estimate, final_fare, created_at, started_at, completed_at`. No `otp`. `RideEventResponse`: `id, from_status, to_status, actor_user_id, created_at`
  - `DriverResponse`: `id, license_number, verification_status, created_at, user (UserResponse), vehicle (id, plate_number, model, color, vehicle_type, or null)`
- **Ride state machine** (`ALLOWED_TRANSITIONS` and `change_ride_status()` in `services/rides.py`; the only code that sets `ride.status` after creation, apart from `create_ride`, which inserts REQUESTED and writes the first event with `from_status` null):

  | From | Allowed to |
  |---|---|
  | REQUESTED | DRIVER_ASSIGNED, CANCELLED, NO_DRIVER_FOUND |
  | DRIVER_ASSIGNED | DRIVER_ARRIVED, CANCELLED |
  | DRIVER_ARRIVED | IN_PROGRESS, CANCELLED |
  | IN_PROGRESS | COMPLETED |
  | COMPLETED, CANCELLED, NO_DRIVER_FOUND | nothing |

  Every transition loads the ride with `SELECT ... FOR UPDATE`, writes a `ride_events` row, and commits once. NO_DRIVER_FOUND has no endpoint yet (matching, M2.4 / M3.3); it is tested by calling the function directly.
- **Tests:** `docker compose exec backend pytest` (69 tests, about 12 s). They run on a separate database `<POSTGRES_DB>_test` (created if missing; tables dropped and created from the models at session start, every table truncated with `RESTART IDENTITY CASCADE` before each test). `conftest.py` refuses to import unless the database name ends with `_test`, uses `NullPool` so simultaneous requests get separate connections, and overrides `get_db`. Fixtures: `client`, `db`, `rider`, `driver` (approved, with vehicle), `admin`, and `make_user(role, approved=True)` for more users. Users get a token directly from `create_access_token` (no login). `test_rides.py` covers: all 49 status pairs against a hand-written list of legal pairs, happy path with events, cancel rules, one active ride per rider, 404/403 access rules, assign rules, two simultaneous cancels, `/rides/active`, ride body validation. `pytest.ini`: `asyncio_mode = auto`, session-scoped event loop, `pythonpath = .`
- **JWT:** HS256, signed with `JWT_SECRET`. Claims: `sub` (user id as string), `role`, `exp` (`ACCESS_TOKEN_EXPIRE_MINUTES`, default 60). `get_current_user` returns 401 + `WWW-Authenticate: Bearer` for a missing, malformed, tampered, or expired token, or a user that no longer exists. `require_role("rider", ...)` returns 403 for other roles.
- **Admin creation (script only):** `docker compose exec backend python create_admin.py --email ... --name ... --password ...` (prints the id; does nothing if the email exists)
- **Static files:** `frontend/` is mounted at `/` with `html=True` after the API routes, so `/rider/` serves `rider/index.html` (`/rider` redirects to `/rider/`)
- **Backend files:** `app/config.py` (settings from env), `app/database.py` (`Base` with constraint naming convention, async engine, `async_session`, `get_db` dependency, Redis client), `app/models.py` (all models), `app/main.py`; `app/schemas.py`, `app/security.py` (argon2 hashing, JWT, `get_current_user`, `require_role`), `app/routers/auth.py`, `app/routers/drivers.py`, `app/routers/admin.py`, `app/services/auth.py`, `app/services/drivers.py`, `app/routers/rides.py`, `app/services/rides.py`, `app/repositories/users.py`, `app/repositories/drivers.py`, `app/repositories/rides.py`, `backend/create_admin.py`, `backend/pytest.ini`, `backend/tests/conftest.py`, `backend/tests/test_rides.py`; `alembic.ini` and `alembic/` (async template; `env.py` takes the URL from settings); `utils/` is still an empty package
- **Redis keys:** none yet
- **WebSocket messages:** none yet
- **Frontend pages** (plain HTML/CSS/ES modules, no libraries; each app is one page with its login form inside; all poll every 3 s):
  - `/rider/` (`rider.js`): log in or register (role rider, phone sent only if filled). Logged in as another role: "This account is a X. Open /X/ instead." Rider: request form with placeholder coordinates (MG Road to Koramangala, typed by hand until M2.1) calls `POST /rides`. Active ride shows id, status text, addresses, driver id once assigned, and the events timeline; Cancel (with `confirm()`) in REQUESTED / DRIVER_ASSIGNED / DRIVER_ARRIVED. A finished ride stays on screen (fetched once with `GET /rides/{id}` after `/rides/active` turns 404) with a "Request a new ride" button
  - `/driver/` (`driver.js`): same login/register with role driver. Shows the driver id, then walks profile form, vehicle form, and the verification text (pending / rejected / approved). When approved: "No ride assigned yet." or the ride with buttons by status (arrived, start, complete, cancel) and the same finished-ride behavior
  - `/admin/` (`admin.js`): login only (admins come from `create_admin.py`). Drivers table with status filter, Refresh, Approve and Reject on every row (backend errors show in the message area). Assign form (ride id, driver id) labelled as a temporary stand-in for matching. Minimal on purpose; the real dashboard is M6.2
  - `shared/api.js`: `api(method, path, body)`, `saveSession`, `getSession`, `clearSession`. Session lives in `sessionStorage` under the key `session` (`{token, user}`). Thrown errors carry `.status` and a readable message (`detail`, or `field: message` for a 422). A 401 with a token clears the session and reloads; a 401 without a token (wrong password) just throws. `shared/base.css` is the common stylesheet; each app has one small CSS file
  - Each page has one `state` object, one `render()` (shows/hides sections, sets `textContent`, never touches inputs), one `refresh()`, and one `act(fn)` used by every button

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
| M1.3 | One `ALLOWED_TRANSITIONS` dict and one `change_ride_status()`; every other path (arrive, start, complete, cancel, assign) calls it | Per-endpoint status checks | One place defines the rules, so the audit trail and the rules cannot drift apart. `create_ride` is the one exception (no previous status) |
| M1.3 | Every transition loads the ride with `FOR UPDATE` (row lock per ride) | Optimistic checks, or no locking | Two simultaneous requests on one ride serialize: the second sees the new status and gets 409. Verified: the simultaneous-cancel test fails 3 of 3 runs without the lock |
| M1.3 | A ride you are not part of returns 404, same as an unknown id; wrong role returns 403 | 403 for other people's rides | Does not reveal which ride ids exist |
| M1.3 | A driver cancelling ends the ride (CANCELLED); it does not return to REQUESTED | Re-opening the ride for matching | State machine has no backward edge; a re-request is a new ride (rematching can be added with M3.3 if wanted) |
| M1.3 | `POST /admin/rides/{id}/assign` as a temporary stand-in for matching | Waiting until M2.4 to test full rides | Lets a ride be clicked through every state now. Keep or remove after M2.4 |
| M1.3 | Tests use a separate `<db>_test` database with `NullPool`, a guard on the name, and truncation before each test | Rolling back a transaction per test, or the dev database | Rollback-per-test cannot test simultaneous requests (they need separate committed connections); the name guard stops tests wiping dev data |
| M1.3 | Tests skip trivial CRUD and the M1.1 / M1.2 endpoints | Full coverage | Per the working agreement: test state machine, matching, concurrency, payments |
| M1.3 | Deliberately no protection against one driver on two rides or one rider with two rides under simultaneous requests: only the plain check in `assign_driver` / `create_ride` | A lock or unique index now | M4.1 reproduces the driver double-assignment on purpose |
| M1.3 | Pinned test packages: pytest 9.1.1, pytest-asyncio 1.4.0, httpx 0.28.1 | Unpinned | Current stable at 2026-10-08 |
| M1.4 | Session in `sessionStorage` (per tab) | `localStorage` | Rider, driver, and admin can be logged in at once in three tabs of one browser |
| M1.4 | Poll every 3 s with `setInterval` (skipped while a previous refresh or a button action is running; not paused in background tabs) | WebSockets now | WebSockets come in M3; polling lets a ride be clicked through today. Background tabs keep polling because several tabs are used for testing |
| M1.4 | Minimal admin page (drivers table, approve/reject, assign form) built now | Waiting for M6.2, or using curl | Without it, drivers cannot be approved and rides cannot be assigned in the browser |
| M1.4 | API data goes into the page with `textContent` or `createElement` only, never `innerHTML` | `innerHTML` templates | Names and addresses are user input (checked with a rider and driver named `<img src=x onerror=alert(1)>`) |
| M1.4 | `render()` never touches inputs; forms are only reset by their submit handler after a successful login or register | Re-rendering whole sections | Polling re-renders every 3 s and would erase what the user is typing (checked: 10 s of polling keeps a half-filled form) |
| M1.4 | Ride coordinates are typed by hand (prefilled placeholders) | A map or geocoder | Map and Nominatim arrive in M2.1 |
| M1.4 | Admin table is only redrawn when its data changed | Redrawing on every poll | A redraw between mouse down and mouse up on Approve would swallow the click |
| M1.4 | Thrown API errors carry `.status` | Matching on message text | Pages need to tell a 404 (no ride, no profile) from a real failure |
| M1.4 | No backend change: `main.py` already mounted `frontend/` with `html=True` | Editing the mount | Nothing to fix |
| M0.1 | Pinned: fastapi 0.142.4, uvicorn 0.54.0, sqlalchemy 2.1.4, asyncpg 0.32.0, redis 8.1.0, pydantic-settings 2.15.0 (M0.2 added alembic 1.20.0) | Unpinned | Current stable at 2026-10-08 |

## Bugs hit

One entry per notable bug: milestone, symptom, root cause, fix.

- **M1.4:** none in the frontend. The browser click-through passed on its first full run. Expect red 404 lines in the browser console while polling: they are `/rides/active` and `/drivers/me` answering "nothing yet", which the pages treat as normal.
- **M1.3:** the first version of the 49-pair transition test used the app's own `ALLOWED_TRANSITIONS` as the expected answer, so a deliberate wrong change to the dict (REQUESTED to COMPLETED) still passed all 69 tests. The mutation check caught it. Fix: the test now has its own hand-written list of legal pairs. Also: `pytest` could not import `app` until `pythonpath = .` was added to `pytest.ini`.
- **M1.2:** none. Note for later: a failed insert (lost race) still consumes an id from the sequence, so ids are not gapless (a race test skipped driver id 4).
- **M0.2:** `alembic revision --autogenerate` produced a migration that dropped ~40 PostGIS tables (`tiger.*`, `topology.*`, `spatial_ref_sys`). Cause: the `postgis/postgis` image creates them, so Alembic sees them as tables missing from our models. Fix: `include_object` filter in `alembic/env.py`.

## Open issues and TODOs

- Drivers cannot edit a profile or vehicle, or resubmit a rejected profile. A rejected driver is stuck unless an admin approves them again.
- Approval does not gate anything yet. M2.3 must require `approved` status before a driver can go online.
- argon2 hashing is synchronous and blocks the event loop (about 100 ms per login or register). Revisit during the M7.2 load test (for example `run_in_executor`).
- Register has a check-then-insert race: two simultaneous requests with the same email can both pass the check, and the second hits the unique constraint and returns 500 instead of 409. Rare; revisit if it ever matters.
- One driver can still be assigned to two rides, and one rider can still create two rides, by simultaneous requests (the checks in `assign_driver` / `create_ride` are check-then-act). Intentional for now; the driver case is reproduced in M4.1.
- OTP is not checked on `/start` until M3.5.
- Decide whether to keep `POST /admin/rides/{id}/assign` after matching exists (M2.4).
- A driver who is cancelled out of a ride stays on `rides.driver_id` (the history keeps who was assigned), so that driver still gets 409, not 404, on further actions for that ride.
- Dev database contains leftover rides, users, and drivers from the M1.3 and M1.4 manual checks (curl walkthrough and browser click-through).
- JWT in `sessionStorage` is readable by any script on the page. Acceptable for a learning project because no third-party scripts are loaded (Leaflet is the only one planned, in M2.1).
- The admin assign page: remove it or keep it after M2.4 matching exists.
- The admin page cannot list rides, so assigning needs the ride id and driver id copied by hand from the other tabs.
- The pages poll, so a status change can take up to 3 s to appear. A failed poll leaves its error message up until the next button press. WebSockets replace polling in M3.
- The M1.4 browser click-through lives only as a throwaway script outside the repo (no automated frontend tests, by decision). Rerun it by hand with the 12-step list if the pages change a lot.
- Swagger Authorize has not been clicked through in a browser in M1.1 or M1.2 (no browser tool). `/docs` serves 200 and the OpenAPI schema declares `HTTPBearer` on `/auth/me` and on all six M1.2 routes and all nine M1.3 routes (`/docs` served 200 in M1.3).

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

# tests (M1.3): uses a separate database named <POSTGRES_DB>_test, created automatically
docker compose exec backend pytest

# rides (M1.3): admin assigns a driver by hand until matching exists (M2.4)
curl -X POST localhost:8000/rides -H "Authorization: Bearer $RIDER" -H 'Content-Type: application/json' \
  -d '{"pickup_lat":12.9716,"pickup_lng":77.5946,"pickup_address":"MG Road","dropoff_lat":12.9352,"dropoff_lng":77.6245,"dropoff_address":"Koramangala"}'
curl -X POST localhost:8000/admin/rides/1/assign -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' -d '{"driver_id":1}'
curl -X POST localhost:8000/rides/1/arrive   -H "Authorization: Bearer $DRIVER"    # then /start, /complete
curl -X POST localhost:8000/rides/1/cancel   -H "Authorization: Bearer $RIDER"
curl localhost:8000/rides/active -H "Authorization: Bearer $RIDER"
curl localhost:8000/rides/1/events -H "Authorization: Bearer $RIDER"

# frontend (M1.4): open each in its own tab; every tab keeps its own login
#   http://localhost:8000/admin/    (log in with an admin made by create_admin.py)
#   http://localhost:8000/driver/   (register, profile, vehicle, wait for approval)
#   http://localhost:8000/rider/    (register, request a ride; the admin assigns it by hand)

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

Automated tests exist for the ride state machine (M1.3). M1.1 and M1.2 were verified by hand with curl and psql. M1.4 was verified with curl for every call the pages make, and with a headless Chrome click-through of the full flow in three tabs.
