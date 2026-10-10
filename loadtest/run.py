"""The load test harness: python loadtest/run.py {reset,seed,probe,run,report} (see README, "Load testing")."""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import psutil

import collect
import common
import report
import seed as seed_module

STEP_USERS = [10, 25, 50, 100, 150, 200, 300, 400]  # total Locust users per step, admins included
WARMUP_S = 30  # per step, left out of every number
HOLD_S = 90  # per step, the measured part
SAMPLE_S = 5  # how often /metrics and docker stats are read
FLEET_DRIVERS = 80  # simulated drivers, started by the harness
REQUESTER_RATIO = 0.3  # share of the riders that request rides
ADMIN_VIEWERS = 2
SEED_RIDERS = 1000
SETTLE_S = 30  # the fleet keeps running this long after the users stop
SETTLE_MAX_S = 900  # and then until no ride is active (rides in progress finish by themselves), but not longer than this
GAUGE_REFRESH_S = 5  # the backend reads database state for its gauges this often (METRICS_REFRESH_SECONDS)
SPAWN_RATE = 5  # users per second: every login hashes a password (about 0.1 s of the server's event loop)
PROBE_USERS = 20
PROBE_WARMUP_S = 5
PROBE_HOLD_S = 30
PROBES = ["health", "login", "active_ride", "wallet", "saved_places", "history", "estimate", "admin_live", "admin_stats", "admin_surge"]
LOCUST = common.ROOT / ".venv-load" / "bin" / "locust"


def reset() -> dict:
    """Stops backend-load, drops and recreates the load database from the template, flushes redis-load, starts backend-load
    fresh (so every counter begins at 0) and waits for /health. Returns the seconds of each part."""
    seconds = {}
    started = time.monotonic()
    common.compose("stop", "-t", "10", "backend-load")
    seconds["stop_backend"] = time.monotonic() - started
    mark = time.monotonic()
    common.psql(f"DROP DATABASE IF EXISTS {common.LOAD_DB} WITH (FORCE)", db="postgres")
    common.psql(f"CREATE DATABASE {common.LOAD_DB} TEMPLATE {common.TEMPLATE_DB}", db="postgres")
    seconds["recreate_database"] = time.monotonic() - mark
    mark = time.monotonic()
    common.compose("exec", "-T", "redis-load", "redis-cli", "FLUSHALL")
    seconds["flush_redis"] = time.monotonic() - mark
    mark = time.monotonic()
    common.compose("up", "-d", "backend-load")
    common.wait_for_health()
    seconds["start_backend"] = time.monotonic() - mark
    seconds["total"] = time.monotonic() - started
    return seconds


def start_fleet(drivers: int, run_dir: Path) -> subprocess.Popen:
    """The unchanged simulator against backend-load: waits until its setup is finished (all drivers online)."""
    log = run_dir / "simulator.log"
    process = subprocess.Popen(
        [str(common.SIM_PYTHON), "simulator/simulator.py", "--drivers", str(drivers), "--api-url", common.API_URL, "--seed", "1", "--speed-kmh", "90"],
        cwd=common.ROOT, stdout=open(log, "w"), stderr=subprocess.STDOUT,
        env={**os.environ, "SIM_ADMIN_EMAIL": common.ADMIN_EMAIL.format(n=0), "SIM_ADMIN_PASSWORD": common.password()},
    )
    started = time.monotonic()
    while f"setup of {drivers} drivers finished" not in log.read_text():
        if process.poll() is not None or time.monotonic() - started > 300:
            raise RuntimeError(f"the fleet did not finish its setup, see {log}")
        time.sleep(1)
    return process


def load_environment(args, run_dir: Path, processes: int) -> dict:
    return {
        **os.environ, "LOADTEST_RUN_DIR": str(run_dir), "LOADTEST_STEPS": ",".join(str(users) for users in args.steps),
        "LOADTEST_WARMUP_S": str(args.warmup), "LOADTEST_HOLD_S": str(args.hold), "LOADTEST_SPAWN_RATE": str(args.spawn_rate),
        "LOADTEST_SEED": str(args.seed), "LOADTEST_REQUESTER_RATIO": str(args.requester_ratio), "LOADTEST_SEED_RIDERS": str(args.seed_riders),
        "LOADTEST_PROCESSES": str(processes),
    }


def begin(args, run_id: str) -> tuple[Path, dict, dict]:
    """The same start of a stepped run and of a probe run: the run directory, a reset, the collectors, the fleet."""
    run_dir = common.RESULTS_DIR / run_id
    run_dir.mkdir(parents=True)
    print(f"run {run_id}: resetting ...")
    timings = reset()
    print("  reset", {name: round(value, 2) for name, value in timings.items()})
    stop = threading.Event()
    roots: dict = {}
    token = common.read_env()["METRICS_TOKEN"]
    collect.machine_facts(run_dir, {"reset_seconds": timings, "args": {key: str(value) for key, value in vars(args).items()}})
    common.psql("SELECT pg_stat_statements_reset()")
    session = {"run_dir": run_dir, "stop": stop, "roots": roots, "token": token, "timings": timings, "threads": [
        collect.launch(collect.scrape_forever, run_dir, stop, token, args.sample), collect.launch(collect.docker_stats_forever, run_dir, stop, args.sample),
        collect.launch(collect.processes_forever, run_dir, stop, roots), collect.launch(collect.pg_activity_forever, run_dir, stop)],
        "log": collect.capture_log(run_dir, collect.now_iso()), "fleet": None}
    if args.drivers:
        print(f"  starting the fleet of {args.drivers} simulated drivers ...")
        session["fleet"] = start_fleet(args.drivers, run_dir)
        roots["simulator"] = psutil.Process(session["fleet"].pid)
    return run_dir, session, timings


def finish(args, session: dict, extra: dict) -> list[dict]:
    """After the load: the settle period, the wait for a quiet system, the last scrape, the checks, the files."""
    run_dir = session["run_dir"]
    print(f"  load finished; settling {args.settle} s with the fleet running ...")
    time.sleep(args.settle)
    waited = collect.wait_until_quiet(args.settle_max)
    time.sleep(GAUGE_REFRESH_S + 1)
    _, final_metrics = collect.scrape(session["token"])
    (run_dir / "final_metrics.txt").write_text(final_metrics)
    session["stop"].set()
    if session["fleet"] is not None:
        session["fleet"].terminate()
        session["fleet"].wait(timeout=60)
    logs, packer = session["log"]
    logs.terminate()
    packer.wait(timeout=60)  # gzip ends by itself at the end of its input and writes the end of the file
    for thread in session["threads"]:
        thread.join(timeout=10)
    collect.dump_statements(run_dir)
    records = report.read_requests(run_dir)
    checks = collect.check_run(final_metrics, records)
    (run_dir / "run.json").write_text(json.dumps({"quiet_wait_s": round(waited, 1), "checks": checks, **extra}, indent=1))
    return checks


def run_stepped(args) -> int:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + (f"-{args.label}" if args.label else "")
    run_dir, session, timings = begin(args, run_id)
    try:
        processes = ["--processes", str(args.processes)] if args.processes > 1 else []
        print(f"  Locust: steps {args.steps}, warm-up {args.warmup} s, hold {args.hold} s, {args.processes} process(es)")
        started = time.time()
        locust = subprocess.Popen(
            [str(LOCUST), "-f", "loadtest/locustfile.py", "--headless", "--host", common.API_URL, *processes, "--csv", str(run_dir / "locust"), "--stop-timeout", "30", "--loglevel", "INFO"],
            cwd=common.ROOT, env=load_environment(args, run_dir, args.processes), stdout=open(run_dir / "locust.log", "w"), stderr=subprocess.STDOUT)
        session["roots"]["locust"] = psutil.Process(locust.pid)
        locust.wait()
        ended = time.time()
        checks = finish(args, session, {"run_id": run_id, "locust_started": started, "locust_ended": ended, "locust_exit": locust.returncode, "reset": timings})
    finally:
        session["stop"].set()
    print(report.build(run_dir))
    print("\n".join(f"{'ok  ' if check['ok'] else 'FAIL'} {check['name']}: {check['detail']}" for check in checks))
    return 0 if all(check["ok"] for check in checks) else 1


def run_probes(args) -> int:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-probe{'-' + args.label if args.label else ''}"
    run_dir, session, timings = begin(args, run_id)
    try:
        for tag in args.only or PROBES:
            print(f"  probe {tag} ...")
            probe_dir = run_dir / f"probe-{tag}"
            probe_dir.mkdir()
            subprocess.run(
                [str(LOCUST), "-f", "loadtest/probes.py", "--headless", "--host", common.API_URL, "-u", str(PROBE_USERS), "-r", str(PROBE_USERS),
                 "-t", f"{PROBE_WARMUP_S + PROBE_HOLD_S}s", "--tags", tag, "--loglevel", "WARNING"],
                cwd=common.ROOT, env={**os.environ, "LOADTEST_RUN_DIR": str(probe_dir), "LOADTEST_PROBE": tag, "LOADTEST_PROCESSES": "1"},
                stdout=open(probe_dir / "locust.log", "w"), stderr=subprocess.STDOUT)
            time.sleep(2)
        checks = finish(args, session, {"run_id": run_id, "probes": args.only or PROBES, "reset": timings})
    finally:
        session["stop"].set()
    print(report.build_probes(run_dir))
    print("\n".join(f"{'ok  ' if check['ok'] else 'FAIL'} {check['name']}: {check['detail']}" for check in checks))
    return 0 if all(check["ok"] for check in checks) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Load test harness for the ride-hailing backend")
    commands = parser.add_subparsers(dest="command", required=True)
    seed_parser = commands.add_parser("seed", help="fill the load database and store it as the template (idempotent)")
    seed_parser.add_argument("--riders", type=int, default=SEED_RIDERS)
    seed_parser.add_argument("--drivers", type=int, default=FLEET_DRIVERS)
    seed_parser.add_argument("--admin-viewers", type=int, default=ADMIN_VIEWERS)
    commands.add_parser("reset", help="drop the load database, recreate it from the template, flush redis-load, restart backend-load")
    for name, text in (("run", "a stepped load run against a fresh reset, then the correctness checks and the report"),
                       ("probe", "the capacity of single endpoints: 20 users on one endpoint at a time")):
        command = commands.add_parser(name, help=text)
        command.add_argument("--steps", type=lambda value: [int(item) for item in value.split(",")], default=STEP_USERS, help="total users per step")
        command.add_argument("--warmup", type=float, default=WARMUP_S)
        command.add_argument("--hold", type=float, default=HOLD_S)
        command.add_argument("--sample", type=float, default=SAMPLE_S, help="seconds between /metrics scrapes and docker stats")
        command.add_argument("--drivers", type=int, default=FLEET_DRIVERS, help="simulated drivers; 0 for none")
        command.add_argument("--requester-ratio", type=float, default=REQUESTER_RATIO)
        command.add_argument("--seed", type=int, default=1, help="seeds every random choice of every simulated user")
        command.add_argument("--seed-riders", type=int, default=SEED_RIDERS, help="how many rider accounts the template has")
        command.add_argument("--processes", type=int, default=1, help="Locust worker processes (more when the generator is the limit)")
        command.add_argument("--spawn-rate", type=float, default=SPAWN_RATE)
        command.add_argument("--settle", type=float, default=SETTLE_S)
        command.add_argument("--settle-max", type=float, default=SETTLE_MAX_S)
        command.add_argument("--label", default="")
        if name == "probe":
            command.add_argument("--only", nargs="*", choices=PROBES, help="probe only these endpoints")
    report_parser = commands.add_parser("report", help="print the report of a finished run again")
    report_parser.add_argument("run", help="a run id or a directory")
    args = parser.parse_args()

    if args.command == "seed":
        seed_module.seed(args.riders, args.drivers, args.admin_viewers)
    elif args.command == "reset":
        print({name: round(value, 2) for name, value in reset().items()})
    elif args.command == "run":
        sys.exit(run_stepped(args))
    elif args.command == "probe":
        sys.exit(run_probes(args))
    elif args.command == "report":
        run_dir = Path(args.run) if Path(args.run).exists() else common.RESULTS_DIR / args.run
        print(report.build_probes(run_dir) if "probe" in run_dir.name else report.build(run_dir))


if __name__ == "__main__":
    main()
