"""The step load shape: the user counts of LOADTEST_STEPS in order, each with a warm-up and a hold. The settings come from the
LOADTEST_* variables that run.py sets. steps.json (in the run directory) tells the report which moment belongs to which step.
The run ends early on a collapse: an error rate of 5 percent or more, or a p95 of 5000 ms or more over the last 10 seconds, in
two checks in a row (checked every 5 seconds, in the hold period only)."""
import json
import os
import time
from pathlib import Path

from locust import LoadTestShape

import stats

CHECK_EVERY_S = 5
WINDOW_S = 10
COLLAPSE_ERROR_RATE = 0.05
COLLAPSE_P95_MS = 5000
MIN_REQUESTS_FOR_A_CHECK = 20
TAIL_BYTES = 4_000_000  # more than WINDOW_S of requests at the highest load


class StepShape(LoadTestShape):
    def __init__(self):
        super().__init__()
        self.run_dir = Path(os.environ["LOADTEST_RUN_DIR"])
        users = [int(value) for value in os.environ["LOADTEST_STEPS"].split(",")]
        self.spawn_rate = float(os.environ["LOADTEST_SPAWN_RATE"])
        warmup_s, hold_s = float(os.environ["LOADTEST_WARMUP_S"]), float(os.environ["LOADTEST_HOLD_S"])
        self.plan = {"started_at": None, "warmup_s": warmup_s, "hold_s": hold_s, "stopped_at": None, "collapse": None, "steps": [
            {"index": index, "users": count, "start": index * (warmup_s + hold_s), "warmup_end": index * (warmup_s + hold_s) + warmup_s,
             "end": (index + 1) * (warmup_s + hold_s)} for index, count in enumerate(users)]}
        self.last_check = 0.0
        self.bad_checks = 0

    def write_plan(self) -> None:
        self.run_dir.joinpath("steps.json").write_text(json.dumps(self.plan, indent=1))

    def last_seconds_of_requests(self, now: float) -> list[dict]:
        """The request records of the last WINDOW_S seconds, read from the tail of every request log in the run directory."""
        records = []
        for path in self.run_dir.glob("requests-*.jsonl"):
            with open(path, "rb") as log:
                log.seek(0, os.SEEK_END)
                log.seek(max(0, log.tell() - TAIL_BYTES))
                for line in log.read().splitlines()[1:]:  # the first line may be cut in half
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue  # the last line may still be being written
                    if record["ts"] >= now - WINDOW_S and stats.is_slo_request(record):
                        records.append(record)
        return records

    def tick(self):
        run_time = self.get_run_time()
        now = time.time()
        if self.plan["started_at"] is None:
            self.plan["started_at"] = now - run_time
            # The absolute times are known now; the report reads them from this file.
            for step in self.plan["steps"]:
                for key in ("start", "warmup_end", "end"):
                    step[key] += self.plan["started_at"]
            self.write_plan()

        current = stats.step_of(now, self.plan["steps"])
        if current is None:  # past the last step
            self.plan["stopped_at"] = now
            self.write_plan()
            return None

        in_hold = stats.hold_step_of(now, self.plan["steps"]) is not None
        if in_hold and now - self.last_check >= CHECK_EVERY_S:
            self.last_check = now
            records = self.last_seconds_of_requests(now)
            errors = sum(1 for record in records if stats.is_error(record))
            p95 = stats.percentile([record["ms"] for record in records], 0.95)
            collapsed = len(records) >= MIN_REQUESTS_FOR_A_CHECK and (errors / len(records) >= COLLAPSE_ERROR_RATE or p95 >= COLLAPSE_P95_MS)
            self.bad_checks = self.bad_checks + 1 if collapsed else 0
            if self.bad_checks >= 2:
                self.plan["stopped_at"] = now
                self.plan["collapse"] = {"step": current, "requests": len(records), "error_rate": errors / len(records), "p95_ms": p95}
                self.write_plan()
                return None
        return self.plan["steps"][current]["users"], self.spawn_rate
