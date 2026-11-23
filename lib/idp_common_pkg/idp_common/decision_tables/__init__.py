# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Decision tables: a subset of DMN decision tables, evaluated in pure Python.

``DecisionTable`` holds inputs, outputs and ordered rules; ``evaluate`` runs it
against a context through the engine a table names. The unary tests in a rule's
cells are the S-FEEL subset in ``sfeel``; ``dmn`` reads and writes DMN XML.

Names are loaded on first use, so importing this package costs nothing, and
``dmn`` (the only module with a third-party dependency, ``defusedxml``) is loaded
only by code that reads or writes XML. See ``README.md``.
"""

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import ENGINES as ENGINES
    from .engine import evaluate as evaluate
    from .models import DecisionTable as DecisionTable

_LAZY = {
    "DecisionTable": "models",
    "evaluate": "engine",
    "ENGINES": "engine",
}


def __getattr__(name: str):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{module}"), name)


__all__ = ["DecisionTable", "ENGINES", "evaluate"]
