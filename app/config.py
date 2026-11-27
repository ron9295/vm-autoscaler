"""Configuration.

* `Settings`  - environment variables (secrets, URLs, timeouts). Loaded with pydantic-settings.
* `AppConfig` - the YAML file mounted from a ConfigMap (targets and policies).
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.errors import ConfigError
from app.domain.models import (
    Mode,
    Policy,
    ResourceName,
    SyncResource,
    Target,
    TargetKey,
)
from app.domain.quantity import QuantityError, parse_bytes, parse_cpu

# --------------------------------------------------------------------------------------
# Environment settings
# --------------------------------------------------------------------------------------


class Settings(BaseSettings):
    """Environment variables. Secrets are SecretStr so they never show up in repr/logs."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    webhook_token: SecretStr
    config_path: Path = Path("/etc/vm-autoscaler/config.yaml")
    log_level: str = "INFO"
    http_timeout_seconds: float = 10.0

    gitops_backend: Literal["internal", "gitlab", "fake"] = "internal"
    gitops_api_url: str | None = None
    gitops_api_token: SecretStr | None = None
    gitops_api_timeout_seconds: float = 10.0
    gitops_api_max_retries: int = Field(default=3, ge=0, le=10)
    # TODO(api): set to false if the backend cannot store annotations yet. Cooldowns then
    # live only in memory and reset when the pod restarts.
    gitops_stores_annotations: bool = True
    gitops_fake_seed_path: Path | None = None

    # GITOPS_BACKEND=gitlab: edit the Argo CD source repo through the GitLab REST API.
    # Timeout and retries reuse GITOPS_API_TIMEOUT_SECONDS / GITOPS_API_MAX_RETRIES.
    gitlab_url: str | None = None
    gitlab_token: SecretStr | None = None

    argocd_url: str | None = None
    argocd_token: SecretStr | None = None
    argocd_sync_method: Literal["api", "cli", "fake"] = "api"
    argocd_insecure_skip_verify: bool = False
    argocd_cli_path: str = "argocd"

    ca_bundle_path: Path | None = None

    readiness_check_dependencies: bool = False

    @model_validator(mode="after")
    def _check_required_urls(self) -> Settings:
        if self.gitops_backend == "internal" and not self.gitops_api_url:
            raise ValueError("GITOPS_API_URL is required when GITOPS_BACKEND=internal")
        if self.gitops_backend == "gitlab" and not (self.gitlab_url and self.gitlab_token):
            raise ValueError("GITLAB_URL and GITLAB_TOKEN are required when GITOPS_BACKEND=gitlab")
        if self.argocd_sync_method in ("api", "cli") and not self.argocd_url:
            raise ValueError("ARGOCD_URL is required when ARGOCD_SYNC_METHOD is api or cli")
        return self


# --------------------------------------------------------------------------------------
# YAML config
# --------------------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(?P<amount>\d+)(?P<unit>[smhd])$")
_DURATION_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_duration(value: Any) -> Any:
    """Turn "30m", "6h", "1d" into a timedelta.

    Anything else (seconds, ISO-8601 like "PT6H") is passed to pydantic as-is.
    """
    if isinstance(value, str):
        match = _DURATION_RE.match(value.strip())
        if match is None:
            return value
        unit = _DURATION_UNITS[match.group("unit")]
        return timedelta(**{unit: int(match.group("amount"))})
    return value


Duration = Annotated[timedelta, BeforeValidator(parse_duration)]


class PolicyConfig(BaseModel):
    """A policy as written in YAML. `max` accepts strings and numbers."""

    model_config = ConfigDict(extra="forbid")

    trigger_percent: float = Field(gt=0, le=100)
    step_percent: float | None = Field(default=None, gt=0, le=200)
    step: int | None = Field(default=None, gt=0)
    max: Annotated[str, BeforeValidator(str)]
    cooldown: Duration
    mode: Mode | None = None
    scale_requests_with_limits: bool = True
    requests_equal_limits: bool = False


class DefaultsConfig(BaseModel):
    """Global defaults."""

    model_config = ConfigDict(extra="forbid")

    value_ref_id: str = "B"


class GitLabTargetConfig(BaseModel):
    """Where a target's manifest lives in GitLab (GITOPS_BACKEND=gitlab)."""

    model_config = ConfigDict(extra="forbid")

    project: Annotated[str, BeforeValidator(str)]  # "infra/monitoring" or a numeric id
    branch: str = "main"
    file: str  # path inside the repo, e.g. clusters/region-a/vmcluster.yaml
    document_kind: str | None = "VMCluster"  # null for a Helm values file
    document_name: str | None = None  # metadata.name; needed if several documents match
    yaml_root: str = "spec.vmstorage"  # e.g. "vmstorage" for a Helm values file
    annotations_path: str = "metadata.annotations"  # relative to the document root


class GitLabConfig(BaseModel):
    """Global GitLab commit settings."""

    model_config = ConfigDict(extra="forbid")

    author_name: str = "vm-autoscaler"
    author_email: str = "vm-autoscaler@example.local"
    # Alias for change_reason_prefix; when set it wins (one prefix, never two).
    commit_message_prefix: str | None = None
    # Where the "last scaled" time for cooldowns comes from:
    #   commits     - the newest matching vm-autoscaler commit on the branch (nothing is
    #                 written into the manifest; stateless, clean diffs)
    #   annotations - vm-autoscaler/last-scaled-<resource> annotations in the manifest
    cooldown_source: Literal["commits", "annotations"] = "commits"


class TargetConfig(BaseModel):
    """One target as written in YAML."""

    model_config = ConfigDict(extra="forbid")

    ocp_cluster: str
    namespace: str
    component: str
    argocd_app: str
    argocd_sync_resources: list[SyncResource] = Field(default_factory=list)
    gitops_ref: dict[str, Any] = Field(default_factory=dict)
    storage_expansion_supported: bool = True
    policies: dict[ResourceName, dict[str, Any]] = Field(default_factory=dict)
    gitlab: GitLabTargetConfig | None = None

    @property
    def key(self) -> TargetKey:
        """The cluster + namespace + component identity."""
        return TargetKey(
            ocp_cluster=self.ocp_cluster, namespace=self.namespace, component=self.component
        )


class AppConfig(BaseModel):
    """The whole YAML config file."""

    model_config = ConfigDict(extra="forbid")

    mode: Mode = Mode.DRY_RUN
    change_reason_prefix: str = "[vm-autoscaler]"
    enabled_components: list[str] = Field(default_factory=lambda: ["vmstorage"])
    argocd_precheck: bool = True
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    gitlab: GitLabConfig = Field(default_factory=GitLabConfig)
    policies: dict[ResourceName, PolicyConfig]
    targets: list[TargetConfig]

    @model_validator(mode="after")
    def _validate(self) -> AppConfig:
        keys = [target.key for target in self.targets]
        duplicates = {str(key) for key in keys if keys.count(key) > 1}
        if duplicates:
            raise ValueError(f"duplicate targets: {sorted(duplicates)}")
        if self.gitlab.commit_message_prefix is not None:
            self.change_reason_prefix = self.gitlab.commit_message_prefix
        # Resolving every target validates all overrides at startup (fail fast).
        for target in self.targets:
            self.resolve_target(target)
        return self

    def resolve_target(self, target: TargetConfig) -> Target:
        """Merge global policies with target overrides into an effective `Target`."""
        policies: dict[ResourceName, Policy] = {}
        for resource, base in self.policies.items():
            merged = {**base.model_dump(), **target.policies.get(resource, {})}
            policy_config = PolicyConfig.model_validate(merged)
            _check_policy(resource, policy_config)
            policies[resource] = Policy.model_validate(policy_config.model_dump())
        unknown = set(target.policies) - set(self.policies)
        if unknown:
            raise ValueError(f"target {target.key}: overrides for undefined policies {unknown}")
        return Target(
            key=target.key,
            argocd_app=target.argocd_app,
            argocd_sync_resources=target.argocd_sync_resources,
            gitops_ref=target.gitops_ref,
            storage_expansion_supported=target.storage_expansion_supported,
            policies=policies,
        )

    def targets_by_key(self) -> dict[TargetKey, Target]:
        """All targets, resolved, indexed by key."""
        return {target.key: self.resolve_target(target) for target in self.targets}

    def longest_cooldown(self) -> timedelta:
        """The longest cooldown of any policy of any target (history look-back window)."""
        cooldowns = [
            policy.cooldown
            for target in self.targets_by_key().values()
            for policy in target.policies.values()
        ]
        return max(cooldowns, default=timedelta(days=1))

    def gitlab_targets(self) -> dict[TargetKey, GitLabTargetConfig]:
        """GitLab file locations of the targets that have a `gitlab` block."""
        return {t.key: t.gitlab for t in self.targets if t.gitlab is not None}


def _check_policy(resource: ResourceName, policy: PolicyConfig) -> None:
    """Make sure each policy has the step and max format its resource needs."""
    try:
        if resource == "replicas":
            if policy.step is None:
                raise ValueError("replicas policy needs 'step'")
            int(policy.max)
            return
        if policy.step_percent is None:
            raise ValueError(f"{resource} policy needs 'step_percent'")
        if resource == "cpu":
            parse_cpu(policy.max)
        else:
            parse_bytes(policy.max)
    except (QuantityError, ValueError) as exc:
        raise ValueError(f"policy {resource}: {exc}") from exc


def check_backend_requirements(settings: Settings, config: AppConfig) -> None:
    """Fail fast when the chosen GitOps backend needs config the targets don't have."""
    if settings.gitops_backend != "gitlab":
        return
    missing = [str(t.key) for t in config.targets if t.gitlab is None]
    if missing:
        raise ConfigError(
            f"GITOPS_BACKEND=gitlab but these targets have no 'gitlab' block: {missing}"
        )


def load_app_config(path: Path) -> AppConfig:
    """Read and validate the YAML config. Raises ConfigError with a readable message."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config file {path}: {exc}") from exc
