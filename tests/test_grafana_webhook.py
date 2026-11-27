import pytest

from app.api.grafana_webhook import (
    GrafanaWebhook,
    extract_value,
    parse_webhook,
)
from app.domain.models import TargetKey
from app.services.scaling_service import group_by_target
from tests.conftest import load_fixture


def _parse(name: str):  # type: ignore[no-untyped-def]
    return parse_webhook(GrafanaWebhook.model_validate(load_fixture(name)), value_ref_id="B")


def test_firing_storage_alert() -> None:
    parsed = _parse("grafana_firing_storage.json")
    assert parsed.ignored == []
    [request] = parsed.requests
    assert request.resource == "storage"
    assert request.value == 85.0
    assert request.autoscale is True
    assert request.fingerprint == "a1b2c3d4e5f60001"
    assert request.target_key == TargetKey(
        ocp_cluster="ocp-prod-region-a", namespace="monitoring", component="vmstorage"
    )


def test_resolved_alert_is_ignored() -> None:
    parsed = _parse("grafana_resolved.json")
    assert parsed.requests == []
    assert [i.reason for i in parsed.ignored] == ["resolved"]


def test_missing_label_is_ignored_not_rejected(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        parsed = _parse("grafana_missing_label.json")
    assert parsed.requests == []
    [ignored] = parsed.ignored
    assert (ignored.fingerprint, ignored.reason) == ("a1b2c3d4e5f60002", "missing_label")
    assert "ocp_cluster" in str(getattr(caplog.records[-1], "fields", {}))


def test_malformed_alert_does_not_block_the_valid_one() -> None:
    payload = load_fixture("grafana_firing_storage.json")
    broken = {**payload["alerts"][0], "fingerprint": "broken"}
    broken["labels"] = {k: v for k, v in broken["labels"].items() if k != "component"}
    payload["alerts"].insert(0, broken)
    parsed = parse_webhook(GrafanaWebhook.model_validate(payload), value_ref_id="B")
    assert [r.fingerprint for r in parsed.requests] == ["a1b2c3d4e5f60001"]
    assert [(i.fingerprint, i.reason) for i in parsed.ignored] == [("broken", "missing_label")]


def test_autoscale_not_true_is_flagged() -> None:
    [request] = _parse("grafana_autoscale_false.json").requests
    assert request.autoscale is False


@pytest.mark.parametrize("resource", ["cpu", "memory", "replicas", "disk"])
def test_unsupported_resource_is_ignored_not_rejected(resource: str) -> None:
    payload = load_fixture("grafana_firing_storage.json")
    payload["alerts"][0]["labels"]["resource"] = resource
    parsed = parse_webhook(GrafanaWebhook.model_validate(payload), value_ref_id="B")
    assert parsed.requests == []
    [ignored] = parsed.ignored
    assert (ignored.reason, ignored.status) == ("unsupported_resource", "firing")


def test_mixed_batch_keeps_the_storage_alert() -> None:
    payload = load_fixture("grafana_firing_storage.json")
    leftover = {**payload["alerts"][0], "fingerprint": "old-cpu-rule"}
    leftover["labels"] = {**leftover["labels"], "resource": "cpu"}
    payload["alerts"].append(leftover)
    parsed = parse_webhook(GrafanaWebhook.model_validate(payload), value_ref_id="B")
    assert [r.resource for r in parsed.requests] == ["storage"]
    assert [(i.fingerprint, i.reason) for i in parsed.ignored] == [
        ("old-cpu-rule", "unsupported_resource")
    ]


def test_missing_label_wins_over_unsupported_resource() -> None:
    payload = load_fixture("grafana_firing_storage.json")
    payload["alerts"][0]["labels"]["resource"] = "cpu"
    del payload["alerts"][0]["labels"]["namespace"]
    parsed = parse_webhook(GrafanaWebhook.model_validate(payload), value_ref_id="B")
    assert parsed.requests == []
    assert [i.reason for i in parsed.ignored] == ["missing_label"]


def test_multiple_alerts_grouped_by_target() -> None:
    parsed = _parse("grafana_multi.json")
    assert len(parsed.requests) == 3
    assert len(parsed.ignored) == 1
    groups = group_by_target(parsed.requests)
    assert len(groups) == 3
    region_a = TargetKey(
        ocp_cluster="ocp-prod-region-a", namespace="monitoring", component="vmstorage"
    )
    assert [r.value for r in groups[region_a]] == [85.0]


def test_extract_value_uses_only_the_configured_ref_id() -> None:
    assert extract_value({"A": 1.0, "B": 2.0}, "B") == 2.0
    assert extract_value({"B": 7}, "B") == 7.0
    assert extract_value({"A": 1.0, "C": None}, "B") is None
    assert extract_value({"B": None}, "B") is None
    assert extract_value({"B": True}, "B") is None
    assert extract_value({"B": "85"}, "B") is None
    assert extract_value(None, "B") is None
    assert extract_value({}, "B") is None


def test_extract_value_warns_when_ref_id_absent(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        assert extract_value({"A": 1.0, "C": 2.0}, "B") is None
    fields = getattr(caplog.records[-1], "fields", {})
    assert fields["ref_id"] == "B"
    assert fields["available"] == ["A", "C"]


def test_missing_ref_id_value_means_value_missing_request() -> None:
    payload = load_fixture("grafana_firing_storage.json")
    payload["alerts"][0]["values"] = {"A": 85.0}
    [request] = parse_webhook(GrafanaWebhook.model_validate(payload), "B").requests
    assert request.value is None


def test_extra_fields_are_allowed() -> None:
    payload = load_fixture("grafana_firing_storage.json")
    payload["somethingNew"] = {"x": 1}
    assert len(parse_webhook(GrafanaWebhook.model_validate(payload), "B").requests) == 1
