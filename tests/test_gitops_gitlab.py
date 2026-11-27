import base64
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from app.adapters.gitops_gitlab import AUTH_HINT, GitLabGitOpsBackend
from app.adapters.manifest import changed_lines
from app.config import GitLabConfig, GitLabTargetConfig
from app.domain.errors import (
    GitOpsConflictError,
    GitOpsNotFoundError,
    GitOpsRejectedError,
    GitOpsUnavailableError,
)
from app.domain.models import ResourceChange
from app.observability import JsonFormatter, Metrics
from tests.conftest import FIXTURES
from tests.factories import TARGET_KEY, target

GITLAB = "https://gitlab.example.internal"
API = f"{GITLAB}/api/v4"
TOKEN = "glpat-very-secret-token-123"
PROJECT = f"{API}/projects/infra%2Fmonitoring"
FILE_URL = f"{PROJECT}/repository/files/clusters%2Fregion-a%2Fvmcluster.yaml"
COMMITS_URL = f"{PROJECT}/repository/commits"
MRS_URL = f"{PROJECT}/merge_requests"
VMCLUSTER = (FIXTURES / "vmcluster.yaml").read_text()
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
ANNOTATIONS = {
    "vm-autoscaler/last-scaled-cpu": "2026-11-27T12:00:00Z",
    "vm-autoscaler/last-scaled-memory": "2026-11-27T12:00:00Z",
}
LOCATION = GitLabTargetConfig(
    project="infra/monitoring",
    branch="main",
    file="clusters/region-a/vmcluster.yaml",
    document_kind="VMCluster",
    document_name="vm-main",
)


def change(field: str, old: str, new: str) -> ResourceChange:
    resource = field.split("_")[0]
    return ResourceChange.model_validate(
        {"resource": resource, "field": field, "yaml_path": f"x.{field}", "old": old, "new": new}
    )


CHANGES = [
    change("cpu_limit", "4", "5"),
    change("cpu_request", "2", "2500m"),
    change("memory_limit", "16Gi", "20Gi"),
    change("memory_request", "16Gi", "20Gi"),
]


def file_body(text: str = VMCLUSTER, commit: str = "c0ffee1") -> dict[str, Any]:
    return {
        "file_path": LOCATION.file,
        "ref": "main",
        "encoding": "base64",
        "content": base64.b64encode(text.encode()).decode(),
        "blob_id": "b10b",
        "last_commit_id": commit,
    }


@pytest.fixture
def metrics() -> Metrics:
    return Metrics()


def make_backend(metrics: Metrics, cooldown_source: str) -> GitLabGitOpsBackend:
    return GitLabGitOpsBackend(
        GITLAB,
        TOKEN,
        2.0,
        3,
        True,
        metrics,
        {TARGET_KEY: LOCATION},
        GitLabConfig(cooldown_source=cooldown_source),  # type: ignore[arg-type]
        clock=lambda: NOW,
        retry_wait_seconds=0,
        history_window=timedelta(hours=24),
    )


@pytest.fixture
async def backend(metrics: Metrics):  # type: ignore[no-untyped-def]
    """Annotations mode: these tests cover the file API and annotation writing."""
    b = make_backend(metrics, "annotations")
    yield b
    await b.close()


async def _apply(backend: GitLabGitOpsBackend, expected: str | None = "c0ffee1"):  # type: ignore[no-untyped-def]
    return await backend.apply_changes(
        target(), CHANGES, "[vm-autoscaler] title\n\ndetails", ANNOTATIONS, expected, "idem-1"
    )


@respx.mock
async def test_read_builds_state(backend: GitLabGitOpsBackend) -> None:
    route = respx.get(FILE_URL).respond(200, json=file_body())
    state = await backend.get_component_state(target())
    assert (state.cpu_request, state.cpu_limit) == ("2", "4")
    assert (state.memory_request, state.memory_limit, state.storage) == ("16Gi", "16Gi", "500Gi")
    assert state.replicas == 3
    assert state.version == "c0ffee1"
    assert state.annotations == {}
    request = route.calls.last.request
    assert request.url.params["ref"] == "main"
    assert request.headers["PRIVATE-TOKEN"] == TOKEN
    assert request.url.raw_path.startswith(
        b"/api/v4/projects/infra%2Fmonitoring/repository/files/clusters%2Fregion-a"
    )


@respx.mock
async def test_missing_path_gives_none(backend: GitLabGitOpsBackend) -> None:
    text = VMCLUSTER.replace('        cpu: "4"   # see capacity doc\n', "")
    respx.get(FILE_URL).respond(200, json=file_body(text))
    state = await backend.get_component_state(target())
    assert state.cpu_limit is None


@respx.mock
async def test_direct_commit_sends_one_minimal_commit(backend: GitLabGitOpsBackend) -> None:
    respx.get(FILE_URL).respond(200, json=file_body())
    commit = respx.post(COMMITS_URL).respond(
        201, json={"id": "abc999", "web_url": f"{GITLAB}/c/abc999"}
    )
    result = await _apply(backend)
    assert (result.accepted, result.change_id, result.url) == (True, "abc999", f"{GITLAB}/c/abc999")
    assert commit.call_count == 1
    assert (
        commit.calls.last.request.url.raw_path
        == b"/api/v4/projects/infra%2Fmonitoring/repository/commits"
    )
    body = json.loads(commit.calls.last.request.content)
    assert body["branch"] == "main" and "start_branch" not in body
    assert body["author_name"] == "vm-autoscaler"
    assert body["commit_message"].startswith("[vm-autoscaler] title")
    assert "Idempotency-Key: idem-1" in body["commit_message"]
    [action] = body["actions"]
    assert (action["action"], action["file_path"], action["last_commit_id"]) == (
        "update",
        LOCATION.file,
        "c0ffee1",
    )
    assert changed_lines(VMCLUSTER, action["content"]) == [
        "  annotations:",
        '    vm-autoscaler/last-scaled-cpu: "2026-11-27T12:00:00Z"',
        '    vm-autoscaler/last-scaled-memory: "2026-11-27T12:00:00Z"',
        '        cpu: "2500m"',
        "        memory: 20Gi",
        '        cpu: "5"   # see capacity doc',
        "        memory: 20Gi",
    ]


@respx.mock
async def test_version_moved_since_read_is_conflict(backend: GitLabGitOpsBackend) -> None:
    respx.get(FILE_URL).respond(200, json=file_body(commit="newer"))
    commit = respx.post(COMMITS_URL)
    with pytest.raises(GitOpsConflictError, match="changed since it was read"):
        await _apply(backend)
    assert commit.call_count == 0


@respx.mock
async def test_value_differs_from_expected_old_is_conflict(backend: GitLabGitOpsBackend) -> None:
    respx.get(FILE_URL).respond(200, json=file_body(VMCLUSTER.replace('cpu: "4"', 'cpu: "6"')))
    with pytest.raises(GitOpsConflictError, match="expected '4'"):
        await _apply(backend, expected=None)


@respx.mock
async def test_stale_last_commit_id_is_conflict(backend: GitLabGitOpsBackend) -> None:
    respx.get(FILE_URL).respond(200, json=file_body())
    respx.post(COMMITS_URL).respond(
        400,
        json={
            "message": "You are attempting to update a file that has changed "
            "since you started editing it."
        },
    )
    with pytest.raises(GitOpsConflictError):
        await _apply(backend)


@respx.mock
async def test_409_is_conflict(backend: GitLabGitOpsBackend) -> None:
    respx.get(FILE_URL).respond(200, json=file_body())
    respx.post(COMMITS_URL).respond(409, json={"message": "conflict"})
    with pytest.raises(GitOpsConflictError):
        await _apply(backend)


@respx.mock
async def test_404_file(backend: GitLabGitOpsBackend) -> None:
    respx.get(FILE_URL).respond(404, json={"message": "404 File Not Found"})
    respx.get(PROJECT).respond(200, json={"id": 1})
    respx.get(f"{PROJECT}/repository/branches/main").respond(200, json={"name": "main"})
    with pytest.raises(
        GitOpsNotFoundError, match="file 'clusters/region-a/vmcluster.yaml' not found"
    ):
        await backend.get_component_state(target())


@respx.mock
async def test_404_branch(backend: GitLabGitOpsBackend) -> None:
    respx.get(FILE_URL).respond(404, json={"message": "404 File Not Found"})
    respx.get(PROJECT).respond(200, json={"id": 1})
    respx.get(f"{PROJECT}/repository/branches/main").respond(404)
    with pytest.raises(GitOpsNotFoundError, match="branch 'main' not found"):
        await backend.get_component_state(target())


@respx.mock
async def test_404_project(backend: GitLabGitOpsBackend) -> None:
    respx.get(FILE_URL).respond(404, json={"message": "404 Project Not Found"})
    respx.get(PROJECT).respond(404)
    with pytest.raises(GitOpsNotFoundError, match="project 'infra/monitoring' not found"):
        await backend.get_component_state(target())


@respx.mock
async def test_403_rejected_with_hint(backend: GitLabGitOpsBackend) -> None:
    respx.get(FILE_URL).respond(200, json={**file_body()})
    respx.post(COMMITS_URL).respond(403, json={"message": "403 Forbidden"})
    with pytest.raises(GitOpsRejectedError) as info:
        await _apply(backend)
    assert info.value.status_code == 403
    assert "check token scope `api` and the branch protection rules" in str(info.value)
    assert AUTH_HINT in str(info.value)


@respx.mock
async def test_other_400_rejected_with_truncated_body(backend: GitLabGitOpsBackend) -> None:
    respx.get(FILE_URL).respond(200, json=file_body())
    respx.post(COMMITS_URL).respond(400, text="y" * 3000)
    with pytest.raises(GitOpsRejectedError) as info:
        await _apply(backend)
    assert len(info.value.body) <= 500 + len("...[truncated]")


@respx.mock
async def test_get_503_is_retried(backend: GitLabGitOpsBackend) -> None:
    route = respx.get(FILE_URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=file_body())]
    )
    await backend.get_component_state(target())
    assert route.call_count == 2


@respx.mock
async def test_get_timeout_gives_unavailable(backend: GitLabGitOpsBackend) -> None:
    route = respx.get(FILE_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(GitOpsUnavailableError):
        await backend.get_component_state(target())
    assert route.call_count == 4


@respx.mock
@pytest.mark.parametrize("failure", [httpx.Response(502), httpx.ReadTimeout("slow")])
async def test_post_is_not_retried(backend: GitLabGitOpsBackend, failure: Any) -> None:
    respx.get(FILE_URL).respond(200, json=file_body())
    route = respx.post(COMMITS_URL).mock(side_effect=[failure])
    with pytest.raises(GitOpsUnavailableError):
        await _apply(backend)
    assert route.call_count == 1


@respx.mock
async def test_merge_request_mode(backend: GitLabGitOpsBackend) -> None:
    search = respx.get(MRS_URL).respond(200, json=[])
    respx.get(FILE_URL).respond(200, json=file_body())
    commit = respx.post(COMMITS_URL).respond(201, json={"id": "abc"})
    create = respx.post(MRS_URL).respond(201, json={"iid": 42, "web_url": f"{GITLAB}/mr/42"})
    result = await backend.open_change_request(
        target(), CHANGES[:1], "[vm-autoscaler] title line\n\n- cpu: details", ANNOTATIONS, "idem-7"
    )
    assert (result.change_id, result.url) == ("!42", f"{GITLAB}/mr/42")
    assert search.calls.last.request.url.params["search"] == "idem-7"
    commit_body = json.loads(commit.calls.last.request.content)
    assert commit_body["start_branch"] == "main"
    assert commit_body["branch"] == "vm-autoscaler/ocp-prod-region-a-cpu-20261127T120000Z"
    assert commit_body["actions"][0]["last_commit_id"] == "c0ffee1"
    mr = json.loads(create.calls.last.request.content)
    assert mr["source_branch"] == commit_body["branch"]
    assert mr["target_branch"] == "main"
    assert mr["title"] == "[vm-autoscaler] title line"
    assert mr["description"].startswith("- cpu: details")
    assert "idem-7" in mr["description"]
    assert mr["remove_source_branch"] is True


@respx.mock
async def test_existing_merge_request_is_reused(backend: GitLabGitOpsBackend) -> None:
    respx.get(MRS_URL).respond(
        200,
        json=[
            {
                "iid": 7,
                "web_url": f"{GITLAB}/mr/7",
                "source_branch": "vm-autoscaler/ocp-prod-region-a-cpu-20261126T000000Z",
                "description": "details\n\nIdempotency-Key: `idem-7`",
            }
        ],
    )
    commit = respx.post(COMMITS_URL)
    result = await backend.open_change_request(target(), CHANGES[:1], "t\n\nd", {}, "idem-7")
    assert result.url == f"{GITLAB}/mr/7"
    assert commit.call_count == 0


@respx.mock
async def test_failed_merge_request_deletes_branch(backend: GitLabGitOpsBackend) -> None:
    respx.get(MRS_URL).respond(200, json=[])
    respx.get(FILE_URL).respond(200, json=file_body())
    respx.post(COMMITS_URL).respond(201, json={"id": "abc"})
    respx.post(MRS_URL).respond(422, json={"message": "nope"})
    delete = respx.delete(
        f"{PROJECT}/repository/branches/vm-autoscaler%2Focp-prod-region-a-cpu-20261127T120000Z"
    ).respond(204)
    with pytest.raises(GitOpsRejectedError):
        await backend.open_change_request(target(), CHANGES[:1], "t\n\nd", {}, "k")
    assert delete.call_count == 1


@respx.mock
async def test_token_never_in_logs_or_exceptions(
    backend: GitLabGitOpsBackend, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    respx.get(FILE_URL).respond(200, json=file_body())
    respx.post(COMMITS_URL).respond(400, text=f"bad header PRIVATE-TOKEN: {TOKEN}")
    with pytest.raises(GitOpsRejectedError) as info:
        await _apply(backend)
    assert TOKEN not in str(info.value)
    respx.get(FILE_URL).respond(503, text=f"echo {TOKEN}")
    with pytest.raises(GitOpsUnavailableError) as unavailable:
        await backend.get_component_state(target())
    assert TOKEN not in str(unavailable.value)
    rendered = "\n".join(JsonFormatter().format(r) for r in caplog.records)
    assert "gitlab request" in rendered
    assert TOKEN not in rendered


@respx.mock
async def test_metrics_per_operation(backend: GitLabGitOpsBackend, metrics: Metrics) -> None:
    respx.get(FILE_URL).respond(200, json=file_body())
    await backend.get_component_state(target())
    assert (
        metrics.registry.get_sample_value(
            "vm_autoscaler_gitops_requests_total",
            {"operation": "get_component_state", "result": "success"},
        )
        == 1
    )


@respx.mock
async def test_readiness_project_check(backend: GitLabGitOpsBackend) -> None:
    respx.get(PROJECT).mock(side_effect=[httpx.Response(200, json={}), httpx.Response(401)])
    assert backend.projects() == ["infra/monitoring"]
    assert await backend.check_project("infra/monitoring") is True
    assert await backend.check_project("infra/monitoring") is False
