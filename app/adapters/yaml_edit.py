"""Edit Kubernetes / Helm YAML files without disturbing anything else.

Uses ruamel.yaml in round-trip mode, so comments, key order, quotes and indentation are
kept. The goal: the diff in GitLab shows only the values we changed.

Rules:
* `set_path` never creates structure. Every key on the path must already exist.
* The only structure we may create is the cooldown annotations map (`ensure_mapping`).
* `check_diff` refuses an edit that changed more lines than expected (formatting drift),
  including line-ending changes.
* The file's newline style is kept: a CRLF file is written back with CRLF.
"""

from __future__ import annotations

import difflib
import io
import re
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import DoubleQuotedScalarString, SingleQuotedScalarString

from app.domain.errors import GitOpsManifestError
from app.domain.quantity import QuantityError, parse_bytes

_SEQUENCE_ITEM_RE = re.compile(r"^(\s*)- ")
_KEY_LINE_RE = re.compile(r"^(\s*)[^\s#-][^:#]*:\s*(#.*)?$")
_BINARY_UNITS = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5}


# --------------------------------------------------------------------------------------
# Load / dump
# --------------------------------------------------------------------------------------


@dataclass
class Manifest:
    """A parsed YAML file: its documents plus what we need to write it back identically."""

    documents: list[Any]
    yaml: YAML
    preamble: str  # comment lines before the first `---` (ruamel would drop them)
    trailing_newline: bool
    newline: str  # "\n" or "\r\n", as in the original file


def _detect_indent(text: str) -> tuple[int, int, int]:
    """Guess (mapping, sequence, offset) indentation from the file, so ruamel matches it."""
    lines = [
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    mapping = 2
    for previous, line in pairwise(lines):
        key = _KEY_LINE_RE.match(previous)
        if key and not _SEQUENCE_ITEM_RE.match(line):
            step = len(line) - len(line.lstrip()) - len(key.group(1))
            if step > 0:
                mapping = step
                break
    offset = 0
    for previous, line in pairwise(lines):
        item = _SEQUENCE_ITEM_RE.match(line)
        key = _KEY_LINE_RE.match(previous)
        if item and key:
            offset = max(len(item.group(1)) - len(key.group(1)), 0)
            break
    return mapping, offset + 2, offset


def _split_preamble(text: str) -> tuple[str, str]:
    """Split off leading comment/blank lines when they are followed by a `---` line."""
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("---"):
            return "".join(lines[:index]), "".join(lines[index:])
        if stripped and not stripped.startswith("#"):
            break
    return "", text


def load(text: str) -> Manifest:
    """Parse a (possibly multi-document) YAML file in round-trip mode.

    CRLF files are parsed as LF and written back as CRLF by `dump`. (ruamel on its own
    writes a mix of both.) A file with mixed line endings comes back with one style only,
    which `check_diff` then refuses.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing_newline = text.endswith("\n")
    text = text.replace("\r\n", "\n")
    preamble, body = _split_preamble(text)
    mapping, sequence, offset = _detect_indent(body)
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096  # never re-wrap long lines
    yaml.indent(mapping=mapping, sequence=sequence, offset=offset)
    yaml.explicit_start = body.lstrip().startswith("---")
    try:
        documents = [doc for doc in yaml.load_all(body)]
    except YAMLError as exc:
        raise GitOpsManifestError(f"file is not valid YAML: {exc}") from exc
    return Manifest(documents, yaml, preamble, trailing_newline, newline)


def dump(manifest: Manifest) -> str:
    """Write the documents back to text."""
    stream = io.StringIO()
    manifest.yaml.dump_all(manifest.documents, stream)
    text = manifest.preamble + stream.getvalue()
    if not manifest.trailing_newline:
        text = text.rstrip("\n")
    if manifest.newline != "\n":
        text = text.replace("\n", manifest.newline)
    return text


# --------------------------------------------------------------------------------------
# Documents and paths
# --------------------------------------------------------------------------------------


def select_document(manifest: Manifest, kind: str | None, name: str | None) -> Any:
    """Return the one document whose `kind` and `metadata.name` match (None = any)."""
    matches = []
    for document in manifest.documents:
        if not isinstance(document, dict):
            continue
        if kind is not None and document.get("kind") != kind:
            continue
        if name is not None and get_path(document, "metadata.name") != name:
            continue
        matches.append(document)
    wanted = f"kind={kind or '*'} name={name or '*'}"
    if not matches:
        raise GitOpsManifestError(f"no YAML document with {wanted}")
    if len(matches) > 1:
        raise GitOpsManifestError(
            f"{len(matches)} YAML documents match {wanted}; set document_name"
        )
    return matches[0]


def _split_path(path: str) -> list[str]:
    return [part for part in path.split(".") if part]


def get_path(node: Any, path: str) -> Any:
    """Return the value at a dotted path, or None if any key on the way is missing."""
    current = node
    for key in _split_path(path):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def set_path(node: Any, path: str, value: Any) -> None:
    """Replace the value at a dotted path. Every key, including the last, must exist."""
    keys = _split_path(path)
    if not keys:
        raise GitOpsManifestError("empty path")
    parent = node
    for index, key in enumerate(keys[:-1]):
        if not isinstance(parent, dict) or key not in parent:
            missing = ".".join(keys[: index + 1])
            raise GitOpsManifestError(f"path {path!r}: parent {missing!r} does not exist")
        parent = parent[key]
    if not isinstance(parent, dict) or keys[-1] not in parent:
        raise GitOpsManifestError(f"path {path!r} does not exist (never created automatically)")
    parent[keys[-1]] = value


def ensure_mapping(node: Any, path: str) -> CommentedMap:
    """Return the mapping at `path`, creating ONLY its last key if missing (annotations)."""
    keys = _split_path(path)
    parent = node if len(keys) <= 1 else get_path(node, ".".join(keys[:-1]))
    if not isinstance(parent, dict):
        raise GitOpsManifestError(f"cannot create {path!r}: its parent does not exist")
    existing = parent.get(keys[-1])
    if existing is None:
        parent[keys[-1]] = CommentedMap()
    elif not isinstance(existing, dict):
        raise GitOpsManifestError(f"{path!r} exists but is not a mapping")
    result: CommentedMap = parent[keys[-1]]
    return result


def set_annotations(node: Any, path: str, annotations: dict[str, str]) -> None:
    """Write annotation values (always double-quoted strings) into the map at `path`."""
    if not annotations:
        return
    target = ensure_mapping(node, path)
    for key, value in annotations.items():
        target[key] = DoubleQuotedScalarString(value)


def scalar_text(value: Any) -> str | None:
    """A YAML scalar as text ("4" for both `4` and `"4"`). None stays None."""
    if value is None or isinstance(value, dict | list):
        return None
    return str(value)


# --------------------------------------------------------------------------------------
# Keeping the file's quantity style
# --------------------------------------------------------------------------------------


def restyle_quantity(old: Any, new_text: str) -> Any:
    """Return the new size `new_text` in the same style as the `old` value in the file.

    * Unit style: `30Gi` stays in Gi, `30720Mi` stays in Mi, plain bytes stay plain bytes
      (when the new size is a whole number of that unit).
    * Type style: a quoted string keeps its quotes; an unquoted number stays a number.
    """
    text = _match_byte_unit(str(old), new_text)
    if isinstance(old, DoubleQuotedScalarString):
        return DoubleQuotedScalarString(text)
    if isinstance(old, SingleQuotedScalarString):
        return SingleQuotedScalarString(text)
    if isinstance(old, int) and not isinstance(old, bool) and text.isdigit():
        return int(text)
    return text


def _match_byte_unit(old_text: str, new_text: str) -> str:
    try:
        new_bytes = parse_bytes(new_text)
    except QuantityError:
        return new_text
    if old_text.isdigit():
        return str(new_bytes)
    for suffix, multiplier in _BINARY_UNITS.items():
        if old_text.endswith(suffix) and new_bytes % multiplier == 0:
            return f"{new_bytes // multiplier}{suffix}"
    return new_text


# --------------------------------------------------------------------------------------
# Diff guard
# --------------------------------------------------------------------------------------


def check_diff(before: str, after: str, max_changed: int, max_added: int) -> None:
    """Refuse edits that touched more lines than expected (e.g. ruamel re-formatting).

    Lines are compared with their line endings, so a CRLF -> LF rewrite counts as a change.
    """
    matcher = difflib.SequenceMatcher(
        a=before.splitlines(keepends=True), b=after.splitlines(keepends=True), autojunk=False
    )
    changed = added = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            changed += i2 - i1
            added += max((j2 - j1) - (i2 - i1), 0)
        elif tag == "insert":
            added += j2 - j1
    if changed > max_changed or added > max_added:
        raise GitOpsManifestError(
            f"refusing to commit: the edit changed {changed} and added {added} lines, "
            f"expected at most {max_changed} changed and {max_added} added "
            "(the file's formatting is probably not round-trip safe)"
        )


def changed_lines(before: str, after: str) -> list[str]:
    """The new versions of changed/added lines, for logs and tests."""
    return [
        line[1:]
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line.startswith("+") and not line.startswith("+++")
    ]
