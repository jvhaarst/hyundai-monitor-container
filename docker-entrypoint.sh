#!/usr/bin/env bash
#
# Assemble monitor.cfg from mounted secrets plus environment, then run the
# collector.
#
# Why assemble instead of mounting a finished cfg: the credentials belong in a
# Secret, the behaviour flags belong in Helm values, and the file has to end up
# as a single INI file in a place the collector looks. Values are written with
# printf '%s', never through sed or envsubst, because the account password
# contains a literal '%' and a substitution engine would eat it.
#
set -euo pipefail

CONFIG_DIR="${MONITOR_CONFIG_DIR:-/config}"
SECRETS_DIR="${MONITOR_SECRETS_DIR:-/secrets}"
CONFIG_FILE="${CONFIG_DIR}/monitor.cfg"
DATA_DIR="${MONITOR_DATA_DIR:-/data}"

die() {
    echo "entrypoint: ERROR: $*" >&2
    exit 1
}

# Read a secret from a file, dropping exactly one trailing newline and nothing
# else: a password may legitimately start or end with whitespace.
#
# This returns non-zero rather than calling die(), and every caller is a plain
# assignment with `|| die`. Calling it from inside the config heredoc would put
# die() in a command-substitution subshell, where exit kills only the subshell:
# the config would be written with an empty username and the collector would
# spend the pod's life retrying a login that cannot work.
read_secret() {
    local name="$1" required="${2:-required}" file="${SECRETS_DIR}/$1" value
    if [[ ! -r "$file" ]]; then
        if [[ "$required" == "optional" ]]; then
            return 0
        fi
        echo "entrypoint: ERROR: secret '${name}' not found at ${file}" >&2
        return 1
    fi
    value="$(< "$file")"
    if [[ -z "$value" && "$required" != "optional" ]]; then
        echo "entrypoint: ERROR: secret '${name}' is empty" >&2
        return 1
    fi
    printf '%s' "$value"
}

# --- Non-credential settings, defaulted to what the host service runs ------
REGION="${MONITOR_REGION:-1}"
BRAND="${MONITOR_BRAND:-2}"
USE_GEOCODE="${MONITOR_USE_GEOCODE:-True}"
USE_GEOCODE_EMAIL="${MONITOR_USE_GEOCODE_EMAIL:-True}"
LANGUAGE="${MONITOR_LANGUAGE:-en}"
ODOMETER_METRIC="${MONITOR_ODOMETER_METRIC:-km}"
INCLUDE_REGENERATE="${MONITOR_INCLUDE_REGENERATE_IN_CONSUMPTION:-False}"
EFF_DAILYSTATS="${MONITOR_CONSUMPTION_EFFICIENCY_FACTOR_DAILYSTATS:-1.0}"
EFF_SUMMARY="${MONITOR_CONSUMPTION_EFFICIENCY_FACTOR_SUMMARY:-1.0}"
INFINITE="${MONITOR_INFINITE:-True}"
INTERVAL_MINUTES="${MONITOR_INFINITE_INTERVAL_MINUTES:-15}"
EXECUTE_COMMANDS="${MONITOR_EXECUTE_COMMANDS_WHEN_SOMETHING_WRITTEN_OR_ERROR:-}"
FORCE_SYNC_MAX_COUNT="${MONITOR_FORCE_SYNC_MAX_COUNT:-10}"

# This flag is the one that can wake the car. It is not configurable here: the
# collector refuses to start when it is True (patch 02), and the only reason to
# make it settable would be to defeat that guard.
FORCE_SYNC_WORKAROUND=False

# --- Refuse to run on a config that could wake the car ---------------------
# The in-process guard is the real one. This is the same check one layer out,
# so a mistake is visible in the first lines of the log rather than after the
# Python import chain.
if [[ "${MONITOR_FORCE_SYNC_WHEN_ODOMETER_DIFFERENT_LOCATION_WORKAROUND:-False}" != "False" ]]; then
    die "monitor_force_sync_when_odometer_different_location_workaround must stay False:" \
        "that path calls force_refresh_all_vehicles_states(), which wakes the car and" \
        "drains the 12 V battery."
fi

# --- Working directory sanity ----------------------------------------------
# The CSVs are resolved relative to the working directory. A wrong or empty
# mount would not fail: monitor.py creates the files and starts a fresh
# history, which looks exactly like a successful start.
[[ -d "$DATA_DIR" ]] || die "data directory ${DATA_DIR} does not exist"
[[ -w "$DATA_DIR" ]] || die "data directory ${DATA_DIR} is not writable"

if [[ "${MONITOR_REQUIRE_EXISTING_HISTORY:-false}" == "true" ]]; then
    for csv in monitor.csv monitor.dailystats.csv monitor.tripinfo.csv; do
        [[ -s "${DATA_DIR}/${csv}" ]] \
            || die "${DATA_DIR}/${csv} is missing or empty while MONITOR_REQUIRE_EXISTING_HISTORY=true." \
                   "Seed the volume from the host before starting, or set the flag to false" \
                   "to start a new history on purpose."
    done
fi

# --- Credentials -----------------------------------------------------------
# Read them here, in the main shell, so a missing or empty secret stops the
# container instead of producing a config that fails to log in.
SECRET_USERNAME="$(read_secret username)" || die "cannot read the account username"
SECRET_PASSWORD="$(read_secret password)" || die "cannot read the account password"
# The pin is optional and empty on this account: region 1 (Europe) logs in
# without one, and the host cfg has `pin =` with nothing after it. An empty
# value must stay empty -- the collector reads the key either way.
SECRET_PIN="$(read_secret pin optional)" || die "cannot read the account pin"

# --- Write the config ------------------------------------------------------
umask 077
mkdir -p "$CONFIG_DIR"

{
    printf '[monitor]\n'
    printf 'region = %s\n' "$REGION"
    printf 'brand = %s\n' "$BRAND"
    printf 'username = %s\n' "$SECRET_USERNAME"
    printf 'password = %s\n' "$SECRET_PASSWORD"
    printf 'pin = %s\n' "$SECRET_PIN"
    printf 'use_geocode = %s\n' "$USE_GEOCODE"
    printf 'use_geocode_email = %s\n' "$USE_GEOCODE_EMAIL"
    printf 'language = %s\n' "$LANGUAGE"
    printf 'odometer_metric = %s\n' "$ODOMETER_METRIC"
    printf 'include_regenerate_in_consumption = %s\n' "$INCLUDE_REGENERATE"
    printf 'consumption_efficiency_factor_dailystats = %s\n' "$EFF_DAILYSTATS"
    printf 'consumption_efficiency_factor_summary = %s\n' "$EFF_SUMMARY"
    printf 'monitor_infinite = %s\n' "$INFINITE"
    printf 'monitor_infinite_interval_minutes = %s\n' "$INTERVAL_MINUTES"
    printf 'monitor_execute_commands_when_something_written_or_error = %s\n' "$EXECUTE_COMMANDS"
    printf 'monitor_force_sync_when_odometer_different_location_workaround = %s\n' "$FORCE_SYNC_WORKAROUND"
    printf 'monitor_force_sync_max_count = %s\n' "$FORCE_SYNC_MAX_COUNT"
    printf '\n'
    # Both integrations are off, but the sections must exist: mqtt_utils.py and
    # domoticz_utils.py read them at import time.
    printf '[MQTT]\n'
    printf 'send_to_mqtt = False\n'
    printf 'mqtt_broker_hostname = localhost\n'
    printf 'mqtt_broker_port = 1883\n'
    printf 'mqtt_broker_username =\n'
    printf 'mqtt_broker_password =\n'
    printf 'mqtt_broker_cabundle =\n'
    printf 'mqtt_main_topic = hyundai_kia_connect_monitor\n'
    printf '\n'
    printf '[Domoticz]\n'
    printf 'send_to_domoticz = False\n'
    printf 'domot_url = http://127.0.0.1:8081\n'
    printf '\n'
    printf 'monitor_monitor_datetime = 0\n'
    printf 'monitor_monitor_longitude = 0\n'
    printf 'monitor_monitor_latitude = 0\n'
    printf 'monitor_monitor_engineon = 0\n'
} > "$CONFIG_FILE"

echo "entrypoint: wrote ${CONFIG_FILE} ($(wc -l < "$CONFIG_FILE") lines, credentials from ${SECRETS_DIR})"
echo "entrypoint: upstream commit $(cat /app/UPSTREAM_COMMIT), TZ=${TZ:-unset}, interval ${INTERVAL_MINUTES}m"
echo "entrypoint: data directory ${DATA_DIR}:"
for csv in monitor.csv monitor.dailystats.csv monitor.tripinfo.csv; do
    if [[ -s "${DATA_DIR}/${csv}" ]]; then
        echo "entrypoint:   ${csv}: $(wc -l < "${DATA_DIR}/${csv}") lines, first data row: $(sed -n '2p' "${DATA_DIR}/${csv}" | cut -c1-40)"
    else
        echo "entrypoint:   ${csv}: absent, will be created"
    fi
done

# Dry run: prove the config is assembled correctly without contacting Hyundai
# and without touching the CSVs. Credentials are printed as a length and a
# shape, never as values, so the '%' in the password can be verified without
# putting it in a log.
if [[ "${MONITOR_PRINT_CONFIG:-false}" == "true" ]]; then
    echo "entrypoint: MONITOR_PRINT_CONFIG=true, printing the assembled config and exiting"
    sed -e 's/^\(username\|password\|pin\) = .*/\1 = <redacted>/' "$CONFIG_FILE"
    printf 'redacted: username %s chars, password %s chars (contains %%: %s), pin %s chars\n' \
        "${#SECRET_USERNAME}" "${#SECRET_PASSWORD}" \
        "$([[ "$SECRET_PASSWORD" == *%* ]] && echo yes || echo no)" "${#SECRET_PIN}"
    exit 0
fi

cd "$DATA_DIR"
exec python -u /app/monitor.py "$@"
