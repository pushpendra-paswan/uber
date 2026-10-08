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

### Try matching by hand (M2.4)

Matching happens inside the ride request: the server assigns the nearest online, approved driver without an active ride within 3 km of the pickup (straight-line distance), or the ride ends as `NO_DRIVER_FOUND`. The driver has no say yet.

1. In the admin tab, approve two drivers.
2. In two driver tabs (one account each), click the map at different distances from where the rider will pick up (for example 1 km and 2 km away) and press **Go online**.
3. In a rider tab, choose a pickup and drop-off and press **Request ride**. The ride shows "A driver has been assigned" with the nearer driver's id, and that driver's tab shows the ride within 3 seconds.
4. A second rider at the same pickup gets the other driver. A third rider sees "No drivers are available nearby right now" and can request again at once.
5. When a driver completes or a rider cancels, that driver can be matched again. A driver who stops pinging (closed tab) is not matched after 30 seconds.

### Run the driver simulator (M2.5)

The simulator starts N fake drivers so there is a fleet to match against. Each one registers (once), gets approved by an admin, goes online, drives around on real roads from the local OSRM, and pings its position every 3 seconds. When matching assigns a ride to a fake driver, it drives to the pickup, arrives, starts the trip, drives to the drop-off, and completes it. It runs on your machine (not in Docker), uses only the public API and the local OSRM, and never calls Nominatim or any OpenStreetMap server.

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
| `--seed` | none | Makes the starting points repeatable |

Drivers are `sim-driver-001@sim.example.com`, `sim-driver-002@...`, and so on, with the password `sim-driver-pass`. Running it again reuses the same accounts. A driver whose position is still in Redis (stopped less than 30 seconds ago) continues from there.

**Try matching by hand:** start `--drivers 30` with the default center, open `/rider/`, pick a pickup near the city center and a drop-off about 2 km away, and press **Request ride**. The ride is assigned to the nearest simulated driver within a couple of seconds, and the rider page walks through assigned, arrived, in progress, and completed on its own (with `--speed-kmh 90`, a 3.7 km trip took about 4 minutes). The rider page does not show the driver moving until M3.2. A pickup far outside the circle ends as `NO_DRIVER_FOUND`.

Every 15 seconds one summary line shows drivers running, drivers on a ride, pings ok and failed, the average ping time, and rides completed. **Ctrl+C** takes every driver offline and exits within a few seconds (a driver on a ride cannot go offline; its presence expires after 30 seconds).

**Do not log in to a simulated driver on the driver page while the simulator runs.** The driver page keeps pinging its own clicked position and fights the simulator over the same driver.

### Go online as a driver

Once the admin has approved the driver, the driver page shows a map. Click it to set where the driver is (clicks outside the city are refused), then press **Go online**. The page keeps the driver online by sending the position every 3 seconds; closing the page lets the driver drop off after 30 seconds. Click the map again to move the driver, and press **Go offline** when done (not possible during an active ride). Online state lives in Redis only. To look at it:

```bash
docker compose exec redis redis-cli GEOPOS drivers:geo <driver_id>     # longitude first, then latitude
docker compose exec redis redis-cli TTL driver:<driver_id>:presence    # about 30 right after an update, -2 once expired
docker compose exec redis redis-cli ZCARD drivers:geo
```

The driver id is shown at the top of the driver page. The GEO set can keep a stale member after the presence key expires; the presence key is what says whether a driver is online.

## WebSockets (M3.1)

The backend accepts WebSocket connections at `ws://localhost:8000/ws` and pushes events to logged-in users. Nothing in the app uses it yet: the pages and the simulator still poll. Driver locations (M3.2) and ride offers (M3.3) are sent on top of it.

**Flow:** connect, then send the token as the FIRST message (never in the URL). If it is valid, the server answers `auth_ok`. Every message, in both directions, is a JSON text frame `{"type": "<string>", "data": {...}}`.

| Direction | type | data | Meaning |
|---|---|---|---|
| client to server | `auth` | `{token}` | First message, within 5 seconds |
| client to server | `ping` | `{}` | Answered with `pong` |
| server to client | `auth_ok` | `{user_id, role}` | Authenticated |
| server to client | `pong` | `{}` | Reply to `ping` |
| server to client | `error` | `{detail}` | Unknown message type (the socket stays open) |
| server to client | anything else | anything | An event published for this user. `auth_ok`, `pong`, and `error` are reserved |

| Close code | Meaning |
|---|---|
| 4400 | Bad message after auth (not a text frame, not a JSON object, or no string `type`) |
| 4401 | Unauthorized (bad first message, or an invalid, expired, or unknown-user token) |
| 4408 | No auth message within 5 seconds |
| 4409 | Replaced by a newer connection (a user keeps at most 5 sockets; the oldest is closed) |

Frames larger than 64 KB are rejected by the server (`--ws-max-size 65536`, close code 1009). Events are lost if the user is not connected at that moment; the REST API stays the source of truth.

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

The reply is the number of backend processes listening (1 in dev). Backend code sends events with `repositories/events.publish(user_id, type, data)`.

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
