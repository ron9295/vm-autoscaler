from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app.adapters.argocd_fake import FakeArgoCdSyncer
from app.adapters.gitops_fake import FakeGitOpsBackend
from app.config import Settings, load_app_config
from app.container import build_container
from app.domain.errors import ArgoCdUnavailableError, GitOpsUnavailableError
from app.domain.models import AppStatus, TargetKey
from app.main import create_app
from app.observability import Metrics
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
    app = create_app(make_settings(), gitops=gitops, syncer=FakeArgoCdSyncer())
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
        "/webhook/grafana", json=load_fixture("grafana_firing_storage.json"), headers=AUTH
    )
    assert response.status_code == 202
    body = response.json()
    assert body["mode"] == "dry_run"
    [decision] = body["decisions"]
    assert decision["decision"] == "dry_run"
    assert decision["resource"] == "storage"
    assert (decision["old"], decision["new"]) == ("30Gi", "36Gi")
    assert gitops.applied == []


def test_missing_label_alert_is_ignored_with_202(client: TestClient) -> None:
    response = client.post(
        "/webhook/grafana", json=load_fixture("grafana_missing_label.json"), headers=AUTH
    )
    assert response.status_code == 202
    body = response.json()
    assert body["decisions"] == []
    assert body["ignored"] == [
        {"fingerprint": "a1b2c3d4e5f60002", "status": "firing", "reason": "missing_label"}
    ]
    metrics = client.get("/metrics").text
    assert 'vm_autoscaler_alerts_ignored_total{reason="missing_label"} 1.0' in metrics


def test_malformed_alert_does_not_block_valid_one(client: TestClient) -> None:
    payload = load_fixture("grafana_firing_storage.json")
    broken = {**payload["alerts"][0], "fingerprint": "broken"}
    broken["labels"] = {k: v for k, v in broken["labels"].items() if k != "namespace"}
    payload["alerts"].insert(0, broken)
    response = client.post("/webhook/grafana", json=payload, headers=AUTH)
    assert response.status_code == 202
    body = response.json()
    assert [d["decision"] for d in body["decisions"]] == ["dry_run"]
    assert [i["reason"] for i in body["ignored"]] == ["missing_label"]


def test_invalid_schema_is_422(client: TestClient) -> None:
    response = client.post("/webhook/grafana", json={"alerts": "nope"}, headers=AUTH)
    assert response.status_code == 422


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
    assert "vm_autoscaler_unknown_targets_total 1.0" in text  # ocp-unknown is not configured


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
        Settings(
            webhook_token="test-webhook-token-0001",
            gitops_backend="internal",
            argocd_sync_method="fake",
        )
    with pytest.raises(ValueError, match="ARGOCD_URL"):
        Settings(
            webhook_token="test-webhook-token-0001", gitops_backend="fake", argocd_sync_method="api"
        )


def test_bearer_with_empty_token_is_401(client: TestClient) -> None:
    for header in ("Bearer ", "Bearer", "Bearer    "):
        response = client.post("/webhook/grafana", json={}, headers={"Authorization": header})
        assert response.status_code == 401


@pytest.mark.parametrize("token", ["", "   ", "short-token"])
def test_weak_webhook_token_rejected_at_startup(token: str) -> None:
    with pytest.raises(ValueError, match="WEBHOOK_TOKEN must be at least 16 characters"):
        Settings(webhook_token=token, gitops_backend="fake", argocd_sync_method="fake")


def test_mixed_batch_storage_decided_cpu_ignored(client: TestClient) -> None:
    payload = load_fixture("grafana_firing_storage.json")
    leftover = {**payload["alerts"][0], "fingerprint": "old-cpu-rule"}
    leftover["labels"] = {**leftover["labels"], "resource": "cpu"}
    payload["alerts"].append(leftover)
    response = client.post("/webhook/grafana", json=payload, headers=AUTH)
    assert response.status_code == 202
    body = response.json()
    assert [d["resource"] for d in body["decisions"]] == ["storage"]
    assert body["ignored"] == [
        {"fingerprint": "old-cpu-rule", "status": "firing", "reason": "unsupported_resource"}
    ]
    metrics = client.get("/metrics").text
    assert 'vm_autoscaler_alerts_ignored_total{reason="unsupported_resource"} 1.0' in metrics


def test_resolved_alerts_are_counted_as_ignored(client: TestClient) -> None:
    client.post("/webhook/grafana", json=load_fixture("grafana_resolved.json"), headers=AUTH)
    metrics = client.get("/metrics").text
    assert 'vm_autoscaler_alerts_ignored_total{reason="resolved"} 1.0' in metrics


@pytest.mark.parametrize(
    ("backend", "flag", "expected"),
    [("internal", False, False), ("internal", True, True), ("fake", False, True)],
)
async def test_stores_annotations_flag_only_applies_to_internal(
    backend: str, flag: bool, expected: bool
) -> None:
    settings = Settings(
        webhook_token=TOKEN,
        config_path=ROOT / "config.example.yaml",
        gitops_backend=backend,  # type: ignore[arg-type]
        gitops_api_url="https://gitops.test",
        gitops_stores_annotations=flag,
        argocd_sync_method="fake",
    )
    container = build_container(settings, load_app_config(settings.config_path), Metrics())
    try:
        assert container.service._annotations_supported is expected
    finally:
        await container.close()


def _direct_commit_config(tmp_path: Path) -> Path:
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    assert "\nmode: dry_run\n" in text
    path = tmp_path / "config.yaml"
    path.write_text(text.replace("\nmode: dry_run\n", "\nmode: direct_commit\n"))
    return path


def _post_with(tmp_path: Path, gitops: FakeGitOpsBackend, syncer: FakeArgoCdSyncer) -> Response:
    app = create_app(make_settings(_direct_commit_config(tmp_path)), gitops=gitops, syncer=syncer)
    with TestClient(app) as test_client:
        return test_client.post(
            "/webhook/grafana", json=load_fixture("grafana_firing_storage.json"), headers=AUTH
        )


def test_gitops_unavailable_returns_503_with_same_body(
    tmp_path: Path, gitops: FakeGitOpsBackend
) -> None:
    gitops.fail_next("get_component_state", GitOpsUnavailableError("down"))
    response = _post_with(tmp_path, gitops, FakeArgoCdSyncer())
    assert response.status_code == 503
    body = response.json()
    assert set(body) == {"request_id", "mode", "decisions", "ignored"}
    assert [d["reason"] for d in body["decisions"]] == ["gitops_unavailable"]


def test_argocd_unavailable_returns_503(tmp_path: Path, gitops: FakeGitOpsBackend) -> None:
    syncer = FakeArgoCdSyncer(fail_status=ArgoCdUnavailableError("down"))
    response = _post_with(tmp_path, gitops, syncer)
    assert response.status_code == 503
    assert [d["reason"] for d in response.json()["decisions"]] == ["argocd_unavailable"]
    assert gitops.applied == []


def test_other_skips_stay_202(tmp_path: Path, gitops: FakeGitOpsBackend) -> None:
    syncer = FakeArgoCdSyncer(status=AppStatus(health="Degraded", sync="Synced"))
    response = _post_with(tmp_path, gitops, syncer)
    assert response.status_code == 202
    assert [d["reason"] for d in response.json()["decisions"]] == ["deployment_not_healthy"]


def _env_settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for name in (
        "GITOPS_BACKEND",
        "GITOPS_API_TIMEOUT_SECONDS",
        "GITOPS_API_MAX_RETRIES",
        "GITLAB_TIMEOUT_SECONDS",
        "GITLAB_MAX_RETRIES",
        "ARGOCD_TIMEOUT_SECONDS",
        "HTTP_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WEBHOOK_TOKEN", TOKEN)
    monkeypatch.setenv("ARGOCD_SYNC_METHOD", "fake")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings()


def test_gitops_backend_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match=r"GITOPS_BACKEND is required.*gitlab \| internal \| fake"):
        _env_settings(monkeypatch)
    with pytest.raises(ValueError, match="GITOPS_BACKEND"):
        _env_settings(monkeypatch, GITOPS_BACKEND="")
    assert _env_settings(monkeypatch, GITOPS_BACKEND="fake").gitops_backend == "fake"


def test_gitlab_timeout_and_retries_fall_back_to_old_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _env_settings(
        monkeypatch,
        GITOPS_BACKEND="fake",
        GITOPS_API_TIMEOUT_SECONDS="7",
        GITOPS_API_MAX_RETRIES="2",
    )
    assert (settings.gitlab_timeout, settings.gitlab_retries) == (7.0, 2)
    settings = _env_settings(
        monkeypatch,
        GITOPS_BACKEND="fake",
        GITOPS_API_TIMEOUT_SECONDS="7",
        GITOPS_API_MAX_RETRIES="2",
        GITLAB_TIMEOUT_SECONDS="4",
        GITLAB_MAX_RETRIES="0",
    )
    assert (settings.gitlab_timeout, settings.gitlab_retries) == (4.0, 0)
    assert settings.gitops_api_timeout_seconds == 7.0  # the internal backend keeps its own


def test_argocd_timeout_accepts_the_old_name(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _env_settings(monkeypatch, GITOPS_BACKEND="fake").argocd_timeout_seconds == 10.0
    old = _env_settings(monkeypatch, GITOPS_BACKEND="fake", HTTP_TIMEOUT_SECONDS="3")
    assert old.argocd_timeout_seconds == 3.0
    new = _env_settings(
        monkeypatch, GITOPS_BACKEND="fake", HTTP_TIMEOUT_SECONDS="3", ARGOCD_TIMEOUT_SECONDS="5"
    )
    assert new.argocd_timeout_seconds == 5.0


def test_resend_after_partial_503_does_not_resize_the_applied_target_twice(
    tmp_path: Path, gitops: FakeGitOpsBackend
) -> None:
    region_b = TargetKey(
        ocp_cluster="ocp-prod-region-b", namespace="monitoring", component="vmstorage"
    )
    gitops.seed(region_b, state())
    gitops.fail_next("get_component_state", GitOpsUnavailableError("down"))  # region-a only
    app = create_app(
        make_settings(_direct_commit_config(tmp_path)), gitops=gitops, syncer=FakeArgoCdSyncer()
    )
    payload = load_fixture("grafana_multi.json")

    def reasons(response: Response) -> dict[str, str]:
        decisions = response.json()["decisions"]
        return {
            d["ocp_cluster"]: d["reason"] for d in decisions if d["ocp_cluster"] != "ocp-unknown"
        }

    with TestClient(app) as test_client:
        first = test_client.post("/webhook/grafana", json=payload, headers=AUTH)
        assert first.status_code == 503
        assert reasons(first)["ocp-prod-region-a"] == "gitops_unavailable"
        assert reasons(first)["ocp-prod-region-b"] == "scaled"

        resend = test_client.post("/webhook/grafana", json=payload, headers=AUTH)
        assert resend.status_code == 202
        assert reasons(resend)["ocp-prod-region-a"] == "scaled"
        assert reasons(resend)["ocp-prod-region-b"] == "cooldown_active"
    assert len(gitops.applied) == 2
