"""The exporter is what the staleness alert depends on, so it gets tested too.

The alert reads one number: hyundai_monitor_last_run_timestamp_seconds. If the
exporter silently stops publishing it -- a changed monitor.lastrun format, a
timezone slip -- the alert goes quiet rather than firing, which is the worst
failure mode available. These tests pin the parsing and the output format.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

EXPORTER_PY = Path(os.environ.get("EXPORTER_PY", "/app/exporter.py"))

LASTRUN = """last run      ; 2026-10-04 10:29 Sun
vin           ; FAKEVIN0000000001
vehicle update; 2026-10-04 10:19:22
gps update    ; 2026-10-03 12:59:01
2026-10-04 10:19:22+00:00, 5.795908, 51.982111, False, 72, 70879.4, 79, False, 2, x, 350
"""


@pytest.fixture
def exporter(tmp_path: Path, monkeypatch):
    """Import exporter.py against a throwaway data directory."""
    monkeypatch.setenv("MONITOR_DATA_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("hm_exporter", EXPORTER_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["hm_exporter"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seeded(tmp_path: Path, exporter):
    (tmp_path / "monitor.lastrun").write_text(LASTRUN, encoding="utf-8")
    (tmp_path / "monitor.csv").write_text("header\nrow1\nrow2\n", encoding="utf-8")
    (tmp_path / "monitor.dailystats.csv").write_text("header\nrow1\n", encoding="utf-8")
    (tmp_path / "monitor.tripinfo.csv").write_text("header\nrow1\n", encoding="utf-8")
    return exporter


def test_last_run_is_parsed_as_utc(seeded) -> None:
    last_run, vehicle_update = seeded.read_lastrun()
    expected = datetime(2026, 10, 4, 10, 29, tzinfo=timezone.utc).timestamp()
    assert last_run == expected
    assert (
        vehicle_update
        == datetime(2026, 10, 4, 10, 19, 22, tzinfo=timezone.utc).timestamp()
    )


def test_metrics_output_carries_the_alerting_metric(seeded) -> None:
    body = seeded.render().decode("utf-8")
    assert "hyundai_monitor_up 1" in body
    assert "hyundai_monitor_last_run_timestamp_seconds 1791109740" in body
    assert "hyundai_monitor_last_vehicle_update_timestamp_seconds 1791109162" in body
    # Row counts exclude the header.
    assert 'hyundai_monitor_csv_rows{file="monitor.csv"} 2' in body
    assert 'hyundai_monitor_csv_rows{file="monitor.tripinfo.csv"} 1' in body
    # Every metric must be declared, or VictoriaMetrics drops the type.
    for metric in (
        "hyundai_monitor_up",
        "hyundai_monitor_last_run_timestamp_seconds",
        "hyundai_monitor_csv_rows",
    ):
        assert f"# HELP {metric} " in body
        assert f"# TYPE {metric} gauge" in body


def test_missing_lastrun_omits_the_metric_rather_than_lying(
    tmp_path: Path, exporter
) -> None:
    """No file means no sample, so `absent()` can catch it.

    Publishing 0 instead would read as 1 January 1970 and make the staleness
    alert fire with a nonsense age, or worse, look plausible.
    """
    body = exporter.render().decode("utf-8")
    samples = [
        line
        for line in body.splitlines()
        if line.startswith("hyundai_monitor_last_run_timestamp_seconds")
    ]
    assert samples == []
    # The declaration stays, so the metric is a known-but-absent series rather
    # than an unknown name.
    assert "# TYPE hyundai_monitor_last_run_timestamp_seconds gauge" in body


def test_unreadable_lastrun_does_not_raise(tmp_path: Path, exporter) -> None:
    """The sidecar must not be able to take the pod down."""
    (tmp_path / "monitor.lastrun").write_text("total nonsense\n", encoding="utf-8")
    assert exporter.read_lastrun() == (None, None)
    assert b"hyundai_monitor_up 1" in exporter.render()


def test_row_counts_are_cached_until_the_file_changes(seeded, tmp_path: Path) -> None:
    assert seeded.count_rows("monitor.csv") == 2
    # Appending changes size and mtime, so the cache must not be trusted.
    with (tmp_path / "monitor.csv").open("a", encoding="utf-8") as handle:
        handle.write("row3\n")
    assert seeded.count_rows("monitor.csv") == 3


def test_a_held_keepalive_connection_does_not_block_other_clients(
    seeded, tmp_path: Path
) -> None:
    """The regression test for 123 restarts in eleven hours.

    vmagent keeps its scrape connection open between scrapes. On a
    single-threaded server that parks the only thread inside
    handle_one_request() forever, so every other client -- the readiness probe
    above all -- completes the TCP handshake in the kernel and then waits for a
    response that never comes. It presents as "context deadline exceeded" and
    looks exactly like a network fault, which is how it was misdiagnosed.

    So: hold one keep-alive connection open mid-conversation, then prove a
    second client still gets served.

    Both client sockets are closed before `server.shutdown()`, because
    `shutdown()` waits for the serve_forever loop to come round again. On a
    regressed server that loop is blocked on the held socket, so closing first
    is what makes this test *fail* in five seconds instead of hanging CI.
    """
    import http.client
    import threading

    server = seeded.ThreadingHTTPServer(("127.0.0.1", 0), seeded.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    squatter = probe = None
    try:
        # A scraper that has finished one request and is holding the socket.
        squatter = http.client.HTTPConnection(host, port, timeout=5)
        squatter.request("GET", "/metrics")
        assert squatter.getresponse().status == 200
        assert squatter.sock is not None, "connection must still be open"

        # A second client, on its own connection, while the first is still open.
        probe = http.client.HTTPConnection(host, port, timeout=5)
        try:
            probe.request("GET", "/metrics")
            response = probe.getresponse()
        except OSError as ex:
            # socket.timeout while the server ignores the connection, or
            # ConnectionResetError once the test tears the squatter down -- both
            # mean the same thing, and both are the bug being pinned.
            pytest.fail(f"second client was not served while one was open: {ex!r}")
        assert response.status == 200
        assert b"hyundai_monitor_up 1" in response.read()
    finally:
        for connection in (probe, squatter):
            if connection is not None:
                connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
