"""Composition root: the one place that picks adapters and wires everything together."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial

from app.adapters.argocd_api import ArgoCdApiSyncer
from app.adapters.argocd_cli import ArgoCdCliSyncer
from app.adapters.argocd_fake import FakeArgoCdSyncer
from app.adapters.gitops_fake import FakeGitOpsBackend
from app.adapters.gitops_gitlab import GitLabGitOpsBackend
from app.adapters.gitops_internal import InternalGitOpsBackend
from app.adapters.http_common import build_verify
from app.config import AppConfig, Settings
from app.domain.ports import DeploymentSyncer, GitOpsBackend
from app.observability import Metrics
from app.readiness import ReadinessChecker
from app.services.scaling_service import Clock, ScalingService, utc_now

logger = logging.getLogger(__name__)


@dataclass
class Container:
    """Everything the routes need, built once at startup."""

    settings: Settings
    config: AppConfig
    metrics: Metrics
    gitops: GitOpsBackend
    syncer: DeploymentSyncer
    service: ScalingService
    readiness: ReadinessChecker

    async def close(self) -> None:
        """Close HTTP clients on shutdown."""
        await self.gitops.close()
        await self.syncer.close()


def _secret(value: object) -> str | None:
    get = getattr(value, "get_secret_value", None)
    return get() if callable(get) else None


def build_gitops_backend(settings: Settings, config: AppConfig, metrics: Metrics) -> GitOpsBackend:
    """Pick the GitOps adapter from GITOPS_BACKEND (internal | gitlab | fake)."""
    if settings.gitops_backend == "fake":
        logger.warning("using the in-memory FAKE GitOps backend; nothing is written to Git")
        if settings.gitops_fake_seed_path is not None:
            return FakeGitOpsBackend.from_yaml(settings.gitops_fake_seed_path)
        return FakeGitOpsBackend()
    if settings.gitops_backend == "gitlab":
        assert settings.gitlab_url is not None  # checked by Settings
        return GitLabGitOpsBackend(
            gitlab_url=settings.gitlab_url,
            token=_secret(settings.gitlab_token) or "",
            timeout=settings.gitops_api_timeout_seconds,
            max_retries=settings.gitops_api_max_retries,
            verify=build_verify(settings.ca_bundle_path),
            metrics=metrics,
            targets=config.gitlab_targets(),
            commit_settings=config.gitlab,
            reason_prefix=config.change_reason_prefix,
            history_window=config.longest_cooldown(),
        )
    assert settings.gitops_api_url is not None  # checked by Settings
    return InternalGitOpsBackend(
        base_url=settings.gitops_api_url,
        token=_secret(settings.gitops_api_token),
        timeout=settings.gitops_api_timeout_seconds,
        max_retries=settings.gitops_api_max_retries,
        verify=build_verify(settings.ca_bundle_path),
        metrics=metrics,
    )


def build_syncer(settings: Settings) -> DeploymentSyncer:
    """Pick the Argo CD adapter from ARGOCD_SYNC_METHOD."""
    if settings.argocd_sync_method == "fake":
        logger.warning("using the FAKE Argo CD syncer; no app is ever synced")
        return FakeArgoCdSyncer()
    assert settings.argocd_url is not None  # checked by Settings
    token = _secret(settings.argocd_token)
    if settings.argocd_sync_method == "cli":
        return ArgoCdCliSyncer(
            settings.argocd_url,
            token,
            settings.http_timeout_seconds,
            insecure=settings.argocd_insecure_skip_verify,
            cli_path=settings.argocd_cli_path,
            ca_bundle_path=settings.ca_bundle_path,
        )
    return ArgoCdApiSyncer(
        settings.argocd_url,
        token,
        settings.http_timeout_seconds,
        build_verify(settings.ca_bundle_path, settings.argocd_insecure_skip_verify),
    )


def build_readiness(settings: Settings, gitops: GitOpsBackend) -> ReadinessChecker:
    """Readiness checker.

    GitLab mode always checks that every configured project is readable (token valid).
    URL reachability checks only run when READINESS_CHECK_DEPENDENCIES=true.
    """
    probes: dict[str, Callable[[], Awaitable[bool]]] = {}
    if isinstance(gitops, GitLabGitOpsBackend):
        for project in gitops.projects():
            probes[f"gitlab_project:{project}"] = partial(gitops.check_project, project)
    if not settings.readiness_check_dependencies:
        return ReadinessChecker(probes=probes)
    urls: dict[str, str] = {}
    if settings.gitops_backend == "internal" and settings.gitops_api_url:
        urls["gitops_api"] = settings.gitops_api_url
    if settings.argocd_sync_method != "fake" and settings.argocd_url:
        urls["argocd"] = settings.argocd_url.rstrip("/") + "/api/version"
    return ReadinessChecker(urls=urls, probes=probes, verify=build_verify(settings.ca_bundle_path))


def build_container(
    settings: Settings,
    config: AppConfig,
    metrics: Metrics,
    *,
    gitops: GitOpsBackend | None = None,
    syncer: DeploymentSyncer | None = None,
    clock: Clock = utc_now,
) -> Container:
    """Build all objects. Tests may pass their own gitops/syncer/clock."""
    gitops = gitops or build_gitops_backend(settings, config, metrics)
    syncer = syncer or build_syncer(settings)
    # Only the internal backend may lack annotation support. GitLab keeps cooldowns in Git
    # (commit history or annotations in the values file); the fake keeps them in memory.
    annotations_supported = (
        settings.gitops_stores_annotations if settings.gitops_backend == "internal" else True
    )
    if not annotations_supported:
        logger.warning(
            "GITOPS_STORES_ANNOTATIONS=false: cooldowns are kept in memory only and reset "
            "when the pod restarts"  # TODO(api): remove once the backend stores annotations
        )
    if config.mode.value == "dry_run":
        logger.warning("global mode is dry_run: no changes will be made")
    service = ScalingService(
        config,
        gitops,
        syncer,
        metrics,
        clock=clock,
        annotations_supported=annotations_supported,
    )
    return Container(
        settings=settings,
        config=config,
        metrics=metrics,
        gitops=gitops,
        syncer=syncer,
        service=service,
        readiness=build_readiness(settings, gitops),
    )
