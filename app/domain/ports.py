"""Ports: the interfaces the service layer depends on.

Adapters in `app/adapters/` implement these. The service never imports an adapter.
"""

from __future__ import annotations

from typing import Protocol

from app.domain.models import (
    AppStatus,
    ChangeResult,
    ComponentState,
    ResourceChange,
    SyncResource,
    Target,
)


class GitOpsBackend(Protocol):
    """Reads and writes component values in the Argo CD source repo."""

    async def get_component_state(self, target: Target) -> ComponentState:
        """Return the current values. Safe to retry."""
        ...

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
        ...

    async def open_change_request(
        self,
        target: Target,
        changes: list[ResourceChange],
        reason: str,
        annotations: dict[str, str],
        idempotency_key: str,
    ) -> ChangeResult:
        """Open a merge/pull request that waits for human approval."""
        ...

    async def close(self) -> None:
        """Release connections."""
        ...


class DeploymentSyncer(Protocol):
    """Talks to Argo CD."""

    async def get_status(self, app: str) -> AppStatus:
        """Return health/sync status of the Argo CD application."""
        ...

    async def sync(self, app: str, resources: list[SyncResource]) -> None:
        """Sync the app, limited to the given resources, without pruning."""
        ...

    async def close(self) -> None:
        """Release connections."""
        ...
