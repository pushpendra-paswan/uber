# Uber Clone

A ride-hailing web app built for learning: riders request trips, nearby drivers accept them, and both sides see live location, fare, and trip status. Three apps (rider, driver, admin) share one FastAPI backend with PostgreSQL/PostGIS and Redis. Frontend is plain HTML, CSS, and JavaScript.

See `CLAUDE.md` for the architecture and milestones, and `PROJECT_CONTEXT.md` for current status.

## Start

```bash
cp .env.example .env        # first time only; then set the city and OSM_EXTRACT_URL (see below)
osrm/prepare.sh             # first time only: builds the routing data (large download, see below)
docker compose up --build
docker compose exec backend alembic upgrade head    # create the tables and the pricing rule (first time, and after a reset)
```

- Rider app: http://localhost:8000/rider/
- Driver app: http://localhost:8000/driver/
- Admin app: http://localhost:8000/admin/ (log in with an admin created below)
- API: http://localhost:8000 (Swagger UI at `/docs`)
- Health check: http://localhost:8000/health
- Postgres: `localhost:5432`, Redis: `localhost:6379`, OSRM (debugging only): `127.0.0.1:5000`

## Set your contact email and city

Place search goes through the public Nominatim server, which requires an identifying User-Agent. In `.env`, replace the contact email in `NOMINATIM_USER_AGENT`. The city is also set in `.env`. The defaults are Bengaluru placeholders, so change them to your own city:

| Variable | Meaning |
|---|---|
| `NOMINATIM_URL` | Geocoding server (default: the public `https://nominatim.openstreetmap.org`) |
| `NOMINATIM_USER_AGENT` | Sent to Nominatim. Must identify this app and include your contact email |
| `CITY_NAME` | Shown in messages such as "No places found in Bengaluru" |
| `CITY_CENTER_LAT`, `CITY_CENTER_LNG`, `MAP_ZOOM` | Where the rider map opens |
| `CITY_SOUTH`, `CITY_WEST`, `CITY_NORTH`, `CITY_EAST` | Bounding box: the map cannot leave it, search is limited to it, and points outside it are rejected (by the map, by `/places/reverse`, and by `POST /rides` and `/rides/estimate`) |
| `OSRM_URL` | Where the backend finds OSRM (default `http://osrm:5000`, the compose service) |
| `OSM_EXTRACT_URL` | Used only by `osrm/prepare.sh`. A regional extract that contains the whole city (default: Geofabrik's southern India, which contains Bengaluru) |

The public Nominatim and OpenStreetMap tile servers are for light use only (1 request per second, no bulk scripts). After editing `.env`, run `docker compose up -d` so the backend picks up the change.

## Prepare the routing data (once)

Routing and fare estimates use OSRM on a map of your city. Set `CITY_*` and `OSM_EXTRACT_URL` in `.env` first, and make sure the extract contains the city. Then run, once:

```bash
osrm/prepare.sh
```

It downloads the regional extract to `osrm/data/region.osm.pbf` (a few hundred MB; about 560 MB for the default), clips it to the city box plus 0.05 degrees of padding with osmium, and builds the OSRM data (car profile, MLD) with the pinned OSRM image. It needs Docker, curl, and awk, and is safe to run twice (the download is skipped if the file exists). `osrm/data/` is not in git. If you change the city, delete `osrm/data/` and run it again.

The `osrm` service exits with an error until this has been run. That is expected: the backend does not depend on it, so the API still starts, and ride estimates answer 502 "Routing is unavailable" until OSRM is up.

## Create an admin

Admins cannot register through the API. Create one with the script (riders and drivers use `POST /auth/register`):

```bash
docker compose exec backend python create_admin.py --email admin@example.com --name "Admin" --password 'at-least-8-chars'
```

## Try a ride in the browser

Open `/admin/`, `/driver/`, and `/rider/` in three tabs of one browser (each tab keeps its own login). Register a driver, add a profile and vehicle, and approve it in the admin tab. Register a rider, pick pickup and drop-off by searching (press Enter) or clicking the map, and request a ride. Once both points are set, the page shows the route and the estimated fare (base fare Rs 50, Rs 12 per km, Rs 2 per minute, Rs 80 minimum; the server works the fare out again when you request). The request is matched at once (see below). The driver tab then moves the ride through arrived, started (with the rider's trip code), and completed (see "Trip flow" below).

### Try matching by hand (M2.4, offers since M3.3)

Matching happens inside the ride request: the server picks the nearest online, approved driver without an active ride or a pending offer within 3 km of the pickup (straight-line distance) and offers them the ride (see "Offer flow" below), or the ride ends as `NO_DRIVER_FOUND` at once when nobody qualifies.

1. In the admin tab, approve two drivers.
2. In two driver tabs (one account each), click the map at different distances from where the rider will pick up (for example 1 km and 2 km away) and press **Go online**.
3. In a rider tab, choose a pickup and drop-off and press **Request ride**. The rider sees "Looking for a driver", and the nearer driver's tab shows the offer panel within about a second. When that driver accepts, the rider page shows "Your driver is on the way" and the trip code.
4. A second rider at the same pickup is not offered the nearer driver while the first offer is open (a driver deciding on an offer is busy), so the farther driver gets that one. A third rider sees "No drivers are available nearby right now" and can request again at once.
5. When a driver completes or a rider cancels, that driver can be offered rides again. A driver who stops pinging (closed tab) is not offered rides after 30 seconds.

### Run the driver simulator (M2.5)

The simulator starts N fake drivers so there is a fleet to match against. Each one registers (once), gets approved by an admin, goes online, drives around on real roads from the local OSRM, and pings its position every 3 seconds. When a fake driver is offered a ride (see "Offer flow"), it accepts, rejects, or ignores the offer by the rates below; when it accepts, it drives to the pickup, arrives, starts the trip, drives to the drop-off, and completes it. It runs on your machine (not in Docker), uses only the public API and the local OSRM, and never calls Nominatim or any OpenStreetMap server.

Install (once; it needs only `httpx`, and an admin account made with `create_admin.py` above):

```bash
python -m venv .venv-sim
.venv-sim/bin/pip install -r simulator/requirements.txt      # Windows: .venv-sim\Scripts\pip
```

Run (the stack must be up and OSRM prepared):

```bash
.venv-sim/bin/python simulator/simulator.py --drivers 30 --admin-email admin@example.com --admin-password 'at-least-8-chars'
# or: export SIM_ADMIN_EMAIL=... SIM_ADMIN_PASSWORD=...  and leave the two flags out
```

| Flag | Default | Meaning |
|---|---|---|
| `--drivers` | 20 | Number of drivers, 1 to 200 (one process) |
| `--admin-email`, `--admin-password` | env `SIM_ADMIN_EMAIL`, `SIM_ADMIN_PASSWORD` | An existing admin, used to approve the drivers |
| `--api-url`, `--osrm-url` | `http://127.0.0.1:8000`, `http://127.0.0.1:5000` | Where the API and OSRM are published |
| `--center-lat`, `--center-lng` | the city center | Center of the fleet area (give both or neither) |
| `--radius-km` | 5 | Radius of the fleet area. Matching only looks 3 km around the pickup, so the fleet lives in a circle instead of the whole city |
| `--speed-kmh` | 30 | Driving speed (each driver gets a random factor between 0.8 and 1.2). Use 90 to finish a trip in a couple of minutes |
| `--seed` | none | Makes the starting points (and the answers to offers) repeatable |
| `--accept-rate` | 0.7 | Share of offers a driver accepts |
| `--reject-rate` | 0.15 | Share of offers a driver rejects. The rest (default 0.15) are ignored and run out after 15 seconds |
| `--otp` | 1234 | The trip code the drivers send when they start a trip (see "Trip flow"). A wrong one makes every driver wait at the pickup and log "wrong trip code" once per ride |
| `--response-delay-min`, `--response-delay-max` | 1, 6 | Seconds a driver waits before answering an offer, picked at random (always at least 2 seconds before the deadline). Answers are rounded up to the simulator's 3-second tick |

The simulator exits with a message if a rate is negative, the two rates add up to more than 1, or the minimum delay is above the maximum. `--accept-rate 0 --reject-rate 0` makes every driver ignore every offer, so a ride walks through up to 5 offers (about 75 seconds) and ends as `NO_DRIVER_FOUND`.

Drivers are `sim-driver-001@sim.example.com`, `sim-driver-002@...`, and so on, with the password `sim-driver-pass`. Running it again reuses the same accounts. A driver whose position is still in Redis (stopped less than 30 seconds ago) continues from there.

**Try matching by hand:** start `--drivers 30` with the default center, open `/rider/`, pick a pickup near the city center and a drop-off about 2 km away, and press **Request ride**. The nearest simulated driver is offered the ride and usually accepts within a few seconds (a reject or an ignored offer moves on to the next driver), and the rider page walks through assigned, arrived, in progress, and completed on its own (with `--speed-kmh 90`, a 3.7 km trip took about 4 minutes). The rider page shows the driver moving (see live tracking below). A pickup far outside the circle ends as `NO_DRIVER_FOUND`.

Every 15 seconds one summary line shows drivers running, drivers on a ride, pings ok and failed, the average ping time, and rides completed. **Ctrl+C** takes every driver offline and exits within a few seconds (a driver on a ride cannot go offline; its presence expires after 30 seconds).

**Do not log in to a simulated driver on the driver page while the simulator runs.** The driver page keeps pinging its own clicked position and fights the simulator over the same driver.

### Watch live tracking (M3.2)

While a ride is assigned, arrived, or in progress, the rider page shows a **Your driver** section (name, vehicle, "Live tracking: connected") and a blue **Driver** marker on the map that glides as the driver's location updates. The driver page shows the pickup (green) and drop-off (red) markers of its ride.

1. Start the simulator: `.venv-sim/bin/python simulator/simulator.py --drivers 20 --speed-kmh 30 ...` (30 km/h is slow enough to watch).
2. Open `/rider/`, log in, click a pickup near the city center and a drop-off about 2 km away, and press **Request ride**.
3. The marker appears and moves toward the pickup. The map fits pickup, drop-off, and driver once; after that it never moves by itself, so you can zoom and pan freely.
4. Open DevTools, Network, WS, click the `/ws` connection, and watch the Messages: a `driver_location` frame arrives about every 3 seconds (`{"type":"driver_location","data":{"ride_id":..,"lat":..,"lng":..,"updated_at":..}}`, the first frame you send is `auth`).
5. Without the simulator: log in as an approved driver on `/driver/`, click the map, press **Go online**, then request a ride near that point as a rider. The marker sits still and "Last location update" moves forward every 3 seconds. Clicking far away on the driver map makes the rider's marker jump there (moves over 500 m are not animated).

The marker lags the real position by up to about 3 seconds on purpose (it glides between updates). If the socket closes (for example the backend restarts), the page reconnects by itself and the marker keeps moving through polling meanwhile (see "Reconnect and outages" below). Check the details endpoint by hand with `curl localhost:8000/rides/<ride_id>/driver -H "Authorization: Bearer $RIDER"`.

### Go online as a driver

Once the admin has approved the driver, the driver page shows a map. Click it to set where the driver is (clicks outside the city are refused), then press **Go online**. The page keeps the driver online by sending the position every 3 seconds; closing the page lets the driver drop off after 30 seconds. Click the map again to move the driver, and press **Go offline** when done (not possible during an active ride). Online state lives in Redis only. To look at it:

```bash
docker compose exec redis redis-cli GEOPOS drivers:geo <driver_id>     # longitude first, then latitude
docker compose exec redis redis-cli TTL driver:<driver_id>:presence    # about 30 right after an update, -2 once expired
docker compose exec redis redis-cli ZCARD drivers:geo
```

The driver id is shown at the top of the driver page. The GEO set can keep a stale member after the presence key expires; the presence key is what says whether a driver is online.

### Offer flow (M3.3)

A ride is no longer assigned the moment it is requested. It is offered to ONE driver at a time, nearest first, and the driver has 15 seconds to answer.

```
POST /rides  ->  REQUESTED, offer to the nearest free driver
                     |-- driver accepts          -> DRIVER_ASSIGNED
                     |-- driver rejects          -> offer to the next nearest driver
                     |-- 15 s pass (no answer)   -> offer to the next nearest driver
                     '-- nobody left (or 5 offers made) -> NO_DRIVER_FOUND
```

- `REQUESTED` now means "offers are being tried"; the state machine itself did not change. A request with nobody in range still ends as `NO_DRIVER_FOUND` straight away.
- Offers are rows in the table `ride_offers` (status `PENDING`, `ACCEPTED`, `REJECTED`, `EXPIRED`, `CANCELLED`), so they survive a restart and are an audit trail. A driver is never offered the same ride twice, a ride has at most 5 offers, and a driver with a pending offer is not offered another ride.
- Commands are REST: `GET /drivers/me/offer` (the pending offer, 404 if none), `POST /offers/{id}/accept` (200 with the ride), `POST /offers/{id}/reject` (204). Accept refuses (409) after the 15 seconds even if the background sweeper has not marked the offer expired yet, and when the driver is offline or already on a ride (403 if not approved).
- The WebSocket only nudges: `offer_created {offer_id, ride_id}` and `offer_closed {offer_id, ride_id, reason}` go to the offered driver, and `ride_updated {ride_id, status}` goes to the rider when a driver accepts or the offers run out. The pages answer an event by calling their REST refresh, and they also poll every 3 seconds, so a missed event only costs a few seconds.
- A background task (one per backend process, polling the database every second) expires offers that ran out of time and moves the ride on.

**Try it with two driver tabs:** approve two drivers; log in to `/driver/` in two tabs (one account each); click the map about 0.5 km from where the rider will pick up in tab A and about 1.2 km away in tab B; press **Go online** in both. In a rider tab, pick two points and press **Request ride**.

1. Within about a second tab A shows a **New ride request** panel with the addresses, the distance to the pickup, the trip distance and time, the fare, "Respond within N seconds", a progress bar, and **Accept** and **Reject** buttons. The pickup (green) and drop-off (red) are drawn on the map.
2. Press **Reject** in tab A: the panel closes and tab B shows the offer. The rider still sees "Looking for a driver".
3. Leave tab B alone: when the countdown ends the panel closes with "The offer expired." and the rider page says "No drivers are available nearby right now" within about 2 seconds.
4. Request again and press **Accept**: tab A switches to the ride view and the rider sees the driver and live tracking.
5. Request again and cancel from the rider tab while the offer is showing: the panel closes with "The rider cancelled the request."

By hand: `curl localhost:8000/drivers/me/offer -H "Authorization: Bearer $DRIVER"`, then `curl -X POST localhost:8000/offers/<id>/accept -H "Authorization: Bearer $DRIVER"` (or `/reject`). Look at the rows with `docker compose exec db psql -U uber -d uber -c 'SELECT id, ride_id, driver_id, status, pickup_distance_m, expires_at FROM ride_offers ORDER BY id DESC LIMIT 10;'`. Run the simulator with the rates above to see many offers answered at once. Since M4.2 two simultaneous requests can no longer offer the same free driver two rides (driver and rider rows are locked, see the stress test below).

### Trip flow (M3.5)

From the accepted offer to the end of the trip:

```
rider requests -> REQUESTED (offer to a driver)
driver accepts -> DRIVER_ASSIGNED   the rider's page shows the trip code
driver arrives -> DRIVER_ARRIVED    POST /rides/{id}/arrive
driver types the code the rider tells them -> IN_PROGRESS   POST /rides/{id}/start  {"otp": "1234"}
driver completes -> COMPLETED       POST /rides/{id}/complete
rider or driver cancels, before the trip starts -> CANCELLED   POST /rides/{id}/cancel
```

- **The trip code is fake on purpose: it is always `1234`.** It is the constant `FAKE_OTP` at the top of `backend/app/services/rides.py`. A real system would make four random digits per ride with Python's `secrets` module. The flow is the real one: the code is stored on the ride (`rides.otp`) when a driver is assigned, only the ride's rider can read it (`GET /rides/{id}/otp`), and the driver must send it to start the trip. It is cleared when the trip starts or the ride is cancelled, and it is never in `RideResponse`, the ride events, or a WebSocket message.
- A wrong code gives `400 Incorrect trip code` and changes nothing. A start on a ride that is not `DRIVER_ARRIVED` gives `409`, with any code (the state is checked first). A code that is not exactly four digits gives `422`.
- Every status change made by arrive, start, complete, and cancel sends `ride_updated {ride_id, status}` to BOTH the rider and the assigned driver after the commit, so both pages update within a second. The pages still poll every 3 seconds as a safety net.
- Either side can cancel while the driver is on the way or waiting at the pickup (not after the trip starts). The other page shows who cancelled ("You cancelled this ride", "The driver cancelled this ride...", "The rider cancelled this ride").
- There are no location checks (arrive and complete work from anywhere), no limit on wrong codes, no cancellation fee, and a driver who cancels does not trigger a new search. Those come later or are listed in `PROJECT_CONTEXT.md`.
- The simulator sends the code itself. `--otp 0000` makes its drivers send a wrong one: each logs "wrong trip code for ride N" once, the ride stays `DRIVER_ARRIVED`, and the rider can cancel it.

**Try it with two tabs (no simulator):** log in as a driver and click the map near where the rider will be picked up, then **Go online**. As a rider, request a ride. The driver accepts: the rider page shows "Your driver is on the way" and a large code (1234). The driver presses **I have arrived**: the rider page changes within a second. The driver types `0000` and presses Enter: "Incorrect trip code", nothing changes, and the typed text stays. The driver types `1234` and presses Enter: both pages show "Trip in progress" and the rider's code disappears. **Complete trip** ends it.

By hand: `curl localhost:8000/rides/<id>/otp -H "Authorization: Bearer $RIDER"` (rider only), then `curl -X POST localhost:8000/rides/<id>/start -H "Authorization: Bearer $DRIVER" -H 'Content-Type: application/json' -d '{"otp":"1234"}'`.

## WebSockets (M3.1)

The backend accepts WebSocket connections at `ws://localhost:8000/ws` and pushes events to logged-in users. Since M3.2 the rider page opens one socket after login and receives `driver_location` events on it; since M3.3 the driver page opens one too (both through `frontend/shared/ws.js`) and gets the offer events. Everything else (ride status, the admin page, the simulator) still polls, and so do the rider and driver pages, as a safety net.

**Flow:** connect, then send the token as the FIRST message (never in the URL). If it is valid, the server answers `auth_ok`. Every message, in both directions, is a JSON text frame `{"type": "<string>", "data": {...}}`.

| Direction | type | data | Meaning |
|---|---|---|---|
| client to server | `auth` | `{token}` | First message, within 5 seconds |
| client to server | `ping` | `{}` | Answered with `pong` |
| server to client | `auth_ok` | `{user_id, role}` | Authenticated |
| server to client | `pong` | `{}` | Reply to `ping` |
| server to client | `error` | `{detail}` | Unknown message type (the socket stays open) |
| server to client | `driver_location` | `{ride_id, lat, lng, updated_at}` | M3.2: sent to the rider of a ride each time its driver pings (`updated_at` is epoch seconds). No other data about the driver |
| server to client | `offer_created` | `{offer_id, ride_id}` | M3.3: sent to the driver who was just offered a ride |
| server to client | `offer_closed` | `{offer_id, ride_id, reason}` | M3.3: sent to the offered driver when the offer ends. `reason` is `accepted`, `rejected`, `expired`, `ride_cancelled`, or (M4.3) `driver_offline`: the sweeper withdrew the offer because the driver went offline, was rejected by an admin, or stopped pinging |
| server to client | `ride_updated` | `{ride_id, status}` | M3.3: sent to the rider when a driver accepts (`DRIVER_ASSIGNED`) or the offers run out (`NO_DRIVER_FOUND`) |
| server to client | anything else | anything | An event published for this user. `auth_ok`, `pong`, and `error` are reserved |

| Close code | Meaning |
|---|---|
| 4400 | Bad message after auth (not a text frame, not a JSON object, or no string `type`) |
| 4401 | Unauthorized (bad first message, or an invalid, expired, or unknown-user token) |
| 4408 | No auth message within 5 seconds |
| 4409 | Replaced by a newer connection (a user keeps at most 5 sockets; the oldest is closed) |

Frames larger than 64 KB are rejected by the server (`--ws-max-size 65536`, close code 1009). Events are lost if the user is not connected at that moment; the REST API stays the source of truth.

### Reconnect and outages (M3.4)

`frontend/shared/ws.js` owns the connection. Pages pass `onEvent` and `onStatus` to `connect()` and never create a `WebSocket`. What it does when the connection ends:

| Situation | Action |
|---|---|
| Close code 4401 (unauthorized) | Clear the session and reload the page, like a REST 401. No retry |
| Close code 4409 (replaced by a newer connection) | Stop, status `closed`. No automatic retry (tabs would kick each other forever). Resumes when the page becomes visible again, the browser goes online, or you press **Reconnect** |
| Close code 4400 (bad message) | Stop, status `closed`. Only **Reconnect** resumes (this means a bug) |
| Any other code, a connection that fails to open, or a dead connection found by the watchdog | Retry with backoff |

- **Backoff:** before the n-th retry the wait is `cap / 2` plus a random part up to `cap / 2`, with `cap = min(30 s, 1 s * 2^n)`: 0.5 to 1 s, 1 to 2, 2 to 4, 4 to 8, 8 to 16, then 15 to 30 s. It starts over only after a connection stayed logged in for 10 seconds, so a server that accepts and drops you keeps being backed off from.
- **Dead connections:** after login the client sends `ping` every 20 seconds and expects any frame back within 8 seconds, so a frozen server or a lost network is noticed within 28 seconds at worst. A socket that does not open or does not answer `auth` within 10 seconds is abandoned too. The old socket is dropped at once, without waiting for the browser's close event (a half-dead connection can take minutes to report one).
- **Waking up:** when a tab becomes visible or the browser goes online, a waiting retry happens immediately, and an open socket is pinged to check it.
- **After a reconnect** the pages re-fetch their state over REST, because events published while the socket was down are lost. The rider page also polls the driver's location every 3 seconds while the socket is not open, so the marker keeps moving (without the smooth glide).
- The status line shows the state ("Live tracking: connection lost. Reconnecting (attempt 3), next try in about 4 s. ...") and a **Reconnect** button appears after 4409 or 4400.

Try the outage cases with a rider tab that has an active ride (start the simulator with `--drivers 20 --speed-kmh 30`):

```bash
docker compose restart backend      # status goes to Reconnecting, then back to connected within seconds
docker compose pause backend        # frozen server: the dead connection is noticed within 28 s; then
docker compose unpause backend      # it reconnects
docker compose stop backend         # long outage: the retry delays grow to 15 to 30 s
docker compose start backend        # it reconnects within one retry period
```

Six tabs of one login: the first shows "paused: this account is open in too many tabs" with a **Reconnect** button (its marker still moves every 3 seconds through polling). Close another tab and press **Reconnect**, and it shows "connected" again. Stopping only Redis (`docker compose stop redis`) does not disconnect anyone: the sockets stay open, but no events are delivered until Redis is back.

Try it with redis-cli and the browser console (log in on /rider/ first):

```js
// in the DevTools console; the token is in sessionStorage under "session"
token = JSON.parse(sessionStorage.getItem("session")).token
ws = new WebSocket("ws://localhost:8000/ws")
ws.onmessage = e => console.log(e.data)
ws.onclose = e => console.log("closed", e.code, e.reason)
ws.onopen = () => ws.send(JSON.stringify({type: "auth", data: {token}}))
// the console prints auth_ok with your user id and role
```

```bash
# send an event to user 5 from a terminal (the channel name ends with the Redis database index, 0 for dev)
docker compose exec redis redis-cli PUBLISH ws:events:0 '{"user_id": 5, "type": "test", "data": {"hello": "world"}}'
```

The reply is the number of backend processes listening (1 in dev). Backend code sends events with `repositories/events.publish(user_id, type, data)`. Since M3.3 it is best-effort: if Redis fails it logs one warning and returns 0, and the request that sent the event is not affected.

## Fares and cancellation fees (M5.1)

When a trip is completed the server works out the **final fare** and stores it on the ride (`final_fare`, with `actual_distance_m`, `actual_duration_s`, and a `fare_breakdown`). Nothing is charged yet (payments are M5.3). The formula is the one the estimate uses (all money in integer paise, half rounded up):

```
fare = max(base + per_km x distance + per_min x time, minimum fare)         # seed rule: 5000, 1200 per km, 200 per minute, 8000
final fare = min(fare, 150 percent of the estimate)
```

Worked example (a real simulator trip): 2977 m and 402 s give 5000 + (1200 x 2977 + 500) // 1000 + (200 x 402 + 30) // 60 = 5000 + 3572 + 1340 = **9912** paise (Rs 99.12), against an estimate of 9696 and a cap of 14544.

- **Time** is `completed_at - started_at` on the server (at least 1 s).
- **Distance is measured from the driver's location pings while the ride is in progress.** There is no location history, so each ping adds the straight-line distance from the previous one to a counter in Redis (`ride:<id>:trip`, kept 24 hours). A ping less than 1 s after the last one is ignored, and a move faster than 45 m/s (162 km/h) is treated as a jump and adds nothing. The first and last ping interval of a trip are lost, so a tracked trip is usually a little short.
- **When tracking is missing or unreliable the ESTIMATED distance is billed**, because it is the route the rider agreed to: no counter or fewer than 2 pings (`no_tracking`, also when Redis is down), or jumps seen and a tracked distance under 50 percent of the estimate (`unreliable_tracking`). Trips driven by hand on the driver page (clicking the map) usually bill the estimated distance; the simulator gives distance-based fares.
- **Cap:** the rider never pays more than 150 percent of the estimate. There is no lower limit, so a shorter trip costs less.
- If there is no pricing rule, completing answers 503 `Pricing is not configured`, the ride stays in progress, and the driver can retry.

**Cancellation fee** (the amount, 3000 paise = Rs 30, and the free window, 120 s, are columns of the pricing rule; the table is code):

| Who cancels | Ride status | Fee |
|---|---|---|
| driver | any allowed | 0 |
| rider | REQUESTED | 0 |
| rider | DRIVER_ASSIGNED, within the free window after the assignment | 0 |
| rider | DRIVER_ASSIGNED, later | 3000 |
| rider | DRIVER_ARRIVED | 3000 |

Cancelling a trip in progress is not possible. The rider can ask first: `GET /rides/{id}/cancellation-fee` answers `{"fee": 3000, "reason": "driver_arrived"}` and uses the same function as the cancel (the rider page shows it in the confirm box). The fee is stored as the ride's `final_fare`; a ride that ends as NO_DRIVER_FOUND keeps `final_fare` NULL.

```bash
docker compose exec -T db psql -U uber -d uber -c "SELECT id, status, final_fare, fare_breakdown FROM rides ORDER BY id DESC LIMIT 10"
docker compose exec redis redis-cli HGETALL ride:<id>:trip      # the distance counter while a trip is in progress
```

## Concurrency stress test (M4.1, M4.2, M4.3)

`simulator/stress.py` fires many requests at the same instant and then looks in the database for broken invariants. M4.1 used it to reproduce a real bug (the offer flow checked "is this driver free?" and wrote the offer in separate steps, with no lock in between); M4.2 fixed it with row locks in Postgres (the rider's `users` row in `POST /rides`, the driver's `drivers` row in matching and in accept; see `PROJECT_CONTEXT.md`). **Exit code 0 ("no violation observed") is the expected result for every scenario, including `chaos` (M4.3).** An exit code of 1 means a lock is missing or broken.

It runs on the host (same venv as the simulator, `httpx` only), changes state only through the public API, and reads the database with read-only SELECTs through `docker compose exec db psql`, so it needs `docker compose` and access to the `db` service from the repository root. It never calls Nominatim or any OpenStreetMap server. Accounts `stress-driver-NN@sim.example.com` and `stress-rider-NNN@sim.example.com` are created once and reused (nothing deletes them). An admin made with `create_admin.py` is needed.

```bash
export SIM_ADMIN_EMAIL=... SIM_ADMIN_PASSWORD=...        # or --admin-email / --admin-password

# I1 and I2: 20 riders request at once while 3 drivers are free, then every offer is accepted at once.
# STOP the simulator first: its drivers would receive offers and dilute the test.
.venv-sim/bin/python simulator/stress.py --scenario drivers --rounds 20

# I3: every rider sends the same request twice at once (also stop the simulator)
.venv-sim/bin/python simulator/stress.py --scenario riders --rounds 10

# against the running fleet (start it first: simulator.py --drivers 30 --speed-kmh 30 --seed 1); watches 30 s per round
.venv-sim/bin/python simulator/stress.py --scenario fleet --rounds 5 --riders 20

# chaos (M4.3): 30 riders and 10 drivers act at random for 60 s (requests, duplicate requests, cancels from both sides,
# accept, reject, ignore, go offline, accept twice at once), then up to 45 s to settle. Stop the simulator first.
.venv-sim/bin/python simulator/stress.py --scenario chaos --seed 1
.venv-sim/bin/python simulator/stress.py --scenario chaos --riders 60 --drivers 20 --chaos-seconds 120

# control run: one request after another, no race possible, exit code 0
.venv-sim/bin/python simulator/stress.py --scenario drivers --sequential --rounds 3 --riders 10

# leave the state of the last round to look at (psql, curl), then clear it
.venv-sim/bin/python simulator/stress.py --scenario drivers --rounds 1 --keep-last-round
.venv-sim/bin/python simulator/stress.py --cleanup-only   # cancels every active ride of the stress riders, takes the stress drivers offline
```

**Chaos flags (M4.3):** `--chaos-seconds` (60, 10 to 600: how long the agents act), `--settle-seconds` (45, 30 to 300: how long the system gets to finish after they stop), `--tolerate-5xx` (count 5xx responses and connection errors and report them, but they do not decide the exit code: use it when you restart the backend or stop Redis by hand during a run), `--otp` (1234, the trip code). The defaults of the scenario are `--riders 30` and `--drivers 10`; `--seed` fixes the agents' choices (each agent has its own `random.Random`), not the timing. After the settle period a ride still REQUESTED or an offer still PENDING is STUCK (printed with its offers); rides left assigned, arrived, or in progress are not. The run ends with `CHAOS CLEAN` or `CHAOS FOUND PROBLEMS: ...`, action counts (so you can see that cancels, double accepts, and drivers going offline really happened), final ride and offer status counts, and the first five 5xx bodies. While it runs, kill the backend or Redis yourself: `docker compose restart backend`, or `docker compose stop redis` and `docker compose start redis` ten seconds later, with `--tolerate-5xx`.

Other flags: `--riders` (2 to 100), `--drivers` (1 to 100), `--rounds` (1 to 50), `--repeat` (riders scenario), `--spread-m`, `--watch-seconds`, `--seed` (repeatable random points, not repeatable timing), `--api-url` (one URL or several separated by commas, see below), `--label` (free text printed in the summary header, so saved outputs identify themselves), `--osrm-url`, `--center-lat/--center-lng`, `--psql-user/--psql-db` (default from `.env`). Ctrl+C runs the cleanup, prints the summary, and exits with the usual code.

**Against two backend processes.** The locks live in Postgres, so they must also hold across processes. `docker-compose.yml` publishes a second port (`127.0.0.1:8001`). Start a second uvicorn inside the same container (it does not auto-reload, so stop it and start it again after every code change, and it inherits the container's environment), then give the stress tool both URLs. Requests of a burst are spread over the URLs one after the other, and in the riders scenario the repeats of one rider alternate between the processes:

```bash
docker compose exec -d backend sh -c 'uvicorn app.main:app --host 0.0.0.0 --port 8001 > /tmp/p2.log 2>&1'   # its log is in /tmp/p2.log, docker compose logs does not show it
.venv-sim/bin/python simulator/stress.py --api-url http://127.0.0.1:8000,http://127.0.0.1:8001 --scenario drivers --rounds 20
.venv-sim/bin/python simulator/stress.py --api-url http://127.0.0.1:8000,http://127.0.0.1:8001 --scenario riders --rounds 10
```

The container has no `pkill`; `docker compose up -d --force-recreate backend` stops the second process along with the container. Both processes run their own offer sweeper and WebSocket listener, which is safe (M3.3).

**The invariants** (`simulator/invariants.sql`, plain SQL you can also run by hand, one row per offender, an empty section means it holds):

| Name | Meaning |
|---|---|
| I1 `driver_pending_offers` | no driver has more than one live PENDING offer |
| I2 `driver_active_rides` | no driver has more than one ride in DRIVER_ASSIGNED, DRIVER_ARRIVED, or IN_PROGRESS (the real double assignment) |
| I3 `rider_active_rides` | no rider has more than one ride in REQUESTED, DRIVER_ASSIGNED, DRIVER_ARRIVED, or IN_PROGRESS |
| I4 `stuck_requested` | no REQUESTED ride without a PENDING offer (a ride nothing will move on) |
| I5 `overdue_pending_offers` | no PENDING offer more than 10 s past its deadline (the sweeper is dead or not keeping up) |
| I6 `orphan_pending_offers` | no PENDING offer on a ride that is not REQUESTED |
| I7 `assigned_without_accepted_offer` | every DRIVER_ASSIGNED, DRIVER_ARRIVED, or IN_PROGRESS ride has an ACCEPTED offer for its own driver |
| I8 `completed_without_fare` | every COMPLETED ride has `final_fare`, `actual_distance_m`, `actual_duration_s`, and a `trip` breakdown (rides settled before M5.1 carry `{"kind": "legacy"}` and are left out) |
| I9 `cancelled_without_settlement` | every CANCELLED ride has a `cancellation` breakdown and a `final_fare` (the fee, which may be 0) equal to the fee in it (legacy rides left out) |
| I10 `fare_on_unsettled_ride` | no ride that is neither COMPLETED nor CANCELLED (active, or NO_DRIVER_FOUND) has a fare, a billed distance or duration, or a breakdown |
| I11 `fare_over_cap` | no trip fare is above 150 percent of the ride's estimate (integer division, like the code) |

I4 to I11 cannot be legitimately violated even for an instant (each pair of changes, and each status change with its settlement, is one transaction), so any sighting at any snapshot counts. Since M4.3 the database also refuses the bad states of I1 to I3 itself (partial unique indexes `uq_ride_offers_one_pending_per_driver`, `uq_rides_one_active_per_driver`, `uq_rides_one_active_per_rider`); the locks stay, because they avoid wasted work.

```bash
docker compose exec -T db psql -U uber -d uber -At -F '|' < simulator/invariants.sql
```

**Lost matches.** The drivers scenario also prints `offers N/E` for every round, where E is min(riders, drivers) (all stress drivers are within range of all pickups). Fewer offers than that means a free driver was skipped, or a rider got NO_DRIVER_FOUND although a driver was free: a defect of its own, but not an invariant, so it does not change the exit code. The summary line `lost matches: <rounds with N below E>` and the total offers against the total expected show it. More offers than E is possible when other drivers are online (the tool warns "this round is diluted": stop the simulator and wait 30 s for its drivers' presence to expire).

**Exit codes:** 0 no violation observed, 1 at least one invariant broke (`RACE REPRODUCED: I1 in 4 of 5 rounds, ...`; in the chaos scenario also a stuck ride, or without `--tolerate-5xx` any 5xx or connection error), 2 the tool could not do its job (setup or login failed, an API URL is unreachable or unhealthy, `INVARIANTS NOT CHECKED: ...` when psql cannot run, the fleet scenario found no fleet or every ride ended NO_DRIVER_FOUND). A clean result is never printed when the check did not run. The snapshots are samples (after the burst, after the accepts, about every second in the fleet scenario), so a violation that disappears before the next snapshot would be missed.

Before the fix (M4.1) the default run (20 riders, 3 drivers) broke I1 and I2 in every round (up to 10 offers and 9 active rides on one driver). After it, every scenario above, including two processes and the chaos scenario, gives exit code 0; the numbers are in `PROJECT_CONTEXT.md` under "Baseline for M4.2" and "Fix and compare results".

## Run the tests

```bash
docker compose exec backend pytest
```

Tests use their own Postgres database (`<POSTGRES_DB>_test`, created automatically) and their own Redis database (index 1; dev uses 0, set by `REDIS_DB`), so your dev data is never touched.

## Stop

```bash
docker compose down
```

## Reset (deletes all database data)

```bash
docker compose down -v
docker compose up --build
docker compose exec backend alembic upgrade head
```
