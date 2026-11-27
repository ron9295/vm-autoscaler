import json
import logging

import httpx
import pytest
import respx

from app.adapters.gitops_internal import InternalGitOpsClient
from app.domain.errors import (
    GitOpsConflictError,
    GitOpsNotFoundError,
    GitOpsRejectedError,
    GitOpsResponseError,
    GitOpsUnavailableError,
)
from app.domain.models import ResourceChange
from app.observability import JsonFormatter, Metrics
from tests.factories import target

BASE = "https://gitops.example.internal"
TOKEN = "super-secret-token-value"
STATE_URL = f"{BASE}/TODO/components"
APPLY_URL = f"{BASE}/TODO/components/changes"
MR_URL = f"{BASE}/TODO/change-requests"

STATE_BODY = {
    "cpu": {"request": "2", "limit": "4"},
    "memory": {"request": "16Gi", "limit": "16Gi"},
    "storage": "500Gi",
    "replicas": 3,
    "annotations": {"vm-autoscaler/last-scaled-cpu": "2026-11-01T00:00:00Z"},
    "version": "abc123",
}
CHANGES = [
    ResourceChange(
        resource="cpu",
        field="cpu_limit",
        yaml_path="spec.vmstorage.resources.limits.cpu",
        old="4",
        new="5",
    )
]


@pytest.fixture
def metrics() -> Metrics:
    return Metrics()


@pytest.fixture
async def client(metrics: Metrics):  # type: ignore[no-untyped-def]
    c = InternalGitOpsClient(BASE, TOKEN, 2.0, 3, True, metrics, retry_wait_seconds=0)
    yield c
    await c.close()


async def _apply(client: InternalGitOpsClient):  # type: ignore[no-untyped-def]
    return await client.apply_changes(target(), CHANGES, "reason", {}, "abc123", "key-1")


@respx.mock
async def test_get_state_maps_response(client: InternalGitOpsClient) -> None:
    route = respx.get(STATE_URL).respond(200, json=STATE_BODY)
    state = await client.get_component_state(target())
    assert (state.cpu_limit, state.memory_request, state.replicas, state.version) == (
        "4",
        "16Gi",
        3,
        "abc123",
    )
    request = route.calls.last.request
    assert request.url.params["cluster"] == "ocp-prod-region-a"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"


@respx.mock
async def test_get_retried_on_503_then_succeeds(
    client: InternalGitOpsClient, metrics: Metrics
) -> None:
    route = respx.get(STATE_URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(503), httpx.Response(200, json=STATE_BODY)]
    )
    await client.get_component_state(target())
    assert route.call_count == 3
    assert (
        metrics.registry.get_sample_value(
            "vm_autoscaler_gitops_requests_total",
            {"operation": "get_component_state", "result": "success"},
        )
        == 1
    )


@respx.mock
async def test_get_gives_up_after_max_retries(client: InternalGitOpsClient) -> None:
    route = respx.get(STATE_URL).respond(503)
    with pytest.raises(GitOpsUnavailableError):
        await client.get_component_state(target())
    assert route.call_count == 4  # 1 try + 3 retries


@respx.mock
async def test_timeout_raises_unavailable(client: InternalGitOpsClient) -> None:
    respx.get(STATE_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(GitOpsUnavailableError, match="ReadTimeout"):
        await client.get_component_state(target())


@respx.mock
async def test_409_raises_conflict(client: InternalGitOpsClient) -> None:
    respx.post(APPLY_URL).respond(409, json={"error": "stale version"})
    with pytest.raises(GitOpsConflictError):
        await _apply(client)


@respx.mock
async def test_404_raises_not_found(client: InternalGitOpsClient) -> None:
    respx.get(STATE_URL).respond(404)
    with pytest.raises(GitOpsNotFoundError):
        await client.get_component_state(target())


@respx.mock
async def test_400_raises_rejected_with_truncated_body(client: InternalGitOpsClient) -> None:
    respx.post(APPLY_URL).respond(400, text="x" * 2000)
    with pytest.raises(GitOpsRejectedError) as info:
        await _apply(client)
    assert info.value.status_code == 400
    assert "HTTP 400" in str(info.value)
    assert len(info.value.body) <= 500 + len("...[truncated]")


@respx.mock
async def test_bad_json_raises_response_error(client: InternalGitOpsClient) -> None:
    respx.get(STATE_URL).respond(200, text="<html>not json</html>")
    with pytest.raises(GitOpsResponseError):
        await client.get_component_state(target())


@respx.mock
async def test_wrong_shape_raises_response_error(client: InternalGitOpsClient) -> None:
    respx.get(STATE_URL).respond(200, json={"replicas": "many"})
    with pytest.raises(GitOpsResponseError):
        await client.get_component_state(target())


@respx.mock
async def test_post_not_retried_after_5xx(client: InternalGitOpsClient) -> None:
    route = respx.post(APPLY_URL).respond(502)
    with pytest.raises(GitOpsUnavailableError):
        await _apply(client)
    assert route.call_count == 1


@respx.mock
async def test_post_not_retried_after_read_timeout(client: InternalGitOpsClient) -> None:
    route = respx.post(APPLY_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(GitOpsUnavailableError):
        await _apply(client)
    assert route.call_count == 1


@respx.mock
async def test_post_retried_on_connect_error(client: InternalGitOpsClient) -> None:
    route = respx.post(APPLY_URL).mock(
        side_effect=[httpx.ConnectError("refused"), httpx.Response(200, json={"accepted": True})]
    )
    result = await _apply(client)
    assert result.accepted
    assert route.call_count == 2


@respx.mock
async def test_writes_send_idempotency_key_and_payload(client: InternalGitOpsClient) -> None:
    apply_route = respx.post(APPLY_URL).respond(200, json={"accepted": True, "id": "sha1"})
    mr_route = respx.post(MR_URL).respond(200, json={"accepted": True, "id": "42", "url": "u"})
    result = await _apply(client)
    assert result.change_id == "sha1"
    mr = await client.open_change_request(target(), CHANGES, "title\n\nbody", {}, "key-2")
    assert (mr.change_id, mr.url) == ("42", "u")
    assert apply_route.calls.last.request.headers["Idempotency-Key"] == "key-1"
    assert mr_route.calls.last.request.headers["Idempotency-Key"] == "key-2"
    body = json.loads(apply_route.calls.last.request.content)
    assert body["expected_version"] == "abc123"
    assert body["changes"][0]["new"] == "5"


@respx.mock
async def test_token_never_logged(
    client: InternalGitOpsClient, caplog: pytest.LogCaptureFixture
) -> None:
    respx.get(STATE_URL).respond(503)
    respx.post(APPLY_URL).respond(400, text=f"echo Authorization: Bearer {TOKEN}")
    caplog.set_level(logging.DEBUG)
    with pytest.raises(GitOpsUnavailableError):
        await client.get_component_state(target())
    with pytest.raises(GitOpsRejectedError):
        await _apply(client)
    formatter = JsonFormatter()
    rendered = "\n".join(formatter.format(record) for record in caplog.records)
    assert "gitops request" in rendered
    assert TOKEN not in rendered
