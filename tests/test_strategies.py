import pytest

from app.domain.models import Mode, SkipReason
from app.domain.strategies import (
    CpuStrategy,
    MemoryStrategy,
    ReplicaStrategy,
    StorageStrategy,
    build_strategy_registry,
)
from tests.factories import policy, state, target


def _values(result):  # type: ignore[no-untyped-def]
    return {change.field: (change.old, change.new) for change in result.changes}


class TestCpu:
    def test_grows_limit_and_request(self) -> None:
        result = CpuStrategy().compute(state(), policy(), target())
        assert _values(result) == {"cpu_limit": ("4", "5"), "cpu_request": ("2", "2500m")}
        assert result.headline is not None and result.headline.field == "cpu_limit"
        assert result.changes[0].yaml_path == "spec.vmstorage.resources.limits.cpu"

    def test_rounds_up_to_100m(self) -> None:
        result = CpuStrategy().compute(
            state(cpu_limit="500m", cpu_request=None), policy(), target()
        )
        assert _values(result) == {"cpu_limit": ("500m", "700m")}

    def test_capped_at_max(self) -> None:
        result = CpuStrategy().compute(state(cpu_limit="15"), policy(max="16"), target())
        assert _values(result)["cpu_limit"] == ("15", "16")

    def test_at_max_skips(self) -> None:
        result = CpuStrategy().compute(state(cpu_limit="16"), policy(max="16"), target())
        assert result.skip_reason is SkipReason.AT_MAX_CAP

    def test_above_max_skips_and_never_decreases(self) -> None:
        result = CpuStrategy().compute(state(cpu_limit="20"), policy(max="16"), target())
        assert result.skip_reason is SkipReason.AT_MAX_CAP
        assert result.changes == []

    def test_value_not_set(self) -> None:
        result = CpuStrategy().compute(state(cpu_limit=None), policy(), target())
        assert result.skip_reason is SkipReason.VALUE_NOT_SET

    def test_requests_not_scaled_when_disabled(self) -> None:
        result = CpuStrategy().compute(state(), policy(scale_requests_with_limits=False), target())
        assert set(_values(result)) == {"cpu_limit"}

    def test_invalid_current_value(self) -> None:
        result = CpuStrategy().compute(state(cpu_limit="four"), policy(), target())
        assert result.skip_reason is SkipReason.INVALID_CURRENT_VALUE


class TestMemory:
    def test_requests_equal_limits(self) -> None:
        result = MemoryStrategy().compute(
            state(memory_request="8Gi"), policy(max="64Gi", requests_equal_limits=True), target()
        )
        assert _values(result) == {
            "memory_limit": ("16Gi", "20Gi"),
            "memory_request": ("8Gi", "20Gi"),
        }

    def test_request_never_decreases(self) -> None:
        # A request bigger than the new limit is left alone rather than lowered.
        result = MemoryStrategy().compute(
            state(memory_request="32Gi"), policy(max="64Gi", requests_equal_limits=True), target()
        )
        assert set(_values(result)) == {"memory_limit"}

    def test_at_max(self) -> None:
        result = MemoryStrategy().compute(state(memory_limit="64Gi"), policy(max="64Gi"), target())
        assert result.skip_reason is SkipReason.AT_MAX_CAP

    def test_value_not_set(self) -> None:
        result = MemoryStrategy().compute(state(memory_limit=None), policy(max="64Gi"), target())
        assert result.skip_reason is SkipReason.VALUE_NOT_SET


class TestStorage:
    def test_grows_to_whole_gi(self) -> None:
        result = StorageStrategy().compute(state(), policy(step_percent=20, max="2Ti"), target())
        assert _values(result) == {"storage": ("500Gi", "600Gi")}
        assert result.changes[0].yaml_path.endswith(
            "volumeClaimTemplate.spec.resources.requests.storage"
        )

    def test_capped_at_max(self) -> None:
        result = StorageStrategy().compute(
            state(storage="1900Gi"), policy(step_percent=20, max="2Ti"), target()
        )
        assert _values(result) == {"storage": ("1900Gi", "2Ti")}

    def test_never_shrinks(self) -> None:
        result = StorageStrategy().compute(
            state(storage="3Ti"), policy(step_percent=20, max="2Ti"), target()
        )
        assert result.skip_reason is SkipReason.AT_MAX_CAP

    def test_expansion_not_supported(self) -> None:
        result = StorageStrategy().compute(
            state(), policy(step_percent=20, max="2Ti"), target(storage_expansion_supported=False)
        )
        assert result.skip_reason is SkipReason.EXPANSION_NOT_SUPPORTED

    def test_value_not_set(self) -> None:
        result = StorageStrategy().compute(state(storage=None), policy(max="2Ti"), target())
        assert result.skip_reason is SkipReason.VALUE_NOT_SET


class TestReplicas:
    def test_adds_step(self) -> None:
        result = ReplicaStrategy().compute(state(), policy(step=1, max="20"), target())
        assert _values(result) == {"replicas": ("3", "4")}

    def test_at_max(self) -> None:
        result = ReplicaStrategy().compute(state(replicas=20), policy(step=1, max="20"), target())
        assert result.skip_reason is SkipReason.AT_MAX_CAP

    def test_value_not_set(self) -> None:
        result = ReplicaStrategy().compute(state(replicas=None), policy(step=1, max="20"), target())
        assert result.skip_reason is SkipReason.VALUE_NOT_SET

    @pytest.mark.parametrize(
        ("global_mode", "expected"),
        [
            (Mode.DIRECT_COMMIT, Mode.MERGE_REQUEST),
            (Mode.MERGE_REQUEST, Mode.MERGE_REQUEST),
            (Mode.DRY_RUN, Mode.DRY_RUN),
        ],
    )
    def test_never_direct_commit(self, global_mode: Mode, expected: Mode) -> None:
        assert ReplicaStrategy().effective_mode(global_mode, policy(step=1)) is expected


def test_policy_mode_can_only_make_it_safer() -> None:
    cpu = CpuStrategy()
    assert (
        cpu.effective_mode(Mode.DIRECT_COMMIT, policy(mode=Mode.MERGE_REQUEST))
        is Mode.MERGE_REQUEST
    )
    assert cpu.effective_mode(Mode.DRY_RUN, policy(mode=Mode.DIRECT_COMMIT)) is Mode.DRY_RUN


def test_registry_has_all_resources() -> None:
    assert set(build_strategy_registry()) == {"cpu", "memory", "storage", "replicas"}
