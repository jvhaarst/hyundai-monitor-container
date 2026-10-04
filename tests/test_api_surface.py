"""The contract between monitor.py and the installed hyundai_kia_connect_api.

This is the test that earns its keep on a dependency bump. It imports the real
package -- no network, no credentials -- and asserts that every attribute,
method and exception the collector reaches for still exists. A release that
renames `ev_battery_percentage` or drops `RateLimitingError` fails here, in the
Renovate pull request, instead of at 03:00 in a pod that logs one line and
stops collecting.

The lists below were derived from monitor.py itself:

    grep -oE "vehicle\\.[a-z_0-9]+" monitor.py | sort -u
    grep -oE "MANAGER\\.[a-z_0-9]+\\(" monitor.py | sort -u
    grep -oE "exceptions\\.[A-Za-z]+" monitor.py | sort -u
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from hyundai_kia_connect_api import Vehicle, VehicleManager, exceptions

# Read off the vehicle to build a CSV row.
VEHICLE_ATTRIBUTES = [
    "_location_last_set_time",
    "car_battery_percentage",
    "daily_stats",
    "day_trip_info",
    "engine_is_running",
    "ev_battery_is_charging",
    "ev_battery_is_plugged_in",
    "ev_battery_percentage",
    "ev_driving_range",
    "geocode",
    "id",
    "last_updated_at",
    "location",
    "location_last_updated_at",
    "location_latitude",
    "location_longitude",
    "month_trip_info",
    "odometer",
    "odometer_unit",
    "timezone",
    # Used for the per-vehicle filename when more than one car is configured.
    "VIN",
]

MANAGER_METHODS = [
    "check_and_refresh_token",
    "update_all_vehicles_with_cached_state",
    "update_day_trip_info",
    "update_month_trip_info",
]

# Passed by keyword when the collector logs in. A rename here is a silent
# TypeError at the one moment that matters: the daily login.
MANAGER_INIT_KWARGS = [
    "brand",
    "geocode_api_enable",
    "geocode_api_key",
    "geocode_api_use_email",
    "geocode_provider",
    "language",
    "password",
    "pin",
    "region",
    "username",
]

# Every exception monitor.py catches by name in handle_exception().
EXCEPTIONS = [
    "APIError",
    "AuthenticationError",
    "DuplicateRequestError",
    "HyundaiKiaException",
    "InvalidAPIResponseError",
    "NoDataFound",
    "RateLimitingError",
    "RequestTimeoutError",
]


def _vehicle_names() -> set[str]:
    names = set(dir(Vehicle))
    if dataclasses.is_dataclass(Vehicle):
        names |= {f.name for f in dataclasses.fields(Vehicle)}
    return names


@pytest.mark.parametrize("attribute", VEHICLE_ATTRIBUTES)
def test_vehicle_has_attribute(attribute: str) -> None:
    assert attribute in _vehicle_names(), (
        f"Vehicle no longer has '{attribute}'. monitor.py reads it to build a "
        "CSV row; find its new name before bumping the API client."
    )


@pytest.mark.parametrize("method", MANAGER_METHODS)
def test_manager_has_method(method: str) -> None:
    assert callable(getattr(VehicleManager, method, None)), (
        f"VehicleManager.{method}() is gone. monitor.py calls it on every run."
    )


@pytest.mark.parametrize("kwarg", MANAGER_INIT_KWARGS)
def test_manager_accepts_login_kwarg(kwarg: str) -> None:
    parameters = inspect.signature(VehicleManager.__init__).parameters
    assert kwarg in parameters, (
        f"VehicleManager(...) no longer accepts '{kwarg}'. The collector passes "
        "it by keyword when logging in."
    )


@pytest.mark.parametrize("name", EXCEPTIONS)
def test_exception_exists(name: str) -> None:
    exception = getattr(exceptions, name, None)
    assert exception is not None, (
        f"exceptions.{name} is gone. monitor.py catches it by name, so its "
        "absence turns a handled error into a crash."
    )
    assert isinstance(exception, type) and issubclass(exception, BaseException)


def test_cached_state_update_is_still_the_read_path() -> None:
    """The no-wake property depends on these being two different calls.

    The collector must only ever call update_all_vehicles_with_cached_state().
    If the API ever merged the cached read into the force-refresh call, the
    guard in monitor.py would still pass while the car got woken anyway.
    """
    cached = getattr(VehicleManager, "update_all_vehicles_with_cached_state", None)
    forced = getattr(VehicleManager, "force_refresh_all_vehicles_states", None)
    assert callable(cached)
    assert callable(forced)
    assert cached is not forced, (
        "The cached read and the force refresh are now the same function. "
        "Every read would wake the car and drain its 12 V battery. Do not ship "
        "this version."
    )
