"""Fixtures for running the collector in a throwaway working directory.

monitor.py has no `if __name__ == "__main__"` guard -- importing it runs it and
ends in sys.exit() -- so every test runs it as a subprocess. That also keeps
one test's module-level state out of the next one's.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Inside the image. Overridable so the suite can also be pointed at a checkout.
MONITOR_PY = Path(os.environ.get("MONITOR_PY", "/app/monitor.py"))
FAKE_API_PATH = Path(__file__).parent / "fake_api"

# Seeds that stand in for the two-year history: a header plus the oldest row of
# each file, taken from the real data. Tests assert these survive untouched.
SEED_MONITOR_CSV = (
    "datetime, longitude, latitude, engineOn, 12V%, odometer, SOC%, charging, "
    "plugged, address, EV range\n"
    "2024-04-01 21:14:36+02:00, 5.795819, 51.982169, False, 94, 26850.1, 16, "
    "False, 0, 76; Bachlaan; Doorwerth; Nederland, 47\n"
)
SEED_DAILYSTATS_CSV = (
    "date, distance, distance_unit, total_consumed, regenerated_energy, "
    "engine_consumption, climate_consumption, onboard_electronics_consumption, "
    "battery_care_consumption\n"
    "20240303 22:55, 71, km, 10461, 4797,  9365, 536, 560, 0\n"
)
SEED_TRIPINFO_CSV = (
    "Date, Start time, Drive time, Idle time, Distance, Avg speed, Max speed\n"
    "20240102,113054,7,2,3,44,92\n"
)

CFG_TEMPLATE = """[monitor]
region = 1
brand = 2
username = test@example.invalid
password = pa%ss%word
pin =
use_geocode = True
use_geocode_email = True
language = en
odometer_metric = km
include_regenerate_in_consumption = False
consumption_efficiency_factor_dailystats = 1.0
consumption_efficiency_factor_summary = 1.0
monitor_infinite = False
monitor_infinite_interval_minutes = 15
monitor_execute_commands_when_something_written_or_error =
monitor_force_sync_when_odometer_different_location_workaround = {force_sync}
monitor_force_sync_max_count = 10

[MQTT]
send_to_mqtt = False
mqtt_broker_hostname = localhost
mqtt_broker_port = 1883
mqtt_broker_username =
mqtt_broker_password =
mqtt_broker_cabundle =
mqtt_main_topic = hyundai_kia_connect_monitor

[Domoticz]
send_to_domoticz = False
domot_url = http://127.0.0.1:8081

monitor_monitor_datetime = 0
monitor_monitor_longitude = 0
monitor_monitor_latitude = 0
monitor_monitor_engineon = 0
"""


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A working directory seeded with the three CSVs and monitor.lastrun."""
    (tmp_path / "monitor.csv").write_text(SEED_MONITOR_CSV, encoding="utf-8")
    (tmp_path / "monitor.dailystats.csv").write_text(
        SEED_DAILYSTATS_CSV, encoding="utf-8"
    )
    (tmp_path / "monitor.tripinfo.csv").write_text(SEED_TRIPINFO_CSV, encoding="utf-8")
    (tmp_path / "monitor.lastrun").write_text(
        "last run      ; 2024-04-01 21:14 Mon\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def write_cfg(data_dir: Path):
    """Write monitor.cfg into the working directory."""

    def _write(force_sync: str = "False") -> Path:
        cfg = data_dir / "monitor.cfg"
        cfg.write_text(CFG_TEMPLATE.format(force_sync=force_sync), encoding="utf-8")
        return cfg

    return _write


@pytest.fixture
def run_collector(data_dir: Path):
    """Run monitor.py once in the working directory, with the fake API."""

    def _run(use_fake_api: bool = True, timeout: int = 120, fail_times: int = 0):
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["TZ"] = "Etc/UTC"
        # sitecustomize.py in the fake API directory turns the collector's
        # 60-second retry sleeps into no-ops when this is set.
        env["TEST_NO_SLEEP"] = "1"
        if fail_times:
            env["FAKE_API_FAIL_TIMES"] = str(fail_times)
        if use_fake_api:
            # First on the path, so it shadows the installed package.
            env["PYTHONPATH"] = os.pathsep.join(
                [str(FAKE_API_PATH), env.get("PYTHONPATH", "")]
            ).rstrip(os.pathsep)
        return subprocess.run(
            [sys.executable, "-u", str(MONITOR_PY)],
            cwd=data_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    return _run


def lines(path: Path) -> list[str]:
    """Non-empty lines of a file."""
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]
