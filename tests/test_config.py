from datetime import timedelta
from pathlib import Path

import pytest

from app.config import AppConfig, load_app_config
from app.domain.errors import ConfigError
from app.domain.models import Mode


def test_example_config_loads(app_config: AppConfig) -> None:
    assert app_config.mode is Mode.DRY_RUN
    targets = app_config.targets_by_key()
    assert len(targets) == 2
    for target in targets.values():
        assert set(target.policies) == {"storage"}
        assert target.policies["storage"].cooldown == timedelta(hours=24)
    assert app_config.longest_cooldown() == timedelta(hours=24)


def test_target_override_is_merged(app_config: AppConfig) -> None:
    raw = app_config.model_dump(mode="json")
    raw["targets"][0]["policies"] = {"storage": {"max": "4Ti"}}
    config = AppConfig.model_validate(raw)
    first = config.targets_by_key()[config.targets[0].key]
    assert first.policies["storage"].max == "4Ti"
    assert first.policies["storage"].step_percent == 20


def test_bad_max_is_rejected(app_config: AppConfig) -> None:
    raw = app_config.model_dump(mode="json")
    raw["policies"]["storage"]["max"] = "lots"
    with pytest.raises(ValueError, match="policy storage"):
        AppConfig.model_validate(raw)


@pytest.mark.parametrize("resource", ["cpu", "memory", "replicas"])
def test_removed_resources_are_rejected(app_config: AppConfig, resource: str) -> None:
    raw = app_config.model_dump(mode="json")
    raw["policies"][resource] = {
        "trigger_percent": 80,
        "step_percent": 25,
        "max": "1",
        "cooldown": "6h",
    }
    with pytest.raises(ValueError):
        AppConfig.model_validate(raw)


def test_unreadable_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_app_config(tmp_path / "missing.yaml")
