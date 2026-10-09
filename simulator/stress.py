"""Concurrency stress test (M4.1). Runs on the host and fires many requests at the same instant, then looks in
the database for broken invariants (simulator/invariants.sql) and prints how often each one broke.

It checks the fix for the double-booking bug (M4.2): exit code 0 is the expected result. Before M4.2 it reproduced
the bug, and exit code 1 (an invariant broke) was expected. It changes nothing in the backend.

Everything that changes state goes through the public API. The only direct database access is read-only SELECTs
through `docker compose exec db psql`. It never touches Redis, Nominatim, or OpenStreetMap (only our API and OSRM).

Usage: python simulator/stress.py --scenario drivers --admin-email ... --admin-password ...
Scenarios: drivers (I1 and I2), riders (I3), fleet (against the running simulator.py fleet).
Stop simulator.py for the drivers and riders scenarios: its drivers would join the test.
--api-url takes several comma-separated URLs (two backend processes): requests are spread over them round-robin.
"""
import argparse
import asyncio
import collections
import logging
import math
import os
import pathlib
import random
import signal
import statistics
import sys
import time

try:
    import httpx
except ImportError:
    print("httpx is not installed. Run: pip install -r simulator/requirements.txt", file=sys.stderr)
    sys.exit(2)

PASSWORD = "stress-pass-1234"
DRIVER_EMAIL = "stress-driver-{n:02d}@sim.example.com"
RIDER_EMAIL = "stress-rider-{n:03d}@sim.example.com"
DRIVER_LIKE = "stress-driver-%@sim.example.com"
RIDER_LIKE = "stress-rider-%@sim.example.com"
SIM_DRIVER_LIKE = "sim-driver-%@sim.example.com"
SETUP_CONCURRENCY = 5  # login hashing blocks the backend's event loop (M1.1)
DRIVER_DISC_M = 2000
DROPOFF_MIN_M = 1500
DROPOFF_MAX_M = 3000
SNAP_RADIUS_M = 300
MAX_TRIES = 10
API_TIMEOUT_S = 30
OSRM_TIMEOUT_S = 5
SQL_TIMEOUT_S = 30
MAX_CONNECTIONS = 200
BARRIER_WAIT_S = 0.5  # every task is parked on the event before it is set
SNAPSHOT_INTERVAL_S = 1
SETTLE_S = 3  # after Ctrl+C or a failure: time for requests still in flight to finish on the server
CLEANUP_PASSES = 5
METERS_PER_DEGREE = 111_320
ACTIVE_STATUSES = "'REQUESTED', 'DRIVER_ASSIGNED', 'DRIVER_ARRIVED', 'IN_PROGRESS'"
ROOT = pathlib.Path(__file__).resolve().parent.parent
INVARIANTS_FILE = ROOT / "simulator" / "invariants.sql"

# section name in invariants.sql -> (short name, what an offender is, what the count counts, columns of a row)
INVARIANTS = {
    "driver_pending_offers": ("I1", "drivers", "offers", ("driver_id", "count", "offer_ids", "ride_ids")),
    "driver_active_rides": ("I2", "drivers", "rides", ("driver_id", "count", "ride_ids", "statuses")),
    "rider_active_rides": ("I3", "riders", "rides", ("rider_id", "count", "ride_ids", "statuses")),
    "stuck_requested": ("I4", "rides", None, ("ride_id", "created_at")),
}

log = logging.getLogger("stress")
status_counts = collections.Counter()  # every HTTP answer of the whole run, by status code


async def api(
    client: httpx.AsyncClient, method: str, path: str, token: str | None = None, body: dict | None = None,
    gate: asyncio.Event | None = None,
) -> httpx.Response:
    """One HTTP call with a bearer token. With a gate it waits for the event first (see burst)."""
    if gate is not None:
        await gate.wait()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = await client.request(method, path, json=body, headers=headers)
    status_counts[response.status_code] += 1
    return response


async def sql(ctx: dict, text: str) -> list[str]:
    """Runs SQL through psql in the db container and returns the non-empty output lines. Read-only by Postgres."""
    args = ctx["args"]
    try:
        process = await asyncio.create_subprocess_exec(
            "docker", "compose", "exec", "-T", "-e", "PGOPTIONS=-c default_transaction_read_only=on", "db",
            "psql", "-U", args.psql_user, "-d", args.psql_db, "-v", "ON_ERROR_STOP=1", "-At", "-F", "|",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=ROOT,
        )
        out, err = await asyncio.wait_for(process.communicate(text.encode()), SQL_TIMEOUT_S)
    except FileNotFoundError:
        raise RuntimeError("psql could not run: the docker command was not found")
    except asyncio.TimeoutError:
        process.kill()
        raise RuntimeError(f"psql did not answer within {SQL_TIMEOUT_S} s")
    if process.returncode != 0:
        raise RuntimeError(f"psql could not run (exit {process.returncode}): {err.decode().strip()}")
    return [line for line in out.decode().splitlines() if line]


async def snap(ctx: dict, min_m: float, max_m: float, on_road: bool = True) -> tuple[float, float]:
    """A random point between min_m and max_m metres from the center, inside the city bounds.

    With on_road it is moved to the nearest road by OSRM (longitude first in the URL), at most SNAP_RADIUS_M away.
    """
    south, west, north, east = ctx["bounds"]
    center_lat, center_lng = ctx["center"]
    rng = ctx["rng"]
    for _ in range(MAX_TRIES):
        bearing = rng.uniform(0, 2 * math.pi)
        distance_m = math.sqrt(rng.uniform(min_m**2, max_m**2))  # even spread over the ring
        lat = center_lat + distance_m * math.cos(bearing) / METERS_PER_DEGREE
        lng = center_lng + distance_m * math.sin(bearing) / (METERS_PER_DEGREE * math.cos(math.radians(center_lat)))
        if on_road:
            response = await ctx["osrm"].get(f"/nearest/v1/driving/{lng},{lat}?number=1")
            body = response.json()
            if body.get("code") != "Ok" or body["waypoints"][0]["distance"] > SNAP_RADIUS_M:
                continue
            lng, lat = body["waypoints"][0]["location"]
        if south <= lat <= north and west <= lng <= east:
            return lat, lng
    raise RuntimeError(f"no usable point found in {MAX_TRIES} tries ({min_m:g} to {max_m:g} m from the center)")


async def setup_accounts(ctx: dict, kind: str, n: int) -> None:
    """Makes one stress account ready (callers gather them): it exists, can log in, and a driver is approved."""
    client = ctx["api"]
    email = (DRIVER_EMAIL if kind == "driver" else RIDER_EMAIL).format(n=n)
    credentials = {"email": email, "password": PASSWORD}
    async with ctx["sem"]:
        response = await api(client, "POST", "/auth/login", None, credentials)
        if response.status_code == 401:
            name = f"Stress {kind.capitalize()} {n:02d}" if kind == "driver" else f"Stress Rider {n:03d}"
            response = await api(
                client, "POST", "/auth/register", None, {"name": name, "email": email, "password": PASSWORD, "role": kind}
            )
            if response.status_code != 201:
                raise RuntimeError(f"setup failed: registering {email} answered {response.status_code} {response.text}")
            response = await api(client, "POST", "/auth/login", None, credentials)
        if response.status_code != 200:
            raise RuntimeError(f"setup failed: login of {email} answered {response.status_code} {response.text}")
        token = response.json()["access_token"]
        ctx["tokens"][email] = token

        if kind == "driver":
            response = await api(client, "GET", "/drivers/me", token)
            if response.status_code == 404:
                response = await api(client, "POST", "/drivers/me/profile", token, {"license_number": f"STR-LIC-{n:02d}"})
            if response.status_code not in (200, 201):
                raise RuntimeError(f"setup failed: profile of {email} answered {response.status_code} {response.text}")
            driver = response.json()
            if driver["vehicle"] is None:
                response = await api(
                    client, "POST", "/drivers/me/vehicle", token,
                    {"plate_number": f"STR{n:02d}", "model": "Stress Car", "color": "Grey"},
                )
                if response.status_code != 201:
                    raise RuntimeError(f"setup failed: vehicle of {email} answered {response.status_code} {response.text}")
                driver = response.json()
            if driver["verification_status"] != "approved":
                response = await api(client, "POST", f"/admin/drivers/{driver['id']}/approve", ctx["admin_token"])
                if response.status_code != 200:
                    raise RuntimeError(f"setup failed: approving {email} answered {response.status_code} {response.text}")


async def burst(ctx: dict, requests: list[tuple]) -> dict:
    """Sends (token, method, path, body) requests so that they all start in the same instant.

    One shared client; warmed up with as many /health calls as there are requests so the connections already
    exist; one task per request, all parked on one event; the event is set after BARRIER_WAIT_S.
    With --sequential the requests go one after another instead (the control run).
    Returns {"results": [{"status", "body", "ms"}] in request order, "summary": text}.
    """
    client = ctx["api"]
    urls = ctx["args"].api_urls
    # Request number i goes to URL i % number of URLs, so the repeats of one rider alternate between processes.
    if ctx["args"].sequential:
        started = time.monotonic()
        outcomes = []
        for index, (token, method, path, body) in enumerate(requests):
            try:
                outcomes.append(await api(client, method, urls[index % len(urls)] + path, token, body))
            except httpx.HTTPError as error:
                outcomes.append(error)
        seconds = time.monotonic() - started
    else:
        await asyncio.gather(*[client.get(urls[index % len(urls)] + "/health") for index in range(len(requests))])
        gate = asyncio.Event()
        tasks = [
            asyncio.create_task(api(client, method, urls[index % len(urls)] + path, token, body, gate))
            for index, (token, method, path, body) in enumerate(requests)
        ]
        await asyncio.sleep(BARRIER_WAIT_S)
        started = time.monotonic()
        gate.set()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        seconds = time.monotonic() - started

    results = []
    counts = collections.Counter()
    for outcome in outcomes:
        if isinstance(outcome, Exception):
            status_counts["error"] += 1
            results.append({"status": 0, "body": None, "ms": None})
            counts[f"error {type(outcome).__name__}"] += 1
            continue
        try:
            body = outcome.json()
        except ValueError:
            body = None
        results.append({"status": outcome.status_code, "body": body, "ms": outcome.elapsed.total_seconds() * 1000})
        label = str(outcome.status_code)
        if outcome.status_code in (200, 201) and isinstance(body, dict) and isinstance(body.get("status"), str):
            label += f" {body['status']}"
        counts[label] += 1

    times = [result["ms"] for result in results if result["ms"] is not None]
    if requests:
        ctx["latencies"].setdefault(requests[0][2].split("/")[1], []).extend(times)
    latency = f" (median {statistics.median(times):.0f} ms, max {max(times):.0f} ms)" if times else ""
    counts_text = ", ".join(f"{label} x{count}" for label, count in sorted(counts.items())) or "nothing sent"
    mode = "one by one" if ctx["args"].sequential else "together"
    return {"results": results, "summary": f"{len(requests)} requests {mode} in {seconds * 1000:.0f} ms{latency}: {counts_text}"}


async def check_invariants(ctx: dict, seen: dict) -> str:
    """Runs invariants.sql once. Adds every offender to `seen` (this round's record) and returns a text like
    'I1 2 drivers (max 3 offers), I2 ok, I3 ok, I4 ok' for everything seen in the round so far."""
    lines = await sql(ctx, INVARIANTS_FILE.read_text())
    found = {}
    section = None
    for line in lines:
        if line.startswith("== "):
            section = line[3:]
            found[section] = []
        elif section is None:
            raise RuntimeError(f"psql output not understood (row before any section): {line}")
        else:
            found[section].append(line.split("|"))
    if set(found) != set(INVARIANTS):
        raise RuntimeError(f"psql output did not contain all four invariants (got {sorted(found)})")
    ctx["checks"] += 1

    parts = []
    for name, (code, who, what, columns) in INVARIANTS.items():
        offenders = seen.setdefault(name, {})
        for row in found[name]:
            if row[0] not in offenders:  # the full row is logged once per round
                log.warning("%s %s offender: %s", code, name, " ".join(f"{column}={value}" for column, value in zip(columns, row)))
            offenders[row[0]] = max(offenders.get(row[0], 0), int(row[1]) if what else 1)
        if not offenders:
            parts.append(f"{code} ok")
        elif what:
            parts.append(f"{code} {len(offenders)} {who} (max {max(offenders.values())} {what})")
        else:
            parts.append(f"{code} {len(offenders)} {who}")
    return ", ".join(parts)


async def cleanup(ctx: dict) -> None:
    """Cancels every active ride of the stress riders (found in the database, so broken state and leftovers of a
    crashed run are cleared too), then takes the stress drivers offline.

    It repeats until a query finds no active ride: requests that were still in flight when a run was stopped are
    finished by the server and create rides after the first listing."""
    client = ctx["api"]
    if ctx["unsettled"]:
        await asyncio.sleep(SETTLE_S)
    driver_emails = list(ctx["driver_emails"])
    if ctx["args"].cleanup_only:
        driver_emails = await sql(ctx, f"SELECT email FROM users WHERE email LIKE '{DRIVER_LIKE}' ORDER BY id")

    cancelled = 0
    for _ in range(CLEANUP_PASSES):
        rows = await sql(
            ctx,
            f"SELECT u.email, r.id FROM rides r JOIN users u ON u.id = r.rider_id "
            f"WHERE u.email LIKE '{RIDER_LIKE}' AND r.status IN ({ACTIVE_STATUSES}) ORDER BY r.id",
        )
        rides = [row.split("|") for row in rows]

        # Accounts of an earlier run have no token yet: log in, a few at a time.
        missing = sorted(({email for email, _ in rides} | set(driver_emails)) - set(ctx["tokens"]))
        for start in range(0, len(missing), SETUP_CONCURRENCY):
            chunk = missing[start : start + SETUP_CONCURRENCY]
            logins = [api(client, "POST", "/auth/login", None, {"email": e, "password": PASSWORD}) for e in chunk]
            for email, answer in zip(chunk, await asyncio.gather(*logins)):
                if answer.status_code == 200:
                    ctx["tokens"][email] = answer.json()["access_token"]
                else:
                    log.warning("cleanup: cannot log in as %s (%s)", email, answer.status_code)

        if not rides:
            break
        cancels = [(email, ride_id) for email, ride_id in rides if email in ctx["tokens"]]
        answers = await asyncio.gather(*[api(client, "POST", f"/rides/{ride_id}/cancel", ctx["tokens"][email]) for email, ride_id in cancels])
        cancelled += sum(1 for answer in answers if answer.status_code == 200)
        if cancelled and any(answer.status_code not in (200, 404, 409) for answer in answers):
            log.warning("cleanup: some cancels failed: %s", sorted({answer.status_code for answer in answers}))
        if ctx["unsettled"]:
            await asyncio.sleep(1)
    else:
        log.warning("cleanup: rides of stress riders are still active after %d passes (an IN_PROGRESS ride cannot be cancelled)", CLEANUP_PASSES)

    online = [email for email in driver_emails if email in ctx["tokens"]]
    answers = await asyncio.gather(*[api(client, "POST", "/drivers/me/offline", ctx["tokens"][email]) for email in online])
    odd = [email for email, answer in zip(online, answers) if answer.status_code not in (200, 404, 409)]
    if odd:
        log.warning("cleanup: taking %s offline failed", odd)
    if cancelled or ctx["args"].cleanup_only:
        log.info("cleanup: %d rides cancelled, %d stress drivers taken offline", cancelled, len(online))


async def scenario_drivers(ctx: dict) -> None:
    """Many riders request at once while only a few drivers are free: a driver must get at most one offer (I1),
    and drivers who hold several offers and accept them all at once must not get several rides (I2)."""
    args = ctx["args"]
    ctx["driver_emails"] = [DRIVER_EMAIL.format(n=n) for n in range(1, args.drivers + 1)]
    started = time.monotonic()
    await asyncio.gather(
        *[setup_accounts(ctx, "driver", n) for n in range(1, args.drivers + 1)],
        *[setup_accounts(ctx, "rider", n) for n in range(1, args.riders + 1)],
    )
    log.info("setup of %d drivers and %d riders took %.1f s", args.drivers, args.riders, time.monotonic() - started)

    for round_number in range(1, args.rounds + 1):
        await cleanup(ctx)
        seen = {}
        ctx["rounds"].append(seen)

        pings = []
        for email in ctx["driver_emails"]:
            lat, lng = await snap(ctx, 0, DRIVER_DISC_M, on_road=False)
            pings.append(api(ctx["api"], "POST", "/drivers/me/online", ctx["tokens"][email], {"lat": lat, "lng": lng}))
        for answer in await asyncio.gather(*pings):
            if answer.status_code != 200:
                raise RuntimeError(f"a stress driver could not go online: {answer.status_code} {answer.text}")

        requests = []
        for n in range(1, args.riders + 1):
            pickup = await snap(ctx, 0, args.spread_m)
            dropoff = await snap(ctx, DROPOFF_MIN_M, DROPOFF_MAX_M)
            body = {
                "pickup_lat": pickup[0], "pickup_lng": pickup[1], "pickup_address": f"Stress pickup {n}",
                "dropoff_lat": dropoff[0], "dropoff_lng": dropoff[1], "dropoff_address": f"Stress drop-off {n}",
            }
            requests.append((ctx["tokens"][RIDER_EMAIL.format(n=n)], "POST", "/rides", body))
        fired = await burst(ctx, requests)

        ride_ids = [int(result["body"]["id"]) for result in fired["results"] if result["status"] == 201]
        await check_invariants(ctx, seen)  # I1 is the point here
        offers = []
        if ride_ids:
            rows = await sql(
                ctx,
                "SELECT o.id, u.email FROM ride_offers o JOIN drivers d ON d.id = o.driver_id JOIN users u ON u.id = d.user_id "
                f"WHERE o.ride_id IN ({', '.join(str(ride_id) for ride_id in ride_ids)}) "
                "AND o.status = 'PENDING' AND o.expires_at > now() ORDER BY o.id",
            )
            offers = [row.split("|") for row in rows]
        expected = min(args.riders, args.drivers)
        ctx["offers"]["got"] += len(offers)
        ctx["offers"]["expected"] += expected
        ctx["offers"]["lost_rounds"] += len(offers) < expected
        mine = [(offer_id, email) for offer_id, email in offers if email in ctx["tokens"] and email.startswith("stress-driver-")]
        if len(mine) != len(offers):
            log.warning("round %d: offers went to drivers that are not stress drivers (%s): other drivers are online, "
                        "stop the simulator, this round is diluted", round_number, len(offers) - len(mine))

        # Every live offer is accepted at the same moment, each with its own driver's token.
        accepted = await burst(ctx, [(ctx["tokens"][email], "POST", f"/offers/{offer_id}/accept", None) for offer_id, email in mine])
        invariants = await check_invariants(ctx, seen)  # I2 is the point here
        log.info("drivers round %d/%d: %s; offers %d/%d; accepts: %s; %s",
                 round_number, args.rounds, fired["summary"], len(offers), expected, accepted["summary"], invariants)
        if not (round_number == args.rounds and args.keep_last_round):
            await cleanup(ctx)


async def scenario_riders(ctx: dict) -> None:
    """Every rider sends the same request several times at once: exactly one may succeed (I3)."""
    args = ctx["args"]
    ctx["driver_emails"] = [DRIVER_EMAIL.format(n=n) for n in range(1, args.drivers + 1)]
    started = time.monotonic()
    await asyncio.gather(
        *[setup_accounts(ctx, "driver", n) for n in range(1, args.drivers + 1)],
        *[setup_accounts(ctx, "rider", n) for n in range(1, args.riders + 1)],
    )
    log.info("setup of %d drivers and %d riders took %.1f s", args.drivers, args.riders, time.monotonic() - started)

    for round_number in range(1, args.rounds + 1):
        await cleanup(ctx)
        seen = {}
        ctx["rounds"].append(seen)

        pings = []
        for email in ctx["driver_emails"]:
            lat, lng = await snap(ctx, 0, DRIVER_DISC_M, on_road=False)
            pings.append(api(ctx["api"], "POST", "/drivers/me/online", ctx["tokens"][email], {"lat": lat, "lng": lng}))
        for answer in await asyncio.gather(*pings):
            if answer.status_code != 200:
                raise RuntimeError(f"a stress driver could not go online: {answer.status_code} {answer.text}")

        requests = []
        for n in range(1, args.riders + 1):
            pickup = await snap(ctx, 0, args.spread_m)
            dropoff = await snap(ctx, DROPOFF_MIN_M, DROPOFF_MAX_M)
            body = {
                "pickup_lat": pickup[0], "pickup_lng": pickup[1], "pickup_address": f"Stress pickup {n}",
                "dropoff_lat": dropoff[0], "dropoff_lng": dropoff[1], "dropoff_address": f"Stress drop-off {n}",
            }
            requests.extend([(ctx["tokens"][RIDER_EMAIL.format(n=n)], "POST", "/rides", body)] * args.repeat)
        fired = await burst(ctx, requests)

        created = []  # how many 201 answers each rider got this round
        for index in range(args.riders):
            answers = fired["results"][index * args.repeat : (index + 1) * args.repeat]
            created.append(sum(1 for answer in answers if answer["status"] == 201))
            per_rider = ctx["per_rider"].setdefault(index + 1, collections.Counter())
            for answer in answers:
                per_rider[answer["status"]] += 1
        several = [count for count in created if count > 1]
        invariants = await check_invariants(ctx, seen)  # I3 is the point here
        log.info(
            "riders round %d/%d: %s; riders with more than one 201: %d (max %d); %s",
            round_number, args.rounds, fired["summary"], len(several), max(created), invariants,
        )
        if not (round_number == args.rounds and args.keep_last_round):
            await cleanup(ctx)


async def scenario_fleet(ctx: dict) -> None:
    """Riders request at once against the running simulator fleet; the simulated drivers answer on their own
    and the invariants are checked about every second."""
    args = ctx["args"]
    ctx["driver_emails"] = []
    started = time.monotonic()
    await asyncio.gather(*[setup_accounts(ctx, "rider", n) for n in range(1, args.riders + 1)])
    log.info("setup of %d riders took %.1f s", args.riders, time.monotonic() - started)
    rows = await sql(ctx, f"SELECT count(*) FROM users WHERE email LIKE '{SIM_DRIVER_LIKE}'")
    if rows[0] == "0":
        raise RuntimeError("no sim-driver users exist: start simulator/simulator.py first (for example --drivers 30 --speed-kmh 30 --seed 1)")

    for round_number in range(1, args.rounds + 1):
        await cleanup(ctx)
        seen = {}
        ctx["rounds"].append(seen)

        requests = []
        for n in range(1, args.riders + 1):
            pickup = await snap(ctx, 0, args.spread_m)
            dropoff = await snap(ctx, DROPOFF_MIN_M, DROPOFF_MAX_M)
            body = {
                "pickup_lat": pickup[0], "pickup_lng": pickup[1], "pickup_address": f"Stress pickup {n}",
                "dropoff_lat": dropoff[0], "dropoff_lng": dropoff[1], "dropoff_address": f"Stress drop-off {n}",
            }
            requests.append((ctx["tokens"][RIDER_EMAIL.format(n=n)], "POST", "/rides", body))
        fired = await burst(ctx, requests)
        ride_ids = [int(result["body"]["id"]) for result in fired["results"] if result["status"] == 201]

        deadline = time.monotonic() + args.watch_seconds
        while True:
            invariants = await check_invariants(ctx, seen)
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(SNAPSHOT_INTERVAL_S)

        statuses = {}
        if ride_ids:
            rows = await sql(ctx, f"SELECT status, count(*) FROM rides WHERE id IN ({', '.join(str(i) for i in ride_ids)}) GROUP BY status ORDER BY status")
            statuses = {row.split("|")[0]: int(row.split("|")[1]) for row in rows}
        ctx["fleet"]["rides"] += sum(statuses.values())
        ctx["fleet"]["no_driver"] += statuses.get("NO_DRIVER_FOUND", 0)
        ended = ", ".join(f"{status} x{count}" for status, count in statuses.items()) or "no rides created"
        log.info("fleet round %d/%d: %s; after %d s the rides are: %s; %s",
                 round_number, args.rounds, fired["summary"], args.watch_seconds, ended, invariants)
        if statuses and set(statuses) == {"NO_DRIVER_FOUND"}:
            log.warning("fleet round %d: every ride ended NO_DRIVER_FOUND (fleet not running or out of range): this round proves nothing", round_number)
        if not (round_number == args.rounds and args.keep_last_round):
            await cleanup(ctx)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Concurrency stress test: fires simultaneous requests and checks database invariants")
    parser.add_argument("--scenario", choices=["drivers", "riders", "fleet"], default="drivers")
    parser.add_argument("--admin-email", default=os.environ.get("SIM_ADMIN_EMAIL"), help="or env SIM_ADMIN_EMAIL")
    parser.add_argument("--admin-password", default=os.environ.get("SIM_ADMIN_PASSWORD"), help="or env SIM_ADMIN_PASSWORD")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000", help="one URL, or several separated by commas (two backend processes)")
    parser.add_argument("--osrm-url", default="http://127.0.0.1:5000")
    parser.add_argument("--center-lat", type=float, help="default: the city center")
    parser.add_argument("--center-lng", type=float, help="default: the city center")
    parser.add_argument("--rounds", type=int, default=5, help="1 to 50")
    parser.add_argument("--riders", type=int, default=20, help="2 to 100")
    parser.add_argument("--drivers", type=int, help="1 to 100; default 3 (drivers), 2 x riders (riders), unused (fleet)")
    parser.add_argument("--repeat", type=int, default=2, help="riders scenario: the same request this many times at once, 2 to 5")
    parser.add_argument("--spread-m", type=float, default=150, help="pickups are this far from the center at most")
    parser.add_argument("--watch-seconds", type=float, default=30, help="fleet scenario: how long to watch after the burst")
    parser.add_argument("--seed", type=int, help="makes the random points repeatable")
    parser.add_argument("--sequential", action="store_true", help="control run: one request after another, no race expected")
    parser.add_argument("--keep-last-round", action="store_true", help="skip the cleanup after the last round")
    parser.add_argument("--cleanup-only", action="store_true", help="only cancel the stress riders' rides and take the stress drivers offline")
    parser.add_argument("--label", default="", help="free text printed in the summary, to tell saved outputs apart")
    parser.add_argument("--psql-user", help="default: POSTGRES_USER from .env")
    parser.add_argument("--psql-db", help="default: POSTGRES_DB from .env")
    args = parser.parse_args()

    env = {}
    if (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator and not key.lstrip().startswith("#"):
                env[key.strip()] = value.strip()
    args.psql_user = args.psql_user or env.get("POSTGRES_USER")
    args.psql_db = args.psql_db or env.get("POSTGRES_DB")
    if not args.psql_user or not args.psql_db:
        parser.error("the database user and name are not in .env (POSTGRES_USER, POSTGRES_DB): pass --psql-user and --psql-db")
    if not args.cleanup_only and (not args.admin_email or not args.admin_password):
        parser.error("admin credentials are needed: --admin-email and --admin-password (or SIM_ADMIN_EMAIL and SIM_ADMIN_PASSWORD). "
                     "The admin must already exist (backend/create_admin.py).")
    if not 1 <= args.rounds <= 50:
        parser.error("--rounds must be between 1 and 50")
    if not 2 <= args.riders <= 100:
        parser.error("--riders must be between 2 and 100")
    if args.drivers is None:
        args.drivers = {"drivers": 3, "riders": min(2 * args.riders, 100), "fleet": 0}[args.scenario]
    if args.scenario != "fleet" and not 1 <= args.drivers <= 100:
        parser.error("--drivers must be between 1 and 100")
    if not 2 <= args.repeat <= 5:
        parser.error("--repeat must be between 2 and 5")
    if args.spread_m < 0 or args.watch_seconds <= 0:
        parser.error("--spread-m must not be negative and --watch-seconds must be positive")
    if (args.center_lat is None) != (args.center_lng is None):
        parser.error("--center-lat and --center-lng must be given together")
    args.api_urls = [url.strip().rstrip("/") for url in args.api_url.split(",") if url.strip()]
    if not args.api_urls:
        parser.error("--api-url needs at least one URL")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs every request at INFO
    log.info(
        "settings: scenario=%s rounds=%d riders=%d drivers=%s repeat=%s spread=%g m watch=%g s sequential=%s keep_last_round=%s "
        "cleanup_only=%s seed=%s api=%s osrm=%s psql=%s/%s label=%s",
        args.scenario, args.rounds, args.riders, args.drivers if args.scenario != "fleet" else "n/a",
        args.repeat if args.scenario == "riders" else "n/a", args.spread_m, args.watch_seconds, args.sequential,
        args.keep_last_round, args.cleanup_only, args.seed, ",".join(args.api_urls), args.osrm_url, args.psql_user, args.psql_db,
        args.label or "-",
    )

    ctx = {
        "args": args, "rng": random.Random(args.seed), "sem": asyncio.Semaphore(SETUP_CONCURRENCY), "tokens": {},
        "driver_emails": [], "rounds": [], "checks": 0, "latencies": {}, "per_rider": {}, "fleet": {"rides": 0, "no_driver": 0},
        "finished": False, "unsettled": False, "offers": {"got": 0, "expected": 0, "lost_rounds": 0},
    }
    failure = None
    interrupted = False
    # keepalive_expiry is below uvicorn's 5 s keep-alive timeout, as in simulator.py.
    limits = httpx.Limits(max_connections=MAX_CONNECTIONS, max_keepalive_connections=MAX_CONNECTIONS, keepalive_expiry=2)
    async with httpx.AsyncClient(base_url=args.api_urls[0], timeout=API_TIMEOUT_S, limits=limits) as api_client, \
            httpx.AsyncClient(base_url=args.osrm_url, timeout=OSRM_TIMEOUT_S) as osrm_client:
        ctx["api"] = api_client
        ctx["osrm"] = osrm_client
        try:
            if not args.cleanup_only:
                for url in args.api_urls:
                    try:
                        health = await api_client.get(f"{url}/health")
                    except httpx.HTTPError as error:
                        raise RuntimeError(f"cannot reach the API at {url}: {error!r}")
                    if health.status_code != 200:
                        raise RuntimeError(f"the API at {url} is not healthy ({health.status_code}): {health.text}")
                try:
                    response = await api(api_client, "POST", "/auth/login", None, {"email": args.admin_email, "password": args.admin_password})
                except httpx.HTTPError as error:
                    raise RuntimeError(f"cannot reach the API at {args.api_urls[0]}: {error!r}")
                if response.status_code != 200:
                    raise RuntimeError(f"admin login failed ({response.status_code}): {response.text}")
                if response.json()["user"]["role"] != "admin":
                    raise RuntimeError(f"{args.admin_email} is not an admin")
                ctx["admin_token"] = response.json()["access_token"]
                response = await api(api_client, "GET", "/places/map-config", ctx["admin_token"])
                city = response.json()
                ctx["bounds"] = (city["south"], city["west"], city["north"], city["east"])
                ctx["center"] = (
                    city["center_lat"] if args.center_lat is None else args.center_lat,
                    city["center_lng"] if args.center_lng is None else args.center_lng,
                )
                south, west, north, east = ctx["bounds"]
                if not (south <= ctx["center"][0] <= north and west <= ctx["center"][1] <= east):
                    raise RuntimeError(f"the center {ctx['center']} is outside the city bounds (south, west, north, east) = {ctx['bounds']}")
                log.info("city %s, center %.5f,%.5f", city["city_name"], *ctx["center"])

                scenario = {"drivers": scenario_drivers, "riders": scenario_riders, "fleet": scenario_fleet}[args.scenario]
                task = asyncio.create_task(scenario(ctx))
                try:
                    asyncio.get_running_loop().add_signal_handler(signal.SIGINT, task.cancel)
                except NotImplementedError:
                    pass  # Windows: asyncio.run cancels the main task on Ctrl+C instead, handled below (not run on Windows)
                try:
                    await task
                    ctx["finished"] = True
                except asyncio.CancelledError:
                    interrupted = True
                    ctx["unsettled"] = True
                    log.warning("interrupted: cleaning up")
        except (RuntimeError, httpx.HTTPError) as error:
            failure = str(error) if isinstance(error, RuntimeError) else f"HTTP problem: {error!r}"
            ctx["unsettled"] = True
        finally:
            if args.cleanup_only or interrupted or not (args.keep_last_round and ctx["finished"]):
                try:
                    await cleanup(ctx)
                except (RuntimeError, httpx.HTTPError) as error:
                    failure = failure or (str(error) if isinstance(error, RuntimeError) else f"HTTP problem: {error!r}")

    if args.cleanup_only:
        if failure:
            log.error("%s", failure)
            return 2
        log.info("cleanup finished")
        return 0

    if failure:
        problem = ("INVARIANTS NOT CHECKED: " if failure.startswith("psql") else "STRESS TEST FAILED: ") + failure
        if not ctx["rounds"]:  # failed before the first round: there is nothing to summarize
            log.error("%s", problem)
            return 2

    rounds_run = len(ctx["rounds"])
    log.info("summary%s: scenario=%s, %d of %d rounds run%s, %d invariant checks, %d API process(es)",
             f" [{args.label}]" if args.label else "", args.scenario, rounds_run, args.rounds,
             " (interrupted)" if interrupted else "", ctx["checks"], len(args.api_urls))
    violated = []
    for name, (code, who, what, _) in INVARIANTS.items():
        rounds_with = [seen[name] for seen in ctx["rounds"] if seen.get(name)]
        worst = max((max(offenders.values()) for offenders in rounds_with), default=0)
        worst_text = f", worst case {worst} {what} on one {who[:-1]}" if what else f", worst case {worst} {who}"
        log.info("  %s %s: violated in %d of %d rounds%s", code, name, len(rounds_with), rounds_run, worst_text if rounds_with else "")
        if rounds_with:
            violated.append(f"{code} in {len(rounds_with)} of {rounds_run} rounds")
    if args.scenario == "drivers":
        # Fewer offers than min(riders, drivers) means a free driver was skipped or a rider got NO_DRIVER_FOUND for nothing:
        # a defect of its own, reported here but not an invariant.
        log.info("  lost matches: %d of %d rounds; offers %d of %d expected", ctx["offers"]["lost_rounds"], rounds_run,
                 ctx["offers"]["got"], ctx["offers"]["expected"])
    log.info("  HTTP answers: %s", ", ".join(f"{code} x{count}" for code, count in sorted(status_counts.items(), key=str)))
    for key, times in ctx["latencies"].items():
        if times:
            log.info("  latency of /%s requests: median %.0f ms, max %.0f ms (%d requests)", key, statistics.median(times), max(times), len(times))
    if ctx["per_rider"]:
        log.info("  per rider (201/409/other over all rounds): %s", " ".join(
            f"{n:03d}:{c[201]}/{c[409]}/{sum(c.values()) - c[201] - c[409]}" for n, c in sorted(ctx["per_rider"].items())))
    if args.scenario == "fleet" and ctx["fleet"]["rides"]:
        log.info("  fleet rides: %d, of them NO_DRIVER_FOUND: %d", ctx["fleet"]["rides"], ctx["fleet"]["no_driver"])

    if failure:
        log.error("%s", problem)
        return 2
    if ctx["checks"] == 0:
        log.error("INVARIANTS NOT CHECKED: no snapshot was taken")
        return 2
    if violated:
        log.info("RACE REPRODUCED: %s", ", ".join(violated))
        return 1
    if args.scenario == "fleet" and ctx["fleet"]["rides"] and ctx["fleet"]["rides"] == ctx["fleet"]["no_driver"]:
        log.error("INCONCLUSIVE: every ride ended NO_DRIVER_FOUND (fleet not running or out of range), the run proves nothing")
        return 2
    log.info("No invariant violations observed in %d rounds", rounds_run)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(2)
