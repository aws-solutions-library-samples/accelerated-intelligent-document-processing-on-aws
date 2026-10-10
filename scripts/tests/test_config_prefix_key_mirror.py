# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Python authority and the TypeScript mirror agree on key canonicalization.

`canonical_key` in `idp_common/config/prefix_mappings.py` decides which config
prefix mapping governs an S3 key. `canonicalKey` in
`src/ui/src/utils/config-prefix-key.ts` is a hand-maintained copy of it, so the
upload panel can preview the key the upload will actually land on rather than the
one the user typed.

Nothing structural stops those two drifting, and a *partial* mirror is worse than
no mirror: one that strips leading and trailing slashes but not interior repeats
previews a typed `acme//invoices` as unmapped while the upload lands on
`acme/invoices/<file>`, which is governed — so under a `reject` mapping the user
gets no warning and a 400 at ingest. This is the same defect class
`test_resolver_log_sanitizer.py` and `test_s3_targets_vendored.py` exist for one
layer down, and `test_prefix_mapping_call_sites.py` for one layer up.

The pin is a **shared fixture table** rather than a reimplementation: this module
asserts the Python side over it, the vitest suite beside the table asserts the
TypeScript side over the same file, so a change to either implementation that is
not matched in the other fails on one side. Comparing the two *sources* textually
would fail on formatting; comparing behaviour over a table is what actually
matters.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from idp_common.config.prefix_mappings import canonical_key

REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).resolve().parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
)

FIXTURES = (
    REPO_ROOT
    / "src"
    / "ui"
    / "src"
    / "utils"
    / "__tests__"
    / "config-prefix-key.fixtures.json"
)

MIRROR = REPO_ROOT / "src" / "ui" / "src" / "utils" / "config-prefix-key.ts"


def _cases() -> list[tuple[str, str]]:
    data = json.loads(FIXTURES.read_text(encoding="utf-8"))
    return [(key, expected) for key, expected in data["cases"]]


@pytest.mark.unit
def test_the_shared_table_exists_and_is_not_empty():
    """A derived gate that silently finds nothing is the failure mode to avoid."""
    assert FIXTURES.is_file(), (
        f"{FIXTURES.relative_to(REPO_ROOT)} is missing. It is the pin between "
        "canonical_key and its TypeScript mirror; without it neither suite is "
        "measuring the other."
    )
    assert len(_cases()) >= 16


@pytest.mark.unit
def test_the_mirror_still_exists_and_names_this_gate():
    """If the mirror is deleted or moved, this gate must not pass vacuously."""
    assert MIRROR.is_file(), (
        f"{MIRROR.relative_to(REPO_ROOT)} is gone. If the mirror was removed "
        "because the preview now asks the server for the canonical form, delete "
        "this gate and its fixture table in the same change."
    )
    text = MIRROR.read_text(encoding="utf-8")
    assert "config-prefix-key.fixtures.json" in text, (
        "The mirror no longer points at the shared fixture table, so a reader has "
        "no way to find out that it is pinned."
    )


@pytest.mark.unit
@pytest.mark.parametrize("key,expected", _cases())
def test_python_canonicalization_matches_the_shared_table(key, expected):
    assert canonical_key(key) == expected, (
        f"canonical_key({key!r}) returned {canonical_key(key)!r}, and the shared "
        f"table says {expected!r}. If the Python rule changed deliberately, update "
        "the table — the vitest suite reads the same file, so the TypeScript mirror "
        "will then fail until it is changed to match."
    )
