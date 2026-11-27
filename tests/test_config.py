from datetime import timedelta
from pathlib import Path

import pytest

from app.config import AppConfig, load_app_config
from app.domain.errors import ConfigError
from app.domain.models import Mode


def test_example_config_loads(app_config: AppConfig) -> None:
    assert app_config.mode is Mode.DRY_RUN
    [target] = app_config.targets_by_key().values()
    assert target.policies["cpu"].cooldown == timedelta(hours=6)
    assert target.policies["replicas"].mode is Mode.MERGE_REQUEST
    assert target.policies["replicas"].max == "20"


def test_target_override_is_merged(app_config: AppConfig) -> None:
    raw = app_config.model_dump(mode="json")
    raw["targets"][0]["policies"] = {"cpu": {"max": "24"}}
    config = AppConfig.model_validate(raw)
    [target] = config.targets_by_key().values()
    assert target.policies["cpu"].max == "24"
    assert target.policies["cpu"].step_percent == 25


def test_bad_max_is_rejected(app_config: AppConfig) -> None:
    raw = app_config.model_dump(mode="json")
    raw["policies"]["memory"]["max"] = "lots"
    with pytest.raises(ValueError, match="policy memory"):
        AppConfig.model_validate(raw)


def test_unreadable_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_app_config(tmp_path / "missing.yaml")
