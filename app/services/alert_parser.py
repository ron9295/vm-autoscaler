"""Turn a Grafana Unified Alerting webhook payload into `ScaleRequest`s.

Only the fields we need are modelled; everything else Grafana sends is allowed and ignored.
"""

from __future__ import annotations

import logging
from typing import Any, cast, get_args

from pydantic import BaseModel, ConfigDict, Field

from app.domain.errors import AlertParseError
from app.domain.models import (
    IgnoredAlert,
    ParsedWebhook,
    ResourceName,
    ScaleRequest,
    TargetKey,
)

logger = logging.getLogger(__name__)

REQUIRED_LABELS = ("ocp_cluster", "namespace", "component", "resource")
AUTOSCALE_LABEL = "autoscale"


class GrafanaAlert(BaseModel):
    """One alert inside the webhook."""

    model_config = ConfigDict(extra="allow")

    status: str
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    values: dict[str, Any] | None = None
    fingerprint: str = ""


class GrafanaWebhook(BaseModel):
    """The Grafana webhook body."""

    model_config = ConfigDict(extra="allow")

    status: str = ""
    receiver: str = ""
    alerts: list[GrafanaAlert] = Field(default_factory=list)


def parse_webhook(payload: GrafanaWebhook, value_ref_id: str) -> ParsedWebhook:
    """Parse the webhook. Raises AlertParseError if a firing alert misses a required label."""
    requests: list[ScaleRequest] = []
    ignored: list[IgnoredAlert] = []
    for alert in payload.alerts:
        if alert.status != "firing":
            logger.info(
                "ignoring non-firing alert",
                extra={"fields": {"fingerprint": alert.fingerprint, "status": alert.status}},
            )
            ignored.append(
                IgnoredAlert(fingerprint=alert.fingerprint, status=alert.status, reason="resolved")
            )
            continue
        requests.append(_to_scale_request(alert, value_ref_id))
    return ParsedWebhook(requests=requests, ignored=ignored)


def _to_scale_request(alert: GrafanaAlert, value_ref_id: str) -> ScaleRequest:
    labels = alert.labels
    for name in REQUIRED_LABELS:
        if not labels.get(name):
            raise AlertParseError(
                f"alert {alert.fingerprint or '<no fingerprint>'} is missing required label "
                f"'{name}'"
            )
    resource = labels["resource"]
    if resource not in get_args(ResourceName):
        raise AlertParseError(
            f"alert {alert.fingerprint}: label 'resource' must be one of "
            f"{list(get_args(ResourceName))}, got {resource!r}"
        )
    return ScaleRequest(
        fingerprint=alert.fingerprint,
        alert_name=labels.get("alertname", ""),
        target_key=TargetKey(
            ocp_cluster=labels["ocp_cluster"],
            namespace=labels["namespace"],
            component=labels["component"],
        ),
        resource=cast(ResourceName, resource),
        value=extract_value(alert.values, value_ref_id),
        # Missing label counts as "not true": the safety switch is off unless set.
        autoscale=labels.get(AUTOSCALE_LABEL) == "true",
    )


def extract_value(values: dict[str, Any] | None, ref_id: str) -> float | None:
    """Return values[ref_id], or the first numeric value, or None if there is none."""
    if not values:
        return None
    candidates = [values.get(ref_id), *values.values()]
    for value in candidates:
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
    return None


def group_by_target(requests: list[ScaleRequest]) -> dict[TargetKey, list[ScaleRequest]]:
    """Group requests so all resources of one target end up in one commit and one sync."""
    groups: dict[TargetKey, list[ScaleRequest]] = {}
    for request in requests:
        groups.setdefault(request.target_key, []).append(request)
    return groups
