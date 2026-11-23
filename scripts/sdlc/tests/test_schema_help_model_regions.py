# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Editor help text that names a code fallback model names it for every Region.

The configuration editor shows each field's ``description`` from the ``Schema:``
block in the templates. The deploy-time step in ``src/lambda/update_configuration``
filters that schema's model picklists by Region but leaves its prose alone, so a
description that names a ``us.`` fallback id reads the same on an EU stack, where
the code substitutes the ``eu.`` id instead (``idp_common.regional_models``).

So a description naming a US id that the EU row of
``REGIONAL_DEFAULT_MODELS`` translates must also name the translation. The ids and
their translations are read from that table, not restated here.
"""

from __future__ import annotations

import importlib
from typing import Any, Iterator

import pytest

pytest.importorskip("yaml")
_regional = pytest.importorskip("idp_common.regional_models")

#: US fallback id -> the id an EU stack substitutes for it.
_EU_ROW: dict[str, str] = _regional.REGIONAL_DEFAULT_MODELS["eu-"]


def _schemas():
    """The sibling sweep's template loader and ``Schema:`` discovery, reused."""
    return importlib.import_module("test_config_schema_order")._schemas()


def _descriptions(node: Any, path: str = "") -> Iterator[tuple[str, str]]:  # noqa: ANN401
    if isinstance(node, dict):
        for key, sub in node.items():
            child = f"{path}.{key}" if path else str(key)
            if key == "description" and isinstance(sub, str):
                yield child, sub
            else:
                yield from _descriptions(sub, child)
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            yield from _descriptions(item, f"{path}[{idx}]")


def _naming_a_fallback() -> list[tuple[str, str, str]]:
    found = []
    for label, schema in _schemas():
        for path, text in _descriptions(schema):
            for us_id in _EU_ROW:
                if us_id in text:
                    found.append((f"{label}::{path}", us_id, text))
    return found


def test_the_sweep_reads_a_description_that_names_a_fallback():
    """Guard the guard: the classification model help names its fallback today."""
    hits = _naming_a_fallback()
    assert any(us_id == _regional.CLASSIFICATION_MODEL_ID for _, us_id, _ in hits), (
        "no schema description names the classification fallback; the sweep reads nothing"
    )


def test_a_description_naming_a_us_fallback_also_names_the_eu_one():
    missing = [
        f"{where}: names {us_id} but not {_EU_ROW[us_id]}"
        for where, us_id, text in _naming_a_fallback()
        if _EU_ROW[us_id] not in text
    ]
    assert not missing, (
        "an EU stack substitutes the eu. id for these fallbacks, so help text naming "
        "only the us. id is wrong there:\n" + "\n".join(missing)
    )
