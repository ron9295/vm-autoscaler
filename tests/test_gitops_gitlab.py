import base64
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
import pytest
import respx

from app.adapters.gitops_gitlab import (
    AUTH_HINT,
    TARGET_MR_OPEN_MESSAGE,
    GitLabGitOpsBackend,
    ref_slug,
    target_branch_prefix,
)
from app.adapters.yaml_edit import changed_lines
from app.config import GitLabConfig, GitLabLocation
from app.domain.errors import (
    GitOpsConflictError,
    GitOpsManifestError,
    GitOpsNotFoundError,
    GitOpsRejectedError,
    GitOpsResponseError,
    GitOpsUnavailableError,
)
from app.domain.models import ResourceChange
from app.observability import JsonFormatter, Metrics
from tests.conftest import FIXTURES
from tests.factories import TARGET_KEY, target

GITLAB = "https://gitlab.example.internal"
API = f"{GITLAB}/api/v4"
TOKEN = "glpat-very-secret-token-123"
PROJECT = f"{API}/projects/infra%2Fvm-values"
FILE_PATH = "ocp-prod-region-a/monitoring/values.yaml"
FILE_URL = f"{PROJECT}/repository/files/ocp-prod-region-a%2Fmonitoring%2Fvalues.yaml"
COMMITS_URL = f"{PROJECT}/repository/commits"
MRS_URL = f"{PROJECT}/merge_requests"
VALUES = (FIXTURES / "values.yaml").read_text()
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
ANNOTATIONS = {"vm-autoscaler/last-scaled-storage": "2026-11-27T12:00:00Z"}
LOCATION = GitLabLocation(
    project="infra/vm-values",
    branch="main",
    file=FILE_PATH,
    document_kind=None,
    document_name=None,
    storage_path="vmstorage.pvc.size",
    annotations_path="vmAutoscaler",
)
CHANGES = [
    ResourceChange(resource="storage", field="storage", yaml_path="ignored", old="30Gi", new="36Gi")
]
# Branch for target ocp-prod-region-a/monitoring at NOW with idempotency key "idem-7".
BRANCH = "vm-autoscaler/ocp-prod-region-a-monitoring-storage-20261127T120000Z-idem-7"
REASON = "[vm-autoscaler] ocp-prod-region-a/monitoring vmstorage storage 30Gi -> 36Gi\n\n- details"


def file_body(text: str = VALUES, commit: str = "c0ffee1") -> dict[str, Any]:
    return {
        "file_path": FILE_PATH,
        "ref": "main",
        "encoding": "base64",
        "content": base64.b64encode(text.encode()).decode(),
        "blob_id": "b10b",
        "last_commit_id": commit,
    }


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
def metrics() -> Metrics:
    return Metrics()


@pytest.fixture
async def backend(metrics: Metrics):  # type: ignore[no-untyped-def]
    """Annotations mode: these tests cover the file API and annotation writing.

    Commit-history cooldowns (the default) are covered in test_gitops_gitlab_cooldown.py.
    """
    b = make_backend(metrics, "annotations")
    yield b
    await b.close()


async def _apply(backend: GitLabGitOpsBackend, expected: str | None = "c0ffee1"):  # type: ignore[no-untyped-def]
    return await backend.apply_changes(target(), CHANGES, REASON, ANNOTATIONS, expected, "idem-1")


@respx.mock
async def test_read_builds_state(backend: GitLabGitOpsBackend) -> None:
    route = respx.get(FILE_URL).respond(200, json=file_body())
    state = await backend.get_component_state(target())
    assert state.storage == "30Gi"
    assert state.version == "c0ffee1"
    assert state.annotations == {}
    request = route.calls.last.request
    assert request.url.params["ref"] == "main"
    assert request.headers["PRIVATE-TOKEN"] == TOKEN
    assert request.url.raw_path.startswith(
        b"/api/v4/projects/infra%2Fvm-values/repository/files/ocp-prod-region-a%2Fmonitoring%2F"
    )


@respx.mock
async def test_read_existing_annotations(backend: GitLabGitOpsBackend) -> None:
    text = VALUES + 'vmAutoscaler:\n  vm-autoscaler/last-scaled-storage: "2026-11-27T01:00:00Z"\n'
    respx.get(FILE_URL).respond(200, json=file_body(text))
    state = await backend.get_component_state(target())
    assert state.annotations == {"vm-autoscaler/last-scaled-storage": "2026-11-27T01:00:00Z"}


@respx.mock
async def test_missing_size_gives_none(backend: GitLabGitOpsBackend) -> None:
    text = VALUES.replace("    size: 30Gi   # grown by vm-autoscaler\n", "")
    respx.get(FILE_URL).respond(200, json=file_body(text))
    assert (await backend.get_component_state(target())).storage is None


@respx.mock
async def test_bad_base64_is_response_error(backend: GitLabGitOpsBackend) -> None:
    respx.get(FILE_URL).respond(200, json={"content": "!!!not base64", "last_commit_id": "x"})
    with pytest.raises(GitOpsResponseError, match="unexpected file response"):
        await backend.get_component_state(target())


@respx.mock
async def test_direct_commit_sends_one_minimal_commit(backend: GitLabGitOpsBackend) -> None:
    respx.get(FILE_URL).respond(200, json=file_body())
    commit = respx.post(COMMITS_URL).respond(
        201, json={"id": "abc999", "web_url": f"{GITLAB}/c/abc999"}
    )
    result = await _apply(backend)
    assert (result.accepted, result.change_id, result.url) == (True, "abc999", f"{GITLAB}/c/abc999")
    assert result.message == REASON.partition("\n")[0]
    assert commit.call_count == 1
    assert (
        commit.calls.last.request.url.raw_path
        == b"/api/v4/projects/infra%2Fvm-values/repository/commits"
    )
    body = json.loads(commit.calls.last.request.content)
    assert body["branch"] == "main" and "start_branch" not in body
    assert (body["author_name"], body["author_email"]) == (
        "vm-autoscaler",
        "vm-autoscaler@example.local",
    )
    assert body["commit_message"].startswith(REASON)
    assert body["commit_message"].endswith("Idempotency-Key: idem-1")
    [action] = body["actions"]
    assert (action["action"], action["file_path"], action["last_commit_id"]) == (
        "update",
        FILE_PATH,
        "c0ffee1",
    )
    assert changed_lines(VALUES, action["content"]) == [
        "    size: 36Gi   # grown by vm-autoscaler",
        "vmAutoscaler:",
        '  vm-autoscaler/last-scaled-storage: "2026-11-27T12:00:00Z"',
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
    respx.get(FILE_URL).respond(200, json=file_body(VALUES.replace("size: 30Gi", "size: 40Gi")))
    commit = respx.post(COMMITS_URL)
    with pytest.raises(GitOpsConflictError, match="expected '30Gi'"):
        await _apply(backend, expected=None)
    assert commit.call_count == 0


@respx.mock
async def test_formatting_drift_is_refused(backend: GitLabGitOpsBackend) -> None:
    # Mixed sequence indentation can't be round-tripped: the diff guard must refuse.
    text = VALUES + "extra:\n- a\nother:\n    - b\n"
    respx.get(FILE_URL).respond(200, json=file_body(text))
    commit = respx.post(COMMITS_URL)
    with pytest.raises(GitOpsManifestError, match="refusing to commit"):
        await _apply(backend)
    assert commit.call_count == 0


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
    with pytest.raises(GitOpsNotFoundError, match=f"file '{FILE_PATH}' not found on branch 'main'"):
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
    with pytest.raises(GitOpsNotFoundError, match="project 'infra/vm-values' not found"):
        await backend.get_component_state(target())


@respx.mock
async def test_403_rejected_with_hint(backend: GitLabGitOpsBackend) -> None:
    respx.get(FILE_URL).respond(200, json=file_body())
    respx.post(COMMITS_URL).respond(403, json={"message": "403 Forbidden"})
    with pytest.raises(GitOpsRejectedError) as info:
        await _apply(backend)
    assert info.value.status_code == 403
    assert "check token scope `api` and the branch protection rules" in str(info.value)
    assert AUTH_HINT in str(info.value)


@respx.mock
async def test_401_rejected_with_hint(backend: GitLabGitOpsBackend) -> None:
    respx.get(FILE_URL).respond(401, json={"message": "401 Unauthorized"})
    with pytest.raises(GitOpsRejectedError, match="check token scope"):
        await backend.get_component_state(target())


@respx.mock
async def test_other_400_rejected_with_truncated_body(backend: GitLabGitOpsBackend) -> None:
    respx.get(FILE_URL).respond(200, json=file_body())
    respx.post(COMMITS_URL).respond(400, text="y" * 3000)
    with pytest.raises(GitOpsRejectedError) as info:
        await _apply(backend)
    assert info.value.status_code == 400
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
    assert route.call_count == 4  # 1 try + 3 retries


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
    result = await backend.open_change_request(target(), CHANGES, REASON, ANNOTATIONS, "idem-7")
    assert (result.change_id, result.url) == ("!42", f"{GITLAB}/mr/42")
    params = search.calls.last.request.url.params
    assert (params["state"], params["target_branch"]) == ("opened", "main")
    commit_body = json.loads(commit.calls.last.request.content)
    assert commit_body["start_branch"] == "main"
    assert commit_body["branch"] == BRANCH
    assert commit_body["actions"][0]["last_commit_id"] == "c0ffee1"
    mr = json.loads(create.calls.last.request.content)
    assert mr["source_branch"] == commit_body["branch"]
    assert mr["target_branch"] == "main"
    assert mr["title"] == REASON.partition("\n")[0]
    assert mr["description"].startswith("- details")
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
                "source_branch": "vm-autoscaler/ocp-prod-region-a-storage-20261126T000000Z",
                "description": "details\n\nIdempotency-Key: `idem-7`",
            }
        ],
    )
    commit = respx.post(COMMITS_URL)
    result = await backend.open_change_request(target(), CHANGES, REASON, {}, "idem-7")
    assert (result.change_id, result.url) == ("!7", f"{GITLAB}/mr/7")
    assert commit.call_count == 0


@respx.mock
async def test_merge_request_from_a_human_branch_is_not_reused(
    backend: GitLabGitOpsBackend,
) -> None:
    respx.get(MRS_URL).respond(
        200, json=[{"iid": 9, "source_branch": "feature/x", "description": "idem-7"}]
    )
    respx.get(FILE_URL).respond(200, json=file_body())
    commit = respx.post(COMMITS_URL).respond(201, json={"id": "abc"})
    respx.post(MRS_URL).respond(201, json={"iid": 43, "web_url": f"{GITLAB}/mr/43"})
    result = await backend.open_change_request(target(), CHANGES, REASON, {}, "idem-7")
    assert result.change_id == "!43"
    assert commit.call_count == 1


@respx.mock
async def test_failed_merge_request_deletes_branch(backend: GitLabGitOpsBackend) -> None:
    respx.get(MRS_URL).respond(200, json=[])
    respx.get(FILE_URL).respond(200, json=file_body())
    respx.post(COMMITS_URL).respond(201, json={"id": "abc"})
    respx.post(MRS_URL).respond(422, json={"message": "nope"})
    delete = respx.delete(f"{PROJECT}/repository/branches/{quote(BRANCH, safe='')}").respond(204)
    with pytest.raises(GitOpsRejectedError):
        await backend.open_change_request(target(), CHANGES, REASON, {}, "idem-7")
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
    respx.get(FILE_URL).respond(200, json=file_body(commit="moved"))
    with pytest.raises(GitOpsConflictError):
        await _apply(backend)
    sample = metrics.registry.get_sample_value
    name = "vm_autoscaler_gitops_requests_total"
    assert sample(name, {"operation": "get_component_state", "result": "success"}) == 1
    assert sample(name, {"operation": "apply_changes", "result": "conflict"}) == 1


@respx.mock
async def test_readiness_project_check(backend: GitLabGitOpsBackend) -> None:
    respx.get(PROJECT).mock(side_effect=[httpx.Response(200, json={}), httpx.Response(401)])
    assert backend.projects() == ["infra/vm-values"]
    assert await backend.check_project("infra/vm-values") is True
    assert await backend.check_project("infra/vm-values") is False


@respx.mock
async def test_readiness_network_error_is_not_ready(backend: GitLabGitOpsBackend) -> None:
    respx.get(PROJECT).mock(side_effect=httpx.ConnectError("down"))
    assert await backend.check_project("infra/vm-values") is False


def _mr(iid: int, source_branch: str, description: str = "details") -> dict[str, Any]:
    return {
        "iid": iid,
        "web_url": f"{GITLAB}/mr/{iid}",
        "source_branch": source_branch,
        "target_branch": "main",
        "description": description,
    }


def _mock_mr_flow() -> tuple[respx.Route, respx.Route]:
    """Empty MR list, readable file, successful branch commit. Returns (commit, create)."""
    respx.get(FILE_URL).respond(200, json=file_body())
    commit = respx.post(COMMITS_URL).respond(201, json={"id": "abc"})
    create = respx.post(MRS_URL)
    return commit, create


@respx.mock
async def test_open_mr_for_same_target_blocks_a_second_one(backend: GitLabGitOpsBackend) -> None:
    other_change = _mr(
        5, "vm-autoscaler/ocp-prod-region-a-monitoring-storage-20261126T000000Z-aaaa"
    )
    respx.get(MRS_URL).respond(200, json=[other_change])
    commit = respx.post(COMMITS_URL)
    result = await backend.open_change_request(target(), CHANGES, REASON, {}, "idem-7")
    assert (result.change_id, result.message) == ("!5", TARGET_MR_OPEN_MESSAGE)
    assert commit.call_count == 0


@respx.mock
async def test_open_mr_for_another_namespace_does_not_block(backend: GitLabGitOpsBackend) -> None:
    other_namespace = _mr(5, "vm-autoscaler/ocp-prod-region-a-logging-storage-20261126T000000Z-a")
    respx.get(MRS_URL).respond(200, json=[other_namespace])
    commit, create = _mock_mr_flow()
    create.respond(201, json={"iid": 6, "web_url": f"{GITLAB}/mr/6"})
    result = await backend.open_change_request(target(), CHANGES, REASON, {}, "idem-7")
    assert result.change_id == "!6"
    assert commit.call_count == 1


@respx.mock
async def test_open_mr_list_is_paged(backend: GitLabGitOpsBackend) -> None:
    unrelated = [_mr(i, f"feature/{i}") for i in range(100)]
    ours = _mr(500, "vm-autoscaler/ocp-prod-region-a-monitoring-storage-20261126T000000Z-b")
    listing = respx.get(MRS_URL).mock(
        side_effect=[httpx.Response(200, json=unrelated), httpx.Response(200, json=[ours])]
    )
    result = await backend.open_change_request(target(), CHANGES, REASON, {}, "idem-7")
    assert result.change_id == "!500"
    assert [c.request.url.params["page"] for c in listing.calls] == ["1", "2"]


@respx.mock
async def test_mr_list_not_a_list_is_response_error(backend: GitLabGitOpsBackend) -> None:
    respx.get(MRS_URL).respond(200, json={"message": "odd"})
    with pytest.raises(GitOpsResponseError, match="merge request list"):
        await backend.open_change_request(target(), CHANGES, REASON, {}, "idem-7")


@respx.mock
async def test_mr_create_timeout_but_mr_exists_is_returned(backend: GitLabGitOpsBackend) -> None:
    respx.get(MRS_URL).mock(
        side_effect=[
            httpx.Response(200, json=[]),  # nothing open before we start
            httpx.Response(200, json=[_mr(77, BRANCH)]),  # lookup by branch after the timeout
        ]
    )
    _, create = _mock_mr_flow()
    create.mock(side_effect=httpx.ReadTimeout("slow"))
    delete = respx.delete(f"{PROJECT}/repository/branches/{quote(BRANCH, safe='')}")
    result = await backend.open_change_request(target(), CHANGES, REASON, {}, "idem-7")
    assert (result.change_id, result.url) == ("!77", f"{GITLAB}/mr/77")
    assert create.call_count == 1  # never retried
    assert delete.call_count == 0


@respx.mock
async def test_mr_create_timeout_and_no_mr_keeps_branch(backend: GitLabGitOpsBackend) -> None:
    lookup = respx.get(MRS_URL).respond(200, json=[])
    _, create = _mock_mr_flow()
    create.mock(side_effect=httpx.ReadTimeout("slow"))
    delete = respx.delete(f"{PROJECT}/repository/branches/{quote(BRANCH, safe='')}")
    with pytest.raises(GitOpsUnavailableError):
        await backend.open_change_request(target(), CHANGES, REASON, {}, "idem-7")
    assert lookup.calls.last.request.url.params["source_branch"] == BRANCH
    assert delete.call_count == 0


@respx.mock
async def test_mr_create_bad_response_is_looked_up_not_deleted(
    backend: GitLabGitOpsBackend,
) -> None:
    respx.get(MRS_URL).respond(200, json=[])
    _, create = _mock_mr_flow()
    create.respond(201, json=["not", "an", "mr"])
    delete = respx.delete(f"{PROJECT}/repository/branches/{quote(BRANCH, safe='')}")
    with pytest.raises(GitOpsResponseError, match="merge request response"):
        await backend.open_change_request(target(), CHANGES, REASON, {}, "idem-7")
    assert delete.call_count == 0


@respx.mock
@pytest.mark.parametrize("body", [["not", "a", "dict"], {"web_url": "u"}, {"id": None}])
async def test_bad_commit_response_is_response_error(
    backend: GitLabGitOpsBackend, metrics: Metrics, body: Any
) -> None:
    respx.get(FILE_URL).respond(200, json=file_body())
    respx.post(COMMITS_URL).respond(201, json=body)
    with pytest.raises(GitOpsResponseError, match="unexpected commit response"):
        await _apply(backend)
    assert (
        metrics.registry.get_sample_value(
            "vm_autoscaler_gitops_requests_total",
            {"operation": "apply_changes", "result": "bad_response"},
        )
        == 1
    )


@respx.mock
async def test_crlf_file_is_committed_with_crlf(backend: GitLabGitOpsBackend) -> None:
    crlf = VALUES.replace("\n", "\r\n")
    respx.get(FILE_URL).respond(200, json=file_body(crlf))
    commit = respx.post(COMMITS_URL).respond(201, json={"id": "abc"})
    no_annotations = GitLabGitOpsBackend(
        GITLAB,
        TOKEN,
        2.0,
        0,
        True,
        Metrics(),
        {TARGET_KEY: LOCATION},
        GitLabConfig(),
        clock=lambda: NOW,
        retry_wait_seconds=0,
    )
    try:
        await no_annotations.apply_changes(target(), CHANGES, REASON, {}, "c0ffee1", "idem-1")
    finally:
        await no_annotations.close()
    content = json.loads(commit.calls.last.request.content)["actions"][0]["content"]
    assert "\r\n" in content and "\n" not in content.replace("\r\n", "")
    assert changed_lines(crlf, content) == ["    size: 36Gi   # grown by vm-autoscaler"]


@pytest.mark.parametrize(
    ("value", "slug"),
    [("ocp-prod-region-a", "ocp-prod-region-a"), ("OCP Prod/A", "ocp-prod-a"), ("a..b", "a.b")],
)
def test_ref_slug(value: str, slug: str) -> None:
    assert ref_slug(value) == slug


def test_target_branch_prefix() -> None:
    assert target_branch_prefix(target()) == "vm-autoscaler/ocp-prod-region-a-monitoring-"
