import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.adapters.argocd_fake import FakeArgoCdSyncer
from app.adapters.gitops_fake import FakeGitOpsBackend
from app.config import AppConfig
from app.domain.errors import (
    ArgoCdError,
    GitOpsConflictError,
    GitOpsNotFoundError,
    GitOpsUnavailableError,
)
from app.domain.models import (
    AppStatus,
    ChangeResult,
    ComponentState,
    Decision,
    Mode,
    ParsedWebhook,
    ScaleDecision,
    ScaleRequest,
    TargetKey,
)
from app.observability import Metrics
from app.services.scaling_service import ScalingService, cooldown_annotation
from tests.factories import TARGET_KEY, state

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
REGION_B = TargetKey(ocp_cluster="ocp-prod-region-b", namespace="monitoring", component="vmstorage")


def request(value: float | None = 85.0, **overrides: object) -> ScaleRequest:
    values: dict[str, object] = {
        "fingerprint": "fp-storage",
        "alert_name": "VmstorageDiskFilling",
        "target_key": TARGET_KEY,
        "resource": "storage",
        "value": value,
        "autoscale": True,
    }
    values.update(overrides)
    return ScaleRequest.model_validate(values)


class Harness:
    def __init__(self, config: AppConfig, mode: Mode, seed: ComponentState | None = None) -> None:
        self.config = config.model_copy(update={"mode": mode})
        self.gitops = FakeGitOpsBackend()
        self.gitops.seed(TARGET_KEY, seed or state())
        self.gitops.seed(REGION_B, state())
        self.syncer = FakeArgoCdSyncer()
        self.metrics = Metrics()
        self.service = ScalingService(
            self.config, self.gitops, self.syncer, self.metrics, clock=lambda: NOW
        )

    async def run(self, *requests: ScaleRequest) -> list[ScaleDecision]:
        return await self.service.handle(ParsedWebhook(requests=list(requests), ignored=[]))


def config_with(app_config: AppConfig, **storage_policy: Any) -> AppConfig:
    raw = app_config.model_dump(mode="json")
    raw["policies"]["storage"].update(storage_policy)
    return AppConfig.model_validate(raw)


@pytest.fixture
def direct(app_config: AppConfig) -> Harness:
    return Harness(app_config, Mode.DIRECT_COMMIT)


# ---------------------------------------------------------------------- modes


async def test_dry_run_changes_nothing(app_config: AppConfig) -> None:
    h = Harness(app_config, Mode.DRY_RUN)
    [decision] = await h.run(request())
    assert decision.decision is Decision.DRY_RUN
    assert (decision.old, decision.new) == ("30Gi", "36Gi")
    assert h.gitops.applied == [] and h.gitops.change_requests == []
    assert h.syncer.syncs == []
    assert h.gitops.states[TARGET_KEY].storage == "30Gi"


async def test_direct_commit_applies_once_and_syncs_once(direct: Harness) -> None:
    [decision] = await direct.run(request())
    assert decision.decision is Decision.APPLIED
    assert decision.synced
    assert (decision.old, decision.new) == ("30Gi", "36Gi")
    [commit] = direct.gitops.applied
    assert [(c.field, c.old, c.new) for c in commit.changes] == [("storage", "30Gi", "36Gi")]
    assert commit.reason.startswith(
        "[vm-autoscaler] ocp-prod-region-a/monitoring vmstorage storage 30Gi -> 36Gi "
        "(usage 85.0% >= 80%)"
    )
    assert "  storage: 30Gi -> 36Gi" in commit.reason
    assert commit.annotations == {cooldown_annotation("storage"): "2026-11-27T12:00:00Z"}
    [(app, resources)] = direct.syncer.syncs
    assert app == "victoria-metrics-region-a"
    assert resources[0].kind == "VMCluster"
    assert direct.gitops.states[TARGET_KEY].storage == "36Gi"


async def test_two_targets_one_commit_and_one_sync_each(direct: Harness) -> None:
    decisions = await direct.run(request(), request(82.5, target_key=REGION_B, fingerprint="fp-b"))
    assert {d.ocp_cluster: d.decision for d in decisions} == {
        "ocp-prod-region-a": Decision.APPLIED,
        "ocp-prod-region-b": Decision.APPLIED,
    }
    assert sorted(str(c.target) for c in direct.gitops.applied) == [
        str(TARGET_KEY),
        str(REGION_B),
    ]
    assert sorted(app for app, _ in direct.syncer.syncs) == [
        "victoria-metrics-region-a",
        "victoria-metrics-region-b",
    ]


async def test_merge_request_mode_does_not_sync(app_config: AppConfig) -> None:
    h = Harness(app_config, Mode.MERGE_REQUEST)
    [decision] = await h.run(request())
    assert decision.decision is Decision.CHANGE_REQUEST_OPENED
    assert decision.url == "https://fake.invalid/mr/1"
    assert h.syncer.syncs == []
    assert h.gitops.applied == []
    [mr] = h.gitops.change_requests
    assert "storage 30Gi -> 36Gi" in mr.reason


async def test_policy_merge_request_wins_over_global_direct_commit(app_config: AppConfig) -> None:
    h = Harness(config_with(app_config, mode="merge_request"), Mode.DIRECT_COMMIT)
    [decision] = await h.run(request())
    assert decision.decision is Decision.CHANGE_REQUEST_OPENED
    assert decision.mode is Mode.MERGE_REQUEST
    assert h.gitops.applied == [] and h.syncer.syncs == []


async def test_policy_cannot_make_dry_run_less_safe(app_config: AppConfig) -> None:
    h = Harness(config_with(app_config, mode="direct_commit"), Mode.DRY_RUN)
    [decision] = await h.run(request())
    assert decision.decision is Decision.DRY_RUN


# ------------------------------------------------------------------ cooldowns


async def test_cooldown_from_annotation_skips(app_config: AppConfig) -> None:
    recent = (NOW - timedelta(hours=1)).isoformat()
    h = Harness(
        app_config,
        Mode.DIRECT_COMMIT,
        state(annotations={cooldown_annotation("storage"): recent}),
    )
    [decision] = await h.run(request())
    assert decision.reason == "cooldown_active"
    assert h.gitops.applied == []


async def test_expired_cooldown_allows_scaling(app_config: AppConfig) -> None:
    old = (NOW - timedelta(hours=25)).isoformat()
    h = Harness(
        app_config, Mode.DIRECT_COMMIT, state(annotations={cooldown_annotation("storage"): old})
    )
    [decision] = await h.run(request())
    assert decision.decision is Decision.APPLIED


async def test_unparseable_cooldown_annotation_is_ignored(app_config: AppConfig) -> None:
    h = Harness(
        app_config,
        Mode.DIRECT_COMMIT,
        state(annotations={cooldown_annotation("storage"): "yesterday"}),
    )
    [decision] = await h.run(request())
    assert decision.decision is Decision.APPLIED


async def test_repeat_alert_is_idempotent(direct: Harness) -> None:
    await direct.run(request())
    [second] = await direct.run(request())
    assert second.reason == "cooldown_active"
    assert len(direct.gitops.applied) == 1
    assert len(direct.syncer.syncs) == 1


async def test_repeat_alert_after_merge_request_does_not_open_another(
    app_config: AppConfig,
) -> None:
    h = Harness(app_config, Mode.MERGE_REQUEST)
    await h.run(request())
    [second] = await h.run(request())
    assert second.reason == "cooldown_active"
    assert len(h.gitops.change_requests) == 1


# ---------------------------------------------------------------- pre-checks


async def test_below_threshold_skips(direct: Harness) -> None:
    [decision] = await direct.run(request(value=50.0))
    assert decision.reason == "below_threshold"


async def test_autoscale_disabled_skips(direct: Harness) -> None:
    [decision] = await direct.run(request(autoscale=False))
    assert decision.reason == "autoscale_disabled"


async def test_component_not_enabled_skips(direct: Harness) -> None:
    key = TargetKey(ocp_cluster="ocp-prod-region-a", namespace="monitoring", component="vmselect")
    [decision] = await direct.run(request(target_key=key))
    assert decision.reason == "component_disabled"


async def test_unknown_target_skips_and_counts(direct: Harness) -> None:
    key = TargetKey(ocp_cluster="nope", namespace="monitoring", component="vmstorage")
    [decision] = await direct.run(request(target_key=key))
    assert decision.reason == "unknown_target"
    assert direct.metrics.registry.get_sample_value("vm_autoscaler_unknown_targets_total") == 1


async def test_value_missing_skips(direct: Harness) -> None:
    [decision] = await direct.run(request(value=None))
    assert decision.reason == "value_missing"


async def test_duplicate_alert_in_batch(direct: Harness) -> None:
    decisions = await direct.run(request(), request(fingerprint="other"))
    assert sorted(d.reason for d in decisions) == ["duplicate_in_batch", "scaled"]
    assert len(direct.gitops.applied) == 1


# ------------------------------------------------------------ state checks


async def test_value_not_set_skips(app_config: AppConfig) -> None:
    h = Harness(app_config, Mode.DIRECT_COMMIT, state(storage=None))
    [decision] = await h.run(request())
    assert decision.reason == "value_not_set"


async def test_at_max_cap_skips(app_config: AppConfig) -> None:
    h = Harness(app_config, Mode.DIRECT_COMMIT, state(storage="2Ti"))
    [decision] = await h.run(request())
    assert decision.reason == "at_max_cap"
    assert h.gitops.applied == []


async def test_expansion_not_supported_skips(app_config: AppConfig) -> None:
    raw = app_config.model_dump(mode="json")
    raw["targets"][0]["storage_expansion_supported"] = False
    h = Harness(AppConfig.model_validate(raw), Mode.DIRECT_COMMIT)
    [decision] = await h.run(request())
    assert decision.reason == "expansion_not_supported"
    assert h.gitops.applied == []


# --------------------------------------------------------------- Argo CD


@pytest.mark.parametrize(
    "status",
    [
        AppStatus(health="Progressing", sync="Synced"),
        AppStatus(health="Degraded", sync="Synced"),
        AppStatus(health="Healthy", sync="Synced", operation_in_progress=True),
        AppStatus(health="Healthy", sync="OutOfSync"),
        AppStatus(health="Healthy", sync="Unknown"),
    ],
)
async def test_argo_unhealthy_skips(direct: Harness, status: AppStatus) -> None:
    direct.syncer.status = status
    [decision] = await direct.run(request())
    assert decision.reason == "deployment_not_healthy"
    assert direct.gitops.applied == []
    assert direct.syncer.syncs == []


async def test_argo_status_unavailable_skips(direct: Harness) -> None:
    direct.syncer.fail_status = ArgoCdError("down")
    [decision] = await direct.run(request())
    assert decision.reason == "argocd_unavailable"
    assert direct.gitops.applied == []


async def test_argo_precheck_can_be_disabled(app_config: AppConfig) -> None:
    config = app_config.model_copy(update={"argocd_precheck": False})
    h = Harness(config, Mode.DIRECT_COMMIT)
    h.syncer.status = AppStatus(health="Degraded", sync="Synced")
    [decision] = await h.run(request())
    assert decision.decision is Decision.APPLIED


async def test_sync_failure_reports_committed_change(direct: Harness) -> None:
    direct.syncer.fail_sync = ArgoCdError("boom")
    [decision] = await direct.run(request())
    assert decision.decision is Decision.APPLIED
    assert decision.reason == "argocd_sync_failed"
    assert decision.synced is False
    assert len(direct.gitops.applied) == 1


# ------------------------------------------------------ GitOps failures


async def test_conflict_retried_once(direct: Harness) -> None:
    direct.gitops.fail_next("apply_changes", GitOpsConflictError("409"))
    [decision] = await direct.run(request())
    assert decision.decision is Decision.APPLIED
    assert len(direct.gitops.applied) == 1


async def test_conflict_twice_skips(direct: Harness) -> None:
    direct.gitops.fail_next("apply_changes", GitOpsConflictError("409"), times=2)
    [decision] = await direct.run(request())
    assert decision.reason == "gitops_conflict"
    assert direct.syncer.syncs == []


async def test_conflict_recomputes_from_fresh_state(direct: Harness) -> None:
    # Someone else grows the PVC to the max between our read and our write.
    original_apply = direct.gitops.apply_changes
    calls = 0

    async def apply_with_race(*args: Any, **kwargs: Any) -> ChangeResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            direct.gitops.seed(TARGET_KEY, state(storage="2Ti", version="v2"))
        return await original_apply(*args, **kwargs)

    direct.gitops.apply_changes = apply_with_race  # type: ignore[method-assign]
    [decision] = await direct.run(request())
    assert calls == 1  # conflict on first try, re-read shows we are at max: no second write
    assert decision.reason == "at_max_cap"
    assert direct.gitops.applied == []


async def test_backend_unavailable_on_write_no_sync(direct: Harness) -> None:
    direct.gitops.fail_next("apply_changes", GitOpsUnavailableError("down"))
    [decision] = await direct.run(request())
    assert decision.reason == "gitops_unavailable"
    assert direct.syncer.syncs == []


async def test_backend_unavailable_on_read(direct: Harness) -> None:
    direct.gitops.fail_next("get_component_state", GitOpsUnavailableError("down"))
    [decision] = await direct.run(request())
    assert decision.reason == "gitops_unavailable"
    assert direct.syncer.syncs == []


async def test_backend_target_not_found(direct: Harness) -> None:
    direct.gitops.fail_next("get_component_state", GitOpsNotFoundError("no such file"))
    [decision] = await direct.run(request())
    assert decision.reason == "gitops_target_not_found"


async def test_merge_request_failure_skips(app_config: AppConfig) -> None:
    h = Harness(app_config, Mode.MERGE_REQUEST)
    h.gitops.fail_next("open_change_request", GitOpsUnavailableError("down"))
    [decision] = await h.run(request())
    assert decision.reason == "gitops_unavailable"


# ------------------------------------------------------------ bookkeeping


async def test_idempotency_key_is_stable(app_config: AppConfig) -> None:
    keys = []
    for _ in range(2):
        h = Harness(app_config, Mode.DIRECT_COMMIT)
        await h.run(request())
        keys.append(h.gitops.applied[0].idempotency_key)
    assert keys[0] == keys[1]


async def test_decision_metric_counted(direct: Harness) -> None:
    await direct.run(request())
    value = direct.metrics.registry.get_sample_value(
        "vm_autoscaler_decisions_total",
        {
            "ocp_cluster": "ocp-prod-region-a",
            "resource": "storage",
            "decision": "applied",
            "reason": "scaled",
        },
    )
    assert value == 1


async def test_unexpected_error_skips_only_that_target(direct: Harness) -> None:
    original = direct.gitops.get_component_state

    async def broken_for_region_a(target: Any) -> ComponentState:
        if target.key == TARGET_KEY:
            raise RuntimeError("bug in a backend")
        return await original(target)

    direct.gitops.get_component_state = broken_for_region_a  # type: ignore[method-assign]
    decisions = await direct.run(request(), request(82.5, target_key=REGION_B, fingerprint="fp-b"))
    by_cluster = {d.ocp_cluster: d for d in decisions}
    assert by_cluster["ocp-prod-region-a"].reason == "internal_error"
    assert by_cluster["ocp-prod-region-a"].decision is Decision.SKIPPED
    assert by_cluster["ocp-prod-region-b"].decision is Decision.APPLIED
    assert (
        direct.metrics.registry.get_sample_value(
            "vm_autoscaler_decisions_total",
            {
                "ocp_cluster": "ocp-prod-region-a",
                "resource": "storage",
                "decision": "skipped",
                "reason": "internal_error",
            },
        )
        == 1
    )


async def test_cancellation_is_not_swallowed(direct: Harness) -> None:
    async def cancelled(target: Any) -> ComponentState:
        raise asyncio.CancelledError

    direct.gitops.get_component_state = cancelled  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await direct.run(request())
