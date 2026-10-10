"""Names, paths and small helpers shared by seed.py, run.py, collect.py, report.py and the Locust files."""
import os
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "loadtest" / "results"
SIM_PYTHON = ROOT / ".venv-sim" / "bin" / "python"  # the simulator's own venv (httpx); the load test venv is .venv-load

COMPOSE = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.loadtest.yml"]
API_URL = "http://127.0.0.1:8100"
LOAD_DB = "ridehail_load"
TEMPLATE_DB = "ridehail_load_template"

# The API refuses .local (a reserved name, like .test), so the accounts use an example.com subdomain, as the simulator does.
RIDER_EMAIL = "rider{n}@loadtest.example.com"  # n from 1 to SEED_RIDERS; the odd ones are funded
ADMIN_EMAIL = "loadadmin{n}@loadtest.example.com"  # 0 is the harness and the simulator's admin, 1 and 2 are the AdminViewers
FUNDED_PAISE = 500_000  # enough for many wallet rides: the most one ride can reserve is a few hundred rupees
# A fixed password of throwaway accounts that exist only in the isolated load database. LOADTEST_PASSWORD overrides it.
DEFAULT_PASSWORD = "loadtest-pass-1"

# Start and drop-off of a ride: snapped to roads by OSRM, route distance 2.8 to 8.0 km (median 6.0), the pickups within 4 km of
# the city center where the simulated fleet drives. Generated once and fixed, so every run asks for the same trips.
POINT_PAIRS = [
    (12.95124, 77.59299, 12.94522, 77.57367),  # 3.1 km
    (12.98905, 77.60507, 13.0045, 77.55512),  # 7.7 km
    (12.9459, 77.57499, 12.98704, 77.54923),  # 7.6 km
    (12.99216, 77.57505, 13.01829, 77.54577),  # 5.9 km
    (12.95726, 77.60292, 12.92176, 77.58206),  # 7.0 km
    (12.97041, 77.60047, 12.96754, 77.57129),  # 4.3 km
    (12.9454, 77.58578, 12.9342, 77.61893),  # 5.7 km
    (12.97484, 77.58836, 12.98195, 77.55669),  # 4.8 km
    (12.98585, 77.56106, 12.98088, 77.54297),  # 2.8 km
    (12.99925, 77.61731, 12.97449, 77.59835),  # 4.9 km
    (12.97425, 77.56196, 12.95687, 77.51632),  # 6.9 km
    (12.96981, 77.56708, 12.9202, 77.58584),  # 7.7 km
    (12.94929, 77.58832, 12.9842, 77.58188),  # 6.0 km
    (12.9758, 77.58292, 13.02887, 77.57689),  # 6.7 km
    (12.96364, 77.60232, 12.99321, 77.58506),  # 5.7 km
    (12.98511, 77.6148, 12.94064, 77.59976),  # 7.2 km
    (12.97456, 77.56782, 12.95771, 77.52513),  # 6.8 km
    (12.97457, 77.57802, 12.93388, 77.57909),  # 5.7 km
    (13.00504, 77.58297, 12.99294, 77.53221),  # 7.4 km
    (12.9541, 77.58889, 12.90536, 77.63147),  # 8.0 km
    (12.98713, 77.62484, 13.00061, 77.66216),  # 6.8 km
    (12.98515, 77.57619, 12.99401, 77.53325),  # 7.2 km
    (12.95819, 77.57558, 12.97605, 77.53477),  # 6.4 km
    (12.98396, 77.58248, 13.00387, 77.54854),  # 5.3 km
    (12.97137, 77.61557, 12.93277, 77.6329),  # 5.8 km
    (13.0002, 77.59843, 13.01461, 77.57155),  # 5.2 km
    (12.95694, 77.56855, 12.93847, 77.58771),  # 4.6 km
    (12.9792, 77.58506, 13.00564, 77.6264),  # 7.1 km
    (12.95891, 77.58578, 12.97196, 77.56492),  # 3.9 km
    (12.94816, 77.57309, 12.97792, 77.55661),  # 6.6 km
    (12.96342, 77.62939, 12.94638, 77.62899),  # 5.4 km
    (12.95021, 77.59661, 12.8998, 77.62863),  # 7.6 km
    (12.97932, 77.56616, 13.00735, 77.56919),  # 4.9 km
    (12.98939, 77.57908, 12.97567, 77.58107),  # 2.9 km
    (12.97276, 77.57227, 12.94527, 77.54536),  # 5.4 km
    (13.00161, 77.60255, 12.98434, 77.62888),  # 4.6 km
    (12.98674, 77.59374, 12.93693, 77.58629),  # 7.6 km
    (12.99672, 77.57766, 12.99743, 77.53478),  # 5.3 km
    (12.95903, 77.56037, 12.96431, 77.58321),  # 3.1 km
    (12.95093, 77.59751, 12.98464, 77.58344),  # 6.6 km
    (12.99682, 77.57466, 13.03885, 77.58646),  # 6.3 km
    (12.97284, 77.56676, 12.93071, 77.55439),  # 6.3 km
    (12.94219, 77.59276, 12.93737, 77.618),  # 4.6 km
    (12.9727, 77.60472, 12.91989, 77.59441),  # 7.2 km
    (12.97081, 77.55913, 13.01711, 77.53149),  # 7.7 km
    (12.95899, 77.57726, 12.94875, 77.52008),  # 7.1 km
    (12.93741, 77.60401, 12.95164, 77.59065),  # 3.1 km
    (12.99922, 77.61474, 12.98005, 77.64363),  # 5.7 km
]


def password() -> str:
    return os.environ.get("LOADTEST_PASSWORD", DEFAULT_PASSWORD)


def read_env() -> dict[str, str]:
    """KEY=VALUE lines of the repository's .env (the same file the compose services read)."""
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def compose(*args: str, input: str | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
    """docker compose with both files, run from the repository root. The command line is never part of an error, because
    some commands carry the throwaway password: a failure raises with the exit code and stderr only."""
    result = subprocess.run([*COMPOSE, *args], cwd=ROOT, input=input, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"docker compose {args[0]} failed (exit {result.returncode}): {result.stderr.strip()[-500:]}")
    return result


def psql(sql: str, db: str = LOAD_DB, timeout: float = 60) -> list[str]:
    """Runs SQL in postgres-load and returns the non-empty output lines, fields separated by |."""
    user = read_env()["POSTGRES_USER"]
    result = compose("exec", "-T", "postgres-load", "psql", "-U", user, "-d", db, "-v", "ON_ERROR_STOP=1", "-At", "-F", "|",
                     input=sql, timeout=timeout)
    return [line for line in result.stdout.splitlines() if line.strip()]


def wait_for_health(timeout: float = 90) -> float:
    """Waits until backend-load answers /health with 200 and returns the seconds it took."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        try:
            with urllib.request.urlopen(f"{API_URL}/health", timeout=2) as response:
                if response.status == 200:
                    return time.monotonic() - started
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"backend-load did not become healthy within {timeout} s")
