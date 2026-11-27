import json
import stat
from pathlib import Path

import httpx
import pytest
import respx

from app.adapters.argocd_api import ArgoCdApiSyncer
from app.adapters.argocd_cli import ArgoCdCliSyncer, resource_arg
from app.domain.errors import ArgoCdError, ArgoCdUnavailableError
from app.domain.models import SyncResource

ARGO = "https://argocd.example.internal"
APP_URL = f"{ARGO}/api/v1/applications/victoria-metrics-region-a"
RESOURCE = SyncResource(
    group="operator.victoriametrics.com", kind="VMCluster", name="vm-main", namespace="monitoring"
)


def app_body(
    health: str = "Healthy", sync: str = "Synced", phase: str | None = "Succeeded"
) -> dict:  # type: ignore[type-arg]
    status: dict = {"health": {"status": health}, "sync": {"status": sync}}  # type: ignore[type-arg]
    if phase:
        status["operationState"] = {"phase": phase}
    return {"metadata": {"name": "victoria-metrics-region-a"}, "status": status}


@pytest.fixture
async def api():  # type: ignore[no-untyped-def]
    syncer = ArgoCdApiSyncer(ARGO, "argo-token", 2.0, True, retry_wait_seconds=0)
    yield syncer
    await syncer.close()


@respx.mock
async def test_status_healthy(api: ArgoCdApiSyncer) -> None:
    respx.get(APP_URL).respond(200, json=app_body())
    status = await api.get_status("victoria-metrics-region-a")
    assert status.is_safe_to_change


@respx.mock
async def test_status_running_operation_not_safe(api: ArgoCdApiSyncer) -> None:
    respx.get(APP_URL).respond(200, json=app_body(phase="Running"))
    status = await api.get_status("victoria-metrics-region-a")
    assert status.operation_in_progress and not status.is_safe_to_change


@respx.mock
async def test_status_retries_then_unavailable(api: ArgoCdApiSyncer) -> None:
    route = respx.get(APP_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(ArgoCdUnavailableError):
        await api.get_status("victoria-metrics-region-a")
    assert route.call_count == 3


@respx.mock
async def test_sync_sends_resources_without_prune(api: ArgoCdApiSyncer) -> None:
    respx.get(APP_URL).respond(200, json=app_body())
    route = respx.post(f"{APP_URL}/sync").respond(200, json={})
    await api.sync("victoria-metrics-region-a", [RESOURCE])
    body = json.loads(route.calls.last.request.content)
    assert body["prune"] is False
    assert body["resources"] == [RESOURCE.model_dump()]
    assert route.calls.last.request.headers["Authorization"] == "Bearer argo-token"


@respx.mock
async def test_sync_already_in_progress_is_success(api: ArgoCdApiSyncer) -> None:
    respx.get(APP_URL).respond(200, json=app_body())
    respx.post(f"{APP_URL}/sync").respond(
        400, json={"error": "another operation is already in progress", "code": 9}
    )
    await api.sync("victoria-metrics-region-a", [RESOURCE])


@respx.mock
async def test_sync_forbidden_raises(api: ArgoCdApiSyncer) -> None:
    respx.get(APP_URL).respond(200, json=app_body())
    respx.post(f"{APP_URL}/sync").respond(403, json={"error": "permission denied"})
    with pytest.raises(ArgoCdError, match="403"):
        await api.sync("victoria-metrics-region-a", [RESOURCE])


@respx.mock
async def test_sync_refreshes_app_before_syncing(api: ArgoCdApiSyncer) -> None:
    calls: list[str] = []
    respx.get(APP_URL).mock(
        side_effect=lambda request: calls.append(f"GET {request.url.query.decode()}")
        or httpx.Response(200, json=app_body())
    )
    respx.post(f"{APP_URL}/sync").mock(
        side_effect=lambda request: calls.append("POST sync") or httpx.Response(200, json={})
    )
    await api.sync("victoria-metrics-region-a", [RESOURCE])
    assert calls == ["GET refresh=normal", "POST sync"]


@respx.mock
async def test_sync_does_not_pin_a_revision(api: ArgoCdApiSyncer) -> None:
    respx.get(APP_URL).respond(200, json=app_body())
    route = respx.post(f"{APP_URL}/sync").respond(200, json={})
    await api.sync("victoria-metrics-region-a", [RESOURCE])
    assert "revision" not in json.loads(route.calls.last.request.content)


@respx.mock
async def test_failed_refresh_stops_the_sync(api: ArgoCdApiSyncer) -> None:
    respx.get(APP_URL).respond(403, json={"error": "permission denied"})
    sync = respx.post(f"{APP_URL}/sync")
    with pytest.raises(ArgoCdError, match="403"):
        await api.sync("victoria-metrics-region-a", [RESOURCE])
    assert sync.call_count == 0


def test_resource_arg() -> None:
    assert resource_arg(RESOURCE) == "operator.victoriametrics.com:VMCluster:monitoring/vm-main"


def _fake_cli(tmp_path: Path, script: str) -> str:
    path = tmp_path / "argocd"
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


async def test_cli_sync_passes_token_via_env_not_argv(tmp_path: Path) -> None:
    log = tmp_path / "calls.log"
    cli = _fake_cli(tmp_path, f'echo "$@" >> {log}\necho "token=$ARGOCD_AUTH_TOKEN" >> {log}\n')
    syncer = ArgoCdCliSyncer("https://argocd.example.internal", "cli-token", 5.0, cli_path=cli)
    await syncer.sync("victoria-metrics-region-a", [RESOURCE])
    refresh_argv, refresh_env, argv, env_line = log.read_text().splitlines()
    assert refresh_argv.startswith("app get victoria-metrics-region-a --refresh -o json")
    assert "app sync victoria-metrics-region-a --async --resource" in argv
    assert "--server argocd.example.internal" in argv
    assert "cli-token" not in argv and "cli-token" not in refresh_argv
    assert env_line == refresh_env == "token=cli-token"


async def test_cli_gets_a_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-must-not-leak")
    monkeypatch.setenv("WEBHOOK_TOKEN", "webhook-must-not-leak")
    monkeypatch.setenv("GITOPS_API_TOKEN", "internal-must-not-leak")
    dump = tmp_path / "env.txt"
    cli = _fake_cli(tmp_path, f"env > {dump}\n")
    ca = tmp_path / "ca.crt"
    syncer = ArgoCdCliSyncer(ARGO, "cli-token", 5.0, cli_path=cli, ca_bundle_path=ca)
    await syncer.sync("victoria-metrics-region-a", [RESOURCE])
    env = dict(line.split("=", 1) for line in dump.read_text().splitlines() if "=" in line)
    assert "must-not-leak" not in dump.read_text()
    assert env["ARGOCD_AUTH_TOKEN"] == "cli-token"
    assert env["SSL_CERT_FILE"] == str(ca)
    assert "PATH" in env and "HOME" in env


async def test_cli_get_status(tmp_path: Path) -> None:
    cli = _fake_cli(tmp_path, f"echo '{json.dumps(app_body(health='Degraded'))}'\n")
    syncer = ArgoCdCliSyncer(ARGO, None, 5.0, cli_path=cli)
    status = await syncer.get_status("victoria-metrics-region-a")
    assert status.health == "Degraded"


async def test_cli_failure_raises(tmp_path: Path) -> None:
    cli = _fake_cli(tmp_path, "echo 'permission denied' >&2\nexit 20\n")
    syncer = ArgoCdCliSyncer(ARGO, None, 5.0, cli_path=cli)
    with pytest.raises(ArgoCdError, match="exit 20"):
        await syncer.sync("victoria-metrics-region-a", [RESOURCE])


async def test_cli_already_running_is_success(tmp_path: Path) -> None:
    # Only `app sync` reports the running operation; the refresh (`app get`) succeeds.
    cli = _fake_cli(
        tmp_path,
        'if [ "$2" = "sync" ]; then\n'
        "  echo 'FATA[0000] another operation is already in progress' >&2; exit 20\n"
        "fi\n",
    )
    syncer = ArgoCdCliSyncer(ARGO, None, 5.0, cli_path=cli)
    await syncer.sync("victoria-metrics-region-a", [RESOURCE])


async def test_cli_missing_binary(tmp_path: Path) -> None:
    syncer = ArgoCdCliSyncer(ARGO, None, 5.0, cli_path=str(tmp_path / "nope"))
    with pytest.raises(ArgoCdUnavailableError):
        await syncer.get_status("x")
