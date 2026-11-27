"""Configuration.

* `Settings`  - environment variables (secrets, URLs, timeouts). Loaded with pydantic-settings.
* `AppConfig` - the YAML file mounted from a ConfigMap (targets and policies).
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from pathlib import Path
from string import Formatter
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
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
from app.domain.quantity import QuantityError, parse_bytes

# --------------------------------------------------------------------------------------
# Environment settings
# --------------------------------------------------------------------------------------


logger = logging.getLogger(__name__)

MIN_WEBHOOK_TOKEN_LENGTH = 16
GITOPS_BACKENDS = ("gitlab", "internal", "fake")


class Settings(BaseSettings):
    """Environment variables. Secrets are SecretStr so they never show up in repr/logs."""

    # populate_by_name: code and tests may use the field names; env vars use the aliases.
    model_config = SettingsConfigDict(env_file=None, extra="ignore", populate_by_name=True)

    webhook_token: SecretStr
    config_path: Path = Path("/etc/vm-autoscaler/config.yaml")
    log_level: str = "INFO"

    # Required: no default, so a missing ConfigMap value stops the pod at startup instead of
    # silently running the unfinished internal backend.
    gitops_backend: Literal["gitlab", "internal", "fake"]
    gitops_api_url: str | None = None
    gitops_api_token: SecretStr | None = None
    gitops_api_timeout_seconds: float = 10.0
    gitops_api_max_retries: int = Field(default=3, ge=0, le=10)
    # GITOPS_BACKEND=internal only (ignored for gitlab and fake).
    # TODO(api): set to false if the internal backend cannot store annotations yet.
    # Cooldowns then live only in memory and reset when the pod restarts.
    gitops_stores_annotations: bool = True
    gitops_fake_seed_path: Path | None = None

    # GITOPS_BACKEND=gitlab: edit the Argo CD source repo through the GitLab REST API.
    gitlab_url: str | None = None
    gitlab_token: SecretStr | None = None
    # Unset: fall back to GITOPS_API_TIMEOUT_SECONDS / GITOPS_API_MAX_RETRIES (old names).
    gitlab_timeout_seconds: float | None = None
    gitlab_max_retries: int | None = Field(default=None, ge=0, le=10)

    argocd_url: str | None = None
    argocd_token: SecretStr | None = None
    # HTTP_TIMEOUT_SECONDS is the old name.
    argocd_timeout_seconds: float = Field(
        default=10.0,
        validation_alias=AliasChoices(
            "argocd_timeout_seconds", "ARGOCD_TIMEOUT_SECONDS", "HTTP_TIMEOUT_SECONDS"
        ),
    )
    argocd_sync_method: Literal["api", "cli", "fake"] = "api"
    argocd_insecure_skip_verify: bool = False
    argocd_cli_path: str = "argocd"

    ca_bundle_path: Path | None = None

    readiness_check_dependencies: bool = False

    @model_validator(mode="before")
    @classmethod
    def _require_backend(cls, data: Any) -> Any:
        """Name the allowed values when GITOPS_BACKEND is missing (pydantic's message is terse)."""
        if isinstance(data, dict) and not data.get("gitops_backend"):
            raise ValueError(
                "GITOPS_BACKEND is required; set it to one of: " + " | ".join(GITOPS_BACKENDS)
            )
        return data

    @field_validator("webhook_token")
    @classmethod
    def _check_webhook_token(cls, value: SecretStr) -> SecretStr:
        """An empty or short token would make the webhook effectively unauthenticated."""
        if len(value.get_secret_value().strip()) < MIN_WEBHOOK_TOKEN_LENGTH:
            raise ValueError(
                f"WEBHOOK_TOKEN must be at least {MIN_WEBHOOK_TOKEN_LENGTH} characters; "
                "generate one with `openssl rand -hex 32`"
            )
        return value

    @model_validator(mode="after")
    def _check_required_urls(self) -> Settings:
        if self.gitops_backend == "internal" and not self.gitops_api_url:
            raise ValueError("GITOPS_API_URL is required when GITOPS_BACKEND=internal")
        if self.gitops_backend == "gitlab" and not (self.gitlab_url and self.gitlab_token):
            raise ValueError("GITLAB_URL and GITLAB_TOKEN are required when GITOPS_BACKEND=gitlab")
        if self.argocd_sync_method in ("api", "cli") and not self.argocd_url:
            raise ValueError("ARGOCD_URL is required when ARGOCD_SYNC_METHOD is api or cli")
        return self

    @property
    def gitlab_timeout(self) -> float:
        """GitLab request timeout: GITLAB_TIMEOUT_SECONDS, else GITOPS_API_TIMEOUT_SECONDS."""
        if self.gitlab_timeout_seconds is not None:
            return self.gitlab_timeout_seconds
        return self.gitops_api_timeout_seconds

    @property
    def gitlab_retries(self) -> int:
        """GitLab read retries: GITLAB_MAX_RETRIES, else GITOPS_API_MAX_RETRIES."""
        if self.gitlab_max_retries is not None:
            return self.gitlab_max_retries
        return self.gitops_api_max_retries


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
    step_percent: float = Field(gt=0, le=200)
    max: Annotated[str, BeforeValidator(str)]
    cooldown: Duration
    mode: Mode | None = None


class DefaultsConfig(BaseModel):
    """Global defaults."""

    model_config = ConfigDict(extra="forbid")

    value_ref_id: str = "B"


_TEMPLATE_FIELDS = {"ocp_cluster", "namespace", "component"}
ProjectId = Annotated[str, BeforeValidator(str)]  # "group/repo" or a numeric id


class GitLabLocation(BaseModel):
    """Where one target's values live in GitLab, fully resolved. Used by the adapter."""

    project: str
    branch: str
    file: str
    document_kind: str | None  # None: no kind filter (Helm values file)
    document_name: str | None  # None: no metadata.name filter
    storage_path: str  # dotted path from the document root, e.g. vmstorage.pvc.size
    annotations_path: str  # only used with cooldown_source: annotations


class GitLabTargetConfig(BaseModel):
    """Optional per-target overrides of the global `gitlab` settings."""

    model_config = ConfigDict(extra="forbid")

    project: ProjectId | None = None
    branch: str | None = None
    file: str | None = None  # overrides gitlab.file_template for this target
    document_kind: str | None = None
    document_name: str | None = None
    storage_path: str | None = None
    annotations_path: str | None = None


class GitLabConfig(BaseModel):
    """Global GitLab settings (GITOPS_BACKEND=gitlab).

    Repo layout: <ocp_cluster>/<namespace>/<values file>, with the PVC size at
    vmstorage.pvc.size. Each target's file is `file_template` filled in with the target's
    ocp_cluster, namespace and component, unless the target sets its own `file`.
    """

    model_config = ConfigDict(extra="forbid")

    project: ProjectId | None = None
    branch: str = "main"
    file_template: str | None = None  # e.g. "{ocp_cluster}/{namespace}/values.yaml"
    document_kind: str | None = None  # set (e.g. VMCluster) only for multi-document files
    document_name: str | None = None
    storage_path: str = "vmstorage.pvc.size"
    annotations_path: str = "vmAutoscaler"  # top-level map; only for cooldown_source=annotations

    author_name: str = "vm-autoscaler"
    author_email: str = "vm-autoscaler@example.local"
    # Deprecated: use the top-level change_reason_prefix. Still accepted when
    # change_reason_prefix is not set; a different value for both is a config error.
    commit_message_prefix: str | None = None
    # Where the "last scaled" time for cooldowns comes from:
    #   commits     - the newest matching vm-autoscaler commit on the branch (nothing is
    #                 written into the manifest; stateless, clean diffs)
    #   annotations - vm-autoscaler/last-scaled-<resource> annotations in the manifest
    cooldown_source: Literal["commits", "annotations"] = "commits"

    @model_validator(mode="after")
    def _check_template(self) -> GitLabConfig:
        if self.file_template is not None:
            fields = {name for _, name, _, _ in Formatter().parse(self.file_template) if name}
            unknown = fields - _TEMPLATE_FIELDS
            if unknown:
                raise ValueError(
                    f"gitlab.file_template uses unknown placeholders {sorted(unknown)}; "
                    f"allowed: {sorted(_TEMPLATE_FIELDS)}"
                )
        return self


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
        self._resolve_prefix()
        # Resolving every target validates all overrides at startup (fail fast).
        for target in self.targets:
            self.resolve_target(target)
        return self

    def _resolve_prefix(self) -> None:
        """One prefix: the commit title drives cooldown matching, so two values are an error."""
        legacy = self.gitlab.commit_message_prefix
        if legacy is None:
            return
        explicit = "change_reason_prefix" in self.model_fields_set
        if explicit and legacy != self.change_reason_prefix:
            raise ValueError(
                f"gitlab.commit_message_prefix {legacy!r} differs from change_reason_prefix "
                f"{self.change_reason_prefix!r}; remove gitlab.commit_message_prefix"
            )
        logger.warning(
            "gitlab.commit_message_prefix is deprecated; use change_reason_prefix instead"
        )
        self.change_reason_prefix = legacy

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

    def gitlab_location(self, target: TargetConfig) -> GitLabLocation:
        """Resolve where a target's values file lives. Raises ConfigError if incomplete."""
        merged: dict[str, Any] = self.gitlab.model_dump(
            include={
                "project",
                "branch",
                "document_kind",
                "document_name",
                "storage_path",
                "annotations_path",
            }
        )
        if target.gitlab is not None:
            merged.update(target.gitlab.model_dump(exclude_unset=True))
        file = merged.pop("file", None)
        if file is None and self.gitlab.file_template is not None:
            file = self.gitlab.file_template.format(
                ocp_cluster=target.ocp_cluster,
                namespace=target.namespace,
                component=target.component,
            )
        problems = [
            name
            for name, value in (("project", merged.get("project")), ("file", file))
            if not value
        ]
        if problems:
            raise ConfigError(
                f"target {target.key}: GitLab {' and '.join(problems)} not set "
                "(set gitlab.project / gitlab.file_template, or per-target gitlab overrides)"
            )
        return GitLabLocation(file=file, **merged)

    def gitlab_targets(self) -> dict[TargetKey, GitLabLocation]:
        """Resolved GitLab file locations of all targets."""
        return {target.key: self.gitlab_location(target) for target in self.targets}


def _check_policy(resource: ResourceName, policy: PolicyConfig) -> None:
    """Make sure the policy's max is a valid size."""
    try:
        parse_bytes(policy.max)
    except QuantityError as exc:
        raise ValueError(f"policy {resource}: max {policy.max!r}: {exc}") from exc


def check_backend_requirements(settings: Settings, config: AppConfig) -> None:
    """Fail fast when the chosen GitOps backend needs config the targets don't have."""
    if settings.gitops_backend != "gitlab":
        return
    config.gitlab_targets()  # raises ConfigError naming the first incomplete target


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
