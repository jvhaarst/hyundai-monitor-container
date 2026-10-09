"""End-to-end runs of monitor.py against the fake API.

These cover the behaviour that matters in the cluster and that no amount of
grepping can prove: one reading becomes exactly one appended row, the existing
history is never rewritten, the no-wake guard stops the process with exit code
3, and nothing on the normal path reaches for the call that wakes the car.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "fake_api"))

from conftest import lines  # noqa: E402
from fake_reading import READING, WAKE_MARKER  # noqa: E402


def test_one_cached_reading_appends_exactly_one_row(
    data_dir: Path, write_cfg, run_collector
) -> None:
    write_cfg()
    before = lines(data_dir / "monitor.csv")

    result = run_collector()

    assert result.returncode == 0, result.stdout + result.stderr
    after = lines(data_dir / "monitor.csv")
    assert len(after) == len(before) + 1, (
        f"expected exactly one new row, got {len(after) - len(before)}\n"
        f"{result.stdout}"
    )

    row = [field.strip() for field in after[-1].split(",")]
    # datetime, longitude, latitude, engineOn, 12V%, odometer, SOC%, charging,
    # plugged, address..., EV range
    assert row[0].startswith("2026-10-04 06:22:19")
    assert row[1] == str(READING["location_longitude"])
    assert row[2] == str(READING["location_latitude"])
    assert row[3] == "False"
    assert row[4] == str(READING["car_battery_percentage"])
    assert row[5] == str(READING["odometer"])
    assert row[6] == str(READING["ev_battery_percentage"])
    assert after[-1].rstrip().endswith(str(READING["ev_driving_range"]))


def test_history_is_only_appended_to(data_dir: Path, write_cfg, run_collector) -> None:
    write_cfg()
    first_rows = {
        name: lines(data_dir / name)[:2]
        for name in ("monitor.csv", "monitor.dailystats.csv", "monitor.tripinfo.csv")
    }

    assert run_collector().returncode == 0

    for name, expected in first_rows.items():
        assert lines(data_dir / name)[:2] == expected, (
            f"{name} was rewritten rather than appended to. A truncation here "
            "looks exactly like a successful start."
        )


def test_lastrun_is_refreshed(data_dir: Path, write_cfg, run_collector) -> None:
    write_cfg()
    before = (data_dir / "monitor.lastrun").read_text(encoding="utf-8")

    assert run_collector().returncode == 0

    after = (data_dir / "monitor.lastrun").read_text(encoding="utf-8")
    assert after != before
    assert after.startswith("last run"), after
    # The staleness alert reads the timestamp off this first line, so what matters
    # is that it is FRESH. Asserting a literal date made this pass only on the day
    # it was written: from 2026-10-05 it failed every run and blocked every PR.
    first = after.splitlines()[0]
    stamp = first.split(";", 1)[1].strip()
    written = datetime.strptime(stamp[:16], "%Y-%m-%d %H:%M")
    age = abs((datetime.now() - written).total_seconds())
    assert age < 3600, f"lastrun timestamp is {age:.0f}s away from now: {first}"


def test_car_is_never_woken(data_dir: Path, write_cfg, run_collector) -> None:
    write_cfg()

    result = run_collector()

    assert result.returncode == 0
    assert not (data_dir / WAKE_MARKER).exists(), (
        "force_refresh_all_vehicles_states() was called on the normal path"
    )
    combined = result.stdout + result.stderr
    assert "force_refresh" not in combined, combined


def test_force_sync_workaround_refuses_to_start(
    data_dir: Path, write_cfg, run_collector
) -> None:
    """Patch 02. Exit 3 before any network call, with the reason in the log."""
    write_cfg(force_sync="True")
    before = lines(data_dir / "monitor.csv")

    result = run_collector()

    assert result.returncode == 3, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "REFUSING TO START" in combined
    assert "force_refresh_all_vehicles_states()" in combined
    assert "12 V battery" in combined
    # It must stop before writing anything at all.
    assert lines(data_dir / "monitor.csv") == before
    assert not (data_dir / WAKE_MARKER).exists()


def test_percent_in_password_survives_the_config_read(
    data_dir: Path, write_cfg, run_collector
) -> None:
    """Patch 01, from the outside: the cfg holds `pa%ss%word`.

    Without interpolation=None this run dies in ConfigParser before it ever
    reaches the fake API, so a successful run is the assertion.
    """
    cfg = write_cfg()
    assert "pa%ss%word" in cfg.read_text(encoding="utf-8")

    result = run_collector()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "InterpolationSyntaxError" not in result.stdout + result.stderr


def test_exception_class_is_logged(data_dir: Path, write_cfg, run_collector) -> None:
    """Patch 03: handle_exception() must name the exception class.

    The fake rejects the first login, which is the only way to reach
    handle_exception() without an account. Without the patch the log reads
    "Exception: fake credentials rejected" and an AuthenticationError is
    indistinguishable from a RateLimitingError -- the ambiguity that cost two
    months of data, twice.
    """
    write_cfg()

    result = run_collector(fail_times=1)

    combined = result.stdout + result.stderr
    assert "Exception: AuthenticationError: fake credentials rejected" in combined, (
        combined[-2000:]
    )
    # The retry after the rejected login still produces the reading.
    assert result.returncode == 0, combined[-2000:]
    assert len(lines(data_dir / "monitor.csv")) == 3
