"""The simulated users of the load test. Each one sends the requests of the real page (frontend/rider/rider.js,
frontend/admin/admin.js) on the same 3 second cadence: the page-open requests first, then the per-tick set of its state.
Not simulated: rider WebSockets (the page keeps one open, but its polling set is the one with the socket open), geocoding
(places/search and places/reverse call the public Nominatim) and the Stripe top-ups. The driver side is the simulator fleet.

Request names are "<METHOD> <route template>", the template the server uses for its metrics, so Locust's counts and the
server's http_requests_total line up. SETUP and TEARDOWN requests are left out of every SLO, OUTCOME events are not requests."""
import json
import os
import random
import time
from datetime import datetime, timezone
from itertools import count
from pathlib import Path

import gevent
from locust import FastHttpUser, constant, events, task
from locust.user.task import LOCUST_STATE_STOPPING

import common
from shape import StepShape  # noqa: F401  (Locust takes the load shape from the locustfile's namespace)

TICK_S = 3.0  # POLL_MS of the pages
SEED = os.environ.get("LOADTEST_SEED", "1")
REQUESTER_RATIO = float(os.environ.get("LOADTEST_REQUESTER_RATIO", "0.3"))
SEED_RIDERS = int(os.environ.get("LOADTEST_SEED_RIDERS", "1000"))
PROCESSES = int(os.environ.get("LOADTEST_PROCESSES", "1"))
UTC_OFFSET_MINUTES = 330  # what the admin page sends for the city's time zone (-new Date().getTimezoneOffset())

FINISHED = ("COMPLETED", "CANCELLED", "NO_DRIVER_FOUND")
CODE_STATUSES = ("DRIVER_ASSIGNED", "DRIVER_ARRIVED")
CANCEL_SHARE = 0.05  # of the requesters cancel while waiting
RATE_SHARE = 0.30  # of the completed trips are rated

request_log = None


@events.init.add_listener
def open_request_log(environment, **kwargs):
    global request_log
    run_dir = Path(os.environ["LOADTEST_RUN_DIR"])
    run_dir.mkdir(parents=True, exist_ok=True)
    request_log = open(run_dir / f"requests-{os.getpid()}.jsonl", "a", buffering=1)  # one file per Locust process


@events.request.add_listener
def write_request(request_type, name, response_time, response_length, exception, response=None, context=None, start_time=None, **kwargs):
    status = getattr(response, "status_code", 0) or 0
    request_log.write(json.dumps({
        "ts": round(start_time if start_time is not None else time.time() - response_time / 1000, 3), "type": request_type,
        "name": name, "ms": round(response_time, 1), "status": status, "success": exception is None}) + "\n")


@events.quitting.add_listener
def close_request_log(environment, **kwargs):
    request_log.close()


rider_counter = count()  # the riders of one process, RiderSession and HistoryBrowser together


class PageUser(FastHttpUser):
    abstract = True
    wait_time = constant(TICK_S)  # only used if a task ever raises: Locust restarts it after this, not at once
    connection_timeout = 10.0
    network_timeout = 20.0  # a request without an answer after this is an error

    def call(self, name: str, path: str | None = None, body: dict | None = None, expect: tuple = (200,), ok_detail: str = "",
             method: str | None = None):
        """One request. `name` is "<METHOD> <route template>" (or "TEARDOWN ..." with a method); the path defaults to the
        template. Returns (status, parsed body or None). `expect` are the statuses the page expects: any other status is
        reported as a failure, unless it is a 409 whose text starts with ok_detail."""
        method = method or name.split(" ", 1)[0]
        with self.client.request(method, path or name.split(" ", 1)[1], name=name, json=body, headers=self.headers, catch_response=True) as response:
            status = response.status_code
            data = None
            if status in (200, 201) or 400 <= status < 500:
                try:
                    data = response.json()
                except ValueError:
                    pass
            if status in expect or (ok_detail and data and str(data.get("detail", "")).startswith(ok_detail)):
                response.success()
            else:
                response.failure(f"status {status}")
        return status, data

    def login(self, email: str) -> None:
        """Logs in and keeps the token. A login that fails or times out (the server is busy hashing passwords) is tried again after
        a tick, like a person pressing the button again; only a refused password stops the test, because the database was not seeded."""
        while True:
            with self.client.post("/auth/login", name="SETUP login", json={"email": email, "password": common.password()}, catch_response=True) as response:
                if response.status_code == 200:
                    response.success()
                    self.headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
                    return
                response.failure(f"login status {response.status_code}")
                if response.status_code == 401:
                    self.environment.runner.quit()
                    raise RuntimeError(f"login of {email} was refused: the load database was not seeded?")
            gevent.sleep(TICK_S)

    def fire(self, name: str, response_time_ms: float = 0) -> None:
        """A ride outcome or a wait time: goes to the same request log, but is not a request."""
        self.environment.events.request.fire(request_type="OUTCOME", name=name, response_time=response_time_ms, response_length=0,
                                             exception=None, context={})

    def wait_for_next_tick(self, next_tick: float) -> float:
        """Sleeps until the next tick boundary and returns the one after it. A tick that took longer than the cadence is not
        caught up, like the page, which skips a poll while the last one still runs."""
        next_tick += TICK_S
        delay = next_tick - time.monotonic()
        if delay > 0:
            gevent.sleep(delay)
            return next_tick
        return time.monotonic()


class RiderSession(PageUser):
    weight = 9
    requester_role = True  # HistoryBrowser is a rider who never requests

    def on_start(self):
        self.ride = None  # first, so on_stop works for a user that is stopped while it is still logging in
        # Unique across the Locust processes: process i takes the accounts i, i + N, i + 2N, ...
        self.number = 1 + self.environment.runner.worker_index + PROCESSES * next(rider_counter) if PROCESSES > 1 else 1 + next(rider_counter)
        if self.number > SEED_RIDERS:
            self.environment.runner.quit()
            raise RuntimeError(f"rider account {self.number} is needed but only {SEED_RIDERS} are seeded: run seed with more riders")
        self.rng = random.Random(f"{SEED}:rider:{self.number}")
        self.pairs = random.Random(f"{SEED}:pairs:{self.number}")  # its own stream: the trips do not depend on how earlier ones ended
        self.requester = self.requester_role and self.rng.random() < REQUESTER_RATIO
        self.funded = self.number % 2 == 1
        self.login(common.RIDER_EMAIL.format(n=self.number))

        self.loaded = set()  # what the page asks for once: "config", "places", and per ride "route", "driver", "otp", "receipt", "ratings"
        self.phase = "idle"  # idle, estimated, waiting, finished
        self.idle_until = time.monotonic() + self.rng.uniform(5, 20)
        self.requested_at = None
        self.assigned_seen = False
        self.cancel_after = None
        self.finished_ticks = 0
        self.price = None
        self.trip = None  # (pickup_lat, pickup_lng, dropoff_lat, dropoff_lng) of the ride being planned
        self.click_at = None

    @task
    def session(self):
        next_tick = time.monotonic()
        while self._state != LOCUST_STATE_STOPPING:
            self.refresh()
            self.act()
            next_tick = self.wait_for_next_tick(next_tick)

    def on_stop(self):
        if self.ride is not None and self.ride["status"] not in FINISHED:
            self.call("TEARDOWN cancel", f"/rides/{self.ride['id']}/cancel", expect=(200, 409), method="POST")

    def refresh(self) -> None:
        """What refresh() of rider.js asks for: the wallet, the active ride and what belongs to it. The first call is the page open."""
        if "config" not in self.loaded:
            self.loaded.add("config")
            self.call("GET /places/map-config")
        reads = [gevent.spawn(self.call, "GET /wallet"),  # the page sends these three at once
                 gevent.spawn(self.call, "GET /wallet/entries", "/wallet/entries?limit=10"),
                 gevent.spawn(self.call, "GET /wallet/topups", "/wallet/topups?limit=5")]
        gevent.joinall(reads)

        # A failed request leaves what the page knows as it was, like the page does.
        status, active = self.call("GET /rides/active", expect=(200, 404))
        if status == 200:
            self.ride = active
        elif status == 404 and self.ride is not None and self.ride["status"] not in FINISHED:
            # /rides/active is 404 once a ride ends, so the page fetches the remembered ride once to show how it ended.
            status, remembered = self.call("GET /rides/{ride_id}", f"/rides/{self.ride['id']}")
            if status == 200:
                self.ride = remembered
        if self.ride is None:
            if "ratings:none" not in self.loaded:
                self.loaded.add("ratings:none")
                self.call("GET /ratings/me")
        else:
            ride = self.ride
            ride_id = ride["id"]
            self.call("GET /rides/{ride_id}/events", f"/rides/{ride_id}/events")
            if f"route:{ride_id}" not in self.loaded:  # the path of the ride is not stored, so the page asks the estimate for it
                self.loaded.add(f"route:{ride_id}")
                self.call("POST /rides/estimate", body=self.estimate_body(ride))
            if ride["driver_id"] is not None and f"driver:{ride_id}" not in self.loaded:
                self.loaded.add(f"driver:{ride_id}")
                self.call("GET /rides/{ride_id}/driver", f"/rides/{ride_id}/driver")
            if ride["status"] in CODE_STATUSES and f"otp:{ride_id}" not in self.loaded:
                self.loaded.add(f"otp:{ride_id}")
                self.call("GET /rides/{ride_id}/otp", f"/rides/{ride_id}/otp")
            charged = ride["status"] in ("COMPLETED", "CANCELLED") and (ride["final_fare"] or 0) > 0 and ride["fare_breakdown"]["kind"] != "legacy"
            if charged and f"receipt:{ride_id}" not in self.loaded:
                self.loaded.add(f"receipt:{ride_id}")
                self.call("GET /rides/{ride_id}/receipt", f"/rides/{ride_id}/receipt")
            if ride["status"] in FINISHED and f"ratings:{ride_id}" not in self.loaded:
                self.loaded.add(f"ratings:{ride_id}")
                self.call("GET /ratings/me")
                if ride["status"] == "COMPLETED":
                    self.call("GET /rides/{ride_id}/rating", f"/rides/{ride_id}/rating")
        if "places" not in self.loaded:
            self.loaded.add("places")
            self.call("GET /saved-places")

    def estimate_body(self, ride: dict) -> dict:
        return {"pickup_lat": ride["pickup_lat"], "pickup_lng": ride["pickup_lng"],
                "dropoff_lat": ride["dropoff_lat"], "dropoff_lng": ride["dropoff_lng"]}

    def act(self) -> None:
        """What this rider does besides watching: a requester asks for a ride, waits for it, may cancel, and may rate it."""
        now = time.monotonic()
        ride = self.ride
        if self.phase == "idle" and self.requester and now >= self.idle_until:
            self.trip = self.pairs.choice(common.POINT_PAIRS)
            self.refresh_estimate()
            self.refresh()  # setting the points is an act() of the page, which refreshes afterwards
            self.phase = "estimated"
            self.click_at = now + self.rng.uniform(2, 5)  # the rider looks at the price and presses the button

        elif self.phase == "estimated" and now >= self.click_at:
            pickup_lat, pickup_lng, dropoff_lat, dropoff_lng = self.trip
            status, data = self.call("POST /rides", expect=(201,), ok_detail="Prices have increased", body={
                "pickup_address": "Load test pickup", "pickup_lat": pickup_lat, "pickup_lng": pickup_lng,
                "dropoff_address": "Load test drop-off", "dropoff_lat": dropoff_lat, "dropoff_lng": dropoff_lng,
                "accepted_surge_percent": self.price, "payment_method": "wallet" if self.funded else "cash"})
            if status == 201:
                self.ride = data
                self.requested_at = time.monotonic()
                self.assigned_seen = False
                self.cancel_after = self.rng.uniform(3, 10) if self.rng.random() < CANCEL_SHARE else None
                self.phase = "waiting"
            elif status == 409:  # the price rose: the page shows the new one and the rider presses the button again
                self.fire("price_changed")
                self.refresh_estimate()
                self.click_at = now + self.rng.uniform(2, 5)
            else:
                self.phase = "idle"
                self.idle_until = now + self.rng.uniform(5, 20)
            self.refresh()

        elif self.phase == "waiting":
            if ride["status"] in FINISHED:
                self.fire(ride["status"].lower())
                self.phase = "finished"
                self.finished_ticks = self.rng.randint(3, 8)  # how long the rider looks at the result
                if ride["status"] == "COMPLETED" and self.rng.random() < RATE_SHARE:
                    self.call("POST /rides/{ride_id}/rating", f"/rides/{ride['id']}/rating", expect=(201,),
                              body={"score": 5, "comment": "Smooth ride, thank you"})
                    self.loaded.discard(f"ratings:{ride['id']}")  # the page loads the ratings again after rating
                    self.refresh()
            else:
                if not self.assigned_seen and ride["status"] != "REQUESTED":
                    self.assigned_seen = True
                    self.fire("ride_assigned_wait", (now - self.requested_at) * 1000)
                if self.cancel_after is not None and ride["status"] == "REQUESTED" and now - self.requested_at >= self.cancel_after:
                    self.cancel_after = None
                    self.call("GET /rides/{ride_id}/cancellation-fee", f"/rides/{ride['id']}/cancellation-fee", expect=(200, 409))
                    self.call("POST /rides/{ride_id}/cancel", f"/rides/{ride['id']}/cancel", expect=(200, 409))
                    self.refresh()

        elif self.phase == "finished":
            self.finished_ticks -= 1
            if self.finished_ticks <= 0:  # the "New ride" button
                self.ride = None
                self.loaded.discard("ratings:none")  # the page asks for your own rating again when the finished ride is gone
                self.phase = "idle"
                self.idle_until = now + self.rng.uniform(5, 20)

    def refresh_estimate(self) -> None:
        pickup_lat, pickup_lng, dropoff_lat, dropoff_lng = self.trip
        _, estimate = self.call("POST /rides/estimate", body={
            "pickup_lat": pickup_lat, "pickup_lng": pickup_lng, "dropoff_lat": dropoff_lat, "dropoff_lng": dropoff_lng})
        self.price = estimate["surge_percent"] if estimate else None


class HistoryBrowser(RiderSession):
    """A rider who does not request rides but opens "My trips" now and then: the history, the saved places and one receipt."""
    weight = 1
    requester_role = False

    @task
    def session(self):
        next_tick = time.monotonic()
        next_visit = next_tick + self.rng.uniform(15, 40)
        while self._state != LOCUST_STATE_STOPPING:
            self.refresh()
            if time.monotonic() >= next_visit:
                next_visit = time.monotonic() + self.rng.uniform(15, 40)
                _, rows = self.call("GET /rides/history", "/rides/history?limit=20")
                self.call("GET /saved-places")
                receipts = [row for row in rows or [] if row["has_receipt"]]
                if receipts:
                    self.call("GET /rides/{ride_id}/receipt", f"/rides/{self.rng.choice(receipts)['id']}/receipt")
                self.refresh()  # the view buttons run act(), which refreshes afterwards
            next_tick = self.wait_for_next_tick(next_tick)


class AdminViewer(PageUser):
    abstract = True
    fixed_count = 1
    admin_number = 0

    def on_start(self):
        self.login(common.ADMIN_EMAIL.format(n=self.admin_number))


class AdminLiveViewer(AdminViewer):
    """The admin page on the Live tab: the live map every tick and the surge zones every third tick (admin.js loadTab)."""
    admin_number = 1

    @task
    def session(self):
        self.call("GET /places/map-config")
        self.call("GET /admin/live")
        self.call("GET /admin/surge")
        next_tick = time.monotonic()
        tick = 0
        while self._state != LOCUST_STATE_STOPPING:
            next_tick = self.wait_for_next_tick(next_tick)
            tick += 1
            self.call("GET /admin/live")
            if tick % 3 == 0:
                self.call("GET /admin/surge")


class AdminOverviewViewer(AdminViewer):
    """The admin page on the Overview tab: the stats of today, asked on every third tick."""
    admin_number = 2

    @task
    def session(self):
        next_tick = time.monotonic()
        tick = 0
        while self._state != LOCUST_STATE_STOPPING:
            if tick % 3 == 0:
                self.stats()
            next_tick = self.wait_for_next_tick(next_tick)
            tick += 1

    def stats(self) -> None:
        now = datetime.now(timezone.utc)
        midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        query = f"since={midnight.strftime('%Y-%m-%dT%H:%M:%S.000Z')}&until={now.strftime('%Y-%m-%dT%H:%M:%S.000Z')}&bucket=hour&utc_offset_minutes={UTC_OFFSET_MINUTES}"
        self.call("GET /admin/stats", f"/admin/stats?{query}")
