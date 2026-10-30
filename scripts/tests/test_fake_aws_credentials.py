# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A fake AWS credential in the tree must read as a placeholder to a secret scanner.

Secret scanners run over this repository and raise a hardcoded-credential finding for
any AWS key ID, or any secret-key-length string beside one, that they cannot tell from
a real credential. A test fixture is reported exactly like a leaked key, and marking
the finding as a false positive does not stop the same line being reported again. The
shape the scanners pass is the one AWS's own documentation uses: a key ID ending in
``EXAMPLE`` (``AKIAIOSFODNN7EXAMPLE``, ``ASIAIOSFODNN7EXAMPLE``) and a secret key
containing it (``wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY``).

Two rules, over every file git tracks or would track:

* every token shaped like an AWS key ID or IAM unique ID ends in ``EXAMPLE``;
* a file holding such a token quotes no 40-character run of the secret-key alphabet
  without ``EXAMPLE`` in it, because a scanner pairs a key ID with a nearby string of
  that length. A git SHA fixture beside a fake key is the usual case; build it as an
  expression, ``"1234abcd" * 5``, which is a value and not a literal.

The convention, and how to keep a fixture distinct while following it, is in
``.claude/skills/testing-qa.md``.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable

import gate_premises
import pytest

KEY_ID_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:AKIA|ASIA|ABIA|ACCA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|APKA"
    r"|A3T[A-Z0-9])[A-Z0-9]{16,17}(?![A-Za-z0-9])"
)
SECRET_LENGTH_LITERAL_RE = re.compile(r"""(?<=["'`])[A-Za-z0-9/+=]{40}(?=["'`])""")
PLACEHOLDER_MARK = "EXAMPLE"
GUIDANCE = (
    "Fake AWS credentials must use AWS's documented placeholder shape so secret "
    "scanners do not report them: end a key ID in EXAMPLE, and build any other "
    '40-character value in the same file as an expression ("1234abcd" * 5). See '
    "'Fake credentials in fixtures' in .claude/skills/testing-qa.md. If the value is "
    "a real credential, rotate it before anything else."
)


def unmarked_key_ids(text: str) -> list[str]:
    return [m for m in KEY_ID_RE.findall(text) if not m.endswith(PLACEHOLDER_MARK)]


def unmarked_secret_length_literals(text: str) -> list[str]:
    if not KEY_ID_RE.search(text):
        return []
    return [
        m for m in SECRET_LENGTH_LITERAL_RE.findall(text) if PLACEHOLDER_MARK not in m
    ]


@functools.cache
def _tree() -> tuple[tuple[str, str], ...]:
    paths = set(gate_premises.tracked_files(prune_local_work=False))
    paths |= set(gate_premises.tracked_files(include_untracked=True))
    out = []
    for rel in sorted(paths):
        path = gate_premises.REPO_ROOT / rel
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        out.append((rel, data.decode("utf-8", errors="replace")))
    return tuple(out)


def _sites(finder: Callable[[str], list[str]]) -> list[str]:
    found = []
    for rel, text in _tree():
        for value in dict.fromkeys(finder(text)):
            for match in re.finditer(re.escape(value), text):
                line = text.count("\n", 0, match.start()) + 1
                found.append(f"{rel}:{line}: {value}")
    return found


@pytest.mark.unit
def test_every_aws_key_id_shaped_token_is_a_documented_placeholder() -> None:
    sites = _sites(unmarked_key_ids)
    assert not sites, GUIDANCE + "\n" + "\n".join(sites)


@pytest.mark.unit
def test_no_secret_length_literal_sits_beside_a_key_id() -> None:
    sites = _sites(unmarked_secret_length_literals)
    assert not sites, GUIDANCE + "\n" + "\n".join(sites)


@pytest.mark.unit
def test_the_tree_scan_reads_this_file() -> None:
    assert any(
        rel == "scripts/tests/test_fake_aws_credentials.py" for rel, _ in _tree()
    )


TAIL = "SCOPED" * 2 + "KEY0"
SHA = "0123abcd" * 5


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "found"),
    [
        (f'key = "ASIA{TAIL}"', [f"ASIA{TAIL}"]),
        (f'key = "AKIA{TAIL}"', [f"AKIA{TAIL}"]),
        (f'role = "AROA{TAIL}X"', [f"AROA{TAIL}X"]),
        (f"key=ASIA{TAIL};", [f"ASIA{TAIL}"]),
        ('key = "ASIAIOSFODNN7EXAMPLE"', []),
        ('role = "AROADBQP57FF2AEXAMPLE"', []),
        (f'key = "XASIA{TAIL}"', []),
        (f'key = "ASIA{TAIL}00"', []),
    ],
)
def test_key_id_rule(text: str, found: list[str]) -> None:
    assert unmarked_key_ids(text) == found


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "found"),
    [
        (f'ID = "AKIAIOSFODNN7EXAMPLE"\nTREE = "{SHA}"', [SHA]),
        (f"ID = 'ASIAIOSFODNN7EXAMPLE'\nTREE = '{SHA}'", [SHA]),
        (f'TREE = "{SHA}"', []),
        (f'ID = "AKIAIOSFODNN7EXAMPLE"\nTREE = "{SHA}0"', []),
        (
            'ID = "AKIAIOSFODNN7EXAMPLE"\n'
            'KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"',
            [],
        ),
        ('ID = "AKIAIOSFODNN7EXAMPLE"\nTREE = "0123abcd" * 5', []),
    ],
)
def test_secret_length_rule(text: str, found: list[str]) -> None:
    assert unmarked_secret_length_literals(text) == found
