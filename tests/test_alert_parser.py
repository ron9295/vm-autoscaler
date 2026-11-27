import pytest

from app.domain.errors import AlertParseError
from app.domain.models import TargetKey
from app.services.alert_parser import (
    GrafanaWebhook,
    extract_value,
    group_by_target,
    parse_webhook,
)
from tests.conftest import load_fixture


def _parse(name: str):  # type: ignore[no-untyped-def]
    return parse_webhook(GrafanaWebhook.model_validate(load_fixture(name)), value_ref_id="B")


def test_firing_cpu_alert() -> None:
    parsed = _parse("grafana_firing_cpu.json")
    assert parsed.ignored == []
    [request] = parsed.requests
    assert request.resource == "cpu"
    assert request.value == 91.2
    assert request.autoscale is True
    assert request.fingerprint == "a1b2c3d4e5f60001"
    assert request.target_key == TargetKey(
        ocp_cluster="ocp-prod-region-a", namespace="monitoring", component="vmstorage"
    )


def test_resolved_alert_is_ignored() -> None:
    parsed = _parse("grafana_resolved.json")
    assert parsed.requests == []
    assert [i.reason for i in parsed.ignored] == ["resolved"]


def test_missing_label_raises_clear_error() -> None:
    with pytest.raises(AlertParseError, match="missing required label 'ocp_cluster'"):
        _parse("grafana_missing_label.json")


def test_autoscale_not_true_is_flagged() -> None:
    [request] = _parse("grafana_autoscale_false.json").requests
    assert request.autoscale is False


def test_unknown_resource_raises() -> None:
    payload = load_fixture("grafana_firing_cpu.json")
    payload["alerts"][0]["labels"]["resource"] = "disk"
    with pytest.raises(AlertParseError, match="'resource' must be one of"):
        parse_webhook(GrafanaWebhook.model_validate(payload), value_ref_id="B")


def test_multiple_alerts_grouped_by_target() -> None:
    parsed = _parse("grafana_multi.json")
    assert len(parsed.requests) == 3
    assert len(parsed.ignored) == 1
    groups = group_by_target(parsed.requests)
    region_a = TargetKey(
        ocp_cluster="ocp-prod-region-a", namespace="monitoring", component="vmstorage"
    )
    assert sorted(r.resource for r in groups[region_a]) == ["cpu", "memory"]
    assert len(groups) == 2


def test_extract_value_prefers_ref_id_then_first_number() -> None:
    assert extract_value({"A": 1.0, "B": 2.0}, "B") == 2.0
    assert extract_value({"A": 1.0, "C": None}, "B") == 1.0
    assert extract_value({"B": None}, "B") is None
    assert extract_value(None, "B") is None


def test_extra_fields_are_allowed() -> None:
    payload = load_fixture("grafana_firing_cpu.json")
    payload["somethingNew"] = {"x": 1}
    assert len(parse_webhook(GrafanaWebhook.model_validate(payload), "B").requests) == 1
