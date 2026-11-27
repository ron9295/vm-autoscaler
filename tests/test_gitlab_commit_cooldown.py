"""Cooldown inferred from commit history (gitlab.cooldown_source: commits, the default)."""

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
import respx

from app.adapters.gitops_gitlab import (
    COOLDOWN_PREFIX,
    GitLabGitOpsBackend,
    last_scaled_from_commits,
    merge_newest,
)
from app.adapters.manifest import changed_lines
from app.config import GitLabConfig, GitLabTargetConfig
from app.domain.models import ResourceChange
from app.observability import Metrics
from app.services.scaling_service import cooldown_annotation
from tests.conftest import FIXTURES
from tests.factories import TARGET_KEY, target

GITLAB = "https://gitlab.example.internal"
PROJECT = f"{GITLAB}/api/v4/projects/infra%2Fmonitoring"
FILE_URL = f"{PROJECT}/repository/files/clusters%2Fregion-a%2Fvmcluster.yaml"
COMMITS_URL = f"{PROJECT}/repository/commits"
VMCLUSTER = (FIXTURES / "vmcluster.yaml").read_text()
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
TITLE = "[vm-autoscaler] ocp-prod-region-a/monitoring vmstorage"
LOCATION = GitLabTargetConfig(
    project="infra/monitoring", file="clusters/region-a/vmcluster.yaml", document_name="vm-main"
)


def commit(message: str, committed: str, authored: str = "2026-11-20T00:00:00Z") -> dict[str, str]:
    return {"id": "x", "message": message, "committed_date": committed, "authored_date": authored}


def file_body() -> dict[str, str]:
    return {"content": base64.b64encode(VMCLUSTER.encode()).decode(), "last_commit_id": "c0ffee1"}


@pytest.fixture
async def backend():  # type: ignore[no-untyped-def]
    b = GitLabGitOpsBackend(
        GITLAB,
        "glpat-x",
        2.0,
        0,
        True,
        Metrics(),
        {TARGET_KEY: LOCATION},
        GitLabConfig(),
        clock=lambda: NOW,
        history_window=timedelta(hours=24),
        retry_wait_seconds=0,
    )
    yield b
    await b.close()


def test_prefix_matches_the_service_annotation_key() -> None:
    assert f"{COOLDOWN_PREFIX}cpu" == cooldown_annotation("cpu")


def test_finds_newest_commit_per_resource() -> None:
    commits = [
        commit(
            f"{TITLE} cpu 4 -> 5 (usage 91.2% >= 80%), "
            "memory 16Gi -> 20Gi (usage 85.0% >= 80%)\n\n- cpu: ...",
            "2026-11-27T10:00:00.000+00:00",
        ),
        commit(f"{TITLE} cpu 3200m -> 4 (usage 90.0% >= 80%)", "2026-11-27T02:00:00Z"),
    ]
    assert last_scaled_from_commits(commits, "[vm-autoscaler]", target()) == {
        cooldown_annotation("cpu"): "2026-11-27T10:00:00Z",
        cooldown_annotation("memory"): "2026-11-27T10:00:00Z",
    }


def test_merge_commit_counts_from_merge_time() -> None:
    message = (
        "Merge branch 'vm-autoscaler/ocp-prod-region-a-replicas-20261125T000000Z' into 'main'\n\n"
        f"{TITLE} replicas 3 -> 4 (usage 90.0% >= 85%)\n\nSee merge request infra/monitoring!42"
    )
    found = last_scaled_from_commits(
        [commit(message, committed="2026-11-27T09:00:00Z", authored="2026-11-25T00:00:00Z")],
        "[vm-autoscaler]",
        target(),
    )
    assert found == {cooldown_annotation("replicas"): "2026-11-27T09:00:00Z"}


@pytest.mark.parametrize(
    "message",
    [
        # other cluster, other component, not ours, other prefix:
        "[vm-autoscaler] ocp-prod-region-b/monitoring vmstorage cpu 4 -> 5 (usage 91%)",
        "[vm-autoscaler] ocp-prod-region-a/monitoring vmselect cpu 4 -> 5 (usage 91%)",
        "Bump cpu 4 -> 5 by hand",
        "[other-bot] ocp-prod-region-a/monitoring vmstorage cpu 4 -> 5",
    ],
)
def test_other_commits_are_ignored(message: str) -> None:
    assert (
        last_scaled_from_commits(
            [commit(message, "2026-11-27T10:00:00Z")], "[vm-autoscaler]", target()
        )
        == {}
    )


def test_revert_of_our_commit_starts_a_cooldown_safe_direction() -> None:
    message = f'Revert "{TITLE} cpu 4 -> 5 (usage 91.2% >= 80%)"'
    found = last_scaled_from_commits(
        [commit(message, "2026-11-27T11:00:00Z")], "[vm-autoscaler]", target()
    )
    assert cooldown_annotation("cpu") in found


def test_merge_newest_keeps_newer_time() -> None:
    merged = merge_newest(
        {"a": "2026-11-27T10:00:00Z", "b": "2026-11-27T10:00:00Z"},
        {"a": "2026-11-27T11:00:00Z", "b": "2026-11-27T09:00:00Z", "c": "2026-11-27T08:00:00Z"},
    )
    assert merged == {
        "a": "2026-11-27T11:00:00Z",
        "b": "2026-11-27T10:00:00Z",
        "c": "2026-11-27T08:00:00Z",
    }


@respx.mock
async def test_state_read_queries_first_parent_history(backend: GitLabGitOpsBackend) -> None:
    respx.get(FILE_URL).respond(200, json=file_body())
    history = respx.get(COMMITS_URL).respond(
        200, json=[commit(f"{TITLE} cpu 4 -> 5 (usage 91.2% >= 80%)", "2026-11-27T10:00:00Z")]
    )
    state = await backend.get_component_state(target())
    assert state.annotations == {cooldown_annotation("cpu"): "2026-11-27T10:00:00Z"}
    params = history.calls.last.request.url.params
    assert params["ref_name"] == "main"
    assert params["path"] == "clusters/region-a/vmcluster.yaml"
    assert params["first_parent"] == "true"
    assert params["since"] == (NOW - timedelta(hours=24)).isoformat()


@respx.mock
async def test_history_is_paged(backend: GitLabGitOpsBackend) -> None:
    respx.get(FILE_URL).respond(200, json=file_body())
    full_page = [commit("unrelated", "2026-11-27T10:00:00Z")] * 100
    last_page = [
        commit(f"{TITLE} memory 16Gi -> 20Gi (usage 85.0% >= 80%)", "2026-11-27T03:00:00Z")
    ]
    route = respx.get(COMMITS_URL).mock(
        side_effect=[
            respx.MockResponse(200, json=full_page),
            respx.MockResponse(200, json=last_page),
        ]
    )
    state = await backend.get_component_state(target())
    assert route.call_count == 2
    assert cooldown_annotation("memory") in state.annotations


@respx.mock
async def test_commit_mode_writes_no_annotations(backend: GitLabGitOpsBackend) -> None:
    respx.get(FILE_URL).respond(200, json=file_body())
    commit_route = respx.post(COMMITS_URL).respond(201, json={"id": "abc"})
    change = ResourceChange(resource="cpu", field="cpu_limit", yaml_path="x", old="4", new="5")
    await backend.apply_changes(
        target(),
        [change],
        "t",
        {cooldown_annotation("cpu"): "2026-11-27T12:00:00Z"},
        "c0ffee1",
        "k",
    )
    content = json.loads(commit_route.calls.last.request.content)["actions"][0]["content"]
    assert changed_lines(VMCLUSTER, content) == ['        cpu: "5"   # see capacity doc']
