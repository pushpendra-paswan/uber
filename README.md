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

Open `/admin/`, `/driver/`, and `/rider/` in three tabs of one browser (each tab keeps its own login). Register a driver, add a profile and vehicle, and approve it in the admin tab. Register a rider, pick pickup and drop-off by searching (press Enter) or clicking the map, and request a ride. Once both points are set, the page shows the route and the estimated fare (base fare Rs 50, Rs 12 per km, Rs 2 per minute, Rs 80 minimum; the server works the fare out again when you request). The request is matched at once (see below). The driver tab then moves the ride through arrived, started, and completed.

### Try matching by hand (M2.4, offers since M3.3)

Matching happens inside the ride request: the server picks the nearest online, approved driver without an active ride or a pending offer within 3 km of the pickup (straight-line distance) and offers them the ride (see "Offer flow" below), or the ride ends as `NO_DRIVER_FOUND` at once when nobody qualifies.

1. In the admin tab, approve two drivers.
2. In two driver tabs (one account each), click the map at different distances from where the rider will pick up (for example 1 km and 2 km away) and press **Go online**.
3. In a rider tab, choose a pickup and drop-off and press **Request ride**. The rider sees "Looking for a driver", and the nearer driver's tab shows the offer panel within about a second. When that driver accepts, the rider page shows "A driver has been assigned".
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

By hand: `curl localhost:8000/drivers/me/offer -H "Authorization: Bearer $DRIVER"`, then `curl -X POST localhost:8000/offers/<id>/accept -H "Authorization: Bearer $DRIVER"` (or `/reject`). Look at the rows with `docker compose exec db psql -U uber -d uber -c 'SELECT id, ride_id, driver_id, status, pickup_distance_m, expires_at FROM ride_offers ORDER BY id DESC LIMIT 10;'`. Run the simulator with the rates above to see many offers answered at once. Known limit until M4: two simultaneous requests can still offer the same free driver two rides.

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
| server to client | `offer_closed` | `{offer_id, ride_id, reason}` | M3.3: sent to the offered driver when the offer ends. `reason` is `accepted`, `rejected`, `expired`, or `ride_cancelled` |
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
