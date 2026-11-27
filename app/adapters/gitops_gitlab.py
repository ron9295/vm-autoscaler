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
    e.g. "[vm-autoscaler] ocp-a/monitoring vmstorage storage 30Gi -> 36Gi (...)". The list
    uses `first_parent=true`, so an approved merge request counts from its merge (or
    squash) commit, i.e. from when it actually landed. Nothing is written to the manifest.
    A wrongly matching message (e.g. a human revert of our commit) can only start a
    cooldown early, never cause an extra change.
    annotations: read/write vm-autoscaler/last-scaled-<resource> in the values file at
    `annotations_path` (fallback if commit history can't be used).

What is edited
    Only the PVC size, at `storage_path` (full dotted path from the document root,
    default `vmstorage.pvc.size`). The file is chosen per target from `file_template`
    (`<ocp_cluster>/<namespace>/<values file>` in the Argo CD source repo).

Merge request mode
    One commit to a new branch (`start_branch` = target branch, so the branch is created
    by the same call), then one merge request. Branch name:
    vm-autoscaler/<cluster>-<namespace>-<resources>-<UTC time>-<idempotency key prefix>.
    Before opening one, the open merge requests into the branch are listed:
    * one with the same idempotency key in its description is returned (same change,
      e.g. after a pod restart);
    * any other open vm-autoscaler MR for the same cluster + namespace is returned too, so
      resizes never stack up while an MR waits for approval.
    If creating the MR fails, the branch is deleted only after a definite rejection. After
    a timeout the MR may exist anyway, so it is looked up by branch and the branch is kept.
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

from app.adapters import yaml_edit
from app.adapters.http_common import send_with_retries, truncate
from app.config import GitLabConfig, GitLabLocation
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
    COOLDOWN_ANNOTATION_PREFIX,
    ChangeResult,
    ComponentState,
    ResourceChange,
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
HISTORY_PAGE_SIZE = 100
MAX_HISTORY_PAGES = 10
MR_PAGE_SIZE = 100
MAX_MR_PAGES = 5
TARGET_MR_OPEN_MESSAGE = "a vm-autoscaler merge request for this target is already open"
_UNSAFE_REF_CHARS_RE = re.compile(r"[^a-z0-9._-]+")
# "storage 30Gi -> 36Gi" inside our commit title (resource, then old -> new).
_RESOURCE_CHANGE_RE = re.compile(r"\b(storage)\s+\S+\s+->\s+\S+")
READINESS_TIMEOUT_SECONDS = 2.0


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
        targets: dict[TargetKey, GitLabLocation],
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
                change_id=commit["id"],
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
            existing = pick_open_merge_request(
                await self._open_merge_requests(location),
                idempotency_key,
                target_branch_prefix(target),
            )
            if existing is not None:
                logger.info(
                    "merge request already open",
                    extra={"fields": {"url": existing.url, "reason": existing.message}},
                )
                return existing

            snapshot = await self._read_file(location)
            content = edit_manifest(
                location, snapshot.text, changes, self._file_annotations(annotations)
            )
            title, _, details = reason.partition("\n\n")
            branch = self._branch_name(target, changes, idempotency_key)
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
                merge_request = await self._create_merge_request(
                    location, branch, title, description
                )
            except (GitOpsUnavailableError, GitOpsResponseError):
                # GitLab may have created the MR before the error: look before giving up.
                found = await self._merge_request_for_branch(location, branch)
                if found is not None:
                    return found
                logger.warning(
                    "merge request creation failed after the branch was pushed; "
                    "branch kept (the MR may still appear); remove it by hand if not",
                    extra={"fields": {"branch": branch}},
                )
                raise
            except (GitOpsRejectedError, GitOpsNotFoundError, GitOpsConflictError):
                await self._delete_branch(location, branch)
                raise
            return merge_request

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

    async def _read_file(self, location: GitLabLocation) -> FileSnapshot:
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
        location: GitLabLocation,
        *,
        branch: str,
        message: str,
        content: str,
        last_commit_id: str,
        start_branch: str | None = None,
    ) -> dict[str, str | None]:
        """Commit one file update. Returns {"id": sha, "web_url": url or None}."""
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
        commit = await self._request(
            "POST",
            f"/projects/{_encode(location.project)}/repository/commits",
            location,
            json=payload,
        )
        if not isinstance(commit, dict) or not commit.get("id"):
            raise GitOpsResponseError(f"unexpected commit response for {location.file}")
        web_url = commit.get("web_url")
        return {"id": str(commit["id"]), "web_url": web_url if isinstance(web_url, str) else None}

    async def _create_merge_request(
        self, location: GitLabLocation, branch: str, title: str, description: str
    ) -> ChangeResult:
        body = await self._request(
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
        result = merge_request_result(body, message=title)
        if result is None:
            raise GitOpsResponseError("unexpected merge request response")
        return result

    async def _open_merge_requests(self, location: GitLabLocation) -> list[dict[str, Any]]:
        """All open merge requests into the target branch (paged, bounded)."""
        found: list[dict[str, Any]] = []
        for page in range(1, MAX_MR_PAGES + 1):
            batch = await self._request(
                "GET",
                f"/projects/{_encode(location.project)}/merge_requests",
                location,
                params={
                    "state": "opened",
                    "target_branch": location.branch,
                    "per_page": str(MR_PAGE_SIZE),
                    "page": str(page),
                },
            )
            if not isinstance(batch, list):
                raise GitOpsResponseError("merge request list response is not a list")
            found.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < MR_PAGE_SIZE:
                return found
        logger.warning(
            "more open merge requests than checked; an existing vm-autoscaler MR may be missed",
            extra={"fields": {"checked": len(found), "branch": location.branch}},
        )
        return found

    async def _merge_request_for_branch(
        self, location: GitLabLocation, branch: str
    ) -> ChangeResult | None:
        """The open MR whose source is `branch`, or None (also None if the lookup fails)."""
        try:
            found = await self._request(
                "GET",
                f"/projects/{_encode(location.project)}/merge_requests",
                location,
                params={"state": "opened", "source_branch": branch},
            )
        except GitOpsError as exc:
            logger.warning(
                "could not look up the merge request for a pushed branch",
                extra={"fields": {"branch": branch, "error": str(exc)}},
            )
            return None
        for item in found if isinstance(found, list) else []:
            if isinstance(item, dict) and item.get("source_branch") == branch:
                return merge_request_result(item, message="merge request created")
        return None

    async def _last_scaled_from_commits(
        self, target: Target, location: GitLabLocation
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

    async def _delete_branch(self, location: GitLabLocation, branch: str) -> None:
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
        location: GitLabLocation,
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

    async def _raise_for_status(self, response: httpx.Response, location: GitLabLocation) -> None:
        status = response.status_code
        body = self._scrub(truncate(response.text))
        if status == 409 or (status == 400 and _is_stale_file_error(response.text)):
            raise GitOpsConflictError(f"{location.file} changed in GitLab: {body}")
        if status == 404:
            raise GitOpsNotFoundError(await self._explain_not_found(location))
        if status in (401, 403):
            raise GitOpsRejectedError(status, f"{body} -- {AUTH_HINT}")
        raise GitOpsRejectedError(status, body)

    async def _explain_not_found(self, location: GitLabLocation) -> str:
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
        except Exception:
            result = "error"
            raise
        finally:
            self._metrics.gitops_requests.labels(operation=operation, result=result).inc()
            self._metrics.gitops_duration.labels(operation=operation).observe(
                time.monotonic() - started
            )

    def _location(self, target: Target) -> GitLabLocation:
        location = self._targets.get(target.key)
        if location is None:  # startup validation makes this unreachable
            raise GitOpsNotFoundError(f"target {target.key} has no GitLab location in the config")
        return location

    def _document(self, location: GitLabLocation, text: str) -> Any:
        return yaml_edit.select_document(
            yaml_edit.load(text), location.document_kind, location.document_name
        )

    def _branch_name(
        self, target: Target, changes: list[ResourceChange], idempotency_key: str
    ) -> str:
        """vm-autoscaler/<cluster>-<namespace>-<resources>-<UTC time>-<key prefix>."""
        resources = "-".join(sorted({change.resource for change in changes}))
        stamp = self._clock().strftime("%Y%m%dT%H%M%SZ")
        # The UTC stamp (digits, "T", "Z") is already ref-safe and stays readable.
        return (
            f"{target_branch_prefix(target)}{ref_slug(resources)}-{stamp}-"
            f"{ref_slug(idempotency_key[:8])}"
        )

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "[REDACTED]") if self._token else text


# --------------------------------------------------------------------------------------
# Manifest <-> domain (no HTTP here)
# --------------------------------------------------------------------------------------


def state_from_document(document: Any, location: GitLabLocation, version: str) -> ComponentState:
    """Build ComponentState from the YAML document. A missing size gives storage=None."""
    storage = yaml_edit.scalar_text(yaml_edit.get_path(document, location.storage_path))
    raw_annotations = yaml_edit.get_path(document, location.annotations_path)
    annotations = (
        {str(key): str(value) for key, value in raw_annotations.items()}
        if isinstance(raw_annotations, dict)
        else {}
    )
    return ComponentState(storage=storage, annotations=annotations, version=version)


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
        f"{COOLDOWN_ANNOTATION_PREFIX}{resource}": when.astimezone(UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        for resource, when in newest.items()
    }


def ref_slug(value: str) -> str:
    """Make text safe for a git branch name: lowercase letters, digits, '.', '_' and '-'."""
    slug = _UNSAFE_REF_CHARS_RE.sub("-", value.lower())
    slug = re.sub(r"\.{2,}", ".", slug)  # ".." is not allowed in git refs
    return slug.strip("-.") or "x"


def target_branch_prefix(target: Target) -> str:
    """Branch prefix shared by all of our MR branches for one cluster + namespace.

    Names that differ only in where the '-' falls (cluster "a" + namespace "b-c" vs
    cluster "a-b" + namespace "c") share a prefix. That only makes the "MR already open"
    check stricter (skips a resize), never looser.
    """
    key = target.key
    return f"{BRANCH_PREFIX}{ref_slug(key.ocp_cluster)}-{ref_slug(key.namespace)}-"


def merge_request_result(body: Any, message: str | None) -> ChangeResult | None:
    """ChangeResult for a GitLab merge request object, or None if it isn't one."""
    if not isinstance(body, dict) or body.get("iid") is None:
        return None
    web_url = body.get("web_url")
    return ChangeResult(
        accepted=True,
        change_id=f"!{body['iid']}",
        url=web_url if isinstance(web_url, str) else None,
        message=message,
    )


def pick_open_merge_request(
    merge_requests: list[dict[str, Any]], idempotency_key: str, branch_prefix: str
) -> ChangeResult | None:
    """Return an open MR to reuse instead of opening a new one, or None.

    1. Same change: one of our branches with this idempotency key in the description.
    2. Same target: any of our branches for this cluster + namespace (don't stack resizes
       while one waits for approval).
    """
    ours = [
        mr for mr in merge_requests if str(mr.get("source_branch", "")).startswith(BRANCH_PREFIX)
    ]
    for mr in ours:
        if idempotency_key in str(mr.get("description") or ""):
            return merge_request_result(mr, message="merge request already open")
    for mr in ours:
        if str(mr.get("source_branch", "")).startswith(branch_prefix):
            return merge_request_result(mr, message=TARGET_MR_OPEN_MESSAGE)
    return None


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
    location: GitLabLocation,
    text: str,
    changes: list[ResourceChange],
    annotations: dict[str, str],
) -> str:
    """Apply the size change + annotations to the file text. Only changed lines differ.

    Raises GitOpsConflictError if the value in the file is no longer `change.old`.
    """
    parsed = yaml_edit.load(text)
    document = yaml_edit.select_document(parsed, location.document_kind, location.document_name)
    for change in changes:
        if change.field != "storage":
            raise GitOpsManifestError(f"unsupported field {change.field!r} (storage only)")
        path = location.storage_path
        current = yaml_edit.get_path(document, path)
        if yaml_edit.scalar_text(current) != change.old:
            raise GitOpsConflictError(
                f"{path} is {yaml_edit.scalar_text(current)!r} in GitLab, expected {change.old!r}"
            )
        yaml_edit.set_path(document, path, yaml_edit.restyle_quantity(current, change.new))

    existing = yaml_edit.get_path(document, location.annotations_path)
    existing_keys = set(existing) if isinstance(existing, dict) else set()
    yaml_edit.set_annotations(document, location.annotations_path, annotations)
    new_text = yaml_edit.dump(parsed)

    updated_keys = existing_keys & set(annotations)
    new_keys = set(annotations) - existing_keys
    yaml_edit.check_diff(
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
