# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Coercion of an extracted reading to a declared numeric parameter type.

Rule validation reaches the solver by three routes. Path-based extraction
(`DataExtractor.extract_values`) reads values out of a structured document at
declared paths; LLM extraction (`RuleTranslator.extract_values_with_llm`) asks a
model for them, and is both the default for a rule with no `path_mappings` and
the fallback whenever path-based extraction fails; and the orchestrator's
production fact-extraction call
(`RuleValidationOrchestratorService._extract_z3_values_from_facts`) hands parsed
JSON straight to `Z3Validator.validate`. All three end up bound to a Z3 variable
by `Z3Validator._bind_values`.

Every route goes through this module for the numeric types, so that "what counts
as a valid `Int`" has exactly one answer rather than one per route. `Bool` and
`String` keep their per-route conversions, which already agree on everything
except an integer read for a `Bool`.

The contract for `Int` is **exactness**: a reading is accepted only if it
denotes a whole number, whatever type or spelling it arrives in. `30`, `30.0`,
`Decimal("30.0")` and `"30"` all bind to 30; `30.9`, `Decimal("30.9")` and
`"30.9"` are refused. Truncating toward zero, as `int()` does, moves the reading
by up to a whole unit, which is enough to flip the verdict of any rule with an
integer threshold — and to flip it in either direction, depending on the
comparator, so no amount of rounding makes it sound. Refusing is what a caller
can act on: the rule reports that it could not be evaluated instead of
confidently reporting the opposite of the truth. See GitHub issue #1057.

A float that is merely *close* to a whole number, such as ``29.999999999999996``,
is likewise refused rather than nudged. Nothing in a reading says which whole
number the model meant, and guessing is the behaviour this module exists to
remove.

`Real` readings are **not** rounded to a double on the way to the solver. There are
two entry points for that reason: `exact_numeric_reading` returns the exact
rational and is what the binding site and the constraint parser use, and
`coerce_numeric_reading` returns the `float` that goes into `extracted_values`,
which is serialised to JSON in the result. They accept and refuse exactly the same
readings, because the second is written in terms of the first.

The code lives in `idp_common.field_paths`, a light module that review routing can
import without the rule validation extra. This module re-exports it.
"""

from idp_common.field_paths import (
    NUMERIC_TYPES,
    coerce_numeric_reading,
    exact_numeric_reading,
)

__all__ = ["NUMERIC_TYPES", "coerce_numeric_reading", "exact_numeric_reading"]
