"""Grafana webhook -> real GitLab adapter -> real Argo CD adapter, both mocked with respx.

Repo layout under test: <ocp_cluster>/<namespace>/values.yaml with vmstorage.pvc.size.
"""

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import respx
import yaml
from fastapi.testclient import TestClient

from app.adapters.yaml_edit import changed_lines
from app.config import Settings
from app.main import create_app
from tests.conftest import FIXTURES, ROOT, load_fixture

WEBHOOK_TOKEN = "e2e-webhook-token-0123"  # >= 16 characters (startup validation)
GITLAB = "https://gitlab.example.internal"
PROJECT = f"{GITLAB}/api/v4/projects/infra%2Fvm-values"
COMMITS_URL = f"{PROJECT}/repository/commits"
ARGO = "https://argocd.example.internal"
VALUES = (FIXTURES / "values.yaml").read_text()
CLUSTERS = ("ocp-prod-region-a", "ocp-prod-region-b")
HEALTHY = {"status": {"health": {"status": "Healthy"}, "sync": {"status": "Synced"}}}


def _file_url(cluster: str) -> str:
    return f"{PROJECT}/repository/files/{cluster}%2Fmonitoring%2Fvalues.yaml"


def _config(tmp_path: Path, mode: str) -> Path:
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    raw["mode"] = mode
    raw["gitlab"]["project"] = "infra/vm-values"
    raw["gitlab"]["file_template"] = "{ocp_cluster}/{namespace}/values.yaml"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def _settings(tmp_path: Path, mode: str) -> Settings:
    return Settings(
        webhook_token=WEBHOOK_TOKEN,
        config_path=_config(tmp_path, mode),
        gitops_backend="gitlab",
        gitlab_url=GITLAB,
        gitlab_token="glpat-e2e",
        argocd_sync_method="api",
        argocd_url=ARGO,
        argocd_token="argo-token",
    )


def _mock_files() -> dict[str, respx.Route]:
    content = base64.b64encode(VALUES.encode()).decode()
    return {
        cluster: respx.get(_file_url(cluster)).respond(
            200, json={"content": content, "encoding": "base64", "last_commit_id": f"sha-{cluster}"}
        )
        for cluster in CLUSTERS
    }


def _mock_history(commits: list[dict[str, Any]] | None = None) -> respx.Route:
    return respx.get(COMMITS_URL).respond(200, json=commits or [])


def _mock_argo() -> dict[str, respx.Route]:
    syncs = {}
    for cluster in CLUSTERS:
        app = f"victoria-metrics-{cluster.removeprefix('ocp-prod-')}"
        respx.get(f"{ARGO}/api/v1/applications/{app}").respond(200, json=HEALTHY)
        syncs[cluster] = respx.post(f"{ARGO}/api/v1/applications/{app}/sync").respond(200, json={})
    return syncs


def _post_alert(settings: Settings) -> dict[tuple[str, str], dict[str, Any]]:
    payload = load_fixture("grafana_multi.json")
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/webhook/grafana", json=payload, headers={"Authorization": f"Bearer {WEBHOOK_TOKEN}"}
        )
    assert response.status_code == 202
    return {(d["ocp_cluster"], d["resource"]): d for d in response.json()["decisions"]}


def _commits_by_file(route: respx.Route) -> dict[str, dict[str, Any]]:
    bodies = [json.loads(call.request.content) for call in route.calls]
    return {body["actions"][0]["file_path"]: body for body in bodies}


@respx.mock
def test_one_commit_and_one_sync_per_target(tmp_path: Path) -> None:
    reads = _mock_files()
    history = _mock_history()
    commit = respx.post(COMMITS_URL).respond(
        201, json={"id": "sha-new", "web_url": f"{GITLAB}/c/1"}
    )
    syncs = _mock_argo()

    decisions = _post_alert(_settings(tmp_path, "direct_commit"))

    for cluster in CLUSTERS:
        decision = decisions[(cluster, "storage")]
        assert (decision["decision"], decision["old"], decision["new"], decision["synced"]) == (
            "applied",
            "30Gi",
            "36Gi",
            True,
        )
        assert reads[cluster].call_count == 2  # state read + re-read before the commit
        assert syncs[cluster].call_count == 1
    assert decisions[("ocp-unknown", "storage")]["reason"] == "unknown_target"
    assert history.call_count == 2  # cooldowns come from the commit history, once per target
    assert commit.call_count == 2

    commits = _commits_by_file(commit)
    assert set(commits) == {f"{c}/monitoring/values.yaml" for c in CLUSTERS}
    region_a = commits["ocp-prod-region-a/monitoring/values.yaml"]
    assert region_a["branch"] == "main"
    assert region_a["actions"][0]["last_commit_id"] == "sha-ocp-prod-region-a"
    assert region_a["commit_message"].startswith(
        "[vm-autoscaler] ocp-prod-region-a/monitoring vmstorage storage 30Gi -> 36Gi "
        "(usage 85.0% >= 80%)"
    )
    # Only the size line changes: no annotations in commits mode.
    assert changed_lines(VALUES, region_a["actions"][0]["content"]) == [
        "    size: 36Gi   # grown by vm-autoscaler"
    ]


@respx.mock
def test_dry_run_reads_gitlab_and_writes_nothing(tmp_path: Path) -> None:
    reads = _mock_files()
    _mock_history()
    commit = respx.post(COMMITS_URL)
    syncs = _mock_argo()

    decisions = _post_alert(_settings(tmp_path, "dry_run"))

    for cluster in CLUSTERS:
        decision = decisions[(cluster, "storage")]
        assert (decision["decision"], decision["old"], decision["new"]) == (
            "dry_run",
            "30Gi",
            "36Gi",
        )
        assert reads[cluster].call_count == 1
        assert syncs[cluster].call_count == 0
    assert commit.call_count == 0


@respx.mock
def test_recent_commit_in_history_blocks_only_that_target(tmp_path: Path) -> None:
    _mock_files()
    recent = datetime.now(UTC) - timedelta(hours=1)
    # The history route is shared by both targets; only region-a's title matches region-a.
    _mock_history(
        [
            {
                "id": "old",
                "message": "[vm-autoscaler] ocp-prod-region-a/monitoring vmstorage "
                "storage 25Gi -> 30Gi (usage 90.0% >= 80%)",
                "committed_date": recent.isoformat(),
            }
        ]
    )
    commit = respx.post(COMMITS_URL).respond(201, json={"id": "new"})
    syncs = _mock_argo()

    decisions = _post_alert(_settings(tmp_path, "direct_commit"))

    assert decisions[("ocp-prod-region-a", "storage")]["reason"] == "cooldown_active"
    assert decisions[("ocp-prod-region-b", "storage")]["decision"] == "applied"
    assert set(_commits_by_file(commit)) == {"ocp-prod-region-b/monitoring/values.yaml"}
    assert syncs["ocp-prod-region-a"].call_count == 0
    assert syncs["ocp-prod-region-b"].call_count == 1
