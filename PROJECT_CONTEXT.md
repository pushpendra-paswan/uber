# PROJECT_CONTEXT.md

Living status file for the Uber-like ride-hailing learning project. Claude Code reads this at the start of every session and updates it after every milestone. Milestone definitions live in `CLAUDE.md`.

## Project summary

Uber-like ride-hailing web app for learning. FastAPI + PostgreSQL/PostGIS + Redis backend with layered structure (routers, services, repositories), plain HTML/CSS/JS frontend (rider, driver, admin), simulated drivers, no ML layer.

## Current status

- Current phase: 2 (Maps, Geo, Fare Estimate, Matching) is in progress. Phases 0 and 1 are complete
- Last completed milestone: M2.2 Routing and estimate (2026-10-08)
- Next milestone: M2.3 Driver presence
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
| M2.1 | Map UI | Done | 2026-10-08 |
| M2.2 | Routing and estimate | Done | 2026-10-08 |
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

- **Services (Docker Compose):** `db` (postgis/postgis:16-3.4, volume `postgres_data`, port 5432), `redis` (redis:7-alpine, port 6379), `osrm` (`ghcr.io/project-osrm/osrm-backend:v26.10.0-debian`, `osrm-routed --algorithm mld /data/city.osrm`, data mounted read-only from `./osrm/data`, published on `127.0.0.1:5000` for debugging only, no healthcheck), `backend` (FastAPI, port 8000, uvicorn --reload). Backend waits for db and redis to be healthy. It does not wait for osrm: with OSRM down the API starts and routing answers 502. The `osrm` container exits with an error until `osrm/prepare.sh` has been run
- **Tables** (migrations `0001_initial_schema`, `0002_unique_vehicle_per_driver`, and the data-only `0003_seed_economy_pricing_rule`; ids are integer PKs, all `created_at` are timestamptz default `now()`, money is integer paise):
  - `users`: role (rider/driver/admin), name, email (unique), phone (unique, nullable), password_hash
  - `drivers`: user_id (FK users, unique), license_number, verification_status (pending/approved/rejected, default pending)
  - `vehicles`: driver_id (FK drivers, unique `uq_vehicles_driver_id`: one vehicle per driver), plate_number (unique, stored normalized: uppercase, no spaces or hyphens), model, color, vehicle_type (default economy)
  - `rides`: rider_id (FK users), driver_id (FK drivers, nullable), pickup/dropoff lat, lng, address, status (default REQUESTED), distance_m, duration_s, fare_estimate, final_fare, otp, started_at, completed_at. Indexed: status, rider_id, driver_id
  - `ride_events`: ride_id (FK, indexed), from_status (nullable), to_status, actor_user_id (FK users, nullable)
  - `payments`: ride_id (FK, indexed), amount, method (cash/wallet/card), status (pending/succeeded/failed/refunded), idempotency_key (unique), gateway_ref (unique, nullable)
  - `ratings`: ride_id, from_user_id, to_user_id (FKs), score (check 1 to 5), comment. Unique (ride_id, from_user_id)
  - `pricing_rules`: vehicle_type (unique), base_fare, per_km, per_min, min_fare, surge_cap (float, default 2.0). One row seeded by migration 0003 (all money in paise): `economy`, base_fare 5000 (Rs 50), per_km 1200 (Rs 12), per_min 200 (Rs 2), min_fare 8000 (Rs 80), surge_cap 2.0. The test database gets the same row after every truncate
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
  - `POST /rides/estimate`: role rider (401 without a token, 403 for driver and admin); JSON `{pickup_lat, pickup_lng, dropoff_lat, dropoff_lng}`; 200 `EstimateResponse` (`distance_m, duration_s, fare_estimate, base_fare, distance_fare, time_fare, minimum_fare_applied, path`; money in paise; `path` is a list of `[lat, lng]` pairs). 422 for out-of-range or identical points, a pickup or drop-off outside the city box (`Pickup is outside the service area`, `Drop-off is outside the service area`), a route under 200 m (`Pickup and drop-off are too close for a ride`), or no route (`No route found between these points. Choose points closer to a road.`, for OSRM `NoRoute` and `NoSegment`); 502 `Routing is unavailable` when OSRM is down, times out, or answers something unexpected; 503 `Pricing is not configured` when there is no rule for `economy`. Declared before the `/rides/{ride_id}` routes. Nothing is stored
  - `POST /rides`: role rider; JSON `{pickup_lat, pickup_lng, pickup_address, dropoff_lat, dropoff_lng, dropoff_address}` (any other field the client sends is ignored); 201 `RideResponse` with status `REQUESTED`; 409 if the rider already has an active ride (checked first, so no routing call is made); the same 422, 502, and 503 errors as the estimate; 422 for out-of-range coordinates, blank address, or identical pickup and drop-off. The server always works out the estimate itself and stores `distance_m`, `duration_s`, and `fare_estimate` on the ride (the path is not stored)
  - `GET /rides/active`: role rider or driver; their active ride, or 404 `No active ride`; 403 for admin. Declared before `/rides/{ride_id}`
  - `GET /rides/{ride_id}` and `GET /rides/{ride_id}/events`: any logged-in user who is the ride's rider, its assigned driver, or an admin. Everyone else, and unknown ids, get the same 404 `Ride not found`
  - `POST /rides/{ride_id}/arrive`, `/start`, `/complete`: role driver (403 otherwise), must be the assigned driver (404 otherwise); 200 `RideResponse`; 409 `Cannot change ride from X to Y` if the transition is not allowed. `/start` sets `started_at` and does not check an OTP yet; `/complete` sets `completed_at` and leaves `final_fare` null
  - `POST /rides/{ride_id}/cancel`: role rider or driver; the rider from REQUESTED / DRIVER_ASSIGNED / DRIVER_ARRIVED, the assigned driver from DRIVER_ASSIGNED / DRIVER_ARRIVED; 409 after IN_PROGRESS or when already ended. Cancelling ends the ride
  - `POST /admin/rides/{ride_id}/assign`: role admin; JSON `{driver_id}`; 200 `RideResponse` with `DRIVER_ASSIGNED`; 404 unknown driver; 409 if the driver is not approved, already has an active ride, or the ride is not REQUESTED. Temporary stand-in for matching (M2.4)
  - `GET /places/map-config`: any logged-in user; 200 `MapConfigResponse` (`city_name, center_lat, center_lng, zoom, south, west, north, east`) built from the `CITY_*` and `MAP_ZOOM` settings
  - `GET /places/search?q=`: any logged-in user; `q` 3 to 200 chars (422 otherwise, also when it is shorter than 3 after trimming and collapsing spaces); 200 list of `PlaceResponse` (`display_name, lat, lng`, floats), up to 5, limited to the city box. 429 `Place search is busy, try again in a second` with `Retry-After: 1` when another uncached Nominatim call ran in the last 1.1 s; 502 `Place search is unavailable` when Nominatim fails or answers non-200
  - `GET /places/reverse?lat=&lng=`: any logged-in user; 200 `PlaceResponse` with the requested coordinates and Nominatim's address; 422 outside the city box (checked with `utils/geo.py`) (or lat/lng out of range); 404 `No address found for this location`; 429 and 502 as for search
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
- **Fare and routing (M2.2):** `services/routing.py` `get_route()` calls `{OSRM_URL}/route/v1/driving/{lng},{lat};{lng},{lat}` with `overview=full&geometries=geojson&steps=false&radiuses=300;300` (timeout 5 s, a new `httpx.AsyncClient` per call), reads the body whatever the HTTP status, and returns `{distance_m, duration_s, path}` with the path converted to `[lat, lng]`. Observed against the real OSRM: `NoSegment` and `NoRoute` both come back as HTTP 400 with the code in the body; `Ok` is 200. `services/pricing.py` `calculate_fare()` loads the rule and uses integer paise only: `distance_fare = (per_km * distance_m + 500) // 1000`, `time_fare = (per_min * duration_s + 30) // 60`, `fare = max(base + distance_fare + time_fare, min_fare)`; no surge, no rounding to whole rupees. `services/rides.py` has `MIN_TRIP_DISTANCE_M = 200`, `estimate_ride()` (bounds check with `utils/geo.is_inside_bounds`, route, minimum distance, fare) and `create_ride()` (409 check, then `estimate_ride()`, then insert). The seed rule gives Rs 169.16 for the MG Road to Koramangala test route (7794 m, 769 s). The service layer calls `routing.get_route(...)` through the module, so tests replace it
- **OSRM data:** `osrm/prepare.sh` (run once, safe to run twice) reads `CITY_*` and `OSM_EXTRACT_URL` from `.env` with grep and cut, downloads the regional extract to `osrm/data/region.osm.pbf` if missing (HEAD check first; stops on a non-200), clips it with osmium in a throwaway `debian:12-slim` container to the city box plus 0.05 degrees (`--strategy=complete_ways`) into `city.osm.pbf`, then runs `osrm-extract -p /opt/car.lua`, `osrm-partition`, `osrm-customize` (MLD) with the pinned image as the host user. It stops the `osrm` container before rewriting the files. For Bengaluru and the southern-zone extract: download 558 MB, clipped file 40 MB, 856 MB in `osrm/data/`, about a minute after the download. `osrm/data/` is in `.gitignore`
- **Tests:** `docker compose exec backend pytest` (95 tests, about 16 s). They run on a separate database `<POSTGRES_DB>_test` (created if missing; tables dropped and created from the models at session start, every table truncated with `RESTART IDENTITY CASCADE` before each test). `conftest.py` refuses to import unless the database name ends with `_test`, uses `NullPool` so simultaneous requests get separate connections, and overrides `get_db`. After each TRUNCATE it inserts the economy pricing rule (same values as the seed), and an autouse fixture replaces `routing.get_route` with a fake (5000 m, 900 s, a two-point path), so no test needs OSRM. Fixtures: `client`, `db`, `rider`, `driver` (approved, with vehicle), `admin`, and `make_user(role, approved=True)` for more users. Users get a token directly from `create_access_token` (no login). `test_rides.py` covers: all 49 status pairs against a hand-written list of legal pairs, happy path with events, cancel rules, one active ride per rider, 404/403 access rules, assign rules, two simultaneous cancels, `/rides/active`, ride body validation. `test_pricing.py` covers: the fare formula against hand-worked values (including the exact-minimum boundary), half-up rounding with a custom rule, the estimate endpoint and its breakdown, outside-the-city, identical, too-short, and no-route errors, a missing rule (503), a ride storing the server's numbers and ignoring the client's, 409 before any routing, and 401/403 for non-riders. Mutation-checked: `max` to `min` and the rounding offsets 500 and 30 to 0 each fail the pricing tests. Ride coordinates in the tests are derived from the `CITY_*` settings. `pytest.ini`: `asyncio_mode = auto`, session-scoped event loop, `pythonpath = .`
- **JWT:** HS256, signed with `JWT_SECRET`. Claims: `sub` (user id as string), `role`, `exp` (`ACCESS_TOKEN_EXPIRE_MINUTES`, default 60). `get_current_user` returns 401 + `WWW-Authenticate: Bearer` for a missing, malformed, tampered, or expired token, or a user that no longer exists. `require_role("rider", ...)` returns 403 for other roles.
- **Admin creation (script only):** `docker compose exec backend python create_admin.py --email ... --name ... --password ...` (prints the id; does nothing if the email exists)
- **Static files:** `frontend/` is mounted at `/` with `html=True` after the API routes, so `/rider/` serves `rider/index.html` (`/rider` redirects to `/rider/`)
- **Backend files:** `app/config.py` (settings from env), `app/database.py` (`Base` with constraint naming convention, async engine, `async_session`, `get_db` dependency, Redis client), `app/models.py` (all models), `app/main.py`; `app/schemas.py`, `app/security.py` (argon2 hashing, JWT, `get_current_user`, `require_role`), `app/routers/auth.py`, `app/routers/drivers.py`, `app/routers/admin.py`, `app/services/auth.py`, `app/services/drivers.py`, `app/routers/rides.py`, `app/services/rides.py`, `app/repositories/users.py`, `app/repositories/drivers.py`, `app/repositories/rides.py`, `app/routers/places.py`, `app/services/places.py`, `app/repositories/places.py`, `app/services/routing.py`, `app/services/pricing.py`, `app/repositories/pricing.py`, `app/utils/geo.py` (`is_inside_bounds`, used by `services/places.py` and `services/rides.py`), `backend/create_admin.py`, `backend/pytest.ini`, `backend/tests/conftest.py`, `backend/tests/test_rides.py`, `backend/tests/test_pricing.py`; `alembic.ini` and `alembic/` (async template; `env.py` takes the URL from settings); `osrm/prepare.sh`. `schemas.py` has `EstimateRequest` (range checks and the "must differ" rule), `EstimateResponse`, and `RideCreate`, which inherits from `EstimateRequest` and adds the two addresses
- **Settings (`.env`, all required, read in `config.py`):** `NOMINATIM_URL`, `NOMINATIM_USER_AGENT`, `OSRM_URL` (`http://osrm:5000`), `CITY_NAME`, `CITY_CENTER_LAT`, `CITY_CENTER_LNG`, `MAP_ZOOM`, `CITY_SOUTH`, `CITY_WEST`, `CITY_NORTH`, `CITY_EAST` (floats except `MAP_ZOOM`). `OSM_EXTRACT_URL` is also in `.env` but is read only by `osrm/prepare.sh` (the backend ignores unknown variables). Compose reads `.env` only when a container is created, so run `docker compose up -d` after editing it
- **Redis keys:** `places:search:<normalized query>` (JSON list of places, TTL 24 h, empty list cached too), `places:reverse:<lat 4 decimals>:<lng 4 decimals>` (JSON `{display_name}`, TTL 24 h), `places:slot` (`SET NX PX 1100`, the 1-request-per-second limiter). Failures (429, 502, 404 no address) are never cached
- **WebSocket messages:** none yet
- **Frontend pages** (plain HTML/CSS/ES modules, no libraries except Leaflet on the rider page; each app is one page with its login form inside; all poll every 3 s):
  - `/rider/` (`rider.js`): log in or register (role rider, phone sent only if filled). Logged in as another role: "This account is a X. Open /X/ instead." Rider: after login it fetches `/places/map-config` once and shows a Leaflet 1.9.4 map (OSM tiles, attribution, zoom 10 to 19, locked to the city box). With no ride, the rider picks Pickup and Drop-off by typing a place and pressing Enter or Search (results are buttons; one request per search, none while typing, under 3 characters shows a message with no request), or by clicking the map (the "Next map click sets" radio picks which point; it switches to the empty one automatically). A click outside the city shows a message; a click calls `/places/reverse` and falls back to `lat, lng` (5 decimals) if there is no address. Markers are green (Pickup) and red (Drop-off) circle markers with fixed-text tooltips. As soon as both points are set, and again whenever either changes (never from polling or `render()`), it calls `POST /rides/estimate`: the old estimate and route line are cleared at once, "Estimating route..." shows, a request counter makes sure only the latest answer is applied, and the map fits the route once per answer (`fitBounds`, padding 40, not animated). An estimate panel shows distance and time ("7.8 km, 13 min"), the estimated fare, the base, distance, and time parts, and "Minimum fare applies" when it does; money uses `Intl.NumberFormat("en-IN", INR)` on paise / 100. A 422 or 502 shows the backend message and keeps Request ride disabled. The route is a `L.polyline` (weight 5, behind the markers) created, updated, and removed in `render()`. Request ride is enabled only when both points and a current estimate exist, and calls `POST /rides` with those points. With a ride, the form is hidden, the map is view-only with the ride's two markers and fits to them once per ride id (polling never moves the map). Active ride shows id, status text, addresses, distance and time, estimated fare (a dash when the ride has no stored values), driver id once assigned, and the events timeline; the ride's route is asked for once per ride (`POST /rides/estimate` with the ride's coordinates, `routeRideId` set before the call so a failure shows once and is not retried every poll) and drawn, also after a reload and on the finished ride; Cancel (with `confirm()`) in REQUESTED / DRIVER_ASSIGNED / DRIVER_ARRIVED. A finished ride stays on screen (fetched once with `GET /rides/{id}` after `/rides/active` turns 404) with its markers and a "Request a new ride" button, which clears the selection, inputs, results lists, markers, estimate, and route line
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
| M1.4 | Ride coordinates were typed by hand (prefilled placeholders). Replaced by the map and place search in M2.1 | A map or geocoder | Map and Nominatim arrived in M2.1 |
| M1.4 | Admin table is only redrawn when its data changed | Redrawing on every poll | A redraw between mouse down and mouse up on Approve would swallow the click |
| M1.4 | Thrown API errors carry `.status` | Matching on message text | Pages need to tell a 404 (no ride, no profile) from a real failure |
| M1.4 | No backend change: `main.py` already mounted `frontend/` with `html=True` | Editing the mount | Nothing to fix |
| M2.1 | Backend proxy for Nominatim: the browser never calls it; the backend sends our User-Agent, caches, and rate-limits | Browser calling Nominatim directly | The public server requires an identifying User-Agent, caching, and 1 request per second, none of which can be enforced from many browsers |
| M2.1 | Search runs only on Enter or the Search button (one request per search) | Search-as-you-type, autocomplete | The public Nominatim usage policy forbids autocomplete |
| M2.1 | 1 request per second limiter is a Redis `SET places:slot 1 NX PX 1100`; a busy slot returns 429 with `Retry-After: 1` | Queueing or sleeping until the slot frees | Simple, no request is held open, and the rider just presses Search again. 1100 ms leaves a margin over 1 s. Only uncached calls take the slot |
| M2.1 | 24-hour cache for search (normalized: trimmed, lowercased, spaces collapsed) and reverse (key = coordinates rounded to 4 decimals, about 11 m). Empty search results are cached; failures and "no address" are not | Shorter or no TTL, caching failures | Addresses rarely change; repeats cost nothing and do not use the limiter. A failure cached for a day would hide a recovered server |
| M2.1 | Reverse returns the clicked coordinates, not Nominatim's snapped ones, and only the address text is cached | Returning (and caching) the snapped point | The marker must sit where the rider clicked; two clicks 1 m apart share one cache entry but each keep their own coordinates |
| M2.1 | Search is `bounded=1` to the city viewbox; reverse rejects points outside the box with 422 before any call | Unbounded world-wide search | One city only; saves calls and keeps results usable |
| M2.1 | City, center, zoom, and bounding box live in `.env` and are served to the frontend through `/places/map-config` | Constants duplicated in JS and Python | One source of truth; changing city is an `.env` edit |
| M2.1 | `call_nominatim(path, params)` is the one allowed private helper: search and reverse both need the slot check, the request, and the error mapping. It holds the service's only try/except (network failures are legitimate) and a new `httpx.AsyncClient` per call | Inlining it twice; a shared client | Used in two places, calls no other helper. A per-call client is fine at 1 request per second |
| M2.1 | Markers are `L.circleMarker` with fixed-string tooltips ("Pickup", "Drop-off"); no address or API data is ever passed to Leaflet | Default pin markers with popups showing the address | Leaflet renders tooltip and popup content as HTML, and addresses come from OpenStreetMap and from users. Default pins also need image files from the CDN |
| M2.1 | Leaflet 1.9.4 from unpkg with SRI hashes (`integrity` + `crossorigin`), script before the module script | Unpinned CDN, a vendored copy, npm | Pinned and verified, with no build step. The leafletjs.com quick-start page now documents 2.0 alpha and no longer lists the 1.9.4 hashes, so the hashes come from Leaflet's docs at the "Update docs release 1.9.4" commit (`7c0f675`) and equal what we computed from the files on unpkg and jsdelivr |
| M2.1 | The map is created the first time its section is visible, then `invalidateSize()` once; markers are created, moved, and removed in `render()` by comparing against the wanted points; fit-to-ride happens once per ride id | Creating the map at page load, fitting on every poll | Leaflet cannot size a hidden container. Polling must never move the map or the rider's view |
| M2.1 | Search results are rebuilt only when their state object changes (identity check); search inputs and the radio are written by event handlers, never by `render()` | Rebuilding results on every poll | Same lesson as the M1.4 admin table: a redraw between mouse down and mouse up swallows the click, and polling must not wipe typing |
| M2.1 | The ride request sends the address stored with the chosen point (cut to 255 characters), not whatever is typed in the input afterwards | Sending the input text | The input may hold a half-typed new search; Nominatim `display_name` can exceed the 255-character limit on `POST /rides` |
| M2.1 | No automated tests for the places code or the page | Mocked Nominatim tests | Per the working agreement (tests are for state machine, matching, concurrency, payments). Verified by hand with curl and a Playwright click-through kept outside the repo |
| M2.2 | The backend proxies OSRM; the browser never calls it | Browser calling OSRM directly | One place for errors, timeouts, and the fare; the OSRM port is only published on localhost for debugging |
| M2.2 | OSRM wants longitude first and sends GeoJSON as `[lng, lat]`: `services/routing.py` handles both, once, and the API returns `path` as `[lat, lng]` pairs | Converting in the frontend or in several places | Leaflet and the rest of the app use latitude first; a swapped pair places a route in the wrong country without any error. Verified: the first path point is `[12.97, 77.59]` |
| M2.2 | The server recomputes the estimate at ride creation and never trusts the client; `RideCreate` has no estimate fields and extras are ignored; the estimate is stored on the ride (`distance_m`, `duration_s`, `fare_estimate`) | Trusting the preview the page showed | A client could send any fare. The stored estimate is also what the ride shows later, whatever the pricing rule becomes |
| M2.2 | `create_ride` checks for an active ride (409) before calling `estimate_ride` | Estimating first | A rider who already has a ride gets 409 with no OSRM call, also when OSRM is down. `create_ride` calling `estimate_ride` is a use case calling a use case, not a helper chain |
| M2.2 | Fare arithmetic is integer paise with half-up rounding (`(rate * quantity + half) // divisor`), `fare = max(subtotal, min_fare)`, and no rounding of the total to whole rupees | Floats, or rounding the total up to a rupee | No float drift; the breakdown always adds up to the total exactly (checked by hand in verification and in tests) |
| M2.2 | The economy pricing rule is seeded by a hand-written data migration (`0003`, `op.bulk_insert`, in paise; downgrade deletes the row) | Seeding in app startup or in a script | The rule is part of the database history, so every fresh `alembic upgrade head` has it; tests re-insert it after each truncate |
| M2.2 | Snap radius 300 m (`radiuses=300;300`) and minimum trip distance 200 m | OSRM's default snapping (snaps to any distance), no minimum | A point in a lake or park is rejected instead of silently routed from the nearest road far away; a trip of a few metres is not a ride |
| M2.2 | `NoRoute` and `NoSegment` both become 422 "No route found...", everything else unexpected 502; the body is read whatever the HTTP status | Trusting the status code | Observed on the real OSRM: both are HTTP 400 with the code in the JSON body |
| M2.2 | The OSRM extract is the region clipped to the city box plus 0.05 degrees (osmium `--strategy=complete_ways`) | Clipping exactly to the box, or running the whole region | A route between two points near the edge may use roads just outside the box; the clipped data builds in about a minute and is far smaller |
| M2.2 | OSRM with the MLD algorithm and the car profile; image pinned to `ghcr.io/project-osrm/osrm-backend:v26.10.0-debian` (same tag in `prepare.sh` and compose) | `latest`, CH, the plain tag | MLD was the requested algorithm and prepared the city in about a minute. Since 26.4.1 the registry only has distro-suffixed tags (the plain `v26.10.0` does not exist), so the newest stable release is the `-debian` multi-arch tag; pulled and run on x86_64 |
| M2.2 | One-time `osrm/prepare.sh`; it reads `.env` with grep and cut, never `source`; the `osrm` service is not in the backend's `depends_on` and has no healthcheck | A Dockerfile that builds the data, a healthcheck, making the backend wait | `NOMINATIM_USER_AGENT` has spaces and parentheses and would break `source`. The image has no curl. The API must start when OSRM is down |
| M2.2 | The route path is not stored on the ride; the rider page asks for it once per ride | Storing a polyline column, caching in Redis | Smallest change that works; M3 live tracking may want a cache (open issue) |
| M2.2 | `is_inside_bounds` in `utils/geo.py` (pure, no settings), used by `services/places.py` and `services/rides.py` | Two inline checks | Used in two places, so it qualifies as a utility |
| M2.2 | `routing.get_route` is called through the module (`from app.services import routing, pricing`) | `from ... import get_route` | Tests replace one attribute and every caller sees the fake |
| M2.2 | Every view change from code in the rider page is not animated (`animate: false`) | Leaflet's default animation | Leaflet silently ignores a view change that arrives during a zoom animation, so an animated result-click zoom swallowed the route fit (see the bugs log) |
| M0.1 | Pinned: fastapi 0.142.4, uvicorn 0.54.0, sqlalchemy 2.1.4, asyncpg 0.32.0, redis 8.1.0, pydantic-settings 2.15.0 (M0.2 added alembic 1.20.0) | Unpinned | Current stable at 2026-10-08 |

## Bugs hit

One entry per notable bug: milestone, symptom, root cause, fix.

- **M2.2:** (1) After picking two places the map ended up zoomed in at street level with the route running off the screen, instead of fitted. Cause: clicking a search result calls `setView(place, 16)`, which starts an animated zoom; the estimate answer arrives about 50 ms later and `fitBounds` runs during that animation, and Leaflet's `_tryAnimatedZoom` silently returns without doing anything while a zoom animation is running. Found by the browser click-through (route bounding box larger than the map, confirmed with a screenshot). Fix: all view changes made from code use `animate: false`. (2) `docker pull ...:v26.10.0` failed with "not found" although GitHub has that release: since 26.4.1 the registry only publishes distro-suffixed tags, so the image is `v26.10.0-debian`. (3) `OSM_EXTRACT_URL` in `.env` did not break the backend, so no `config.py` change was needed (settings ignore unknown environment variables). (4) Two click-through script mistakes, not app bugs: the mock for `/places/**` also caught `/places/map-config`, and Playwright waits forever on Leaflet's disabled zoom-out button at minimum zoom (also seen in M2.1).
- **M2.1:** no bug in the app code. Two things worth knowing: (1) the leafletjs.com quick-start page no longer shows Leaflet 1.9.4 (it documents 2.0 alpha), so the integrity hashes could not be copied from it; they were checked against Leaflet's docs at the 1.9.4 release commit instead (see the decisions log). (2) The click-through script first failed on its own checks: Leaflet snaps the map center to whole pixels (about 1e-5 degrees off) and a third zoom-out click hits the disabled button at min zoom 10. Both were test mistakes, not app bugs.
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
- Dev database contains leftover rides, users, and drivers from the M1.3, M1.4, and M2.1 manual checks (curl walkthrough and browser click-throughs; M2.1 left an admin `m21admin@example.com`, several `m21*` riders and drivers, and a few rides; M2.2 left an admin `m22admin@example.com`, riders and drivers `m22*` and `m22ui-*`, and a few rides, one of them still REQUESTED, plus a second `pricing_rules` id after the downgrade/upgrade check). Dev Redis holds cached places for a day.
- JWT in `sessionStorage` is readable by any script on the page. Acceptable for a learning project: Leaflet is the only third-party script, loaded from unpkg at a pinned version with an SRI hash (M2.1).
- The admin assign page: remove it or keep it after M2.4 matching exists.
- The admin page cannot list rides, so assigning needs the ride id and driver id copied by hand from the other tabs.
- The pages poll, so a status change can take up to 3 s to appear. A failed poll leaves its error message up until the next button press. WebSockets replace polling in M3.
- The M1.4 browser click-through lives only as a throwaway script outside the repo (no automated frontend tests, by decision). Rerun it by hand with the 12-step list if the pages change a lot.
- The public OSM tile and Nominatim servers are for light use only. The M2.5 simulator and the M7.2 load test must never call them: use OSRM and fixed coordinates.
- Resolved in M2.2: `POST /rides` and `POST /rides/estimate` now reject points outside the city box (`is_inside_bounds`).
- Resolved in M2.2: `CITY_*` and the OSRM extract match, because `osrm/prepare.sh` builds the extract from the `CITY_*` values (Bengaluru placeholders, southern-zone extract). Still open: set `NOMINATIM_USER_AGENT` to your own contact email (the default text still says "replace with your contact email").
- OSRM durations are free-flow driving times with no live traffic, so durations and the time part of the fare are optimistic.
- Surge is not applied until M5.2. The final fare in M5.1 should reuse `pricing.calculate_fare` and the same pricing rule.
- The route path is not stored on the ride: the rider page re-requests it once per ride. M3 live tracking may need to cache it.
- OSRM data must be regenerated when the city changes: edit `CITY_*` and `OSM_EXTRACT_URL` in `.env`, delete `osrm/data/`, run `osrm/prepare.sh`. The prepared data is not refreshed when OpenStreetMap changes.
- `routing.py` makes a new `httpx.AsyncClient` per call, like `places.py`. Fine locally; share one client if this grows. Every point change in the rider page costs one OSRM call and one database read, with no rate limit.
- The admin and driver pages do not show fares or distances yet. Rides created before M2.2 have null estimate fields; the rider page shows dashes for them.
- Not exercised against the real OSRM: the 5 s timeout and the "any other code gives 502" branch (only OSRM being stopped, `NoSegment`, and `NoRoute` were). The rider page fit/animation behavior was checked in headless Chrome only.
- `places` makes a new `httpx.AsyncClient` per uncached call. Fine at 1 request per second; share one client if this grows.
- The "no address" 404 path was not seen against the real Nominatim: Bengaluru has no open water, and a lake centre still resolved to a nearby building. It is verified against a local stub that returns `{"error": ...}`.
- The busy (429) message was seen in the browser from the real limiter once, right after a reverse call; it depends on timing, so a faster or slower network can miss the 1.1 s window.
- The page has no automated frontend tests. The M2.1 and M2.2 Playwright scripts live outside the repo (scratchpad); the M2.2 one mocks Nominatim and the map tiles but uses the real OSRM, backend, and database.
- Swagger Authorize has not been clicked through in a browser in M1.1 or M1.2 (no browser tool). `/docs` serves 200 and the OpenAPI schema declares `HTTPBearer` on `/auth/me` and on all six M1.2 routes and all nine M1.3 routes (`/docs` served 200 in M1.3).

## How to run and test

```bash
cp .env.example .env                  # first time only; set CITY_* and OSM_EXTRACT_URL for your city
osrm/prepare.sh                       # first time only (M2.2): downloads a regional extract (558 MB for the default), clips it to the city, builds the OSRM data in osrm/data/
docker compose up --build             # start db, redis, osrm, backend
curl localhost:8000/health            # expect 200 {"status":"ok","postgres":"ok","redis":"ok"}
docker compose stop redis             # /health now returns 503 with redis "error"
docker compose start redis
docker compose down                   # stop
docker compose down -v                # stop and delete the database volume (full reset)

# database migrations (run after every fresh database)
docker compose exec backend alembic upgrade head                              # create/update all tables and seed the economy pricing rule
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

# routing and fare (M2.2): needs osrm/prepare.sh to have run and `docker compose up -d osrm`
curl -X POST localhost:8000/rides/estimate -H "Authorization: Bearer $RIDER" -H 'Content-Type: application/json' \
  -d '{"pickup_lat":12.9716,"pickup_lng":77.5946,"dropoff_lat":12.9352,"dropoff_lng":77.6245}'   # 7794 m, 769 s, 16916 paise
curl "http://127.0.0.1:5000/route/v1/driving/77.5946,12.9716;77.6245,12.9352?overview=false"      # OSRM directly (longitude first)
docker compose exec db psql -U uber -d uber -c 'SELECT * FROM pricing_rules;'
docker compose stop osrm              # estimates now answer 502 "Routing is unavailable"; a rider with an active ride still gets 409 on POST /rides

# places (M2.1): needs NOMINATIM_USER_AGENT with your contact email and the CITY_* values in .env, then `docker compose up -d`
# Never script or loop these: the public Nominatim server allows 1 request per second and light use only
curl localhost:8000/places/map-config -H "Authorization: Bearer $RIDER"
curl "localhost:8000/places/search?q=mg%20road" -H "Authorization: Bearer $RIDER"
curl "localhost:8000/places/reverse?lat=12.9757&lng=77.6063" -H "Authorization: Bearer $RIDER"
docker compose exec redis redis-cli keys 'places:*'

# frontend (M1.4, map added in M2.1): open each in its own tab; every tab keeps its own login
#   http://localhost:8000/admin/    (log in with an admin made by create_admin.py)
#   http://localhost:8000/driver/   (register, profile, vehicle, wait for approval)
#   http://localhost:8000/rider/    (register, pick two points on the map or by search, see the route and fare, request a ride; the admin assigns it by hand)

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

Automated tests exist for the ride state machine (M1.3). M1.1 and M1.2 were verified by hand with curl and psql. M1.4 was verified with curl for every call the pages make, and with a headless Chrome click-through of the full flow in three tabs. M2.2 was verified with curl and psql (OSRM directly, the seed migration up/down/up and an empty autogenerate, the estimate endpoint with hand-checked arithmetic and path order, every 4xx/5xx path, OSRM stopped, ride creation with a fake fare in the body), a pytest mutation check on the fare logic, and a headless Chrome click-through of the rider page with the admin and driver tabs (route and panel, quick changes with a held-back stale answer, too close, outside the city, OSRM down and back, request, reload mid-ride, full trip to COMPLETED, a ride with null estimate fields, phone width). M2.1 was verified with curl (auth, validation, caching, limiter, failure paths, about 9 real Nominatim calls in total) and a headless Chrome (Playwright) click-through of the rider page covering search, map clicks, markers, fit-to-ride, polling not moving the map or wiping typing, the full ride, the XSS address, and phone width.
