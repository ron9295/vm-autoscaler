from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.adapters.argocd_fake import FakeDeploymentSyncer
from app.adapters.gitops_fake import FakeGitOpsBackend
from app.config import Settings
from app.main import create_app
from tests.conftest import ROOT, load_fixture
from tests.factories import TARGET_KEY, state

TOKEN = "test-webhook-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def make_settings(config_path: Path = ROOT / "config.example.yaml") -> Settings:
    return Settings(
        webhook_token=TOKEN,
        config_path=config_path,
        gitops_backend="fake",
        argocd_sync_method="fake",
    )


@pytest.fixture
def gitops() -> FakeGitOpsBackend:
    backend = FakeGitOpsBackend()
    backend.seed(TARGET_KEY, state())
    return backend


@pytest.fixture
def client(gitops: FakeGitOpsBackend) -> Iterator[TestClient]:
    app = create_app(make_settings(), gitops=gitops, syncer=FakeDeploymentSyncer())
    with TestClient(app) as test_client:
        yield test_client


def test_webhook_requires_token(client: TestClient) -> None:
    assert client.post("/webhook/grafana", json={}).status_code == 401
    bad = {"Authorization": "Bearer wrong"}
    assert client.post("/webhook/grafana", json={}, headers=bad).status_code == 401
    basic = {"Authorization": f"Basic {TOKEN}"}
    assert client.post("/webhook/grafana", json={}, headers=basic).status_code == 401


def test_unauthenticated_bad_body_still_401(client: TestClient) -> None:
    response = client.post("/webhook/grafana", content=b"not json")
    assert response.status_code == 401


def test_dry_run_webhook_returns_decision(client: TestClient, gitops: FakeGitOpsBackend) -> None:
    response = client.post(
        "/webhook/grafana", json=load_fixture("grafana_firing_cpu.json"), headers=AUTH
    )
    assert response.status_code == 202
    body = response.json()
    assert body["mode"] == "dry_run"
    [decision] = body["decisions"]
    assert decision["decision"] == "dry_run"
    assert (decision["old"], decision["new"]) == ("4", "5")
    assert gitops.applied == []


def test_missing_label_is_422(client: TestClient) -> None:
    response = client.post(
        "/webhook/grafana", json=load_fixture("grafana_missing_label.json"), headers=AUTH
    )
    assert response.status_code == 422
    assert "missing required label 'ocp_cluster'" in response.json()["detail"]


def test_invalid_json_is_422(client: TestClient) -> None:
    response = client.post("/webhook/grafana", content=b"{oops", headers=AUTH)
    assert response.status_code == 422


def test_resolved_is_ignored(client: TestClient) -> None:
    response = client.post(
        "/webhook/grafana", json=load_fixture("grafana_resolved.json"), headers=AUTH
    )
    assert response.status_code == 202
    assert response.json()["decisions"] == []
    assert response.json()["ignored"][0]["reason"] == "resolved"


def test_metrics_exposes_counters(client: TestClient) -> None:
    client.post("/webhook/grafana", json=load_fixture("grafana_multi.json"), headers=AUTH)
    text = client.get("/metrics").text
    assert 'vm_autoscaler_alerts_received_total{status="firing"} 3.0' in text
    assert 'vm_autoscaler_alerts_received_total{status="resolved"} 1.0' in text
    assert "vm_autoscaler_decisions_total{" in text
    assert "vm_autoscaler_handle_duration_seconds_count 1.0" in text
    assert "vm_autoscaler_unknown_targets_total 1.0" in text  # region-b is not configured


def test_health_and_ready(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["checks"] == {"config": True}


def test_bad_config_fails_at_startup(tmp_path: Path) -> None:
    bad = tmp_path / "config.yaml"
    bad.write_text("mode: yolo\npolicies: {}\ntargets: []\n")
    with pytest.raises(Exception, match="invalid config file"):
        create_app(make_settings(bad))


def test_settings_require_urls_for_real_adapters() -> None:
    with pytest.raises(ValueError, match="GITOPS_API_URL"):
        Settings(webhook_token="x", gitops_backend="internal", argocd_sync_method="fake")
    with pytest.raises(ValueError, match="ARGOCD_URL"):
        Settings(webhook_token="x", gitops_backend="fake", argocd_sync_method="api")
