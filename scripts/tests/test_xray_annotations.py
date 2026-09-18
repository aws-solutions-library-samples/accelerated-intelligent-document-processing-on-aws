# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Assert X-Ray annotation keys and values are shapes the SDK actually keeps.

Four pipeline Lambdas wrote `xray_recorder.put_annotation("document_id",
{document.id})`. The braces make a one-element *set*, not the string. X-Ray
annotations must be a string, number or boolean, so the value was **dropped** —
`Entity.put_annotation` logs "ignoring unsupported annotation value type" and
returns without touching `self.annotations`, so nothing is recorded at all and
nothing is stringified. Trace filtering by document id therefore did not work on
exactly the four functions that do the expensive work (OCR, classification,
extraction, assessment). The same call was already correct in the two
rule-validation functions, which is how it reads as copy-paste drift rather than
intent.

The key is validated the same silent way: `Entity.put_annotation` drops any key
containing a character outside `_valid_annotation_key_characters`
(`string.ascii_letters + string.digits + '_'`), so `"document.id"` records
nothing either. Both are checked here.

Two layers, deliberately:

* `test_sdk_drops_*` calls the real `aws_xray_sdk` Segment offline and asserts
  what survives in `segment.annotations`. This proves the *behaviour* — it would
  have caught the original bug directly rather than by pattern-matching — and it
  is what keeps the AST rules below honest if the SDK's validation ever changes.
* The AST scan proves the *source has the right shape* everywhere, including the
  files no linter or type checker looks at (see below). Kept as a second line
  because the behavioural test can only cover call sites a test actually invokes,
  and these four are Lambda handlers that need a full document to run.

Nothing could have caught this on the way in:

* The four files live under `patterns/`, which `ruff.toml` excludes wholesale,
  so no ruff rule runs on them at all.
* Even with ruff enabled there, `B018` (useless-expression) does not fire. B018
  flags a useless expression *statement*; here the set literal is an argument,
  which is a legitimate position for a set. Verified empirically against these
  files before this test was written.
* basedpyright cannot see it either: `patterns/*/src` is in pyrightconfig's
  `exclude`, and `reportArgumentType` is "none" repo-wide.

So the guard has to be a static assertion of its own, which is this file. It
parses the AST rather than grepping, so it also catches the list and dict
spellings and is not confused by formatting or line breaks.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Where X-Ray instrumentation lives. Kept broad on purpose: the defect spread by
# copy-paste, so the check must cover anywhere a handler could be added.
SEARCH_ROOTS = ("patterns", "src/lambda", "nested", "feature-platform", "options")

SKIP_DIR_NAMES = {
    "__pycache__",
    "node_modules",
    ".aws-sam",
    "build",
    "dist",
    ".venv",
    "vendor",
}

# put_annotation(key, value) — value must be a scalar.
# put_metadata(key, value) legitimately accepts dicts, so it is not checked.
CHECKED_METHOD = "put_annotation"

BAD_VALUE_NODES = (ast.Set, ast.SetComp, ast.List, ast.ListComp, ast.Dict, ast.Tuple)

# aws_xray_sdk.core.models.entity._valid_annotation_key_characters is
# `string.ascii_letters + string.digits + '_'`. Anything else and the SDK logs
# "ignoring annnotation with unsupported characters in key" (its typo) and drops
# the annotation.
VALID_ANNOTATION_KEY = re.compile(r"\A[A-Za-z0-9_]+\Z")


def _python_files() -> list[Path]:
    files: list[Path] = []
    for root in SEARCH_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if any(
                part in SKIP_DIR_NAMES for part in path.relative_to(REPO_ROOT).parts
            ):
                continue
            files.append(path)
    return sorted(files)


def _annotation_calls(path: Path) -> list[ast.Call]:
    """Every `<something>.put_annotation(...)` call in the file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - not our concern
        return []
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == CHECKED_METHOD
    ]


def _arg(call: ast.Call, position: int, name: str) -> ast.expr | None:
    """The `name` argument of `call`, whether passed positionally or by keyword.

    Reading only `call.args` was itself a hole in this guard: the defect in
    keyword form — `put_annotation(key="document_id", value={document.id})` — is
    dropped by the SDK identically, and a developer fixing a call site is quite
    likely to switch to keyword form for clarity while carrying the stray braces
    across. `**kwargs` is not resolvable statically and is reported by
    `test_no_put_annotation_hides_its_arguments` rather than skipped.
    """
    if len(call.args) > position:
        return call.args[position]
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


pytestmark = pytest.mark.unit


def test_the_scan_finds_the_instrumentation() -> None:
    """Guard against this file passing because it searched the wrong place.

    If the roots list or the AST match ever stops finding put_annotation calls,
    every other assertion here becomes vacuous and silent.
    """
    total = sum(len(_annotation_calls(p)) for p in _python_files())
    assert total >= 10, (
        f"expected to find the X-Ray put_annotation calls, found {total}. "
        "SEARCH_ROOTS or the AST matcher is probably stale — fix the scan before "
        "trusting this file."
    )


def test_no_put_annotation_value_is_a_collection_literal() -> None:
    findings: list[str] = []
    for path in _python_files():
        for call in _annotation_calls(path):
            value = _arg(call, 1, "value")
            if value is None:
                continue
            if isinstance(value, BAD_VALUE_NODES):
                rel = path.relative_to(REPO_ROOT)
                findings.append(
                    f"{rel}:{value.lineno} passes "
                    f"{type(value).__name__} as the annotation value"
                )

    assert not findings, (
        "X-Ray annotation values must be a string, number or boolean. These calls "
        "pass a collection literal, which the SDK drops outright — nothing is "
        "recorded and nothing is stringified — so trace filtering silently stops "
        "working:\n  " + "\n  ".join(findings) + "\n"
        "This is almost always a stray pair of braces: "
        'put_annotation("document_id", {document.id}) should be '
        'put_annotation("document_id", document.id).'
    )


def test_no_put_annotation_key_has_invalid_characters() -> None:
    """A key outside `[A-Za-z0-9_]` is dropped just as silently as a bad value.

    Only literal keys can be checked statically. A computed key (an f-string, a
    variable, a `"prefix" + name`) is skipped, which is a real limit of this
    scan; the behavioural test below is what covers the SDK rule itself.
    """
    findings: list[str] = []
    for path in _python_files():
        for call in _annotation_calls(path):
            key = _arg(call, 0, "key")
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            if not VALID_ANNOTATION_KEY.match(key.value):
                rel = path.relative_to(REPO_ROOT)
                findings.append(f"{rel}:{key.lineno} uses key {key.value!r}")

    assert not findings, (
        "X-Ray annotation keys may contain only letters, digits and underscore "
        "(`aws_xray_sdk...entity._valid_annotation_key_characters`). The SDK logs "
        "a warning and drops the annotation for anything else, so the trace field "
        "simply never appears:\n  " + "\n  ".join(findings) + "\n"
        "Use `document_id`, not `document.id` or `document-id`."
    )


def test_no_put_annotation_hides_its_arguments() -> None:
    """No call may pass its key/value through `*args` or `**kwargs`.

    Both checks above resolve arguments statically, so a splatted call would be
    skipped and neither rule would apply to it — a hole that would widen exactly
    when someone wraps `put_annotation` in a helper. There are none today; this
    keeps it that way rather than letting the scan quietly stop covering a call.
    """
    findings: list[str] = []
    for path in _python_files():
        for call in _annotation_calls(path):
            splatted = any(isinstance(a, ast.Starred) for a in call.args) or any(
                kw.arg is None for kw in call.keywords
            )
            if splatted:
                rel = path.relative_to(REPO_ROOT)
                findings.append(f"{rel}:{call.lineno}")

    assert not findings, (
        "these put_annotation calls pass arguments via *args/**kwargs, which this "
        "file cannot inspect statically — the key/value rules would not apply to "
        "them:\n  " + "\n  ".join(findings) + "\n"
        "Pass the key and value explicitly, or extend this scan to follow the "
        "wrapper."
    )


# --------------------------------------------------------------------------
# Behavioural layer: what the SDK actually keeps, checked against the SDK.
#
# Entirely offline — a bare `Segment` needs no recorder, no sampling decision
# and no network. `importorskip` rather than a module-scope import so the AST
# rules above still run in an environment without the optional extra;
# `aws-xray-sdk` is declared in idp_common_pkg's `test` extra (as well as
# `docs_service`) so CI, which installs `[all,dev,test]`, does run this.
# --------------------------------------------------------------------------


def _segment():
    segment_mod = pytest.importorskip(
        "aws_xray_sdk.core.models.segment",
        reason="aws-xray-sdk not installed; the AST rules above still apply",
    )
    return segment_mod.Segment(name="test-xray-annotations")


def test_sdk_keeps_scalar_annotation_values() -> None:
    """Baseline: without this the drop assertions below could pass vacuously."""
    segment = _segment()
    segment.put_annotation("document_id", "abc-123")
    segment.put_annotation("page_count", 7)
    segment.put_annotation("cached", True)
    assert segment.annotations == {
        "document_id": "abc-123",
        "page_count": 7,
        "cached": True,
    }


@pytest.mark.parametrize(
    "value",
    [
        pytest.param({"abc-123"}, id="set-the-shipped-bug"),
        pytest.param(["abc-123"], id="list"),
        pytest.param(("abc-123",), id="tuple"),
        pytest.param({"id": "abc-123"}, id="dict"),
    ],
)
def test_sdk_drops_collection_annotation_values(value: object) -> None:
    """The original defect, reproduced against the real SDK.

    `put_annotation("document_id", {document.id})` records NOTHING — it is not
    stringified to `{'abc-123'}`, which is what the first draft of this file
    hedged. That is the whole reason the bug was invisible: no wrong value to
    notice in a trace, just a missing field.
    """
    segment = _segment()
    segment.put_annotation("document_id", value)
    assert segment.annotations == {}, (
        f"expected the SDK to drop a {type(value).__name__} annotation value, but "
        f"it recorded {segment.annotations!r}. If the SDK has started coercing "
        "these, the AST rule above may no longer describe a real defect — "
        "re-check before relaxing it."
    )


@pytest.mark.parametrize(
    "key", ["document.id", "document-id", "document id", "document:id"]
)
def test_sdk_drops_annotation_keys_with_invalid_characters(key: str) -> None:
    segment = _segment()
    segment.put_annotation(key, "abc-123")
    assert segment.annotations == {}, (
        f"expected the SDK to drop key {key!r}, but it recorded "
        f"{segment.annotations!r}."
    )
