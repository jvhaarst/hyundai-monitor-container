# Grafana alert rules for the IONIQ 5 collector

This file is a pointer, deliberately. It used to hold a second copy of the rule
YAML, which drifts the moment a rule is edited in the one place that is
actually deployed.

## Where the rules live

Source of truth: `~/code/upgrade_nodes/k3s/monitoring/values.yaml`, under
`.grafana.alerting."rules.yaml"`, group `Hyundai / hyundai-monitor`. The
`victoria-metrics-k8s-stack` chart renders that into the ConfigMap
`victoria-metrics-grafana` and mounts it at
`/etc/grafana/provisioning/alerting/`, which is why the rules show as
*Provisioned* in the Grafana UI and cannot be edited there.

The chart pipes those values through `tpl`, so Grafana's own templating has to
be escaped in `values.yaml`: write ``{{`{{ $labels.file }}`}}``, not
`{{ $labels.file }}`. An unescaped reference fails the Helm render.

Check the committed copy against the cluster:

```sh
kubectl --context raspi5 -n monitoring get cm victoria-metrics-grafana \
  -o json | jq -r '.data["rules.yaml"]' > /tmp/live-rules.yaml
yq -o=json -P 'sort_keys(..)' /tmp/live-rules.yaml > /tmp/live.json
yq '.grafana.alerting."rules.yaml"' \
  ~/code/upgrade_nodes/k3s/monitoring/values.yaml \
  | yq -o=json -P 'sort_keys(..)' > /tmp/git.json
diff /tmp/git.json /tmp/live.json
```

Only the `tpl` escaping above should differ.

Warning: `helm upgrade -f values.yaml` replaces the whole `grafana.alerting`
list. If that file is missing a group the cluster has, the upgrade silently
deletes those alerts. Run `helm get values` first and reconcile, as the
`upgrade_nodes` README says.

## The four rules

| uid | for | noData | What it means |
|---|---|---|---|
| `hyundai-monitor-metrics-missing` | 30m | Alerting | No metrics at all. The collector may be healthy — suspect the exporter sidecar or the scrape. |
| `hyundai-monitor-stale` | 1h | OK | `hyundai_monitor_last_run_timestamp_seconds` over 48 h old. Collection has stopped. |
| `hyundai-monitor-history-shrank` | 5m | OK | A CSV lost rows against its 7-day maximum. Truncation or the wrong volume. |
| `hyundai-monitor-refused-to-start` | 5m | OK | The collector exited 3: the no-wake guard tripped. |

They replace the host timer `car-monitor-alert.timer`, which read
`monitor.lastrun` and mailed through msmtp on `raspi5` with `STALE_DAYS=2`.
Grafana evaluates them against the `VictoriaMetrics` datasource and mails
through its existing SMTP contact point, so there is no `vmalert`, no
`alertmanager` and no second mail path.

## Two reasons the split exists

Absence and staleness are two faults, so they are two rules.

On 2026-10-09 the exporter sidecar wedged while the collector kept writing
happily. The metric vanished, the staleness rule fired through
`noDataState: Alerting`, and its summary announced that the collector had not
run for two days. That was false, and it sent the investigation at the wrong
container.

`hyundai-monitor-metrics-missing` therefore owns absence, and it queries `up{}`
rather than `absent()`. `absent()` returns *no series* when the metric is
present, and no series is NoData, so with `noDataState: Alerting` that rule
fires exactly when everything is healthy — which it did, for ten hours on
2026-10-09. `up{}` always returns a series while the target is known, so NoData
there means only that the target is gone. `hyundai-monitor-stale` keeps
`noDataState: OK`, so one fault cannot produce two mails, and it only judges a
timestamp it can actually see.
