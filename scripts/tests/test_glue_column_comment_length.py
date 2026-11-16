# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every Glue table column ``Comment`` must fit Glue's 255-character limit.

Glue rejects a column comment longer than 255 characters with a validation error,
and CloudFormation surfaces that as a failed ``AWS::Glue::Table`` — so the whole stack
update fails and rolls back. Nothing catches it earlier: ``cfn-lint`` has no length
rule for this property. Column comments here carry usage guidance for whoever queries
the table, which is exactly the kind of text that grows past the limit unnoticed.

That is not hypothetical: the ``n_runs`` comment on the metering rollup tables reached
311 characters and the first thing to notice was a stack update rolling back at
``MeteringDocsHourlyTable`` and ``MeteringDocsDailyTable``.

Both ``StorageDescriptor.Columns`` and ``PartitionKeys`` are checked, since Glue
applies the same limit to each.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Glue's documented maximum for ``Column.Comment``.
MAX_GLUE_COLUMN_COMMENT = 255


class _CfnLoader(yaml.SafeLoader):
    """CloudFormation short-form tags are not valid YAML tags; keep the value."""


def _passthrough(loader, _tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_CfnLoader.add_multi_constructor("!", _passthrough)


def _templates() -> list[Path]:
    """Every git-tracked template that declares a Glue table, discovered by content.

    Reading only tracked files keeps build output and other worktrees' copies out,
    so the result is the same here as in CI.
    """
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "*.yml", "*.yaml"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    found = []
    for rel in (p for p in out.split("\0") if p):
        path = REPO_ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "AWSTemplateFormatVersion" in text and "AWS::Glue::Table" in text:
            found.append(path)
    return found


def _column_comments() -> list[tuple[Path, str, str, str]]:
    out: list[tuple[Path, str, str, str]] = []
    for path in _templates():
        doc = yaml.load(path.read_text(encoding="utf-8"), Loader=_CfnLoader)
        for logical_id, resource in (doc.get("Resources") or {}).items():
            if not isinstance(resource, dict):
                continue
            if resource.get("Type") != "AWS::Glue::Table":
                continue
            table = (resource.get("Properties") or {}).get("TableInput") or {}
            columns = list((table.get("StorageDescriptor") or {}).get("Columns") or [])
            columns += list(table.get("PartitionKeys") or [])
            for column in columns:
                if not isinstance(column, dict):
                    continue
                comment = column.get("Comment")
                if isinstance(comment, str):
                    out.append((path, logical_id, str(column.get("Name")), comment))
    return out


@pytest.mark.unit
def test_column_comments_are_discovered():
    """A vacuous pass here would hide every over-long comment."""
    assert _column_comments(), (
        "discovery found no Glue column Comment at all, though template.yaml declares "
        "some — the CloudFormation tag loader or the content-based template discovery "
        "has broken, and this file is now checking nothing."
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "path,logical_id,column,comment",
    _column_comments(),
    ids=[f"{lid}.{col}" for _, lid, col, _ in _column_comments()],
)
def test_glue_column_comment_fits_the_limit(
    path: Path, logical_id: str, column: str, comment: str
):
    assert len(comment) <= MAX_GLUE_COLUMN_COMMENT, (
        f"{path.relative_to(REPO_ROOT)}: {logical_id} column {column!r} has a "
        f"{len(comment)}-character Comment, over Glue's {MAX_GLUE_COLUMN_COMMENT} "
        "limit, so the table fails to create and the stack rolls back. Shorten it "
        "and keep the longer explanation in the docs."
    )
