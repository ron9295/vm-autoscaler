import json
import logging

import httpx
import pytest

from app.observability import JsonFormatter, redact, request_id_var
from app.readiness import ReadinessChecker


def _record(message: str, fields: dict[str, object] | None = None) -> logging.LogRecord:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, message, None, None)
    if fields is not None:
        record.fields = fields
    return record


def test_json_log_has_fields_and_request_id() -> None:
    token = request_id_var.set("req-1")
    try:
        line = JsonFormatter().format(
            _record("scale decision", {"resource": "storage", "old": "30Gi"})
        )
    finally:
        request_id_var.reset(token)
    entry = json.loads(line)
    assert entry["message"] == "scale decision"
    assert (entry["request_id"], entry["resource"], entry["old"]) == ("req-1", "storage", "30Gi")


def test_tokens_are_redacted() -> None:
    assert redact("Authorization: Bearer abc.def") == "Authorization: Bearer [REDACTED]"
    line = JsonFormatter().format(
        _record("sent Bearer s3cret", {"authorization": "Bearer s3cret", "note": "bearer s3cret"})
    )
    assert "s3cret" not in line


async def test_readiness_without_dependency_checks() -> None:
    assert await ReadinessChecker().check() == {"config": True}


async def test_readiness_checks_urls_and_caches() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "argocd" in str(request.url):
            raise httpx.ConnectError("down")
        return httpx.Response(401)  # any HTTP answer means reachable

    checker = ReadinessChecker(
        urls={"gitops_api": "https://gitops.test", "argocd": "https://argocd.test/api/version"},
        transport=httpx.MockTransport(handler),
    )
    assert await checker.check() == {"config": True, "gitops_api": True, "argocd": False}
    await checker.check()
    assert len(calls) == 2  # second call served from cache


def test_readiness_cache_fields_are_not_constructor_arguments() -> None:
    with pytest.raises(TypeError):
        ReadinessChecker(_cached={})  # type: ignore[call-arg]
    assert "_cached" not in repr(ReadinessChecker())
