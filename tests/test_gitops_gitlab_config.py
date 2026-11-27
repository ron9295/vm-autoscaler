from typing import Any

import pytest

from app.config import AppConfig, Settings, check_backend_requirements
from app.domain.errors import ConfigError


def _config(
    app_config: AppConfig, gitlab: dict[str, Any] | None = None, **target0: Any
) -> AppConfig:
    raw = app_config.model_dump(mode="json", exclude_unset=True)
    raw["gitlab"] = gitlab if gitlab is not None else raw["gitlab"]
    if target0:
        raw["targets"][0]["gitlab"] = target0
    return AppConfig.model_validate(raw)


def _gitlab_settings() -> Settings:
    return Settings(
        webhook_token="test-webhook-token-0001",
        gitops_backend="gitlab",
        gitlab_url="https://gitlab.test",
        gitlab_token="t",
        argocd_sync_method="fake",
    )


def test_settings_require_gitlab_url_and_token() -> None:
    with pytest.raises(ValueError, match="GITLAB_URL and GITLAB_TOKEN"):
        Settings(
            webhook_token="test-webhook-token-0001",
            gitops_backend="gitlab",
            argocd_sync_method="fake",
        )


def test_file_path_comes_from_template(app_config: AppConfig) -> None:
    config = _config(
        app_config, {"project": 1234, "file_template": "{ocp_cluster}/{namespace}/values.yaml"}
    )
    locations = {str(k): v for k, v in config.gitlab_targets().items()}
    a = locations["ocp-prod-region-a/monitoring/vmstorage"]
    assert a.file == "ocp-prod-region-a/monitoring/values.yaml"
    assert (
        locations["ocp-prod-region-b/monitoring/vmstorage"].file
        == "ocp-prod-region-b/monitoring/values.yaml"
    )
    assert a.project == "1234"  # numeric ids are accepted
    assert (a.branch, a.storage_path, a.document_kind, a.document_name) == (
        "main",
        "vmstorage.pvc.size",
        None,
        None,
    )


def test_example_config_resolves(app_config: AppConfig) -> None:
    location = next(iter(app_config.gitlab_targets().values()))
    assert location.file == "ocp-prod-region-a/monitoring/TODO_VALUES_FILE.yaml"
    assert location.storage_path == "vmstorage.pvc.size"


def test_per_target_override_wins(app_config: AppConfig) -> None:
    config = _config(
        app_config, None, file="special/values.yaml", branch="release", storage_path="a.b"
    )
    location = config.gitlab_location(config.targets[0])
    assert (location.file, location.branch, location.storage_path) == (
        "special/values.yaml",
        "release",
        "a.b",
    )
    other = config.gitlab_location(config.targets[1])
    assert other.branch == "main"


def test_unknown_template_placeholder_rejected(app_config: AppConfig) -> None:
    with pytest.raises(ValueError, match="unknown placeholders"):
        _config(
            app_config, {"project": "g/r", "file_template": "{cluster}/{namespace}/values.yaml"}
        )


def test_gitlab_mode_requires_project_and_file(app_config: AppConfig) -> None:
    config = _config(app_config, {"project": None, "file_template": None})
    with pytest.raises(ConfigError, match="GitLab project and file not set"):
        check_backend_requirements(_gitlab_settings(), config)
    check_backend_requirements(_gitlab_settings(), app_config)


def test_other_backends_do_not_need_gitlab_settings(app_config: AppConfig) -> None:
    config = _config(app_config, {})
    fake = Settings(
        webhook_token="test-webhook-token-0001", gitops_backend="fake", argocd_sync_method="fake"
    )
    check_backend_requirements(fake, config)


def test_commit_message_prefix_alone_is_used_with_a_deprecation_warning(
    app_config: AppConfig, caplog: pytest.LogCaptureFixture
) -> None:
    raw = app_config.model_dump(mode="json", exclude_unset=True)
    raw.pop("change_reason_prefix", None)
    raw["gitlab"] = {"project": "g/r", "commit_message_prefix": "[bot]"}
    config = AppConfig.model_validate(raw)
    assert config.change_reason_prefix == "[bot]"
    assert "commit_message_prefix is deprecated" in caplog.text


def test_conflicting_prefixes_are_rejected(app_config: AppConfig) -> None:
    raw = app_config.model_dump(mode="json")
    raw["change_reason_prefix"] = "[one]"
    raw["gitlab"] = {"project": "g/r", "commit_message_prefix": "[two]"}
    with pytest.raises(ValueError, match="differs from change_reason_prefix"):
        AppConfig.model_validate(raw)


def test_same_prefix_in_both_places_is_accepted(app_config: AppConfig) -> None:
    raw = app_config.model_dump(mode="json")
    raw["change_reason_prefix"] = "[same]"
    raw["gitlab"] = {"project": "g/r", "commit_message_prefix": "[same]"}
    assert AppConfig.model_validate(raw).change_reason_prefix == "[same]"
