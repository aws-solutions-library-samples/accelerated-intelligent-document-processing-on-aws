# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Z3-based business rule validation engine."""

from .data_extractor import DataExtractor
from .exceptions import (
    ExtractionError,
    TranslationError,
    ValidationError,
    ValidationSystemError,
)
from .models import Parameter, PathMapping, RuleJSON, RuleWithValues, ValidationResult
from .rule_translator import RuleTranslator


def __getattr__(name):
    """Lazy import for z3-dependent modules (Z3Validator, ValidationSystem).

    These require the z3 Python package which is only available in Lambda
    environments with the z3 layer attached. Importing them eagerly would
    break Lambdas that only need RuleTranslator (e.g., configuration_resolver).
    """
    if name == "Z3Validator":
        from .z3_validator import Z3Validator

        return Z3Validator
    if name == "ValidationSystem":
        from .validation_system import ValidationSystem

        return ValidationSystem
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "Parameter",
    "PathMapping",
    "RuleJSON",
    "RuleWithValues",
    "ValidationResult",
    "ValidationSystemError",
    "TranslationError",
    "ExtractionError",
    "ValidationError",
    "RuleTranslator",
    "DataExtractor",
    "Z3Validator",
    "ValidationSystem",
]
