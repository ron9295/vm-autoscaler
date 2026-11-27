"""HTTP client for our internal backend that edits the Argo CD source repo.

TODO(api): the API contract is not final. Everything that depends on the wire format
lives in the `_PATH_*` constants and the `_to_*` / `_from_*` mapper functions at the
bottom of this file. Fill those in; nothing else in the service has to change.

Retry rules (no double commits):
* GET is retried on connection errors, timeouts and 5xx (exponential backoff + jitter).
* POST is retried only when the request provably never left this process
  (connect error / connect timeout / pool timeout). A 5xx or read timeout on a write is
  NOT retried, because the backend may already have committed.
* Writes carry an `Idempotency-Key` header so the backend can dedupe later.
"""

from __future__ import annotations

import logging
import ssl
import time
from collections.abc import Callable
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.adapters.http_common import truncate
from app.domain.errors import (
    GitOpsConflictError,
    GitOpsError,
    GitOpsNotFoundError,
    GitOpsRejectedError,
    GitOpsResponseError,
    GitOpsUnavailableError,
)
from app.domain.models import ChangeResult, ComponentState, ResourceChange, Target
from app.observability import Metrics

logger = logging.getLogger(__name__)

T = TypeVar("T")

# TODO(api): replace with the real paths once the backend API is designed.
_PATH_STATE = "/TODO/components"
_PATH_APPLY = "/TODO/components/changes"
_PATH_CHANGE_REQUEST = "/TODO/change-requests"

# Errors where the request was never sent, so even a write is safe to retry.
_NOT_SENT_ERRORS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)


class _ServerError(Exception):
    """Internal marker: the backend answered with a 5xx (retryable for GET only)."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"HTTP {response.status_code}")


class InternalGitOpsClient:
    """HTTP client for the internal backend that edits the Argo CD source repo.

    TODO(api): paths and payloads below are placeholders. Update the
    `_to_*_payload` / `_from_*_response` mappers once the API contract is final.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None,
        timeout: float,
        max_retries: int,
        verify: ssl.SSLContext | bool,
        metrics: Metrics,
        *,
        retry_wait_seconds: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Accept": "application/json", "User-Agent": "vm-autoscaler"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout),
            verify=verify,
            transport=transport,
        )
        self._max_retries = max_retries
        self._retry_wait = retry_wait_seconds
        self._metrics = metrics

    # ----------------------------------------------------------------------------------
    # Public API (the GitOpsBackend port)
    # ----------------------------------------------------------------------------------

    async def get_component_state(self, target: Target) -> ComponentState:
        """Read the current resource values of the target."""
        # TODO(api): GET {base}/TODO/components?cluster=..&namespace=..&component=..
        return await self._call(
            "get_component_state",
            "GET",
            _PATH_STATE,
            _from_state_response,
            params=_to_state_params(target),
        )

    async def apply_changes(
        self,
        target: Target,
        changes: list[ResourceChange],
        reason: str,
        annotations: dict[str, str],
        expected_version: str | None,
        idempotency_key: str,
    ) -> ChangeResult:
        """Commit the changes directly to the Argo CD source repo."""
        # TODO(api): POST/PATCH {base}/TODO/components/... with changes + annotations + reason
        payload = _to_apply_payload(target, changes, reason, annotations, expected_version)
        return await self._call(
            "apply_changes",
            "POST",
            _PATH_APPLY,
            _from_apply_response,
            json=payload,
            idempotency_key=idempotency_key,
        )

    async def open_change_request(
        self,
        target: Target,
        changes: list[ResourceChange],
        reason: str,
        annotations: dict[str, str],
        idempotency_key: str,
    ) -> ChangeResult:
        """Open a merge/pull request that waits for human approval."""
        # TODO(api): POST {base}/TODO/change-requests
        payload = _to_change_request_payload(target, changes, reason, annotations)
        return await self._call(
            "open_change_request",
            "POST",
            _PATH_CHANGE_REQUEST,
            _from_change_request_response,
            json=payload,
            idempotency_key=idempotency_key,
        )

    async def close(self) -> None:
        """Close the shared HTTP client."""
        await self._http.aclose()

    # ----------------------------------------------------------------------------------
    # HTTP plumbing
    # ----------------------------------------------------------------------------------

    async def _call(
        self,
        operation: str,
        method: str,
        path: str,
        mapper: Callable[[Any], T],
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> T:
        """Send one request with the retry rules, map errors, map the JSON body."""
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        started = time.monotonic()
        attempts = 0
        status: int | None = None
        result = "success"
        try:
            response, attempts = await self._send_with_retries(
                method, path, params=params, json=json, headers=headers
            )
            status = response.status_code
            _raise_for_status(response)
            return _map_body(operation, mapper, _parse_json(response))
        except GitOpsError as exc:
            result = _result_label(exc)
            raise
        finally:
            duration = time.monotonic() - started
            self._metrics.gitops_requests.labels(operation=operation, result=result).inc()
            self._metrics.gitops_duration.labels(operation=operation).observe(duration)
            logger.info(
                "gitops request",
                extra={
                    "fields": {
                        "operation": operation,
                        "method": method,
                        "path": path,
                        "status": status,
                        "result": result,
                        "duration_seconds": round(duration, 3),
                        "retries": max(attempts - 1, 0),
                    }
                },
            )

    async def _send_with_retries(
        self, method: str, path: str, **kwargs: Any
    ) -> tuple[httpx.Response, int]:
        """Send with tenacity. Returns (response, attempts). Raises GitOpsUnavailableError."""
        is_read = method == "GET"
        retry_on: tuple[type[Exception], ...] = (
            (httpx.TransportError, _ServerError) if is_read else _NOT_SENT_ERRORS
        )
        attempts = 0
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._max_retries + 1),
            wait=wait_exponential_jitter(
                initial=self._retry_wait, max=self._retry_wait * 20, jitter=self._retry_wait
            ),
            retry=retry_if_exception_type(retry_on),
            reraise=True,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    attempts += 1
                    response = await self._http.request(method, path, **kwargs)
                    if response.status_code >= 500:
                        raise _ServerError(response)
                    return response, attempts
        except _ServerError as exc:
            raise GitOpsUnavailableError(
                f"{method} {path}: HTTP {exc.response.status_code} after {attempts} attempt(s): "
                f"{truncate(exc.response.text)}"
            ) from exc
        except httpx.TransportError as exc:  # includes all timeouts
            raise GitOpsUnavailableError(
                f"{method} {path}: {type(exc).__name__} after {attempts} attempt(s)"
            ) from exc
        raise GitOpsUnavailableError(f"{method} {path}: no attempt made")  # pragma: no cover


def _map_body(operation: str, mapper: Callable[[Any], T], body: Any) -> T:
    """Run a response mapper; turn validation problems into GitOpsResponseError."""
    try:
        return mapper(body)
    except (ValidationError, KeyError, TypeError, ValueError) as exc:
        raise GitOpsResponseError(f"{operation}: unexpected response shape: {exc}") from exc


def _raise_for_status(response: httpx.Response) -> None:
    status = response.status_code
    if status < 400:
        return
    body = truncate(response.text)
    if status == 409:
        raise GitOpsConflictError(f"conflict: {body}")
    if status == 404:
        raise GitOpsNotFoundError(f"not found: {body}")
    raise GitOpsRejectedError(status, body)


def _parse_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise GitOpsResponseError(f"response is not valid JSON: {truncate(response.text)}") from exc


def _result_label(exc: GitOpsError) -> str:
    labels: dict[type[GitOpsError], str] = {
        GitOpsUnavailableError: "unavailable",
        GitOpsConflictError: "conflict",
        GitOpsNotFoundError: "not_found",
        GitOpsRejectedError: "rejected",
        GitOpsResponseError: "bad_response",
    }
    return labels.get(type(exc), "error")


# ======================================================================================
# Wire-format mappers. TODO(api): this is the ONLY place that knows the API's JSON shape.
# ======================================================================================


def _to_state_params(target: Target) -> dict[str, str]:
    """TODO(api): query parameters that identify a component in the backend."""
    params = {
        "cluster": target.key.ocp_cluster,
        "namespace": target.key.namespace,
        "component": target.key.component,
    }
    # gitops_ref from config (app id, repo key, ...) is passed through as-is.
    params.update({key: str(value) for key, value in target.gitops_ref.items()})
    return params


class _RequestLimitWire(BaseModel):
    model_config = ConfigDict(extra="ignore")
    request: str | None = None
    limit: str | None = None


class _StateResponseWire(BaseModel):
    """TODO(api): placeholder response shape for GET component state:

    {"cpu": {"request": "2", "limit": "4"}, "memory": {"request": "16Gi", "limit": "16Gi"},
     "storage": "500Gi", "replicas": 3, "annotations": {...}, "version": "<commit sha>"}
    """

    model_config = ConfigDict(extra="ignore")
    cpu: _RequestLimitWire = _RequestLimitWire()
    memory: _RequestLimitWire = _RequestLimitWire()
    storage: str | None = None
    replicas: int | None = None
    annotations: dict[str, str] = {}
    version: str | None = None


def _from_state_response(body: Any) -> ComponentState:
    """TODO(api): map the GET response to ComponentState."""
    wire = _StateResponseWire.model_validate(body)
    return ComponentState(
        cpu_request=wire.cpu.request,
        cpu_limit=wire.cpu.limit,
        memory_request=wire.memory.request,
        memory_limit=wire.memory.limit,
        storage=wire.storage,
        replicas=wire.replicas,
        annotations=wire.annotations,
        version=wire.version,
    )


def _changes_to_wire(changes: list[ResourceChange]) -> list[dict[str, str]]:
    """TODO(api): how one field change is described to the backend."""
    return [{"path": c.yaml_path, "field": c.field, "old": c.old, "new": c.new} for c in changes]


def _to_apply_payload(
    target: Target,
    changes: list[ResourceChange],
    reason: str,
    annotations: dict[str, str],
    expected_version: str | None,
) -> dict[str, Any]:
    """TODO(api): placeholder body for a direct commit."""
    return {
        "target": _to_state_params(target),
        "changes": _changes_to_wire(changes),
        "annotations": annotations,
        "commit_message": reason,
        "expected_version": expected_version,
    }


def _to_change_request_payload(
    target: Target, changes: list[ResourceChange], reason: str, annotations: dict[str, str]
) -> dict[str, Any]:
    """TODO(api): placeholder body for opening a merge/pull request."""
    title, _, description = reason.partition("\n\n")
    return {
        "target": _to_state_params(target),
        "changes": _changes_to_wire(changes),
        "annotations": annotations,
        "title": title,
        "description": description,
    }


class _WriteResponseWire(BaseModel):
    """TODO(api): placeholder response shape for writes:

    {"accepted": true, "id": "<commit sha or MR id>", "url": "https://...", "message": "..."}
    """

    model_config = ConfigDict(extra="ignore")
    accepted: bool
    id: str | None = None
    url: str | None = None
    message: str | None = None


def _from_apply_response(body: Any) -> ChangeResult:
    """TODO(api): map the commit response to ChangeResult."""
    wire = _WriteResponseWire.model_validate(body)
    return ChangeResult(
        accepted=wire.accepted, change_id=wire.id, url=wire.url, message=wire.message
    )


def _from_change_request_response(body: Any) -> ChangeResult:
    """TODO(api): map the merge/pull request response to ChangeResult."""
    wire = _WriteResponseWire.model_validate(body)
    return ChangeResult(
        accepted=wire.accepted, change_id=wire.id, url=wire.url, message=wire.message
    )
