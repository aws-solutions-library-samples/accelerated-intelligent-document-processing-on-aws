# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every backend document ``Status`` must have a message in the UI's status map.

The Documents list's Status column shows each stored value as it is (``QUEUED``,
``HITL_IN_PROGRESS``), so the UI reads like the CLI, the SDK and IDP Monitor. To
make those values translatable without changing the English, each is a react-intl
message in ``src/ui/src/components/common/object-status-renderer.tsx`` whose
English is the stored value. A value the map does not know falls to a raw-value
branch that shows it as it is in every language.

That branch is what makes the gap quiet. A status added to the backend enum
renders in English exactly as a mapped one would, so no English test and no
screenshot notices that under ``fr-CA`` it alone stays untranslated. In the other
direction, a UI message no backend value produces is dead: translators pay for it
and nothing ever shows it.

So this test derives both sets from source and fails in **both** directions. Read
from **source** rather than imported, on the Python side too: that keeps the gate
runnable without the package installed, and the assertion is about what is
committed rather than about whatever an editable install resolves to.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The UI's message map, one entry per stored value.
UI_MODULE = REPO_ROOT / "src/ui/src/components/common/object-status-renderer.tsx"

#: The backend enum the stored values come from.
BACKEND_MODULE = REPO_ROOT / "lib/idp_common_pkg/idp_common/models.py"
BACKEND_ENUM = "Status"


def _backend_values() -> set[str]:
    """The string value of every member of ``class Status(Enum)``.

    Read with ``ast`` rather than a regex because one member is parenthesised
    (``REJECTED = ("REJECTED"  # ...)``) and several carry comments that run on
    to the next lines; the parser sees through both.
    """
    assert BACKEND_MODULE.is_file(), (
        f"{BACKEND_MODULE.relative_to(REPO_ROOT)} has moved; update BACKEND_MODULE"
    )
    tree = ast.parse(
        BACKEND_MODULE.read_text(encoding="utf-8"), filename=str(BACKEND_MODULE)
    )
    enum = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == BACKEND_ENUM
        ),
        None,
    )
    assert enum is not None, (
        f"{BACKEND_MODULE.relative_to(REPO_ROOT)} no longer declares "
        f"`class {BACKEND_ENUM}`. If the status vocabulary moved, point this gate "
        "at its new home rather than deleting it."
    )
    values: set[str] = set()
    for node in enum.body:
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        member = ast.unparse(node.targets[0])
        assert isinstance(value, ast.Constant) and isinstance(value.value, str), (
            f"{BACKEND_MODULE.relative_to(REPO_ROOT)}:{node.lineno}: the value of "
            f"`{BACKEND_ENUM}.{member}` is not a string literal. Teach this reader to "
            "read that form rather than skip the member: a skipped member is a status "
            "this gate never checks against the UI."
        )
        values.add(value.value)
    return values


def _ui_values() -> set[str]:
    """The top-level keys of ``const statusMessages = defineMessages<...>({ ... })``."""
    source = UI_MODULE.read_text(encoding="utf-8")
    match = re.search(
        r"^const statusMessages = defineMessages<[^\n]*>\(\{\n(.*?)^\}\);",
        source,
        re.DOTALL | re.MULTILINE,
    )
    assert match, (
        f"{UI_MODULE.relative_to(REPO_ROOT)} no longer declares "
        "`const statusMessages = defineMessages<...>({` on one line. That map is "
        "what gives each stored status a translatable message; if it was "
        "restructured, update this gate to read the new shape rather than "
        "removing it."
    )
    # One `  KEY: {` per entry, at the map's own indentation; the nested
    # `id`/`defaultMessage`/`description` lines sit deeper and are lower case.
    return set(re.findall(r"^\s{2}([A-Z_]+): \{$", match.group(1), re.MULTILINE))


@pytest.mark.unit
def test_every_backend_status_has_a_ui_message():
    missing = sorted(_backend_values() - _ui_values())
    assert missing == [], (
        "the backend writes these statuses, but the UI's statusMessages map has no "
        "message for them, so the Status column shows them untranslated in every "
        "language: " + json.dumps(missing) + "\nAdd them to "
        f"{UI_MODULE.relative_to(REPO_ROOT)} (English = the stored value)."
    )


@pytest.mark.unit
def test_the_ui_has_no_message_for_a_status_the_backend_does_not_declare():
    """A dead entry is translated and never shown."""
    unknown = sorted(_ui_values() - _backend_values())
    assert unknown == [], (
        "the UI's statusMessages map has messages for statuses the backend "
        f"`{BACKEND_ENUM}` enum does not declare: " + json.dumps(unknown) + "\nEither "
        "the status was renamed on the backend, or the entry is dead and should be "
        f"removed from {UI_MODULE.relative_to(REPO_ROOT)}."
    )


@pytest.mark.unit
def test_both_sides_are_non_empty():
    """Non-vacuity: a reader that silently matched nothing would pass both above."""
    assert len(_backend_values()) >= 20
    assert len(_ui_values()) >= 20
