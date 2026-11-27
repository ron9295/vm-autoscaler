import pytest

from app.domain.models import Mode, SkipReason
from app.domain.strategies import StorageStrategy, build_strategy_registry
from tests.factories import policy, state, target


def _values(result):  # type: ignore[no-untyped-def]
    return {change.field: (change.old, change.new) for change in result.changes}


def test_grows_to_whole_gi() -> None:
    result = StorageStrategy().compute(state(storage="30Gi"), policy(step_percent=20), target())
    assert _values(result) == {"storage": ("30Gi", "36Gi")}
    assert result.headline is not None
    assert result.changes[0].yaml_path.endswith(
        "volumeClaimTemplate.spec.resources.requests.storage"
    )


def test_capped_at_max() -> None:
    result = StorageStrategy().compute(state(storage="1900Gi"), policy(max="2Ti"), target())
    assert _values(result) == {"storage": ("1900Gi", "2Ti")}


def test_at_max_skips() -> None:
    result = StorageStrategy().compute(state(storage="2Ti"), policy(max="2Ti"), target())
    assert result.skip_reason is SkipReason.AT_MAX_CAP


def test_never_shrinks() -> None:
    result = StorageStrategy().compute(state(storage="3Ti"), policy(max="2Ti"), target())
    assert result.skip_reason is SkipReason.AT_MAX_CAP
    assert result.changes == []


def test_expansion_not_supported() -> None:
    result = StorageStrategy().compute(state(), policy(), target(storage_expansion_supported=False))
    assert result.skip_reason is SkipReason.EXPANSION_NOT_SUPPORTED


def test_value_not_set() -> None:
    result = StorageStrategy().compute(state(storage=None), policy(), target())
    assert result.skip_reason is SkipReason.VALUE_NOT_SET


def test_invalid_current_value() -> None:
    result = StorageStrategy().compute(state(storage="lots"), policy(), target())
    assert result.skip_reason is SkipReason.INVALID_CURRENT_VALUE


@pytest.mark.parametrize(
    ("global_mode", "policy_mode", "expected"),
    [
        (Mode.DIRECT_COMMIT, None, Mode.DIRECT_COMMIT),
        (Mode.DIRECT_COMMIT, Mode.MERGE_REQUEST, Mode.MERGE_REQUEST),
        (Mode.DRY_RUN, Mode.DIRECT_COMMIT, Mode.DRY_RUN),
    ],
)
def test_policy_mode_can_only_make_it_safer(
    global_mode: Mode, policy_mode: Mode | None, expected: Mode
) -> None:
    assert StorageStrategy().effective_mode(global_mode, policy(mode=policy_mode)) is expected


def test_registry_is_storage_only() -> None:
    assert set(build_strategy_registry()) == {"storage"}
