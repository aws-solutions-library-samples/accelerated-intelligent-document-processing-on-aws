# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The review statuses the web UI mirrors must match the backend's, in both directions.

``src/ui/src/components/common/review-status.ts`` mirrors four vocabularies of
``lib/idp_common_pkg/idp_common/review_queues/statuses.py``:

* ``QueueItemState``, the state of a review queue item. The UI labels each one
  through ``stateMessages``, one react-intl message per state, and shows a state
  it has no label for as stored, in every language;
* ``HitlKind``, what a stored ``HITLStatus`` value means once normalised;
* ``ReviewStatus`` and ``LegacyHitlStatus``, the stored ``HITLStatus`` spellings
  the UI's normaliser reads. A spelling it does not know reads as "no review".

Each of those fallbacks is what makes a gap quiet. A state added to the backend
reads in English exactly as a labelled one does, so no English test notices that
under ``fr-CA`` it alone stays untranslated; a status spelling the UI does not
know shows a document as never reviewed. In the other direction, a value only
the UI has is dead, and a label for it is translated for nothing.

So this test derives each pair from source and fails in **both** directions.
Both sides are read from **source**, not imported: the gate runs without the
package installed, and it checks what is committed.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
#: The UI's mirror of the review statuses, with the queue item state labels.
UI_MODULE = REPO_ROOT / "src/ui/src/components/common/review-status.ts"
#: The backend's one declaration of the review statuses.
BACKEND_MODULE = REPO_ROOT / "lib/idp_common_pkg/idp_common/review_queues/statuses.py"


def _backend_tree() -> ast.Module:
    assert BACKEND_MODULE.is_file(), (
        f"{BACKEND_MODULE.relative_to(REPO_ROOT)} has moved; update BACKEND_MODULE"
    )
    return ast.parse(
        BACKEND_MODULE.read_text(encoding="utf-8"), filename=str(BACKEND_MODULE)
    )


def _string_value(node: ast.expr, where: str) -> str:
    assert isinstance(node, ast.Constant) and isinstance(node.value, str), (
        f"{BACKEND_MODULE.relative_to(REPO_ROOT)}:{node.lineno}: `{ast.unparse(node)}` "
        f"in {where} is not a string literal. Teach this reader that form rather "
        "than skip it: a skipped value is one this gate never checks against the UI."
    )
    return node.value


def _backend_enum(name: str) -> set[str]:
    """The string value of every member of ``class <name>(str, Enum)``."""
    enum = next(
        (
            node
            for node in _backend_tree().body
            if isinstance(node, ast.ClassDef) and node.name == name
        ),
        None,
    )
    assert enum is not None, (
        f"{BACKEND_MODULE.relative_to(REPO_ROOT)} no longer declares `class {name}`. "
        "If the vocabulary moved, point this gate at its new home rather than "
        "deleting it."
    )
    return {
        _string_value(node.value, f"`{name}`")
        for node in enum.body
        if isinstance(node, ast.Assign)
    }


def _backend_literal(name: str) -> set[str]:
    """The strings of the module-level ``<name> = Literal["...", ...]``."""
    value = next(
        (
            node.value
            for node in _backend_tree().body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets
            )
        ),
        None,
    )
    assert value is not None, (
        f"{BACKEND_MODULE.relative_to(REPO_ROOT)} no longer declares a module-level "
        f"`{name} = Literal[...]`. If the vocabulary moved, point this gate at its "
        "new home rather than deleting it."
    )
    assert isinstance(value, ast.Subscript) and ast.unparse(value.value) in (
        "Literal",
        "typing.Literal",
    ), (
        f"`{name}` in {BACKEND_MODULE.relative_to(REPO_ROOT)} is no longer a "
        "`Literal[...]`. Teach this reader the new form rather than skipping it."
    )
    members = value.slice.elts if isinstance(value.slice, ast.Tuple) else [value.slice]
    return {_string_value(member, f"`{name}`") for member in members}


def _ui_source() -> str:
    return UI_MODULE.read_text(encoding="utf-8")


def _ui_state_labels() -> set[str]:
    """The top-level keys of ``const stateMessages = defineMessages<...>({ ... })``."""
    match = re.search(
        r"^const stateMessages = defineMessages<[^\n]*>\(\{\n(.*?)^\}\);",
        _ui_source(),
        re.DOTALL | re.MULTILINE,
    )
    assert match, (
        f"{UI_MODULE.relative_to(REPO_ROOT)} no longer declares "
        "`const stateMessages = defineMessages<...>({` on one line. That map is what "
        "gives each queue item state a translatable label; if it was restructured, "
        "update this gate to read the new shape rather than removing it."
    )
    # One `  KEY: {` per entry, at the map's own indentation; the nested `id`,
    # `defaultMessage` and `description` lines sit deeper.
    return set(re.findall(r"^ {2}([A-Z][A-Z_]*): \{$", match.group(1), re.MULTILINE))


def _ui_union(name: str) -> set[str]:
    """The members of ``export type <name> = 'a' | 'b' | ...;``."""
    match = re.search(rf"^export type {name} = ([^;]+);", _ui_source(), re.MULTILINE)
    assert match, (
        f"{UI_MODULE.relative_to(REPO_ROOT)} no longer declares `export type {name} = "
        "'...' | ...;`. If the mirror moved, point this gate at it rather than "
        "deleting it."
    )
    members = [part.strip() for part in match.group(1).split("|")]
    for member in members:
        assert re.fullmatch(r"'[^']*'", member), (
            f"`{member}` in the UI's `{name}` is not a single-quoted string literal. "
            "Teach this reader that form rather than skip it."
        )
    return {member[1:-1] for member in members}


def _ui_const(name: str) -> set[str]:
    """The values of ``export const <name> = { KEY: '...', ... } as const;``."""
    match = re.search(
        rf"^export const {name} = \{{\n(.*?)^\}} as const;",
        _ui_source(),
        re.DOTALL | re.MULTILINE,
    )
    assert match, (
        f"{UI_MODULE.relative_to(REPO_ROOT)} no longer declares `export const {name} "
        "= { ... } as const;`. If the mirror moved, point this gate at it rather than "
        "deleting it."
    )
    values: set[str] = set()
    for line in match.group(1).splitlines():
        entry = re.fullmatch(r" {2}[A-Z][A-Z_]*: '([^']*)',", line)
        assert entry, (
            f"`{line.strip()}` in the UI's `{name}` is not a `KEY: 'value',` entry. "
            "Teach this reader that form rather than skip it."
        )
        values.add(entry.group(1))
    return values


@dataclass(frozen=True)
class Mirror:
    """One backend vocabulary and the UI declaration that mirrors it."""

    name: str
    backend: Callable[[], set[str]]
    ui: Callable[[], set[str]]
    #: What the UI declaration is, for the failure messages.
    ui_name: str
    #: How many values each side had when this gate was written.
    floor: int


MIRRORS = (
    Mirror(
        "QueueItemState",
        lambda: _backend_enum("QueueItemState"),
        _ui_state_labels,
        "stateMessages map of queue item state labels",
        8,
    ),
    Mirror(
        "HitlKind",
        lambda: _backend_literal("HitlKind"),
        lambda: _ui_union("HitlKind"),
        "HitlKind union",
        7,
    ),
    Mirror(
        "ReviewStatus",
        lambda: _backend_enum("ReviewStatus"),
        lambda: _ui_const("ReviewStatus"),
        "ReviewStatus object",
        3,
    ),
    Mirror(
        "LegacyHitlStatus",
        lambda: _backend_enum("LegacyHitlStatus"),
        lambda: _ui_const("LegacyHitlStatus"),
        "LegacyHitlStatus object",
        8,
    ),
)


@pytest.mark.unit
@pytest.mark.parametrize("mirror", MIRRORS, ids=lambda mirror: mirror.name)
def test_every_backend_value_is_in_the_ui(mirror: Mirror):
    missing = sorted(mirror.backend() - mirror.ui())
    assert missing == [], (
        f"the backend's `{mirror.name}` has these values, but the UI's "
        f"{mirror.ui_name} lacks them, so the UI shows or reads them wrongly: "
        + json.dumps(missing)
        + f"\nAdd them to {UI_MODULE.relative_to(REPO_ROOT)} (a state label is keyed "
        "by the stored value, with the English label as the defaultMessage)."
    )


@pytest.mark.unit
@pytest.mark.parametrize("mirror", MIRRORS, ids=lambda mirror: mirror.name)
def test_the_ui_has_no_value_the_backend_does_not_define(mirror: Mirror):
    """A dead entry is translated or matched for nothing."""
    unknown = sorted(mirror.ui() - mirror.backend())
    assert unknown == [], (
        f"the UI's {mirror.ui_name} has values the backend's `{mirror.name}` does "
        "not define: " + json.dumps(unknown) + "\nEither the value was renamed on "
        "the backend, or the entry is dead and should be removed from "
        f"{UI_MODULE.relative_to(REPO_ROOT)}."
    )


@pytest.mark.unit
@pytest.mark.parametrize("mirror", MIRRORS, ids=lambda mirror: mirror.name)
def test_both_sides_are_non_empty(mirror: Mirror):
    """Non-vacuity: a reader that silently matched nothing would pass both above."""
    floor = (
        f"fewer than the {mirror.floor} `{mirror.name}` values this gate was written "
        "against; fix the reader, or lower the floor if the vocabulary really shrank"
    )
    assert len(mirror.backend()) >= mirror.floor, f"the backend reader found {floor}"
    assert len(mirror.ui()) >= mirror.floor, f"the UI reader found {floor}"
