import pytest

from app.config import AppConfig, Settings, check_backend_requirements
from app.domain.errors import ConfigError


def _with_gitlab(app_config: AppConfig, **global_gitlab: str) -> AppConfig:
    raw = app_config.model_dump(mode="json")
    raw["targets"][0]["gitlab"] = {"project": 1234, "file": "clusters/a/vmcluster.yaml"}
    raw["gitlab"] = global_gitlab
    return AppConfig.model_validate(raw)


def _gitlab_settings() -> Settings:
    return Settings(
        webhook_token="x",
        gitops_backend="gitlab",
        gitlab_url="https://gitlab.test",
        gitlab_token="t",
        argocd_sync_method="fake",
    )


def test_settings_require_gitlab_url_and_token() -> None:
    with pytest.raises(ValueError, match="GITLAB_URL and GITLAB_TOKEN"):
        Settings(webhook_token="x", gitops_backend="gitlab", argocd_sync_method="fake")


def test_target_gitlab_block_defaults(app_config: AppConfig) -> None:
    config = _with_gitlab(app_config)
    [location] = config.gitlab_targets().values()
    assert location.project == "1234"  # numeric ids are accepted
    assert (location.branch, location.document_kind, location.yaml_root) == (
        "main",
        "VMCluster",
        "spec.vmstorage",
    )
    assert location.annotations_path == "metadata.annotations"


def test_gitlab_mode_requires_block_on_every_target(app_config: AppConfig) -> None:
    raw = app_config.model_dump(mode="json")
    raw["targets"][0].pop("gitlab", None)
    config = AppConfig.model_validate(raw)
    with pytest.raises(ConfigError, match="have no 'gitlab' block"):
        check_backend_requirements(_gitlab_settings(), config)
    check_backend_requirements(_gitlab_settings(), _with_gitlab(app_config))


def test_commit_message_prefix_overrides_reason_prefix(app_config: AppConfig) -> None:
    config = _with_gitlab(app_config, commit_message_prefix="[bot]")
    assert config.change_reason_prefix == "[bot]"
