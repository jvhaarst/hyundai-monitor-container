# hyundai-monitor-container

Container image and Helm chart for the IONIQ 5 telemetry collector
([`hyundai_kia_connect_monitor`](https://github.com/ZuinigeRijder/hyundai_kia_connect_monitor)),
so it can run in the k3s cluster instead of as a host service on `raspi5`.

The collector takes one **cached** reading from Hyundai's servers every 15
minutes and appends it to three CSV files that go back to January 2024. Those
CSVs are the point of the project; everything in this repository is arranged
around not losing them and around never waking the car.

## The hard constraint: the car is never woken

`force_refresh_all_vehicles_states()` wakes the car, drains its 12 V battery
and returns nothing a cached read does not already give. Three things keep it
out of reach:

1. `monitor_force_sync_when_odometer_different_location_workaround` is written
   as `False` by the entrypoint and is **not** exposed as a chart value.
2. `patches/02-refuse-force-sync-workaround.patch` makes the collector log an
   error naming `force_refresh_all_vehicles_states()` and the 12 V battery, and
   `sys.exit(3)`, if that flag is ever `True` anyway.
3. The entrypoint repeats the same check one layer out, so a mistake shows up
   in the first lines of the pod log.

A change that could reach force-refresh is a release blocker, not a bug.

## What is in the image

| Piece | Version | Source |
|---|---|---|
| Python | 3.12.12 | `python:3.12.12-slim-trixie`; the API client declares `Requires-Python >=3.12`, and 3.12.12 is what the working host install runs |
| Collector | commit `a3744c0` | tarball of the pinned commit, plus the three patches |
| `hyundai-kia-connect-api` | 4.33.1 | PyPI |

The host had `hyundai_kia_connect_api` vendored as a second git checkout at tag
`v4.33.1`. That tree was diffed against the PyPI distribution of the same
version on 2026-10-04 and is **byte-identical**, so the fix from upstream PR
#1322 is already inside 4.33.1 and nothing is lost by taking the dependency
from PyPI — where Renovate can see it.

Deliberately absent: `geopy` (reverse geocoding goes to Nominatim over plain
`requests`), `gspread` and `paho-mqtt` (`send_to_mqtt = False`, no Google
Sheets, `send_to_domoticz = False`, and neither module is imported at import
time). Image size: 163 MB for `linux/arm64`.

`tini` is PID 1. Python as PID 1 leaves `SIGTERM` unhandled, and the kernel
ignores unhandled signals for PID 1, so without an init every rollout would
wait out the full termination grace period and then be `SIGKILL`ed.

### The three patches

They are applied with `patch -F0` (no fuzz) during the build, and the build
then greps the result. An upstream bump that moves this code **fails the
build** instead of quietly producing an image without them, which is exactly
what a re-vendored copy of the edited files would hide.

| Patch | Why it exists |
|---|---|
| `01-configparser-no-interpolation.patch` | The account password contains a literal `%`. Default `ConfigParser` reads it as interpolation syntax and the login fails. Verified in the built image: `interpolation=None` reads it back intact, the default parser raises `InterpolationSyntaxError`. |
| `02-refuse-force-sync-workaround.patch` | The no-wake guard above. Verified in the built image: exit code 3 and the error line. |
| `03-log-exception-class.patch` | Logs `type(ex).__name__`. Every handler funnels through `handle_exception()`, so without it an `AuthenticationError` and a `RateLimitingError` are indistinguishable. That ambiguity cost two months of data, twice. |

## Configuration

`monitor.cfg` is assembled at startup by `docker-entrypoint.sh`: behaviour flags
from chart values (as environment variables), credentials from a mounted
Secret, written with `printf '%s'` and never through `sed` or `envsubst` —
a substitution engine would eat the `%` in the password. The assembled file
lands in a tmpfs `emptyDir` at `/config`, reached through a symlink
`/app/monitor.cfg`, because `get_filepath()` looks in the working directory
(the data volume, which must never hold credentials) and then next to the
script.

Credentials: `credentials.existingSecret` pointing at a Secret with keys
`username`, `password` and `pin` (recommended), or `credentials.username` /
`.password` / `.pin` for the chart to create one. The pin is empty on this
account — region 1 (Europe) logs in without one — and an empty value is
accepted; the key itself must exist, because the collector reads it. Nothing credential-shaped is
baked into the image or committed here.

**Timezone is `Etc/UTC` and stays there.** Every node moved to `Etc/UTC` on
2026-10-03 and the newest CSV rows are UTC. `dailystats` aggregates per
calendar day, so another zone would shift the day boundary partway through a
two-year history. Do not "improve" this to `Europe/Amsterdam`.

## Deployment shape

- Single-replica `Deployment`, `strategy: Recreate`. One long-lived process
  that sleeps 15 minutes between cached reads and logs in once a day — not a
  CronJob. `RollingUpdate` would deadlock: the new pod waits for a
  ReadWriteOnce Longhorn volume the old pod still holds (Multi-Attach).
- `storageClassName: longhorn`, set explicitly because this cluster has more
  than one StorageClass marked default.
- The PVC carries `helm.sh/resource-policy: keep`.
- Non-root (uid 1000), read-only root filesystem, all capabilities dropped,
  `seccompProfile: RuntimeDefault`.
- Requests `10m` CPU / `64Mi`, limit `160Mi`, no CPU limit. Measured on the
  host: 30 MB RSS after 23 hours, negligible CPU. The 4 GB nodes sit at 78–82%
  memory, so the request is the real number and the limit only covers the
  once-a-day login.
- The Secret volume uses `defaultMode: 0440`. With `fsGroup` set the kubelet
  writes those files as `root:fsGroup`, so `0400` would lock out the non-root
  collector.
- No liveness or readiness probe. There is nothing to serve, and a probe that
  restarted the pod mid-append would risk the one thing worth protecting. A
  stuck collector is caught by the staleness alert instead.

### Restart semantics, and where they differ from the host unit

`monitor.py` exits `112` via `die()` after more than 96 consecutive errors
(~24 h at a 15-minute interval), and exits `3` from the no-wake guard. The host
unit handles both: `Restart=always` for the first, `StartLimitBurst=5` /
`StartLimitIntervalSec=1h` to make the second land in `systemctl --failed`.

A `Deployment` pod can only have `restartPolicy: Always`, so **exit 3 becomes
`CrashLoopBackOff`, not a permanent failure.** The backoff caps at five
minutes, the exit code is in `lastState.terminated.exitCode`, and the reason is
the first `ERROR` line in the log. Alerting on `kube_pod_container_status_restarts_total`
for this deployment is the replacement for `systemctl --failed`; see below.

## Installing

```sh
helm repo add hyundai-monitor https://jvhaarst.github.io/hyundai-monitor-container
helm repo update
kubectl create namespace hyundai-monitor

kubectl -n hyundai-monitor create secret generic hyundai-monitor-credentials \
  --from-literal=username='<account e-mail>' \
  --from-literal=password='<password, % and all, no escaping>' \
  --from-literal=pin=''

helm -n hyundai-monitor upgrade --install hyundai-monitor hyundai-monitor/hyundai-monitor \
  --set credentials.existingSecret=hyundai-monitor-credentials
```

`image.tag` defaults to the chart's `appVersion`, which is an immutable tag
published from a repository release, so `helm upgrade` actually rolls the pod.
`image.tag: main` does **not**: the pod spec is unchanged when that tag moves,
and the new image only lands on the next restart. If you use `main`, treat

```sh
kubectl -n hyundai-monitor rollout restart deployment/hyundai-monitor
```

as part of the release. The chart prints a warning when `image.tag` is `main`.

## Cutover: seeding the history

The three CSVs and `monitor.lastrun` must be carried over, not restarted. As of
2026-10-03 on the host:

```
monitor.csv              7142 lines   2024-04-01 21:14:36 -> 2026-10-03 17:22:28
monitor.dailystats.csv   2102 lines   20240303 -> 20261003
monitor.tripinfo.csv     2687 lines   20240102 -> 20261003
monitor.lastrun          4 lines, first line `last run ; <ts>`
```

1. Install with the collector switched off and the shell pod on, so nothing
   writes while the volume is being filled:

   ```sh
   helm -n hyundai-monitor upgrade --install hyundai-monitor hyundai-monitor/hyundai-monitor \
     --set credentials.existingSecret=hyundai-monitor-credentials \
     --set collector.enabled=false --set shell.enabled=true
   ```

2. Stop the host service, so there is exactly one writer from here on, and copy
   the files in:

   ```sh
   ssh haars001@raspi5 'sudo systemctl stop hyundai-monitor'
   SRC=/home/haars001/bin/hyundai_kona/hyundai_kia_connect_monitor
   POD=$(kubectl -n hyundai-monitor get pod -l app.kubernetes.io/component=shell -o name | head -1)
   for f in monitor.csv monitor.dailystats.csv monitor.tripinfo.csv monitor.lastrun; do
     ssh haars001@raspi5 "cat $SRC/$f" > "/tmp/$f"
     kubectl -n hyundai-monitor cp "/tmp/$f" "hyundai-monitor/${POD#pod/}:/data/$f"
   done
   ```

3. Check the boundaries **before** starting the collector. A truncation looks
   exactly like a successful start:

   ```sh
   kubectl -n hyundai-monitor exec "$POD" -- sh -c \
     'for f in monitor.csv monitor.dailystats.csv monitor.tripinfo.csv; do
        echo "$f: $(wc -l < /data/$f) lines"; sed -n 2p /data/$f | cut -c1-25; done'
   ```

   The first data rows must still read `2024-04-01 21:14:36`, `20240303 22:55`
   and `20240102`.

4. Start the collector and retire the shell pod:

   ```sh
   helm -n hyundai-monitor upgrade --install hyundai-monitor hyundai-monitor/hyundai-monitor \
     --set credentials.existingSecret=hyundai-monitor-credentials \
     --set collector.enabled=true --set shell.enabled=false
   ```

`requireExistingHistory` defaults to `true`: the collector refuses to start when
any of the three CSVs is missing or empty, because the alternative is a silent
fresh start on a wrong or empty volume. Set it to `false` only when a new
history is what you want.

## Dry-run story: proving it works before trusting it

Three steps, strictly in this order. Steps 1 and 2 touch neither the host
service nor the real CSVs, so they can run while the host service is still
collecting.

**1. Prove the config, with nothing leaving the pod.** `MONITOR_PRINT_CONFIG=true`
assembles `monitor.cfg`, prints it with the credentials redacted, reports
whether the password still contains its `%`, and exits 0 without contacting
Hyundai:

```sh
docker run --rm -v ./secrets:/secrets:ro -v ./empty:/data \
  -e MONITOR_PRINT_CONFIG=true ghcr.io/jvhaarst/hyundai-monitor:<tag>
```

**2. Prove one real cached reading, against a copy of the history.** Run the
image once, with `monitor_infinite = False`, pointed at a *copy* of the CSVs.
The host service keeps running and keeps writing the real files; this run can
only touch the copy, so there is never a moment with two writers on one file.

```sh
# on raspi5
SRC=/home/haars001/bin/hyundai_kona/hyundai_kia_connect_monitor
WORK=/home/haars001/dryrun; mkdir -p "$WORK/data" "$WORK/secrets"
cp "$SRC"/monitor.csv "$SRC"/monitor.dailystats.csv "$SRC"/monitor.tripinfo.csv \
   "$SRC"/monitor.lastrun "$WORK/data/"
# credentials, read once out of the host cfg
for k in username password pin; do
  sed -n "s/^$k *= *//p" "$SRC/monitor.cfg" | head -1 | tr -d '\n' > "$WORK/secrets/$k"
done
chmod 440 "$WORK/secrets"/*
wc -l "$WORK"/data/*.csv > /tmp/before.txt

# --userns=keep-id is for rootless podman on raspi5, so that uid 1000 in the
# container is uid 1000 on the host and can read the staged files. Under
# Docker proper, drop it.
docker run --rm --userns=keep-id \
  -v "$WORK/secrets":/secrets:ro -v "$WORK/data":/data \
  -e MONITOR_INFINITE=False -e MONITOR_REQUIRE_EXISTING_HISTORY=true \
  ghcr.io/jvhaarst/hyundai-monitor:<tag>

wc -l "$WORK"/data/*.csv > /tmp/after.txt
diff /tmp/before.txt /tmp/after.txt
tail -1 "$WORK/data/monitor.csv"
head -2 "$WORK/data/monitor.csv"
rm -rf "$WORK/secrets"   # the credentials were on disk only for this run
```

To see the append path rather than the no-change path, drop the last row of the
copy (`sed -i '$ d' "$WORK/data/monitor.csv"`) before the run: the collector
then appends exactly that reading back.

What proves the run:

- The log shows one login and one cached read, and **no** mention of
  `force_refresh_all_vehicles_states`.
- `monitor.csv` grew by **at most one row**, and the new row's timestamp is
  from this run. Zero rows is also a pass: the collector appends only when
  something changed, so a parked, unplugged car at the same SOC writes nothing.
  `monitor.lastrun` is rewritten either way — check its first line.
- `head -2` still shows the 2024-04-01 row.
- The process exits 0.

**3. Cut over.** Follow "Cutover: seeding the history" above, which stops the
host service before the pod ever starts. From that point the pod is the only
writer.

## Rolling back to the host service

The host checkout, its conda env and the unit file are untouched by any of
this, so rollback is two commands plus a decision about the rows the pod
collected.

```sh
# 1. Stop the only writer in the cluster.
helm -n hyundai-monitor upgrade --install hyundai-monitor hyundai-monitor/hyundai-monitor \
  --set credentials.existingSecret=hyundai-monitor-credentials \
  --set collector.enabled=false --set shell.enabled=true

# 2. Copy the rows the pod collected back to the host, if any.
POD=$(kubectl -n hyundai-monitor get pod -l app.kubernetes.io/component=shell -o name | head -1)
for f in monitor.csv monitor.dailystats.csv monitor.tripinfo.csv monitor.lastrun; do
  kubectl -n hyundai-monitor cp "hyundai-monitor/${POD#pod/}:/data/$f" "/tmp/from-pod-$f"
done
# Inspect, then replace the host copies (keep a backup first).

# 3. Hand collection back.
ssh haars001@raspi5 'sudo systemctl start hyundai-monitor && systemctl status hyundai-monitor'
```

Then `--set shell.enabled=false`. Keep the release and the PVC: the PVC is
annotated `helm.sh/resource-policy: keep`, so even `helm uninstall` leaves the
data behind.

## Staleness alerting (out of scope here — proposal)

The host timer `car-monitor-alert.timer` reads `monitor.lastrun`, compares it
against `STALE_DAYS=2` / `REMIND_DAYS=7` and pipes mail to
`/usr/sbin/sendmail -t` through msmtp. That path breaks once the collector
leaves `raspi5`: no other node has a working MTA, and the file is on a Longhorn
volume that only the collector pod mounts. Working logic lives in
`nodes/raspi5/car-monitor-alert/` in the `upgrade_nodes` repo.

The replacement should not be another mail-sending timer. The cluster already
runs VictoriaMetrics with `VMRule` support (see `ntp_dashboard_k8s`), and that
is the one place an alert can see both failure modes:

1. **Staleness.** The collector does not export metrics, so the signal has to
   come from somewhere. Cheapest honest option: have the pod touch a file and
   expose its age — i.e. a small sidecar or an exporter container reading
   `monitor.lastrun` and publishing one gauge,
   `hyundai_monitor_last_run_timestamp_seconds`. Then
   `time() - hyundai_monitor_last_run_timestamp_seconds > 2*86400` is the alert,
   with the same two-day threshold as today.
2. **Visible failure.** `kube_pod_container_status_last_terminated_exitcode{exitcode="3"}`
   for this deployment, and a restart-rate alert for the 112 crash loop. This is
   what replaces `systemctl --failed`, which a `Deployment` cannot reproduce.

Both land in the existing Alertmanager route, so there is no second mail path
to maintain. Deliberately not built in this pass: it needs a decision about the
sidecar, and the alert should be wired into the same route as the rest of the
cluster's alerts rather than bolted onto this chart.

## Renovate and gated automerge

`renovate.json` automerges `minor`, `patch`, `digest` and `pin` with
`platformAutomerge: true`. Majors are never automerged. Two dependencies are
held back from automerge on purpose:

- the pinned upstream collector commit (`git-refs`), because a bump is exactly
  the moment the three patches can break, and
- `hyundai-kia-connect-api` (`pypi`), because it is the login path.

A green build on those PRs means the patches still apply; it is not a reason to
merge without reading the diff. Python minor and major bumps are held too.

Automerge is only safe when something gates it. After the first build has run
on `main`, confirm the check's real name from a check run rather than guessing
it from the workflow YAML, and wire up the rest:

```sh
# 1. The check's real name
gh api repos/jvhaarst/hyundai-monitor-container/commits/main/check-runs \
  --jq '.check_runs[].name'

# 2. Repository settings
gh api -X PATCH repos/jvhaarst/hyundai-monitor-container \
  -F allow_auto_merge=true -F delete_branch_on_merge=true

# 3. Ruleset `main protection` gating that check, pinned to the GitHub Actions
#    app (integration_id 15368), with the repository-admin role bypassed so
#    direct pushes to main still work.
gh api -X POST repos/jvhaarst/hyundai-monitor-container/rulesets --input - <<'JSON'
{
  "name": "main protection",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "bypass_actors": [ { "actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always" } ],
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    { "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "required_status_checks": [ { "context": "build", "integration_id": 15368 } ]
      }
    }
  ]
}
JSON
```

Replace `"context": "build"` with the name step 1 printed.

## Releasing

The chart's `appVersion` is the image tag, and image semver tags come from the
repository's own `v*` git tags. So the order is fixed:

1. Bump `appVersion` in `charts/hyundai-monitor/Chart.yaml` when the image
   changes, and commit. `version` only needs touching to move the chart's
   major.minor.
2. Tag and push the tag: `git tag v0.2.0 && git push origin v0.2.0`. That runs
   "Build and Push Container", which publishes `0.2.0`, `0.2` and `0`.
3. The chart release runs on the `charts/**` change and **refuses to publish**
   until `ghcr.io/jvhaarst/hyundai-monitor:<appVersion>` exists and has a
   `linux/arm64` platform. It waits up to ten minutes for a tag pushed
   alongside the commit, then fails with the tag it wanted.

That gate is what stops a chart going out ahead of its image. Without it the
mismatch surfaces as `ImagePullBackOff` during `helm upgrade`, and because the
Deployment uses `strategy: Recreate` the old pod is already gone at that point,
so collection stops until someone rolls back.

The published chart version is the `Chart.yaml` major.minor with the workflow
run number as the patch digit, as in `ntp_dashboard_k8s` and
`garmin_health_data_k8s`. Nothing in CI commits to the default branch:
the inherited auto-bump step did, which the `main protection` ruleset rejects
with `GH013` (it bypasses the repository-admin role, not the GitHub Actions
app) and which also lost a push race against a Renovate automerge. Requiring a
hand-written bump was the alternative, but Renovate's busybox tag lives in the
chart's `values.yaml`: that PR automerges, triggers the release and carries no
bump, so each one would leave a red run on main.

The image build is the other half of the safety net: the three patches are
applied inside it with `patch -F0` and then grepped for, and the push happens
in the same action as the build. A patch that no longer applies fails the
build, so no tag moves and no image reaches the registry. Both nets were tested
by breaking a patch's context (`Hunk #1 FAILED`) and by deleting a patch file
so the others still applied (the grep step caught it); each gave
`docker build exit=1`. The same test against the upstream commit Renovate
proposes in PR #1 (`4b819d5`) applied all three cleanly, at offsets.

## Repository layout

```
Dockerfile                     multi-stage, arm64 + amd64
docker-entrypoint.sh           assembles monitor.cfg, guards, then execs monitor.py
patches/                       the three local fixes, applied with -F0 at build time
charts/hyundai-monitor/        Helm chart, published to GitHub Pages
.github/workflows/build.yaml   image to ghcr.io/jvhaarst/hyundai-monitor (the gating check)
.github/workflows/lint.yaml    helm lint + template, and a check that the chart
                               never grows a force-sync knob
.github/workflows/helm-release.yaml  packages the chart and publishes the index
```

Local tooling is `uv` for Python and `jq`/`yq` for JSON/YAML, in scripts and CI
steps alike.

## Verified on 2026-10-04

- Host checkout is at `a3744c0` with exactly the three local edits; the patched
  build tree is byte-identical to the files the host service is running.
- Vendored `hyundai_kia_connect_api` v4.33.1 is byte-identical to the PyPI
  distribution of 4.33.1.
- `arm64` image builds on `raspi5` and runs: Python 3.12.12, the API client
  imports, all three patches present and effective (including exit code 3).
- The assembled config is read back intact by `ConfigParser(interpolation=None)`
  with the `%` in place, and the default parser raises
  `InterpolationSyntaxError` on the same file.
- The entrypoint refuses to start on a missing or empty secret, on a missing
  history when `requireExistingHistory=true`, and on any attempt to enable the
  force-sync workaround.

- Step 2 of the dry-run story was executed on `raspi5` against the real
  account, on a copy of the CSVs, while the host service kept running:
  one login, one cached read (`vehicle update 2026-10-04 06:22:19`, 12 V 72%,
  SOC 79%, odometer 70879.4 km), **exactly one** row appended to
  `monitor.csv`, `monitor.lastrun` rewritten, exit code 0, no mention of
  `force_refresh` anywhere in the log, and the 2024-04-01 first row untouched.
  A second run with the cached state already matching appended nothing, which
  is the expected no-change behaviour.
- The account has no PIN: region 1 (Europe) logs in without one, and the host
  cfg carries `pin =` with nothing after it. The entrypoint and the chart treat
  the pin as optional; an empty value stays empty.

- The published artefacts: `ghcr.io/jvhaarst/hyundai-monitor:main` is public and
  multi-arch (`linux/amd64`, `linux/arm64`, 163 MB), and chart 0.1.0 is in the
  index at `https://jvhaarst.github.io/hyundai-monitor-container`. The pulled
  arm64 image was re-checked on `raspi5`: config assembly with the `%` and an
  empty pin, and exit code 3 from the no-wake guard.
- `main protection` is active on the default branch, gating on the check named
  `build` pinned to `integration_id: 15368`, with the repository-admin role
  bypassed. `allow_auto_merge` and `delete_branch_on_merge` are on, and
  Renovate has onboarded the repository.

Not yet done: the cutover itself — seeding the PVC and stopping
`hyundai-monitor.service` on `raspi5`.

GitHub Pages had to be switched to "GitHub Actions" before the chart release
workflow could publish; a fresh fork or clone of this repository needs the same
one-off step (`gh api -X POST repos/<owner>/<repo>/pages -f build_type=workflow`).
