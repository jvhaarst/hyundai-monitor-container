# Prompt: containerise the IONIQ 5 telemetry collector

Facts in this file were read off the live system on 2026-10-03. Verify anything load-bearing
before relying on it — see "Verify first" at the end.

---

Build a container image and Helm chart for the Hyundai IONIQ 5 telemetry collector that
currently runs as a host service on raspi5, so it can move into the k3s cluster (context
`raspi5`). Follow the same pattern as my other projects: own git repo, Dockerfile, a Helm
chart published from the repo, GitHub Actions that build and push a multi-arch image to
ghcr.io, and renovate.json with gated automerge.

## What runs today

Host: raspi5, user `haars001`, directory
`/home/haars001/bin/hyundai_kona/hyundai_kia_connect_monitor`.

It is a git checkout of https://github.com/ZuinigeRijder/hyundai_kia_connect_monitor pinned
at commit `a3744c0`, with `hyundai_kia_connect_api` **v4.33.1** vendored as a second
checkout inside it and put on `PYTHONPATH`. There is no upstream container image. The
Docker recipe in upstream Discussion #56 is stale — `python:3.11`, cron inside the
container, no arm64 — do not follow it.

Driven by `/etc/systemd/system/hyundai-monitor.service`, which runs
`python3 -u monitor.py` as one long-lived process (`monitor_infinite = True`,
`monitor_infinite_interval_minutes = 15`), not a cron job. Interpreter is a conda env at
`/home/haars001/miniforge3/envs/hkc/bin/python3`, **Python 3.12.12**.

Installed packages in that env, which is what the image has to reproduce:

```
beautifulsoup4 4.15.0   certifi 2026.7.22      cryptography 46.0.3
gspread 5.12.4          paho-mqtt 2.1.0        pycryptodome 3.23.0
PyJWT 2.10.1            python-dateutil 2.9.0.post0
pytz 2025.2             requests 2.32.5        requests-oauthlib 2.0.0
tzdata 2026.4           urllib3 2.6.3
```

`geopy` is deliberately **not** installed — reverse geocoding goes to Nominatim over plain
`requests` and works without it. `gspread` and `paho-mqtt` are present but unused
(`send_to_mqtt = False`, no Google Sheets, `send_to_domoticz = False`); drop them unless
something needs them.

Logging is `logging_config.ini` with a `StreamHandler` to stdout, so journal/stdout is
already the log sink. No log file to mount.

## The hard constraint

**The car must never be woken.** `force_refresh_all_vehicles_states()` drains the 12 V
battery and returns nothing interesting. Everything the collector does is a cached read
from Hyundai's servers, which is also why the documented 200-calls-per-day limit does not
bite: 96 cached reads per day plus one login.

`monitor.cfg` must keep `monitor_force_sync_when_odometer_different_location_workaround =
False`, and the local guard below must stay in place. Treat any change that could call
force-refresh as a release blocker.

## Three local patches that must survive an upstream bump

These are not upstream. A plain tag checkout drops them silently, which has already
happened once. Carry them as `.patch` files applied during the image build, so the build
**fails loudly** if upstream moves and a patch no longer applies — do not re-vendor the
edited files.

1. `monitor.py` and `monitor_utils.py`: `configparser.ConfigParser(interpolation=None)`
   instead of `ConfigParser()`, in both places. **The account password contains a `%`**,
   which ConfigParser otherwise reads as interpolation syntax and the login fails.

2. `monitor.py`, right after `MONITOR_FORCE_SYNC_COUNT = 0`: refuse to start if the
   force-sync workaround is enabled, log an error naming
   `force_refresh_all_vehicles_states()` and the 12 V battery, and `sys.exit(3)`.

3. `monitor.py` in `handle_exception()`: log the exception class too —
   `f"Exception: {type(ex).__name__}: {exception_str}"`. Every handler funnels through
   there, so without it an `AuthenticationError` and a `RateLimitingError` are
   indistinguishable in the log. That ambiguity cost two months of lost data twice.

## Data that must be carried over, not restarted

Three CSVs in the working directory, appended to forever. They are the whole point of the
project — over two years of history:

```
monitor.csv              7142 lines   2024-04-01 21:14:36 -> 2026-10-03 17:22:28
monitor.dailystats.csv   2102 lines   20240303 -> 20261003
monitor.tripinfo.csv     2687 lines   20240102 -> 20261003
```

Plus `monitor.lastrun`, a 4-line status file whose first line (`last run ; <ts>`) is what
the staleness alert reads.

They go on a Longhorn PVC, pre-seeded from the host before first start. The container must
only ever append. Verify after cutover that the first line of each file is still the 2024
record — a truncation would look like a successful start.

Because the PVC is RWO Longhorn and this is a single-replica Deployment, set
`strategy: Recreate`. A RollingUpdate deadlocks on Multi-Attach.

## Configuration and secrets

`monitor.cfg` is one INI file holding `username`, `password`, `pin` and the behaviour
flags. Credentials and the VIN are sensitive: keep them in a Secret and assemble the cfg at
startup, or mount the whole cfg from a Secret. Do not bake any of it into the image or
commit it.

Non-credential settings currently in use: `region = 1`, `brand = 2`, `use_geocode = True`,
`use_geocode_email = True`, `language = en`, `odometer_metric = km`,
`include_regenerate_in_consumption = False`, both consumption efficiency factors `1.0`,
`monitor_force_sync_max_count = 10`.

**Timezone: set it to `Etc/UTC` and leave it there.** Every node moved to `Etc/UTC` on
2026-10-03 and the host service already runs that way, so the newest CSV rows are UTC.
`dailystats` aggregates per day, so changing the zone would shift the day boundary partway
through a two-year history. Do not "improve" this to Europe/Amsterdam.

## Deployment shape

- Single-replica Deployment, `strategy: Recreate`, not a CronJob — `monitor_infinite` means
  one process that sleeps 15 minutes between cached reads and logs in once a day.
- arm64 is required (Raspberry Pi 5 and 4 nodes). Build multi-arch or arm64-only.
- Run as non-root with a read-only root filesystem if the CSV directory allows it.
- **The cluster has no memory headroom** — the 4 GB nodes sit at 78–82%. Measured
  2026-10-03, the host process used 38 MB RSS and negligible CPU after 14 hours of uptime.
  Size requests from that, with a limit that leaves room for the daily login, and keep the
  image small.
- Restart policy equivalent of the current unit: `monitor.py` exits 112 via `die()` after
  more than 96 consecutive errors (~24 h at a 15-minute interval), and exits 3 from the
  guard above. A crash-loop backoff is right for the first; the second should end up
  visibly failed rather than retrying forever.

## Repo conventions to match my other projects

- Dockerfile at the repo root, chart under `charts/<name>/`.
- Two workflows: build-and-push the image to `ghcr.io/jvhaarst/<name>`, and release the
  chart. Note that `image.tag: main` with `pullPolicy: Always` does **not** make
  `helm upgrade` roll the pods, because the pod spec is unchanged — either publish an
  immutable tag or digest per build, or document that a `kubectl rollout restart` is part
  of the release.
- `renovate.json` with automerge for minor/patch/digest/pin, `platformAutomerge: true`,
  majors never automerged, plus `allow_auto_merge`/`delete_branch_on_merge` on the repo and
  a `main protection` ruleset gating on the build check pinned to the GitHub Actions app.
  Confirm the check's real name from a check run, not from the workflow YAML.
- Add `# renovate:` datasource comments for the base image, the pinned upstream monitor
  commit and `hyundai_kia_connect_api` 4.33.1, so a bump is a reviewable PR rather than
  drift. Upstream bumps are the moment the three patches break, which is the point.
- Use `uv` for any local Python tooling, and `jq`/`yq` for JSON/YAML in scripts and CI
  steps — no Python one-liners for parsing.

## Out of scope for the image, but say what you would do

A host timer `car-monitor-alert.timer` emails me when collection goes stale (reads
`monitor.lastrun`, `STALE_DAYS=2`, `REMIND_DAYS=7`, pipes to `/usr/sbin/sendmail -t` via
msmtp on raspi5). The working logic is in `nodes/raspi5/car-monitor-alert/` in my
`upgrade_nodes` repo. Moving the collector into the cluster breaks that path, because no
node but raspi5 has a working MTA. Propose a replacement but do not build it in this pass.

## Finish with

A dry-run story: how to prove the image collects a real cached reading and appends exactly
one row, without the host service and the pod both writing, and how to roll back to the
host service if it does not.

## Verify first

Two things worth checking before you commit to an approach:

- The `hyundai_kia_connect_api` is vendored as a git checkout at `v4.33.1`, not
  pip-installed. Installing `hyundai-kia-connect-api==4.33.1` from PyPI is cleaner and lets
  Renovate track it, but diff it against the vendored tree first — that copy may carry the
  PR #1322 fix that got the login working again.
- The `hkc` conda env runs Python 3.12.12 while raspi5's system Python is 3.11.2. Build on
  3.12 to match what is actually working, not on whatever the base image defaults to.
