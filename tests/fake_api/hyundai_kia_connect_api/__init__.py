"""A stand-in for hyundai_kia_connect_api that serves one fixed cached reading.

Put this directory first on PYTHONPATH and monitor.py runs end to end with no
network, no credentials and no account: enough to prove that a reading becomes
exactly one CSV row, and that nothing in the collector reaches for the call
that wakes the car.

This is not a model of the real API. The test that checks the real API still
has the attributes and methods the collector uses is test_api_surface.py, which
imports the installed package; the two are meant to be read together.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timezone
from typing import Any

from fake_reading import FAIL_TIMES_ENV, READING, WAKE_MARKER


class CarWokenError(AssertionError):
    """Raised when something tries to wake the car."""


@dataclass
class Vehicle:
    """Only the attributes monitor.py reads."""

    id: str = "fake-vehicle-id"
    name: str = "IONIQ 5"
    model: str = "IONIQ 5"
    VIN: str = READING["vin"]
    registration_date: str = "2023-01-01"
    odometer: float = READING["odometer"]
    odometer_unit: int = 1  # 1 = km
    car_battery_percentage: int = READING["car_battery_percentage"]
    engine_is_running: bool = False
    ev_battery_percentage: int = READING["ev_battery_percentage"]
    ev_battery_is_charging: bool = False
    ev_battery_is_plugged_in: int = 0
    ev_driving_range: int = READING["ev_driving_range"]
    ev_driving_range_unit: int = 1
    location_latitude: float = READING["location_latitude"]
    location_longitude: float = READING["location_longitude"]
    location_last_updated_at: datetime = READING["last_updated_at"]
    last_updated_at: datetime = READING["last_updated_at"]
    timezone: Any = timezone.utc
    geocode: tuple = (READING["geocode_name"], None)
    daily_stats: list = field(default_factory=list)
    day_trip_info: Any = None
    month_trip_info: Any = None
    data: dict = field(default_factory=dict)
    _location_last_set_time: datetime = READING["last_updated_at"]

    @property
    def location(self) -> tuple:
        return (self.location_latitude, self.location_longitude)


_ATTEMPT_FILE = ".fake_api_attempts"


def _next_attempt() -> int:
    """Count attempts across processes, since the collector may re-exec."""
    try:
        with open(_ATTEMPT_FILE, "r", encoding="utf-8") as handle:
            attempts = int(handle.read().strip() or "0")
    except (OSError, ValueError):
        attempts = 0
    attempts += 1
    with open(_ATTEMPT_FILE, "w", encoding="utf-8") as handle:
        handle.write(str(attempts))
    return attempts


class VehicleManager:
    """Serves the cached reading above and refuses to wake the car."""

    def __init__(self, **kwargs: Any) -> None:
        fail_times = int(os.environ.get(FAIL_TIMES_ENV, "0"))
        if fail_times and _next_attempt() <= fail_times:
            raise AuthenticationError("fake credentials rejected")
        self.init_kwargs = kwargs
        self.vehicles = {"fake-vehicle-id": Vehicle()}
        self.token = "fake-token"

    def check_and_refresh_token(self) -> bool:
        return True

    def update_all_vehicles_with_cached_state(self) -> None:
        """The only read the collector is allowed to make."""

    def update_day_trip_info(self, *_args: Any, **_kwargs: Any) -> None:
        """No trip info in the fixture."""

    def update_month_trip_info(self, *_args: Any, **_kwargs: Any) -> None:
        """No trip info in the fixture."""

    def force_refresh_all_vehicles_states(self) -> None:
        # Leave evidence on disk as well as raising: the collector catches
        # broad exceptions in places, and the test must still see this.
        with open(WAKE_MARKER, "w", encoding="utf-8") as marker:
            marker.write("force_refresh_all_vehicles_states() was called\n")
        raise CarWokenError(
            "force_refresh_all_vehicles_states() was called, which wakes the "
            "car and drains its 12 V battery"
        )


class _Error(Exception):
    """Base for the fake exception hierarchy."""


class HyundaiKiaException(_Error):
    pass


class AuthenticationError(HyundaiKiaException):
    pass


class RateLimitingError(HyundaiKiaException):
    pass


class NoDataFound(HyundaiKiaException):
    pass


class DuplicateRequestError(HyundaiKiaException):
    pass


class RequestTimeoutError(HyundaiKiaException):
    pass


class InvalidAPIResponseError(HyundaiKiaException):
    pass


class APIError(HyundaiKiaException):
    pass


class ServiceTemporaryUnavailable(HyundaiKiaException):
    pass


class _ExceptionsModule:
    """monitor.py does `from hyundai_kia_connect_api import exceptions`."""

    HyundaiKiaException = HyundaiKiaException
    AuthenticationError = AuthenticationError
    RateLimitingError = RateLimitingError
    NoDataFound = NoDataFound
    DuplicateRequestError = DuplicateRequestError
    RequestTimeoutError = RequestTimeoutError
    InvalidAPIResponseError = InvalidAPIResponseError
    APIError = APIError
    ServiceTemporaryUnavailable = ServiceTemporaryUnavailable


exceptions = _ExceptionsModule()

__all__ = [
    "Vehicle",
    "VehicleManager",
    "exceptions",
    "READING",
    "WAKE_MARKER",
    "FAIL_TIMES_ENV",
]

# Make `import hyundai_kia_connect_api.exceptions` work too.
import sys as _sys  # noqa: E402

_sys.modules[__name__ + ".exceptions"] = exceptions  # type: ignore[assignment]

assert os.path.basename(os.path.dirname(__file__)) == "hyundai_kia_connect_api"
