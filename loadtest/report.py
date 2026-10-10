"""Turns a run directory into the report: one table per aspect, per step, computed on the HOLD period only. Both latencies are
shown: the client side (what Locust measured, including any queueing before the app) and the server side (the histogram
http_request_duration_seconds, which starts when the app receives the request). python loadtest/run.py report <run id>"""
import gzip
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import stats

INF = math.inf


def read_requests(run_dir: Path) -> list[dict]:
    return [json.loads(line) for path in sorted(run_dir.glob("**/requests-*.jsonl")) for line in path.read_text().splitlines()]


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def read_scrapes(run_dir: Path) -> list[tuple[float, list]]:
    """[(epoch, samples)] in time order, from metrics.txt.gz."""
    scrapes = []
    with gzip.open(run_dir / "metrics.txt.gz", "rt") as source:
        for block in source.read().split("#SCRAPE ")[1:]:
            header, _, text = block.partition("\n")
            scrapes.append((float(header), stats.parse_metrics(text)))
    return scrapes


def read_log(run_dir: Path) -> list[dict]:
    """The http_request lines of the backend log: ts (epoch), method, route, status, duration_ms, db_queries, db_ms."""
    lines = []
    with gzip.open(run_dir / "backend.log.gz", "rt", errors="replace") as source:
        for text in source:
            try:
                line = json.loads(text)
            except ValueError:
                continue  # the first lines of a uvicorn start are plain text
            if line.get("msg") == "http_request":
                line["ts"] = datetime.strptime(line["ts"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()
                lines.append(line)
    return lines


def nearest(scrapes: list, moment: float) -> tuple[float, list]:
    return min(scrapes, key=lambda scrape: abs(scrape[0] - moment))


def buckets_of(samples: list, name: str, **match) -> list[tuple[float, float]]:
    """The cumulative buckets of a histogram, summed over every series whose labels contain `match` (a route may be a set)."""
    summed: dict[float, float] = {}
    for sample_name, labels, value in samples:
        if sample_name == f"{name}_bucket" and all(labels.get(key) in allowed for key, allowed in match.items()):
            bound = INF if labels["le"] == "+Inf" else float(labels["le"])
            summed[bound] = summed.get(bound, 0) + value
    return sorted(summed.items())


def histogram_totals(samples: list, name: str, method: str, route: str) -> tuple[float, float]:
    """(sum, count) of one histogram series of the HTTP latency, to get a mean between two scrapes."""
    return tuple(sum(value for sample_name, labels, value in samples if sample_name == f"{name}_{key}" and labels.get("method") == method
                     and labels.get("route") == route) for key in ("sum", "count"))


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def mean_connections(seconds: list[dict], kind: str) -> float:
    """Mean number of load database connections in a state ("active", "idle in transaction") over the sampled seconds."""
    return sum(count for row in seconds for key, count in row["counts"].items() if key.startswith(kind)) / max(1, len(seconds))


def delta_quantiles(first: list, last: list, name: str, **match) -> tuple[float | None, float | None, float | None, float | None]:
    """p50, p95, p99 and the upper bound of the highest bucket that grew, between two scrapes (seconds)."""
    before, after = buckets_of(first, name, **match), buckets_of(last, name, **match)
    grown = [(bound, count_after - count_before) for (bound, count_before), (_, count_after) in zip(before, after)]
    highest = None
    for position, (bound, cumulative) in enumerate(grown):
        if cumulative > (grown[position - 1][1] if position else 0):
            highest = bound
    return (stats.histogram_quantile(0.50, grown), stats.histogram_quantile(0.95, grown), stats.histogram_quantile(0.99, grown), highest)


def server_5xx(first: list, last: list) -> float:
    """Responses with a status of 500 or above between two scrapes, from every client (Locust users and the fleet)."""
    def total(samples: list) -> float:
        return sum(value for name, labels, value in samples if name == "http_requests_total" and labels["status"].startswith("5"))
    return total(last) - total(first)


def gauge_max(scrapes: list, name: str, start: float, end: float) -> float | None:
    values = [value for ts, samples in scrapes if start <= ts <= end for sample_name, _, value in samples if sample_name == name]
    return max(values) if values else None


def ms(seconds: float | None) -> str:
    return "-" if seconds is None else f"{seconds * 1000:.0f}"


def table(header: list[str], rows: list[list]) -> str:
    return "\n".join(["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows])


def build(run_dir: Path) -> str:
    plan = json.loads((run_dir / "steps.json").read_text())
    run = json.loads((run_dir / "run.json").read_text())
    machine = json.loads((run_dir / "machine.json").read_text())
    records = read_requests(run_dir)
    scrapes = read_scrapes(run_dir)
    docker = read_jsonl(run_dir / "docker_stats.jsonl")
    processes = read_jsonl(run_dir / "processes.jsonl")
    activity = read_jsonl(run_dir / "pg_activity.jsonl")
    steps = plan["steps"]

    in_step: dict[int, list] = {step["index"]: [] for step in steps}
    outcomes: dict[int, list] = {step["index"]: [] for step in steps}
    for record in records:
        index = stats.hold_step_of(record["ts"], steps)
        if index is not None:
            (outcomes if record["type"] == "OUTCOME" else in_step)[index].append(record)

    verdicts, load_rows, server_rows, outcome_rows = [], [], [], []
    for step in steps:
        index, start, end = step["index"], step["warmup_end"], step["end"]
        window = in_step[index]
        if not window:  # the run stopped before this step
            continue
        verdict = stats.evaluate_step(window)
        groups = verdict["groups"]
        core, estimate, ride = groups.get("core_read"), groups.get("POST /rides/estimate"), groups.get("POST /rides")
        locust_cpu = max((sum(p["cpu"] for p in processes if p["pid"] == pid and start <= p["ts"] < end) /
                          max(1, sum(1 for p in processes if p["pid"] == pid and start <= p["ts"] < end))
                          for pid in {p["pid"] for p in processes if p["label"] == "locust"}), default=0.0)
        invalid = stats.is_invalid(locust_cpu)
        verdicts.append({"index": index, "slo_ok": verdict["slo_ok"], "invalid": invalid})

        first, last = nearest(scrapes, start)[1], nearest(scrapes, end)[1]
        read_routes = {record["name"].split(" ", 1)[1] for record in window if record["type"] == "GET"}
        server_core = delta_quantiles(first, last, "http_request_duration_seconds", method={"GET"}, route=read_routes)
        server_estimate = delta_quantiles(first, last, "http_request_duration_seconds", method={"POST"}, route={"/rides/estimate"})
        server_ride = delta_quantiles(first, last, "http_request_duration_seconds", method={"POST"}, route={"/rides"})
        lag = delta_quantiles(first, last, "event_loop_lag_seconds")
        container_cpu = {name: [row["cpu"] for row in docker if row["name"].endswith(f"{name}-1") and start <= row["ts"] < end] for name in ("backend-load", "postgres-load", "redis-load")}
        cpu = {name: (sum(values) / len(values) if values else 0.0) for name, values in container_cpu.items()}
        seconds_in_window = [row for row in activity if start <= row["ts"] < end]

        errors_5xx = sum(1 for record in window if stats.is_slo_request(record) and record["status"] >= 500)
        failures = verdict["errors"] - errors_5xx
        failed = [check["name"] for check in verdict["checks"] if not check["ok"]]
        logged_in = sum(1 for record in records if record["name"] == "SETUP login" and record["success"] and record["ts"] < start)
        load_rows.append([index, f"{step['users']} ({logged_in} logged in)", f"{verdict['requests'] / (end - start):.0f}",
                          *(f"{core[key]:.0f}" if core else "-" for key in ("p50", "p95", "p99")),
                          f"{ms(server_core[0])}/{ms(server_core[1])}/{ms(server_core[2])}",
                          f"{estimate['p95']:.0f}/{ms(server_estimate[1])}" if estimate else "-", f"{ride['p95']:.0f}/{ms(server_ride[1])}" if ride else "-",
                          f"{verdict['error_rate'] * 100:.2f}% ({errors_5xx} 5xx, {failures} other; server 5xx {server_5xx(first, last):.0f})", "INVALID" if invalid else ("ok" if verdict["slo_ok"] else "SLO: " + ", ".join(failed)),
                          f"{locust_cpu:.0f}"])
        server_rows.append([index, step["users"], f"{ms(lag[1])}/{ms(lag[3])}", f"{gauge_max(scrapes, 'db_pool_checked_out', start, end):.0f}",
                            f"{gauge_max(scrapes, 'db_pool_overflow', start, end):.0f}", f"{gauge_max(scrapes, 'http_requests_in_flight', start, end):.0f}",
                            f"{cpu['backend-load']:.0f}", f"{cpu['postgres-load']:.0f}", f"{cpu['redis-load']:.0f}",
                            f"{mean_connections(seconds_in_window, 'active'):.1f}", f"{mean_connections(seconds_in_window, 'idle in transaction'):.1f}"])
        mix = stats.outcome_mix(outcomes[index])
        waits = [record["ms"] / 1000 for record in outcomes[index] if record["name"] == "ride_assigned_wait"]
        outcome_rows.append([index, step["users"], *(mix.get(name, 0) for name in ("completed", "cancelled", "no_driver_found", "price_changed")),
                             f"{stats.percentile(waits, 0.5):.1f}/{stats.percentile(waits, 0.95):.1f}" if waits else "-"])

    capacity, knee = stats.capacity_and_knee(verdicts)
    by_index = {step["index"]: step["users"] for step in steps}
    lines = [f"# Load run {run['run_id']}", "",
             f"commit {machine['git_commit']}, {machine['nproc']} CPUs, {machine['memory_gb']} GB, steps {[step['users'] for step in steps]}, warm-up {plan['warmup_s']:.0f} s, "
             f"hold {plan['hold_s']:.0f} s, reset {run['reset']['total']:.1f} s, quiet after {run['quiet_wait_s']:.0f} s", ""]
    if plan["collapse"]:
        lines += [f"**Stopped early by the collapse rule** in step {plan['collapse']['step']}: {plan['collapse']}", ""]
    lines += ["## SLOs per step (hold period). Latencies in ms, client side first; server side is the histogram p50/p95/p99 (the app only)", "",
              table(["step", "users", "req/s", "core p50", "core p95", "core p99", "server core p50/p95/p99", "estimate p95 client/server", "ride request p95 client/server",
                     "errors", "verdict", "Locust CPU %"], load_rows), "",
              "## Server health per step: loop lag p95/max-bucket (ms), pool checked out max, pool overflow max, in flight max (5 s samples), CPU % of each container, mean connections", "",
              table(["step", "users", "loop lag p95/max", "pool out", "overflow", "in flight", "backend CPU", "postgres CPU", "redis CPU", "pg active", "pg idle in tx"], server_rows), "",
              "## Ride outcomes in the hold period (events) and the wait until a driver was assigned (p50/p95 seconds)", "",
              table(["step", "users", "completed", "cancelled", "no_driver_found", "price_changed", "assigned wait s"], outcome_rows), ""]
    if capacity is None:
        lines.append("**Capacity: none** (the first step already violates an SLO or is INVALID).")
    else:
        lines.append(f"**Capacity: step {capacity} ({by_index[capacity]} users).**" + (" No SLO was violated: **no bottleneck reached up to "
                     f"{by_index[max(by_index)]} users**." if knee is None else ""))
    if knee is not None:
        lines.append(f"**Knee: step {knee} ({by_index[knee]} users).**")
    lines += ["", "## Correctness checks", "", *[f"- {'ok' if check['ok'] else '**FAIL**'} {check['name']}: {check['detail']}" for check in run["checks"]], ""]
    statements = json.loads((run_dir / "pg_stat_statements.json").read_text())
    lines += ["## Top statements by total time (pg_stat_statements)", "", table(["total ms", "calls", "mean ms", "rows", "statement"], [
        [s["total_ms"], s["calls"], s["mean_ms"], s["rows"], s["query"][:110]] for s in statements["total_exec_time"][:10]]), ""]
    text = "\n".join(lines)
    (run_dir / "report.md").write_text(text)
    return text


def build_probes(run_dir: Path) -> str:
    run = json.loads((run_dir / "run.json").read_text())
    scrapes = read_scrapes(run_dir)
    log = read_log(run_dir)
    rows = []
    for tag in run["probes"]:
        records = read_requests(run_dir / f"probe-{tag}")
        if not records:
            rows.append([tag, "no requests"])
            continue
        first_ts = min(record["ts"] for record in records)
        start, end = first_ts + 5, first_ts + 35
        window = [record for record in records if start <= record["ts"] < end and record["name"] != "SETUP login"]
        name = max({record["name"] for record in window}, key=lambda candidate: sum(1 for record in window if record["name"] == candidate)) if window else "-"
        method, _, route = name.partition(" ")
        client = [record["ms"] for record in window]
        errors = sum(1 for record in window if stats.is_error(record))
        first, last = nearest(scrapes, start)[1], nearest(scrapes, end)[1]
        sum_before, count_before = histogram_totals(first, "http_request_duration_seconds", method, route)
        sum_after, count_after = histogram_totals(last, "http_request_duration_seconds", method, route)
        server_mean = (sum_after - sum_before) / (count_after - count_before) * 1000 if count_after > count_before else None
        matching = [line for line in log if line["method"] == method and line["route"] == route and start <= line["ts"] < end]
        duration, queries, db = ([line[key] for line in matching] for key in ("duration_ms", "db_queries", "db_ms"))
        non_db = [d - b for d, b in zip(duration, db)]
        rows.append([tag, name, f"{len(window) / (end - start):.0f}", *(f"{stats.percentile(client, q):.0f}" if client else "-" for q in (0.5, 0.95, 0.99)),
                     f"{server_mean:.1f}" if server_mean is not None else "-", f"{mean(duration):.1f}" if duration else "-",
                     f"{mean(queries):.1f}/{stats.percentile(queries, 0.95):.0f}" if queries else "-", f"{mean(db):.1f}/{stats.percentile(db, 0.95):.1f}" if db else "-",
                     f"{mean(non_db):.1f}" if non_db else "-", errors])
    text = "\n".join([f"# Probes {run['run_id']}: {len(run['probes'])} endpoints, 20 users each, 5 s warm-up, 30 s measured, the fleet running", "",
                      table(["probe", "request", "req/s", "client p50", "client p95", "client p99", "server mean (histogram)", "server mean (log)", "db queries mean/p95",
                             "db ms mean/p95", "non-db ms mean", "errors"], rows), "",
                      "Server columns include the requests of the simulated fleet on the same route (only /rides/active is shared).", ""])
    (run_dir / "report.md").write_text(text)
    return text
