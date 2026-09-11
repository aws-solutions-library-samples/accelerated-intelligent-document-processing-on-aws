#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Fail on hardcoded ``arn:aws:`` ARNs in first-party Python source.

WHY THIS EXISTS. `make check-arn-partitions` has always guarded GovCloud ARN
correctness — but only in **CloudFormation templates** and Step Functions ASL.
Python was never scanned, so runtime code could format an ARN with a literal
``arn:aws:`` and ship. It did: a live GovCloud deployment reported every Bedrock
Data Automation invoke failing with ``The provided ARN is invalid`` because
``BdaService`` built its data-automation profile ARN as
``f"arn:aws:bedrock:{region}:..."`` (issue #527). The same pattern was in three
other places, including the Knowledge Base resolver's model ARN.

This is the missing half of that gate. It is deliberately narrow — one pattern,
``arn:aws:`` — matching what the template gate flags, because that is the case
that silently breaks in ``aws-us-gov`` and ``aws-cn``.

The right fix is almost never a suppression: derive the partition from the caller
identity (``sts:GetCallerIdentity()["Arn"].split(":")[1]``) or take it from the
template via an env var (``!Ref AWS::Partition``).

WHEN A MATCH IS LEGITIMATE (a regex that *detects* the pattern, a CLI default
documented as commercial-only), mark the line:

    pattern = r"arn:aws:(?!\\$\\{AWS::Partition\\})"  # arn-partition-ok: detector

Docstrings are skipped automatically — an ARN in a doctest or an example is
documentation, and a pragma comment inside a docstring would become part of the
string rather than a comment (the same trap that made two `# nosec` markers
inert elsewhere in this repo).

Usage:
    python3 scripts/check_python_arn_partitions.py [--quiet]
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# First-party Python that runs against AWS. Deliberately excludes tests (they
# assert on literal ARNs by design) and generated/vendored trees.
SCAN_ROOTS = (
    "lib/idp_common_pkg/idp_common",
    "lib/idp_sdk/idp_sdk",
    "lib/idp_cli_pkg/idp_cli",
    "lib/idp_feature_sdk/idp_feature_sdk",
    "lib/idp_mcp_connector_pkg",
    "src/lambda",
    "nested",
    "scripts",
    "patterns",
    "feature-platform",
    # User-deployed Lambda hook samples and the benchmark harness both talk to
    # AWS and are shipped to customers, so they belong in the gate.
    "samples",
    "benchmarks",
)

# Path fragments that exclude a file entirely.
EXCLUDE_FRAGMENTS = (
    "/tests/",
    "/test_",
    "/.aws-sam/",
    "/build/",
    "/node_modules/",
    "/__pycache__/",
    "/.venv/",
    "/site-packages/",
    "/conftest.py",
    # The CI/SDLC deploy harness runs only in the commercial CI account, by
    # construction (it provisions the pipeline's own IAM and test stacks there).
    # Gating it would add suppressions to code that cannot run in another
    # partition. If the harness ever grows a GovCloud probe, drop this exclusion.
    "/scripts/sdlc/",
    # This checker names the pattern it searches for.
    "/check_python_arn_partitions.py",
)

NEEDLE = "arn:aws:"
PRAGMA = "arn-partition-ok"

GUIDANCE = """
Hardcoded 'arn:aws:' does not resolve in the aws-us-gov or aws-cn partitions —
AWS account IDs do not exist across partitions, so the ARN is simply invalid
there (and the error usually reads as a permissions problem, not a partition
one).

Fix by deriving the partition rather than assuming it:

  identity = boto3.client("sts").get_caller_identity()
  partition = (identity.get("Arn") or "arn:aws:").split(":")[1] or "aws"
  arn = f"arn:{partition}:service:{region}:{identity['Account']}:resource/x"

In a Lambda, prefer passing it in from the template:

  Environment:
    Variables:
      AWS_PARTITION: !Ref AWS::Partition

If a match is genuinely intentional (a regex that DETECTS this pattern, a
documented commercial-only default), append a pragma with a reason:

  ...  # arn-partition-ok: <why this is correct>
""".rstrip()


def _is_excluded(path: Path) -> bool:
    posix = f"/{path.as_posix()}/"
    if any(fragment in posix for fragment in EXCLUDE_FRAGMENTS):
        return True
    # Vendored copies of idp_common (e.g. the data-generator bundles its own
    # snapshot). The canonical source under lib/ is what we gate; flagging the
    # copies would report the same line several times and cannot be fixed
    # independently — they are refreshed from lib/.
    if "/idp_common_pkg/" in posix and not path.as_posix().startswith("lib/"):
        return True
    return False


def _arn_is_in_comment(line: str) -> bool:
    """True if the ARN occurrence sits inside a ``#`` comment.

    Approximate but sufficient: if a ``#`` appears before the ARN and is not
    itself inside a quoted string, the ARN is commentary. Prose describing an ARN
    format ("# ARN format: arn:aws:states:...") is documentation, and requiring a
    pragma on it would be noise.
    """
    hash_idx = -1
    in_single = in_double = False
    escaped = False
    for i, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            # A backslash escape — the next char is literal, so a \" must not
            # be read as closing a string (which would mis-detect the comment
            # boundary and could suppress a real finding).
            escaped = True
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            hash_idx = i
            break
    if hash_idx == -1:
        return False
    return line.index(NEEDLE) > hash_idx


def _prose_line_ranges(source: str) -> set[int]:
    """Line numbers belonging to a DOCSTRING.

    Deliberately docstrings only, not every multi-line string. An earlier version
    skipped all multi-line literals on the grounds that "a pragma cannot live
    inside a string literal" — but that is defeated by the statement-level pragma
    below: a comment after the closing quotes is inside the statement span, so a
    non-docstring multi-line string IS suppressible. Skipping them all created two
    real false negatives:

      * an IAM policy embedded as a triple-quoted JSON document — genuine ARN
        construction, not prose;
      * implicit string concatenation across lines
        (``"arn:aws:s3:::" "bucket/key"``), which the parser sees as one
        multi-line constant.

    Docstrings stay exempt because they are documentation by definition and a
    pragma inside one would become part of the string (the trap that made two
    `# nosec` markers inert in this repo). Prose in a non-docstring literal —
    argparse epilogs, usage banners — takes a pragma on the closing line.

    Returns an empty set if the file does not parse — a syntax error is not this
    checker's business.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    lines: set[int] = set()
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        # Docstrings (including single-line ones).
        if isinstance(node, holders):
            body = getattr(node, "body", None)
            if body:
                first = body[0]
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    lines.update(
                        range(first.lineno, (first.end_lineno or first.lineno) + 1)
                    )
    return lines


def _statement_spans(source: str) -> list[tuple[int, int]]:
    """(start, end) line spans of every statement, innermost last.

    Used so a pragma may sit anywhere in the STATEMENT that builds the ARN, not
    only on the exact physical line. `ruff format` reflows long expressions and
    moves a trailing comment to the closing-paren line, which would silently
    detach a physical-line-only pragma — the suppression would stop working
    after a purely cosmetic reformat.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt):
            spans.append((node.lineno, node.end_lineno or node.lineno))
    # Innermost (shortest) spans last so the narrowest match wins.
    spans.sort(key=lambda s: s[1] - s[0], reverse=True)
    return spans


def scan_file(path: Path) -> list[tuple[int, str]]:
    """Return [(lineno, line)] for offending lines in one file."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    if NEEDLE not in source:
        return []

    lines = source.splitlines()
    skip = _prose_line_ranges(source)
    spans = _statement_spans(source)

    def _statement_is_annotated(lineno: int) -> bool:
        """True if a pragma applies to the ARN on `lineno`.

        The pragma must appear AT or AFTER the ARN's line, and within the
        innermost statement containing it. Directional on purpose:

          * after allows the two positions a pragma legitimately takes — the
            ARN's own line, and a later line of the same statement, because
            `ruff format` moves a trailing comment to the closing line when it
            reflows an expression, and a multi-line string literal can only be
            annotated past its closing quotes;
          * before is rejected, which closes the leak in the earlier
            "anywhere in the statement" rule: a pragma on the OPENING line of a
            large dict or call suppressed every ARN nested inside it, so one
            intentional suppression silently covered unrelated ARNs added later.
        """
        best = None
        for start, end in spans:
            if start <= lineno <= end:
                best = (start, end)  # later (narrower) spans overwrite
        if best is None:
            return PRAGMA in lines[lineno - 1]
        _, end = best
        return any(PRAGMA in ln for ln in lines[lineno - 1 : end])

    findings = []
    for lineno, line in enumerate(lines, start=1):
        if NEEDLE not in line or lineno in skip:
            continue
        if PRAGMA in line or _arn_is_in_comment(line):
            continue
        if _statement_is_annotated(lineno):
            continue
        findings.append((lineno, line.strip()))
    return findings


def iter_python_files() -> list[Path]:
    seen: set[Path] = set()
    for root in SCAN_ROOTS:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            rel = path.relative_to(REPO_ROOT)
            if _is_excluded(rel):
                continue
            seen.add(rel)
    return sorted(seen)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quiet", action="store_true", help="only print on failure"
    )
    args = parser.parse_args()

    files = iter_python_files()
    all_findings: list[tuple[Path, int, str]] = []
    for rel in files:
        for lineno, line in scan_file(REPO_ROOT / rel):
            all_findings.append((rel, lineno, line))

    if not all_findings:
        if not args.quiet:
            print(
                f"✅ No hardcoded '{NEEDLE}' references in first-party Python "
                f"({len(files)} files checked)"
            )
        return 0

    print(f"ERROR: Found {len(all_findings)} hardcoded '{NEEDLE}' reference(s) "
          "in first-party Python:")
    for rel, lineno, line in all_findings:
        print(f"  {rel}:{lineno}: {line}")
    print(GUIDANCE)
    return 1


if __name__ == "__main__":
    sys.exit(main())
