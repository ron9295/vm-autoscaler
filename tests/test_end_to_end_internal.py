"""Full flow: Grafana webhook -> real InternalGitOpsBackend -> real ArgoCdApiSyncer.

Both backends are mocked with respx at the HTTP level, using the placeholder
TODO(api) paths and payload shapes.
"""

import json
from pathlib import Path

import respx
import yaml
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import ROOT, load_fixture

GITOPS = "https://gitops.example.internal"
ARGO = "https://argocd.example.internal"
AUTH = {"Authorization": "Bearer e2e-webhook-token-0001"}
HEALTHY = {"status": {"health": {"status": "Healthy"}, "sync": {"status": "Synced"}}}


def _config(tmp_path: Path) -> Path:
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    raw["mode"] = "direct_commit"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        webhook_token="e2e-webhook-token-0001",
        config_path=_config(tmp_path),
        gitops_backend="internal",
        gitops_api_url=GITOPS,
        gitops_api_token="gitops-token",
        argocd_sync_method="api",
        argocd_url=ARGO,
        argocd_token="argo-token",
    )


@respx.mock
def test_storage_alerts_one_commit_and_one_sync_per_target(tmp_path: Path) -> None:
    state_route = respx.get(f"{GITOPS}/TODO/components").respond(
        200, json={"storage": "30Gi", "annotations": {}, "version": "sha-1"}
    )
    apply_route = respx.post(f"{GITOPS}/TODO/components/changes").respond(
        200, json={"accepted": True, "id": "sha-2", "url": f"{GITOPS}/commit/sha-2"}
    )
    status_routes = [
        respx.get(f"{ARGO}/api/v1/applications/{app}").respond(200, json=HEALTHY)
        for app in ("victoria-metrics-region-a", "victoria-metrics-region-b")
    ]
    sync_routes = [
        respx.post(f"{ARGO}/api/v1/applications/{app}/sync").respond(200, json={})
        for app in ("victoria-metrics-region-a", "victoria-metrics-region-b")
    ]

    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.post(
            "/webhook/grafana", json=load_fixture("grafana_multi.json"), headers=AUTH
        )

    assert response.status_code == 202
    decisions = {d["ocp_cluster"]: d for d in response.json()["decisions"]}
    region_a = decisions["ocp-prod-region-a"]
    assert (region_a["decision"], region_a["old"], region_a["new"], region_a["synced"]) == (
        "applied",
        "30Gi",
        "36Gi",
        True,
    )
    assert region_a["change_id"] == "sha-2"
    assert decisions["ocp-prod-region-b"]["decision"] == "applied"
    assert decisions["ocp-unknown"]["reason"] == "unknown_target"

    assert state_route.call_count == 2  # one read per known target
    assert apply_route.call_count == 2  # one commit per known target
    # Per app: one health pre-check, then one refresh right before the sync.
    assert [r.call_count for r in status_routes] == [2, 2]
    for route in status_routes:
        assert [c.request.url.params.get("refresh") for c in route.calls] == [None, "normal"]
    assert [r.call_count for r in sync_routes] == [1, 1]

    commits = [json.loads(call.request.content) for call in apply_route.calls]
    region_a_commit = next(c for c in commits if c["target"]["cluster"] == "ocp-prod-region-a")
    assert region_a_commit["expected_version"] == "sha-1"
    assert [(c["field"], c["old"], c["new"]) for c in region_a_commit["changes"]] == [
        ("storage", "30Gi", "36Gi")
    ]
    assert set(region_a_commit["annotations"]) == {"vm-autoscaler/last-scaled-storage"}
    assert region_a_commit["commit_message"].startswith(
        "[vm-autoscaler] ocp-prod-region-a/monitoring vmstorage storage 30Gi -> 36Gi "
        "(usage 85.0% >= 80%)"
    )
    assert all("Idempotency-Key" in call.request.headers for call in apply_route.calls)

    sync = json.loads(sync_routes[0].calls.last.request.content)
    assert sync["prune"] is False
    assert sync["resources"][0]["kind"] == "VMCluster"
