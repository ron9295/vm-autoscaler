"""Small builders for domain objects used across tests."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.domain.models import ComponentState, Mode, Policy, Target, TargetKey

TARGET_KEY = TargetKey(
    ocp_cluster="ocp-prod-region-a", namespace="monitoring", component="vmstorage"
)


def policy(**overrides: Any) -> Policy:
    values: dict[str, Any] = {
        "trigger_percent": 80,
        "step_percent": 25,
        "max": "16",
        "cooldown": timedelta(hours=6),
    }
    values.update(overrides)
    return Policy(**values)


def target(**overrides: Any) -> Target:
    values: dict[str, Any] = {
        "key": TARGET_KEY,
        "argocd_app": "victoria-metrics-region-a",
        "policies": {
            "cpu": policy(max="16"),
            "memory": policy(max="64Gi", requests_equal_limits=True),
            "storage": policy(step_percent=20, max="2Ti", cooldown=timedelta(hours=24)),
            "replicas": policy(
                step_percent=None, step=1, max="20", mode=Mode.MERGE_REQUEST, trigger_percent=85
            ),
        },
    }
    values.update(overrides)
    return Target(**values)


def state(**overrides: Any) -> ComponentState:
    values: dict[str, Any] = {
        "cpu_request": "2",
        "cpu_limit": "4",
        "memory_request": "16Gi",
        "memory_limit": "16Gi",
        "storage": "500Gi",
        "replicas": 3,
        "version": "v1",
    }
    values.update(overrides)
    return ComponentState(**values)
