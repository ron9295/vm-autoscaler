"""Domain models.

These models describe *our* view of the world. They are deliberately independent of
the Grafana payload format, the internal GitOps API wire format and the Argo CD API.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Only storage is scaled for now. CPU, memory and replica scaling were removed; they can be
# restored from git history (commit "Full version: cpu, memory, storage and replica scaling").
ResourceName = Literal["storage"]
RESOURCE_NAMES: tuple[ResourceName, ...] = ("storage",)


class Mode(StrEnum):
    """How a computed change is delivered. Ordered from safest to most automatic."""

    DRY_RUN = "dry_run"
    MERGE_REQUEST = "merge_request"
    DIRECT_COMMIT = "direct_commit"

    @property
    def rank(self) -> int:
        """Lower rank means safer."""
        return _MODE_RANK[self]


_MODE_RANK = {Mode.DRY_RUN: 0, Mode.MERGE_REQUEST: 1, Mode.DIRECT_COMMIT: 2}


def safest_mode(*modes: Mode | None) -> Mode:
    """Return the safest of the given modes (ignoring None)."""
    present = [mode for mode in modes if mode is not None]
    return min(present, key=lambda mode: mode.rank)


class Decision(StrEnum):
    """Outcome of one scale request."""

    APPLIED = "applied"
    CHANGE_REQUEST_OPENED = "change_request_opened"
    DRY_RUN = "dry_run"
    SKIPPED = "skipped"


class SkipReason(StrEnum):
    """Why a scale request did not lead to a change."""

    AUTOSCALE_DISABLED = "autoscale_disabled"
    COMPONENT_DISABLED = "component_disabled"
    UNKNOWN_TARGET = "unknown_target"
    NO_POLICY = "no_policy"
    VALUE_MISSING = "value_missing"
    BELOW_THRESHOLD = "below_threshold"
    COOLDOWN_ACTIVE = "cooldown_active"
    VALUE_NOT_SET = "value_not_set"
    INVALID_CURRENT_VALUE = "invalid_current_value"
    AT_MAX_CAP = "at_max_cap"
    EXPANSION_NOT_SUPPORTED = "expansion_not_supported"
    DEPLOYMENT_NOT_HEALTHY = "deployment_not_healthy"
    ARGOCD_UNAVAILABLE = "argocd_unavailable"
    GITOPS_CONFLICT = "gitops_conflict"
    GITOPS_UNAVAILABLE = "gitops_unavailable"
    GITOPS_TARGET_NOT_FOUND = "gitops_target_not_found"
    GITOPS_REJECTED = "gitops_rejected"
    GITOPS_ERROR = "gitops_error"
    DUPLICATE_IN_BATCH = "duplicate_in_batch"
    INTERNAL_ERROR = "internal_error"  # unexpected bug; logged with a traceback


class TargetKey(BaseModel):
    """Identifies one component instance: cluster + namespace + component."""

    model_config = ConfigDict(frozen=True)

    ocp_cluster: str
    namespace: str
    component: str

    def __str__(self) -> str:
        return f"{self.ocp_cluster}/{self.namespace}/{self.component}"


class SyncResource(BaseModel):
    """One Kubernetes object that Argo CD should sync (instead of the whole app)."""

    group: str
    kind: str
    name: str
    namespace: str | None = None


class Policy(BaseModel):
    """Effective scaling policy for one resource of one target."""

    trigger_percent: float = Field(gt=0, le=100)
    step_percent: float = Field(gt=0, le=200)
    max: str
    cooldown: timedelta
    mode: Mode | None = None


class Target(BaseModel):
    """A configured component that the autoscaler is allowed to change."""

    key: TargetKey
    argocd_app: str
    argocd_sync_resources: list[SyncResource] = Field(default_factory=list)
    gitops_ref: dict[str, Any] = Field(default_factory=dict)
    storage_expansion_supported: bool = True
    policies: dict[ResourceName, Policy]


class ScaleRequest(BaseModel):
    """One firing alert, translated into a request to scale one resource."""

    fingerprint: str
    alert_name: str
    target_key: TargetKey
    resource: ResourceName
    value: float | None
    autoscale: bool


class IgnoredAlert(BaseModel):
    """An alert in the webhook that we deliberately did not act on."""

    fingerprint: str
    status: str
    reason: str


class ParsedWebhook(BaseModel):
    """Result of parsing one Grafana webhook."""

    requests: list[ScaleRequest]
    ignored: list[IgnoredAlert]


class ComponentState(BaseModel):
    """Current resource values of a component, as stored in the Argo CD source repo."""

    storage: str | None = None
    annotations: dict[str, str] = Field(default_factory=dict)
    version: str | None = None  # optimistic-concurrency token (commit sha / etag)


StateField = Literal["storage"]


class ResourceChange(BaseModel):
    """One value to change in the component spec."""

    resource: ResourceName
    field: StateField
    yaml_path: str
    old: str
    new: str


class ChangeResult(BaseModel):
    """What the GitOps backend did with our change."""

    accepted: bool
    change_id: str | None = None  # commit sha or MR/PR id
    url: str | None = None  # link to commit / MR, if provided
    message: str | None = None


class AppStatus(BaseModel):
    """Health and sync state of an Argo CD application."""

    health: str
    sync: str
    operation_in_progress: bool = False

    @property
    def is_safe_to_change(self) -> bool:
        """True when the app is Healthy and no sync operation is running."""
        return self.health == "Healthy" and not self.operation_in_progress


class ScaleDecision(BaseModel):
    """What the service decided for one scale request. Returned to Grafana and logged."""

    fingerprint: str
    ocp_cluster: str
    namespace: str
    component: str
    resource: str
    value: float | None
    decision: Decision
    reason: str
    mode: Mode | None = None
    old: str | None = None
    new: str | None = None
    change_id: str | None = None
    url: str | None = None
    synced: bool = False
