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
from app.api.grafana_webhook import GrafanaWebhook, parse_webhook
from app.api.security import verify_webhook_token
from app.domain.models import SkipReason
from app.observability import request_id_var

logger = logging.getLogger(__name__)

router = APIRouter()

# Transient failures: answer 503 so Grafana re-sends the alert instead of waiting for the
# next repeat interval.
RETRYABLE_REASONS = frozenset(
    {SkipReason.GITOPS_UNAVAILABLE.value, SkipReason.ARGOCD_UNAVAILABLE.value}
)


@router.post(
    "/webhook/grafana",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(verify_webhook_token)],
)
async def grafana_webhook(request: Request, container: ContainerDep) -> JSONResponse:
    """Receive a Grafana Unified Alerting webhook and return a summary of decisions.

    Returns 202, or 503 (same body) when GitOps or Argo CD was unavailable, so Grafana
    re-sends the alert. Handling is idempotent, so a re-send is harmless.

    The body is parsed here (after the token check) so an unauthenticated caller always
    gets 401, never a validation error.
    """
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
    token = request_id_var.set(request_id)
    try:
        payload = await _read_payload(request)
        for alert in payload.alerts:
            container.metrics.alerts_received.labels(status=alert.status or "unknown").inc()
        parsed = parse_webhook(payload, container.config.defaults.value_ref_id)
        for ignored in parsed.ignored:
            container.metrics.alerts_ignored.labels(reason=ignored.reason).inc()
        decisions = await container.service.handle(parsed)
        body: dict[str, Any] = {
            "request_id": request_id,
            "mode": container.config.mode.value,
            "decisions": [d.model_dump(mode="json") for d in decisions],
            "ignored": [i.model_dump(mode="json") for i in parsed.ignored],
        }
        retry = any(d.reason in RETRYABLE_REASONS for d in decisions)
        code = status.HTTP_503_SERVICE_UNAVAILABLE if retry else status.HTTP_202_ACCEPTED
        return JSONResponse(body, status_code=code)
    finally:
        request_id_var.reset(token)


async def _read_payload(request: Request) -> GrafanaWebhook:
    try:
        raw = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "body is not valid JSON"
        ) from exc
    try:
        return GrafanaWebhook.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
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
