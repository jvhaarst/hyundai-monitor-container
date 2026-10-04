"""Make the collector's retry sleeps instant during tests.

monitor.py retries a failed login 15 times with `time.sleep(60)` between
attempts, which is right in production and useless in a test. Python imports
sitecustomize automatically at interpreter startup if it is on sys.path, and
the fake API directory is put on PYTHONPATH by the test fixtures, so this takes
effect for the collector subprocess and nothing else.

Guarded by an environment variable so that merely having this directory on the
path cannot disarm a real deployment's backoff.
"""

import os

if os.environ.get("TEST_NO_SLEEP") == "1":
    import time

    _real_sleep = time.sleep

    def _no_sleep(_seconds: float) -> None:
        """Return at once, whatever was asked for."""

    time.sleep = _no_sleep  # type: ignore[assignment]
