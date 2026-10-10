"""What is recorded during a run, and the correctness checks after it. Everything goes to files in the run directory:
metrics.txt.gz (every /metrics scrape, each after a "#SCRAPE <epoch>" line), docker_stats.jsonl, processes.jsonl (CPU and
memory of the Locust processes and the simulator), pg_activity.jsonl (connections by state and wait event, every second),
backend.log.gz (the JSON log of backend-load), pg_stat_statements.json, machine.json."""
import gzip
import json
import platform
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import psutil

import common
import stats

CONTAINERS = ["backend-load", "postgres-load", "redis-load"]
ACTIVE_STATUSES = "'REQUESTED', 'DRIVER_ASSIGNED', 'DRIVER_ARRIVED', 'IN_PROGRESS'"
PG_ACTIVITY_SQL = (
    "SELECT extract(epoch FROM clock_timestamp())::numeric(16,3), coalesce((SELECT json_object_agg(k, n) FROM "
    "(SELECT coalesce(state, 'null') || '/' || coalesce(wait_event_type, '-') AS k, count(*) AS n FROM pg_stat_activity "
    f"WHERE datname = '{common.LOAD_DB}' AND backend_type = 'client backend' AND pid <> pg_backend_pid() GROUP BY 1) t), '{{}}'::json);\n\\watch 1\n"
)
STATEMENTS_SQL = (
    "SELECT jsonb_agg(t)::text FROM (SELECT round(total_exec_time::numeric, 1) AS total_ms, calls, round(mean_exec_time::numeric, 3) AS mean_ms, rows, "
    "left(regexp_replace(query, '\\s+', ' ', 'g'), 200) AS query FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database "
    f"WHERE datname = '{common.LOAD_DB}') ORDER BY {{order}} DESC LIMIT 20) t"
)


def scrape(token: str) -> tuple[float, str]:
    request = urllib.request.Request(f"{common.API_URL}/metrics", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=10) as response:
        text = response.read().decode()
    return time.time(), text


def launch(target, *args) -> threading.Thread:
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()
    return thread


def scrape_forever(run_dir: Path, stop: threading.Event, token: str, sample_s: float) -> None:
    """/metrics every sample_s seconds, and exactly at the start, the end of the warm-up and the end of every step once the
    load shape has written steps.json, so the hold period of a step has its own first and last scrape."""
    last = 0.0
    with gzip.open(run_dir / "metrics.txt.gz", "wt") as out:
        while not stop.is_set():
            boundaries = []
            plan_file = run_dir / "steps.json"
            if plan_file.exists():
                plan = json.loads(plan_file.read_text())
                boundaries = [step[key] for step in plan["steps"] for key in ("start", "warmup_end", "end") if plan["started_at"]]
            next_at = min([last + sample_s] + [t for t in boundaries if t > last])
            if stop.wait(max(0.0, next_at - time.time())):
                break
            try:
                last, text = scrape(token)
            except OSError:
                stop.wait(1)  # the backend is restarting: a gap in the series says so
                continue
            out.write(f"#SCRAPE {last}\n{text}\n")
            out.flush()


def docker_stats_forever(run_dir: Path, stop: threading.Event, sample_s: float) -> None:
    names = [f"uber-{service}-1" for service in CONTAINERS]
    with open(run_dir / "docker_stats.jsonl", "w", buffering=1) as out:
        while not stop.is_set():
            result = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}", *names],
                                    capture_output=True, text=True)
            now = time.time()
            for line in result.stdout.splitlines():
                name, cpu, memory = line.split("|")
                used = memory.split("/")[0].strip()
                megabytes = float(used[:-3]) * (1024 if used.endswith("GiB") else 1) if used.endswith(("MiB", "GiB")) else 0.0
                out.write(json.dumps({"ts": round(now, 3), "name": name, "cpu": float(cpu.rstrip("%")), "mem_mb": round(megabytes, 1)}) + "\n")
            stop.wait(sample_s)


def processes_forever(run_dir: Path, stop: threading.Event, roots: dict) -> None:
    """CPU (percent of one core) and resident memory of the registered processes and all their children, every second.
    roots maps a label to a psutil.Process and is changed by run.py while this runs."""
    known: dict[int, psutil.Process] = {}  # cpu_percent measures from the previous call of the same object
    with open(run_dir / "processes.jsonl", "w", buffering=1) as out:
        while not stop.is_set():
            for label, root in list(roots.items()):
                try:
                    family = [root, *root.children(recursive=True)]
                except psutil.NoSuchProcess:
                    continue
                for process in family:
                    process = known.setdefault(process.pid, process)
                    try:
                        out.write(json.dumps({"ts": round(time.time(), 3), "label": label, "pid": process.pid,
                                              "cpu": process.cpu_percent(interval=None), "rss_mb": round(process.memory_info().rss / 2**20, 1)}) + "\n")
                    except psutil.NoSuchProcess:
                        known.pop(process.pid, None)
            stop.wait(1)


def pg_activity_forever(run_dir: Path, stop: threading.Event) -> None:
    user = common.read_env()["POSTGRES_USER"]
    watcher = subprocess.Popen([*common.COMPOSE, "exec", "-T", "postgres-load", "psql", "-U", user, "-d", "postgres", "-At", "-F", "|"],
                               cwd=common.ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    watcher.stdin.write(PG_ACTIVITY_SQL)
    watcher.stdin.flush()
    threading.Thread(target=lambda: (stop.wait(), watcher.terminate()), daemon=True).start()
    with open(run_dir / "pg_activity.jsonl", "w", buffering=1) as out:
        for line in watcher.stdout:
            ts, _, counts = line.partition("|")
            if counts:
                out.write(json.dumps({"ts": float(ts), "counts": json.loads(counts)}) + "\n")


def capture_log(run_dir: Path, since: str) -> tuple[subprocess.Popen, subprocess.Popen]:
    """The log of backend-load, gzipped as it arrives. Stop both processes when the run is over."""
    logs = subprocess.Popen([*common.COMPOSE, "logs", "-f", "--no-log-prefix", "--since", since, "backend-load"],
                            cwd=common.ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    packer = subprocess.Popen(["gzip", "-c"], stdin=logs.stdout, stdout=open(run_dir / "backend.log.gz", "wb"))
    return logs, packer


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def machine_facts(run_dir: Path, extra: dict) -> None:
    postgres = dict(line.split("|") for line in common.psql(
        "SELECT name, setting || coalesce(unit, '') FROM pg_settings WHERE name IN ('shared_buffers', 'max_connections', 'work_mem', "
        "'effective_cache_size', 'shared_preload_libraries', 'synchronous_commit', 'fsync', 'max_wal_size', 'jit')"))
    pool = common.compose("exec", "-T", "backend-load", "python", "-c",
                          "from app.database import engine; p = engine.pool; print(type(p).__name__, p.size(), p._max_overflow, p._timeout, p._pre_ping)").stdout.split()
    command = json.loads(common.compose("config", "--format", "json", "backend-load").stdout)["services"]["backend-load"]["command"]
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=common.ROOT, capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=common.ROOT, capture_output=True, text=True).stdout.strip())
    docker_info = subprocess.run(["docker", "info", "--format", "{{.NCPU}} {{.MemTotal}}"], capture_output=True, text=True).stdout.split()
    facts = {
        "nproc": psutil.cpu_count(), "memory_gb": round(psutil.virtual_memory().total / 2**30, 1),
        "memory_available_gb": round(psutil.virtual_memory().available / 2**30, 1), "docker_cpus": docker_info[0],
        "kernel": platform.release(), "python": platform.python_version(), "git_commit": commit + ("+uncommitted changes" if dirty else ""),
        "postgres_settings": postgres, "postgres_image": "postgis/postgis:16-3.4",
        "pool": dict(zip(("class", "size", "max_overflow", "timeout_s", "pre_ping"), pool)),
        "uvicorn_command": " ".join(command),
        "load_average_before": psutil.getloadavg(), **extra,
    }
    (run_dir / "machine.json").write_text(json.dumps(facts, indent=1))


def dump_statements(run_dir: Path) -> None:
    result = {order: json.loads(common.psql(STATEMENTS_SQL.format(order=order))[0] or "[]") for order in ("total_exec_time", "mean_exec_time")}
    (run_dir / "pg_stat_statements.json").write_text(json.dumps(result, indent=1))


def wait_until_quiet(max_wait_s: float) -> float:
    """Waits until no ride is in an active status and no offer is pending, so the counters and the database can be compared
    exactly. Rides in progress finish by themselves (the fleet drives them), which can take minutes. Returns the seconds waited."""
    started = time.monotonic()
    while time.monotonic() - started < max_wait_s:
        active = common.psql(f"SELECT (SELECT count(*) FROM rides WHERE status IN ({ACTIVE_STATUSES})) + (SELECT count(*) FROM ride_offers WHERE status = 'PENDING')")
        if int(active[0]) == 0:
            return time.monotonic() - started
        time.sleep(5)
    return time.monotonic() - started


def check_run(final_metrics: str, records: list[dict]) -> list[dict]:
    """The correctness checks. A failure here outranks any performance finding. Each check is {name, ok, detail}."""
    checks = []
    invariants = (common.ROOT / "simulator" / "invariants.sql").read_text()
    user = common.read_env()["POSTGRES_USER"]
    output = common.compose("exec", "-T", "-e", "PGOPTIONS=-c default_transaction_read_only=on", "postgres-load", "psql", "-U", user,
                            "-d", common.LOAD_DB, "-v", "ON_ERROR_STOP=1", "-At", "-F", "|", input=invariants).stdout.splitlines()
    sections: dict[str, int] = {}
    for line in output:
        if line.startswith("== "):
            current = line[3:]
            sections[current] = 0
        elif line.strip():
            sections[current] += 1
    broken = {name: count for name, count in sections.items() if count}
    checks.append({"name": "invariants I1 to I24", "ok": len(sections) == 24 and not broken, "detail": f"{len(sections)} invariants, violated: {broken or 'none'}"})

    samples = stats.parse_metrics(final_metrics)
    in_metrics = {}
    for name, labels, value in samples:
        if name == "ride_transitions_total":
            in_metrics[(labels["from_status"], labels["to_status"])] = int(value)
    in_sql = {(a, b): int(n) for a, b, n in (line.split("|") for line in common.psql(
        "SELECT coalesce(from_status::text, 'none'), to_status::text, count(*) FROM ride_events GROUP BY 1, 2 ORDER BY 1, 2"))}
    checks.append({"name": "ride_transitions_total equals ride_events", "ok": in_metrics == in_sql,
                   "detail": f"metrics {sum(in_metrics.values())}, sql {sum(in_sql.values())}, {len(in_sql)} kinds" + ("" if in_metrics == in_sql else f", differences: {dict(set(in_metrics.items()) ^ set(in_sql.items()))}")})

    created_metrics = int(sum(value for name, labels, value in samples
                              if name == "http_requests_total" and labels == {"method": "POST", "route": "/rides", "status": "201"}))
    created_locust = sum(1 for record in records if record["name"] == "POST /rides" and record["status"] == 201)
    unanswered = sum(1 for record in records if record["name"] == "POST /rides" and record["status"] == 0)  # timed out: the server may have created the ride
    created_sql = int(common.psql("SELECT count(*) FROM rides")[0])
    checks.append({"name": "POST /rides 201: metrics, Locust and rides table agree", "ok": created_metrics == created_sql and created_locust <= created_metrics <= created_locust + unanswered,
                   "detail": f"metrics {created_metrics}, sql {created_sql}, Locust {created_locust} (+ {unanswered} requests without an answer)"})

    errors = sum(value for name, labels, value in samples if name == "observability_errors_total")
    checks.append({"name": "observability_errors_total is 0", "ok": errors == 0, "detail": f"{errors:g}"})

    stuck = int(common.psql(f"SELECT count(*) FROM rides WHERE status IN ({ACTIVE_STATUSES})")[0])
    pending = int(common.psql("SELECT count(*) FROM ride_offers WHERE status = 'PENDING'")[0])
    checks.append({"name": "no ride in an active status, no pending offer", "ok": stuck == 0 and pending == 0, "detail": f"{stuck} rides, {pending} offers"})
    return checks
