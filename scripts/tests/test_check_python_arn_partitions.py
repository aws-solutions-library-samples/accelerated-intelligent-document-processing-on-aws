# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the Python hardcoded-ARN-partition gate.

`make check-arn-partitions` scanned CloudFormation templates and Step Functions
ASL but never Python, which is how `f"arn:aws:bedrock:{region}:..."` reached
runtime and made every Bedrock Data Automation invoke fail in GovCloud with
"The provided ARN is invalid" (issue #527).

These tests pin the properties that decide whether the gate is useful or noise:
it must catch real ARN construction, stay quiet on documentation, and — the
subtle one — keep honouring a suppression after `ruff format` reflows the line
the pragma was written on.
"""

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import check_python_arn_partitions as gate  # noqa: E402

pytestmark = pytest.mark.unit


def _scan(tmp_path, source: str):
    target = tmp_path / "sample.py"
    target.write_text(source)
    return gate.scan_file(target)


def test_flags_real_arn_construction(tmp_path):
    findings = _scan(
        tmp_path,
        'def build(region, account):\n'
        '    return f"arn:aws:bedrock:{region}:{account}:data-automation-profile/x"\n',
    )
    assert len(findings) == 1
    assert findings[0][0] == 2


def test_partition_aware_construction_is_clean(tmp_path):
    findings = _scan(
        tmp_path,
        'def build(partition, region, account):\n'
        '    return f"arn:{partition}:bedrock:{region}:{account}:thing/x"\n',
    )
    assert findings == []


def test_docstring_example_is_not_flagged(tmp_path):
    """An ARN in a doctest/example is documentation, and cannot carry a pragma."""
    findings = _scan(
        tmp_path,
        'def build():\n'
        '    """Return an ARN.\n'
        '\n'
        '    >>> build()\n'
        '    "arn:aws:bedrock:us-east-1::foundation-model/x"\n'
        '    """\n'
        '    return None\n',
    )
    assert findings == []


def test_comment_is_not_flagged(tmp_path):
    findings = _scan(
        tmp_path,
        "# ARN format: arn:aws:states:<region>:<account>:execution:<name>\n"
        "x = 1\n",
    )
    assert findings == []


def test_multiline_usage_string_is_not_flagged(tmp_path):
    """argparse epilog / usage blocks are prose and cannot carry a pragma."""
    findings = _scan(
        tmp_path,
        'EPILOG = """\n'
        "Examples:\n"
        "  --model-arn arn:aws:bedrock:us-east-1:123456789012:custom-model/x\n"
        '"""\n',
    )
    assert findings == []


def test_pragma_on_the_same_line_suppresses(tmp_path):
    findings = _scan(
        tmp_path,
        'PATTERN = r"arn:aws:(?!x)"  # arn-partition-ok: detector\n',
    )
    assert findings == []


def test_pragma_survives_a_reflowed_statement(tmp_path):
    """The regression that made the first version of this gate wrong.

    `ruff format` wraps a long expression and moves the trailing comment to the
    closing-paren line. A physical-line-only pragma check would silently stop
    suppressing after a purely cosmetic reformat — so the pragma is honoured
    anywhere in the enclosing STATEMENT.
    """
    findings = _scan(
        tmp_path,
        "def f(identity):\n"
        "    partition = (\n"
        '        (identity.get("Arn") or "arn:aws:").split(":")[1] or "aws"\n'
        "    )  # arn-partition-ok: fallback used only to PARSE the partition out\n"
        "    return partition\n",
    )
    assert findings == []


def test_pragma_does_not_leak_to_a_neighbouring_statement(tmp_path):
    """A suppression must not silently cover the NEXT statement's ARN."""
    findings = _scan(
        tmp_path,
        'good = r"arn:aws:(?!x)"  # arn-partition-ok: detector\n'
        'bad = f"arn:aws:s3:::{bucket}/*"\n',
    )
    assert len(findings) == 1
    assert findings[0][0] == 2


def test_vendored_idp_common_copies_are_excluded():
    """Only the canonical lib/ copy is gated; vendored snapshots are refreshed."""
    assert gate._is_excluded(
        Path("feature-platform/idp-data-generator/idp_common_pkg/idp_common/x.py")
    )
    assert not gate._is_excluded(Path("lib/idp_common_pkg/idp_common/x.py"))


def test_tests_and_build_artifacts_are_excluded():
    for path in (
        "lib/idp_common_pkg/tests/unit/test_thing.py",
        "nested/api-resolvers/.aws-sam/build/Fn/index.py",
        "src/lambda/foo/__pycache__/index.py",
    ):
        assert gate._is_excluded(Path(path)), path


def test_repo_is_currently_clean():
    """The gate must pass on the committed tree — otherwise it cannot be wired in."""
    offenders = [
        (rel, lineno, line)
        for rel in gate.iter_python_files()
        for lineno, line in gate.scan_file(gate.REPO_ROOT / rel)
    ]
    assert offenders == [], (
        "hardcoded arn:aws: in first-party Python: "
        + "; ".join(f"{r}:{n}: {ln}" for r, n, ln in offenders)
    )
