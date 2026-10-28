# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Only ``idp_common.review_queues.testset_labels`` marks a test-set label reviewed.

A reviewed label carries provenance two readers rely on: ``labelSource =
reviewed-human`` (draft labeling leaves such a label alone, and the queue counts it
as reviewed), an ``_editHistory`` entry with the field diffs, and the
confidence-curve observations the review produced. Three writers once set it, each
with different provenance, one of them in the browser. They now all call
``TestSetLabels.write_reviewed``, and this scan keeps it that way: no other
first-party module may assign the reviewed value to ``labelSource``. The machine
value (``draft-machine``) is not policed: a regroup's new section and the labeling
harvest write it where they are.

It reads source text, through git, so a build copy under ``.aws-sam`` is never read.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from repo_files import tracked_paths

REPO = Path(__file__).resolve().parents[2]

#: The source trees that hold handlers, resolvers and the library.
SCANNED = ("src/lambda/", "nested/", "patterns/", "lib/idp_common_pkg/idp_common/")

#: A ``labelSource`` set to the reviewed value: a dict key or an item assignment.
REVIEWED_WRITE = re.compile(
    r"labelSource[\"']?\]?\s*[:=]\s*(LABEL_SOURCE_HUMAN|[\"']reviewed-human[\"'])"
)

#: The one writer, and each other match with why it is not a label write.
ALLOWED = {
    "lib/idp_common_pkg/idp_common/review_queues/testset_labels.py": "the one writer",
    "lib/idp_common_pkg/idp_common/review_queues/testset_summaries.py": (
        "a summary of the labels (the document is reviewed when every section is), "
        "not a label write"
    ),
}


def _is_test(rel: str) -> bool:
    return (
        "/tests/" in f"/{rel}"
        or Path(rel).name.startswith("test_")
        or Path(rel).name == "conftest.py"
    )


def _scanned_files() -> list[str]:
    files = []
    for path in tracked_paths(REPO, "*.py"):
        rel = path.relative_to(REPO).as_posix()
        if (
            rel.startswith(SCANNED)
            and not _is_test(rel)
            and "/.aws-sam/" not in f"/{rel}"
        ):
            files.append(rel)
    return files


def _writers(files: list[str]) -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for rel in files:
        for number, line in enumerate(
            (REPO / rel).read_text(encoding="utf-8").splitlines(), start=1
        ):
            if line.lstrip().startswith("#"):
                continue
            if REVIEWED_WRITE.search(line):
                found.setdefault(rel, []).append(number)
    return found


@pytest.mark.unit
def test_the_scan_sees_the_files_it_polices():
    files = _scanned_files()
    assert "src/lambda/complete_section_review/index.py" in files
    assert "nested/api-resolvers/src/lambda/test_set_resolver/index.py" in files
    assert set(ALLOWED) <= set(files)


@pytest.mark.unit
def test_only_the_shared_writer_marks_a_label_reviewed():
    writers = _writers(_scanned_files())
    unlisted = {f: lines for f, lines in writers.items() if f not in ALLOWED}
    assert not unlisted, (
        "These set labelSource to reviewed-human themselves; write the label with "
        f"idp_common.review_queues.testset_labels.TestSetLabels.write_reviewed: {unlisted}"
    )


@pytest.mark.unit
def test_every_allowed_file_still_matches():
    writers = _writers(_scanned_files())
    stale = sorted(set(ALLOWED) - set(writers))
    assert not stale, f"stale ALLOWED entries, which no longer match: {stale}"


@pytest.mark.unit
@pytest.mark.parametrize(
    "line, matches",
    [
        ('    data["labelSource"] = "reviewed-human"', True),
        ("    label['labelSource'] = LABEL_SOURCE_HUMAN", True),
        ('    {"labelSource": "reviewed-human"}', True),
        ('    content = {"labelSource": LABEL_SOURCE_DRAFT}', False),
        ('    if label.get("labelSource") == "reviewed-human":', False),
    ],
)
def test_the_pattern(line, matches):
    assert bool(REVIEWED_WRITE.search(line)) is matches
