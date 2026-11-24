# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every prompt-cache state the backend records must be one the UI types and words, and only those.

``idp_common.bedrock.prompt_cache`` lists its states in ``CACHE_STATES``, and
``cache_state`` classifies one metering aggregate into one of them. A section's
result records its state, and the edit page's Processing Report shows it as a
prompt-cache note. The UI types the state as ``PromptCacheState`` in
``src/ui/src/components/common/promptCacheModel.ts`` and words each note's headline
through ``stateHeadlineMessages``, one react-intl message per state, keyed by the state
as written. A state the UI does not know reads as the ``no-cache-data`` note, "no cache
usage reported by this model or backend".

That fallback is what makes a gap quiet. A state the backend starts recording shows,
in every language, as a note saying the model reported no cache usage, and no English
test notices. In the other direction, a UI state the backend never records is dead:
translators pay for its note and nothing shows it.

So this test reads all three from **source**, not by importing (the gate runs without
the package installed, and it checks what is committed), and fails in **both**
directions.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The UI module with the ``PromptCacheState`` union and the ``stateHeadlineMessages`` map.
UI_MODULE = REPO_ROOT / "src/ui/src/components/common/promptCacheModel.ts"

#: The backend module whose module-level ``CACHE_STATES = (...)`` lists the states.
BACKEND_MODULE = REPO_ROOT / "lib/idp_common_pkg/idp_common/bedrock/prompt_cache.py"
BACKEND_NAME = "CACHE_STATES"


def _relative(path: Path) -> Path:
    return path.relative_to(REPO_ROOT)


def _backend_states() -> set[str]:
    """The strings of the module-level ``CACHE_STATES = ("caching", ...)``."""
    assert BACKEND_MODULE.is_file(), (
        f"{_relative(BACKEND_MODULE)} has moved; update BACKEND_MODULE in this test"
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
    assert isinstance(value, ast.Tuple), (
        f"{_relative(BACKEND_MODULE)} no longer declares a module-level "
        f"`{BACKEND_NAME} = (...)` tuple. If the state vocabulary moved, point this "
        "test at its new home rather than deleting it."
    )
    states: set[str] = set()
    for member in value.elts:
        assert isinstance(member, ast.Constant) and isinstance(member.value, str), (
            f"{_relative(BACKEND_MODULE)}:{member.lineno}: `{ast.unparse(member)}` in "
            f"`{BACKEND_NAME}` is not a string literal. Teach this reader that form "
            "rather than skip it: a skipped state is one this test never checks."
        )
        states.add(member.value)
    return states


def _ui_source() -> str:
    assert UI_MODULE.is_file(), (
        f"{_relative(UI_MODULE)} has moved; update UI_MODULE in this test"
    )
    return UI_MODULE.read_text(encoding="utf-8")


def _ui_union() -> set[str]:
    """The members of ``export type PromptCacheState = 'caching' | ...;``."""
    match = re.search(
        r"^export type PromptCacheState = ([^;\n]+);$", _ui_source(), re.MULTILINE
    )
    assert match, (
        f"{_relative(UI_MODULE)} no longer declares `export type PromptCacheState = "
        "'...' | '...';` on one line. If the union was restructured, update this "
        "test to read the new shape rather than removing it."
    )
    members = [member.strip() for member in match.group(1).split("|")]
    assert all(re.fullmatch(r"'[^']+'", member) for member in members), (
        f"`PromptCacheState` in {_relative(UI_MODULE)} is no longer a union of string "
        f"literals: {match.group(1)}. Teach this reader the new form rather than "
        "skipping it."
    )
    return {member[1:-1] for member in members}


def _ui_headlines() -> set[str]:
    """The top-level keys of ``const stateHeadlineMessages = defineMessages<...>({ ... })``."""
    match = re.search(
        r"^const stateHeadlineMessages = defineMessages<[^\n]*>\(\{\n(.*?)^\}\);",
        _ui_source(),
        re.DOTALL | re.MULTILINE,
    )
    assert match, (
        f"{_relative(UI_MODULE)} no longer declares `const stateHeadlineMessages = "
        "defineMessages<...>({` on one line. That map is what gives each state's note "
        "a translatable headline; if it was restructured, update this test to read "
        "the new shape rather than removing it."
    )
    # One `  state: {` or `  'state-name': {` per entry, at the map's own indentation;
    # the nested `id`, `defaultMessage` and `description` lines sit deeper.
    return {
        quoted or bare
        for quoted, bare in re.findall(
            r"^ {2}(?:'([a-z][a-z-]*)'|([a-z][a-zA-Z]*)): \{$",
            match.group(1),
            re.MULTILINE,
        )
    }


@pytest.mark.unit
def test_every_backend_state_is_a_ui_state():
    missing = sorted(_backend_states() - _ui_union())
    assert missing == [], (
        f"the backend's {BACKEND_NAME} lists these prompt-cache states, but the UI's "
        f"PromptCacheState union does not: {json.dumps(missing)}\nAdd each to the "
        f"union in {_relative(UI_MODULE)}, and give it a headline in "
        "stateHeadlineMessages, keyed by the state as written."
    )


@pytest.mark.unit
def test_the_ui_types_no_state_the_backend_does_not_record():
    unknown = sorted(_ui_union() - _backend_states())
    assert unknown == [], (
        "the UI's PromptCacheState union has states the backend's "
        f"{BACKEND_NAME} does not list: {json.dumps(unknown)}\nEither the state was "
        "renamed on the backend, or it is dead: remove it from the union and from "
        f"stateHeadlineMessages in {_relative(UI_MODULE)}."
    )


@pytest.mark.unit
def test_every_backend_state_has_a_headline():
    missing = sorted(_backend_states() - _ui_headlines())
    assert missing == [], (
        f"the backend's {BACKEND_NAME} lists these prompt-cache states, but the UI's "
        "stateHeadlineMessages map has no headline for them, so their notes read as "
        f"'no cache usage reported' in every language: {json.dumps(missing)}\nAdd an "
        f"entry for each to stateHeadlineMessages in {_relative(UI_MODULE)}, keyed by "
        "the state as written, and word its note in describePromptCache."
    )


@pytest.mark.unit
def test_the_ui_has_no_headline_for_a_state_the_backend_does_not_record():
    """A dead entry is translated and never shown."""
    unknown = sorted(_ui_headlines() - _backend_states())
    assert unknown == [], (
        "the UI's stateHeadlineMessages map has headlines for states the backend's "
        f"{BACKEND_NAME} does not list: {json.dumps(unknown)}\nEither the state was "
        "renamed on the backend, or the entry is dead and should be removed from "
        f"{_relative(UI_MODULE)}."
    )


@pytest.mark.unit
def test_all_three_are_read_at_full_strength():
    """Non-vacuity: readers that silently matched nothing would pass every check above."""
    floor = (
        "fewer than the 6 prompt-cache states this test was written against; fix the "
        "reader, or lower the floor if the vocabulary really shrank"
    )
    assert len(_backend_states()) >= 6, f"the backend reader found {floor}"
    assert len(_ui_union()) >= 6, f"the PromptCacheState reader found {floor}"
    assert len(_ui_headlines()) >= 6, f"the stateHeadlineMessages reader found {floor}"
