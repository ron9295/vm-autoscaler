"""Full flow: Grafana webhook -> real InternalGitOpsClient -> real ArgoCdApiSyncer.

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
APP = "victoria-metrics-region-a"
AUTH = {"Authorization": "Bearer e2e-token"}


def _config(tmp_path: Path) -> Path:
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    raw["mode"] = "direct_commit"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def _state(annotations: dict[str, str] | None = None) -> dict:  # type: ignore[type-arg]
    return {
        "cpu": {"request": "2", "limit": "4"},
        "memory": {"request": "16Gi", "limit": "16Gi"},
        "storage": "500Gi",
        "replicas": 3,
        "annotations": annotations or {},
        "version": "sha-1",
    }


@respx.mock
def test_cpu_and_memory_one_commit_one_sync(tmp_path: Path) -> None:
    state_route = respx.get(f"{GITOPS}/TODO/components").respond(200, json=_state())
    apply_route = respx.post(f"{GITOPS}/TODO/components/changes").respond(
        200, json={"accepted": True, "id": "sha-2", "url": f"{GITOPS}/commit/sha-2"}
    )
    status_route = respx.get(f"{ARGO}/api/v1/applications/{APP}").respond(
        200, json={"status": {"health": {"status": "Healthy"}, "sync": {"status": "Synced"}}}
    )
    sync_route = respx.post(f"{ARGO}/api/v1/applications/{APP}/sync").respond(200, json={})

    settings = Settings(
        webhook_token="e2e-token",
        config_path=_config(tmp_path),
        gitops_backend="internal",
        gitops_api_url=GITOPS,
        gitops_api_token="gitops-token",
        argocd_sync_method="api",
        argocd_url=ARGO,
        argocd_token="argo-token",
    )
    payload = load_fixture("grafana_multi.json")
    with TestClient(create_app(settings)) as client:
        response = client.post("/webhook/grafana", json=payload, headers=AUTH)

    assert response.status_code == 202
    decisions = {(d["ocp_cluster"], d["resource"]): d for d in response.json()["decisions"]}
    cpu = decisions[("ocp-prod-region-a", "cpu")]
    memory = decisions[("ocp-prod-region-a", "memory")]
    assert (cpu["decision"], cpu["old"], cpu["new"], cpu["synced"]) == ("applied", "4", "5", True)
    assert (memory["old"], memory["new"], memory["change_id"]) == ("16Gi", "20Gi", "sha-2")
    assert decisions[("ocp-prod-region-b", "storage")]["reason"] == "unknown_target"

    assert state_route.call_count == 1
    assert apply_route.call_count == 1
    assert status_route.call_count == 1
    assert sync_route.call_count == 1

    commit = json.loads(apply_route.calls.last.request.content)
    assert commit["expected_version"] == "sha-1"
    assert {c["field"] for c in commit["changes"]} == {
        "cpu_limit",
        "cpu_request",
        "memory_limit",
        "memory_request",
    }
    assert set(commit["annotations"]) == {
        "vm-autoscaler/last-scaled-cpu",
        "vm-autoscaler/last-scaled-memory",
    }
    assert commit["commit_message"].startswith(
        "[vm-autoscaler] ocp-prod-region-a/monitoring vmstorage cpu 4 -> 5 (usage 91.2% >= 80%)"
    )
    assert "Idempotency-Key" in apply_route.calls.last.request.headers

    sync = json.loads(sync_route.calls.last.request.content)
    assert sync["prune"] is False
    assert sync["resources"][0]["kind"] == "VMCluster"
