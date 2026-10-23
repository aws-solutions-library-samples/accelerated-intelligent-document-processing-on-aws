# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The UI's list of pre-0.9 classification keys matches the migration's.

The editor tells a user importing an old file which keys will be converted
(`src/ui/src/utils/classification-config.ts`). A key the migration converts but
the UI does not list would be converted with no notice.
"""

import json
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
