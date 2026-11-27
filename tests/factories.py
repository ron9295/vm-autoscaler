"""Small builders for domain objects used across tests."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.domain.models import ComponentState, Policy, Target, TargetKey

TARGET_KEY = TargetKey(
    ocp_cluster="ocp-prod-region-a", namespace="monitoring", component="vmstorage"
)


def policy(**overrides: Any) -> Policy:
    values: dict[str, Any] = {
        "trigger_percent": 80,
        "step_percent": 20,
        "max": "2Ti",
        "cooldown": timedelta(hours=24),
    }
    values.update(overrides)
    return Policy(**values)


def target(**overrides: Any) -> Target:
    values: dict[str, Any] = {
        "key": TARGET_KEY,
        "argocd_app": "victoria-metrics-region-a",
        "policies": {"storage": policy()},
    }
    values.update(overrides)
    return Target(**values)


def state(**overrides: Any) -> ComponentState:
    values: dict[str, Any] = {"storage": "30Gi", "version": "v1"}
    values.update(overrides)
    return ComponentState(**values)
