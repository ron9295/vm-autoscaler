from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.adapters.argocd_fake import FakeDeploymentSyncer
from app.adapters.gitops_fake import FakeGitOpsBackend
from app.config import AppConfig
from app.domain.errors import ArgoCdError, GitOpsConflictError, GitOpsUnavailableError
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


def request(resource: str = "cpu", value: float = 91.2, **overrides: object) -> ScaleRequest:
    values: dict[str, object] = {
        "fingerprint": f"fp-{resource}",
        "alert_name": f"VmstorageHigh{resource}",
        "target_key": TARGET_KEY,
        "resource": resource,
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
        self.syncer = FakeDeploymentSyncer()
        self.metrics = Metrics()
        self.service = ScalingService(
            self.config, self.gitops, self.syncer, self.metrics, clock=lambda: NOW
        )

    async def run(self, *requests: ScaleRequest) -> list[ScaleDecision]:
        return await self.service.handle(ParsedWebhook(requests=list(requests), ignored=[]))


@pytest.fixture
def direct(app_config: AppConfig) -> Harness:
    return Harness(app_config, Mode.DIRECT_COMMIT)


async def test_dry_run_changes_nothing(app_config: AppConfig) -> None:
    h = Harness(app_config, Mode.DRY_RUN)
    [decision] = await h.run(request("cpu"))
    assert decision.decision is Decision.DRY_RUN
    assert (decision.old, decision.new) == ("4", "5")
    assert h.gitops.applied == [] and h.gitops.change_requests == []
    assert h.syncer.syncs == []
    assert h.gitops.states[TARGET_KEY].cpu_limit == "4"


async def test_direct_commit_cpu_and_memory_one_commit_one_sync(direct: Harness) -> None:
    decisions = await direct.run(request("cpu"), request("memory", 85.0))
    assert {d.decision for d in decisions} == {Decision.APPLIED}
    assert all(d.synced for d in decisions)
    assert len(direct.gitops.applied) == 1
    assert len(direct.syncer.syncs) == 1
    [commit] = direct.gitops.applied
    assert {c.field for c in commit.changes} == {
        "cpu_limit",
        "cpu_request",
        "memory_limit",
        "memory_request",
    }
    assert commit.reason.startswith(
        "[vm-autoscaler] ocp-prod-region-a/monitoring vmstorage cpu 4 -> 5 (usage 91.2% >= 80%), "
        "memory 16Gi -> 20Gi (usage 85.0% >= 80%)"
    )
    assert commit.annotations == {
        cooldown_annotation("cpu"): "2026-11-27T12:00:00Z",
        cooldown_annotation("memory"): "2026-11-27T12:00:00Z",
    }
    app, resources = direct.syncer.syncs[0]
    assert app == "victoria-metrics-region-a"
    assert resources[0].kind == "VMCluster"
    new_state = direct.gitops.states[TARGET_KEY]
    assert (new_state.cpu_limit, new_state.memory_limit) == ("5", "20Gi")


async def test_cooldown_from_annotation_skips(app_config: AppConfig) -> None:
    recent = (NOW - timedelta(hours=1)).isoformat()
    h = Harness(
        app_config, Mode.DIRECT_COMMIT, state(annotations={cooldown_annotation("cpu"): recent})
    )
    [decision] = await h.run(request("cpu"))
    assert decision.reason == "cooldown_active"
    assert h.gitops.applied == []


async def test_expired_cooldown_allows_scaling(app_config: AppConfig) -> None:
    old = (NOW - timedelta(hours=7)).isoformat()
    h = Harness(
        app_config, Mode.DIRECT_COMMIT, state(annotations={cooldown_annotation("cpu"): old})
    )
    [decision] = await h.run(request("cpu"))
    assert decision.decision is Decision.APPLIED


async def test_repeat_alert_is_idempotent(direct: Harness) -> None:
    await direct.run(request("cpu"))
    [second] = await direct.run(request("cpu"))
    assert second.reason == "cooldown_active"
    assert len(direct.gitops.applied) == 1


async def test_below_threshold_skips(direct: Harness) -> None:
    [decision] = await direct.run(request("cpu", value=50.0))
    assert decision.reason == "below_threshold"


async def test_autoscale_disabled_skips(direct: Harness) -> None:
    [decision] = await direct.run(request("cpu", autoscale=False))
    assert decision.reason == "autoscale_disabled"


async def test_component_not_enabled_skips(direct: Harness) -> None:
    key = TargetKey(ocp_cluster="ocp-prod-region-a", namespace="monitoring", component="vmselect")
    [decision] = await direct.run(request("cpu", target_key=key))
    assert decision.reason == "component_disabled"


async def test_unknown_target_skips_and_counts(direct: Harness) -> None:
    key = TargetKey(ocp_cluster="nope", namespace="monitoring", component="vmstorage")
    [decision] = await direct.run(request("cpu", target_key=key))
    assert decision.reason == "unknown_target"
    assert direct.metrics.registry.get_sample_value("vm_autoscaler_unknown_targets_total") == 1


async def test_value_missing_skips(direct: Harness) -> None:
    [decision] = await direct.run(request("cpu", value=None))
    assert decision.reason == "value_missing"


async def test_value_not_set_skips(app_config: AppConfig) -> None:
    h = Harness(app_config, Mode.DIRECT_COMMIT, state(cpu_limit=None))
    [decision] = await h.run(request("cpu"))
    assert decision.reason == "value_not_set"


async def test_at_max_cap_skips(app_config: AppConfig) -> None:
    h = Harness(app_config, Mode.DIRECT_COMMIT, state(cpu_limit="16"))
    [decision] = await h.run(request("cpu"))
    assert decision.reason == "at_max_cap"
    assert h.gitops.applied == []


@pytest.mark.parametrize(
    "status",
    [
        AppStatus(health="Progressing", sync="Synced"),
        AppStatus(health="Degraded", sync="Synced"),
        AppStatus(health="Healthy", sync="OutOfSync", operation_in_progress=True),
    ],
)
async def test_argo_unhealthy_skips(direct: Harness, status: AppStatus) -> None:
    direct.syncer.status = status
    [decision] = await direct.run(request("cpu"))
    assert decision.reason == "deployment_not_healthy"
    assert direct.gitops.applied == []
    assert direct.syncer.syncs == []


async def test_argo_precheck_can_be_disabled(app_config: AppConfig) -> None:
    config = app_config.model_copy(update={"argocd_precheck": False})
    h = Harness(config, Mode.DIRECT_COMMIT)
    h.syncer.status = AppStatus(health="Degraded", sync="Synced")
    [decision] = await h.run(request("cpu"))
    assert decision.decision is Decision.APPLIED


async def test_conflict_retried_once(direct: Harness) -> None:
    direct.gitops.fail_next("apply_changes", GitOpsConflictError("409"))
    [decision] = await direct.run(request("cpu"))
    assert decision.decision is Decision.APPLIED
    assert len(direct.gitops.applied) == 1


async def test_conflict_twice_skips(direct: Harness) -> None:
    direct.gitops.fail_next("apply_changes", GitOpsConflictError("409"), times=2)
    [decision] = await direct.run(request("cpu"))
    assert decision.reason == "gitops_conflict"
    assert direct.syncer.syncs == []


async def test_conflict_recomputes_from_fresh_state(direct: Harness) -> None:
    # Someone else bumps the limit to 16 between our read and our write.
    original_apply = direct.gitops.apply_changes
    calls = 0

    async def apply_with_race(*args: Any, **kwargs: Any) -> ChangeResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            direct.gitops.seed(TARGET_KEY, state(cpu_limit="16", version="v2"))
        return await original_apply(*args, **kwargs)

    direct.gitops.apply_changes = apply_with_race  # type: ignore[method-assign]
    [decision] = await direct.run(request("cpu"))
    assert calls == 1  # conflict on first try, re-read shows we are at max: no second write
    assert decision.reason == "at_max_cap"
    assert direct.gitops.applied == []


async def test_backend_unavailable_no_sync(direct: Harness) -> None:
    direct.gitops.fail_next("apply_changes", GitOpsUnavailableError("down"))
    [decision] = await direct.run(request("cpu"))
    assert decision.reason == "gitops_unavailable"
    assert direct.syncer.syncs == []


async def test_backend_unavailable_on_read(direct: Harness) -> None:
    direct.gitops.fail_next("get_component_state", GitOpsUnavailableError("down"))
    [decision] = await direct.run(request("cpu"))
    assert decision.reason == "gitops_unavailable"


async def test_replicas_always_merge_request(direct: Harness) -> None:
    [decision] = await direct.run(request("replicas", 90.0))
    assert decision.decision is Decision.CHANGE_REQUEST_OPENED
    assert decision.mode is Mode.MERGE_REQUEST
    assert (decision.old, decision.new) == ("3", "4")
    assert len(direct.gitops.change_requests) == 1
    assert direct.gitops.applied == []
    assert direct.syncer.syncs == []


async def test_merge_request_mode_does_not_sync(app_config: AppConfig) -> None:
    h = Harness(app_config, Mode.MERGE_REQUEST)
    [decision] = await h.run(request("storage", 85.0))
    assert decision.decision is Decision.CHANGE_REQUEST_OPENED
    assert decision.url == "https://fake.invalid/mr/1"
    assert h.syncer.syncs == []
    [mr] = h.gitops.change_requests
    assert "storage 500Gi -> 600Gi" in mr.reason


async def test_mixed_modes_split_into_commit_and_mr(direct: Harness) -> None:
    decisions = await direct.run(request("cpu"), request("replicas", 90.0))
    by_resource = {d.resource: d.decision for d in decisions}
    assert by_resource == {"cpu": Decision.APPLIED, "replicas": Decision.CHANGE_REQUEST_OPENED}
    assert len(direct.gitops.applied) == 1 and len(direct.gitops.change_requests) == 1


async def test_sync_failure_reports_committed_change(direct: Harness) -> None:
    direct.syncer.fail_sync = ArgoCdError("boom")
    [decision] = await direct.run(request("cpu"))
    assert decision.decision is Decision.APPLIED
    assert decision.reason == "argocd_sync_failed"
    assert decision.synced is False


async def test_duplicate_resource_in_batch(direct: Harness) -> None:
    decisions = await direct.run(request("cpu"), request("cpu", fingerprint="other"))
    assert sorted(d.reason for d in decisions) == ["duplicate_in_batch", "scaled"]


async def test_idempotency_key_is_stable(app_config: AppConfig) -> None:
    keys = []
    for _ in range(2):
        h = Harness(app_config, Mode.DIRECT_COMMIT)
        await h.run(request("cpu"))
        keys.append(h.gitops.applied[0].idempotency_key)
    assert keys[0] == keys[1]


async def test_decision_metric_counted(direct: Harness) -> None:
    await direct.run(request("cpu"))
    value = direct.metrics.registry.get_sample_value(
        "vm_autoscaler_decisions_total",
        {
            "ocp_cluster": "ocp-prod-region-a",
            "resource": "cpu",
            "decision": "applied",
            "reason": "scaled",
        },
    )
    assert value == 1
