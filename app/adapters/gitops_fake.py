"""In-memory GitOps backend for tests and local runs (`GITOPS_BACKEND=fake`)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.domain.errors import GitOpsConflictError, GitOpsError, GitOpsNotFoundError
from app.domain.models import ChangeResult, ComponentState, ResourceChange, Target, TargetKey

logger = logging.getLogger(__name__)


@dataclass
class RecordedChange:
    """One write the fake received, so tests can assert on it."""

    target: TargetKey
    changes: list[ResourceChange]
    reason: str
    annotations: dict[str, str]
    idempotency_key: str


@dataclass
class FakeGitOpsBackend:
    """Keeps component state in a dict. Can be told to fail the next N calls of an operation."""

    states: dict[TargetKey, ComponentState] = field(default_factory=dict)
    applied: list[RecordedChange] = field(default_factory=list)
    change_requests: list[RecordedChange] = field(default_factory=list)
    _failures: dict[str, list[GitOpsError]] = field(default_factory=dict)
    _version_counter: int = 0

    @classmethod
    def from_yaml(cls, path: Path) -> FakeGitOpsBackend:
        """Seed from a YAML list of {ocp_cluster, namespace, component, state: {...}}."""
        backend = cls()
        entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        for entry in entries:
            key = TargetKey(
                ocp_cluster=entry["ocp_cluster"],
                namespace=entry["namespace"],
                component=entry["component"],
            )
            backend.seed(key, ComponentState.model_validate(entry.get("state", {})))
        return backend

    def seed(self, key: TargetKey, state: ComponentState) -> None:
        """Set the state of one target."""
        self.states[key] = state.model_copy(deep=True)

    def fail_next(self, operation: str, error: GitOpsError, times: int = 1) -> None:
        """Make the next `times` calls of `operation` raise `error`."""
        self._failures.setdefault(operation, []).extend([error] * times)

    def _maybe_fail(self, operation: str) -> None:
        pending = self._failures.get(operation)
        if pending:
            raise pending.pop(0)

    def _get(self, key: TargetKey) -> ComponentState:
        if key not in self.states:
            raise GitOpsNotFoundError(f"fake backend has no state for {key}")
        return self.states[key]

    async def get_component_state(self, target: Target) -> ComponentState:
        """Return a copy of the stored state."""
        self._maybe_fail("get_component_state")
        return self._get(target.key).model_copy(deep=True)

    async def apply_changes(
        self,
        target: Target,
        changes: list[ResourceChange],
        reason: str,
        annotations: dict[str, str],
        expected_version: str | None,
        idempotency_key: str,
    ) -> ChangeResult:
        """Apply changes in memory, checking the optimistic-concurrency version."""
        self._maybe_fail("apply_changes")
        current = self._get(target.key)
        if expected_version is not None and current.version != expected_version:
            raise GitOpsConflictError(
                f"version mismatch: expected {expected_version}, have {current.version}"
            )
        updated = current.model_copy(deep=True)
        for change in changes:
            setattr(updated, change.field, change.new)
        updated.annotations.update(annotations)
        self._version_counter += 1
        updated.version = f"fake-{self._version_counter}"
        self.states[target.key] = updated
        self.applied.append(
            RecordedChange(target.key, list(changes), reason, dict(annotations), idempotency_key)
        )
        logger.info("fake backend applied changes", extra={"fields": {"reason": reason}})
        return ChangeResult(accepted=True, change_id=updated.version, message="applied in memory")

    async def open_change_request(
        self,
        target: Target,
        changes: list[ResourceChange],
        reason: str,
        annotations: dict[str, str],
        idempotency_key: str,
    ) -> ChangeResult:
        """Record the change request; state is not changed (it waits for approval)."""
        self._maybe_fail("open_change_request")
        self._get(target.key)
        self.change_requests.append(
            RecordedChange(target.key, list(changes), reason, dict(annotations), idempotency_key)
        )
        number = len(self.change_requests)
        return ChangeResult(
            accepted=True, change_id=f"fake-mr-{number}", url=f"https://fake.invalid/mr/{number}"
        )

    async def close(self) -> None:
        """Nothing to close."""
