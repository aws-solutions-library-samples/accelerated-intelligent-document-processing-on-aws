# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The decision-model question types agree between the library and the UI.

``QUESTION_TYPES`` in ``idp_common.decision_model.wire`` is what the client sends;
``DECISION_QUESTION_TYPES`` in ``src/ui/src/constants/schemaConstants.ts`` is what
the editor offers. A type in one and not the other is either a question the UI
cannot author or a control that writes a type the backend rejects (GitHub #1656).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from idp_common.decision_model.wire import QUESTION_TYPES

REPO = Path(__file__).resolve().parents[2]
UI_CONSTANTS = REPO / "src/ui/src/constants/schemaConstants.ts"


def _ui_array(name: str) -> tuple[str, ...]:
    text = UI_CONSTANTS.read_text()
    match = re.search(rf"export const {name} = \[([^\]]*)\]", text)
    assert match, f"{name} not found in {UI_CONSTANTS}"
    return tuple(re.findall(r"'([^']+)'", match.group(1)))


def _ui_option_values() -> tuple[str, ...]:
    text = UI_CONSTANTS.read_text()
    block = text.split("export const DECISION_QUESTION_TYPE_OPTIONS", 1)[1]
    block = block.split("];", 1)[0]
    return tuple(re.findall(r"value: '([^']+)'", block))


@pytest.mark.unit
def test_the_ui_offers_exactly_the_library_types_in_order():
    assert _ui_array("DECISION_QUESTION_TYPES") == QUESTION_TYPES


@pytest.mark.unit
def test_every_type_has_one_ui_option():
    assert _ui_option_values() == QUESTION_TYPES
