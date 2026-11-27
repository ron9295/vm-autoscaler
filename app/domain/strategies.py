"""One scaling strategy per resource type.

A strategy knows:
* which fields of `ComponentState` (and which YAML paths in the VMCluster spec) it edits,
* how to compute the new value (grow by a step, round up, cap at max, never decrease),
* the most automatic delivery mode it allows (replicas: merge_request only).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

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
from app.domain.quantity import (
    QuantityError,
    format_bytes,
    format_cpu,
    grow_bytes,
    grow_cpu,
    parse_bytes,
    parse_cpu,
)


@dataclass(frozen=True)
class ComputeResult:
    """Either a list of changes, or a skip reason. The first change is the "headline" one."""

    changes: list[ResourceChange] = field(default_factory=list)
    skip_reason: SkipReason | None = None
    detail: str = ""

    @property
    def headline(self) -> ResourceChange | None:
        """The change to show in commit messages (e.g. the CPU limit)."""
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


# --------------------------------------------------------------------------------------
# CPU and memory: a request and a limit that grow together
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class QuantityKind:
    """How to parse, grow and format one kind of quantity (CPU cores or bytes)."""

    parse: Callable[[str], Decimal | int]
    grow: Callable[[Decimal | int, float], Decimal | int]
    format: Callable[[Decimal | int], str]


def _grow_cpu(value: Decimal | int, step_percent: float) -> Decimal:
    return grow_cpu(Decimal(value), step_percent)


def _format_cpu(value: Decimal | int) -> str:
    return format_cpu(Decimal(value))


def _grow_bytes(value: Decimal | int, step_percent: float) -> int:
    return grow_bytes(int(value), step_percent)


def _format_bytes(value: Decimal | int) -> str:
    return format_bytes(int(value))


CPU_KIND = QuantityKind(parse=parse_cpu, grow=_grow_cpu, format=_format_cpu)
BYTES_KIND = QuantityKind(parse=parse_bytes, grow=_grow_bytes, format=_format_bytes)


class RequestLimitStrategy(ScalingStrategy):
    """Shared logic for CPU and memory: grow the limit, then adjust the request.

    The alert value is "percent of limit", so the limit is what we grow.
    """

    kind: QuantityKind
    request_field: StateField
    limit_field: StateField

    def compute(self, state: ComponentState, policy: Policy, target: Target) -> ComputeResult:
        current_limit_text = getattr(state, self.limit_field)
        if current_limit_text is None:
            return skip(SkipReason.VALUE_NOT_SET, f"{self.limit_field} is not set")
        try:
            current_limit = self.kind.parse(current_limit_text)
            max_value = self.kind.parse(policy.max)
        except QuantityError as exc:
            return skip(SkipReason.INVALID_CURRENT_VALUE, str(exc))

        if current_limit >= max_value:
            return skip(SkipReason.AT_MAX_CAP, f"{self.limit_field} {current_limit_text} >= max")

        assert policy.step_percent is not None  # guaranteed by config validation
        new_limit = min(self.kind.grow(current_limit, policy.step_percent), max_value)
        component = target.key.component
        changes = [
            self._change(
                self.limit_field, current_limit_text, self.kind.format(new_limit), component
            )
        ]

        request_change = self._request_change(state, policy, new_limit, component)
        if request_change is not None:
            changes.append(request_change)
        return ComputeResult(changes=changes)

    def _request_change(
        self, state: ComponentState, policy: Policy, new_limit: Decimal | int, component: str
    ) -> ResourceChange | None:
        current_text = getattr(state, self.request_field)
        if current_text is None:
            return None  # never invent a request; only the limit changes
        current = self.kind.parse(current_text)
        if policy.requests_equal_limits:
            target_value = new_limit
        elif policy.scale_requests_with_limits:
            assert policy.step_percent is not None
            target_value = min(self.kind.grow(current, policy.step_percent), new_limit)
        else:
            return None
        new_value = max(current, target_value)  # never decrease
        if new_value == current:
            return None
        return self._change(
            self.request_field, current_text, self.kind.format(new_value), component
        )

    def _change(
        self, state_field: StateField, old: str, new: str, component: str
    ) -> ResourceChange:
        return ResourceChange(
            resource=self.resource,
            field=state_field,
            yaml_path=yaml_path(component, state_field),
            old=old,
            new=new,
        )


class CpuStrategy(RequestLimitStrategy):
    """CPU: grow the limit by step_percent (rounded up to 100m), optionally the request too."""

    resource: ResourceName = "cpu"
    kind = CPU_KIND
    request_field: StateField = "cpu_request"
    limit_field: StateField = "cpu_limit"


class MemoryStrategy(RequestLimitStrategy):
    """Memory: grow the limit by step_percent (rounded up to whole Gi).

    With `requests_equal_limits` the request is set to the new limit, so the pod has
    guaranteed memory (no OOM surprises for a stateful database).
    """

    resource: ResourceName = "memory"
    kind = BYTES_KIND
    request_field: StateField = "memory_request"
    limit_field: StateField = "memory_limit"


# --------------------------------------------------------------------------------------
# Storage and replicas
# --------------------------------------------------------------------------------------


class StorageStrategy(ScalingStrategy):
    """Storage: grow the PVC request by step_percent (rounded up to whole Gi). Never shrinks."""

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
        assert policy.step_percent is not None
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


class ReplicaStrategy(ScalingStrategy):
    """Replicas: add `step` nodes, capped at max. Only ever opens a merge request."""

    resource: ResourceName = "replicas"
    max_mode = Mode.MERGE_REQUEST

    def compute(self, state: ComponentState, policy: Policy, target: Target) -> ComputeResult:
        if state.replicas is None:
            return skip(SkipReason.VALUE_NOT_SET, "replicas is not set")
        max_value = int(policy.max)
        if state.replicas >= max_value:
            return skip(SkipReason.AT_MAX_CAP, f"replicas {state.replicas} >= max {max_value}")
        assert policy.step is not None
        new_value = min(state.replicas + policy.step, max_value)
        return ComputeResult(
            changes=[
                ResourceChange(
                    resource="replicas",
                    field="replicas",
                    yaml_path=yaml_path(target.key.component, "replicas"),
                    old=str(state.replicas),
                    new=str(new_value),
                )
            ]
        )


# --------------------------------------------------------------------------------------
# YAML paths and registry
# --------------------------------------------------------------------------------------

# Paths inside the VMCluster custom resource. {component} is vmstorage / vmselect / vminsert.
_YAML_PATHS: dict[StateField, str] = {
    "cpu_request": "spec.{component}.resources.requests.cpu",
    "cpu_limit": "spec.{component}.resources.limits.cpu",
    "memory_request": "spec.{component}.resources.requests.memory",
    "memory_limit": "spec.{component}.resources.limits.memory",
    "storage": "spec.{component}.storage.volumeClaimTemplate.spec.resources.requests.storage",
    "replicas": "spec.{component}.replicaCount",
}


def yaml_path(component: str, state_field: StateField) -> str:
    """The VMCluster YAML path that holds this field for the given component."""
    return _YAML_PATHS[state_field].format(component=component)


def build_strategy_registry() -> dict[ResourceName, ScalingStrategy]:
    """Return a fresh `resource name -> strategy` registry."""
    strategies: list[ScalingStrategy] = [
        CpuStrategy(),
        MemoryStrategy(),
        StorageStrategy(),
        ReplicaStrategy(),
    ]
    return {strategy.resource: strategy for strategy in strategies}
