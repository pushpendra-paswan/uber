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

Open `/admin/`, `/driver/`, and `/rider/` in three tabs of one browser (each tab keeps its own login). Register a driver, add a profile and vehicle, and approve it in the admin tab. Register a rider, pick pickup and drop-off by searching (press Enter) or clicking the map, and request a ride. Once both points are set, the page shows the route and the estimated fare (base fare Rs 50, Rs 12 per km, Rs 2 per minute, Rs 80 minimum; the server works the fare out again when you request). Then assign it to the driver in the admin tab (copy the ride id and driver id by hand). The driver tab then moves the ride through arrived, started, and completed.

## Run the tests

```bash
docker compose exec backend pytest
```

Tests use their own database (`<POSTGRES_DB>_test`, created automatically), so your dev data is never touched.

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
