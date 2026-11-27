# vm-autoscaler

A small service that grows the **vmstorage PVC size** of VictoriaMetrics clusters on
OpenShift **through GitOps**, when Grafana says the disks are filling up.

```
Grafana alert ──webhook──▶ vm-autoscaler ──▶ GitLab API (edits Argo source repo)   [GITOPS_BACKEND=gitlab]
                                    │      or Internal GitOps backend            [GITOPS_BACKEND=internal]
                                    └──────▶ Argo CD API (sync app)
                                                   └─▶ VM operator resizes the vmstorage PVCs
```

1. Grafana fires an alert ("fullest vmstorage PVC is 85% used") and calls `POST /webhook/grafana`.
2. The service reads the cluster, namespace, component and value from the alert labels.
3. It reads the current PVC size of that cluster/namespace from the Argo CD source repo
   (through the GitLab API, or through the internal GitOps backend — see "GitLab mode").
4. It computes a safe new size (one step up, rounded up to whole Gi, capped, respecting a cooldown).
5. It writes the change to the repo (a direct commit, or a merge request).
6. It triggers an Argo CD sync of just the `VMCluster` object. The VM operator then resizes
   the PVCs.

The service never runs `git` or clones anything, and never edits the cluster directly.
Git is the source of truth, and every change is a normal commit or MR with a clear message:

```
[vm-autoscaler] ocp-prod-region-a/monitoring vmstorage storage 30Gi -> 36Gi (usage 85.0% >= 80%)
```

## Scope

Only the **vmstorage PVC size** is scaled for now. CPU, memory and replica scaling were
removed to keep the code small. They can be restored from git history: commit `fe03ac2`
("Full version: cpu, memory, storage and replica scaling").

---

## Repo layout (GitLab)

The Argo CD source repo (default branch `main`) looks like this:

```
<ocp_cluster>/                      # one directory per OpenShift cluster, e.g. ocp-prod-region-a
  <namespace>/                      # one directory per namespace, e.g. monitoring
    <values file>                   # Helm values; the PVC size is at vmstorage.pvc.size
```

```yaml
# <ocp_cluster>/<namespace>/<values file>
vmstorage:
  pvc:
    size: 30Gi
```

The service finds the file of each target with **`gitlab.file_template`**, filled in with
the target's `ocp_cluster`, `namespace` and `component`:

```yaml
gitlab:
  project: group/repo                                  # GitLab project path or numeric id
  branch: main
  file_template: "{ocp_cluster}/{namespace}/values.yaml"
  storage_path: vmstorage.pvc.size                     # dotted path inside the file
```

So the target `ocp-prod-region-a` / `monitoring` edits
`ocp-prod-region-a/monitoring/values.yaml` at `vmstorage.pvc.size`.

* `ocp_cluster` and `namespace` in each target (and in the Grafana alert labels) must be
  **exactly the directory names** in the repo.
* A single target can override any of these with its own `gitlab:` block, e.g.
  `gitlab: { file: special/path/values.yaml }` or `gitlab: { storage_path: vmstorage.disk.size }`.
* **Before deploying, fill in the TODO placeholders** in `config.example.yaml` /
  `deploy/openshift/configmap.yaml`: `gitlab.project` and the real file name in
  `gitlab.file_template` (it is `TODO_VALUES_FILE.yaml` until you set it). A wrong path is
  reported clearly: GitLab returns 404 and the decision says which of project, branch or
  file was not found.
* The service never creates keys. If `vmstorage.pvc.size` is missing in a file, that target
  is skipped with `value_not_set`.

---

## Safety rules (and why each one exists)

Every rule below is implemented in `app/services/scaling_service.py` or
`app/domain/strategies.py` and has a test.

| # | Rule | Why |
|---|------|-----|
| 1 | Only act if the alert label `autoscale="true"` and the component is in `enabled_components` (only `vmstorage`). | An explicit opt-in per alert rule. A missing label means "off". |
| 2 | Only act if the value ≥ the policy's `trigger_percent`. The value is read only from the query `value_ref_id`; if it is absent or not a number the alert is skipped (`value_missing`) and a WARNING lists the available refIds. | Protects against a misconfigured Grafana rule that fires too early. |
| 3 | **Cooldown** (24h by default). The last-scaled time comes from Git: in GitLab mode from the **commit history** of the values file (default), otherwise from an annotation `vm-autoscaler/last-scaled-storage` written with each change. | Gives the resize time to finish and show effect. Keeps the service stateless: no database, history lives in Git. Grafana re-sends firing alerts every `repeat_interval`; these are skipped. |
| 4 | New size = current × (1 + step%), rounded up to whole Gi, capped at `max`. Already at max → skip `at_max_cap`, logged as **WARNING**. | Small, predictable steps. `at_max_cap` means a human must act (add vmstorage nodes, reduce retention). |
| 5 | **Only increase. Storage never shrinks.** | A PVC cannot be shrunk; a smaller value in Git would only break the sync. |
| 6 | **Argo pre-check:** skip if the app is not `Healthy` **and `Synced`**, or a sync is already running (`deployment_not_healthy`). Configurable (`argocd_precheck`), on by default. | Don't stack a change on a cluster that is already changing or sick. An `OutOfSync` app has an unrelated pending Git change that our targeted sync could roll out too. |
| 7 | One change per target at a time (in-process lock). The Deployment runs **1 replica** with `Recreate`. | Two concurrent changes to the same target would conflict. |
| 8 | Optimistic concurrency: the state's `version` (in GitLab: the file's `last_commit_id`) must still match when writing. On a conflict, re-read once and recompute; a second conflict skips (`gitops_conflict`). If the backend is unavailable, skip and **do not sync**. | Never overwrite someone else's change; never sync Argo without a confirmed commit. |
| 9 | **Modes** (see below). | Lets you roll out from "watch only" to "fully automatic". |
| 10 | Sync only the listed resources (`argocd_sync_resources`), `prune: false`. "Operation already in progress" counts as success. | A minimal blast radius: one object, nothing ever deleted. |
| 11 | `storage_expansion_supported: false` on a target → skip with `expansion_not_supported`. | For StorageClasses that cannot expand volumes. |

Other safety details:

* Writes are **never retried after the server may have received them** (no double
  commits). Each write carries an idempotency key (hash of target + change + alert
  fingerprint); in GitLab mode it is in the commit message and MR description.
* Unknown target (no config entry for cluster + namespace + component) → `unknown_target`,
  logged as WARNING and counted. The service never guesses.
* If the repo has no value for the PVC size → `value_not_set`. The service never invents a
  starting value.

### Modes

| Mode | What happens |
|------|--------------|
| `dry_run` | Read the current size, compute and log the diff, return it. Change nothing. |
| `merge_request` | Open an MR with the change and a full explanation. No Argo sync. |
| `direct_commit` | Commit to the Argo source repo; **only if accepted**, sync Argo. |

The effective mode is the **safest** of the global `mode` and the policy's `mode` (if
set). A policy can make things safer, never less safe.

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
gitlab:                           # see "Repo layout" and "GitLab mode"
  project: TODO/group/repo
  branch: main
  file_template: "{ocp_cluster}/{namespace}/TODO_VALUES_FILE.yaml"
  storage_path: vmstorage.pvc.size
  cooldown_source: commits
policies:
  storage: { trigger_percent: 80, step_percent: 20, max: "2Ti", cooldown: 24h }
targets:
  - ocp_cluster: ocp-prod-region-a
    namespace: monitoring
    component: vmstorage
    argocd_app: victoria-metrics-region-a
    argocd_sync_resources:
      - { group: operator.victoriametrics.com, kind: VMCluster, name: vm-main, namespace: monitoring }
    storage_expansion_supported: true
    # gitlab: { file: other/path.yaml }        # optional per-target overrides
    # policies: { storage: { max: "4Ti" } }    # optional per-target policy overrides
```

* `cooldown` accepts `30m`, `6h`, `1d` (or ISO-8601 like `PT24H`).
* `max` is the largest size the service will ever write (e.g. `2Ti`).
* Only the `storage` policy exists; `cpu`, `memory` or `replicas` entries are rejected.
* The file is validated at startup. A bad file stops the pod with a clear error.

To add a target: add an entry under `targets` (its `ocp_cluster`/`namespace` must match the
repo directories and the Grafana labels), and make sure the GitLab token may push to the repo.

### Environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `WEBHOOK_TOKEN` | – (required) | Shared with the Grafana contact point. Secret. At least 16 characters (the service refuses to start otherwise); use `openssl rand -hex 32`. |
| `CONFIG_PATH` | `/etc/vm-autoscaler/config.yaml` | |
| `GITOPS_BACKEND` | – (required) | `gitlab` (set in the OpenShift ConfigMap), `internal`, or `fake` (in-memory, local only). The service refuses to start without it. |
| `GITLAB_URL` | – | Required when `gitlab`, e.g. `https://gitlab.internal.example` |
| `GITLAB_TOKEN` | – | Required when `gitlab`. Sent as `PRIVATE-TOKEN`. Secret. |
| `GITOPS_API_URL` | – | Required when `internal` |
| `GITOPS_API_TOKEN` | – | `internal` only. Sent as `Authorization: Bearer …`. Secret. |
| `GITLAB_TIMEOUT_SECONDS` | `GITOPS_API_TIMEOUT_SECONDS` | GitLab, per request. Unset: uses the `GITOPS_API_TIMEOUT_SECONDS` value |
| `GITLAB_MAX_RETRIES` | `GITOPS_API_MAX_RETRIES` | GitLab, reads only. Unset: uses the `GITOPS_API_MAX_RETRIES` value |
| `GITOPS_API_TIMEOUT_SECONDS` | `10` | Per request, internal backend (also the GitLab fallback, so old configs keep working) |
| `GITOPS_API_MAX_RETRIES` | `3` | Reads only, internal backend (also the GitLab fallback) |
| `GITOPS_STORES_ANNOTATIONS` | `true` | `internal` only: `false` → cooldowns in memory only (reset on restart; WARNING at startup) |
| `GITOPS_FAKE_SEED_PATH` | – | YAML seed for the fake backend |
| `ARGOCD_URL` | – | Required for `api`/`cli` |
| `ARGOCD_TOKEN` | – | Secret |
| `ARGOCD_SYNC_METHOD` | `api` | `api`, `cli` (needs the `argocd` binary in the image) or `fake` |
| `ARGOCD_INSECURE_SKIP_VERIFY` | `false` | Avoid; use `CA_BUNDLE_PATH` |
| `ARGOCD_CLI_PATH` | `argocd` | For `cli` |
| `CA_BUNDLE_PATH` | – | Internal CA bundle for GitLab, the backend and Argo |
| `ARGOCD_TIMEOUT_SECONDS` | `10` | Argo CD calls. The old name `HTTP_TIMEOUT_SECONDS` still works |
| `READINESS_CHECK_DEPENDENCIES` | `false` | `/readyz` also checks the backend/Argo URLs and, in GitLab mode, that every project is readable (2s timeout, cached 60s). Off by default so a GitLab blip does not make the only pod unready |
| `LOG_LEVEL` | `INFO` | |

---

## GitLab mode

By default in OpenShift (`GITOPS_BACKEND=gitlab`) the service edits the Argo CD source repo
itself through the **GitLab REST API v4**. The safety rules, modes, cooldown and Argo sync
are the same as for the internal backend.
**To switch to the internal GitOps backend later, set `GITOPS_BACKEND=internal`** (plus
`GITOPS_API_URL` / `GITOPS_API_TOKEN`). Nothing else needs to change.

### How it works

| Step | GitLab API call |
|------|-----------------|
| Read state | `GET /projects/:id/repository/files/:file?ref=<branch>` (base64 content + `last_commit_id`) |
| Cooldown | `GET /projects/:id/repository/commits?ref_name=<branch>&path=<file>&since=<now - cooldown>&first_parent=true` |
| Direct commit | `POST /projects/:id/repository/commits`, one `update` action with `last_commit_id` |
| Merge request | the same commit with `start_branch=<branch>` and `branch=vm-autoscaler/<cluster>-<namespace>-storage-<UTC time>-<8-char change key>`, then `POST /projects/:id/merge_requests` (`remove_source_branch: true`) |
| Readiness | `GET /projects/:id` for each configured project, must answer 200 (cached 60s) |

* **Concurrency:** before writing, the service re-reads the file. If `last_commit_id`
  moved, or the size differs from what the decision was based on, that's a conflict
  (re-read once). The commit carries `last_commit_id` too, so GitLab itself rejects it if
  the file changed in the gap.
* **Minimal diffs:** the YAML is edited with ruamel.yaml in round-trip mode. Comments, key
  order, quotes and indentation stay as they are, and the value keeps its style
  (`"30Gi"` stays quoted, `30720Mi` stays in Mi). The diff is one line:
  `size: 30Gi` → `size: 36Gi`. A guard **refuses to commit** if more lines changed than
  expected (reason `gitops_error`, logged). That only happens if a file's formatting can't
  be round-tripped; fix the file's formatting by hand once.
* **Cooldowns come from the commit history** (`gitlab.cooldown_source: commits`, the
  default): the date of the newest commit on the branch whose message contains our title
  for this target, e.g.
  `[vm-autoscaler] ocp-prod-region-a/monitoring vmstorage storage 30Gi -> 36Gi (...)`.
  * Nothing is written into the values file except the size.
  * `first_parent=true` + the commit date means an approved MR counts from when it was
    **merged** (its merge or squash commit carries the MR title), not from when it was opened.
  * A message that wrongly matches (e.g. a human `Revert "[vm-autoscaler] …"`) can only
    start a cooldown early, never cause an extra change.
  * **The commit title format is load-bearing.** If you change `change_reason_prefix`,
    older commits stop matching and cooldowns expire early once. It is the only prefix
    setting; the old `gitlab.commit_message_prefix` still works (with a deprecation
    warning) but a different value in both places is a startup error.
  * Check once in `dry_run` against your GitLab that a merged MR is seen (it should show
    `cooldown_active`). The GitLab docs don't spell out how `path` and `first_parent`
    combine for merge commits.
  * `cooldown_source: annotations` instead writes `vm-autoscaler/last-scaled-storage` into
    the values file under `gitlab.annotations_path` (default: a top-level `vmAutoscaler`
    map). Do **not** point it at pod annotations: changing those restarts the pods. If the
    chart validates its values against a schema, add the key there too.
* **One open merge request per target at a time.** While a vm-autoscaler MR for a
  cluster/namespace is open, new alerts for it return that MR instead of opening another
  (so resizes never pile up while waiting for approval). The MR description also carries
  the change's idempotency key, so a re-sent alert after a pod restart finds its own MR.
* **If MR creation fails after the branch commit:** on a definite rejection the branch is
  deleted. On a timeout (GitLab may have created the MR anyway) the service looks the MR
  up by its branch and returns it; if it can't find it, the branch is kept and a warning
  names it — delete it by hand if no MR shows up.
* **Line endings:** CRLF files stay CRLF. Any edit that would rewrite more than the size
  line (including line endings) is refused.
* **Argo sync:** right after the commit the service asks Argo CD to refresh the app
  (`GET /api/v1/applications/<app>?refresh=normal`) and then syncs it, so Argo doesn't
  sync a cached older revision. The existing `applications, get` permission covers it.
* **Errors:** timeouts/5xx → `gitops_unavailable` (GETs retried, POSTs never retried after
  GitLab got them); stale file / 409 → `gitops_conflict`; 404 → `gitops_target_not_found`
  with the missing project, branch or file named; 401/403 → `gitops_rejected` with a hint
  about the token scope and branch protection.

### GitLab setup

1. **Token:** create a **project access token** on the repo with scope **`api`**. Put it in
   the Secret as `GITLAB_TOKEN`.
2. **Role:**
   * `merge_request` mode: **Developer** is enough (it pushes `vm-autoscaler/*` branches and
     opens MRs). Don't protect `vm-autoscaler/*` branches, or allow Developers to push to them.
   * `direct_commit` mode on a protected `main`: the token's role must be in "Allowed to
     push and merge" for that branch — usually **Maintainer**.
3. **If branch protection doesn't allow a bot to push to `main`, use `merge_request`
   mode** and merge by hand (or with your normal MR approval rules).
4. The token is sent only as the `PRIVATE-TOKEN` header. It is never logged, and it is
   removed from any GitLab response text that ends up in an error message.

---

## Rollout plan

1. **Fill in the TODOs** (`gitlab.project`, the values file name, Argo app names, the
   `VMCluster` name) and deploy in **`dry_run`** (the default). Wire up Grafana and watch
   the decisions in the logs and in `vm_autoscaler_decisions_total`. Check that
   `old -> new` shows the real sizes from the repo (this proves the file path is right).
2. **`merge_request`**. Every resize becomes an MR. A human reviews and merges; Argo applies
   it as usual. Stay here until you trust the numbers.
3. **`direct_commit`**. The service commits and syncs by itself.

## Storage risks

* **Volume expansion must be allowed.** PVC expansion needs the StorageClass to have
  `allowVolumeExpansion: true`. If it doesn't, set `storage_expansion_supported: false` on
  the target; requests then skip with `expansion_not_supported`.
* **The VM operator resizes the PVCs** when the VMCluster storage request grows, unless the
  `operator.victoriametrics.com/pvc-allow-volume-expansion: "false"` annotation disables it.
  Check that your chart passes `vmstorage.pvc.size` through to the VMCluster storage request.
* **Storage never shrinks.** A wrong `max` cannot be undone automatically, and a PVC cannot
  be made smaller. Pick `max` carefully.
* **Fire early.** When a vmstorage node reaches `-storage.minFreeDiskSpaceBytes` it turns
  **read-only** and vminsert reroutes its data to the other nodes, which fills *them* faster.
  The storage alert must fire well before that (the example rule fires at 80% for 30m).
* **`at_max_cap` means a human must act.** The service has reached the configured ceiling.
  The answer is usually more vmstorage nodes or shorter retention. Alert on
  `vm_autoscaler_decisions_total{reason="at_max_cap"}`.

## Shutdown and restarts (SIGTERM)

Rollouts, node drains and evictions all stop the pod with SIGTERM:

1. **preStop `sleep 5`** — OpenShift first removes the pod from the Service; the delay lets
   that settle so no new webhook lands on a pod that is shutting down.
2. **uvicorn drains** (it is PID 1 and gets SIGTERM directly): it stops accepting
   connections and waits up to **80s** (`--timeout-graceful-shutdown 80`) for in-flight
   webhooks to finish, then closes the GitLab/Argo HTTP clients.
3. **`terminationGracePeriodSeconds: 90`** — SIGKILL only after that.

Worst case, one webhook can take a few minutes: every GitLab GET (file, commit history,
re-read) is retried up to `GITLAB_MAX_RETRIES` times with a
`GITLAB_TIMEOUT_SECONDS` timeout, plus the Argo status, refresh and sync calls. That
only happens when GitLab or Argo is timing out. There is deliberately no hard time limit
per webhook: cancelling in the middle of a GitLab write would leave it unknown whether the
commit landed. The per-target lock, the re-read before writing, and the cooldown from Git
history keep this safe; at worst Grafana sees a timeout and re-sends. To shorten it,
lower `GITLAB_TIMEOUT_SECONDS` (e.g. 5) and `GITLAB_MAX_RETRIES` (e.g. 2).

A webhook that is still running after 80s is cancelled and Grafana gets an error, so it
re-sends the alert. Because state lives in Git (commit history), nothing is lost and
nothing is applied twice: the re-sent alert sees the cooldown if the commit landed.
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
* Handling is idempotent (cooldown from Git + in-memory record + idempotency key), so a
  re-send after a timeout is harmless.
* When any decision is `gitops_unavailable` or `argocd_unavailable` (a transient outage),
  the handler returns `503` with the same JSON body, so Grafana re-sends the alert
  promptly. Every other outcome, including other skips, is `202`.

All calls have timeouts. Keep `GITLAB_TIMEOUT_SECONDS × (retries + 1)` plus Argo
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
  -H 'Authorization: Bearer dev-webhook-token-local' -H 'Content-Type: application/json' \
  --data @tests/fixtures/grafana_firing_storage.json
```

Response (abridged):

```json
{"mode": "dry_run",
 "decisions": [{"resource": "storage", "value": 85.0, "decision": "dry_run",
                "old": "30Gi", "new": "36Gi", "ocp_cluster": "ocp-prod-region-a"}]}
```

Other fixtures in `tests/fixtures/`: `grafana_multi.json` (several clusters, one unknown),
`grafana_resolved.json`, `grafana_missing_label.json`, `grafana_autoscale_false.json`.
To try `direct_commit` locally, copy `config.example.yaml`, set `mode: direct_commit`, and
point `CONFIG_PATH` at it — the fake backend and fake Argo record everything in memory.

## Deploy to OpenShift

Everything is deployed with `oc` and kustomize (`-k`), which is built into `oc`.

**1. Once, before the first deploy**

```bash
# Build and push the image
make image IMAGE=<registry>/vm-autoscaler:0.1.0 && podman push <registry>/vm-autoscaler:0.1.0

# Create the namespace yourself (the manifests never create it).
# If you use another name, change `namespace:` in deploy/openshift/kustomization.yaml.
oc new-project vm-autoscaler        # or your own namespace tooling

# Create the Secret (not part of the manifests, so tokens never live in Git)
oc -n vm-autoscaler create secret generic vm-autoscaler-secrets \
  --from-literal=WEBHOOK_TOKEN="$(openssl rand -hex 32)" \
  --from-literal=GITLAB_TOKEN=... \
  --from-literal=ARGOCD_TOKEN=...
```

Fill in the `TODO`s first: `deploy/openshift/configmap.yaml` (`GITLAB_URL`, `ARGOCD_URL`,
`gitlab.project`, `gitlab.file_template`, targets, Argo app names) and
`deploy/openshift/kustomization.yaml` (`images:` registry and tag).

**2. Plan** — see exactly what would change, without changing anything:

```bash
oc kustomize deploy/openshift                     # the rendered YAML
oc apply -k deploy/openshift --dry-run=server     # server-side validation (schemas, SCC admission)
oc diff -k deploy/openshift                       # diff against what is running
```

`oc diff` exits with code 1 when there are differences; that is expected, not an error.

**3. Apply and check**

```bash
oc apply -k deploy/openshift
oc -n vm-autoscaler rollout status deployment/vm-autoscaler
oc -n vm-autoscaler logs deployment/vm-autoscaler | head    # JSON logs; look for "vm-autoscaler started"
oc -n vm-autoscaler exec deployment/vm-autoscaler -- \
  python -c "import urllib.request as u; print(u.urlopen('http://localhost:8080/readyz').read().decode())"
```

`/readyz` only shows `"gitlab_project:<project>": true` when `READINESS_CHECK_DEPENDENCIES=true`;
`false` means the token or project path is wrong (use it for the first rollout check, then
leave it off so a GitLab blip does not make the only pod unready). Start with `mode: dry_run` and watch the decisions in the logs before moving
on (see "Rollout plan").

Config changes: edit the ConfigMap in `deploy/openshift/configmap.yaml`, run plan and apply
again, then `oc -n vm-autoscaler rollout restart deployment/vm-autoscaler` (the config is
read at startup).

What's in `deploy/openshift/`:

* `deployment.yaml` — 1 replica, `Recreate`, `restricted-v2` compatible (non-root, no
  privilege escalation, all capabilities dropped, seccomp `RuntimeDefault`, read-only root
  filesystem with an `emptyDir` on `/tmp`), probes on `/healthz` and `/readyz`, preStop
  delay and a 90s termination grace period.
* `configmap.yaml` — non-secret env, `config.yaml` (a copy of `config.example.yaml`; keep
  them in sync), and an injected trusted-CA bundle.
* `secret.example.yaml` — the keys of the Secret, placeholders only. **Not applied.**
* `service.yaml`; `route.yaml` only if Grafana is outside the cluster (not applied by default).
* `networkpolicy.yaml` — ingress only from the Grafana namespace and the monitoring scrapers.
* `servicemonitor.yaml` (or `vmservicescrape.yaml` for the VM operator).

The image is built from `registry.access.redhat.com/ubi9/python-312`, installs only the
hash-pinned packages in `requirements.txt`, and runs as UID 1001 / group 0 (works with
OpenShift's random UIDs). For air-gapped builds pass `--build-arg PIP_INDEX_URL=<mirror>`.
At runtime the service makes no internet calls.

### Least privilege

* **GitLab token** (`GITOPS_BACKEND=gitlab`): a project access token on the repo that holds
  the values files — see "GitLab setup".
* **GitOps backend token** (`GITOPS_BACKEND=internal`): must be scoped to the vmstorage
  components listed in `targets` only (requirement for the backend team).
* **Argo CD token:** a project role allowed only `get` and `sync` on the listed apps
  (`get` also covers the refresh the service does right before each sync). With
  `ARGOCD_SYNC_METHOD=cli`, the `argocd` process gets a minimal environment (PATH, HOME,
  locale, proxy variables, its own token, and `SSL_CERT_FILE` from `CA_BUNDLE_PATH`), never
  the GitLab or webhook tokens:

  ```yaml
  # AppProject "victoria-metrics"
  spec:
    roles:
      - name: vm-autoscaler
        description: vm-autoscaler may read and sync the VictoriaMetrics apps
        policies:
          - p, proj:victoria-metrics:vm-autoscaler, applications, get, victoria-metrics/victoria-metrics-region-a, allow
          - p, proj:victoria-metrics:vm-autoscaler, applications, sync, victoria-metrics/victoria-metrics-region-a, allow
          - p, proj:victoria-metrics:vm-autoscaler, applications, get, victoria-metrics/victoria-metrics-region-b, allow
          - p, proj:victoria-metrics:vm-autoscaler, applications, sync, victoria-metrics/victoria-metrics-region-b, allow
  ```

  Create the token with `argocd proj role create-token victoria-metrics vm-autoscaler`.
* The pod does not mount a Kubernetes service account token: it never calls the cluster API.
* Tokens are never logged. `Authorization` / `PRIVATE-TOKEN` headers and anything that looks
  like a token are redacted from log lines.

---

## Connecting the internal GitOps API

Only needed for `GITOPS_BACKEND=internal`. The backend API is not final, so the client in
`app/adapters/gitops_internal.py` uses placeholders. List every place to touch with:

```bash
grep -rn "TODO(api)" app/
```

What to fill in — **all in `app/adapters/gitops_internal.py`**, nothing else changes:

| Item | What it does |
|------|--------------|
| `_PATH_STATE`, `_PATH_APPLY`, `_PATH_CHANGE_REQUEST` | Real URL paths. Change the HTTP method in `apply_changes` if it should be `PATCH`/`PUT`. |
| `_to_state_params(target)` | How a component is identified (query params). `target.gitops_ref` from config is passed through. |
| `_StateResponseWire` + `_from_state_response(body)` | Map the GET response to `ComponentState` (storage size, annotations, version). |
| `_changes_to_wire(changes)` | How one change is described (each has `field`, `yaml_path`, `old`, `new`). |
| `_to_apply_payload(...)` | Direct-commit body: changes, annotations, commit message, `expected_version`. |
| `_to_change_request_payload(...)` | MR body: changes, annotations, title, description. |
| `_WriteResponseWire` + `_from_apply_response` / `_from_change_request_response` | Map write responses to `ChangeResult` (`accepted`, `change_id`, `url`, `message`). |

Then:

1. Update the placeholder JSON in `tests/test_gitops_internal.py` and
   `tests/test_end_to_end_internal.py` to the real shapes, and run `make test`.
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
| `vm_autoscaler_alerts_ignored_total` | `reason` (`resolved`, `unsupported_resource`, `missing_label`) |
| `vm_autoscaler_decisions_total` | `ocp_cluster`, `resource`, `decision`, `reason` |
| `vm_autoscaler_unknown_targets_total` | – |
| `vm_autoscaler_gitops_requests_total` | `operation`, `result` |
| `vm_autoscaler_gitops_request_duration_seconds` | `operation` |
| `vm_autoscaler_argocd_sync_total` | `result` |
| `vm_autoscaler_handle_duration_seconds` | – |

Response codes of `/webhook/grafana`: `202` normally; `503` (same body) if a decision is
`gitops_unavailable` or `argocd_unavailable`, so Grafana retries; `401` bad token; `422` body
that is not valid JSON or not a Grafana webhook. An alert missing a required label is
ignored on its own (`missing_label`, WARNING log), never a `422` for the batch.

Decisions: `applied`, `change_request_opened`, `dry_run`, `skipped`. Skip reasons include
`cooldown_active`, `at_max_cap`, `below_threshold`, `value_not_set`, `unknown_target`,
`deployment_not_healthy`, `gitops_conflict`, `gitops_unavailable`, `gitops_target_not_found`,
`expansion_not_supported`, and `internal_error` (an unexpected bug while handling one target:
logged at ERROR with a traceback; other targets in the same webhook are still handled). An
`applied` decision with reason `argocd_sync_failed` means the
commit landed but the sync call failed (Argo auto-sync, if enabled, will still pick it up).

Suggested alerts on the service itself:

```promql
# A human must act: storage reached the configured max
increase(vm_autoscaler_decisions_total{reason="at_max_cap"}[1h]) > 0

# Resized 3+ times in a week (the cooldown is 24h): the PVC is probably NOT growing
# (expansion disabled in the operator/StorageClass, chart not passing the value, no sync).
# Check `oc get pvc` before it reaches max.
sum by (ocp_cluster) (increase(vm_autoscaler_decisions_total{decision="applied"}[7d])) >= 3

# Errors talking to GitLab / Argo, or a bug
increase(vm_autoscaler_decisions_total{reason=~"gitops_.*|argocd_.*|internal_error"}[1h]) > 0

# Alert rules missing a required label (ocp_cluster/namespace/component/resource)
increase(vm_autoscaler_alerts_ignored_total{reason="missing_label"}[1h]) > 0

# Alert rules that send something other than storage (old cpu/memory/replica rules?)
increase(vm_autoscaler_alerts_ignored_total{reason="unsupported_resource"}[1h]) > 0
```

## Code map

```
app/
  main.py                      FastAPI app factory + lifespan
  container.py                 composition root: picks adapters from env
  config.py                    Settings (env) + YAML config models (GitLab file locations)
  readiness.py                 /readyz checks
  observability.py             JSON logging, redaction, Prometheus metrics
  api/routes.py                /webhook/grafana, /healthz, /readyz, /metrics
  api/security.py              bearer token check (constant-time)
  api/grafana_webhook.py       Grafana payload models + parser -> ScaleRequests
  domain/models.py             Target, Policy, ScaleRequest, ComponentState, ScaleDecision, ...
  domain/ports.py              GitOpsBackend and DeploymentSyncer protocols
  domain/strategies.py         StorageStrategy + registry
  domain/quantity.py           Kubernetes size parse/format/round
  domain/errors.py             our exceptions
  services/scaling_service.py  the decision rules and orchestration
  adapters/gitops_gitlab.py    GitLab REST API v4 backend (GITOPS_BACKEND=gitlab)
  adapters/yaml_edit.py        round-trip YAML editing (ruamel.yaml), diff guard
  adapters/gitops_internal.py  internal backend client (TODO(api) placeholders)
  adapters/gitops_fake.py      in-memory backend
  adapters/argocd_api.py       Argo CD REST adapter
  adapters/argocd_cli.py       Argo CD CLI adapter
  adapters/argocd_fake.py      in-memory Argo stand-in
docs/grafana-alert-rules.md    Grafana alert rule, contact point, notification policy
deploy/openshift/              OpenShift manifests (kustomize)
```
