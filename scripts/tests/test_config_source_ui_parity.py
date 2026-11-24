# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every backend configuration source must have a label in the UI's map, and only those.

A document records where its configuration profile came from (``prefix-mapping``,
``metadata``, ``document-pin``, ...), and Document Details shows that value as a
short sentence such as "Assigned by prefix mapping". Each label is a react-intl
message in ``src/ui/src/components/document-panel/DocumentPanel.tsx``, keyed by the
stored value. A value the map does not know is shown as it is.

That fallback is what makes a gap quiet. A source the backend starts writing
renders as its raw value, in English, in every language, and no test or screenshot
notices that it alone has no label and is never translated. In the other
direction, a UI label no backend value produces is dead: translators pay for it
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

#: The UI's label map, one entry per stored value.
UI_MODULE = REPO_ROOT / "src/ui/src/components/document-panel/DocumentPanel.tsx"

#: The backend module that declares the stored values as ``SOURCE_*`` constants.
BACKEND_MODULE = REPO_ROOT / "lib/idp_common_pkg/idp_common/config/prefix_mappings.py"
BACKEND_PREFIX = "SOURCE_"


def _backend_values() -> set[str]:
    """The string value of every module-level ``SOURCE_* = "..."`` assignment."""
    assert BACKEND_MODULE.is_file(), (
        f"{BACKEND_MODULE.relative_to(REPO_ROOT)} has moved; update BACKEND_MODULE"
    )
    tree = ast.parse(
        BACKEND_MODULE.read_text(encoding="utf-8"), filename=str(BACKEND_MODULE)
    )
    values: set[str] = set()
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id.startswith(BACKEND_PREFIX)
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            values.add(node.value.value)
    return values


def _ui_values() -> set[str]:
    """The top-level keys of ``const configSourceMessages = defineMessages<...>({ ... })``."""
    source = UI_MODULE.read_text(encoding="utf-8")
    match = re.search(
        r"^const configSourceMessages = defineMessages<[^\n]*>\(\{\n(.*?)^\}\);",
        source,
        re.DOTALL | re.MULTILINE,
    )
    assert match, (
        f"{UI_MODULE.relative_to(REPO_ROOT)} no longer declares "
        "`const configSourceMessages = defineMessages<...>({` on one line. That map "
        "is what gives each configuration source a translatable label; if it was "
        "restructured, update this gate to read the new shape rather than removing it."
    )
    # One `  key: {` per entry at the map's own indentation, the key quoted when it
    # has a hyphen; the nested `id`/`defaultMessage`/`description` lines sit deeper.
    return set(
        re.findall(r"^\s{2}'?([a-z][a-z-]*)'?: \{$", match.group(1), re.MULTILINE)
    )


@pytest.mark.unit
def test_every_backend_config_source_has_a_ui_label():
    missing = sorted(_backend_values() - _ui_values())
    assert missing == [], (
        "the backend records these configuration sources, but the UI's "
        "configSourceMessages map has no label for them, so Document Details shows "
        "them raw and untranslated in every language: " + json.dumps(missing) + "\n"
        f"Add them to {UI_MODULE.relative_to(REPO_ROOT)}."
    )


@pytest.mark.unit
def test_the_ui_has_no_label_for_a_config_source_the_backend_does_not_declare():
    """A dead entry is translated and never shown."""
    unknown = sorted(_ui_values() - _backend_values())
    assert unknown == [], (
        "the UI's configSourceMessages map has labels for configuration sources "
        f"that {BACKEND_MODULE.relative_to(REPO_ROOT)} does not declare as "
        f"`{BACKEND_PREFIX}*` constants: " + json.dumps(unknown) + "\nEither the "
        "source was renamed on the backend, or the entry is dead and should be "
        f"removed from {UI_MODULE.relative_to(REPO_ROOT)}."
    )


@pytest.mark.unit
def test_both_sides_are_non_empty():
    """Non-vacuity: a reader that silently matched nothing would pass both above."""
    assert len(_backend_values()) >= 5
    assert len(_ui_values()) >= 5
