"""Pure functions of the load test report: percentiles, the Prometheus histogram rule, step assignment by timestamp, the SLO
checks and the capacity and knee. No files, no clock, no network. A request record is a dict with the keys
ts (epoch seconds), type ("GET", "POST", ..., or "OUTCOME"), name, ms, status (0 when no answer came) and success."""
import math
import re

# The SLOs, evaluated on the hold period of a step.
CORE_READ_P95_MS = 300
CORE_READ_P99_MS = 1000
POST_P95_MS = 800
POST_NAMES = ("POST /rides/estimate", "POST /rides")
MAX_ERROR_RATE = 0.005  # the rate must be BELOW this
MAX_LOCUST_CPU_PERCENT = 70  # of one core, on average over the hold; above it the generator is the limit and the step is INVALID

METRIC_LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)$')
LABEL_PAIR = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse_metrics(text: str) -> list[tuple[str, dict, float]]:
    """The samples of a Prometheus text exposition as (name, labels, value)."""
    samples = []
    for line in text.splitlines():
        match = METRIC_LINE.match(line)
        if line.startswith("#") or match is None:
            continue
        labels = {key: value for key, value in LABEL_PAIR.findall(match.group(2) or "")}
        samples.append((match.group(1), labels, float(match.group(3))))
    return samples


def histogram_quantile(q: float, buckets: list[tuple[float, float]]) -> float | None:
    """The Prometheus rule. buckets are (upper bound, cumulative count), sorted, the last one +inf. The rank is q * total;
    take the first bucket whose cumulative count reaches it and interpolate linearly inside it (the first bucket starts at 0).
    A rank in the +inf bucket gives the highest finite bound. None when nothing was observed."""
    total = buckets[-1][1] if buckets else 0
    if total == 0:
        return None
    rank = q * total
    lower_bound, lower_count = 0.0, 0.0
    for upper_bound, count in buckets:
        if count >= rank:
            if math.isinf(upper_bound):
                return lower_bound
            return lower_bound + (upper_bound - lower_bound) * (rank - lower_count) / (count - lower_count)
        lower_bound, lower_count = upper_bound, count
    return lower_bound


def percentile(samples: list[float], q: float) -> float | None:
    """Nearest rank: the ceil(q * n)-th smallest sample (at least the first). None for no samples."""
    if not samples:
        return None
    ordered = sorted(samples)
    rank = max(1, math.ceil(round(q * len(ordered), 9)))
    return ordered[rank - 1]


def percentile_from_counts(counts: dict[float, int], q: float) -> float | None:
    """The same nearest rank, from {value: how many times} (Locust's response time cache)."""
    total = sum(counts.values())
    if total == 0:
        return None
    rank = max(1, math.ceil(round(q * total, 9)))
    seen = 0
    for value in sorted(counts):
        seen += counts[value]
        if seen >= rank:
            return value
    return None


def step_of(ts: float, steps: list[dict]) -> int | None:
    """The step a moment belongs to, warm-up included: its start is inclusive and its end is exclusive, so the end of a
    step is the start of the next one. A step is {index, users, start, warmup_end, end}."""
    for step in steps:
        if step["start"] <= ts < step["end"]:
            return step["index"]
    return None


def hold_step_of(ts: float, steps: list[dict]) -> int | None:
    """The step whose HOLD period contains the moment, or None: the warm-up of every step is left out."""
    for step in steps:
        if step["warmup_end"] <= ts < step["end"]:
            return step["index"]
    return None


def is_slo_request(record: dict) -> bool:
    """Setup, teardown and outcome events are not requests of the page mix and never count toward an SLO."""
    return record["type"] != "OUTCOME" and not record["name"].startswith(("SETUP ", "TEARDOWN "))


def is_error(record: dict) -> bool:
    """A status of 500 or above, no answer at all (timeout, refused), or any request the user class marked as failed
    (a 4xx it did not expect). A 404 that the page expects is a success."""
    return record["status"] >= 500 or record["status"] == 0 or not record["success"]


def group_of(record: dict) -> str | None:
    if record["type"] == "GET":
        return "core_read"
    if record["name"] in POST_NAMES:
        return record["name"]
    return None


def evaluate_step(records: list[dict]) -> dict:
    """The SLO verdict of one step, from the records of its hold period (outcome and setup events are filtered out here).
    Every percentile is taken from the raw response times of the whole group, never from an average of averages. A group
    with no request in the step has no check."""
    requests = [record for record in records if is_slo_request(record)]
    groups: dict[str, list[float]] = {}
    for record in requests:
        group = group_of(record)
        if group is not None:
            groups.setdefault(group, []).append(record["ms"])
    summary = {
        group: {"count": len(values), "p50": percentile(values, 0.50), "p95": percentile(values, 0.95), "p99": percentile(values, 0.99)}
        for group, values in groups.items()
    }

    checks = []
    if "core_read" in summary:
        checks.append({"name": "core_read p95", "value": summary["core_read"]["p95"], "limit": CORE_READ_P95_MS})
        checks.append({"name": "core_read p99", "value": summary["core_read"]["p99"], "limit": CORE_READ_P99_MS})
    for name in POST_NAMES:
        if name in summary:
            checks.append({"name": f"{name} p95", "value": summary[name]["p95"], "limit": POST_P95_MS})
    errors = sum(1 for record in requests if is_error(record))
    error_rate = errors / len(requests) if requests else 0.0
    checks.append({"name": "error rate", "value": error_rate, "limit": MAX_ERROR_RATE})
    for check in checks:
        check["ok"] = check["value"] < check["limit"]
    return {"requests": len(requests), "errors": errors, "error_rate": error_rate, "groups": summary, "checks": checks,
            "slo_ok": all(check["ok"] for check in checks)}


def is_invalid(locust_cpu_percent: float) -> bool:
    return locust_cpu_percent > MAX_LOCUST_CPU_PERCENT


def capacity_and_knee(steps: list[dict]) -> tuple[int | None, int | None]:
    """steps are {index, slo_ok, invalid} in order. Capacity is the highest step where every SLO holds and the step is not
    INVALID (None when there is none). The knee is the first step that violates an SLO (None when nothing does)."""
    passing = [step["index"] for step in steps if step["slo_ok"] and not step["invalid"]]
    violating = [step["index"] for step in steps if not step["slo_ok"]]
    return (max(passing) if passing else None, min(violating) if violating else None)


def outcome_mix(records: list[dict]) -> dict[str, int]:
    """How often each ride outcome (completed, cancelled, no_driver_found, ...) was recorded."""
    mix: dict[str, int] = {}
    for record in records:
        if record["type"] == "OUTCOME":
            mix[record["name"]] = mix.get(record["name"], 0) + 1
    return mix
