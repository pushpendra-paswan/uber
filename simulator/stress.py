"""Concurrency stress test (M4.1). Runs on the host and fires many requests at the same instant, then looks in
the database for broken invariants (simulator/invariants.sql) and prints how often each one broke.

It checks the fix for the double-booking bug (M4.2): exit code 0 is the expected result. Before M4.2 it reproduced
the bug, and exit code 1 (an invariant broke) was expected. It changes nothing in the backend.

Everything that changes state goes through the public API. The only direct database access is read-only SELECTs
through `docker compose exec db psql`. It never touches Redis, Nominatim, or OpenStreetMap (only our API and OSRM).

Usage: python simulator/stress.py --scenario drivers --admin-email ... --admin-password ...
Scenarios: drivers (I1 and I2), riders (I3), fleet (against the running simulator.py fleet), chaos (M4.3: riders and
drivers acting at random for a while, then a settle period; looks for any invariant violation and for stuck rides; since
M5.3 riders pay from a funded wallet or in cash and an admin credits wallets with replayed requests), payments (M5.3: top-up
requests repeated at once, signed webhooks delivered many times at once, bad signatures refused; needs the backend to use the
local fake Stripe, simulator/fake_stripe.py).
Stop simulator.py for the drivers, riders and chaos scenarios: its drivers would join the test.
Exit codes: 0 no violation (expected for every scenario), 1 a violation, a stuck ride, or a 5xx, 2 the check could not run.
--api-url takes several comma-separated URLs (two backend processes): requests are spread over them round-robin.
"""
import argparse
import asyncio
import collections
import hashlib
import hmac
import json
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
CHAOS_PLACES = 60  # pickup / drop-off pairs, snapped once before the agents start
CHAOS_STATUS_INTERVAL_S = 10
FINISHED_STATUSES = ("COMPLETED", "CANCELLED", "NO_DRIVER_FOUND")

# section name in invariants.sql -> (short name, what an offender is, what the count counts, columns of a row)
INVARIANTS = {
    "driver_pending_offers": ("I1", "drivers", "offers", ("driver_id", "count", "offer_ids", "ride_ids")),
    "driver_active_rides": ("I2", "drivers", "rides", ("driver_id", "count", "ride_ids", "statuses")),
    "rider_active_rides": ("I3", "riders", "rides", ("rider_id", "count", "ride_ids", "statuses")),
    "stuck_requested": ("I4", "rides", None, ("ride_id", "created_at")),
    "overdue_pending_offers": ("I5", "offers", None, ("offer_id", "ride_id", "driver_id", "expires_at")),
    "orphan_pending_offers": ("I6", "offers", None, ("offer_id", "ride_id", "driver_id", "ride_status")),
    "assigned_without_accepted_offer": ("I7", "rides", None, ("ride_id", "driver_id", "status")),
    "completed_without_fare": ("I8", "rides", None, ("ride_id", "final_fare", "actual_distance_m", "actual_duration_s", "kind")),
    "cancelled_without_settlement": ("I9", "rides", None, ("ride_id", "final_fare", "kind", "fee")),
    "fare_on_unsettled_ride": ("I10", "rides", None, ("ride_id", "status", "final_fare", "kind")),
    "fare_over_cap": ("I11", "rides", None, ("ride_id", "final_fare", "fare_estimate")),
    "surge_settlement_mismatch": (
        "I12", "rides", None, ("ride_id", "ride_surge", "breakdown_surge", "normal_fare", "surge_amount", "computed_fare")
    ),
    "wallet_balance_mismatch": ("I13", "wallets", None, ("user_id", "balance", "entries_sum", "last_balance_after")),
    "ledger_running_balance_mismatch": ("I14", "entries", None, ("entry_id", "user_id", "balance_after", "running_sum")),
    "negative_wallet": ("I15", "wallets", None, ("user_id", "balance")),
    "ride_payment_mismatch": (
        "I16", "rides", None, ("ride_id", "final_fare", "payment_count", "payment_amount", "payment_method", "ride_method")
    ),
    "wallet_charge_mismatch": ("I17", "rides", None, ("ride_id", "payment_amount", "entry_count", "entry_amount")),
    "topup_credit_mismatch": ("I18", "top-ups", None, ("topup_id", "status", "amount", "entry_count", "entry_amount")),
}
FUND_TO_PAISE = 500000  # the chaos riders' wallets are filled up to 5,000 rupees
ADJUST_MAX_PAISE = 1000000  # the most one adjustment may move
WEBHOOK_COPIES = 10  # payments scenario: one event delivered this many times at once
OTHER_EVENT_COPIES = 3  # ... plus this many deliveries of a different event for the same session

log = logging.getLogger("stress")
status_counts = collections.Counter()  # every HTTP answer of the whole run, by status code
bodies_5xx = []  # (method, path, status, body) of the first server errors, to print in the summary


async def api(
    client: httpx.AsyncClient, method: str, path: str, token: str | None = None, body: dict | bytes | None = None,
    gate: asyncio.Event | None = None, headers: dict | None = None,
) -> httpx.Response:
    """One HTTP call with a bearer token and optional extra headers. A bytes body is sent as it is (a webhook); anything
    else as JSON. With a gate it waits for the event first (see burst)."""
    if gate is not None:
        await gate.wait()
    headers = {**({"Authorization": f"Bearer {token}"} if token else {}), **(headers or {})}
    if isinstance(body, bytes):
        response = await client.request(method, path, content=body, headers=headers)
    else:
        response = await client.request(method, path, json=body, headers=headers)
    status_counts[response.status_code] += 1
    if response.status_code >= 500 and len(bodies_5xx) < 5:
        bodies_5xx.append((method, path, response.status_code, response.text[:300]))
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
        ctx["user_ids"][email] = response.json()["user"]["id"]

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
    """Sends (token, method, path, body[, headers]) requests so that they all start in the same instant.

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
        for index, (token, method, path, body, *extra) in enumerate(requests):
            try:
                outcomes.append(await api(client, method, urls[index % len(urls)] + path, token, body, headers=extra[0] if extra else None))
            except httpx.HTTPError as error:
                outcomes.append(error)
        seconds = time.monotonic() - started
    else:
        await asyncio.gather(*[client.get(urls[index % len(urls)] + "/health") for index in range(len(requests))])
        gate = asyncio.Event()
        tasks = [
            asyncio.create_task(api(client, method, urls[index % len(urls)] + path, token, body, gate, extra[0] if extra else None))
            for index, (token, method, path, body, *extra) in enumerate(requests)
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
    'I1 2 drivers (max 3 offers), I3 1 riders' for everything seen in the round so far ('I1-I18 ok' when nothing)."""
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
        raise RuntimeError(f"psql output did not contain all eighteen invariants (got {sorted(found)})")
    ctx["checks"] += 1

    parts = []
    for name, (code, who, what, columns) in INVARIANTS.items():
        offenders = seen.setdefault(name, {})
        for row in found[name]:
            if row[0] not in offenders:  # the full row is logged once per round
                log.warning("%s %s offender: %s", code, name, " ".join(f"{column}={value}" for column, value in zip(columns, row)))
            offenders[row[0]] = max(offenders.get(row[0], 0), int(row[1]) if what else 1)
        if what and offenders:
            parts.append(f"{code} {len(offenders)} {who} (max {max(offenders.values())} {what})")
        elif offenders:
            parts.append(f"{code} {len(offenders)} {who}")
    return ", ".join(parts) or "I1-I18 ok"


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


async def chaos_rider(ctx: dict, email: str, rng: random.Random, counters: dict, deadline: float) -> None:
    """One rider until the deadline: requests a ride (sometimes the same request twice at once), cancels a quarter of
    them after a few seconds, follows the rest to the end (cancelling after 90 s), pauses, and requests again."""
    client, token, urls = ctx["api"], ctx["tokens"][email], ctx["args"].api_urls
    ride_id = cancel_at = None
    cancelled = False
    while time.monotonic() < deadline:
        try:
            if ride_id is None:
                pickup, dropoff = rng.choice(ctx["places"])
                body = {
                    "pickup_lat": pickup[0], "pickup_lng": pickup[1], "pickup_address": "Stress pickup",
                    "dropoff_lat": dropoff[0], "dropoff_lng": dropoff[1], "dropoff_address": "Stress drop-off",
                }
                duplicate = rng.random() < 0.10
                if rng.random() < 0.20:
                    # Like the rider page: ask for the price first and accept exactly that multiplier.
                    quoted = await api(client, "POST", rng.choice(urls) + "/rides/estimate", token, {
                        key: value for key, value in body.items() if not key.endswith("address")
                    })
                    if quoted.status_code == 200:
                        body["accepted_surge_percent"] = quoted.json()["surge_percent"]
                # Half of the rides are paid from the wallet. The wallets are funded for it, so a 402 should not happen.
                body["payment_method"] = "wallet" if rng.random() < 0.50 else "cash"
                counters["rides_requested"] += 1
                counters["wallet_rides_requested"] += body["payment_method"] == "wallet"
                answers = await asyncio.gather(
                    *[api(client, "POST", rng.choice(urls) + "/rides", token, body) for _ in range(2 if duplicate else 1)]
                )
                counters["wallet_402"] += sum(1 for answer in answers if answer.status_code == 402)
                # The price went up between the quote and the request: normal, not a failure.
                price_up = [answer for answer in answers if answer.status_code == 409 and str(answer.json().get("detail", "")).startswith("Prices have increased")]
                counters["surge_409"] += len(price_up)
                if duplicate:
                    counters["duplicate_requests"] += 1
                    counters["duplicates_refused"] += sum(1 for answer in answers if answer.status_code == 409) - len(price_up)
                created = [answer for answer in answers if answer.status_code == 201]
                if created:
                    ride_id, cancelled = created[0].json()["id"], False
                    cancel_at = time.monotonic() + (rng.uniform(0, 10) if rng.random() < 0.25 else 90)
                    continue
                # Refused, for example because an earlier request whose answer was lost made a ride: follow that one.
                active = await api(client, "GET", rng.choice(urls) + "/rides/active", token)
                if active.status_code == 200:
                    ride_id, cancelled, cancel_at = active.json()["id"], False, time.monotonic() + 90
                else:
                    await asyncio.sleep(rng.uniform(0, 3))
            else:
                await asyncio.sleep(2 if cancelled else max(0, min(2, cancel_at - time.monotonic())))
                if not cancelled and time.monotonic() >= cancel_at:
                    cancelled = True
                    answer = await api(client, "POST", rng.choice(urls) + f"/rides/{ride_id}/cancel", token)
                    counters["rider_cancels"] += 1
                    counters["rider_cancels_ok"] += answer.status_code == 200
                active = await api(client, "GET", rng.choice(urls) + "/rides/active", token)
                if active.status_code == 404:
                    final = await api(client, "GET", rng.choice(urls) + f"/rides/{ride_id}", token)
                    counters[f"ride_{final.json().get('status')}"] += final.status_code == 200
                    ride_id = None
                    await asyncio.sleep(rng.uniform(0, 3))
        except httpx.HTTPError:
            counters["transport_errors"] += 1
            await asyncio.sleep(1)


async def chaos_driver(ctx: dict, email: str, location: tuple, rng: random.Random, counters: dict, deadline: float) -> None:
    """One driver until the deadline, a tick every second: pings its location every 3 s, takes the next step of its ride
    after a short pause (sometimes cancelling instead), and answers an offer at random: accept 50, reject 15, ignore 15,
    go offline for 2 to 5 s 10, accept twice at the same moment 10 (percent)."""
    client, token, urls, otp = ctx["api"], ctx["tokens"][email], ctx["args"].api_urls, ctx["args"].otp
    here = {"lat": location[0], "lng": location[1]}
    online, offline_until, last_ping = True, 0.0, time.monotonic()
    plan = None  # (ride id, status, when to act): the step to take on the active ride
    ignored = set()
    while time.monotonic() < deadline:
        await asyncio.sleep(1)
        now = time.monotonic()
        try:
            if not online:
                if now >= offline_until:
                    online = (await api(client, "POST", rng.choice(urls) + "/drivers/me/online", token, here)).status_code == 200
                    last_ping = now
                continue
            if now - last_ping >= 3:
                last_ping = now
                ping = await api(client, "POST", rng.choice(urls) + "/drivers/me/location", token, here)
                if ping.status_code == 409:  # the presence key is gone (for example Redis was restarted): come back
                    await api(client, "POST", rng.choice(urls) + "/drivers/me/online", token, here)

            active = await api(client, "GET", rng.choice(urls) + "/rides/active", token)
            if active.status_code == 200:
                ride = active.json()
                if plan is None or plan[:2] != (ride["id"], ride["status"]):
                    pause = rng.uniform(1, 4) if ride["status"] == "IN_PROGRESS" else rng.uniform(0, 3)
                    plan = (ride["id"], ride["status"], now + pause)
                elif now >= plan[2]:
                    step = f"{rng.choice(urls)}/rides/{ride['id']}"
                    if ride["status"] != "IN_PROGRESS" and rng.random() < 0.10:
                        await api(client, "POST", step + "/cancel", token)
                        counters["driver_cancels"] += 1
                    elif ride["status"] == "DRIVER_ASSIGNED":
                        await api(client, "POST", step + "/arrive", token)
                    elif ride["status"] == "DRIVER_ARRIVED":
                        await api(client, "POST", step + "/start", token, {"otp": otp})
                    elif ride["status"] == "IN_PROGRESS":
                        counters["trips_completed"] += (await api(client, "POST", step + "/complete", token)).status_code == 200
                continue

            plan = None
            offer = await api(client, "GET", rng.choice(urls) + "/drivers/me/offer", token)
            if offer.status_code != 200 or offer.json()["id"] in ignored:
                continue
            offer_id = offer.json()["id"]
            roll = rng.random() * 100
            if roll < 50:
                answers = [await api(client, "POST", rng.choice(urls) + f"/offers/{offer_id}/accept", token)]
            elif roll < 65:
                counters["rejects"] += (await api(client, "POST", rng.choice(urls) + f"/offers/{offer_id}/reject", token)).status_code == 204
                answers = []
            elif roll < 80:
                ignored.add(offer_id)
                counters["ignores"] += 1
                answers = []
            elif roll < 90:
                if (await api(client, "POST", rng.choice(urls) + "/drivers/me/offline", token)).status_code == 200:
                    counters["offline_events"] += 1
                    online, offline_until = False, now + rng.uniform(2, 5)
                answers = []
            else:
                answers = list(await asyncio.gather(
                    api(client, "POST", rng.choice(urls) + f"/offers/{offer_id}/accept", token),
                    api(client, "POST", rng.choice(urls) + f"/offers/{offer_id}/accept", token),
                ))
                counters["double_accepts"] += 1
                counters["double_accepts_one_200"] += sorted(answer.status_code for answer in answers).count(200) == 1
            counters["accepts_won"] += sum(1 for answer in answers if answer.status_code == 200)
            counters["accepts_refused"] += sum(1 for answer in answers if answer.status_code != 200)
        except httpx.HTTPError:
            counters["transport_errors"] += 1

    # The deadline: a trip still in progress is finished, because a driver in the middle of a trip cannot be cleaned up.
    try:
        active = await api(client, "GET", urls[0] + "/rides/active", token)
        if active.status_code == 200 and active.json()["status"] == "IN_PROGRESS":
            counters["trips_completed"] += (await api(client, "POST", urls[0] + f"/rides/{active.json()['id']}/complete", token)).status_code == 200
    except httpx.HTTPError:
        counters["transport_errors"] += 1


async def chaos_admin(ctx: dict, rng: random.Random, counters: dict, deadline: float) -> None:
    """The admin until the deadline, a tick every 2 s: credits a random stress rider 10 to 100 rupees with a fresh
    Idempotency-Key, and 30 percent of the time sends the SAME request twice at once. Every successful answer for one key must
    carry the same entry id (adjust_replay_mismatch counts the keys where they did not)."""
    client, urls = ctx["api"], ctx["args"].api_urls
    user_ids = [ctx["user_ids"][RIDER_EMAIL.format(n=n)] for n in range(1, ctx["args"].riders + 1)]
    while time.monotonic() < deadline:
        await asyncio.sleep(2)
        user_id = rng.choice(user_ids)
        body = {"amount": rng.randrange(10, 101) * 100, "note": "stress credit"}
        headers = {"Idempotency-Key": f"stress-credit-{time.time_ns()}"}
        twice = rng.random() < 0.30
        try:
            answers = await asyncio.gather(
                *[api(client, "POST", rng.choice(urls) + f"/admin/wallets/{user_id}/adjust", ctx["admin_token"], body, headers=headers)
                  for _ in range(2 if twice else 1)]
            )
        except httpx.HTTPError:
            counters["transport_errors"] += 1
            continue
        counters["adjustments_sent"] += 1
        counters["adjustments_created"] += any(answer.status_code == 201 for answer in answers)
        if twice:
            counters["adjust_replays_sent"] += 1
            if len({answer.json()["id"] for answer in answers if answer.status_code in (200, 201)}) > 1:
                counters["adjust_replay_mismatch"] += 1


async def scenario_chaos(ctx: dict) -> None:
    """Riders and drivers act at random for --chaos-seconds, snapshotting I1 to I18 every 2 s. Then the agents stop and
    the system gets --settle-seconds to finish: no REQUESTED ride and no PENDING offer may be left (those are STUCK).
    Rides legitimately left assigned, arrived, or in progress are not stuck."""
    args = ctx["args"]
    ctx["driver_emails"] = [DRIVER_EMAIL.format(n=n) for n in range(1, args.drivers + 1)]
    started = time.monotonic()
    await asyncio.gather(
        *[setup_accounts(ctx, "driver", n) for n in range(1, args.drivers + 1)],
        *[setup_accounts(ctx, "rider", n) for n in range(1, args.riders + 1)],
    )
    log.info("setup of %d drivers and %d riders took %.1f s", args.drivers, args.riders, time.monotonic() - started)
    await cleanup(ctx)

    seen = {}
    ctx["rounds"].append(seen)
    locations = {}
    pings = []
    for email in ctx["driver_emails"]:
        locations[email] = await snap(ctx, 0, DRIVER_DISC_M, on_road=False)
        body = {"lat": locations[email][0], "lng": locations[email][1]}
        pings.append(api(ctx["api"], "POST", "/drivers/me/online", ctx["tokens"][email], body))
    for answer in await asyncio.gather(*pings):
        if answer.status_code != 200:
            raise RuntimeError(f"a stress driver could not go online: {answer.status_code} {answer.text}")
    ctx["places"] = [(await snap(ctx, 0, args.spread_m), await snap(ctx, DROPOFF_MIN_M, DROPOFF_MAX_M)) for _ in range(CHAOS_PLACES)]

    # Fill every rider's wallet up to FUND_TO_PAISE through the admin API (never more than one adjustment's limit at a time),
    # then remember the starting balances for the check at the end.
    unix_time = int(time.time())
    for n in range(1, args.riders + 1):
        email = RIDER_EMAIL.format(n=n)
        balance = (await api(ctx["api"], "GET", "/wallet", ctx["tokens"][email])).json()["balance"]
        while balance < FUND_TO_PAISE:
            step = min(FUND_TO_PAISE - balance, ADJUST_MAX_PAISE)
            answer = await api(
                ctx["api"], "POST", f"/admin/wallets/{ctx['user_ids'][email]}/adjust", ctx["admin_token"],
                {"amount": step, "note": "stress funding"}, headers={"Idempotency-Key": f"stress-fund-{n}-{unix_time}-{balance}"},
            )
            if answer.status_code not in (200, 201):
                raise RuntimeError(f"funding the wallet of {email} answered {answer.status_code} {answer.text}")
            balance += step
        ctx["starting"][ctx["user_ids"][email]] = (await api(ctx["api"], "GET", "/wallet", ctx["tokens"][email])).json()["balance"]

    since = (await sql(ctx, "SELECT now()"))[0]
    mine = f"r.created_at >= '{since}' AND r.rider_id IN (SELECT id FROM users WHERE email LIKE '{RIDER_LIKE}')"
    counters = ctx["counters"]
    seed = args.seed if args.seed is not None else random.randrange(1_000_000)
    log.info("chaos: %d riders and %d drivers for %d s (then up to %d s to settle), agent seed %d",
             args.riders, args.drivers, args.chaos_seconds, args.settle_seconds, seed)
    start = time.monotonic()
    deadline = start + args.chaos_seconds
    agents = [
        asyncio.create_task(chaos_rider(ctx, RIDER_EMAIL.format(n=n), random.Random(f"{seed}-rider-{n}"), counters, deadline))
        for n in range(1, args.riders + 1)
    ] + [
        asyncio.create_task(chaos_driver(ctx, email, locations[email], random.Random(f"{seed}-driver-{n}"), counters, deadline))
        for n, email in enumerate(ctx["driver_emails"], 1)
    ] + [asyncio.create_task(chaos_admin(ctx, random.Random(f"{seed}-admin"), counters, deadline))]
    settle_deadline = None
    next_status = start + CHAOS_STATUS_INTERVAL_S
    try:
        while True:
            await check_invariants(ctx, seen)
            rows = await sql(
                ctx,
                f"SELECT 'ride', r.status, count(*) FROM rides r WHERE {mine} GROUP BY r.status UNION ALL "
                f"SELECT 'offer', o.status, count(*) FROM ride_offers o JOIN rides r ON r.id = o.ride_id WHERE {mine} GROUP BY o.status",
            )
            ctx["chaos"] = {"ride": {}, "offer": {}}
            for row in rows:
                kind, status, number = row.split("|")
                ctx["chaos"][kind][status] = int(number)
            now = time.monotonic()
            if now >= next_status:
                next_status += CHAOS_STATUS_INTERVAL_S
                sent = sum(status_counts.values()) + counters["transport_errors"]
                server_errors = sum(number for code, number in status_counts.items() if isinstance(code, int) and code >= 500)
                log.info("chaos %3.0f s: %d requests, rides %s, offers %s, 409 x%d, 5xx %d, violations %d", now - start, sent,
                         dict(sorted(ctx["chaos"]["ride"].items())), dict(sorted(ctx["chaos"]["offer"].items())),
                         status_counts[409], server_errors, sum(len(offenders) for offenders in seen.values()))
            if all(agent.done() for agent in agents):
                settle_deadline = settle_deadline or now + args.settle_seconds
                if ctx["chaos"]["ride"].get("REQUESTED", 0) + ctx["chaos"]["offer"].get("PENDING", 0) == 0 or now >= settle_deadline:
                    break
            await asyncio.sleep(1)
    finally:
        for agent in agents:
            agent.cancel()  # only does something when the run is interrupted
    for agent in agents:
        if agent.exception() is not None:
            raise RuntimeError(f"a chaos agent crashed: {agent.exception()!r}")

    # What is left after the settle period.
    rows = await sql(
        ctx,
        "SELECT r.id, r.status, o.id, o.driver_id, o.status, o.expires_at FROM rides r LEFT JOIN ride_offers o ON o.ride_id = r.id "
        f"WHERE {mine} AND (r.status = 'REQUESTED' OR EXISTS (SELECT 1 FROM ride_offers p WHERE p.ride_id = r.id AND p.status = 'PENDING')) "
        "ORDER BY r.id, o.id",
    )
    for row in rows:
        ride_id, ride_status, offer_id, driver_id, offer_status, expires_at = row.split("|")
        ctx["stuck"].setdefault(ride_id, {"status": ride_status, "offers": []})
        ctx["stuck"][ride_id]["offers"].append(f"offer {offer_id} driver {driver_id} {offer_status} expires {expires_at}")
    for ride_id, stuck in ctx["stuck"].items():
        log.error("STUCK ride %s (%s): %s", ride_id, stuck["status"], "; ".join(stuck["offers"]) or "no offers")
    ctx["left_active"] = sum(ctx["chaos"]["ride"].get(status, 0) for status in ("DRIVER_ASSIGNED", "DRIVER_ARRIVED", "IN_PROGRESS"))
    other = await sql(
        ctx,
        f"SELECT count(*) FROM ride_offers o JOIN rides r ON r.id = o.ride_id JOIN drivers d ON d.id = o.driver_id JOIN users u ON u.id = d.user_id "
        f"WHERE {mine} AND u.email NOT LIKE '{DRIVER_LIKE}'",
    )
    if other[0] != "0":
        log.warning("%s offers went to drivers that are not stress drivers: other drivers are online, the run is diluted", other[0])
    # The money of the run, read-only: settled trips and the cancellation fees that were charged.
    money = await sql(
        ctx,
        "SELECT count(*) FILTER (WHERE r.status = 'COMPLETED'), COALESCE(sum(r.final_fare) FILTER (WHERE r.status = 'COMPLETED'), 0), "
        "count(*) FILTER (WHERE r.status = 'CANCELLED' AND r.final_fare > 0), "
        "COALESCE(sum(r.final_fare) FILTER (WHERE r.status = 'CANCELLED' AND r.final_fare > 0), 0) "
        f"FROM rides r WHERE {mine}",
    )
    ctx["money"] = [int(value) for value in money[0].split("|")]
    # The wallet money of the run, read-only: rides by payment method with what was charged, the adjustments that were
    # credited, and for every stress rider final balance == starting balance + credits - wallet charges.
    rows = await sql(
        ctx,
        "SELECT r.payment_method, r.status, count(*), COALESCE(sum(p.amount), 0) FROM rides r LEFT JOIN payments p ON p.ride_id = r.id "
        f"WHERE {mine} AND r.status IN ('COMPLETED', 'CANCELLED') GROUP BY 1, 2 ORDER BY 1, 2",
    )
    ctx["paid_rides"] = [row.split("|") for row in rows]
    rows = await sql(
        ctx,
        "SELECT count(*), COALESCE(sum(amount), 0) FROM wallet_entries WHERE kind = 'ADJUSTMENT' "
        f"AND created_at >= '{since}' AND user_id IN (SELECT id FROM users WHERE email LIKE '{RIDER_LIKE}')",
    )
    ctx["adjustments"] = [int(value) for value in rows[0].split("|")]
    rows = await sql(
        ctx,
        "SELECT u.id, COALESCE(w.balance, 0), "
        "COALESCE((SELECT sum(e.amount) FROM wallet_entries e WHERE e.user_id = u.id AND e.kind = 'ADJUSTMENT' "
        f"AND e.created_at >= '{since}'), 0), "
        "COALESCE((SELECT sum(p.amount) FROM payments p JOIN rides q ON q.id = p.ride_id WHERE q.rider_id = u.id "
        f"AND p.method = 'wallet' AND q.created_at >= '{since}'), 0) "
        f"FROM users u LEFT JOIN wallets w ON w.user_id = u.id WHERE u.email LIKE '{RIDER_LIKE}' ORDER BY u.id",
    )
    ctx["wallet_mismatches"] = []
    for row in rows:
        user_id, final, credits, charges = (int(value) for value in row.split("|"))
        if user_id in ctx["starting"] and final != ctx["starting"][user_id] + credits - charges:
            ctx["wallet_mismatches"].append(f"user {user_id}: final {final} != start {ctx['starting'][user_id]} + credits {credits} - charges {charges}")
    # Surge, read-only: rides that were quoted above 1.0x, the highest multiplier, and the surge part of the settled trips
    # (only trips whose breakdown has the key).
    surge = await sql(
        ctx,
        "SELECT count(*) FILTER (WHERE r.surge_percent > 100), COALESCE(max(r.surge_percent), 100), "
        "COALESCE(sum((r.fare_breakdown->>'surge_amount')::int) FILTER (WHERE r.status = 'COMPLETED' "
        "AND (r.fare_breakdown->>'kind') = 'trip' AND r.fare_breakdown ? 'surge_percent'), 0) "
        f"FROM rides r WHERE {mine}",
    )
    ctx["surge"] = [int(value) for value in surge[0].split("|")]


async def scenario_payments(ctx: dict) -> None:
    """Top-ups and webhooks under repetition (M5.3). Each round, for all riders at once: the same top-up request five times
    (one top-up, one Stripe session), then the signed paid event delivered ten times with one event id plus three times with
    another (exactly one credit), then three badly signed events (refused, nothing changes). I1 to I18 are checked after each
    round. Needs the backend to talk to simulator/fake_stripe.py: it creates a Checkout Session per rider per round."""
    args = ctx["args"]
    ctx["driver_emails"] = []
    await asyncio.gather(*[setup_accounts(ctx, "rider", n) for n in range(1, args.riders + 1)])
    log.warning("payments: this creates one Checkout Session per rider per round in whichever Stripe the backend uses; "
                "run it against the local fake (simulator/fake_stripe.py), not a real Stripe account")
    secret = args.webhook_secret.encode()
    counters, problems = ctx["counters"], ctx["payment_problems"]
    webhook_headers = {"Content-Type": "application/json"}
    riders = {n: ctx["user_ids"][RIDER_EMAIL.format(n=n)] for n in range(1, args.riders + 1)}

    for round_number in range(1, args.rounds + 1):
        seen = {}
        ctx["rounds"].append(seen)
        before = {int(row.split("|")[0]): int(row.split("|")[1]) for row in await sql(
            ctx, f"SELECT user_id, balance FROM wallets WHERE user_id IN ({', '.join(str(i) for i in riders.values())})")}

        # 1. The same request five times at once per rider: one top-up each.
        stamp = time.time_ns()
        keys = {n: f"stress-topup-{n}-{stamp}" for n in riders}
        amounts = {n: ctx["rng"].randrange(100, 501) * 100 for n in riders}  # 100 to 500 rupees
        fired = await burst(ctx, [
            (ctx["tokens"][RIDER_EMAIL.format(n=n)], "POST", "/wallet/topups", {"amount": amounts[n]}, {"Idempotency-Key": keys[n]})
            for n in riders for _ in range(5)
        ])
        topup_ids = {}
        for index, n in enumerate(riders):
            answers = fired["results"][index * 5 : (index + 1) * 5]
            if any(a["status"] == 503 and "not configured" in str(a["body"]) for a in answers):
                raise RuntimeError("Stripe is not configured on the backend (POST /wallet/topups answered 503): "
                                   "start simulator/fake_stripe.py and set the three STRIPE_* values in .env")
            for position, answer in enumerate(answers):
                if answer["status"] == 409:  # another copy of the request is still talking to Stripe: once more, a second later
                    await asyncio.sleep(1)
                    retry = await api(ctx["api"], "POST", "/wallet/topups", ctx["tokens"][RIDER_EMAIL.format(n=n)], {"amount": amounts[n]},
                                      headers={"Idempotency-Key": keys[n]})
                    counters["topup_409_retried"] += 1
                    answers[position] = {"status": retry.status_code, "body": retry.json()}
            ids = {a["body"]["id"] for a in answers if a["status"] in (200, 201)}
            counters["topup_requests"] += len(answers)
            counters["topup_requests_ok"] += sum(1 for a in answers if a["status"] in (200, 201))
            if len(ids) != 1 or any(a["status"] not in (200, 201) for a in answers):
                problems.append(f"round {round_number} rider {n}: top-up answers {[a['status'] for a in answers]}, ids {sorted(ids)}")
            if ids:
                topup_ids[n] = ids.pop()
        rows = await sql(ctx, f"SELECT user_id, count(*) FROM wallet_topups WHERE idempotency_key IN ({', '.join(repr(k) for k in keys.values())}) GROUP BY user_id")
        if sorted(rows) != sorted(f"{user_id}|1" for user_id in riders.values()):
            problems.append(f"round {round_number}: not exactly one top-up row per rider and key ({len(rows)} riders have rows)")
        counters["topups_created"] += len(topup_ids)
        if not topup_ids:
            raise RuntimeError(f"round {round_number}: no top-up could be created ({problems[-1] if problems else 'no answers'})")

        # 2. Pay every top-up: the same signed event ten times at once plus another event three times, all riders together.
        sessions = {int(r.split("|")[0]): r.split("|")[1] for r in await sql(
            ctx, f"SELECT id, stripe_session_id FROM wallet_topups WHERE id IN ({', '.join(str(i) for i in topup_ids.values())})")}
        now = int(time.time())
        deliveries = []
        for n, topup_id in topup_ids.items():
            session = {"id": sessions[topup_id], "object": "checkout.session", "payment_status": "paid", "status": "complete",
                       "amount_total": amounts[n], "currency": "inr", "client_reference_id": str(topup_id),
                       "payment_intent": f"pi_stress_{topup_id}_{stamp}"}
            for letter, copies in (("a", WEBHOOK_COPIES), ("b", OTHER_EVENT_COPIES)):
                body = json.dumps({"id": f"evt_stress_{stamp}_{topup_id}_{letter}", "object": "event", "type": "checkout.session.completed",
                                   "created": now, "data": {"object": session}}).encode()
                header = f"t={now},v1=" + hmac.new(secret, f"{now}.".encode() + body, hashlib.sha256).hexdigest()
                deliveries.extend([(None, "POST", "/webhooks/stripe", body, {**webhook_headers, "Stripe-Signature": header})] * copies)
        fired = await burst(ctx, deliveries)
        per_topup = WEBHOOK_COPIES + OTHER_EVENT_COPIES
        for index, (n, topup_id) in enumerate(topup_ids.items()):
            answers = fired["results"][index * per_topup : (index + 1) * per_topup]
            labels = [(a["body"] or {}).get("status") if a["status"] == 200 else a["status"] for a in answers]
            counters.update(f"webhook_{label}" for label in labels)
            counters["webhook_deliveries"] += len(labels)
            # One processed. The first delivery of the OTHER event id is ignored (new event, top-up already credited); every
            # other delivery is a duplicate of one of the two event ids.
            if labels.count("processed") != 1 or labels.count("ignored") != 1 or labels.count("duplicate") != WEBHOOK_COPIES + OTHER_EVENT_COPIES - 2:
                problems.append(f"round {round_number} top-up {topup_id}: webhook answers {sorted(map(str, labels))}")
        rows = await sql(
            ctx,
            "SELECT t.id, t.user_id, t.status, t.amount, count(e.id), COALESCE(sum(e.amount), 0) FROM wallet_topups t "
            f"LEFT JOIN wallet_entries e ON e.topup_id = t.id WHERE t.id IN ({', '.join(str(i) for i in topup_ids.values())}) GROUP BY t.id",
        )
        credited = collections.Counter()
        for row in rows:
            topup_id, user_id, status, amount, entry_count, entry_amount = row.split("|")
            if (status, int(entry_count), int(entry_amount)) != ("SUCCEEDED", 1, int(amount)):
                problems.append(f"round {round_number} top-up {topup_id}: {status}, {entry_count} entries totalling {entry_amount} for {amount}")
            credited[int(user_id)] += int(amount)
        after = {int(row.split("|")[0]): int(row.split("|")[1]) for row in await sql(
            ctx, f"SELECT user_id, balance FROM wallets WHERE user_id IN ({', '.join(str(i) for i in riders.values())})")}
        for user_id, amount in credited.items():
            if after.get(user_id, 0) - before.get(user_id, 0) != amount:
                problems.append(f"round {round_number} user {user_id}: balance rose by {after.get(user_id, 0) - before.get(user_id, 0)}, top-ups {amount}")

        # 3. Three events that must be refused, and nothing may change because of them.
        snapshot = "SELECT (SELECT count(*) FROM stripe_events), (SELECT count(*) FROM wallet_entries), (SELECT COALESCE(sum(balance), 0) FROM wallets), (SELECT count(*) FROM wallet_topups WHERE status = 'SUCCEEDED')"
        state_before = await sql(ctx, snapshot)
        bad = []
        any_topup = next(iter(topup_ids.items()))
        for name, signing_secret, timestamp, tamper in (("wrong secret", b"whsec_not_the_secret", now, False), ("tampered body", secret, now, True), ("old timestamp", secret, now - 600, False)):
            body = json.dumps({"id": f"evt_stress_{stamp}_bad_{len(bad)}", "object": "event", "type": "checkout.session.completed", "created": now,
                               "data": {"object": {"id": sessions[any_topup[1]], "payment_status": "paid", "amount_total": amounts[any_topup[0]],
                                                   "currency": "inr", "client_reference_id": str(any_topup[1])}}}).encode()
            header = f"t={timestamp},v1=" + hmac.new(signing_secret, f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
            bad.append((None, "POST", "/webhooks/stripe", body + b" " if tamper else body, {**webhook_headers, "Stripe-Signature": header}))
        fired = await burst(ctx, bad)
        counters["bad_events_sent"] += len(bad)
        counters["bad_events_refused"] += sum(1 for r in fired["results"] if r["status"] == 400)
        if [r["status"] for r in fired["results"]] != [400, 400, 400]:
            problems.append(f"round {round_number}: bad events answered {[r['status'] for r in fired['results']]}, not 400 each")
        if await sql(ctx, snapshot) != state_before:
            problems.append(f"round {round_number}: the database changed after bad events")

        invariants = await check_invariants(ctx, seen)
        log.info("payments round %d/%d: %d top-ups; webhooks %s; %s", round_number, args.rounds, len(topup_ids),
                 {label[8:]: count for label, count in sorted(counters.items()) if label.startswith("webhook_") and label != "webhook_deliveries"}, invariants)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Concurrency stress test: fires simultaneous requests and checks database invariants")
    parser.add_argument("--scenario", choices=["drivers", "riders", "fleet", "chaos", "payments"], default="drivers")
    parser.add_argument("--admin-email", default=os.environ.get("SIM_ADMIN_EMAIL"), help="or env SIM_ADMIN_EMAIL")
    parser.add_argument("--admin-password", default=os.environ.get("SIM_ADMIN_PASSWORD"), help="or env SIM_ADMIN_PASSWORD")
    parser.add_argument("--webhook-secret", default=os.environ.get("STRIPE_WEBHOOK_SECRET"), help="payments scenario: the backend's webhook secret, or env STRIPE_WEBHOOK_SECRET")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000", help="one URL, or several separated by commas (two backend processes)")
    parser.add_argument("--osrm-url", default="http://127.0.0.1:5000")
    parser.add_argument("--center-lat", type=float, help="default: the city center")
    parser.add_argument("--center-lng", type=float, help="default: the city center")
    parser.add_argument("--rounds", type=int, default=5, help="1 to 50")
    parser.add_argument("--riders", type=int, help="2 to 100; default 20 (30 for chaos)")
    parser.add_argument("--drivers", type=int, help="1 to 100; default 3 (drivers), 2 x riders (riders), 10 (chaos), unused (fleet)")
    parser.add_argument("--repeat", type=int, default=2, help="riders scenario: the same request this many times at once, 2 to 5")
    parser.add_argument("--spread-m", type=float, default=150, help="pickups are this far from the center at most")
    parser.add_argument("--watch-seconds", type=float, default=30, help="fleet scenario: how long to watch after the burst")
    parser.add_argument("--chaos-seconds", type=int, default=60, help="chaos scenario: how long the agents act, 10 to 600")
    parser.add_argument("--settle-seconds", type=int, default=45, help="chaos scenario: how long the system gets to finish after the agents stop, 30 to 300")
    parser.add_argument("--tolerate-5xx", action="store_true", help="chaos scenario: count 5xx answers and report them, but they do not decide the exit code")
    parser.add_argument("--otp", default="1234", help="chaos scenario: the trip code sent to start a trip (the backend's fake code is 1234)")
    parser.add_argument("--seed", type=int, help="makes the random points (and the chaos agents' choices) repeatable")
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
    if args.scenario == "payments" and not args.webhook_secret:
        parser.error("the payments scenario signs webhooks: --webhook-secret (or env STRIPE_WEBHOOK_SECRET) is needed")
    if not 1 <= args.rounds <= 50:
        parser.error("--rounds must be between 1 and 50")
    if args.riders is None:
        args.riders = 30 if args.scenario == "chaos" else 20
    if not 2 <= args.riders <= 100:
        parser.error("--riders must be between 2 and 100")
    if args.drivers is None:
        args.drivers = {"drivers": 3, "riders": min(2 * args.riders, 100), "fleet": 0, "chaos": 10, "payments": 0}[args.scenario]
    if not 10 <= args.chaos_seconds <= 600:
        parser.error("--chaos-seconds must be between 10 and 600")
    if not 30 <= args.settle_seconds <= 300:
        parser.error("--settle-seconds must be between 30 and 300")
    if args.scenario not in ("fleet", "payments") and not 1 <= args.drivers <= 100:
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
        "cleanup_only=%s seed=%s chaos=%s api=%s osrm=%s psql=%s/%s label=%s",
        args.scenario, args.rounds, args.riders, args.drivers if args.scenario not in ("fleet", "payments") else "n/a",
        args.repeat if args.scenario == "riders" else "n/a", args.spread_m, args.watch_seconds, args.sequential,
        args.keep_last_round, args.cleanup_only, args.seed,
        f"{args.chaos_seconds} s + settle {args.settle_seconds} s, tolerate_5xx={args.tolerate_5xx}, otp={args.otp}" if args.scenario == "chaos" else "n/a",
        ",".join(args.api_urls), args.osrm_url, args.psql_user, args.psql_db, args.label or "-",
    )

    ctx = {
        "args": args, "rng": random.Random(args.seed), "sem": asyncio.Semaphore(SETUP_CONCURRENCY), "tokens": {}, "user_ids": {}, "starting": {},
        "driver_emails": [], "rounds": [], "checks": 0, "latencies": {}, "per_rider": {}, "fleet": {"rides": 0, "no_driver": 0},
        "finished": False, "unsettled": False, "offers": {"got": 0, "expected": 0, "lost_rounds": 0},
        "counters": collections.Counter(), "payment_problems": [], "stuck": {}, "chaos": {"ride": {}, "offer": {}}, "left_active": 0,
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

                scenario = {
                    "drivers": scenario_drivers, "riders": scenario_riders, "fleet": scenario_fleet, "chaos": scenario_chaos,
                    "payments": scenario_payments,
                }[args.scenario]
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
        problem = ("INVARIANTS NOT CHECKED: " if failure.startswith(("psql", "Stripe is not configured")) else "STRESS TEST FAILED: ") + failure
        if not ctx["rounds"]:  # failed before the first round: there is nothing to summarize
            log.error("%s", problem)
            return 2

    rounds_run = len(ctx["rounds"])
    log.info("summary%s: scenario=%s, %d of %d rounds run%s, %d invariant checks, %d API process(es)",
             f" [{args.label}]" if args.label else "", args.scenario, rounds_run, 1 if args.scenario == "chaos" else args.rounds,
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

    if args.scenario == "chaos" and not failure:
        counters = ctx["counters"]
        server_errors = sum(number for code, number in status_counts.items() if isinstance(code, int) and code >= 500)
        log.info("  actions: %d rides requested; %d cancels by riders (%d accepted) and %d by drivers; %d duplicate ride requests "
                 "(%d answers were 409); %d trips completed", counters["rides_requested"], counters["rider_cancels"],
                 counters["rider_cancels_ok"], counters["driver_cancels"], counters["duplicate_requests"],
                 counters["duplicates_refused"], counters["trips_completed"])
        log.info("  %d requests were refused because the price went up after the quote (surge_409, normal)", counters["surge_409"])
        log.info("  offers answered: %d accepts won, %d accepts refused, %d double accepts (%d returned exactly one 200), %d rejects, "
                 "%d ignored, %d drivers went offline", counters["accepts_won"], counters["accepts_refused"], counters["double_accepts"],
                 counters["double_accepts_one_200"], counters["rejects"], counters["ignores"], counters["offline_events"])
        log.info("  riders saw their rides end as: %s", dict(sorted((k[5:], v) for k, v in counters.items() if k.startswith("ride_"))))
        log.info("  final rides by status: %s", dict(sorted(ctx["chaos"]["ride"].items())))
        completed, fares, fees, fee_total = ctx["money"]
        log.info("  money: %d completed rides, final fares %d paise in all; %d cancelled rides with a fee, fees %d paise in all",
                 completed, fares, fees, fee_total)
        wallet_done = {(row[0], row[1]): (int(row[2]), int(row[3])) for row in ctx["paid_rides"]}
        log.info("  payments: wallet rides %d completed (charged %d paise) and %d cancelled (fees %d paise); cash rides %d completed "
                 "(%d paise) and %d cancelled (fees %d paise)",
                 *wallet_done.get(("wallet", "COMPLETED"), (0, 0)), *wallet_done.get(("wallet", "CANCELLED"), (0, 0)),
                 *wallet_done.get(("cash", "COMPLETED"), (0, 0)), *wallet_done.get(("cash", "CANCELLED"), (0, 0)))
        log.info("  admin: %d adjustments sent, %d created (%d paise credited in all), %d sent twice at once, %d replay mismatches; "
                 "%d wallet rides requested, %d answered 402 (should be 0)",
                 counters["adjustments_sent"], counters["adjustments_created"], ctx["adjustments"][1], counters["adjust_replays_sent"],
                 counters["adjust_replay_mismatch"], counters["wallet_rides_requested"], counters["wallet_402"])
        log.info("  wallets: final balance == starting balance + credits - wallet charges for %d of %d stress riders",
                 len(ctx["starting"]) - len(ctx["wallet_mismatches"]), len(ctx["starting"]))
        for mismatch in ctx["wallet_mismatches"]:
            log.error("  WALLET MISMATCH %s", mismatch)
        surged, highest, surge_total = ctx["surge"]
        log.info("  surge: %d rides requested above 1.0x, highest multiplier %d percent, surge part of the completed trips %d paise in all",
                 surged, highest, surge_total)
        if not surged:
            log.warning("  no ride had surge in this run: it proves nothing about the surge path")
        log.info("  final offers by status: %s", dict(sorted(ctx["chaos"]["offer"].items())))
        log.info("  5xx answers: %d, connection errors: %d%s", server_errors, counters["transport_errors"],
                 " (tolerated)" if args.tolerate_5xx else "")
        for method, path, status, body in bodies_5xx:
            log.info("    first 5xx: %s %s -> %d %s", method, path, status, body)
        log.info("  violations per invariant (offenders seen): %s", ", ".join(
            f"{code} {len(ctx['rounds'][0].get(name, {}))}" for name, (code, *_rest) in INVARIANTS.items()))
        log.info("  stuck rides after %d s of settling: %d; rides legitimately left active (assigned, arrived, in progress): %d",
                 args.settle_seconds, len(ctx["stuck"]), ctx["left_active"])
        for what, label in (("wallet_rides_requested", "wallet ride request"), ("adjust_replays_sent", "adjustment sent twice at once"),
                            ("duplicate_requests", "duplicate ride request"), ("rider_cancels_ok", "rider cancel"), ("driver_cancels", "driver cancel"),
                            ("trips_completed", "completed trip"), ("offline_events", "driver going offline"), ("double_accepts", "double accept"),
                            ("rejects", "reject"), ("ignores", "ignored offer")):
            if not counters[what]:
                log.warning("  no %s happened in this run: it proves nothing about that case", label)
        problems = [f"{code} in {len(ctx['rounds'][0][name])} offenders" for name, (code, *_rest) in INVARIANTS.items() if ctx["rounds"][0].get(name)]
        if ctx["stuck"]:
            problems.append(f"{len(ctx['stuck'])} stuck rides")
        if counters["adjust_replay_mismatch"]:
            problems.append(f"{counters['adjust_replay_mismatch']} adjustment replays with different entry ids")
        if ctx["wallet_mismatches"]:
            problems.append(f"{len(ctx['wallet_mismatches'])} wallets whose final balance does not add up")
        if server_errors and not args.tolerate_5xx:
            problems.append(f"{server_errors} server errors (5xx)")
        if counters["transport_errors"] and not args.tolerate_5xx:
            problems.append(f"{counters['transport_errors']} connection errors")
        if ctx["checks"] == 0:
            log.error("INVARIANTS NOT CHECKED: no snapshot was taken")
            return 2
        if problems:
            log.info("CHAOS FOUND PROBLEMS: %s", ", ".join(problems))
            return 1
        log.info("CHAOS CLEAN")
        return 0

    if args.scenario == "payments" and not failure:
        counters = ctx["counters"]
        by_answer = {label[8:]: count for label, count in sorted(counters.items()) if label.startswith("webhook_") and label != "webhook_deliveries"}
        log.info("  top-ups: %d created (%d of %d create requests succeeded, %d retried after a 409 in progress)",
                 counters["topups_created"], counters["topup_requests_ok"], counters["topup_requests"], counters["topup_409_retried"])
        log.info("  webhook deliveries: %d, by answer: %s (one processed per top-up expected: %d top-ups)",
                 counters["webhook_deliveries"], by_answer, counters["topups_created"])
        log.info("  bad events: %d sent, %d refused with 400", counters["bad_events_sent"], counters["bad_events_refused"])
        for problem in ctx["payment_problems"][:20]:
            log.error("  PAYMENT PROBLEM %s", problem)
        if ctx["checks"] == 0:
            log.error("INVARIANTS NOT CHECKED: no snapshot was taken")
            return 2
        if violated or ctx["payment_problems"]:
            log.info("PAYMENTS FOUND PROBLEMS: %s", ", ".join(violated + [f"{len(ctx['payment_problems'])} payment problems"] * bool(ctx["payment_problems"])))
            return 1
        log.info("PAYMENTS CLEAN")
        return 0

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
