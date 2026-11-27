"""The scaling service: turns parsed alerts into safe GitOps changes and Argo CD syncs.

Flow per target (cluster + namespace + component):

    pre-checks -> lock -> read state -> cooldown -> compute -> deliver by mode -> sync -> record

No HTTP details live here; only calls to the `GitOpsBackend` and `DeploymentSyncer` ports.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.config import AppConfig
from app.domain.errors import (
    ArgoCdError,
    GitOpsConflictError,
    GitOpsError,
    GitOpsNotFoundError,
    GitOpsRejectedError,
    GitOpsUnavailableError,
)
from app.domain.models import (
    COOLDOWN_ANNOTATION_PREFIX,
    ChangeResult,
    ComponentState,
    Decision,
    Mode,
    OutcomeReason,
    ParsedWebhook,
    Policy,
    ResourceChange,
    ResourceName,
    ScaleDecision,
    ScaleRequest,
    SkipReason,
    Target,
    TargetKey,
)
from app.domain.ports import DeploymentSyncer, GitOpsBackend
from app.domain.strategies import ComputeResult, ScalingStrategy, build_strategy_registry
from app.observability import Metrics

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]


def group_by_target(requests: list[ScaleRequest]) -> dict[TargetKey, list[ScaleRequest]]:
    """Group requests so all resources of one target end up in one commit and one sync."""
    groups: dict[TargetKey, list[ScaleRequest]] = {}
    for request in requests:
        groups.setdefault(request.target_key, []).append(request)
    return groups


def utc_now() -> datetime:
    """The default clock."""
    return datetime.now(UTC)


def cooldown_annotation(resource: str) -> str:
    """Annotation holding when a resource was last scaled: vm-autoscaler/last-scaled-<resource>."""
    return f"{COOLDOWN_ANNOTATION_PREFIX}{resource}"


def make_idempotency_key(
    target: Target, changes: list[ResourceChange], fingerprints: list[str]
) -> str:
    """Stable hash of target + changes + alert fingerprints, so the backend can dedupe."""
    material = {
        "target": str(target.key),
        "changes": [[c.field, c.old, c.new] for c in changes],
        "fingerprints": sorted(fingerprints),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


@dataclass
class Candidate:
    """A request that passed the pre-checks and will be evaluated against current state."""

    request: ScaleRequest
    strategy: ScalingStrategy
    policy: Policy
    mode: Mode


@dataclass
class PlannedChange:
    """A candidate with the changes computed for it."""

    candidate: Candidate
    result: ComputeResult

    @property
    def changes(self) -> list[ResourceChange]:
        """The field changes for this resource."""
        return self.result.changes


@dataclass
class Plan:
    """Result of planning: changes to deliver plus requests that were skipped."""

    changes: list[PlannedChange] = field(default_factory=list)
    skipped: list[ScaleDecision] = field(default_factory=list)


class TargetLocks:
    """One asyncio.Lock per target, so only one change per target runs at a time."""

    def __init__(self) -> None:
        self._locks: dict[TargetKey, asyncio.Lock] = {}

    def for_target(self, key: TargetKey) -> asyncio.Lock:
        """Return the lock for this target, creating it on first use."""
        return self._locks.setdefault(key, asyncio.Lock())


class ScalingService:
    """Orchestrates: parse -> validate target -> state -> cooldown -> compute -> apply -> sync."""

    def __init__(
        self,
        config: AppConfig,
        gitops: GitOpsBackend,
        syncer: DeploymentSyncer,
        metrics: Metrics,
        *,
        clock: Clock = utc_now,
        annotations_supported: bool = True,
        strategies: dict[ResourceName, ScalingStrategy] | None = None,
    ) -> None:
        self._config = config
        self._targets = config.targets_by_key()
        self._gitops = gitops
        self._syncer = syncer
        self._metrics = metrics
        self._clock = clock
        self._annotations_supported = annotations_supported
        self._strategies = strategies or build_strategy_registry()
        self._locks = TargetLocks()
        # Extra safety on top of Git annotations; also the only cooldown store when the
        # backend cannot keep annotations. Resets on restart.
        self._last_scaled: dict[tuple[TargetKey, str], datetime] = {}

    # ----------------------------------------------------------------------------------
    # Entry point
    # ----------------------------------------------------------------------------------

    async def handle(self, parsed: ParsedWebhook) -> list[ScaleDecision]:
        """Handle all firing alerts of one webhook. Returns one decision per alert."""
        started = time.monotonic()
        decisions: list[ScaleDecision] = []
        try:
            for key, requests in group_by_target(parsed.requests).items():
                decisions.extend(await self._handle_target_safely(key, requests))
        finally:
            self._metrics.handle_duration.observe(time.monotonic() - started)
        for decision in decisions:
            self._record(decision)
        return decisions

    async def _handle_target_safely(
        self, key: TargetKey, requests: list[ScaleRequest]
    ) -> list[ScaleDecision]:
        """Handle one target; an unexpected bug skips only this target, never the webhook.

        Known failures (GitOps/Argo errors) are already turned into skip decisions inside
        `_handle_target`. This is the last line of defence for anything else, so one bad
        target cannot hide the decisions for the others. `asyncio.CancelledError` is a
        BaseException and is deliberately not caught (shutdown must still cancel us).
        """
        try:
            return await self._handle_target(key, requests)
        except Exception:
            logger.error(
                "unexpected error while handling target; skipped",
                exc_info=True,
                extra={"fields": {"target": str(key)}},
            )
            return skip_all(requests, SkipReason.INTERNAL_ERROR)

    async def _handle_target(
        self, key: TargetKey, requests: list[ScaleRequest]
    ) -> list[ScaleDecision]:
        decisions: list[ScaleDecision] = []
        enabled: list[ScaleRequest] = []
        for request in requests:
            reason = self._enabled_check(request)
            if reason is None:
                enabled.append(request)
            else:
                decisions.append(skipped(request, reason))
        if not enabled:
            return decisions

        target = self._targets.get(key)
        if target is None:
            self._metrics.unknown_targets.inc()
            return decisions + [skipped(r, SkipReason.UNKNOWN_TARGET) for r in enabled]

        candidates, precheck_skips = self._select_candidates(target, enabled)
        decisions.extend(precheck_skips)
        if not candidates:
            return decisions

        async with self._locks.for_target(key):
            decisions.extend(await self._plan_and_deliver(target, candidates))
        return decisions

    # ----------------------------------------------------------------------------------
    # Pre-checks (no I/O)
    # ----------------------------------------------------------------------------------

    def _enabled_check(self, request: ScaleRequest) -> SkipReason | None:
        """Rule 1: the autoscale label is "true" and the component is enabled."""
        if not request.autoscale:
            return SkipReason.AUTOSCALE_DISABLED
        if request.target_key.component not in self._config.enabled_components:
            return SkipReason.COMPONENT_DISABLED
        return None

    def _select_candidates(
        self, target: Target, requests: list[ScaleRequest]
    ) -> tuple[list[Candidate], list[ScaleDecision]]:
        """Rule 2 (threshold), plus policy lookup and de-duplication per resource."""
        candidates: dict[str, Candidate] = {}
        decisions: list[ScaleDecision] = []
        for request in requests:
            policy = target.policies.get(request.resource)
            strategy = self._strategies.get(request.resource)
            if policy is None or strategy is None:
                decisions.append(skipped(request, SkipReason.NO_POLICY))
            elif request.value is None:
                decisions.append(skipped(request, SkipReason.VALUE_MISSING))
            elif request.value < policy.trigger_percent:
                decisions.append(skipped(request, SkipReason.BELOW_THRESHOLD))
            elif request.resource in candidates:
                decisions.append(skipped(request, SkipReason.DUPLICATE_IN_BATCH))
            else:
                mode = strategy.effective_mode(self._config.mode, policy)
                candidates[request.resource] = Candidate(request, strategy, policy, mode)
        return list(candidates.values()), decisions

    # ----------------------------------------------------------------------------------
    # Planning (cooldown + compute)
    # ----------------------------------------------------------------------------------

    async def _plan_and_deliver(
        self, target: Target, candidates: list[Candidate]
    ) -> list[ScaleDecision]:
        try:
            state = await self._gitops.get_component_state(target)
        except GitOpsError as exc:
            return skip_all([c.request for c in candidates], gitops_skip_reason(exc), str(exc))

        plan = self._plan(target, candidates, state)
        decisions = list(plan.skipped)

        dry_run = [p for p in plan.changes if p.candidate.mode is Mode.DRY_RUN]
        merge_request = [p for p in plan.changes if p.candidate.mode is Mode.MERGE_REQUEST]
        direct = [p for p in plan.changes if p.candidate.mode is Mode.DIRECT_COMMIT]

        decisions.extend(
            planned_decision(p, Decision.DRY_RUN, OutcomeReason.DRY_RUN) for p in dry_run
        )
        if merge_request:
            decisions.extend(await self._open_change_request(target, merge_request))
        if direct:
            decisions.extend(await self._commit_and_sync(target, direct, state))
        return decisions

    def _plan(self, target: Target, candidates: list[Candidate], state: ComponentState) -> Plan:
        plan = Plan()
        now = self._clock()
        for candidate in candidates:
            request = candidate.request
            if self._cooldown_active(target, candidate, state, now):
                plan.skipped.append(skipped(request, SkipReason.COOLDOWN_ACTIVE))
                continue
            result = candidate.strategy.compute(state, candidate.policy, target)
            if result.skip_reason is not None:
                decision = skipped(request, result.skip_reason, mode=candidate.mode)
                plan.skipped.append(decision)
                if result.detail:
                    logger.info(
                        "strategy skipped",
                        extra={"fields": {"resource": request.resource, "detail": result.detail}},
                    )
                continue
            plan.changes.append(PlannedChange(candidate, result))
        return plan

    def _cooldown_active(
        self, target: Target, candidate: Candidate, state: ComponentState, now: datetime
    ) -> bool:
        """Rule 3: skip if this resource was scaled less than `cooldown` ago."""
        resource = candidate.request.resource
        times = [self._last_scaled.get((target.key, resource))]
        times.append(parse_timestamp(state.annotations.get(cooldown_annotation(resource))))
        last = max((t for t in times if t is not None), default=None)
        return last is not None and now - last < candidate.policy.cooldown

    def _annotations_for(self, planned: list[PlannedChange]) -> dict[str, str]:
        if not self._annotations_supported:
            return {}
        stamp = self._clock().strftime("%Y-%m-%dT%H:%M:%SZ")
        return {cooldown_annotation(p.candidate.request.resource): stamp for p in planned}

    def _remember_scaled(self, target: Target, planned: list[PlannedChange]) -> None:
        now = self._clock()
        for p in planned:
            self._last_scaled[(target.key, p.candidate.request.resource)] = now

    # ----------------------------------------------------------------------------------
    # Delivery
    # ----------------------------------------------------------------------------------

    async def _open_change_request(
        self, target: Target, planned: list[PlannedChange]
    ) -> list[ScaleDecision]:
        """Mode merge_request: open one MR for all changes. Never syncs Argo."""
        changes = flatten(planned)
        try:
            result = await self._gitops.open_change_request(
                target,
                changes,
                self._build_reason(target, planned),
                self._annotations_for(planned),
                make_idempotency_key(target, changes, fingerprints(planned)),
            )
        except GitOpsError as exc:
            return skip_all(
                [p.candidate.request for p in planned], gitops_skip_reason(exc), str(exc)
            )
        if not result.accepted:
            return skip_all(
                [p.candidate.request for p in planned], SkipReason.GITOPS_REJECTED, result.message
            )
        # Remember it, so Grafana's repeat notifications don't open a new MR every hour.
        self._remember_scaled(target, planned)
        return [
            planned_decision(
                p, Decision.CHANGE_REQUEST_OPENED, OutcomeReason.CHANGE_REQUEST_OPENED, result
            )
            for p in planned
        ]

    async def _commit_and_sync(
        self, target: Target, planned: list[PlannedChange], state: ComponentState
    ) -> list[ScaleDecision]:
        """Mode direct_commit: Argo pre-check, commit (one conflict retry), then sync."""
        requests = [p.candidate.request for p in planned]
        unhealthy = await self._argocd_precheck(target)
        if unhealthy is not None:
            return skip_all(requests, unhealthy[0], unhealthy[1])

        decisions: list[ScaleDecision] = []
        result: ChangeResult | None = None
        for attempt in (1, 2):
            changes = flatten(planned)
            try:
                result = await self._gitops.apply_changes(
                    target,
                    changes,
                    self._build_reason(target, planned),
                    self._annotations_for(planned),
                    state.version,
                    make_idempotency_key(target, changes, fingerprints(planned)),
                )
                break
            except GitOpsConflictError as exc:
                requests = [p.candidate.request for p in planned]
                if attempt == 2:
                    return decisions + skip_all(requests, SkipReason.GITOPS_CONFLICT, str(exc))
                logger.warning(
                    "gitops conflict, re-reading state once",
                    extra={"fields": {"target": str(target.key)}},
                )
                try:
                    state = await self._gitops.get_component_state(target)
                except GitOpsError as read_exc:
                    reason = gitops_skip_reason(read_exc)
                    return decisions + skip_all(requests, reason, str(read_exc))
                plan = self._plan(target, [p.candidate for p in planned], state)
                decisions.extend(plan.skipped)
                planned = plan.changes
                if not planned:
                    return decisions
            except GitOpsError as exc:
                requests = [p.candidate.request for p in planned]
                return decisions + skip_all(requests, gitops_skip_reason(exc), str(exc))

        requests = [p.candidate.request for p in planned]
        if result is None or not result.accepted:
            message = result.message if result else None
            return decisions + skip_all(requests, SkipReason.GITOPS_REJECTED, message)

        self._remember_scaled(target, planned)
        synced = await self._sync(target)
        outcome = OutcomeReason.SCALED if synced else OutcomeReason.ARGOCD_SYNC_FAILED
        decisions.extend(
            planned_decision(p, Decision.APPLIED, outcome, result, synced=synced) for p in planned
        )
        return decisions

    async def _argocd_precheck(self, target: Target) -> tuple[SkipReason, str] | None:
        """Rule 6: don't stack a change on a sick, busy or out-of-sync app. Skip reason or None."""
        if not self._config.argocd_precheck:
            return None
        try:
            status = await self._syncer.get_status(target.argocd_app)
        except ArgoCdError as exc:
            return SkipReason.ARGOCD_UNAVAILABLE, str(exc)
        if not status.is_safe_to_change:
            detail = (
                f"health={status.health} sync={status.sync} "
                f"operation_in_progress={status.operation_in_progress}"
            )
            return SkipReason.DEPLOYMENT_NOT_HEALTHY, detail
        return None

    async def _sync(self, target: Target) -> bool:
        """Rule 10: sync only the listed resources. Returns True on success."""
        try:
            await self._syncer.sync(target.argocd_app, target.argocd_sync_resources)
        except ArgoCdError as exc:
            self._metrics.argocd_sync.labels(result="failure").inc()
            logger.error(
                "argo cd sync failed; the change is committed and auto-sync may still apply it",
                extra={"fields": {"argocd_app": target.argocd_app, "error": str(exc)}},
            )
            return False
        self._metrics.argocd_sync.labels(result="success").inc()
        return True

    def _build_reason(self, target: Target, planned: list[PlannedChange]) -> str:
        """Commit / MR message. First line like:

        [vm-autoscaler] ocp-a/monitoring vmstorage storage 30Gi -> 36Gi (usage 85.0% >= 80%)

        The GitLab backend reads cooldowns back from this title, so keep its format.
        """
        key = target.key
        summaries = []
        details = []
        for p in planned:
            request, policy, headline = p.candidate.request, p.candidate.policy, p.result.headline
            assert headline is not None
            summaries.append(
                f"{request.resource} {headline.old} -> {headline.new} "
                f"(usage {request.value:.1f}% >= {policy.trigger_percent:g}%)"
            )
            details.append(
                f"- {request.resource}: alert {request.alert_name or '-'} "
                f"(fingerprint {request.fingerprint}), policy step {policy.step_percent:g}%, "
                f"max {policy.max}, cooldown {policy.cooldown}"
            )
            # Field names, not YAML paths: each backend knows its own file layout.
            details.extend(f"  {c.field}: {c.old} -> {c.new}" for c in p.changes)
        title = (
            f"{self._config.change_reason_prefix} {key.ocp_cluster}/{key.namespace} "
            f"{key.component} {', '.join(summaries)}"
        )
        return title + "\n\n" + "\n".join(details)

    # ----------------------------------------------------------------------------------
    # Recording
    # ----------------------------------------------------------------------------------

    def _record(self, decision: ScaleDecision) -> None:
        """Count and log one decision."""
        self._metrics.decisions.labels(
            ocp_cluster=decision.ocp_cluster,
            resource=decision.resource,
            decision=decision.decision.value,
            reason=decision.reason.value,
        ).inc()
        level = logging.WARNING if decision.reason in _WARNING_REASONS else logging.INFO
        logger.log(
            level,
            "scale decision",
            extra={
                "fields": decision.model_dump(
                    mode="json",
                    include={
                        "fingerprint",
                        "ocp_cluster",
                        "namespace",
                        "component",
                        "resource",
                        "value",
                        "decision",
                        "reason",
                        "old",
                        "new",
                        "mode",
                        "change_id",
                    },
                )
            },
        )


_WARNING_REASONS: set[SkipReason | OutcomeReason] = {
    SkipReason.AT_MAX_CAP,  # a human must act (e.g. add nodes)
    SkipReason.UNKNOWN_TARGET,
    SkipReason.GITOPS_CONFLICT,
    SkipReason.GITOPS_UNAVAILABLE,
    SkipReason.GITOPS_REJECTED,
    SkipReason.GITOPS_ERROR,
    SkipReason.GITOPS_TARGET_NOT_FOUND,
    SkipReason.ARGOCD_UNAVAILABLE,
    SkipReason.DEPLOYMENT_NOT_HEALTHY,
    SkipReason.INTERNAL_ERROR,
    OutcomeReason.ARGOCD_SYNC_FAILED,
}


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


def gitops_skip_reason(exc: GitOpsError) -> SkipReason:
    """Map a GitOps exception to a skip reason."""
    if isinstance(exc, GitOpsUnavailableError):
        return SkipReason.GITOPS_UNAVAILABLE
    if isinstance(exc, GitOpsConflictError):
        return SkipReason.GITOPS_CONFLICT
    if isinstance(exc, GitOpsNotFoundError):
        return SkipReason.GITOPS_TARGET_NOT_FOUND
    if isinstance(exc, GitOpsRejectedError):
        return SkipReason.GITOPS_REJECTED
    return SkipReason.GITOPS_ERROR


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp from an annotation. Bad values are logged and ignored."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        logger.warning(
            "ignoring unparseable cooldown annotation", extra={"fields": {"value": value}}
        )
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def flatten(planned: list[PlannedChange]) -> list[ResourceChange]:
    """All field changes of all planned resources."""
    return [change for p in planned for change in p.changes]


def fingerprints(planned: list[PlannedChange]) -> list[str]:
    """Alert fingerprints of all planned resources."""
    return [p.candidate.request.fingerprint for p in planned]


def skipped(
    request: ScaleRequest, reason: SkipReason, detail: str | None = None, mode: Mode | None = None
) -> ScaleDecision:
    """A SKIPPED decision for one request."""
    if detail:
        logger.info(
            "skip detail",
            extra={
                "fields": {"fingerprint": request.fingerprint, "reason": reason, "detail": detail}
            },
        )
    return _base_decision(request, Decision.SKIPPED, reason, mode)


def skip_all(
    requests: list[ScaleRequest], reason: SkipReason, detail: str | None = None
) -> list[ScaleDecision]:
    """SKIPPED decisions for several requests with the same reason."""
    return [skipped(request, reason, detail) for request in requests]


def planned_decision(
    planned: PlannedChange,
    decision: Decision,
    reason: OutcomeReason,
    result: ChangeResult | None = None,
    *,
    synced: bool = False,
) -> ScaleDecision:
    """A decision for a request that produced a change (dry run, MR or commit)."""
    headline = planned.result.headline
    scale_decision = _base_decision(
        planned.candidate.request, decision, reason, planned.candidate.mode
    )
    scale_decision.old = headline.old if headline else None
    scale_decision.new = headline.new if headline else None
    scale_decision.synced = synced
    if result is not None:
        scale_decision.change_id = result.change_id
        scale_decision.url = result.url
    return scale_decision


def _base_decision(
    request: ScaleRequest, decision: Decision, reason: SkipReason | OutcomeReason, mode: Mode | None
) -> ScaleDecision:
    key = request.target_key
    return ScaleDecision(
        fingerprint=request.fingerprint,
        ocp_cluster=key.ocp_cluster,
        namespace=key.namespace,
        component=key.component,
        resource=request.resource,
        value=request.value,
        decision=decision,
        reason=reason,
        mode=mode,
    )
