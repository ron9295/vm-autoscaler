import pytest

from app.adapters.yaml_edit import (
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

VALUES = (FIXTURES / "values.yaml").read_text()
SIZE = "vmstorage.pvc.size"

MULTI_DOC = """\
# leading comment before the first document
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: extra
---
apiVersion: operator.victoriametrics.com/v1beta1
kind: VMCluster
metadata:
  name: vm-main
spec:
  vmstorage:
    pvc:
      size: 30Gi
---
apiVersion: operator.victoriametrics.com/v1beta1
kind: VMCluster
metadata:
  name: vm-other
spec: {}
"""


def _resize(text: str, new: str, path: str = SIZE) -> str:
    manifest = load(text)
    doc = select_document(manifest, None, None)
    set_path(doc, path, restyle_quantity(get_path(doc, path), new))
    return dump(manifest)


def test_round_trip_is_byte_identical() -> None:
    assert dump(load(VALUES)) == VALUES
    assert dump(load(MULTI_DOC)) == MULTI_DOC


def test_round_trip_without_trailing_newline() -> None:
    assert dump(load("a: 1\nb: 2")) == "a: 1\nb: 2"


def test_resize_changes_only_the_size_line_and_keeps_comment() -> None:
    after = _resize(VALUES, "36Gi")
    assert changed_lines(VALUES, after) == ["    size: 36Gi   # grown by vm-autoscaler"]
    check_diff(VALUES, after, max_changed=1, max_added=0)
    assert get_path(load(after).documents[0], "vmselect.pvc.size") == "10Gi"  # untouched


def test_get_path() -> None:
    doc = select_document(load(VALUES), None, None)
    assert get_path(doc, SIZE) == "30Gi"
    assert get_path(doc, "vmstorage.nope.size") is None
    assert get_path(doc, "vmstorage.pvc.size.deeper") is None


def test_set_path_never_creates_structure() -> None:
    doc = select_document(load(VALUES), None, None)
    with pytest.raises(GitOpsManifestError, match="parent 'vmstorage.persistence' does not exist"):
        set_path(doc, "vmstorage.persistence.size", "1Gi")
    with pytest.raises(GitOpsManifestError, match="never created"):
        set_path(doc, "vmstorage.pvc.capacity", "1Gi")
    with pytest.raises(GitOpsManifestError, match="empty path"):
        set_path(doc, "", "1Gi")


def test_select_document_by_kind_and_name() -> None:
    manifest = load(MULTI_DOC)
    doc = select_document(manifest, "VMCluster", "vm-main")
    assert get_path(doc, "spec.vmstorage.pvc.size") == "30Gi"


def test_select_document_ambiguous_or_missing() -> None:
    manifest = load(MULTI_DOC)
    with pytest.raises(GitOpsManifestError, match="2 YAML documents match"):
        select_document(manifest, "VMCluster", None)
    with pytest.raises(GitOpsManifestError, match="no YAML document"):
        select_document(manifest, "VMCluster", "nope")


def test_select_single_values_document_without_kind() -> None:
    assert get_path(select_document(load(VALUES), None, None), "vmstorage.replicaCount") == 3


def test_annotations_map_created_at_top_level() -> None:
    manifest = load(VALUES)
    doc = manifest.documents[0]
    set_annotations(
        doc, "vmAutoscaler", {"vm-autoscaler/last-scaled-storage": "2026-11-27T12:00:00Z"}
    )
    after = dump(manifest)
    assert changed_lines(VALUES, after) == [
        "vmAutoscaler:",
        '  vm-autoscaler/last-scaled-storage: "2026-11-27T12:00:00Z"',
    ]
    check_diff(VALUES, after, max_changed=0, max_added=2)


def test_annotations_cannot_create_parents() -> None:
    doc = load(VALUES).documents[0]
    with pytest.raises(GitOpsManifestError, match="parent does not exist"):
        set_annotations(doc, "metadata.annotations", {"k": "v"})


def test_annotations_path_must_be_a_mapping() -> None:
    doc = load(VALUES).documents[0]
    with pytest.raises(GitOpsManifestError, match="not a mapping"):
        set_annotations(doc, "vmstorage.replicaCount", {"k": "v"})


def test_existing_annotation_updated_in_place() -> None:
    text = 'vmAutoscaler:\n  vm-autoscaler/last-scaled-storage: "old"\n  keep: me\n'
    manifest = load(text)
    set_annotations(
        manifest.documents[0], "vmAutoscaler", {"vm-autoscaler/last-scaled-storage": "new"}
    )
    assert dump(manifest) == text.replace('"old"', '"new"')


@pytest.mark.parametrize(
    ("old_yaml", "new_text", "expected_yaml"),
    [
        ("size: 30Gi", "36Gi", "size: 36Gi"),
        ('size: "30Gi"', "36Gi", 'size: "36Gi"'),
        ("size: '30Gi'", "36Gi", "size: '36Gi'"),
        ("size: 30720Mi", "36Gi", "size: 36864Mi"),  # Mi stays Mi
        ("size: 1Ti", "1229Gi", "size: 1229Gi"),  # not a whole Ti: falls back to Gi
        ("size: 32212254720", "36Gi", "size: 38654705664"),  # plain bytes stay plain bytes
        ('size: "32212254720"', "36Gi", 'size: "38654705664"'),
    ],
)
def test_size_style_preserved(old_yaml: str, new_text: str, expected_yaml: str) -> None:
    assert _resize(old_yaml + "\n", new_text, path="size") == expected_yaml + "\n"


def test_indented_sequence_style_preserved() -> None:
    # The fixture indents list items under their key ("  tolerations:\n    - key"):
    # the round trip must keep that, or every list line would show up in the diff.
    after = _resize(VALUES, "36Gi")
    assert "  tolerations:\n    - key: dedicated" in after


def test_unindented_sequence_style_preserved() -> None:
    text = "vmstorage:\n  pvc:\n    size: 30Gi\n  tolerations:\n  - key: a\n    value: b\n"
    assert changed_lines(text, _resize(text, "36Gi")) == ["    size: 36Gi"]


def test_check_diff_rejects_drift() -> None:
    with pytest.raises(GitOpsManifestError, match="refusing to commit"):
        check_diff("a: 1\nb: 2\nc: 3\n", "a: 9\nb: 9\nc: 3\n", max_changed=1, max_added=0)
    with pytest.raises(GitOpsManifestError, match="added 1"):
        check_diff("a: 1\n", "a: 2\nb: 3\n", max_changed=1, max_added=0)


def test_invalid_yaml() -> None:
    with pytest.raises(GitOpsManifestError, match="not valid YAML"):
        load("a: [1, 2\n")


def test_crlf_file_keeps_crlf_and_changes_only_the_size_line() -> None:
    crlf = VALUES.replace("\n", "\r\n")
    assert dump(load(crlf)) == crlf  # byte-identical round trip
    after = _resize(crlf, "36Gi")
    assert after.count("\r\n") == crlf.count("\r\n")
    assert "\n" not in after.replace("\r\n", "")
    check_diff(crlf, after, max_changed=1, max_added=0)
    assert changed_lines(crlf, after) == ["    size: 36Gi   # grown by vm-autoscaler"]


def test_line_ending_rewrite_is_refused() -> None:
    crlf = VALUES.replace("\n", "\r\n")
    with pytest.raises(GitOpsManifestError, match="refusing to commit"):
        check_diff(crlf, crlf.replace("\r\n", "\n"), max_changed=1, max_added=0)


def test_mixed_line_endings_are_refused() -> None:
    mixed = "vmstorage:\r\n  pvc:\n    size: 30Gi\n"
    after = _resize(mixed, "36Gi")
    with pytest.raises(GitOpsManifestError, match="refusing to commit"):
        check_diff(mixed, after, max_changed=1, max_added=0)
