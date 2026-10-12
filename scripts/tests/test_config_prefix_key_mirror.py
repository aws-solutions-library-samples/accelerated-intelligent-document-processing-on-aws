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

from idp_common.config.prefix_mappings import canonical_key, prefix_rejection_reason

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


def _rejection_cases() -> list[tuple[str, str | None]]:
    data = json.loads(FIXTURES.read_text(encoding="utf-8"))
    return [(prefix, fragment) for prefix, fragment in data["rejections"]]


@pytest.mark.unit
def test_the_rejection_table_exists_and_covers_both_verdicts():
    """A table of only-refusals or only-acceptances would pin half the rule."""
    cases = _rejection_cases()
    assert len(cases) >= 12
    assert any(fragment is None for _, fragment in cases), "no accepted prefix"
    assert any(fragment is not None for _, fragment in cases), "no refused prefix"
    # Entries matching several rules are what pin the ORDER; without one the table
    # cannot tell a reordered implementation from a correct one.
    assert ("/acme//x/", "must not start with '/'") in cases


@pytest.mark.unit
@pytest.mark.parametrize("prefix,fragment", _rejection_cases())
def test_python_prefix_rejection_matches_the_shared_table(prefix, fragment):
    """`prefix_rejection_reason` is the authority `prefixError` mirrors.

    The fragment names which RULE fires rather than the whole sentence: the client
    expands two of these messages for the admin reading them, so the two sides may
    word a refusal differently. What they may not do is disagree about *whether* a
    prefix is refused, or about *which* rule refused it -- which is what a reader
    of two different reasons for the same prefix would have to reconcile.
    """
    actual = prefix_rejection_reason(prefix)
    if fragment is None:
        assert actual is None, (
            f"the server refuses {prefix!r} ({actual!r}) but the shared table says "
            f"it is usable, so the form would accept a mapping the API rejects"
        )
    else:
        assert actual is not None, (
            f"the server accepts {prefix!r}, which the shared table says is "
            f"refused for: {fragment!r}"
        )
        assert fragment in actual, (
            f"prefix_rejection_reason({prefix!r}) refused it for a DIFFERENT rule "
            f"than the table records.\n  got:      {actual!r}\n  expected to "
            f"contain: {fragment!r}\nIf the check order changed deliberately, "
            f"update the table -- the vitest suite reads the same file, so the "
            f"TypeScript mirror will then fail until it matches."
        )


@pytest.mark.unit
def test_the_server_only_rules_are_recorded_and_real():
    """The documented asymmetries have to be asymmetries that exist.

    The client does not restate three of the server's rules, and that is a decision
    rather than drift -- but a decision recorded in a file nothing checks rots. Each
    entry is verified to be a rule the server actually enforces, so deleting one
    server-side leaves a note describing a difference that is no longer there.
    """
    data = json.loads(FIXTURES.read_text(encoding="utf-8"))
    rules = data["serverOnlyRules"]
    assert set(rules) == {
        "leading-or-trailing-whitespace",
        "prefix-length-in-bytes",
        "empty-prefix",
    }
    for reason in rules.values():
        assert len(reason) > 40, "a recorded asymmetry needs its reason written out"

    # Each one, measured against the real implementation.
    assert prefix_rejection_reason(" acme/ ") is not None
    assert prefix_rejection_reason("a" * 600 + "/") is not None
    assert prefix_rejection_reason("") is not None

    # And the client's counterpart genuinely does not carry them: it is handed the
    # TRIMMED value, so the whitespace rule is unreachable there by construction.
    assert prefix_rejection_reason(" acme/ ".strip()) is None
