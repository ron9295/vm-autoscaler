"""Argo CD adapter using the `argocd` CLI (`ARGOCD_SYNC_METHOD=cli`).

For teams that prefer the CLI. The `argocd` binary must be present in the image (it is
not installed by default; see the Dockerfile). The token is passed through the
ARGOCD_AUTH_TOKEN environment variable, never on the command line (visible in `ps`).
The subprocess gets a minimal environment (never our other secrets, such as GITLAB_TOKEN
or WEBHOOK_TOKEN). An internal CA bundle is passed as SSL_CERT_FILE.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

from app.adapters.argocd_api import ALREADY_RUNNING_MARKER, parse_app_status
from app.adapters.http_common import truncate
from app.domain.errors import ArgoCdError, ArgoCdUnavailableError
from app.domain.models import AppStatus, SyncResource

logger = logging.getLogger(__name__)

# Environment variables the argocd CLI may see (no secrets).
_PASSED_ENV = ("PATH", "HOME", "LANG", "TZ", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")


def resource_arg(resource: SyncResource) -> str:
    """Format a resource for `--resource`: GROUP:KIND:NAME or GROUP:KIND:NAMESPACE/NAME."""
    name = f"{resource.namespace}/{resource.name}" if resource.namespace else resource.name
    return f"{resource.group}:{resource.kind}:{name}"


class ArgoCdCliSyncer:
    """Run `argocd app get` / `argocd app sync` as subprocesses."""

    def __init__(
        self,
        server: str,
        token: str | None,
        timeout: float,
        *,
        insecure: bool = False,
        cli_path: str = "argocd",
        ca_bundle_path: Path | None = None,
    ) -> None:
        self._server = server.removeprefix("https://").removeprefix("http://").rstrip("/")
        self._token = token
        self._timeout = timeout
        self._insecure = insecure
        self._cli_path = cli_path
        self._ca_bundle_path = ca_bundle_path

    async def get_status(self, app: str) -> AppStatus:
        """`argocd app get <app> -o json`."""
        output = await self._run(["app", "get", app, "-o", "json"])
        try:
            return parse_app_status(json.loads(output))
        except json.JSONDecodeError as exc:
            raise ArgoCdError("argocd app get did not return JSON") from exc

    async def sync(self, app: str, resources: list[SyncResource]) -> None:
        """Refresh, then `argocd app sync <app> --resource ... --async` (no prune).

        The refresh makes Argo CD re-read Git first, so the sync applies our new commit.
        """
        await self._run(["app", "get", app, "--refresh", "-o", "json"])
        args = ["app", "sync", app, "--async"]
        for resource in resources:
            args += ["--resource", resource_arg(resource)]
        try:
            await self._run(args)
        except ArgoCdError as exc:
            if ALREADY_RUNNING_MARKER in str(exc).lower():
                logger.info(
                    "argo cd sync already in progress, treating as success",
                    extra={"fields": {"argocd_app": app}},
                )
                return
            raise

    async def close(self) -> None:
        """Nothing to close."""

    async def _run(self, args: list[str]) -> str:
        command = [self._cli_path, *args, "--server", self._server, "--grpc-web"]
        if self._insecure:
            command.append("--insecure")
        env = self._environment()
        logger.info("running argocd cli", extra={"fields": {"args": " ".join(args)}})
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            raise ArgoCdUnavailableError(f"argocd CLI not found at {self._cli_path!r}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), self._timeout)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ArgoCdUnavailableError(f"argocd {args[0]} {args[1]} timed out") from exc
        if process.returncode != 0:
            raise ArgoCdError(
                f"argocd {' '.join(args[:2])} failed (exit {process.returncode}): "
                f"{truncate(stderr.decode(errors='replace'))}"
            )
        return stdout.decode(errors="replace")

    def _environment(self) -> dict[str, str]:
        """Only what the CLI needs. Never pass on our own secrets from os.environ."""
        env = {name: os.environ[name] for name in _PASSED_ENV if name in os.environ}
        # The CLI writes its config under $HOME; the image sets HOME=/tmp (an emptyDir).
        env.setdefault("HOME", tempfile.gettempdir())
        if self._token:
            env["ARGOCD_AUTH_TOKEN"] = self._token
        if self._ca_bundle_path is not None:
            env["SSL_CERT_FILE"] = str(self._ca_bundle_path)
        return env
