"""GitOps backend that edits the Argo CD source repo through the GitLab REST API v4.

Selected with `GITOPS_BACKEND=gitlab`. No git binary and no clone: files are read with the
repository files API and written with the commits API.

Optimistic concurrency
    `ComponentState.version` is the file's `last_commit_id`. `apply_changes` re-reads the
    file, refuses (GitOpsConflictError) if `last_commit_id` moved since the state read,
    and sends `last_commit_id` with the commit, so GitLab itself rejects the commit if the
    file changed in the short gap between our re-read and our write.

    Why re-read instead of caching the text from `get_component_state`? It keeps this
    adapter stateless, works the same for merge requests (which get no expected_version),
    needs no change to the GitOpsBackend port, and costs one extra GET per change.

Cooldowns (`gitlab.cooldown_source`)
    commits (default): the last-scaled time of each resource is the date of the newest
    commit on the branch whose message matches our own commit title for this target,
    e.g. "[vm-autoscaler] ocp-a/monitoring vmstorage cpu 4 -> 5 (...)". The commit list
    uses `first_parent=true`, so an approved merge request counts from its merge (or
    squash) commit, i.e. from when it actually landed. Nothing is written to the manifest.
    A wrongly matching message (e.g. a human revert of our commit) can only start a
    cooldown early, never cause an extra change.
    annotations: read/write vm-autoscaler/last-scaled-<resource> in the manifest.

Merge request mode
    One commit to a new branch (`start_branch` = target branch, so the branch is created
    by the same call), then one merge request. If a merge request for the same change is
    already open (found by the idempotency key in its description), it is returned instead
    of opening a duplicate (e.g. after a pod restart).
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import ssl
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from app.adapters import manifest
from app.adapters.http_common import send_with_retries, truncate
from app.config import GitLabConfig, GitLabTargetConfig
from app.domain.errors import (
    GitOpsConflictError,
    GitOpsError,
    GitOpsManifestError,
    GitOpsNotFoundError,
    GitOpsRejectedError,
    GitOpsResponseError,
    GitOpsUnavailableError,
)
from app.domain.models import (
    ChangeResult,
    ComponentState,
    ResourceChange,
    StateField,
    Target,
    TargetKey,
)
from app.observability import Metrics

logger = logging.getLogger(__name__)

AUTH_HINT = (
    "check token scope `api` and the branch protection rules (direct commits to a "
    "protected branch need a role that is allowed to push, usually Maintainer; merge "
    "request mode needs Developer)"
)
BRANCH_PREFIX = "vm-autoscaler/"
COOLDOWN_PREFIX = "vm-autoscaler/last-scaled-"  # same key the ScalingService reads
HISTORY_PAGE_SIZE = 100
MAX_HISTORY_PAGES = 10
# "cpu 4 -> 5" inside our commit title (resource, then old -> new).
_RESOURCE_CHANGE_RE = re.compile(r"\b(cpu|memory|storage|replicas)\s+\S+\s+->\s+\S+")
READINESS_TIMEOUT_SECONDS = 2.0

# Where each ComponentState field lives, relative to the target's `yaml_root`.
FIELD_PATHS: dict[StateField, str] = {
    "cpu_request": "resources.requests.cpu",
    "cpu_limit": "resources.limits.cpu",
    "memory_request": "resources.requests.memory",
    "memory_limit": "resources.limits.memory",
    "storage": "storage.volumeClaimTemplate.spec.resources.requests.storage",
    "replicas": "replicaCount",
}
FIELD_KINDS: dict[StateField, manifest.QuantityKind] = {
    "cpu_request": "cpu",
    "cpu_limit": "cpu",
    "memory_request": "bytes",
    "memory_limit": "bytes",
    "storage": "bytes",
    "replicas": "count",
}


@dataclass(frozen=True)
class FileSnapshot:
    """A file as read from GitLab."""

    text: str
    last_commit_id: str


class GitLabGitOpsBackend:
    """Implements the GitOpsBackend port on top of the GitLab REST API v4."""

    def __init__(
        self,
        gitlab_url: str,
        token: str,
        timeout: float,
        max_retries: int,
        verify: ssl.SSLContext | bool,
        metrics: Metrics,
        targets: dict[TargetKey, GitLabTargetConfig],
        commit_settings: GitLabConfig,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        reason_prefix: str = "[vm-autoscaler]",
        history_window: timedelta = timedelta(days=1),
        retry_wait_seconds: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._http = httpx.AsyncClient(
            base_url=gitlab_url.rstrip("/") + "/api/v4",
            headers={
                "PRIVATE-TOKEN": token,
                "Accept": "application/json",
                "User-Agent": "vm-autoscaler",
            },
            timeout=httpx.Timeout(timeout),
            verify=verify,
            transport=transport,
        )
        self._max_retries = max_retries
        self._retry_wait = retry_wait_seconds
        self._metrics = metrics
        self._targets = targets
        self._commit_settings = commit_settings
        self._clock = clock
        self._reason_prefix = reason_prefix
        self._history_window = history_window

    # ----------------------------------------------------------------------------------
    # GitOpsBackend port
    # ----------------------------------------------------------------------------------

    async def get_component_state(self, target: Target) -> ComponentState:
        """Read the manifest file and build the current state from it."""
        async with self._measure("get_component_state"):
            location = self._location(target)
            snapshot = await self._read_file(location)
            document = self._document(location, snapshot.text)
            state = state_from_document(document, location, snapshot.last_commit_id)
            if self._uses_commit_history:
                history = await self._last_scaled_from_commits(target, location)
                state.annotations = merge_newest(state.annotations, history)
            return state

    async def apply_changes(
        self,
        target: Target,
        changes: list[ResourceChange],
        reason: str,
        annotations: dict[str, str],
        expected_version: str | None,
        idempotency_key: str,
    ) -> ChangeResult:
        """Commit all changes and annotations to the target branch in ONE commit."""
        async with self._measure("apply_changes"):
            location = self._location(target)
            snapshot = await self._read_file(location)
            if expected_version is not None and snapshot.last_commit_id != expected_version:
                raise GitOpsConflictError(
                    f"{location.file} changed since it was read "
                    f"({expected_version} -> {snapshot.last_commit_id})"
                )
            content = edit_manifest(
                location, snapshot.text, changes, self._file_annotations(annotations)
            )
            commit = await self._commit(
                location,
                branch=location.branch,
                message=f"{reason}\n\nIdempotency-Key: {idempotency_key}",
                content=content,
                last_commit_id=snapshot.last_commit_id,
            )
            return ChangeResult(
                accepted=True,
                change_id=str(commit.get("id")),
                url=commit.get("web_url"),
                message=reason.partition("\n")[0],
            )

    async def open_change_request(
        self,
        target: Target,
        changes: list[ResourceChange],
        reason: str,
        annotations: dict[str, str],
        idempotency_key: str,
    ) -> ChangeResult:
        """Commit to a new branch and open a merge request into the target branch."""
        async with self._measure("open_change_request"):
            location = self._location(target)
            existing = await self._find_open_merge_request(location, idempotency_key)
            if existing is not None:
                logger.info("merge request already open", extra={"fields": {"url": existing.url}})
                return existing

            snapshot = await self._read_file(location)
            content = edit_manifest(
                location, snapshot.text, changes, self._file_annotations(annotations)
            )
            title, _, details = reason.partition("\n\n")
            branch = self._branch_name(target, changes)
            await self._commit(
                location,
                branch=branch,
                start_branch=location.branch,
                message=reason,
                content=content,
                last_commit_id=snapshot.last_commit_id,
            )
            description = f"{details}\n\nIdempotency-Key: `{idempotency_key}`".strip()
            try:
                merge_request = await self._request(
                    "POST",
                    f"/projects/{_encode(location.project)}/merge_requests",
                    location,
                    json={
                        "source_branch": branch,
                        "target_branch": location.branch,
                        "title": title,
                        "description": description,
                        "remove_source_branch": True,
                    },
                )
            except GitOpsError:
                await self._delete_branch(location, branch)
                raise
            return ChangeResult(
                accepted=True,
                change_id=f"!{merge_request.get('iid')}",
                url=merge_request.get("web_url"),
                message=title,
            )

    async def close(self) -> None:
        """Close the shared HTTP client."""
        await self._http.aclose()

    # ----------------------------------------------------------------------------------
    # Readiness
    # ----------------------------------------------------------------------------------

    def projects(self) -> list[str]:
        """All configured GitLab projects (for /readyz)."""
        return sorted({location.project for location in self._targets.values()})

    async def check_project(self, project: str) -> bool:
        """True only if GET /projects/:id answers 200 (token valid and project visible)."""
        try:
            response = await self._http.get(
                f"/projects/{_encode(project)}", timeout=READINESS_TIMEOUT_SECONDS
            )
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    # ----------------------------------------------------------------------------------
    # GitLab calls
    # ----------------------------------------------------------------------------------

    async def _read_file(self, location: GitLabTargetConfig) -> FileSnapshot:
        body = await self._request(
            "GET",
            f"/projects/{_encode(location.project)}/repository/files/{_encode(location.file)}",
            location,
            params={"ref": location.branch},
        )
        try:
            text = base64.b64decode(body["content"], validate=True).decode("utf-8")
            return FileSnapshot(text=text, last_commit_id=str(body["last_commit_id"]))
        except (KeyError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
            raise GitOpsResponseError(
                f"unexpected file response for {location.file}: {exc}"
            ) from exc

    async def _commit(
        self,
        location: GitLabTargetConfig,
        *,
        branch: str,
        message: str,
        content: str,
        last_commit_id: str,
        start_branch: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "branch": branch,
            "commit_message": message,
            "author_name": self._commit_settings.author_name,
            "author_email": self._commit_settings.author_email,
            "actions": [
                {
                    "action": "update",
                    "file_path": location.file,
                    "content": content,
                    "last_commit_id": last_commit_id,
                }
            ],
        }
        if start_branch is not None:
            payload["start_branch"] = start_branch
        commit: dict[str, Any] = await self._request(
            "POST",
            f"/projects/{_encode(location.project)}/repository/commits",
            location,
            json=payload,
        )
        return commit

    async def _find_open_merge_request(
        self, location: GitLabTargetConfig, idempotency_key: str
    ) -> ChangeResult | None:
        found = await self._request(
            "GET",
            f"/projects/{_encode(location.project)}/merge_requests",
            location,
            params={
                "state": "opened",
                "target_branch": location.branch,
                "search": idempotency_key,
                "in": "description",
            },
        )
        for merge_request in found if isinstance(found, list) else []:
            if str(merge_request.get("source_branch", "")).startswith(BRANCH_PREFIX) and (
                idempotency_key in str(merge_request.get("description", ""))
            ):
                return ChangeResult(
                    accepted=True,
                    change_id=f"!{merge_request.get('iid')}",
                    url=merge_request.get("web_url"),
                    message="merge request already open",
                )
        return None

    async def _last_scaled_from_commits(
        self, target: Target, location: GitLabTargetConfig
    ) -> dict[str, str]:
        """{cooldown annotation: newest commit date} from our commits on the branch."""
        since = (self._clock() - self._history_window).isoformat()
        commits: list[Any] = []
        for page in range(1, MAX_HISTORY_PAGES + 1):
            batch = await self._request(
                "GET",
                f"/projects/{_encode(location.project)}/repository/commits",
                location,
                params={
                    "ref_name": location.branch,
                    "path": location.file,
                    "since": since,
                    "first_parent": "true",
                    "per_page": str(HISTORY_PAGE_SIZE),
                    "page": str(page),
                },
            )
            if not isinstance(batch, list):
                raise GitOpsResponseError("commit list response is not a list")
            commits.extend(batch)
            if len(batch) < HISTORY_PAGE_SIZE:
                break
        return last_scaled_from_commits(commits, self._reason_prefix, target)

    def _file_annotations(self, annotations: dict[str, str]) -> dict[str, str]:
        """In commits mode the cooldown lives in Git history, not in the manifest."""
        return {} if self._uses_commit_history else annotations

    @property
    def _uses_commit_history(self) -> bool:
        return self._commit_settings.cooldown_source == "commits"

    async def _delete_branch(self, location: GitLabTargetConfig, branch: str) -> None:
        """Best-effort cleanup of a branch whose merge request could not be created."""
        try:
            await self._request(
                "DELETE",
                f"/projects/{_encode(location.project)}/repository/branches/{_encode(branch)}",
                location,
                expect_json=False,
            )
        except GitOpsError as exc:
            logger.warning(
                "could not delete orphan branch; remove it by hand",
                extra={"fields": {"branch": branch, "error": str(exc)}},
            )

    async def _request(
        self,
        method: str,
        path: str,
        location: GitLabTargetConfig,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        expect_json: bool = True,
    ) -> Any:
        """One GitLab call: retries (GET only), logging, and error mapping."""
        started = time.monotonic()
        attempts = 0
        status: int | None = None
        try:
            response, attempts = await send_with_retries(
                self._http,
                method,
                path,
                max_retries=self._max_retries,
                retry_wait=self._retry_wait,
                scrub=self._scrub,
                params=params,
                json=json,
            )
            status = response.status_code
            if status >= 400:
                await self._raise_for_status(response, location)
            if not expect_json:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise GitOpsResponseError(
                    f"{method} {path}: response is not JSON: {self._scrub(truncate(response.text))}"
                ) from exc
        finally:
            logger.info(
                "gitlab request",
                extra={
                    "fields": {
                        "method": method,
                        "path": path,
                        "status": status,
                        "duration_seconds": round(time.monotonic() - started, 3),
                        "retries": max(attempts - 1, 0),
                    }
                },
            )

    async def _raise_for_status(
        self, response: httpx.Response, location: GitLabTargetConfig
    ) -> None:
        status = response.status_code
        body = self._scrub(truncate(response.text))
        if status == 409 or (status == 400 and _is_stale_file_error(response.text)):
            raise GitOpsConflictError(f"{location.file} changed in GitLab: {body}")
        if status == 404:
            raise GitOpsNotFoundError(await self._explain_not_found(location))
        if status in (401, 403):
            raise GitOpsRejectedError(status, f"{body} -- {AUTH_HINT}")
        raise GitOpsRejectedError(status, body)

    async def _explain_not_found(self, location: GitLabTargetConfig) -> str:
        """GitLab says "404 File Not Found" for a missing branch too: find out which it is."""
        project = _encode(location.project)
        try:
            if (await self._http.get(f"/projects/{project}")).status_code == 404:
                return f"GitLab project {location.project!r} not found (or the token cannot see it)"
            branch_path = f"/projects/{project}/repository/branches/{_encode(location.branch)}"
            if (await self._http.get(branch_path)).status_code == 404:
                return (
                    f"branch {location.branch!r} not found in GitLab project {location.project!r}"
                )
        except httpx.HTTPError:
            pass
        return (
            f"file {location.file!r} not found on branch {location.branch!r} "
            f"in GitLab project {location.project!r}"
        )

    # ----------------------------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------------------------

    @asynccontextmanager
    async def _measure(self, operation: str) -> AsyncIterator[None]:
        """Count and time one port operation in the gitops metrics."""
        started = time.monotonic()
        result = "success"
        try:
            yield
        except GitOpsError as exc:
            result = _result_label(exc)
            raise
        finally:
            self._metrics.gitops_requests.labels(operation=operation, result=result).inc()
            self._metrics.gitops_duration.labels(operation=operation).observe(
                time.monotonic() - started
            )

    def _location(self, target: Target) -> GitLabTargetConfig:
        location = self._targets.get(target.key)
        if location is None:  # startup validation makes this unreachable
            raise GitOpsNotFoundError(f"target {target.key} has no gitlab block in the config")
        return location

    def _document(self, location: GitLabTargetConfig, text: str) -> Any:
        return manifest.select_document(
            manifest.load(text), location.document_kind, location.document_name
        )

    def _branch_name(self, target: Target, changes: list[ResourceChange]) -> str:
        resources = "-".join(sorted({change.resource for change in changes}))
        stamp = self._clock().strftime("%Y%m%dT%H%M%SZ")
        return f"{BRANCH_PREFIX}{target.key.ocp_cluster}-{resources}-{stamp}"

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "[REDACTED]") if self._token else text


# --------------------------------------------------------------------------------------
# Manifest <-> domain (no HTTP here)
# --------------------------------------------------------------------------------------


def state_from_document(
    document: Any, location: GitLabTargetConfig, version: str
) -> ComponentState:
    """Build ComponentState from the YAML document. A missing path gives None."""
    values: dict[str, Any] = {}
    for state_field, relative in FIELD_PATHS.items():
        raw = manifest.get_path(document, manifest.join_path(location.yaml_root, relative))
        values[state_field] = manifest.scalar_text(raw)
    replicas = values.pop("replicas")
    try:
        replica_count = int(replicas) if replicas is not None else None
    except ValueError as exc:
        raise GitOpsManifestError(f"replicaCount {replicas!r} is not a number") from exc
    raw_annotations = manifest.get_path(document, location.annotations_path)
    annotations = (
        {str(key): str(value) for key, value in raw_annotations.items()}
        if isinstance(raw_annotations, dict)
        else {}
    )
    return ComponentState(
        **values, replicas=replica_count, annotations=annotations, version=version
    )


def last_scaled_from_commits(
    commits: list[Any], reason_prefix: str, target: Target
) -> dict[str, str]:
    """Find, per resource, the newest commit whose message names our change to it.

    Matches any line of the message, so merge commits (whose message contains the MR
    title on a later line) and squash commits both count.
    """
    key = target.key
    title_re = re.compile(
        re.escape(reason_prefix)
        + r"\s+"
        + re.escape(f"{key.ocp_cluster}/{key.namespace}")
        + r"\s+"
        + re.escape(key.component)
        + r"\s+(?P<changes>.+)"
    )
    newest: dict[str, datetime] = {}
    for commit in commits:
        when = _commit_date(commit)
        if when is None:
            continue
        for line in str(commit.get("message") or commit.get("title") or "").splitlines():
            match = title_re.search(line)
            if match is None:
                continue
            for resource in _RESOURCE_CHANGE_RE.findall(match.group("changes")):
                if resource not in newest or when > newest[resource]:
                    newest[resource] = when
    return {
        f"{COOLDOWN_PREFIX}{resource}": when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        for resource, when in newest.items()
    }


def merge_newest(first: dict[str, str], second: dict[str, str]) -> dict[str, str]:
    """Merge two {annotation: ISO time} maps, keeping the newer time for each key."""
    merged = dict(first)
    for name, value in second.items():
        current = _parse_iso(merged.get(name))
        candidate = _parse_iso(value)
        if candidate is not None and (current is None or candidate > current):
            merged[name] = value
    return merged


def _commit_date(commit: Any) -> datetime | None:
    """When the commit landed on the branch (committed_date, not authored_date)."""
    if not isinstance(commit, dict):
        return None
    return _parse_iso(commit.get("committed_date") or commit.get("created_at"))


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def edit_manifest(
    location: GitLabTargetConfig,
    text: str,
    changes: list[ResourceChange],
    annotations: dict[str, str],
) -> str:
    """Apply changes + annotations to the file text. Only the changed lines differ."""
    parsed = manifest.load(text)
    document = manifest.select_document(parsed, location.document_kind, location.document_name)
    for change in changes:
        path = manifest.join_path(location.yaml_root, FIELD_PATHS[change.field])
        current = manifest.get_path(document, path)
        if manifest.scalar_text(current) != change.old:
            raise GitOpsConflictError(
                f"{path} is {manifest.scalar_text(current)!r} in GitLab, expected {change.old!r}"
            )
        manifest.set_path(
            document,
            path,
            manifest.restyle_quantity(current, change.new, FIELD_KINDS[change.field]),
        )

    existing = manifest.get_path(document, location.annotations_path)
    existing_keys = set(existing) if isinstance(existing, dict) else set()
    manifest.set_annotations(document, location.annotations_path, annotations)
    new_text = manifest.dump(parsed)

    updated_keys = existing_keys & set(annotations)
    new_keys = set(annotations) - existing_keys
    manifest.check_diff(
        text,
        new_text,
        max_changed=len(changes) + len(updated_keys),
        max_added=len(new_keys) + (0 if isinstance(existing, dict) or not annotations else 1),
    )
    return new_text


def _encode(value: str) -> str:
    """URL-encode a project path or file path for the GitLab API ("a/b" -> "a%2Fb")."""
    return quote(value, safe="")


def _is_stale_file_error(body: str) -> bool:
    text = body.lower()
    return "changed since" in text or "has changed" in text or "last_commit_id" in text


def _result_label(exc: GitOpsError) -> str:
    labels: dict[type[GitOpsError], str] = {
        GitOpsUnavailableError: "unavailable",
        GitOpsConflictError: "conflict",
        GitOpsNotFoundError: "not_found",
        GitOpsRejectedError: "rejected",
        GitOpsResponseError: "bad_response",
        GitOpsManifestError: "manifest_error",
    }
    return labels.get(type(exc), "error")
