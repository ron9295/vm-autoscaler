"""Scaling strategies, one per resource type. Only storage exists for now.

A strategy knows:
* which `ComponentState` field it edits (and its YAML path, for backends that want one),
* how to compute the new value (grow by a step, round up, cap at max, never decrease),
* the most automatic delivery mode it allows.

CPU, memory and replica strategies were removed (storage-only scope). They can be restored
from git history: commit "Full version: cpu, memory, storage and replica scaling".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.domain.models import (
    ComponentState,
    Mode,
    Policy,
    ResourceChange,
    ResourceName,
    SkipReason,
    StateField,
    Target,
    safest_mode,
)
from app.domain.quantity import QuantityError, format_bytes, grow_bytes, parse_bytes


@dataclass(frozen=True)
class ComputeResult:
    """Either a list of changes, or a skip reason. The first change is the "headline" one."""

    changes: list[ResourceChange] = field(default_factory=list)
    skip_reason: SkipReason | None = None
    detail: str = ""

    @property
    def headline(self) -> ResourceChange | None:
        """The change to show in commit messages."""
        return self.changes[0] if self.changes else None


def skip(reason: SkipReason, detail: str = "") -> ComputeResult:
    """Shortcut for a ComputeResult that skips."""
    return ComputeResult(skip_reason=reason, detail=detail)


class ScalingStrategy(ABC):
    """Base class for all strategies."""

    resource: ResourceName
    max_mode: Mode = Mode.DIRECT_COMMIT

    @abstractmethod
    def compute(self, state: ComponentState, policy: Policy, target: Target) -> ComputeResult:
        """Compute the changes for this resource, or a skip reason."""

    def effective_mode(self, global_mode: Mode, policy: Policy) -> Mode:
        """The safest of the global mode, the policy mode and what this strategy allows."""
        return safest_mode(global_mode, policy.mode, self.max_mode)


class StorageStrategy(ScalingStrategy):
    """Storage: grow the PVC size by step_percent (rounded up to whole Gi). Never shrinks."""

    resource: ResourceName = "storage"

    def compute(self, state: ComponentState, policy: Policy, target: Target) -> ComputeResult:
        if not target.storage_expansion_supported:
            return skip(SkipReason.EXPANSION_NOT_SUPPORTED)
        if state.storage is None:
            return skip(SkipReason.VALUE_NOT_SET, "storage is not set")
        try:
            current = parse_bytes(state.storage)
            max_value = parse_bytes(policy.max)
        except QuantityError as exc:
            return skip(SkipReason.INVALID_CURRENT_VALUE, str(exc))
        if current >= max_value:
            return skip(SkipReason.AT_MAX_CAP, f"storage {state.storage} >= max {policy.max}")
        new_value = min(grow_bytes(current, policy.step_percent), max_value)
        return ComputeResult(
            changes=[
                ResourceChange(
                    resource="storage",
                    field="storage",
                    yaml_path=yaml_path(target.key.component, "storage"),
                    old=state.storage,
                    new=format_bytes(new_value),
                )
            ]
        )


# Path inside the VMCluster custom resource, used by the internal GitOps backend.
# The GitLab backend uses its own configurable path (gitlab.storage_path).
_YAML_PATHS: dict[StateField, str] = {
    "storage": "spec.{component}.storage.volumeClaimTemplate.spec.resources.requests.storage",
}


def yaml_path(component: str, state_field: StateField) -> str:
    """The VMCluster YAML path that holds this field for the given component."""
    return _YAML_PATHS[state_field].format(component=component)


def build_strategy_registry() -> dict[ResourceName, ScalingStrategy]:
    """Return a fresh `resource name -> strategy` registry."""
    strategies: list[ScalingStrategy] = [StorageStrategy()]
    return {strategy.resource: strategy for strategy in strategies}
