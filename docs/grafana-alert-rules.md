# Grafana alert rule for vm-autoscaler (vmstorage storage)

vm-autoscaler only acts on Grafana Unified Alerting webhooks. Its scope is the
**vmstorage PVC size**. This page sets up the storage alert rule, the contact point
and the notification policy.

## The contract (what the rule MUST provide)

| Label         | Example              | Meaning                                                      |
|---------------|----------------------|--------------------------------------------------------------|
| `ocp_cluster` | `ocp-prod-region-a`  | OpenShift cluster name                                       |
| `namespace`   | `monitoring`         | Namespace of the VMCluster                                   |
| `component`   | `vmstorage`          | Only `vmstorage` is enabled                                  |
| `resource`    | `storage`            | Must be `storage` (the only resource handled)                |
| `autoscale`   | `"true"`             | Safety switch. Anything else, or missing, means "do nothing" |

**`ocp_cluster` and `namespace` must be exactly the directory names in the GitOps repo**
(`<ocp_cluster>/<namespace>/<values file>`) and must match a `targets` entry in the
service config. An alert for a cluster/namespace that is not configured is skipped with
`unknown_target`.

The **value** must be a percentage (0–100) in the query with refId **`B`** (configurable with
`defaults.value_ref_id`). The service reads **only** that refId: if it is missing or not a
number, the alert is skipped with `value_missing` and a WARNING lists the refIds it did get.
There is no fallback to another query, so check the refId before going past `dry_run`.

A firing alert without `ocp_cluster`, `namespace`, `component` or `resource` is ignored on
its own (`missing_label`, logged as a WARNING that names the label and counted in
`vm_autoscaler_alerts_ignored_total`); the other alerts in the webhook are still handled. A firing alert with a
`resource` other than `storage` is ignored (`unsupported_resource`, logged as a WARNING and
counted in `vm_autoscaler_alerts_ignored_total`) and the rest of the webhook is still
handled. Resolved alerts are ignored.

> **Delete the old CPU, memory and replica alert rules** if they were provisioned from an
> earlier version of this guide (`resource: cpu|memory|replicas`, `autoscale: "true"`).
> They no longer do anything and only show up as `unsupported_resource`.

**Use `max` across PVCs.** The fullest vmstorage node decides. Data is not spread evenly
across nodes (especially after adding nodes), so an average hides the node that is about
to fill up.

The `ocp_cluster` label below comes from a `cluster` label on your metrics (an external
label added by each cluster's Prometheus / vmagent). If each Grafana only sees one cluster,
replace `{{ $labels.cluster }}` with the fixed cluster name.

The threshold in the rule should match `policies.storage.trigger_percent` (80). The service
checks the value again (value ≥ `trigger_percent`) to protect against a misconfigured rule.

## Alert rule (file provisioning)

Save as e.g. `/etc/grafana/provisioning/alerting/vmstorage-autoscale.yaml`. Replace
`VM_DATASOURCE_UID` with the UID of your VictoriaMetrics / Prometheus datasource.

The rule has three steps: **A** = the query (already a percentage), **B** = reduce A to one
number (`last`), **C** = threshold on B (the alert condition).

```yaml
apiVersion: 1
groups:
  - orgId: 1
    name: vmstorage-autoscale
    folder: VictoriaMetrics
    interval: 1m
    rules:
      - uid: vmstorage-storage-autoscale
        title: VmstorageDiskFilling
        condition: C
        for: 30m
        labels:
          ocp_cluster: '{{ $labels.cluster }}'
          namespace: '{{ $labels.namespace }}'
          component: vmstorage
          resource: storage
          autoscale: "true"
        annotations:
          summary: 'Fullest vmstorage PVC is {{ $values.B.Value | printf "%.1f" }}% used'
          description: >-
            Must fire well before vmstorage hits -storage.minFreeDiskSpaceBytes, at which
            point the node turns read-only and vminsert reroutes its data to other nodes.
        data:
          - refId: A
            datasourceUid: VM_DATASOURCE_UID
            relativeTimeRange: { from: 600, to: 0 }
            model:
              refId: A
              instant: true
              # VM operator PVC names look like vmstorage-db-vmstorage-<cluster>-<n>.
              # Adjust the regex if your chart names the PVCs differently.
              expr: |
                max by (cluster, namespace) (
                  kubelet_volume_stats_used_bytes{persistentvolumeclaim=~"vmstorage-db-.*"}
                  /
                  kubelet_volume_stats_capacity_bytes{persistentvolumeclaim=~"vmstorage-db-.*"}
                ) * 100
          - refId: B
            datasourceUid: __expr__
            model: { refId: B, type: reduce, expression: A, reducer: last }
          - refId: C
            datasourceUid: __expr__
            model:
              refId: C
              type: threshold
              expression: B
              conditions: [{ evaluator: { type: gt, params: [80] } }]
```

### Extra signal for humans (not for vm-autoscaler)

`vm_free_disk_space_bytes` (exposed by vmstorage) shows free space as vmstorage sees it.
A useful extra alert is free space getting close to `-storage.minFreeDiskSpaceBytes`, e.g.
`min by (cluster, namespace) (vm_free_disk_space_bytes{job=~".*vmstorage.*"}) < 50 * 1024^3`.
Route that one to humans (no `autoscale` label). It also covers the case where the
autoscaler has reached `max` and skips with `at_max_cap`.

## Contact point

Grafana → Alerting → Contact points → New → **Webhook**:

* URL (Grafana in the same cluster): `http://vm-autoscaler.vm-autoscaler.svc:8080/webhook/grafana`
  (or the Route URL if Grafana is outside the cluster)
* HTTP method: `POST`
* Authorization header – scheme: `Bearer`, credentials: the `WEBHOOK_TOKEN` value
* Leave "Disable resolved message" off (resolved alerts are logged and ignored)

As provisioning YAML (the token is read from Grafana's environment):

```yaml
apiVersion: 1
contactPoints:
  - orgId: 1
    name: vm-autoscaler
    receivers:
      - uid: vm-autoscaler-webhook
        type: webhook
        settings:
          url: http://vm-autoscaler.vm-autoscaler.svc:8080/webhook/grafana
          httpMethod: POST
          authorization_scheme: Bearer
          authorization_credentials: ${VM_AUTOSCALER_WEBHOOK_TOKEN}
```

## Notification policy

Route `autoscale="true"` to the contact point. Group by target, so each
cluster/namespace gets its own webhook and its own decision.

```yaml
apiVersion: 1
policies:
  - orgId: 1
    receiver: default-email          # your existing default receiver
    routes:
      - receiver: vm-autoscaler
        object_matchers:
          - ["autoscale", "=", "true"]
        group_by: [ocp_cluster, namespace, component]
        group_wait: 1m
        group_interval: 5m
        repeat_interval: 1h          # re-send while firing; the service is idempotent
        continue: true               # humans' routes still receive these alerts too
```

> Provisioning `policies` **replaces the whole notification policy tree** of the org. If
> you manage policies in the UI, add the route there instead.

`repeat_interval: 1h` is fine: a repeated alert inside the cooldown window (24h by
default) is skipped with `cooldown_active`. After the PVC grows, the used percentage drops
and the alert resolves by itself.
