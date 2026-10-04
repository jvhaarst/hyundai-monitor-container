#!/usr/bin/env python3
"""Publish the collector's progress as Prometheus metrics.

The host service was watched by a timer that read `monitor.lastrun` off disk and
mailed when it went stale. In the cluster nothing can read that file except a
process in this pod, because the volume is ReadWriteOnce. So this exporter runs
as a sidecar, reads the same file, and turns it into the one number an alert
needs: when the collector last completed a run.

Deliberately stdlib only, single-threaded, and no dependency on the collector
itself. It must never be the reason the pod restarts, so every read failure
becomes a metric rather than an exception.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

DATA_DIR = Path(os.environ.get("MONITOR_DATA_DIR", "/data"))
LASTRUN = DATA_DIR / "monitor.lastrun"
PORT = int(os.environ.get("EXPORTER_PORT", "9091"))

CSV_FILES = ("monitor.csv", "monitor.dailystats.csv", "monitor.tripinfo.csv")

# `last run      ; 2026-10-04 10:29 Sun` and `vehicle update; 2026-10-04 01:58:31`
LASTRUN_RE = re.compile(r"^last run\s*;\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})")
VEHICLE_RE = re.compile(r"^vehicle update\s*;\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s: %(levelname)s: exporter: %(message)s",
    datefmt="%Y%m%d %H:%M:%S",
)

# Counting lines means reading monitor.csv, which is over a megabyte. Cache on
# (size, mtime) so a 30-second scrape interval does not re-read it every time.
_line_cache: dict[str, tuple[tuple[int, float], int]] = {}


def _parse_utc(text: str) -> float:
    """Parse a naive timestamp as UTC.

    The collector writes local time and the deployment pins TZ=Etc/UTC, which is
    deliberate and documented: the CSV history is UTC. Reading these as UTC is
    therefore correct, and if the timezone were ever changed the staleness
    metric would be wrong by the offset -- which is one more reason not to.
    """
    fmt = "%Y-%m-%d %H:%M:%S" if text.count(":") == 2 else "%Y-%m-%d %H:%M"
    return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp()


def read_lastrun() -> tuple[float | None, float | None]:
    """Return (last run, last vehicle update) as epoch seconds."""
    last_run = vehicle_update = None
    try:
        with LASTRUN.open("r", encoding="utf-8") as handle:
            for line in handle:
                match = LASTRUN_RE.match(line)
                if match:
                    last_run = _parse_utc(match.group(1))
                    continue
                match = VEHICLE_RE.match(line)
                if match:
                    vehicle_update = _parse_utc(match.group(1))
    except FileNotFoundError:
        logging.warning("%s does not exist yet", LASTRUN)
    except (OSError, ValueError) as ex:
        logging.warning("cannot read %s: %s: %s", LASTRUN, type(ex).__name__, ex)
    return last_run, vehicle_update


def count_rows(name: str) -> int | None:
    """Data rows in a CSV, excluding its header."""
    path = DATA_DIR / name
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (stat.st_size, stat.st_mtime)
    cached = _line_cache.get(name)
    if cached and cached[0] == key:
        return cached[1]
    try:
        with path.open("rb") as handle:
            lines = sum(1 for line in handle if line.strip())
    except OSError as ex:
        logging.warning("cannot count %s: %s", path, ex)
        return None
    rows = max(lines - 1, 0)
    _line_cache[name] = (key, rows)
    return rows


def render() -> bytes:
    last_run, vehicle_update = read_lastrun()
    out: list[str] = [
        "# HELP hyundai_monitor_up Whether the exporter can read the data directory.",
        "# TYPE hyundai_monitor_up gauge",
        f"hyundai_monitor_up {1 if DATA_DIR.is_dir() else 0}",
        "# HELP hyundai_monitor_last_run_timestamp_seconds When the collector last"
        " completed a run, from monitor.lastrun.",
        "# TYPE hyundai_monitor_last_run_timestamp_seconds gauge",
    ]
    if last_run is not None:
        out.append(f"hyundai_monitor_last_run_timestamp_seconds {last_run:.0f}")
    out += [
        "# HELP hyundai_monitor_last_vehicle_update_timestamp_seconds The timestamp"
        " of the newest cached reading Hyundai returned.",
        "# TYPE hyundai_monitor_last_vehicle_update_timestamp_seconds gauge",
    ]
    if vehicle_update is not None:
        out.append(
            f"hyundai_monitor_last_vehicle_update_timestamp_seconds {vehicle_update:.0f}"
        )
    out += [
        "# HELP hyundai_monitor_csv_rows Data rows per CSV, excluding the header."
        " These only ever grow; a drop means the history was truncated.",
        "# TYPE hyundai_monitor_csv_rows gauge",
    ]
    for name in CSV_FILES:
        rows = count_rows(name)
        if rows is not None:
            out.append(f'hyundai_monitor_csv_rows{{file="{name}"}} {rows}')
    return ("\n".join(out) + "\n").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] not in ("/metrics", "/"):
            self.send_error(404)
            return
        try:
            body = render()
        except Exception as ex:  # pylint: disable=broad-except
            # Never let a bad read take the sidecar down with the collector.
            logging.error("render failed: %s: %s", type(ex).__name__, ex)
            self.send_error(500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        """Quiet: a scrape every 30 seconds is not news."""


def main() -> None:
    logging.info("serving metrics on :%d from %s", PORT, DATA_DIR)
    server = HTTPServer(("", PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
