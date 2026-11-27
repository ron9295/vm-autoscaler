"""Argo CD adapter using the REST API (`ARGOCD_SYNC_METHOD=api`, the default).

Needs a project-scoped token allowed only `applications, get` and `applications, sync`.
"""

from __future__ import annotations

import logging
import ssl
from typing import Any
from urllib.parse import quote

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.adapters.http_common import truncate
from app.domain.errors import ArgoCdError, ArgoCdUnavailableError
from app.domain.models import AppStatus, SyncResource

logger = logging.getLogger(__name__)

ALREADY_RUNNING_MARKER = "another operation is already in progress"
_IN_PROGRESS_PHASES = {"Running", "Terminating"}


class ArgoCdApiSyncer:
    """Get status and sync Argo CD applications over the REST API."""

    def __init__(
        self,
        base_url: str,
        token: str | None,
        timeout: float,
        verify: ssl.SSLContext | bool,
        *,
        status_retries: int = 2,
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
        self._status_retries = status_retries
        self._retry_wait = retry_wait_seconds

    async def get_status(self, app: str) -> AppStatus:
        """GET /api/v1/applications/{app}, retried briefly on network errors."""
        path = f"/api/v1/applications/{quote(app, safe='')}"
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._status_retries + 1),
            wait=wait_exponential(multiplier=self._retry_wait, max=5),
            retry=retry_if_exception_type(ArgoCdUnavailableError),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                response = await self._send("GET", path)
        _raise_for_status(response, app)
        return parse_app_status(_json(response))

    async def sync(self, app: str, resources: list[SyncResource]) -> None:
        """Refresh the app, then sync it limited to `resources`, never pruning.

        The refresh (GET ...?refresh=normal, needs only `applications, get`) makes Argo CD
        re-read the Git repo first, so the sync applies the commit we just made rather than
        a cached older revision. No revision is pinned: pinning could roll back a newer
        commit that landed in the meantime.

        "Another operation is already in progress" counts as success: an (auto-)sync
        is already rolling out the latest commit.
        """
        app_path = f"/api/v1/applications/{quote(app, safe='')}"
        refreshed = await self._send("GET", app_path, params={"refresh": "normal"})
        _raise_for_status(refreshed, app)
        path = f"{app_path}/sync"
        body: dict[str, Any] = {"prune": False, "dryRun": False}
        if resources:
            body["resources"] = [resource.model_dump(exclude_none=True) for resource in resources]
        response = await self._send("POST", path, json=body)
        if response.status_code >= 400 and ALREADY_RUNNING_MARKER in response.text.lower():
            logger.info(
                "argo cd sync already in progress, treating as success",
                extra={"fields": {"argocd_app": app}},
            )
            return
        _raise_for_status(response, app)
        logger.info("argo cd sync started", extra={"fields": {"argocd_app": app}})

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise ArgoCdUnavailableError(f"{method} {path}: {type(exc).__name__}") from exc
        logger.info(
            "argo cd request",
            extra={"fields": {"method": method, "path": path, "status": response.status_code}},
        )
        if response.status_code >= 500 and ALREADY_RUNNING_MARKER not in response.text.lower():
            raise ArgoCdUnavailableError(f"{method} {path}: HTTP {response.status_code}")
        return response


def parse_app_status(body: Any) -> AppStatus:
    """Read health, sync and operation phase from an Argo CD Application object."""
    if not isinstance(body, dict):
        raise ArgoCdError("unexpected application response")
    status = body.get("status") or {}
    phase = (status.get("operationState") or {}).get("phase")
    return AppStatus(
        health=(status.get("health") or {}).get("status", "Unknown"),
        sync=(status.get("sync") or {}).get("status", "Unknown"),
        operation_in_progress=phase in _IN_PROGRESS_PHASES or body.get("operation") is not None,
    )


def _raise_for_status(response: httpx.Response, app: str) -> None:
    if response.status_code >= 400:
        raise ArgoCdError(
            f"argo cd app {app}: HTTP {response.status_code}: {truncate(response.text)}"
        )


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise ArgoCdError("argo cd response is not valid JSON") from exc
