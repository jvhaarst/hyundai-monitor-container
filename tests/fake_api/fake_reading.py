"""The fixture values the fake API serves, in a module both sides can import.

The fake package itself cannot be imported by the test process to read these:
test_api_surface.py imports the *real* `hyundai_kia_connect_api` and that lands
in sys.modules first, so a later `from hyundai_kia_connect_api import READING`
picks up the real package and fails. This module has a name nothing else
claims, so both the fake and the tests can import it.
"""

from datetime import datetime, timezone

# The single cached reading the fake serves. The tests assert the CSV row that
# the collector writes against exactly these values.
READING = {
    "vin": "FAKEVIN0000000001",
    "odometer": 70879.4,
    "car_battery_percentage": 72,
    "ev_battery_percentage": 79,
    "ev_driving_range": 347,
    "location_longitude": 5.795908,
    "location_latitude": 51.982111,
    "last_updated_at": datetime(2026, 10, 4, 6, 22, 19, tzinfo=timezone.utc),
    "geocode_name": "72, Bachlaan, Doorwerth, Nederland",
}

# Written into the working directory if force_refresh_all_vehicles_states() is
# ever called, so a test can assert on it even when the collector swallows the
# exception.
WAKE_MARKER = "FORCE_REFRESH_WAS_CALLED"

# Number of logins to reject before succeeding, for driving the error path in
# handle_exception() without an account.
FAIL_TIMES_ENV = "FAKE_API_FAIL_TIMES"
