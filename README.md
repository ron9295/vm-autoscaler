# vm-autoscaler

A small service that grows VictoriaMetrics **vmstorage** resources (CPU, memory, storage,
replicas) on OpenShift **through GitOps**, when Grafana says they are under pressure.

```
Grafana alert ──webhook──▶ vm-autoscaler ──▶ GitLab API (edits Argo source repo)   [GITOPS_BACKEND=gitlab]
                                    │      or Internal GitOps backend            [GITOPS_BACKEND=internal]
                                    └──────▶ Argo CD API (sync app)
                                                   └─▶ VM operator rolls vmstorage pods one by one
```

1. Grafana fires an alert (e.g. "vmstorage CPU at 91% of limit") and calls `POST /webhook/grafana`.
2. The service reads the cluster, namespace, component, resource and value from the alert labels.
3. It reads the current values of that component from the Argo CD source repo (through the
   GitLab API, or through the internal GitOps backend — see "GitLab mode").
4. It computes a safe new value (one step up, rounded, capped, respecting a cooldown).
5. It asks the backend to write the change to the Argo CD source repo (or to open a merge request).
6. It triggers an Argo CD sync of just the `VMCluster` object. The VM operator then rolls the
   vmstorage pods one by one.

The service never runs `git` or clones anything, and never edits the cluster directly. Git is the
source of truth, and every change is a normal commit or MR with a clear message, e.g.

```
[vm-autoscaler] ocp-prod-region-a/monitoring vmstorage cpu 4 -> 5 (usage 91.2% >= 80%)
```

---

## Safety rules (and why each one exists)

Every rule below is implemented in `app/services/scaling_service.py` or
`app/domain/strategies.py` and has a test.

| # | Rule | Why |
|---|------|-----|
| 1 | Only act if the alert label `autoscale="true"` and the component is in `enabled_components` (only `vmstorage` today). | An explicit opt-in per alert rule. A missing label means "off". |
| 2 | Only act if the value ≥ the policy's `trigger_percent`. | Protects against a misconfigured Grafana rule that fires too early. |
| 3 | **Cooldown** per resource (e.g. 6h for CPU). The last-scaled time comes from Git: in GitLab mode from the **commit history** of the manifest (default), otherwise from an annotation `vm-autoscaler/last-scaled-<resource>` written with each change. | Gives the change time to roll out and show effect. Keeps the service stateless: no database, history lives in Git. Grafana re-sends firing alerts every `repeat_interval`; these are skipped. |
| 4 | New value = current × (1 + step%), rounded up (CPU to 100m, memory/storage to whole Gi), capped at `max`. Already at max → skip `at_max_cap`, logged as **WARNING**. | Small, predictable steps. `at_max_cap` means a human must act (add nodes, rebalance). |
| 5 | **Only increase.** Never decrease anything. | Shrinking a stateful database automatically is dangerous; storage cannot shrink at all. |
| 6 | **Argo pre-check:** skip if the app is not `Healthy` or a sync is already running (`deployment_not_healthy`). Configurable (`argocd_precheck`), on by default. | Don't stack a rolling restart on a cluster that is already restarting or sick. |
| 7 | One change per target at a time (in-process lock). The Deployment runs **1 replica** with `Recreate`. | Two concurrent changes to the same target would mean two rolling restarts. |
| 8 | Optimistic concurrency: the state's `version` is sent as `expected_version`. On a conflict (409), re-read once and recompute; a second conflict skips (`gitops_conflict`). If the backend is unavailable, skip and **do not sync**. | Never overwrite someone else's change; never sync Argo without a confirmed commit. |
| 9 | **Modes** (see below). Replica changes are **never** committed directly; they always become a merge request. | Adding a vmstorage node is a big decision (data distribution, cost). A human approves it. |
| 10 | Sync only the listed resources (`argocd_sync_resources`), `prune: false`. "Operation already in progress" counts as success. | A minimal blast radius: one object, nothing ever deleted. |

Other safety details:

* Writes to the backend are **never retried after the server may have received them**
  (no double commits). Each write carries an `Idempotency-Key` header (hash of target +
  changes + alert fingerprints) so the backend can dedupe.
* Unknown target (no config entry for cluster + namespace + component) → `unknown_target`,
  logged as WARNING and counted. The service never guesses.
* If the backend has no value for what a strategy needs (e.g. no CPU limit) → `value_not_set`.
  The service never invents a starting value.
* Alerts for CPU and memory of the same target in one webhook become **one commit and one
  sync** — one rolling restart instead of two.

### Modes

| Mode | What happens |
|------|--------------|
| `dry_run` | Read the current state, compute and log the diff, return it. Change nothing. |
| `merge_request` | Open an MR with the change and a full explanation. No Argo sync. |
| `direct_commit` | Commit to the Argo source repo; **only if accepted**, sync Argo. |

The effective mode is the **safest** of: the global `mode`, the policy's `mode` (if set),
and what the strategy allows (replicas: at most `merge_request`). So a policy can make a
resource safer, never less safe. In global `dry_run`, even replicas are dry-run only.

---

## Configuration

### YAML file (`config.example.yaml`, mounted from a ConfigMap)

```yaml
mode: dry_run                     # global default
change_reason_prefix: "[vm-autoscaler]"
enabled_components: [vmstorage]
argocd_precheck: true
defaults:
  value_ref_id: "B"               # Grafana query refId holding the usage percent
policies:
  cpu:     { trigger_percent: 80, step_percent: 25, max: "16",   cooldown: 6h, scale_requests_with_limits: true }
  memory:  { trigger_percent: 80, step_percent: 25, max: "64Gi", cooldown: 6h, requests_equal_limits: true }
  storage: { trigger_percent: 80, step_percent: 20, max: "2Ti",  cooldown: 24h }
  replicas: { trigger_percent: 85, step: 1, max: 20, cooldown: 24h, mode: merge_request }
targets:
  - ocp_cluster: ocp-prod-region-a
    namespace: monitoring
    component: vmstorage
    argocd_app: victoria-metrics-region-a
    argocd_sync_resources:
      - { group: operator.victoriametrics.com, kind: VMCluster, name: vm-main, namespace: monitoring }
    storage_expansion_supported: true
    gitops_ref: {}                # passed as-is to the backend
    policies: { cpu: { max: "24" } }   # optional per-target overrides
```

* `cooldown` accepts `30m`, `6h`, `1d` (or ISO-8601 like `PT6H`).
* CPU: the **limit** grows by `step_percent` (alerts are "% of limit"). With
  `scale_requests_with_limits`, the request grows by the same step, never above the new limit.
* Memory: with `requests_equal_limits`, the request is set to the new limit (guaranteed
  memory — no OOM surprises for a database).
* The file is validated at startup. A bad file stops the pod with a clear error.

To add a target: add an entry under `targets`, make sure the Grafana rules produce the
matching `ocp_cluster` / `namespace` labels, and make sure the backend token may edit it.

### Environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `WEBHOOK_TOKEN` | – (required) | Shared with the Grafana contact point. Secret. |
| `CONFIG_PATH` | `/etc/vm-autoscaler/config.yaml` | |
| `GITOPS_BACKEND` | `internal` | `gitlab` (default in the OpenShift ConfigMap), `internal`, or `fake` (in-memory, local only) |
| `GITLAB_URL` | – | Required when `gitlab`, e.g. `https://gitlab.internal.example` |
| `GITLAB_TOKEN` | – | Required when `gitlab`. Sent as `PRIVATE-TOKEN`. Secret. |
| `GITOPS_API_URL` | – | Required when `internal` |
| `GITOPS_API_TOKEN` | – | Sent as `Authorization: Bearer …`. Secret. |
| `GITOPS_API_TIMEOUT_SECONDS` | `10` | Per request (internal backend and GitLab) |
| `GITOPS_API_MAX_RETRIES` | `3` | Reads only (internal backend and GitLab) |
| `GITOPS_STORES_ANNOTATIONS` | `true` | `false` → cooldowns in memory only (reset on restart; WARNING at startup) |
| `GITOPS_FAKE_SEED_PATH` | – | YAML seed for the fake backend |
| `ARGOCD_URL` | – | Required for `api`/`cli` |
| `ARGOCD_TOKEN` | – | Secret |
| `ARGOCD_SYNC_METHOD` | `api` | `api`, `cli` (needs the `argocd` binary in the image) or `fake` |
| `ARGOCD_INSECURE_SKIP_VERIFY` | `false` | Avoid; use `CA_BUNDLE_PATH` |
| `CA_BUNDLE_PATH` | – | Internal CA bundle for the backend and Argo |
| `HTTP_TIMEOUT_SECONDS` | `10` | Argo CD calls |
| `READINESS_CHECK_DEPENDENCIES` | `false` | `/readyz` also checks the backend/Argo URLs (2s timeout, cached 60s) |
| `LOG_LEVEL` | `INFO` | |

---

## Rollout plan

1. **`dry_run`** (default). Deploy, wire up Grafana, and watch the decisions in the logs and
   in `vm_autoscaler_decisions_total`. Check that the computed `old -> new` values make sense.
2. **`merge_request`**. Every change becomes an MR. A human reviews and merges; Argo applies
   it as usual. Leave it here until you trust the numbers.
3. **`direct_commit`** for CPU, memory and storage. Replicas stay `merge_request` forever.
   You can promote one resource at a time by setting the global mode to `direct_commit` and
   `mode: merge_request` on the policies you are not ready to automate yet.

## Important risks

* **Every resize is a rolling restart of vmstorage.** The VM operator restarts the pods one
  by one. While a node restarts, vminsert reroutes its share of the data to the other nodes,
  which raises *their* load. That's why the thresholds must not be too high: scaling at 95%
  CPU means the remaining nodes take the extra load while already near their limit. Keep
  triggers around 80% and steps moderate.
* **`at_max_cap` means a human must act.** The service has reached the configured ceiling.
  Usually the answer is more vmstorage nodes (and possibly more OpenShift nodes). Alert on
  `vm_autoscaler_decisions_total{reason="at_max_cap"}`.
* **Replica scale-down is never automatic**, and scale-up is only ever an MR: vmstorage is
  stateful, data does not move to new nodes, and removing a node loses its data from queries.
* **Storage:**
  * PVC expansion needs the StorageClass to have `allowVolumeExpansion: true`. If it doesn't,
    set `storage_expansion_supported: false` on the target; storage requests then skip with
    `expansion_not_supported`.
  * The VictoriaMetrics operator resizes the PVCs when the VMCluster storage request grows,
    unless the `operator.victoriametrics.com/pvc-allow-volume-expansion: "false"` annotation
    disables it.
  * When a vmstorage node reaches `-storage.minFreeDiskSpaceBytes` it turns **read-only** and
    vminsert reroutes its data to the other nodes. The storage alert must fire well before
    that (the example rule fires at 80% for 30m).
  * Storage never shrinks — a wrong `max` cannot be undone automatically.

## Shutdown and restarts (SIGTERM)

Rollouts, node drains and evictions all stop the pod with SIGTERM:

1. **preStop `sleep 5`** — OpenShift first removes the pod from the Service; the delay lets
   that settle so no new webhook lands on a pod that is shutting down.
2. **uvicorn drains** (it is PID 1 and gets SIGTERM directly): it stops accepting
   connections and waits up to **80s** (`--timeout-graceful-shutdown 80`) for in-flight
   webhooks to finish, then closes the GitLab/Argo HTTP clients.
3. **`terminationGracePeriodSeconds: 90`** — SIGKILL only after that.

A webhook that is still running after 80s is cancelled and Grafana gets an error, so it
re-sends the alert. Because state lives in Git (commit history / annotations), nothing is
lost and nothing is applied twice: the re-sent alert sees the cooldown if the commit landed.
The one gap: if the commit landed but the Argo sync call didn't happen, Argo auto-sync (if
enabled) still applies it; otherwise sync the app by hand. Normal webhooks take well under
a second; 80s only matters when GitLab or Argo are timing out.

There's no PodDisruptionBudget on purpose: with 1 replica it would block node drains.
The webhook is unavailable for a few seconds during a restart, which Grafana tolerates.

## Why the work is done inside the request (not a background task)

The webhook handler does the GitOps and Argo calls before returning `202` with the
decisions. This is the simpler correct choice:

* Grafana (and you, with `curl`) get the real result — useful in dry run.
* No work is lost if the pod stops right after answering: either Grafana got an answer
  for completed work, or it re-sends the alert on its repeat interval.
* Handling is idempotent (cooldown annotation + in-memory record + `Idempotency-Key`),
  so a re-send after a timeout is harmless.

All calls have timeouts. Keep `GITOPS_API_TIMEOUT_SECONDS × (retries + 1)` plus Argo
timeouts below the webhook timeout of your Grafana version.

---

## Run locally

Requires Python 3.12 (or [`uv`](https://github.com/astral-sh/uv), which fetches it).

```bash
make install            # venv + pinned dev dependencies
make check              # lint + typecheck + test
make run                # fake GitOps backend (examples/fake-state.yaml), fake Argo, dry_run
```

In another terminal:

```bash
make sample
# same as:
curl -sS -X POST http://127.0.0.1:8080/webhook/grafana \
  -H 'Authorization: Bearer dev-token' -H 'Content-Type: application/json' \
  --data @tests/fixtures/grafana_firing_cpu.json
```

Response (abridged):

```json
{"mode": "dry_run",
 "decisions": [{"resource": "cpu", "value": 91.2, "decision": "dry_run",
                "old": "4", "new": "5", "ocp_cluster": "ocp-prod-region-a"}]}
```

Other fixtures in `tests/fixtures/`: `grafana_multi.json` (several targets and resources),
`grafana_resolved.json`, `grafana_missing_label.json`, `grafana_autoscale_false.json`.
To try `direct_commit` locally, copy `config.example.yaml`, set `mode: direct_commit`, and
point `CONFIG_PATH` at it — the fake backend and fake Argo record everything in memory.

## Deploy to OpenShift

```bash
make image IMAGE=<registry>/vm-autoscaler:0.1.0 && podman push <registry>/vm-autoscaler:0.1.0
oc new-project vm-autoscaler
oc -n vm-autoscaler create secret generic vm-autoscaler-secrets \
  --from-literal=WEBHOOK_TOKEN="$(openssl rand -hex 32)" \
  --from-literal=GITLAB_TOKEN=... \
  --from-literal=ARGOCD_TOKEN=...
# edit deploy/openshift/configmap.yaml (URLs, targets) and kustomization.yaml (image)
oc apply -k deploy/openshift
```

What's in `deploy/openshift/`:

* `deployment.yaml` — 1 replica, `Recreate`, `restricted-v2` compatible (non-root, no
  privilege escalation, all capabilities dropped, seccomp `RuntimeDefault`, read-only root
  filesystem with an `emptyDir` on `/tmp`), probes on `/healthz` and `/readyz`.
* `configmap.yaml` — non-secret env, `config.yaml`, and an injected trusted-CA bundle.
* `secret.example.yaml` — the keys of the Secret, placeholders only. **Not applied.**
* `service.yaml`; `route.yaml` only if Grafana is outside the cluster (not applied by default).
* `networkpolicy.yaml` — ingress only from the Grafana namespace and the monitoring scrapers.
* `servicemonitor.yaml` (or `vmservicescrape.yaml` for the VM operator).

The image is built from `registry.access.redhat.com/ubi9/python-312`, installs only the
hash-pinned packages in `requirements.txt`, and runs as UID 1001 / group 0 (works with
OpenShift's random UIDs). For air-gapped builds pass `--build-arg PIP_INDEX_URL=<mirror>`.
At runtime the service makes no internet calls.

### Least privilege

* **GitLab token** (`GITOPS_BACKEND=gitlab`): a project access token on the repo(s) that
  hold the manifests — see "GitLab mode".
* **GitOps backend token** (`GITOPS_BACKEND=internal`): must be scoped to the vmstorage
  components listed in `targets` only (requirement for the backend team).
* **Argo CD token:** a project role allowed only `get` and `sync` on the listed apps:

  ```yaml
  # AppProject "victoria-metrics"
  spec:
    roles:
      - name: vm-autoscaler
        description: vm-autoscaler may read and sync the VictoriaMetrics apps
        policies:
          - p, proj:victoria-metrics:vm-autoscaler, applications, get, victoria-metrics/victoria-metrics-region-a, allow
          - p, proj:victoria-metrics:vm-autoscaler, applications, sync, victoria-metrics/victoria-metrics-region-a, allow
  ```

  Create the token with `argocd proj role create-token victoria-metrics vm-autoscaler`.
* The pod does not mount a Kubernetes service account token: it never calls the cluster API.
* Tokens are never logged. `Authorization` headers and anything that looks like a bearer
  token are redacted from log lines.

---

## GitLab mode

The internal GitOps backend is not usable yet, so by default (`GITOPS_BACKEND=gitlab`) the
service edits the Argo CD source repo itself through the **GitLab REST API v4**. Nothing
else changes: the same safety rules, modes, cooldowns and Argo sync apply.
**To switch back to the internal backend later, set `GITOPS_BACKEND=internal`** (and
`GITOPS_API_URL` / `GITOPS_API_TOKEN`). Nothing else needs to change.

### How it works

| Step | GitLab API call |
|------|-----------------|
| Read state | `GET /projects/:id/repository/files/:file?ref=<branch>` (base64 content + `last_commit_id`) |
| Direct commit | `POST /projects/:id/repository/commits`, one `update` action with `last_commit_id` |
| Merge request | the same commit with `start_branch=<branch>` and `branch=vm-autoscaler/<cluster>-<resources>-<UTC time>`, then `POST /projects/:id/merge_requests` (`remove_source_branch: true`) |
| Readiness | `GET /projects/:id` for each configured project, must answer 200 (cached 60s) |

* **One commit** carries all changes of one target.
* **Cooldowns come from the commit history** (`gitlab.cooldown_source: commits`, the
  default). On every state read the service lists the branch's commits for the file
  (`GET /projects/:id/repository/commits?ref_name=<branch>&path=<file>&since=<now - longest
  cooldown>&first_parent=true`) and takes, per resource, the date of the newest commit
  whose message contains our title for this target, e.g.
  `[vm-autoscaler] ocp-prod-region-a/monitoring vmstorage cpu 4 -> 5 (...)`.
  * Nothing is written into the manifest, so diffs show only the real value changes.
  * `first_parent=true` + the commit date means an approved MR counts from when it was
    **merged** (its merge or squash commit carries the MR title), not from when the bot
    opened it.
  * A message that wrongly matches (e.g. a human `Revert "[vm-autoscaler] …"`) can only
    start a cooldown early, never cause an extra change.
  * **The commit title format is now load-bearing.** If you change `change_reason_prefix`,
    older commits stop matching and cooldowns expire early once.
  * Check once in `dry_run` against your GitLab that a merged MR is seen (it should show
    `cooldown_active` for that resource). The docs don't spell out how `path` and
    `first_parent` combine for merge commits.
  * `cooldown_source: annotations` brings back the annotation behaviour.
* **Concurrency:** `ComponentState.version` is the file's `last_commit_id`. Before
  writing, the service re-reads the file. If `last_commit_id` moved, or a value differs
  from what the decision was based on, that's a conflict (re-read once, as for the internal
  backend). The commit carries `last_commit_id` too, so GitLab rejects it if the file
  changed in the gap. We re-read instead of caching the text from the first read: it keeps
  the service stateless and needs no interface change, at the cost of one extra GET.
* **Minimal diffs:** the YAML is edited with ruamel.yaml in round-trip mode. Comments, key
  order, quotes and indentation stay as they are, and values keep their style (`"4"` stays
  a quoted string, `4` stays a number, `16384Mi` stays in Mi, `4000m` stays in millicores).
  A guard **refuses to commit** if more lines changed than expected (reason `gitops_error`,
  logged). That only happens if a file's formatting can't be round-tripped; fix the file's
  formatting by hand once.
* **Nothing is ever created** except the cooldown `annotations` map (annotations mode only). A missing value
  (e.g. no CPU limit) reads as "not set" and the request is skipped with `value_not_set`.
* **Merge requests are not duplicated:** the MR description carries the change's
  idempotency key. If an open MR with that key exists (e.g. after a pod restart), it is
  returned instead. If MR creation fails after the branch commit, the branch is deleted.
* **Errors:** timeouts/5xx → `gitops_unavailable` (GETs retried, POSTs never retried after
  GitLab got them); stale file / 409 → `gitops_conflict`; 404 → `gitops_target_not_found`
  with the missing project, branch or file named; 401/403 → `gitops_rejected` with a hint
  about the token scope and branch protection.
* The commit message is the same as for the internal backend:
  `[vm-autoscaler] ocp-prod-region-a/monitoring vmstorage cpu 4 -> 5 (usage 91.2% >= 80%), memory 16Gi -> 20Gi (usage 85.0% >= 80%)`,
  followed by details and an `Idempotency-Key:` line.

### Config

Every target needs a `gitlab` block when `GITOPS_BACKEND=gitlab` (checked at startup):

```yaml
gitlab:                                   # global
  author_name: vm-autoscaler
  author_email: vm-autoscaler@example.local
  commit_message_prefix: "[vm-autoscaler]"  # optional; replaces change_reason_prefix

targets:
  - ocp_cluster: ocp-prod-region-a
    # ...
    gitlab:
      project: infra/monitoring          # path or numeric id
      branch: main
      file: clusters/region-a/vmcluster.yaml
      document_kind: VMCluster           # multi-document files: pick by kind ...
      document_name: vm-main             # ... and metadata.name
      yaml_root: spec.vmstorage
      annotations_path: metadata.annotations   # annotations mode only; relative to the document root
```

Global option `gitlab.cooldown_source: commits | annotations` (default `commits`).

Paths under `yaml_root`: `resources.requests.cpu`, `resources.limits.cpu`,
`resources.requests.memory`, `resources.limits.memory`,
`storage.volumeClaimTemplate.spec.resources.requests.storage`, `replicaCount`.

**Helm values file:** set `document_kind: null`, `document_name: null`, `yaml_root: vmstorage`.
With the default `cooldown_source: commits` nothing else is needed. Only in annotations
mode: a values file has no `metadata`, so set `annotations_path` to a map whose parent exists, e.g.
a top-level `vmAutoscaler` key (`annotations_path: vmAutoscaler`). **Do not** point it at
`vmstorage.podAnnotations`: changing pod annotations restarts the pods. If the chart
validates its values against a schema, add the key there too.

### GitLab setup

1. **Token:** create a **project access token** (or a group access token if the manifests
   live in several projects) with scope **`api`**. Put it in the Secret as `GITLAB_TOKEN`.
2. **Role:**
   * `merge_request` mode: **Developer** is enough (it pushes `vm-autoscaler/*` branches and
     opens MRs). Don't protect `vm-autoscaler/*` branches, or allow Developers to push to them.
   * `direct_commit` mode on a protected `main`: the token's role must be in "Allowed to
     push and merge" for that branch — usually **Maintainer**.
3. **If branch protection doesn't allow a bot to push to `main`, use `merge_request`
   mode** and merge by hand (or with your normal MR approval rules).
4. The token is sent only as the `PRIVATE-TOKEN` header. It is never logged, and it is
   removed from any GitLab response text that ends up in an error message.

## Connecting the internal GitOps API

The backend API is not final, so the client in `app/adapters/gitops_internal.py` uses
placeholders. List every place to touch with:

```bash
grep -rn "TODO(api)" app/
```

What to fill in — **all in `app/adapters/gitops_internal.py`**, nothing else changes:

| Item | What it does |
|------|--------------|
| `_PATH_STATE`, `_PATH_APPLY`, `_PATH_CHANGE_REQUEST` | Real URL paths. Change the HTTP method in `apply_changes` if it should be `PATCH`/`PUT`. |
| `_to_state_params(target)` | How a component is identified (query params). `target.gitops_ref` from config is passed through. |
| `_StateResponseWire` + `_from_state_response(body)` | Map the GET response to `ComponentState` (cpu/memory request+limit, storage, replicas, annotations, version). |
| `_changes_to_wire(changes)` | How one change is described (each has `field`, `yaml_path`, `old`, `new`). |
| `_to_apply_payload(...)` | Direct-commit body: changes, annotations, commit message, `expected_version`. |
| `_to_change_request_payload(...)` | MR body: changes, annotations, title, description. |
| `_WriteResponseWire` + `_from_apply_response` / `_from_change_request_response` | Map write responses to `ChangeResult` (`accepted`, `change_id`, `url`, `message`). |

Then:

1. Update the placeholder JSON in `tests/test_gitops_internal.py` and
   `tests/test_end_to_end.py` to the real shapes, and run `make test`.
2. If the backend cannot store annotations yet, set `GITOPS_STORES_ANNOTATIONS=false`
   (cooldowns are then in memory only and reset on restart).
3. Error mapping is already done for you: 409 → conflict (re-read + retry once),
   404 → unknown target, other 4xx → rejected (status + first 500 chars of the body),
   5xx/timeouts/connection errors → unavailable, bad JSON/shape → response error.

## Observability

Logs are JSON, one object per line, with `request_id`, `fingerprint`, `ocp_cluster`,
`namespace`, `component`, `resource`, `value`, `decision`, `reason`, `old`, `new`, `mode`.

Metrics on `/metrics`:

| Metric | Labels |
|--------|--------|
| `vm_autoscaler_alerts_received_total` | `status` |
| `vm_autoscaler_decisions_total` | `ocp_cluster`, `resource`, `decision`, `reason` |
| `vm_autoscaler_unknown_targets_total` | – |
| `vm_autoscaler_gitops_requests_total` | `operation`, `result` |
| `vm_autoscaler_gitops_request_duration_seconds` | `operation` |
| `vm_autoscaler_argocd_sync_total` | `result` |
| `vm_autoscaler_handle_duration_seconds` | – |

Decisions: `applied`, `change_request_opened`, `dry_run`, `skipped`. Skip reasons include
`cooldown_active`, `at_max_cap`, `below_threshold`, `value_not_set`, `unknown_target`,
`deployment_not_healthy`, `gitops_conflict`, `gitops_unavailable`, `expansion_not_supported`.
An `applied` decision with reason `argocd_sync_failed` means the commit landed but the sync
call failed (Argo auto-sync, if enabled, will still pick it up).

## Code map

```
app/
  main.py                      FastAPI app factory + lifespan
  container.py                 composition root: picks adapters from env
  config.py                    Settings (env) + YAML config models
  readiness.py                 /readyz checks
  observability.py             JSON logging, redaction, Prometheus metrics
  api/routes.py                /webhook/grafana, /healthz, /readyz, /metrics
  api/security.py              bearer token check (constant-time)
  domain/models.py             Target, Policy, ScaleRequest, ComponentState, ScaleDecision, ...
  domain/ports.py              GitOpsBackend and DeploymentSyncer protocols
  domain/strategies.py         Cpu/Memory/Storage/Replica strategies + registry
  domain/quantity.py           Kubernetes quantity parse/format/round
  domain/errors.py             our exceptions
  services/alert_parser.py     Grafana payload -> ScaleRequests
  services/scaling_service.py  the decision rules and orchestration
  adapters/gitops_gitlab.py    GitLab REST API v4 backend (GITOPS_BACKEND=gitlab)
  adapters/manifest.py         round-trip YAML editing (ruamel.yaml), diff guard
  adapters/gitops_internal.py  real backend client (TODO(api) placeholders)
  adapters/gitops_fake.py      in-memory backend
  adapters/argocd_api.py       Argo CD REST adapter
  adapters/argocd_cli.py       Argo CD CLI adapter
  adapters/argocd_fake.py      in-memory Argo stand-in
```
