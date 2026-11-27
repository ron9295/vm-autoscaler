"""Turn a Grafana Unified Alerting webhook payload into `ScaleRequest`s.

Only the fields we need are modelled; everything else Grafana sends is allowed and ignored.
The only supported `resource` label value is "storage" (vmstorage PVC size). Firing alerts
for any other resource (e.g. cpu/memory left over from old alert rules) are ignored one by
one with reason "unsupported_resource", so they never block a storage alert in the same
webhook. A firing alert missing a required label is still rejected (HTTP 422).
"""

from __future__ import annotations

import logging
from typing import Any, cast, get_args

from pydantic import BaseModel, ConfigDict, Field

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
IGNORED_RESOLVED = "resolved"
IGNORED_UNSUPPORTED_RESOURCE = "unsupported_resource"
IGNORED_MISSING_LABEL = "missing_label"


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
    """Parse the webhook. Firing alerts that cannot be acted on are listed as ignored."""
    requests: list[ScaleRequest] = []
    ignored: list[IgnoredAlert] = []
    for alert in payload.alerts:
        if alert.status != "firing":
            logger.info(
                "ignoring non-firing alert",
                extra={"fields": {"fingerprint": alert.fingerprint, "status": alert.status}},
            )
            ignored.append(
                IgnoredAlert(
                    fingerprint=alert.fingerprint, status=alert.status, reason=IGNORED_RESOLVED
                )
            )
            continue
        missing = _missing_label(alert)
        if missing is not None:
            logger.warning(
                "ignoring alert with a missing required label; fix its alert rule",
                extra={"fields": {"fingerprint": alert.fingerprint, "missing_label": missing}},
            )
            ignored.append(
                IgnoredAlert(
                    fingerprint=alert.fingerprint,
                    status=alert.status,
                    reason=IGNORED_MISSING_LABEL,
                )
            )
            continue
        resource = alert.labels["resource"]
        if resource not in get_args(ResourceName):
            logger.warning(
                "ignoring alert for unsupported resource; remove or fix its alert rule",
                extra={
                    "fields": {
                        "fingerprint": alert.fingerprint,
                        "resource": resource,
                        "supported": list(get_args(ResourceName)),
                    }
                },
            )
            ignored.append(
                IgnoredAlert(
                    fingerprint=alert.fingerprint,
                    status=alert.status,
                    reason=IGNORED_UNSUPPORTED_RESOURCE,
                )
            )
            continue
        requests.append(_to_scale_request(alert, value_ref_id))
    return ParsedWebhook(requests=requests, ignored=ignored)


def _missing_label(alert: GrafanaAlert) -> str | None:
    """Return the name of the first required label the alert lacks, or None."""
    for name in REQUIRED_LABELS:
        if not alert.labels.get(name):
            return name
    return None


def _to_scale_request(alert: GrafanaAlert, value_ref_id: str) -> ScaleRequest:
    """Build a ScaleRequest from a firing alert whose labels were already checked."""
    labels = alert.labels
    resource = labels["resource"]
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
    """Return values[ref_id] if it is a number, else None (fail closed, no guessing).

    Taking "the first number" when the configured query is absent could scale on the wrong
    metric (e.g. a raw byte count instead of a percent), so a missing ref_id means no value.
    """
    if not values:
        return None
    if ref_id not in values:
        logger.warning(
            "alert has no value for the configured query ref_id",
            extra={"fields": {"ref_id": ref_id, "available": sorted(values)}},
        )
        return None
    value = values[ref_id]
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    logger.warning(
        "alert value for the configured query ref_id is not a number",
        extra={"fields": {"ref_id": ref_id, "value": repr(value)}},
    )
    return None
