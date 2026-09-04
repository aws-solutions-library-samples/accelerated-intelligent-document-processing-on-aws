# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Schema utilities for IDP common library.

**Re-exports are lazy (PEP 562).** ``pydantic_generator`` imports
``datamodel_code_generator``, an extraction-only dependency that is deliberately
absent from most Lambda layers. Importing it eagerly here made
``from idp_common.schema.multi_instance import …`` — a pure, dependency-free
module — drag the whole code generator in with it, so any Lambda without the
extraction extra died at import time with ``No module named
'datamodel_code_generator'``. That is exactly how it was found: the
``UpdateDefaultConfig`` custom resource failed a stack update, because
``config/models.py`` validates class schemas and now needs the multi-instance
helper.

The lazy re-export keeps this package's public API identical for existing
callers while leaving its dependency-free submodules importable on their own.
``tests/unit/schema/test_lazy_package_imports.py`` pins the behaviour.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from idp_common.schema.pydantic_generator import (
        CircularReferenceError,
        PydanticModelGenerationError,
        clean_schema_for_generation,
        create_pydantic_model_from_json_schema,
        validate_json_schema_for_pydantic,
    )

_LAZY_EXPORTS = {
    "CircularReferenceError": "idp_common.schema.pydantic_generator",
    "PydanticModelGenerationError": "idp_common.schema.pydantic_generator",
    "clean_schema_for_generation": "idp_common.schema.pydantic_generator",
    "create_pydantic_model_from_json_schema": "idp_common.schema.pydantic_generator",
    "validate_json_schema_for_pydantic": "idp_common.schema.pydantic_generator",
}

__all__ = [
    "CircularReferenceError",
    "PydanticModelGenerationError",
    "clean_schema_for_generation",
    "create_pydantic_model_from_json_schema",
    "validate_json_schema_for_pydantic",
]


def __getattr__(name: str) -> Any:
    """Import a re-exported symbol on first access, not at package import."""
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value  # cache, so repeat access is a plain global lookup
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
