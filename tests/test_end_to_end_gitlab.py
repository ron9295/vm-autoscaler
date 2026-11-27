"""Grafana CPU + memory alert -> ONE GitLab commit -> ONE Argo CD sync (real adapters, respx)."""

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import respx
import yaml
from fastapi.testclient import TestClient

from app.adapters.manifest import changed_lines
from app.config import Settings
from app.main import create_app
from tests.conftest import FIXTURES, ROOT, load_fixture

GITLAB = "https://gitlab.example.internal"
PROJECT = f"{GITLAB}/api/v4/projects/infra%2Fmonitoring"
FILE_URL = f"{PROJECT}/repository/files/clusters%2Fregion-a%2Fvmcluster.yaml"
ARGO = "https://argocd.example.internal"
APP = "victoria-metrics-region-a"
VMCLUSTER = (FIXTURES / "vmcluster.yaml").read_text()


def _config(tmp_path: Path, mode: str) -> Path:
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    raw["mode"] = mode
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def _settings(tmp_path: Path, mode: str) -> Settings:
    return Settings(
        webhook_token="e2e-token",
        config_path=_config(tmp_path, mode),
        gitops_backend="gitlab",
        gitlab_url=GITLAB,
        gitlab_token="glpat-e2e",
        argocd_sync_method="api",
        argocd_url=ARGO,
        argocd_token="argo-token",
    )


def _mock_file() -> respx.Route:
    content = base64.b64encode(VMCLUSTER.encode()).decode()
    return respx.get(FILE_URL).respond(
        200, json={"content": content, "encoding": "base64", "last_commit_id": "c0ffee1"}
    )


def _mock_history(commits: list[dict[str, str]] | None = None) -> respx.Route:
    return respx.get(f"{PROJECT}/repository/commits").respond(200, json=commits or [])


def _post_alert(settings: Settings) -> dict:  # type: ignore[type-arg]
    payload = load_fixture("grafana_multi.json")
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/webhook/grafana", json=payload, headers={"Authorization": "Bearer e2e-token"}
        )
    assert response.status_code == 202
    return {(d["ocp_cluster"], d["resource"]): d for d in response.json()["decisions"]}


@respx.mock
def test_cpu_and_memory_one_gitlab_commit_one_sync(tmp_path: Path) -> None:
    read = _mock_file()
    history = _mock_history()
    commit = respx.post(f"{PROJECT}/repository/commits").respond(
        201, json={"id": "sha-new", "web_url": f"{GITLAB}/c/sha-new"}
    )
    respx.get(f"{ARGO}/api/v1/applications/{APP}").respond(
        200, json={"status": {"health": {"status": "Healthy"}, "sync": {"status": "Synced"}}}
    )
    sync = respx.post(f"{ARGO}/api/v1/applications/{APP}/sync").respond(200, json={})

    decisions = _post_alert(_settings(tmp_path, "direct_commit"))

    cpu = decisions[("ocp-prod-region-a", "cpu")]
    memory = decisions[("ocp-prod-region-a", "memory")]
    assert (cpu["decision"], cpu["old"], cpu["new"], cpu["synced"]) == ("applied", "4", "5", True)
    assert (memory["old"], memory["new"], memory["url"]) == ("16Gi", "20Gi", f"{GITLAB}/c/sha-new")
    assert read.call_count == 2  # state read + re-read before the commit
    assert history.call_count == 1  # cooldowns come from the commit history
    assert commit.call_count == 1
    assert sync.call_count == 1
    body = json.loads(commit.calls.last.request.content)
    assert body["commit_message"].startswith(
        "[vm-autoscaler] ocp-prod-region-a/monitoring vmstorage cpu 4 -> 5 (usage 91.2% >= 80%), "
        "memory 16Gi -> 20Gi (usage 85.0% >= 80%)"
    )
    lines = changed_lines(VMCLUSTER, body["actions"][0]["content"])
    assert '        cpu: "5"   # see capacity doc' in lines
    assert len(lines) == 4  # only the 4 values: cooldowns live in Git history, not the file


@respx.mock
def test_dry_run_reads_gitlab_and_writes_nothing(tmp_path: Path) -> None:
    read = _mock_file()
    _mock_history()
    commit = respx.post(f"{PROJECT}/repository/commits")
    decisions = _post_alert(_settings(tmp_path, "dry_run"))
    cpu = decisions[("ocp-prod-region-a", "cpu")]
    assert (cpu["decision"], cpu["old"], cpu["new"]) == ("dry_run", "4", "5")
    assert read.call_count == 1
    assert commit.call_count == 0


@respx.mock
def test_recent_commit_in_history_blocks_rescaling(tmp_path: Path) -> None:
    _mock_file()
    recent = datetime.now(UTC) - timedelta(hours=1)
    _mock_history(
        [
            {
                "id": "old",
                "message": "[vm-autoscaler] ocp-prod-region-a/monitoring vmstorage "
                "cpu 3 -> 4 (usage 90.0% >= 80%)",
                "committed_date": recent.isoformat(),
            }
        ]
    )
    commit = respx.post(f"{PROJECT}/repository/commits").respond(201, json={"id": "new"})
    respx.get(f"{ARGO}/api/v1/applications/{APP}").respond(
        200, json={"status": {"health": {"status": "Healthy"}, "sync": {"status": "Synced"}}}
    )
    respx.post(f"{ARGO}/api/v1/applications/{APP}/sync").respond(200, json={})

    decisions = _post_alert(_settings(tmp_path, "direct_commit"))

    assert decisions[("ocp-prod-region-a", "cpu")]["reason"] == "cooldown_active"
    assert decisions[("ocp-prod-region-a", "memory")]["decision"] == "applied"
    changes = json.loads(commit.calls.last.request.content)["actions"][0]["content"]
    assert '        cpu: "4"   # see capacity doc' in changes  # cpu untouched
