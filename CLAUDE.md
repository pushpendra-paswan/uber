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
| Payments | Internal wallet ledger, then Stripe test mode (Checkout, plain `httpx`, no SDK) |
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
│   │   │   ├── offers.py      # accept / reject an offer
│   │   │   ├── payments.py    # wallet, top-ups, and the Stripe webhook (no prefix)
│   │   │   ├── places.py
│   │   │   ├── admin.py
│   │   │   └── websocket.py
│   │   ├── services/          # business logic
│   │   │   ├── auth.py
│   │   │   ├── drivers.py
│   │   │   ├── rides.py       # ride lifecycle and state machine
│   │   │   ├── matching.py    # finds drivers and creates offers
│   │   │   ├── offers.py      # accept, reject, expiry, and the offers sweeper
│   │   │   ├── pricing.py     # fare estimate, final fare, surge snapshot and multiplier
│   │   │   ├── payments.py    # charge_ride (the settlement hook), Stripe Checkout top-ups, credit_topup, the webhook (M5.3)
│   │   │   ├── wallet.py      # post_entry (the only writer of wallets and wallet_entries), wallet view, admin adjustments (M5.3)
│   │   │   ├── places.py      # Nominatim search/reverse proxy, map config
│   │   │   └── routing.py     # OSRM route: distance, duration, path as [lat, lng]
│   │   ├── repositories/      # all database and Redis access
│   │   │   ├── users.py       # includes lock(): FOR UPDATE on the user's row (M4.2)
│   │   │   ├── drivers.py     # includes driver locations in Redis GEO, get_online_positions() for the surge snapshot (M5.2), and try_lock() / lock(): FOR UPDATE on the driver's row (M4.2)
│   │   │   ├── events.py      # WebSocket events: Redis pub/sub publish and subscribe
│   │   │   ├── offers.py
│   │   │   ├── rides.py       # includes count_unmet_demand_by_zone() for surge (M5.2)
│   │   │   ├── payments.py    # payments rows, wallet top-ups, processed Stripe event ids (M5.3)
│   │   │   ├── wallet.py      # wallet row lock and balance, ledger entries (M5.3)
│   │   │   ├── places.py      # Redis cache and rate-limit slot for Nominatim
│   │   │   ├── pricing.py     # pricing rule lookup, and the surge snapshot in Redis (get_snapshot / save_snapshot, M5.2)
│   │   │   └── ratings.py
│   │   └── utils/             # small generic pure functions (geo.py: is_inside_bounds, geohash_encode for surge zones, money.py, ...)
│   └── tests/
├── osrm/
│   ├── prepare.sh             # run once: download the map, clip to the city, build OSRM data
│   └── data/                  # generated by prepare.sh, not in git
├── frontend/
│   ├── shared/                # api.js (fetch + token), ws.js (WebSocket client with reconnect), base.css
│   ├── rider/                 # index.html, rider.js, rider.css
│   ├── driver/                # index.html, driver.js, driver.css
│   └── admin/                 # index.html, admin.js, admin.css
└── simulator/
    ├── simulator.py           # fake drivers (M2.5)
    ├── stress.py              # fires simultaneous requests and checks eighteen database invariants; scenarios drivers, riders, fleet (M4.1), chaos (M4.3, with wallets since M5.3) and payments (M5.3)
    ├── invariants.sql         # the eighteen invariants I1 to I18, read-only SQL, run by stress.py or by hand
    ├── fake_stripe.py         # a local fake of the Stripe API the backend uses (M5.3), standard library only, NOT part of the app
    └── requirements.txt       # httpx only
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

**WebSockets:** the WebSocket router owns the socket registry (a module-level dict) and the Redis listener task. It may open its own short database session for authentication only, never a request-scoped one (`Depends(get_db)` would hold a Postgres connection for as long as the socket stays open). Services send events to users by calling `repositories/events.publish` directly; there is no service wrapper.

**Events:** events are small "something changed" nudges with no personal data. Commands go over REST and REST is the source of truth, so a missed event is harmless. Events are published AFTER the commit, never before, so a client that refreshes sees the new data. `events.publish` is best-effort (it logs a warning and returns 0 when Redis fails), so a failed publish never fails or undoes a request that already committed. Every ride status change made by arrive, start, complete, and cancel is published as `ride_updated` to both participants (the rider and the assigned driver), after the commit.

**Offers sweeper:** `services/offers.py` runs one background asyncio task per backend process (`sweep_forever`, started in the lifespan) that expires offers past their deadline. It runs outside any request, so it is the second approved place that opens its own database sessions (the first is WebSocket authentication): a new session per offer, taken from `database.async_session` at call time so tests can replace it.

**Lock order:** whenever more than one row is locked, lock the RIDE row first, then the OFFER row (accept, reject, expiry, and cancel all do). This prevents deadlocks between them. The first read of an offer before the lock selects only columns, because SQLAlchemy's identity map would otherwise return a stale copy of the entity for the locked read.

### Concurrency rules

Three places read "is this free?" and then write: `create_ride` (the rider has no active ride), `matching.offer_to_next_driver` (the driver is available), and `offers.accept` (the driver has no active ride). Each one holds a Postgres row lock (`FOR UPDATE`) from before the deciding check until after the commit. Any new code that reads availability or an active-ride count and then writes must follow the same rules:

- **Lock, then check as a separate statement, then write, then commit.** The lock is released by the commit or the rollback; there is nothing to release by hand. Under READ COMMITTED every statement takes a fresh snapshot, so only a check that starts after the lock sees what the previous lock holder committed. A check that was already running when the lock was granted (an earlier prefilter, or a WHERE clause inside the locking query) can be stale. The prefilter in matching is kept as a cheap way to avoid useless locks, and the check after the lock decides.
- **The lock query is a column-only select** (`select(Driver.id)...with_for_update()`), never an entity load, so the identity map cannot return an older copy.
- **Never hold a lock across an OSRM or Nominatim call.** `create_ride` locks the rider's row after `estimate_ride`, and keeps the unlocked first check as the cheap early exit.
- **Lock order.** Blocking locks are taken only as: ride row, offer row, driver row (accept); or the rider's user row and nothing else that blocks (create_ride). Every lock taken while another is held in matching is non-blocking (`SKIP LOCKED`), so no cycle can form.
- **Offers skip busy drivers, riders and accepts wait.** If a candidate's row is locked, matching moves on to the next candidate: someone else is deciding about that driver right now, and waiting would line up the whole city behind one driver. `users.lock` and `drivers.lock` wait at most `database.LOCK_WAIT_MS` (3000 ms, `SET LOCAL lock_timeout`) and the service answers 503 `Busy, please retry` (it catches only SQLSTATE 55P03, nothing broader).
- **Tests widen the race window with `widen()`** in `tests/conftest.py` (a repository function sleeps after computing its result), never with a sleep in production code. A new lock needs a test that fails without it.

**Safety net (M4.3).** The locks prevent wasted work; three partial unique indexes make the bad states impossible even if a lock is ever missed: `uq_ride_offers_one_pending_per_driver` (one PENDING offer per driver), `uq_rides_one_active_per_driver` (DRIVER_ASSIGNED, DRIVER_ARRIVED, IN_PROGRESS), `uq_rides_one_active_per_rider` (REQUESTED and those three). They are declared in `models.py` with `postgresql_where`, written to match the stored enum values (upper-case names). The pending-offer index counts an offer past its deadline that the sweeper has not handled yet (`now()` cannot be in an index condition), so `get_available_ids` excludes a driver with ANY pending offer.

- **`IntegrityError` is caught narrowly, only at the three places that can hit an index**, and never becomes a 500: `create_ride` wraps the ride creation through the commit (rollback, 409 "You already have an active ride"); `matching.offer_to_next_driver` wraps the offer insert in a savepoint (`async with db.begin_nested():`), catches the error outside it, logs WARNING `offer skipped for driver <id>: <error text>` and moves to the next candidate; `accept` wraps everything from the first change through the commit (rollback, 409). The warning means a lock failed; with the locks working it never fires. Any new code that reads availability or an active-ride count and then writes gets the lock AND handles the index it can hit.
- **The sweeper withdraws offers of vanished drivers.** `expire_due_offers` reads all PENDING offers (`offers_repo.list_pending(db, limit)`), does ONE `drivers.get_online_ids` call (never with an empty list), and closes an offer when its deadline has passed (reason `expired`) or else when its driver has no presence key (reason `driver_offline`: offline, rejected by an admin, or the page died; time wins when both apply). Each offer is handled in a new session that locks the ride, then the offer, and re-checks that it is still pending and still due. A failing offer is logged once per offer id and the pass goes on; outage errors (`RedisError`, `OSError`, `InterfaceError`, `OperationalError`) abort the pass.
- **`offer_closed` reasons:** accepted, rejected, expired, ride_cancelled, driver_offline. Nothing may assume a fixed list of three.
- **Invariants I1 to I18** (`simulator/invariants.sql`, checked by every `simulator/stress.py` scenario): I1 one live pending offer per driver, I2 one active ride per driver, I3 one active ride per rider, I4 no REQUESTED ride without a PENDING offer, I5 no PENDING offer more than 10 s overdue, I6 no PENDING offer on a ride that is not REQUESTED, I7 every assigned, arrived, or in-progress ride has an ACCEPTED offer for its own driver, and since M5.1 I8 every COMPLETED ride has its fare and a `trip` breakdown, I9 every CANCELLED ride has its fee and a `cancellation` breakdown, I10 no unsettled ride has a fare, I11 no trip fare is above 150 percent of the estimate (rides marked `legacy` are left out of I8 and I9), and since M5.2 I12 a settled trip used the surge multiplier locked on its ride and its surge arithmetic adds up (trips whose breakdown has no `surge_percent` key are left out), and since M5.3 I13 a wallet's balance equals the sum of its entries and the `balance_after` of its latest entry, I14 every entry's `balance_after` is the running sum, I15 no wallet is negative, I16 every settled ride with a fare has exactly one succeeded payment of that amount and method (none without a fare), I17 every wallet payment has exactly one `RIDE_CHARGE` entry of minus its amount and nothing else has one, I18 a SUCCEEDED top-up has exactly one `TOPUP` entry of its amount and no other top-up has any. None can be legitimately violated even for an instant, so any sighting counts. The `chaos` scenario (random riders and drivers, riders paying from funded wallets or in cash, an admin replaying credits, a settle period, STUCK = a REQUESTED ride or PENDING offer left after it) must end `CHAOS CLEAN`; the `payments` scenario (repeated top-ups, replayed and badly signed webhooks) must end `PAYMENTS CLEAN`; every scenario is expected to exit 0.
- **Tests that need the forbidden state** (two pending offers for one driver, two active rides for one rider) cannot insert it any more: reach the code another way, or use the `locks_disabled` fixture to prove what the indexes alone do.

### Money rules (M5.1)

- **All fare logic lives in `services/pricing.py`:** `calculate_fare`, `record_trip_point`, `settle_completed_ride`, `cancellation_fee`, `settle_cancelled_ride`. `services/rides.py` and `services/drivers.py` call it; there is no fare arithmetic anywhere else.
- **Amounts are integer paise.** The one float is the distance accumulator in Redis, rounded to an integer before it is billed.
- **`rides.final_fare` is what the rider owes, set exactly once.** COMPLETED: the trip fare. CANCELLED: the cancellation fee, which may be 0. NO_DRIVER_FOUND and every active status: NULL. It is stored, not charged (payments are M5.3). `fare_estimate` stays the upfront estimate.
- **Settlement is in the same transaction, under the same ride lock, as the status change** (`driver_set_status` for COMPLETED, `cancel` for CANCELLED), so a settled ride always has its fare and two requests cannot settle it twice. A missing pricing rule gives 503 and the ride keeps its status, so the driver can retry. Completing a trip keeps working when Redis is down.
- **The breakdown (`rides.fare_breakdown`, JSONB) stores amounts, not rates**, so a later rule change leaves old rides alone. Kinds: `trip`, `cancellation`, and `legacy` (a data migration marks rides settled before M5.1; invariants and receipts skip them).
- **Actual distance comes from the driver's pings while the ride is IN_PROGRESS** (there is no location history). Each ping is added to the Redis hash `ride:{ride_id}:trip` (`distance_m`, `lat`, `lng`, `ts`, `pings`, `jumps`; TTL 24 h, never deleted): pings less than `MIN_PING_GAP_S` apart are ignored, a segment faster than `MAX_PLAUSIBLE_SPEED_MS` counts as a jump and adds no distance. Recording never fails a ping (a `RedisError` is logged and swallowed).
- **The estimated distance is billed when tracking is missing or unreliable** (`no_tracking`, `unreliable_tracking`), because it is the route the rider agreed to. The fare is capped at `FARE_CAP_PERCENT` of the estimate; there is no lower bound.
- **The cancellation quote and the real cancel call the same function** (`pricing.cancellation_fee`), so they cannot disagree. A driver never causes a fee. **Cancellation fees are never surged.**


### Ledger and payment rules (M5.3)

- **The wallet is a ledger.** `wallet_entries` is append-only (signed integer paise, each row with `balance_after`); `wallets.balance` always equals the sum of the user's entries. **`post_entry` in `services/wallet.py` is the ONLY function that writes `wallets` or `wallet_entries`.** It locks the wallet row, adds the entry, updates the balance, and never commits: the caller commits it together with its cause, in one transaction. Wallet rows are created lazily by the first entry (`INSERT ... ON CONFLICT DO NOTHING`, then a column-only `SELECT ... FOR UPDATE`); reading never creates one.
- **Lock order:** ride row, then wallet row (settlement); top-up row, then wallet row (credit). **The wallet row is a leaf lock:** nothing else is locked while it is held, and `post_entry` is the last database write before the commit in every flow, so it cannot be part of a cycle. Lock first, check as a separate statement after, then write. Never hold a lock across a Stripe call.
- **Paying for rides.** `rides.payment_method` (`cash` or `wallet`) is chosen at request time and never changed; a wallet ride needs `balance >= pricing.fare_cap(fare_estimate)` (402 otherwise), so the charge at settlement can always be covered. The reserved amount is derived (the cap of the rider's one active wallet ride), never stored. **`pricing.fare_cap` is the one definition of the cap** (settlement, estimate, reservation, request check).
- **The settlement charge** (`payments.charge_ride`, called by `driver_set_status` for COMPLETED and by `cancel` for CANCELLED, right before the commit): when `final_fare > 0` it creates one `payments` row (`idempotency_key = "ride:{id}:charge"`, unique ride) and, for a wallet ride, one `RIDE_CHARGE` entry. A failure rolls the whole settlement back. It never touches Redis. Cash is assumed collected by the driver.
- **Idempotency.** Completing or cancelling twice is refused by the state machine (409), so those endpoints take no key. Top-ups and admin adjustments take an `Idempotency-Key` header (8 to 64 characters of `A-Za-z0-9_-`, required): unique (user, key), the same key returns the same row, another body is 409. The call to Stripe carries `Idempotency-Key: topup-{our top-up id}`, and the top-up row is committed BEFORE that call. Webhooks: the Stripe event id is inserted first (`ON CONFLICT DO NOTHING`) in the SAME transaction as the credit; crediting goes through ONE function, `payments.credit_topup`, which locks the top-up row, re-checks its status, and trusts only our own row (session id, amount, currency), never the metadata. Partial unique indexes on the ledger are the safety net (`uq_wallet_entries_one_charge_per_ride`, `uq_wallet_entries_one_credit_per_topup`, `payments.ride_id`).
- **Webhooks and Stripe.** The `Stripe-Signature` is verified by hand (`hmac`, `hashlib`) over the RAW body, with a 5 minute tolerance; events that do not match are answered 200 `ignored`, never 5xx. **No `stripe` package:** Stripe is called with `httpx` through the one function `payments.call_stripe`. **Only test keys** (`sk_test_`, `rk_test_`) are used; anything else is "not configured". **Never log** a key, a secret, the signature header, an idempotency key, or the raw body.

### Surge rules (M5.2)

- **Multipliers are integer percents** (100 is no surge, 150 is 1.5x), never floats. The only floats are the pricing rule's `surge_cap` and the Redis GEO positions.
- **A zone is the geohash of the PICKUP at `ZONE_PRECISION` 5** (`utils/geo.geohash_encode`, in-house). Demand = DISTINCT riders with a REQUESTED or NO_DRIVER_FOUND ride created in the last `DEMAND_WINDOW_S` (180 s) in the zone; supply = AVAILABLE drivers (matching's own definition, `get_available_ids`) whose position is in the zone. Pressure = `demand * 100 // max(supply, 1)`; the multiplier comes from the `SURGE_STEPS` table, and below `MIN_DEMAND_FOR_SURGE` (3) there is never surge.
- **One city-wide snapshot** (`pricing.get_surge_snapshot`) is cached in Redis (`surge:snapshot`, `SURGE_CACHE_TTL_S` 15 s) and stores the UNCAPPED step values. `get_surge_percent` applies the cap from the pricing rule (`max(100, int(surge_cap * 100 + 0.5))`) at lookup time. A TTL of 0 turns the cache off. Surge code uses `datetime.now(timezone.utc)`, never the `time` module.
- **Redis failure means no surge:** a quote whose snapshot cannot be read or written logs one warning and uses 100. Postgres errors are not caught.
- **Surge multiplies the whole normal fare after the minimum fare** (`(normal_fare * surge_percent + 50) // 100`, half up). The multiplier is **locked on the ride at request time** (`rides.surge_percent`, with `pickup_zone`; a check constraint keeps it between 100 and 200) and `settle_completed_ride` reuses it, never the current one; the 150 percent cap works on the surged estimate.
- **The accepted-multiplier rule:** `RideCreate.accepted_surge_percent` (optional, 100 to 200). If present and the current multiplier is higher, `POST /rides` answers 409 "Prices have increased..." and creates nothing; if equal or lower the ride is created at the CURRENT multiplier; if absent the current one is accepted. The check is in `create_ride` after the early active-ride 409 and after `estimate_ride`, and before any lock: **all surge reads happen before any lock is taken.** A rider's own request is not part of their own price.

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
- `finish_offer()` in `services/offers.py`: closes an offer that was rejected or ran out of time, then offers the ride to the next driver or ends it. Used by reject and by expiry, so it is the one private helper in that file.
- `users.lock()`, `drivers.try_lock()`, and `drivers.lock()` in the repositories: the row locks of the concurrency rules above. Their callers in the services wrap only `users.lock` and `drivers.lock` in a `try/except` for the lock timeout.
- `notify_ride_updated()` in `services/rides.py`: after the commit, publishes `ride_updated` to the ride's rider and its assigned driver. Used by `driver_set_status` and `cancel`, so it is the one private helper in that file.
- `get_current_user` and role-check dependencies in `security.py`, and `user_from_token` (the one place a JWT becomes a user, shared by HTTP and WebSocket auth).
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

`REQUESTED` means offers are being tried: the ride is offered to one driver at a time (table `ride_offers`). `DRIVER_ASSIGNED` happens only when a driver accepts an offer. `NO_DRIVER_FOUND` happens when nobody qualifies or the offers run out.

**Trip code:** `change_ride_status()` sets `ride.otp` (the fake code `1234`, the constant `FAKE_OTP` in `services/rides.py`) when a ride becomes `DRIVER_ASSIGNED`, and clears it when the ride becomes `IN_PROGRESS` or `CANCELLED`. Starting a trip needs the code, and the state is checked before the code. Only the ride's rider can read it (`GET /rides/{id}/otp`); it never appears in `RideResponse`, ride events, or WebSocket events.

### Frontend conventions

- Plain HTML, CSS, and JavaScript. Use `<script type="module">`. No libraries except Leaflet.
- One JS file and one CSS file per app. The only shared JS module is `shared/api.js`.
- One `state` object per page and one `render()` function that updates the DOM from it.
- Call `fetch` through `shared/api.js` (it adds the JWT header). `shared/ws.js` owns the connection (reconnect, backoff, heartbeat, watchdog): pages only pass `onEvent` and `onStatus`, never create a `WebSocket` themselves, and re-fetch their state over REST after a reconnected open. The allowed functions inside `ws.js` are `connect`, `disconnect`, `reconnectNow`, `openSocket`, `sendPing`, and `connectionLost`.
- On WebSocket reconnect, re-fetch the current ride state from the REST API.
- The rider and driver pages open one WebSocket when a session starts. Socket handlers only update `state` and call `render()`, or react to an event by calling the page's existing REST refresh (events say "something changed", REST says what). Polling stays as the safety net. The driver marker is moved by `requestAnimationFrame` outside `render()`.
- Allowed functions besides `act()`: `tickCountdown` in `driver.js` (the offer countdown), `applyDriverLocation`, `animateDriver`, and `fetchEstimate` (the estimate call shared by the point selection and a refused request, M5.2) in `rider.js`.
- Keep HTML semantic and CSS simple. No CSS frameworks.

## Commands

```bash
osrm/prepare.sh                           # once, before the first start: build the routing data for the city
docker compose up --build                 # start everything (api, postgres, redis, osrm)
docker compose exec backend alembic upgrade head
docker compose exec backend alembic revision --autogenerate -m "message"
docker compose exec backend pytest
python simulator/simulator.py --drivers 50 --admin-email ... --admin-password ...   # on the host, in a venv with simulator/requirements.txt
python simulator/stress.py --scenario drivers --admin-email ... --admin-password ...   # M4.1/M4.2: simultaneous requests + invariant checks; exit 0 = no violation (expected for every scenario, chaos included: --scenario chaos), exit 1 = an invariant broke or a ride is stuck; --api-url takes two comma-separated URLs; --cleanup-only clears it
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
- **M4.1 Reproduce the bug:** stress tool that fires simultaneous requests and checks four database invariants
- **M4.2 Fix and compare:** Redis lock (`SET NX PX`) vs Postgres `FOR UPDATE SKIP LOCKED`, and keep the better one
- **M4.3 Edge cases:** partial unique indexes as a safety net behind the locks (IntegrityError becomes a 409 or a skipped candidate), the sweeper withdraws the offers of vanished drivers and isolates failing offers, cancel/expiry/reject/accept races, duplicate accepts and accept-after-timeout covered by tests, and a chaos stress scenario that finds no stuck ride and no invariant violation

Done when: a stress test shows zero double assignments and no stuck rides.

### Phase 5: Pricing and Payments
- **M5.1 Final fare:** final fare from tracked distance and actual time with a cap, and cancellation fees
- **M5.2 Surge pricing:** geohash zones (precision 5, in-house encoder), a step-table multiplier from unmet demand (distinct riders, 180 s) against available drivers, shown in the estimate, locked on the ride at request time and reused at settlement, capped by the pricing rule's `surge_cap`; `accepted_surge_percent` so a rider is never charged more than they saw; an admin snapshot endpoint
- **M5.3 Payments:** a rider wallet as an append-only ledger (`post_entry`, balance equals the sum of the entries), wallet or cash per ride (a wallet ride needs the fare cap in the wallet), Stripe test-mode Checkout top-ups (hosted page, `httpx`, no SDK, test keys only) with our idempotency key plus a Stripe key derived from our row, a signed webhook deduplicated by event id in the credit's own transaction, one `credit_topup` shared by the webhook and a sync endpoint, admin adjustments, invariants I13 to I18, a local fake Stripe, and a `payments` stress scenario
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
