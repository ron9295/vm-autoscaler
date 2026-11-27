"""HTTP routes: /webhook/grafana, /healthz, /readyz, /metrics."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import ValidationError

from app.api.deps import ContainerDep
from app.api.security import verify_webhook_token
from app.domain.errors import AlertParseError
from app.observability import request_id_var
from app.services.alert_parser import GrafanaWebhook, parse_webhook

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/webhook/grafana",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(verify_webhook_token)],
)
async def grafana_webhook(request: Request, container: ContainerDep) -> dict[str, Any]:
    """Receive a Grafana Unified Alerting webhook and return a summary of decisions.

    The body is parsed here (after the token check) so an unauthenticated caller always
    gets 401, never a validation error.
    """
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
    token = request_id_var.set(request_id)
    try:
        payload = await _read_payload(request)
        for alert in payload.alerts:
            container.metrics.alerts_received.labels(status=alert.status or "unknown").inc()
        try:
            parsed = parse_webhook(payload, container.config.defaults.value_ref_id)
        except AlertParseError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        decisions = await container.service.handle(parsed)
        return {
            "request_id": request_id,
            "mode": container.config.mode.value,
            "decisions": [d.model_dump(mode="json") for d in decisions],
            "ignored": [i.model_dump(mode="json") for i in parsed.ignored],
        }
    finally:
        request_id_var.reset(token)


async def _read_payload(request: Request) -> GrafanaWebhook:
    try:
        raw = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "body is not valid JSON") from exc
    try:
        return GrafanaWebhook.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"not a Grafana webhook payload: {exc.errors(include_url=False)}",
        ) from exc


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(container: ContainerDep) -> JSONResponse:
    """Readiness: config loaded and (optionally) dependencies reachable."""
    checks = await container.readiness.check()
    ready = all(checks.values())
    code = status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse({"ready": ready, "checks": checks}, status_code=code)


@router.get("/metrics")
async def metrics(container: ContainerDep) -> Response:
    """Prometheus metrics."""
    return Response(generate_latest(container.metrics.registry), media_type=CONTENT_TYPE_LATEST)
