"""Fake driver fleet (M2.5). Runs on the host and uses only the public API and the local OSRM.

Each driver registers (if needed), gets approved by the admin, goes online, wanders on real roads,
pings its position every 3 seconds, and drives any ride that matching gives it from start to finish.

Usage: python simulator/simulator.py --drivers 50 --admin-email ... --admin-password ...
"""
import argparse
import asyncio
import logging
import math
import os
import random
import signal
import sys
import time

try:
    import httpx
except ImportError:
    sys.exit("httpx is not installed. Run: pip install -r simulator/requirements.txt")

PING_INTERVAL_S = 3  # same as the heartbeat of the driver page (M2.3); presence expires after 30 s
ARRIVAL_WAIT_TICKS = 2
SETUP_CONCURRENCY = 5  # login hashing blocks the backend's event loop (M1.1)
MAX_DRIVERS = 200
SNAP_RADIUS_M = 300
MAX_TRIES = 10
SUMMARY_INTERVAL_S = 15
API_TIMEOUT_S = 30
OSRM_TIMEOUT_S = 5
DRIVER_PASSWORD = "sim-driver-pass"
EMAIL_DOMAIN = "sim.example.com"
METERS_PER_DEGREE = 111_320
RIDE_ACTION_LOGS = {"arrive": "arrived at pickup", "start": "trip started", "complete": "trip completed"}

# Shared by all driver tasks (one thread, so plain counters are safe).
stats = {
    "pings_ok": 0,
    "pings_failed": 0,
    "pings_answered": 0,  # pings that got an HTTP answer; only these have a time
    "ping_ms": 0.0,
    "completed": 0,
    "setup_finished": 0,
    "running": set(),
    "riding": set(),
}


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    # Duplicated from the backend on purpose: the simulator never imports backend code.
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


async def api(client: httpx.AsyncClient, method: str, path: str, token: str, body: dict | None = None) -> httpx.Response:
    return await client.request(method, path, json=body, headers={"Authorization": f"Bearer {token}"})


async def get_route(client: httpx.AsyncClient, from_lat: float, from_lng: float, to_lat: float, to_lng: float):
    """Returns (path, problem). The path is a list of (lat, lng); problem is "no route" or an error text."""
    # OSRM wants longitude first and sends [lng, lat] pairs. This is the one place that converts.
    url = (
        f"/route/v1/driving/{from_lng},{from_lat};{to_lng},{to_lat}"
        f"?overview=full&geometries=geojson&steps=false&radiuses={SNAP_RADIUS_M};{SNAP_RADIUS_M}"
    )
    # OSRM answers NoRoute and NoSegment with HTTP 400, so the body is read whatever the status is.
    try:
        response = await client.get(url)
        body = response.json()
    except (httpx.HTTPError, ValueError) as error:
        return None, f"OSRM error ({type(error).__name__})"
    code = body.get("code") if isinstance(body, dict) else None
    if code == "Ok":
        return [(lat, lng) for lng, lat in body["routes"][0]["geometry"]["coordinates"]], None
    if code in ("NoRoute", "NoSegment"):
        return None, "no route"
    return None, f"OSRM answered {code or response.status_code}"


async def run_driver(n, args, world, sem, admin_token, api_client, osrm_client, stop) -> None:
    name = f"sim-driver-{n:03d}"
    log = logging.getLogger(name)
    email = f"{name}@{EMAIL_DOMAIN}"
    credentials = {"email": email, "password": DRIVER_PASSWORD}
    rng = random.Random(None if args.seed is None else f"{args.seed}-{n}")  # one generator per driver: repeatable
    speed_factor = rng.uniform(0.8, 1.2)
    speed_m_s = args.speed_kmh * speed_factor / 3.6
    south, west, north, east = world["bounds"]
    center_lat, center_lng = world["center"]
    loop = asyncio.get_running_loop()

    # ---- Setup: every step is skipped when it is already done, so a rerun reuses the accounts ----
    token = None
    pos = None
    ready = False
    notes = []
    async with sem:
        if stop.is_set():
            return
        try:
            r = await api_client.post("/auth/login", json=credentials)
            if r.status_code == 401:
                r = await api_client.post(
                    "/auth/register",
                    json={"name": f"Sim Driver {n:03d}", "email": email, "password": DRIVER_PASSWORD, "role": "driver"},
                )
                r.raise_for_status()
                notes.append("registered")
                r = await api_client.post("/auth/login", json=credentials)
            r.raise_for_status()
            token = r.json()["access_token"]

            r = await api(api_client, "GET", "/drivers/me", token)
            if r.status_code == 404:
                r = await api(api_client, "POST", "/drivers/me/profile", token, {"license_number": f"SIM-LIC-{n:03d}"})
                notes.append("profile created")
            r.raise_for_status()
            driver = r.json()
            if driver["vehicle"] is None:
                r = await api(
                    api_client, "POST", "/drivers/me/vehicle", token,
                    {"plate_number": f"SIM{n:03d}", "model": "Sim Car", "color": "Grey"},
                )
                r.raise_for_status()
                driver = r.json()
                notes.append("vehicle added")
            if driver["verification_status"] != "approved":
                r = await api(api_client, "POST", f"/admin/drivers/{driver['id']}/approve", admin_token)
                r.raise_for_status()
                notes.append("approved")

            r = await api(api_client, "GET", "/drivers/me/presence", token)
            r.raise_for_status()
            presence = r.json()
            if presence["online"]:
                pos = (presence["lat"], presence["lng"])
                notes.append("resumed from its last position")
            else:
                for _ in range(MAX_TRIES):
                    bearing = rng.uniform(0, 2 * math.pi)
                    distance_m = args.radius_km * 1000 * math.sqrt(rng.random())
                    lat = center_lat + distance_m * math.cos(bearing) / METERS_PER_DEGREE
                    lng = center_lng + distance_m * math.sin(bearing) / (METERS_PER_DEGREE * math.cos(math.radians(center_lat)))
                    if not (south <= lat <= north and west <= lng <= east):
                        continue
                    response = await osrm_client.get(f"/nearest/v1/driving/{lng},{lat}?number=1")
                    body = response.json()
                    if body.get("code") != "Ok" or body["waypoints"][0]["distance"] > SNAP_RADIUS_M:
                        continue
                    lng, lat = body["waypoints"][0]["location"]
                    # A road cut off from the rest of the network would leave the driver parked for good.
                    path, _ = await get_route(osrm_client, lat, lng, center_lat, center_lng)
                    if path is not None:
                        pos = (lat, lng)
                        break
                if pos is None:
                    log.error("setup failed: no start point connected to the center found in %d tries", MAX_TRIES)
            if pos is not None:
                r = await api(api_client, "POST", "/drivers/me/online", token, {"lat": pos[0], "lng": pos[1]})
                r.raise_for_status()
                ready = True
                log.info("online at %.5f,%.5f (%s), speed x%.2f", pos[0], pos[1], ", ".join(notes) or "account reused", speed_factor)
        except httpx.HTTPStatusError as error:
            log.error("setup failed: %s %s", error.response.status_code, error.response.text)
        except (httpx.HTTPError, ValueError) as error:
            log.error("setup failed: %r", error)
    stats["setup_finished"] += 1
    if stats["setup_finished"] == args.drivers:
        log.info("setup of %d drivers finished after %.1f s", args.drivers, time.monotonic() - world["started"])
    if not ready:
        return

    # ---- Ticks ----
    plan = (None, None)  # (kind, ride id); kind is wander, pickup, wait, or dropoff
    path = None
    cum = []  # cumulative length in metres at each path point
    seg = 0
    traveled = 0.0
    target = None
    waited = 0
    finished_ride = None
    problems = {}
    rejected = False
    last_tick = loop.time()
    next_tick = last_tick + rng.uniform(0, PING_INTERVAL_S)  # staggered so pings do not arrive in lockstep
    stats["running"].add(n)
    while not stop.is_set():
        delay = next_tick - loop.time()
        if delay > 0:
            try:
                await asyncio.wait_for(stop.wait(), delay)
                break
            except asyncio.TimeoutError:
                pass
        now = loop.time()
        elapsed = now - last_tick
        last_tick = now
        next_tick += PING_INTERVAL_S  # from the schedule, not from now, so the loop does not drift
        if next_tick < now:
            next_tick = now + PING_INTERVAL_S
        tick_problems = {}

        # (1) Work out the plan from the driver's active ride.
        ride = None
        known = False
        try:
            r = await api(api_client, "GET", "/rides/active", token)
            if r.status_code == 200:
                ride = r.json()
                known = True
            elif r.status_code == 404:
                known = True
            else:
                tick_problems["ride"] = f"GET /rides/active answered {r.status_code}"
        except httpx.HTTPError as error:
            tick_problems["ride"] = f"GET /rides/active failed ({type(error).__name__})"
        if known:
            if ride is None:
                wanted = ("wander", None)
            elif ride["status"] == "DRIVER_ASSIGNED":
                wanted = ("pickup", ride["id"])
            elif ride["status"] == "DRIVER_ARRIVED":
                wanted = ("wait", ride["id"])
            elif ride["status"] == "IN_PROGRESS":
                wanted = ("dropoff", ride["id"])
            else:
                wanted = plan
            if wanted != plan:
                if wanted[0] == "wander" and plan[1] is not None and plan[1] != finished_ride:
                    log.info("ride %s is gone, wandering again", plan[1])
                if wanted[0] == "pickup":
                    target = (ride["pickup_lat"], ride["pickup_lng"])
                    log.info("ride %s assigned, %.0f m to pickup", ride["id"], haversine_m(*pos, *target))
                if wanted[0] == "dropoff":
                    target = (ride["dropoff_lat"], ride["dropoff_lng"])
                plan = wanted
                path = None
                traveled = 0.0
                seg = 0
                waited = 0
                if plan[0] == "wander":
                    stats["riding"].discard(n)
                else:
                    stats["riding"].add(n)

        # (2) Advance along the plan. A leg that has no route yet gets one first.
        if plan[0] in ("wander", "pickup", "dropoff") and path is None:
            problem = "no route"
            if plan[0] == "wander":
                for _ in range(MAX_TRIES):
                    bearing = rng.uniform(0, 2 * math.pi)
                    distance_m = args.radius_km * 1000 * math.sqrt(rng.random())
                    lat = center_lat + distance_m * math.cos(bearing) / METERS_PER_DEGREE
                    lng = center_lng + distance_m * math.sin(bearing) / (METERS_PER_DEGREE * math.cos(math.radians(center_lat)))
                    if not (south <= lat <= north and west <= lng <= east):
                        continue
                    path, problem = await get_route(osrm_client, pos[0], pos[1], lat, lng)
                    # Another point only helps when the point was the problem, not when OSRM is down.
                    if path is not None or problem != "no route":
                        break
            else:
                path, problem = await get_route(osrm_client, pos[0], pos[1], target[0], target[1])
            if path is None:
                tick_problems["route"] = problem
            else:
                cum = [0.0]
                for i in range(1, len(path)):
                    cum.append(cum[-1] + haversine_m(*path[i - 1], *path[i]))
                traveled = 0.0
                seg = 0
        leg_done = False
        if path is not None:
            traveled += speed_m_s * elapsed
            if traveled >= cum[-1]:
                pos = path[-1]
                leg_done = True
            else:
                while cum[seg + 1] <= traveled:
                    seg += 1
                fraction = (traveled - cum[seg]) / (cum[seg + 1] - cum[seg])
                pos = (
                    path[seg][0] + fraction * (path[seg + 1][0] - path[seg][0]),
                    path[seg][1] + fraction * (path[seg + 1][1] - path[seg][1]),
                )

        # (3) Handle a finished leg. A 409 means the ride changed under us: the next tick resyncs.
        action = None
        if plan[0] == "wander" and leg_done:
            path = None
        elif plan[0] == "pickup" and leg_done:
            action = "arrive"
        elif plan[0] == "wait":
            if waited < ARRIVAL_WAIT_TICKS:
                waited += 1
            else:
                action = "start"
        elif plan[0] == "dropoff" and leg_done:
            action = "complete"
        if action is not None:
            try:
                r = await api(api_client, "POST", f"/rides/{plan[1]}/{action}", token)
                if r.status_code == 200:
                    log.info("ride %s: %s", plan[1], RIDE_ACTION_LOGS[action])
                    if action == "complete":
                        stats["completed"] += 1
                        finished_ride = plan[1]
                elif r.status_code != 409:
                    tick_problems["action"] = f"{action} answered {r.status_code}"
            except httpx.HTTPError as error:
                tick_problems["action"] = f"{action} failed ({type(error).__name__})"

        # (4) Ping. Clamped so a road that leaves the city box never causes a 422.
        ping = {"lat": min(max(pos[0], south), north), "lng": min(max(pos[1], west), east)}
        needs_online = False
        for attempt in range(2):  # the second attempt only happens after a re-login
            started = time.monotonic()
            try:
                r = await api(api_client, "POST", "/drivers/me/location", token, ping)
            except httpx.HTTPError as error:
                stats["pings_failed"] += 1
                tick_problems["ping"] = f"ping failed ({type(error).__name__})"
                break
            stats["pings_answered"] += 1
            stats["ping_ms"] += (time.monotonic() - started) * 1000
            if r.status_code == 200:
                stats["pings_ok"] += 1
                break
            stats["pings_failed"] += 1
            if r.status_code == 401 and attempt == 0:
                try:
                    r = await api_client.post("/auth/login", json=credentials)
                except httpx.HTTPError as error:
                    tick_problems["ping"] = f"login failed ({type(error).__name__})"
                    break
                if r.status_code != 200:
                    tick_problems["ping"] = f"login answered {r.status_code}"
                    break
                token = r.json()["access_token"]
                continue
            if r.status_code == 409:
                needs_online = True
            elif r.status_code == 403:
                log.error("ping answered 403 (%s), this driver stops", r.json().get("detail"))
                rejected = True
            else:
                tick_problems["ping"] = f"ping answered {r.status_code}"
            break
        if needs_online:
            try:
                r = await api(api_client, "POST", "/drivers/me/online", token, ping)
                if r.status_code == 200:
                    log.info("presence had expired, online again")
                else:
                    tick_problems["ping"] = f"online answered {r.status_code}"
            except httpx.HTTPError as error:
                tick_problems["ping"] = f"online failed ({type(error).__name__})"

        # Log only changes: a problem is logged when it starts and when it is gone, not every tick.
        # A problem that is missing from the next tick counts as recovered: the failing step is retried every tick.
        for category, text in tick_problems.items():
            if problems.get(category) != text:
                log.warning("%s: %s", category, text)
        for category in problems:
            if category not in tick_problems:
                log.info("%s recovered", category)
        problems = tick_problems
        if rejected:
            break

    stats["running"].discard(n)
    stats["riding"].discard(n)
    # A driver on a ride cannot go offline (409); its presence just expires.
    try:
        await api(api_client, "POST", "/drivers/me/offline", token)
    except httpx.HTTPError:
        log.warning("could not go offline, presence will expire")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Fake driver fleet for the Uber clone")
    parser.add_argument("--drivers", type=int, default=20, help=f"number of drivers, 1 to {MAX_DRIVERS}")
    parser.add_argument("--admin-email", default=os.environ.get("SIM_ADMIN_EMAIL"), help="or env SIM_ADMIN_EMAIL")
    parser.add_argument("--admin-password", default=os.environ.get("SIM_ADMIN_PASSWORD"), help="or env SIM_ADMIN_PASSWORD")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--osrm-url", default="http://127.0.0.1:5000")
    parser.add_argument("--center-lat", type=float, help="fleet center, default: the city center")
    parser.add_argument("--center-lng", type=float, help="fleet center, default: the city center")
    parser.add_argument("--radius-km", type=float, default=5)
    parser.add_argument("--speed-kmh", type=float, default=30)
    parser.add_argument("--seed", type=int, help="makes a run repeatable")
    args = parser.parse_args()

    if not 1 <= args.drivers <= MAX_DRIVERS:
        sys.exit(f"--drivers must be between 1 and {MAX_DRIVERS}")
    if not args.admin_email or not args.admin_password:
        sys.exit("Admin credentials are needed: --admin-email and --admin-password (or SIM_ADMIN_EMAIL and SIM_ADMIN_PASSWORD). "
                 "The admin must already exist (backend/create_admin.py).")
    if (args.center_lat is None) != (args.center_lng is None):
        sys.exit("--center-lat and --center-lng must be given together")
    if args.radius_km <= 0 or args.speed_kmh <= 0:
        sys.exit("--radius-km and --speed-kmh must be positive")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs every request at INFO
    log = logging.getLogger("sim")
    # keepalive_expiry is below uvicorn's 5 s keep-alive timeout: with equal values a request can reach a
    # connection the server has just closed (RemoteProtocolError).
    limits = httpx.Limits(max_connections=MAX_DRIVERS + 50, max_keepalive_connections=MAX_DRIVERS + 50, keepalive_expiry=2)
    async with httpx.AsyncClient(base_url=args.api_url, timeout=API_TIMEOUT_S, limits=limits) as api_client, \
            httpx.AsyncClient(base_url=args.osrm_url, timeout=OSRM_TIMEOUT_S, limits=limits) as osrm_client:
        try:
            r = await api_client.post("/auth/login", json={"email": args.admin_email, "password": args.admin_password})
            if r.status_code != 200:
                sys.exit(f"Admin login failed ({r.status_code}): {r.text}")
            if r.json()["user"]["role"] != "admin":
                sys.exit(f"{args.admin_email} is not an admin")
            admin_token = r.json()["access_token"]
            r = await api(api_client, "GET", "/places/map-config", admin_token)
            r.raise_for_status()
            city = r.json()
        except httpx.HTTPError as error:
            sys.exit(f"Cannot reach the API at {args.api_url}: {error!r}")

        center_lat = city["center_lat"] if args.center_lat is None else args.center_lat
        center_lng = city["center_lng"] if args.center_lng is None else args.center_lng
        bounds = (city["south"], city["west"], city["north"], city["east"])
        if not (bounds[0] <= center_lat <= bounds[2] and bounds[1] <= center_lng <= bounds[3]):
            sys.exit(f"The center {center_lat},{center_lng} is outside the city bounds (south, west, north, east) = {bounds}")
        try:
            r = await osrm_client.get(f"/nearest/v1/driving/{center_lng},{center_lat}?number=1")
            near_road = r.json()["code"] == "Ok" and r.json()["waypoints"][0]["distance"] <= SNAP_RADIUS_M
        except (httpx.HTTPError, ValueError, KeyError) as error:
            sys.exit(f"Cannot use OSRM at {args.osrm_url}: {error!r} (is `docker compose up -d osrm` running?)")
        if not near_road:
            sys.exit(f"The center {center_lat},{center_lng} is more than {SNAP_RADIUS_M} m from a road. Choose another center.")

        log.info(
            "settings: drivers=%d api=%s osrm=%s city=%s center=%.5f,%.5f radius=%.1f km speed=%.0f km/h seed=%s",
            args.drivers, args.api_url, args.osrm_url, city["city_name"], center_lat, center_lng,
            args.radius_km, args.speed_kmh, args.seed,
        )

        stop = asyncio.Event()
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGINT, stop.set)
        except NotImplementedError:
            pass  # Windows: asyncio.run cancels this task on Ctrl+C instead, handled below
        world = {"bounds": bounds, "center": (center_lat, center_lng), "started": time.monotonic()}
        sem = asyncio.Semaphore(SETUP_CONCURRENCY)
        tasks = [
            asyncio.create_task(run_driver(n, args, world, sem, admin_token, api_client, osrm_client, stop))
            for n in range(1, args.drivers + 1)
        ]

        last = {"ok": 0, "failed": 0, "answered": 0, "ms": 0.0}
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), SUMMARY_INTERVAL_S)
                except asyncio.TimeoutError:
                    answered = stats["pings_answered"] - last["answered"]
                    log.info(
                        "summary: running %d/%d, on a ride %d, pings ok %d failed %d, avg ping %.0f ms, rides completed %d",
                        len(stats["running"]), args.drivers, len(stats["riding"]),
                        stats["pings_ok"] - last["ok"], stats["pings_failed"] - last["failed"],
                        (stats["ping_ms"] - last["ms"]) / answered if answered else 0, stats["completed"],
                    )
                    last = {"ok": stats["pings_ok"], "failed": stats["pings_failed"],
                            "answered": stats["pings_answered"], "ms": stats["ping_ms"]}
        except asyncio.CancelledError:
            pass  # Ctrl+C on Windows

        log.info("stopping, taking drivers offline")
        stop.set()
        done, pending = await asyncio.wait(tasks, timeout=10)
        for task in pending:
            task.cancel()
        for task in done:
            if task.exception() is not None:
                log.error("a driver task crashed: %r", task.exception())
        log.info(
            "final: ran %.0f s, pings ok %d failed %d, avg ping %.0f ms, rides completed %d",
            time.monotonic() - world["started"], stats["pings_ok"], stats["pings_failed"],
            stats["ping_ms"] / stats["pings_answered"] if stats["pings_answered"] else 0, stats["completed"],
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
