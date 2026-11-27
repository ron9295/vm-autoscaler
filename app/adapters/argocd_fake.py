"""In-memory Argo CD stand-in for tests and local runs (`ARGOCD_SYNC_METHOD=fake`)."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.errors import ArgoCdError
from app.domain.models import AppStatus, SyncResource


@dataclass
class FakeArgoCdSyncer:
    """Returns a configurable status and records sync calls."""

    status: AppStatus = field(default_factory=lambda: AppStatus(health="Healthy", sync="Synced"))
    syncs: list[tuple[str, list[SyncResource]]] = field(default_factory=list)
    fail_sync: ArgoCdError | None = None
    fail_status: ArgoCdError | None = None

    async def get_status(self, app: str) -> AppStatus:
        """Return the configured status."""
        if self.fail_status is not None:
            raise self.fail_status
        return self.status

    async def sync(self, app: str, resources: list[SyncResource]) -> None:
        """Record the sync."""
        if self.fail_sync is not None:
            raise self.fail_sync
        self.syncs.append((app, list(resources)))

    async def close(self) -> None:
        """Nothing to close."""
