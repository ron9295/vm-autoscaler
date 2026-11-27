import pytest

from app.adapters.manifest import (
    changed_lines,
    check_diff,
    dump,
    get_path,
    load,
    restyle_quantity,
    select_document,
    set_annotations,
    set_path,
)
from app.domain.errors import GitOpsManifestError
from tests.conftest import FIXTURES

VMCLUSTER = (FIXTURES / "vmcluster.yaml").read_text()

HELM = """\
# Helm values for victoria-metrics-cluster
vmstorage:
  replicaCount: 3
  resources:
    limits:
      cpu: 4          # cores
      memory: 16384Mi
  tolerations:
    - key: dedicated
      value: monitoring
"""


def test_round_trip_is_byte_identical() -> None:
    assert dump(load(VMCLUSTER)) == VMCLUSTER
    assert dump(load(HELM)) == HELM


def test_round_trip_without_trailing_newline() -> None:
    assert dump(load("a: 1\nb: 2")) == "a: 1\nb: 2"


def test_select_document_by_kind_and_name() -> None:
    manifest = load(VMCLUSTER)
    doc = select_document(manifest, "VMCluster", "vm-main")
    assert get_path(doc, "spec.vmstorage.replicaCount") == 3


def test_select_document_ambiguous_or_missing() -> None:
    manifest = load(VMCLUSTER)
    with pytest.raises(GitOpsManifestError, match="2 YAML documents match"):
        select_document(manifest, "VMCluster", None)
    with pytest.raises(GitOpsManifestError, match="no YAML document"):
        select_document(manifest, "VMCluster", "nope")


def test_select_single_helm_document_without_kind() -> None:
    doc = select_document(load(HELM), None, None)
    assert get_path(doc, "vmstorage.replicaCount") == 3


def test_get_path_missing_is_none() -> None:
    doc = select_document(load(VMCLUSTER), "VMCluster", "vm-main")
    assert get_path(doc, "spec.vmstorage.resources.limits.cpu") == "4"
    assert get_path(doc, "spec.vmstorage.nope.cpu") is None


def test_set_path_keeps_comments_and_order() -> None:
    manifest = load(VMCLUSTER)
    doc = select_document(manifest, "VMCluster", "vm-main")
    old = get_path(doc, "spec.vmstorage.resources.limits.cpu")
    set_path(doc, "spec.vmstorage.resources.limits.cpu", restyle_quantity(old, "5", "cpu"))
    after = dump(manifest)
    assert changed_lines(VMCLUSTER, after) == ['        cpu: "5"   # see capacity doc']
    check_diff(VMCLUSTER, after, max_changed=1, max_added=0)


def test_set_path_missing_parent_raises() -> None:
    doc = select_document(load(VMCLUSTER), "VMCluster", "vm-main")
    with pytest.raises(GitOpsManifestError, match="parent 'spec.vmstorage.nope' does not exist"):
        set_path(doc, "spec.vmstorage.nope.cpu", "1")
    with pytest.raises(GitOpsManifestError, match="never created"):
        set_path(doc, "spec.vmstorage.resources.limits.gpu", "1")


def test_annotations_map_is_created() -> None:
    manifest = load(VMCLUSTER)
    doc = select_document(manifest, "VMCluster", "vm-main")
    set_annotations(
        doc, "metadata.annotations", {"vm-autoscaler/last-scaled-cpu": "2026-11-27T12:00:00Z"}
    )
    after = dump(manifest)
    assert changed_lines(VMCLUSTER, after) == [
        "  annotations:",
        '    vm-autoscaler/last-scaled-cpu: "2026-11-27T12:00:00Z"',
    ]
    check_diff(VMCLUSTER, after, max_changed=0, max_added=2)


def test_annotations_cannot_create_parents() -> None:
    doc = select_document(load(HELM), None, None)
    with pytest.raises(GitOpsManifestError, match="parent does not exist"):
        set_annotations(doc, "metadata.annotations", {"k": "v"})


def test_existing_annotation_updated_in_place() -> None:
    text = 'metadata:\n  annotations:\n    vm-autoscaler/last-scaled-cpu: "old"\n    keep: me\n'
    manifest = load(text)
    set_annotations(
        manifest.documents[0], "metadata.annotations", {"vm-autoscaler/last-scaled-cpu": "new"}
    )
    assert dump(manifest) == text.replace('"old"', '"new"')


@pytest.mark.parametrize(
    ("old_yaml", "new_text", "kind", "expected_yaml"),
    [
        ('cpu: "4"', "5", "cpu", 'cpu: "5"'),
        ("cpu: '4'", "5", "cpu", "cpu: '5'"),
        ("cpu: 4", "5", "cpu", "cpu: 5"),
        ("cpu: 2", "2500m", "cpu", "cpu: 2500m"),
        ("cpu: 4000m", "5", "cpu", "cpu: 5000m"),
        ("cpu: 1.5", "1900m", "cpu", "cpu: 1.9"),
        ("cpu: 500m", "700m", "cpu", "cpu: 700m"),
        ("memory: 16Gi", "20Gi", "bytes", "memory: 20Gi"),
        ("memory: 16384Mi", "20Gi", "bytes", "memory: 20480Mi"),
        ('memory: "16Gi"', "20Gi", "bytes", 'memory: "20Gi"'),
        ("replicaCount: 3", "4", "count", "replicaCount: 4"),
    ],
)
def test_quantity_style_preserved(
    old_yaml: str, new_text: str, kind: str, expected_yaml: str
) -> None:
    manifest = load(old_yaml + "\n")
    doc = manifest.documents[0]
    key = old_yaml.split(":")[0]
    set_path(doc, key, restyle_quantity(doc[key], new_text, kind))  # type: ignore[arg-type]
    assert dump(manifest) == expected_yaml + "\n"


def test_helm_sequence_indent_preserved() -> None:
    manifest = load(HELM)
    doc = manifest.documents[0]
    old = get_path(doc, "vmstorage.resources.limits.memory")
    set_path(doc, "vmstorage.resources.limits.memory", restyle_quantity(old, "20Gi", "bytes"))
    after = dump(manifest)
    assert changed_lines(HELM, after) == ["      memory: 20480Mi"]


def test_check_diff_rejects_drift() -> None:
    with pytest.raises(GitOpsManifestError, match="refusing to commit"):
        check_diff("a: 1\nb: 2\nc: 3\n", "a: 9\nb: 9\nc: 3\n", max_changed=1, max_added=0)


def test_invalid_yaml() -> None:
    with pytest.raises(GitOpsManifestError, match="not valid YAML"):
        load("a: [1, 2\n")
