"""Fills the load database once and stores it as the template database that every run starts from (python loadtest/run.py seed).

Migrations, the admins (create_admin.py), SEED_RIDERS riders through the registration API (the odd ones funded through the admin
wallet adjustment), the simulator's driver accounts (the unchanged simulator, run once until its setup is finished), then the
template. Idempotent: whatever exists is left alone, and the template is rebuilt only when the content changed."""
import os
import subprocess
import sys

import requests

import common

FLEET_PASSWORD_ENV = {"SIM_ADMIN_EMAIL": common.ADMIN_EMAIL.format(n=0)}
FINGERPRINT_SQL = """
SELECT count(*), coalesce(md5(string_agg(id || ':' || email || ':' || role::text, ',' ORDER BY id)), '') FROM users;
SELECT count(*), coalesce(sum(balance), 0) FROM wallets;
SELECT count(*) FROM drivers;
SELECT count(*) FROM vehicles;
"""


def seed(riders: int, drivers: int, admins: int) -> None:
    common.compose("up", "-d", "--wait", "postgres-load", "redis-load")
    print("migrating the load database ...")
    common.compose("run", "--rm", "--no-deps", "backend-load", "alembic", "upgrade", "head")
    common.psql("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
    before = common.psql(FINGERPRINT_SQL)

    for n in range(admins + 1):  # the harness admin and the AdminViewers
        result = common.compose("run", "--rm", "--no-deps", "backend-load", "python", "create_admin.py",
                                "--email", common.ADMIN_EMAIL.format(n=n), "--name", f"Load Admin {n}", "--password", common.password())
        print(result.stdout.strip())

    common.compose("up", "-d", "backend-load")
    common.wait_for_health()
    api = requests.Session()
    login = api.post(f"{common.API_URL}/auth/login", json={"email": common.ADMIN_EMAIL.format(n=0), "password": common.password()})
    login.raise_for_status()
    admin_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    existing = {line for line in common.psql("SELECT email FROM users WHERE email LIKE 'rider%@loadtest.example.com'")}
    missing = [n for n in range(1, riders + 1) if common.RIDER_EMAIL.format(n=n) not in existing]
    print(f"riders: {len(existing)} exist, registering {len(missing)} (the server hashes each password, about 0.1 s)")
    for done, n in enumerate(missing, 1):
        response = api.post(f"{common.API_URL}/auth/register", json={
            "name": f"Load Rider {n}", "email": common.RIDER_EMAIL.format(n=n), "password": common.password(), "role": "rider"})
        response.raise_for_status()
        if done % 200 == 0:
            print(f"  registered {done}")

    ids = {email: int(user_id) for user_id, email in (line.split("|") for line in common.psql(
        "SELECT id, email FROM users WHERE email LIKE 'rider%@loadtest.example.com'"))}
    funded = {int(line) for line in common.psql("SELECT user_id FROM wallets")}
    to_fund = [n for n in range(1, riders + 1, 2) if ids[common.RIDER_EMAIL.format(n=n)] not in funded]
    print(f"wallets: {len(funded)} exist, funding {len(to_fund)} riders with {common.FUNDED_PAISE} paise")
    for n in to_fund:
        response = api.post(f"{common.API_URL}/admin/wallets/{ids[common.RIDER_EMAIL.format(n=n)]}/adjust", headers={
            **admin_headers, "Idempotency-Key": f"load-funding-{n}"}, json={"amount": common.FUNDED_PAISE, "note": "load test funding"})
        response.raise_for_status()

    have = int(common.psql("SELECT count(*) FROM users WHERE email LIKE 'sim-driver-%@sim.example.com'")[0])
    if have < drivers:
        print(f"fleet: {have} driver accounts exist, running the simulator until its setup of {drivers} drivers is finished ...")
        simulator = subprocess.Popen(
            [str(common.SIM_PYTHON), "simulator/simulator.py", "--drivers", str(drivers), "--api-url", common.API_URL,
             "--seed", "1", "--speed-kmh", "90"],
            cwd=common.ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env={**os.environ, **FLEET_PASSWORD_ENV, "SIM_ADMIN_PASSWORD": common.password()},
        )
        for line in simulator.stdout:
            if f"setup of {drivers} drivers finished" in line:
                break
        simulator.terminate()
        simulator.wait(timeout=60)
    else:
        print(f"fleet: {have} driver accounts exist")

    after = common.psql(FINGERPRINT_SQL)
    template_exists = common.psql(f"SELECT 1 FROM pg_database WHERE datname = '{common.TEMPLATE_DB}'") != []
    if after == before and template_exists:
        print("nothing changed, the template stays as it is")
        return
    # A database that has a connection cannot be copied.
    common.compose("stop", "backend-load")
    common.psql(f"DROP DATABASE IF EXISTS {common.TEMPLATE_DB} WITH (FORCE)", db="postgres")
    common.psql(f"CREATE DATABASE {common.TEMPLATE_DB} TEMPLATE {common.LOAD_DB}", db="postgres")
    print(f"template {common.TEMPLATE_DB} created: users, wallets, drivers, vehicles = {after}")


if __name__ == "__main__":
    sys.exit("run it through: python loadtest/run.py seed")
