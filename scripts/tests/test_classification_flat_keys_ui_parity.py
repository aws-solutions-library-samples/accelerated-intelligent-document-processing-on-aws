# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The UI's list of pre-0.9 classification keys matches the migration's.

The editor tells a user importing an old file which keys will be converted
(`src/ui/src/utils/classification-config.ts`). A key the migration converts but
the UI does not list would be converted with no notice.
"""

import json
import math
import re
from pathlib import Path

import pytest

from idp_common.config.migrations.v08_to_v09 import FLAT_KEYS

REPO = Path(__file__).resolve().parents[2]
UI_FILE = REPO / "src/ui/src/utils/classification-config.ts"


@pytest.mark.unit
def test_the_ui_lists_exactly_the_keys_the_migration_converts():
    source = UI_FILE.read_text()
    start = source.index("export const FLAT_CLASSIFICATION_KEYS")
    end = source.index("] as const;", start)
    ui_keys = set(re.findall(r"'(\w+)'", source[start:end]))
    # classificationMethod is converted by the hop before (v07_to_v08).
    assert ui_keys == set(FLAT_KEYS) | {"classificationMethod"}


def _ts_const(source: str, name: str) -> set[str]:
    """The string members of an exported `const <name> = [...] as const;` array."""
    start = source.index(f"export const {name}")
    end = source.index("] as const;", start)
    return set(re.findall(r"'([\w.-]+)'", source[start:end]))


@pytest.mark.unit
def test_the_ui_knows_exactly_the_section_splitting_values_the_backend_accepts():
    """The import dialog tells the user what will happen to their file.

    ⚠️ **A value the backend accepts and the UI does not** makes the dialog warn
    that a correct file will be refused. **A value the UI accepts and the backend
    does not** is worse: the dialog promises the page-limit conversion for a value
    that is in fact rejected on save, which is the state this gate was added for —
    the TypeScript predicate's docstring claimed to mirror `v08_to_v09.py` while the
    Python side had changed under it, and nothing failed.
    """
    from idp_common.config.models import (
        DEFAULT_SECTION_SPLITTING,
        SECTION_SPLITTING_VALUES,
    )

    source = UI_FILE.read_text()
    assert _ts_const(source, "SECTION_SPLITTING_VALUES") == set(
        SECTION_SPLITTING_VALUES
    )
    assert f"DEFAULT_SECTION_SPLITTING = '{DEFAULT_SECTION_SPLITTING}'" in source, (
        "the UI's default section-splitting value is not the one the config layer "
        f"substitutes for an unset one ({DEFAULT_SECTION_SPLITTING})"
    )


@pytest.mark.unit
def test_this_gate_reads_the_arrays_rather_than_passing_on_an_empty_one():
    """Guard the guard: both parity checks compare sets, so two empty sets agree."""
    source = UI_FILE.read_text()
    for name in ("FLAT_CLASSIFICATION_KEYS", "SECTION_SPLITTING_VALUES"):
        assert _ts_const(source, name), (
            f"read no members from the UI's {name}, so the parity assertion over it "
            "would pass by comparing nothing"
        )


#: The shared table, authored beside the TypeScript it is also read by. See its own
#: `$comment` for why the cases live in one file rather than in two suites.
_PAGE_LIMIT_FIXTURES = REPO / "src/ui/src/utils/__tests__/page-limit.fixtures.json"


def _page_limit_cases() -> list[tuple]:
    body = json.loads(_PAGE_LIMIT_FIXTURES.read_text())
    cases = [(case["json"], case["limit"], case["why"]) for case in body["cases"]]
    assert len(cases) >= 10, (
        f"read only {len(cases)} cases from {_PAGE_LIMIT_FIXTURES.name}, which is "
        "below the table it was written with — this check would pass by exercising "
        "almost nothing"
    )
    return cases


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected", "why"), _page_limit_cases(), ids=lambda v: repr(v)[:24]
)
def test_the_page_limit_predicate_answers_the_shared_table(value, expected, why):
    """⚠️ The two value lists agreeing is not the two predicates agreeing.

    The assertions above compare `FLAT_KEYS` and `SECTION_SPLITTING_VALUES` by
    reading the TypeScript with a regex, which cannot see a semantic divergence —
    and one had already happened. The TypeScript stringified its input before
    testing it, so an unquoted `2.5` answered false while `_is_page_limit`
    truncates it to a two-page limit and the migration performs the conversion: the
    import dialog said nothing and boundary detection went off. So the cases are
    authored once and run on both sides, this being the Python half.
    """
    from idp_common.config.migrations.v08_to_v09 import _is_page_limit

    assert _is_page_limit(value) is expected, why


#: The two spellings JSON cannot carry, so they cannot live in the shared table —
#: `Infinity` and `NaN` are not JSON. They belong here rather than in the
#: `FileShapeConfig` suite because what they threaten is the *migration* predicate:
#: it truncates, and `int(inf)` raises `OverflowError` while `int(nan)` raises
#: `ValueError`. `conversion_notices` calls it from above the `try` that wraps the
#: merge, so a raise there aborts `idp-cli config-validate` with a traceback instead
#: of reporting the value.
_NON_FINITE_PAGE_LIMITS = (float("inf"), float("-inf"), float("nan"))


@pytest.mark.unit
@pytest.mark.parametrize("value", _NON_FINITE_PAGE_LIMITS, ids=repr)
def test_a_non_finite_page_limit_is_answered_rather_than_raised(value):
    from idp_common.config.migrations.v08_to_v09 import (
        _is_page_limit,
        conversion_notices,
    )

    assert _is_page_limit(value) is False
    # And the caller that sits outside a try block still returns.
    notices = conversion_notices(
        {"classification": {"model": "m", "maxPagesForClassification": value}}
    )
    assert notices, "the pre-0.9 layout notice should still be produced"


@pytest.mark.unit
@pytest.mark.parametrize("value", _NON_FINITE_PAGE_LIMITS, ids=repr)
def test_a_non_finite_page_limit_normalises_in_the_model_too(value):
    """The two predicates must agree about it, which is the table's whole point —
    they just cannot agree *in* the table, because JSON has no spelling for it."""
    from idp_common.config.models import FileShapeConfig

    assert (
        FileShapeConfig(maxPagesForClassification=value).maxPagesForClassification
        == "ALL"
    )


@pytest.mark.unit
def test_no_float_spelling_is_unaccounted_for_by_both_case_sets():
    """Closure over the inputs these two predicates are asserted about.

    The cases live in two places for one reason — JSON has no spelling for
    ``Infinity`` or ``NaN`` — and a split like that is how a value ends up in
    neither set. So: ``math.isfinite`` partitions ``float`` on exactly ``inf``,
    ``-inf`` and ``nan``, which is why :data:`_NON_FINITE_PAGE_LIMITS` is three long
    and not an open list; every member of it really is non-finite, so none of them
    belongs in the table; and nothing in the table is non-finite, so none of those
    belongs here. Between them the two sets leave no float spelling uncovered.
    """
    assert all(not math.isfinite(v) for v in _NON_FINITE_PAGE_LIMITS), (
        "a finite value in this list belongs in the shared table instead, where both "
        "languages can read it"
    )
    assert len(_NON_FINITE_PAGE_LIMITS) == 3, (
        "math.isfinite partitions float on exactly inf, -inf and nan, so a fourth "
        "entry here is either a duplicate or not a float spelling at all"
    )
    table = [
        case["json"] for case in json.loads(_PAGE_LIMIT_FIXTURES.read_text())["cases"]
    ]
    stranded = [v for v in table if isinstance(v, float) and not math.isfinite(v)]
    assert not stranded, (
        f"these non-finite values sit in the shared table, which cannot express them "
        f"as JSON, so the TypeScript half never sees them: {stranded}"
    )
