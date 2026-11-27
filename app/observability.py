"""JSON logging and Prometheus metrics.

Log structured fields like this:

    logger.info("decision", extra={"fields": {"resource": "storage", "decision": "applied"}})
"""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Histogram

# The request id of the webhook being handled, added to every log line automatically.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_BEARER_RE = re.compile(r"(?i)(bearer\s+)[^\s\"',]+")
_PRIVATE_TOKEN_RE = re.compile(r"(?i)(private[-_]token[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+")
_SECRET_KEYS = {
    "authorization",
    "token",
    "password",
    "secret",
    "auth_token",
    "private-token",
    "private_token",
}


def redact(text: str) -> str:
    """Replace anything that looks like a bearer token or a GitLab PRIVATE-TOKEN."""
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    return _PRIVATE_TOKEN_RE.sub(r"\1[REDACTED]", text)


def _redact_fields(fields: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in fields.items():
        if key.lower() in _SECRET_KEYS:
            clean[key] = "[REDACTED]"
        elif isinstance(value, str):
            clean[key] = redact(value)
        else:
            clean[key] = value
    return clean


class JsonFormatter(logging.Formatter):
    """Format log records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
            "request_id": request_id_var.get(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            entry.update(_redact_fields(fields))
        if record.exc_info:
            entry["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, default=str)


def configure_logging(level: str) -> None:
    """Send JSON logs to stdout. Safe to call more than once."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # Make uvicorn's own loggers use our JSON handler too.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
    # httpx logs full URLs at INFO; we log our own request lines instead.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class Metrics:
    """All Prometheus metrics, registered in their own registry (no global state)."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.alerts_received = Counter(
            "vm_autoscaler_alerts_received_total",
            "Alerts received from Grafana, by alert status.",
            ["status"],
            registry=self.registry,
        )
        self.alerts_ignored = Counter(
            "vm_autoscaler_alerts_ignored_total",
            "Alerts deliberately not acted on (resolved, unsupported_resource).",
            ["reason"],
            registry=self.registry,
        )
        self.decisions = Counter(
            "vm_autoscaler_decisions_total",
            "Scaling decisions made.",
            ["ocp_cluster", "resource", "decision", "reason"],
            registry=self.registry,
        )
        self.unknown_targets = Counter(
            "vm_autoscaler_unknown_targets_total",
            "Alerts for a cluster/namespace/component that is not in the config.",
            registry=self.registry,
        )
        self.gitops_requests = Counter(
            "vm_autoscaler_gitops_requests_total",
            "Operations on the GitOps backend (GitLab or internal), by result.",
            ["operation", "result"],
            registry=self.registry,
        )
        self.gitops_duration = Histogram(
            "vm_autoscaler_gitops_request_duration_seconds",
            "Duration of GitOps backend operations (GitLab or internal), including retries.",
            ["operation"],
            registry=self.registry,
        )
        self.argocd_sync = Counter(
            "vm_autoscaler_argocd_sync_total",
            "Argo CD sync calls.",
            ["result"],
            registry=self.registry,
        )
        self.handle_duration = Histogram(
            "vm_autoscaler_handle_duration_seconds",
            "Time to handle one Grafana webhook.",
            registry=self.registry,
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60),
        )
