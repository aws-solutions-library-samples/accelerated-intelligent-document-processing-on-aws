# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every Inbox ``Severity`` the backend defines must have a label in the UI.

The Inbox table, its severity filter, the bell and the message dialog show a
message's severity through ``severityMessages`` in
``src/ui/src/components/inbox/severity.ts``, one react-intl message per value. A
value the map does not know is shown capitalised, as stored, in every language.

That fallback is what makes the gap quiet. A severity added to the backend's
``Severity`` literal reads in English exactly as a mapped one does, so no English
test notices that under ``fr-CA`` it alone stays untranslated, and that the
table's severity filter does not offer it. In the other direction, a UI message
no backend value produces is dead: translators pay for it and nothing shows it.

So this test derives both sets from source and fails in **both** directions. Both
sides are read from **source**, not imported: the gate runs without the package
installed, and it checks what is committed.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The UI's label map, one entry per severity.
UI_MODULE = REPO_ROOT / "src/ui/src/components/inbox/severity.ts"

#: The backend module whose ``Severity = Literal[...]`` lists the stored values.
BACKEND_MODULE = REPO_ROOT / "lib/idp_common_pkg/idp_common/notifications/models.py"
BACKEND_NAME = "Severity"


def _backend_values() -> set[str]:
    """The strings of the module-level ``Severity = Literal["info", ...]``."""
    assert BACKEND_MODULE.is_file(), (
        f"{BACKEND_MODULE.relative_to(REPO_ROOT)} has moved; update BACKEND_MODULE"
    )
    tree = ast.parse(
        BACKEND_MODULE.read_text(encoding="utf-8"), filename=str(BACKEND_MODULE)
    )
    value = next(
        (
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == BACKEND_NAME
                for target in node.targets
            )
        ),
        None,
    )
    assert value is not None, (
        f"{BACKEND_MODULE.relative_to(REPO_ROOT)} no longer declares a module-level "
        f"`{BACKEND_NAME} = Literal[...]`. If the severity vocabulary moved, point "
        "this gate at its new home rather than deleting it."
    )
    assert isinstance(value, ast.Subscript) and ast.unparse(value.value) in (
        "Literal",
        "typing.Literal",
    ), (
        f"`{BACKEND_NAME}` in {BACKEND_MODULE.relative_to(REPO_ROOT)} is no longer a "
        "`Literal[...]`. Teach this reader the new form rather than skipping it."
    )
    members = value.slice.elts if isinstance(value.slice, ast.Tuple) else [value.slice]
    values: set[str] = set()
    for member in members:
        assert isinstance(member, ast.Constant) and isinstance(member.value, str), (
            f"{BACKEND_MODULE.relative_to(REPO_ROOT)}:{member.lineno}: "
            f"`{ast.unparse(member)}` in `{BACKEND_NAME}` is not a string literal. "
            "Teach this reader that form rather than skip it: a skipped value is a "
            "severity this gate never checks against the UI."
        )
        values.add(member.value)
    return values


def _ui_values() -> set[str]:
    """The top-level keys of ``const severityMessages = defineMessages<...>({ ... })``."""
    source = UI_MODULE.read_text(encoding="utf-8")
    match = re.search(
        r"^const severityMessages = defineMessages<[^\n]*>\(\{\n(.*?)^\}\);",
        source,
        re.DOTALL | re.MULTILINE,
    )
    assert match, (
        f"{UI_MODULE.relative_to(REPO_ROOT)} no longer declares "
        "`const severityMessages = defineMessages<...>({` on one line. That map is "
        "what gives each severity a translatable label; if it was restructured, "
        "update this gate to read the new shape rather than removing it."
    )
    # One `  key: {` per entry, at the map's own indentation; the nested `id`,
    # `defaultMessage` and `description` lines sit deeper.
    return set(re.findall(r"^ {2}([a-z]\w*): \{", match.group(1), re.MULTILINE))


@pytest.mark.unit
def test_every_backend_severity_has_a_ui_label():
    missing = sorted(_backend_values() - _ui_values())
    assert missing == [], (
        "the backend's Inbox `Severity` has these values, but the UI's "
        "severityMessages map has no label for them, so they show untranslated in "
        "every language and the Inbox severity filter does not offer them: "
        + json.dumps(missing)
        + f"\nAdd them to {UI_MODULE.relative_to(REPO_ROOT)}, keyed by the stored "
        "value, with the English label as the defaultMessage."
    )


@pytest.mark.unit
def test_the_ui_has_no_label_for_a_severity_the_backend_does_not_define():
    """A dead entry is translated and never shown."""
    unknown = sorted(_ui_values() - _backend_values())
    assert unknown == [], (
        "the UI's severityMessages map has labels for severities the backend "
        f"`{BACKEND_NAME}` literal does not list: " + json.dumps(unknown) + "\nEither "
        "the severity was renamed on the backend, or the entry is dead and should be "
        f"removed from {UI_MODULE.relative_to(REPO_ROOT)}."
    )


@pytest.mark.unit
def test_both_sides_are_non_empty():
    """Non-vacuity: a reader that silently matched nothing would pass both above."""
    floor = (
        "fewer than the 4 severities this gate was written against; fix the reader, "
        "or lower the floor if the vocabulary really shrank"
    )
    assert len(_backend_values()) >= 4, f"the backend reader found {floor}"
    assert len(_ui_values()) >= 4, f"the UI reader found {floor}"
