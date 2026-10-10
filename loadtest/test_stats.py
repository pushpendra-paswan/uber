"""Tests of loadtest/stats.py. No database, no Docker: pytest loadtest"""
import math

import pytest

import stats

INF = math.inf
STEPS = [
    {"index": 0, "users": 10, "start": 100.0, "warmup_end": 130.0, "end": 220.0},
    {"index": 1, "users": 25, "start": 220.0, "warmup_end": 250.0, "end": 340.0},
]


def record(ms=50, name="GET /wallet", type_="GET", status=200, success=True, ts=0.0):
    return {"ts": ts, "type": type_, "name": name, "ms": ms, "status": status, "success": success}


# --- histogram quantile (the Prometheus rule), three worked cases and the edges ---


def test_quantile_interpolates_inside_the_first_bucket_that_reaches_the_rank():
    # total 40, rank 20: le=0.1 holds 10, le=0.5 reaches 30. 0.1 + 0.4 * (20 - 10) / (30 - 10) = 0.3
    buckets = [(0.1, 10), (0.5, 30), (1.0, 40), (INF, 40)]
    assert stats.histogram_quantile(0.5, buckets) == pytest.approx(0.3)


def test_quantile_in_a_later_bucket():
    # rank 38 reaches le=1.0 (40) after le=0.5 (30): 0.5 + 0.5 * (38 - 30) / (40 - 30) = 0.9
    buckets = [(0.1, 10), (0.5, 30), (1.0, 40), (INF, 40)]
    assert stats.histogram_quantile(0.95, buckets) == pytest.approx(0.9)


def test_quantile_in_the_infinite_bucket_is_the_highest_finite_bound():
    # total 10, rank 9: le=0.5 holds only 8, so the rank is in +Inf
    assert stats.histogram_quantile(0.9, [(0.1, 5), (0.5, 8), (INF, 10)]) == 0.5


def test_quantile_of_the_first_bucket_starts_at_zero_and_nothing_gives_none():
    assert stats.histogram_quantile(0.5, [(0.1, 10), (INF, 10)]) == pytest.approx(0.05)
    assert stats.histogram_quantile(0.5, [(0.1, 0), (INF, 0)]) is None
    assert stats.histogram_quantile(0.5, [(0.1, 0), (0.5, 10), (INF, 10)]) == pytest.approx(0.3)


# --- exact percentiles: nearest rank, the ceil(q * n)-th smallest ---


def test_percentile_of_one_sample_is_that_sample():
    assert stats.percentile([7.0], 0.5) == 7.0
    assert stats.percentile([7.0], 0.99) == 7.0


def test_percentile_of_two_samples():
    assert stats.percentile([2.0, 1.0], 0.50) == 1.0  # ceil(1.0) = 1st
    assert stats.percentile([2.0, 1.0], 0.95) == 2.0  # ceil(1.9) = 2nd


def test_percentile_of_ten_samples():
    samples = list(range(10, 0, -1))
    assert stats.percentile(samples, 0.50) == 5
    assert stats.percentile(samples, 0.90) == 9  # ceil(9.0) = 9th, not the 10th
    assert stats.percentile(samples, 0.95) == 10  # ceil(9.5) = 10th


def test_percentile_of_a_hundred_samples():
    samples = list(range(1, 101))
    assert stats.percentile(samples, 0.50) == 50
    assert stats.percentile(samples, 0.95) == 95
    assert stats.percentile(samples, 0.99) == 99
    assert stats.percentile([], 0.5) is None


def test_percentile_from_counts_matches_the_expanded_samples():
    counts = {10: 3, 20: 5, 300: 2}
    expanded = [10] * 3 + [20] * 5 + [300] * 2
    for q in (0.5, 0.8, 0.95, 0.99):
        assert stats.percentile_from_counts(counts, q) == stats.percentile(expanded, q)


# --- which step a moment belongs to ---


def test_a_moment_at_a_step_start_belongs_to_that_step_and_at_its_end_to_the_next():
    assert stats.step_of(100.0, STEPS) == 0
    assert stats.step_of(219.999, STEPS) == 0
    assert stats.step_of(220.0, STEPS) == 1
    assert stats.step_of(99.999, STEPS) is None
    assert stats.step_of(340.0, STEPS) is None  # the end of the last step


def test_the_hold_period_starts_when_the_warm_up_ends_and_the_end_is_the_next_steps_warm_up():
    assert stats.hold_step_of(130.0, STEPS) == 0
    assert stats.hold_step_of(219.999, STEPS) == 0
    assert stats.hold_step_of(220.0, STEPS) is None  # the warm-up of step 1
    assert stats.hold_step_of(250.0, STEPS) == 1
    assert stats.hold_step_of(339.999, STEPS) == 1
    assert stats.hold_step_of(340.0, STEPS) is None


def test_the_warm_up_is_excluded_from_every_step():
    for ts in (100.0, 115.0, 129.999, 220.0, 235.0, 249.999):
        assert stats.hold_step_of(ts, STEPS) is None


# --- the SLOs ---


def reads(ms_values):
    return [record(ms=ms) for ms in ms_values]


def test_a_step_passes_only_when_every_check_holds():
    records = reads([20] * 100) + [record(ms=100, name="POST /rides/estimate", type_="POST")] * 5
    result = stats.evaluate_step(records)
    assert result["slo_ok"] and all(check["ok"] for check in result["checks"])
    slow_post = reads([20] * 100) + [record(ms=900, name="POST /rides", type_="POST")] * 5
    assert not stats.evaluate_step(slow_post)["slo_ok"]


def test_the_p95_decides_and_not_the_mean():
    # 94 fast reads and 6 slow ones: the mean is 56 ms, far below 300, but the 95th of 100 is slow.
    records = reads([10] * 94 + [800] * 6)
    result = stats.evaluate_step(records)
    assert sum(r["ms"] for r in records) / len(records) < stats.CORE_READ_P95_MS
    assert result["groups"]["core_read"]["p95"] == 800
    assert not result["slo_ok"]
    # and the other way round: a few slow requests above the p95 rank do not fail the step
    assert stats.evaluate_step(reads([10] * 96 + [800] * 4))["slo_ok"]


def test_the_limits_are_strict():
    assert stats.evaluate_step(reads([299] * 100))["slo_ok"]
    assert not stats.evaluate_step(reads([300] * 100))["slo_ok"]
    p99_slow = reads([10] * 98 + [1000] * 2)
    assert not stats.evaluate_step(p99_slow)["slo_ok"]  # p99 is 1000, not below 1000
    assert stats.evaluate_step(reads([10] * 99 + [999]))["slo_ok"]


def test_the_error_rate_must_stay_below_half_a_percent():
    ok = reads([10] * 996) + [record(status=500, success=False)] * 4  # 4 / 1000 = 0.4 percent
    at_limit = reads([10] * 995) + [record(status=500, success=False)] * 5  # exactly 0.5 percent
    assert stats.evaluate_step(ok)["slo_ok"]
    assert not stats.evaluate_step(at_limit)["slo_ok"]
    assert stats.evaluate_step(at_limit)["error_rate"] == pytest.approx(0.005)


def test_outcome_setup_and_teardown_events_are_not_requests():
    records = reads([10] * 10) + [
        record(ms=9000, name="SETUP login", type_="POST", status=500, success=False),
        record(ms=0, name="TEARDOWN cancel", type_="POST", status=404, success=False),
        record(ms=0, name="completed", type_="OUTCOME", status=0, success=True),
    ]
    result = stats.evaluate_step(records)
    assert result["requests"] == 10 and result["errors"] == 0 and result["slo_ok"]


def test_a_group_without_requests_has_no_check():
    result = stats.evaluate_step(reads([10] * 10))
    assert [check["name"] for check in result["checks"]] == ["core_read p95", "core_read p99", "error rate"]


# --- errors ---


@pytest.mark.parametrize("status, success, error", [
    (200, True, False), (201, True, False), (404, True, False),  # a 404 the page expects (no active ride)
    (404, False, True), (409, False, True), (422, False, True),  # a 4xx the user class did not expect
    (500, True, True), (503, False, True),  # 5xx is an error even when something marked it as a success
    (0, True, True), (0, False, True),  # no answer: timeout or refused
])
def test_error_classification(status, success, error):
    assert stats.is_error(record(status=status, success=success)) is error


def test_an_unexpected_4xx_makes_the_step_fail_the_error_rate():
    records = reads([10] * 100) + [record(status=409, success=False)] * 5
    result = stats.evaluate_step(records)
    assert result["errors"] == 5 and not result["slo_ok"]


def test_outcome_mix_counts_only_outcome_events():
    records = [record(name="completed", type_="OUTCOME"), record(name="completed", type_="OUTCOME"),
               record(name="no_driver_found", type_="OUTCOME"), record(), record(name="completed", type_="GET")]
    assert stats.outcome_mix(records) == {"completed": 2, "no_driver_found": 1}


# --- capacity and knee ---


def steps_of(*verdicts):
    """Each verdict is "ok", "slow" (an SLO is violated) or "invalid" (fine but the generator was saturated)."""
    return [{"index": i, "slo_ok": v != "slow", "invalid": v == "invalid"} for i, v in enumerate(verdicts)]


def test_capacity_is_the_highest_passing_step_and_the_knee_the_first_failing_one():
    assert stats.capacity_and_knee(steps_of("ok", "ok", "ok", "slow", "slow")) == (2, 3)


def test_nothing_violates_means_no_knee_and_the_last_step_is_the_capacity():
    assert stats.capacity_and_knee(steps_of("ok", "ok", "ok")) == (2, None)


def test_the_first_step_already_violating_means_no_capacity():
    assert stats.capacity_and_knee(steps_of("slow", "slow")) == (None, 0)


def test_an_invalid_step_is_never_the_capacity():
    assert stats.capacity_and_knee(steps_of("ok", "ok", "invalid")) == (1, None)
    assert stats.capacity_and_knee(steps_of("invalid", "invalid")) == (None, None)


def test_a_saturated_generator_is_invalid_above_seventy_percent_of_a_core():
    assert not stats.is_invalid(70.0)
    assert stats.is_invalid(70.1)


# --- the Prometheus text parser ---


def test_parse_metrics_reads_labels_and_values():
    text = '# HELP x help\n# TYPE x counter\nhttp_requests_total{method="GET",route="/wallet",status="200"} 12.0\nup 1\nlag_bucket{le="+Inf"} 3\n'
    assert stats.parse_metrics(text) == [
        ("http_requests_total", {"method": "GET", "route": "/wallet", "status": "200"}, 12.0),
        ("up", {}, 1.0),
        ("lag_bucket", {"le": "+Inf"}, 3.0),
    ]
