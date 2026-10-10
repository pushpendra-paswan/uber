"""Single endpoint capacity: PROBE_USERS users send ONE kind of request as fast as they can (no wait time). One task per core
endpoint, each selectable with a Locust tag (locust -f loadtest/probes.py --tags wallet). python loadtest/run.py probe runs all
of them one after the other with the fleet running. A user logs in once when it starts (SETUP login)."""
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path

from locust import FastHttpUser, events, tag, task

import common

ADMIN_ACCOUNTS = 3  # loadadmin0 to loadadmin2
request_log = None
accounts = count()


@events.init.add_listener
def open_request_log(environment, **kwargs):
    global request_log
    request_log = open(Path(os.environ["LOADTEST_RUN_DIR"]) / f"requests-{os.getpid()}.jsonl", "a", buffering=1)


@events.request.add_listener
def write_request(request_type, name, response_time, response_length, exception, response=None, context=None, start_time=None, **kwargs):
    request_log.write(json.dumps({
        "ts": round(start_time if start_time is not None else time.time() - response_time / 1000, 3), "type": request_type,
        "name": name, "ms": round(response_time, 1), "status": getattr(response, "status_code", 0) or 0, "success": exception is None}) + "\n")


@events.quitting.add_listener
def close_request_log(environment, **kwargs):
    request_log.close()


class Probe(FastHttpUser):
    connection_timeout = 10.0
    network_timeout = 30.0

    def on_start(self):
        number = next(accounts)
        admin = os.environ["LOADTEST_PROBE"].startswith("admin")
        self.email = common.ADMIN_EMAIL.format(n=number % ADMIN_ACCOUNTS) if admin else common.RIDER_EMAIL.format(n=1 + number)
        self.rng = random.Random(f"probe:{number}")
        with self.client.post("/auth/login", name="SETUP login", json={"email": self.email, "password": common.password()}, catch_response=True) as response:
            response.success()
            self.headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    def get(self, path: str, name: str, expect: tuple = (200,)) -> None:
        with self.client.get(path, name=f"GET {name}", headers=self.headers, catch_response=True) as response:
            if response.status_code in expect:
                response.success()
            else:
                response.failure(f"status {response.status_code}")

    @tag("health")
    @task
    def health(self):
        self.client.get("/health", name="GET /health")

    @tag("login")
    @task
    def login(self):
        self.client.post("/auth/login", name="POST /auth/login", json={"email": self.email, "password": common.password()})

    @tag("active_ride")
    @task
    def active_ride(self):
        self.get("/rides/active", "/rides/active", expect=(404,))

    @tag("wallet")
    @task
    def wallet(self):
        self.get("/wallet", "/wallet")

    @tag("saved_places")
    @task
    def saved_places(self):
        self.get("/saved-places", "/saved-places")

    @tag("history")
    @task
    def history(self):
        self.get("/rides/history?limit=20", "/rides/history")

    @tag("estimate")
    @task
    def estimate(self):
        pickup_lat, pickup_lng, dropoff_lat, dropoff_lng = self.rng.choice(common.POINT_PAIRS)
        self.client.post("/rides/estimate", name="POST /rides/estimate", headers=self.headers, json={
            "pickup_lat": pickup_lat, "pickup_lng": pickup_lng, "dropoff_lat": dropoff_lat, "dropoff_lng": dropoff_lng})

    @tag("admin_live")
    @task
    def admin_live(self):
        self.get("/admin/live", "/admin/live")

    @tag("admin_stats")
    @task
    def admin_stats(self):
        now = datetime.now(timezone.utc)  # the "week" period of the Overview tab
        since, until = (now - timedelta(days=6)).strftime("%Y-%m-%dT00:00:00.000Z"), now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.get(f"/admin/stats?since={since}&until={until}&bucket=day&utc_offset_minutes=330", "/admin/stats")

    @tag("admin_surge")
    @task
    def admin_surge(self):
        self.get("/admin/surge", "/admin/surge")
