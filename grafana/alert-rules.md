# Grafana alert rules for vm-autoscaler

vm-autoscaler only acts on Grafana Unified Alerting webhooks. This page shows how to set up
the alert rules, the contact point and the notification policy.

## The contract (what every rule MUST provide)

| Label         | Example              | Meaning                                                      |
|---------------|----------------------|--------------------------------------------------------------|
| `ocp_cluster` | `ocp-prod-region-a`  | OpenShift cluster name; must match `targets[].ocp_cluster`   |
| `namespace`   | `monitoring`         | Namespace of the VMCluster                                   |
| `component`   | `vmstorage`          | Only `vmstorage` is enabled today                            |
| `resource`    | `cpu`                | One of `cpu`, `memory`, `storage`, `replicas`                |
| `autoscale`   | `"true"`             | Safety switch. Anything else, or missing, means "do nothing" |

The **value** must be a percentage (0–100) in the query with refId **`B`** (configurable with
`defaults.value_ref_id`). If `B` is missing, the service uses the first numeric value.

A firing alert without `ocp_cluster`, `namespace`, `component` or `resource` is rejected
with HTTP 422 and a message that names the missing label. Resolved alerts are ignored.

**Always use `max` across pods.** The fullest vmstorage node decides. Data is not spread
evenly across nodes (especially after adding nodes), so an average hides the node that is
about to fill up.

The `ocp_cluster` label below comes from a `cluster` label on your metrics (an external
label added by each cluster's Prometheus / vmagent). If each Grafana only sees one cluster,
replace `{{ $labels.cluster }}` with the fixed cluster name.

Thresholds in the rules should match `trigger_percent` in the service config. The service
checks the value again (value ≥ `trigger_percent`) to protect against a misconfigured rule.

## Alert rules (file provisioning)

Save as e.g. `/etc/grafana/provisioning/alerting/vmstorage-autoscale.yaml`. Replace
`VM_DATASOURCE_UID` with the UID of your VictoriaMetrics / Prometheus datasource.

Each rule has three steps: **A** = the query (already a percentage), **B** = reduce A to one
number (`last`), **C** = threshold on B (the alert condition).

```yaml
apiVersion: 1
groups:
  - orgId: 1
    name: vmstorage-autoscale
    folder: VictoriaMetrics
    interval: 1m
    rules:
      # ------------------------------------------------------------------ CPU
      - uid: vmstorage-cpu-autoscale
        title: VmstorageHighCpu
        condition: C
        for: 15m
        labels:
          ocp_cluster: '{{ $labels.cluster }}'
          namespace: '{{ $labels.namespace }}'
          component: vmstorage
          resource: cpu
          autoscale: "true"
        annotations:
          summary: 'vmstorage CPU at {{ $values.B.Value | printf "%.1f" }}% of limit'
          description: >-
            Fullest vmstorage pod uses this share of its CPU limit. Context: check
            throttling with rate(container_cpu_cfs_throttled_periods_total{container="vmstorage"}[5m])
            / rate(container_cpu_cfs_periods_total{container="vmstorage"}[5m]).
        data:
          - refId: A
            datasourceUid: VM_DATASOURCE_UID
            relativeTimeRange: { from: 600, to: 0 }
            model:
              refId: A
              instant: true
              expr: |
                max by (cluster, namespace) (
                  sum by (cluster, namespace, pod) (
                    rate(container_cpu_usage_seconds_total{container="vmstorage"}[5m])
                  )
                  /
                  sum by (cluster, namespace, pod) (
                    kube_pod_container_resource_limits{container="vmstorage", resource="cpu"}
                  )
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

      # --------------------------------------------------------------- memory
      - uid: vmstorage-memory-autoscale
        title: VmstorageHighMemory
        condition: C
        for: 10m
        labels:
          ocp_cluster: '{{ $labels.cluster }}'
          namespace: '{{ $labels.namespace }}'
          component: vmstorage
          resource: memory
          autoscale: "true"
        annotations:
          summary: 'vmstorage memory at {{ $values.B.Value | printf "%.1f" }}% of limit'
        data:
          - refId: A
            datasourceUid: VM_DATASOURCE_UID
            relativeTimeRange: { from: 600, to: 0 }
            model:
              refId: A
              instant: true
              expr: |
                max by (cluster, namespace) (
                  sum by (cluster, namespace, pod) (
                    container_memory_working_set_bytes{container="vmstorage"}
                  )
                  /
                  sum by (cluster, namespace, pod) (
                    kube_pod_container_resource_limits{container="vmstorage", resource="memory"}
                  )
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

      # -------------------------------------------------------------- storage
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

      # ------------------------------------------------------------- replicas
      # Only ever opens a merge request (the service enforces this).
      # Value = percent of vmstorage pods that are above 70% CPU AND 70% memory.
      - uid: vmstorage-replicas-autoscale
        title: VmstorageClusterSaturated
        condition: C
        for: 1h
        labels:
          ocp_cluster: '{{ $labels.cluster }}'
          namespace: '{{ $labels.namespace }}'
          component: vmstorage
          resource: replicas
          autoscale: "true"
        annotations:
          summary: '{{ $values.B.Value | printf "%.0f" }}% of vmstorage pods are saturated'
        data:
          - refId: A
            datasourceUid: VM_DATASOURCE_UID
            relativeTimeRange: { from: 600, to: 0 }
            model:
              refId: A
              instant: true
              expr: |
                (
                  count by (cluster, namespace) (
                    (
                      sum by (cluster, namespace, pod) (rate(container_cpu_usage_seconds_total{container="vmstorage"}[5m]))
                      / sum by (cluster, namespace, pod) (kube_pod_container_resource_limits{container="vmstorage", resource="cpu"})
                      > 0.7
                    )
                    and on (cluster, namespace, pod)
                    (
                      sum by (cluster, namespace, pod) (container_memory_working_set_bytes{container="vmstorage"})
                      / sum by (cluster, namespace, pod) (kube_pod_container_resource_limits{container="vmstorage", resource="memory"})
                      > 0.7
                    )
                  )
                  /
                  count by (cluster, namespace) (
                    kube_pod_container_resource_limits{container="vmstorage", resource="cpu"}
                  )
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
              conditions: [{ evaluator: { type: gt, params: [85] } }]
```

### Alternatives and extra signals

* **VictoriaMetrics' own disk metric.** `vm_free_disk_space_bytes` (exposed by vmstorage)
  shows free space as vmstorage sees it. A useful extra (non-autoscale) alert is free space
  getting close to `-storage.minFreeDiskSpaceBytes`, e.g.
  `min by (cluster, namespace) (vm_free_disk_space_bytes{job=~".*vmstorage.*"}) < 50 * 1024^3`.
  Route that one to humans, not to vm-autoscaler.
* **Replicas by active series budget.** If you have a per-node budget for active series,
  use `max(...active series per vmstorage pod...) / <budget> * 100` as query A instead.
  Keep `for: 1h` or longer: adding a vmstorage node is a big, slow change.

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

Route `autoscale="true"` to the contact point. Group by target so CPU and memory alerts for
the same vmstorage arrive in **one** webhook — the service then makes one commit and one
Argo sync (one rolling restart instead of two).

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
        group_wait: 1m               # wait so cpu + memory land in the same webhook
        group_interval: 5m
        repeat_interval: 1h          # re-send while firing; the service is idempotent
        continue: true               # humans' routes still receive these alerts too
```

> Provisioning `policies` **replaces the whole notification policy tree** of the org. If
> you manage policies in the UI, add the route there instead.

`repeat_interval: 1h` is fine: a repeated alert inside the cooldown window is skipped with
`cooldown_active`. After a change is applied the new limit makes the usage percentage drop,
and the alert resolves by itself.
