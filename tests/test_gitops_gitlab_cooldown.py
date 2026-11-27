"""Cooldown inferred from commit history (gitlab.cooldown_source: commits, the default)."""

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx

from app.adapters.gitops_gitlab import (
    GitLabGitOpsBackend,
    last_scaled_from_commits,
    merge_newest,
)
from app.adapters.yaml_edit import changed_lines
from app.config import GitLabConfig, GitLabLocation
from app.domain.errors import GitOpsResponseError
from app.domain.models import COOLDOWN_ANNOTATION_PREFIX, ResourceChange
from app.observability import Metrics
from app.services.scaling_service import cooldown_annotation
from tests.conftest import FIXTURES
from tests.factories import TARGET_KEY, target

GITLAB = "https://gitlab.example.internal"
PROJECT = f"{GITLAB}/api/v4/projects/infra%2Fvm-values"
FILE_PATH = "ocp-prod-region-a/monitoring/values.yaml"
FILE_URL = f"{PROJECT}/repository/files/ocp-prod-region-a%2Fmonitoring%2Fvalues.yaml"
COMMITS_URL = f"{PROJECT}/repository/commits"
VALUES = (FIXTURES / "values.yaml").read_text()
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
TITLE = "[vm-autoscaler] ocp-prod-region-a/monitoring vmstorage"
STORAGE = cooldown_annotation("storage")
LOCATION = GitLabLocation(
    project="infra/vm-values",
    branch="main",
    file=FILE_PATH,
    document_kind=None,
    document_name=None,
    storage_path="vmstorage.pvc.size",
    annotations_path="vmAutoscaler",
)


def commit(message: str, committed: str, authored: str = "2026-11-20T00:00:00Z") -> dict[str, str]:
    return {"id": "x", "message": message, "committed_date": committed, "authored_date": authored}


def file_body() -> dict[str, str]:
    return {"content": base64.b64encode(VALUES.encode()).decode(), "last_commit_id": "c0ffee1"}


def found(commits: list[dict[str, Any]]) -> dict[str, str]:
    return last_scaled_from_commits(commits, "[vm-autoscaler]", target())


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


def test_default_cooldown_source_is_commits() -> None:
    assert GitLabConfig().cooldown_source == "commits"


def test_prefix_matches_the_service_annotation_key() -> None:
    assert f"{COOLDOWN_ANNOTATION_PREFIX}storage" == STORAGE


def test_finds_newest_matching_commit() -> None:
    commits = [
        commit(
            f"{TITLE} storage 30Gi -> 36Gi (usage 85.0% >= 80%)\n\n- storage: ...",
            "2026-11-27T10:00:00.000+00:00",
        ),
        commit(f"{TITLE} storage 25Gi -> 30Gi (usage 90.0% >= 80%)", "2026-11-27T02:00:00Z"),
    ]
    assert found(commits) == {STORAGE: "2026-11-27T10:00:00Z"}


def test_merge_commit_counts_from_merge_time() -> None:
    message = (
        "Merge branch 'vm-autoscaler/ocp-prod-region-a-storage-20261125T000000Z' into 'main'\n\n"
        f"{TITLE} storage 30Gi -> 36Gi (usage 85.0% >= 80%)\n\nSee merge request infra/vm-values!42"
    )
    merged = commit(message, committed="2026-11-27T09:00:00Z", authored="2026-11-25T00:00:00Z")
    assert found([merged]) == {STORAGE: "2026-11-27T09:00:00Z"}


def test_squash_commit_with_mr_title_counts() -> None:
    squash = commit(f"{TITLE} storage 30Gi -> 36Gi (usage 85.0% >= 80%)", "2026-11-27T08:00:00Z")
    assert found([squash]) == {STORAGE: "2026-11-27T08:00:00Z"}


@pytest.mark.parametrize(
    "message",
    [
        "[vm-autoscaler] ocp-prod-region-b/monitoring vmstorage storage 30Gi -> 36Gi (85%)",
        "[vm-autoscaler] ocp-prod-region-a/logging vmstorage storage 30Gi -> 36Gi (85%)",
        "[vm-autoscaler] ocp-prod-region-a/monitoring vmselect storage 10Gi -> 12Gi (85%)",
        "[other-bot] ocp-prod-region-a/monitoring vmstorage storage 30Gi -> 36Gi",
        "Bump vmstorage storage 30Gi -> 36Gi by hand",
        "[vm-autoscaler] ocp-prod-region-a/monitoring vmstorage nothing changed",
    ],
)
def test_other_commits_are_ignored(message: str) -> None:
    assert found([commit(message, "2026-11-27T10:00:00Z")]) == {}


def test_revert_of_our_commit_starts_a_cooldown_safe_direction() -> None:
    message = f'Revert "{TITLE} storage 30Gi -> 36Gi (usage 85.0% >= 80%)"'
    assert STORAGE in found([commit(message, "2026-11-27T11:00:00Z")])


def test_commits_without_a_usable_date_are_skipped() -> None:
    bad = {"message": f"{TITLE} storage 30Gi -> 36Gi", "committed_date": "yesterday"}
    assert found([bad, "not-a-dict"]) == {}  # type: ignore[list-item]


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
        200,
        json=[commit(f"{TITLE} storage 25Gi -> 30Gi (usage 85% >= 80%)", "2026-11-27T10:00:00Z")],
    )
    state = await backend.get_component_state(target())
    assert state.storage == "30Gi"
    assert state.annotations == {STORAGE: "2026-11-27T10:00:00Z"}
    params = history.calls.last.request.url.params
    assert params["ref_name"] == "main"
    assert params["path"] == FILE_PATH
    assert params["first_parent"] == "true"
    assert params["since"] == (NOW - timedelta(hours=24)).isoformat()


@respx.mock
async def test_history_is_paged(backend: GitLabGitOpsBackend) -> None:
    respx.get(FILE_URL).respond(200, json=file_body())
    full_page = [commit("unrelated", "2026-11-27T10:00:00Z")] * 100
    last_page = [commit(f"{TITLE} storage 25Gi -> 30Gi (usage 85% >= 80%)", "2026-11-27T03:00:00Z")]
    route = respx.get(COMMITS_URL).mock(
        side_effect=[
            respx.MockResponse(200, json=full_page),
            respx.MockResponse(200, json=last_page),
        ]
    )
    state = await backend.get_component_state(target())
    assert route.call_count == 2
    assert route.calls.last.request.url.params["page"] == "2"
    assert STORAGE in state.annotations


@respx.mock
async def test_history_that_is_not_a_list_is_an_error(backend: GitLabGitOpsBackend) -> None:
    respx.get(FILE_URL).respond(200, json=file_body())
    respx.get(COMMITS_URL).respond(200, json={"message": "odd"})
    with pytest.raises(GitOpsResponseError, match="not a list"):
        await backend.get_component_state(target())


@respx.mock
async def test_commit_mode_writes_no_annotations(backend: GitLabGitOpsBackend) -> None:
    respx.get(FILE_URL).respond(200, json=file_body())
    commit_route = respx.post(COMMITS_URL).respond(201, json={"id": "abc"})
    change = ResourceChange(
        resource="storage", field="storage", yaml_path="x", old="30Gi", new="36Gi"
    )
    await backend.apply_changes(
        target(), [change], "t", {STORAGE: "2026-11-27T12:00:00Z"}, "c0ffee1", "k"
    )
    content = json.loads(commit_route.calls.last.request.content)["actions"][0]["content"]
    assert changed_lines(VALUES, content) == ["    size: 36Gi   # grown by vm-autoscaler"]
