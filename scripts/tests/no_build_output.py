# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Refuse a test session that leaves SAM build output in the checkout.

One definition, loaded by path from the `conftest.py` of every suite that needs it —
the same arrangement `scripts/tests/no_outbound_aws.py` and
`scripts/tests/first_party_provenance.py` use, and for the same reason: the
alternative is a copy per suite, and a list of copies drifts.

## What this is for

Several build and packaging steps in this tree resolve **both** their inputs and
their output directory against the process cwd. A test that chdirs to the repository
root to satisfy the inputs therefore writes the output into the working tree.

The cost is not tidiness. `scripts/srt/run.py` discovers SAM build output by globbing
the project root for `.aws-sam` directories, and `make srt-scan` drops into a degraded
mode when it finds any — phantom LOCAL-ONLY findings, and, because `sam package`
re-serializes YAML and drops the `# checkov:skip=` comments that keep the source
templates clean, a checkov crash on the largest templates. So a test that leaves build
output makes the security gate depend on whether the test suite ran first; it stops
being the scan CI runs. That shipped once: issue #1491, where
`lib/idp_sdk/tests/unit/test_multi_doc_discovery_source_zip.py` ran the real packaging
step with the cwd at the repository root.

## Two properties worth stating, because getting either wrong makes this useless

**The semantics are "unchanged by this session", never "absent".** A genuine
`python3 publish.py …` writes these very paths in the checkout it runs from, so a
developer tree legitimately and commonly carries them. A control that demanded absence
would go red on any built tree and blame the test run for files it did not create.

**Watch each artefact, not only its directory.** A directory's mtime moves when an
entry is created, deleted or renamed — *not* when an existing file is rewritten in
place. So on exactly the built trees described above, a re-leak would truncate and
rewrite the artefact with the enclosing directory's mtime untouched. Snapshotting the
directories alone would pass the case this control exists for.

## Why the discovery is a glob rather than a list

`publish.py` writes `.aws-sam` directories throughout the tree, not only at the root:
`nested/<x>/.aws-sam/packaged.yaml`, `patterns/<x>/.aws-sam/packaged.yaml`,
`feature-platform/*/.aws-sam/packaged.yaml`, plus `.aws-sam/idp-main.yaml`,
`.aws-sam/layers/` and `.aws-sam/unified-source.zip` at the root. A hardcoded list of
watched paths would leave a test that leaks `patterns/unified/.aws-sam` degrading
`srt-scan` with this control green.

The predicate below is therefore **the same one the scanner uses** — `**/.aws-sam`,
`is_dir()`, excluding anything under `node_modules` — so the control and the scanner
agree about what counts by construction rather than by two lists being kept in step.
"""

from __future__ import annotations

from pathlib import Path

#: Mirrors `scripts/srt/run.py`'s own discovery. Keep the two identical: a path this
#: misses is a path that degrades the scan with the control green.
_GLOB = "**/.aws-sam"
_EXCLUDED_PART = "node_modules"


def build_output_dirs(project_root: Path) -> list[Path]:
    """Every SAM build directory the SRT scan would discover under `project_root`."""
    return sorted(
        p
        for p in project_root.glob(_GLOB)
        if p.is_dir() and _EXCLUDED_PART not in p.parts
    )


def snapshot(project_root: Path) -> dict[str, tuple[bool, int | None]]:
    """`{repo-relative path: (exists, st_mtime_ns)}` for the build output present.

    Covers each discovered directory **and every file under it**, so an in-place
    rewrite of an existing artefact is visible — see the module docstring.
    """
    state: dict[str, tuple[bool, int | None]] = {}

    def record(p: Path) -> None:
        rel = p.relative_to(project_root).as_posix()
        try:
            state[rel] = (True, p.stat().st_mtime_ns)
        except OSError:
            # Raced away between glob and stat; absent is the honest record.
            state[rel] = (False, None)

    for directory in build_output_dirs(project_root):
        record(directory)
        for child in sorted(directory.rglob("*")):
            record(child)
    return state


def changed(before: dict, after: dict) -> list[str]:
    """Paths that appeared, vanished or moved between two snapshots."""
    return sorted(
        rel
        for rel in set(before) | set(after)
        if before.get(rel, (False, None)) != after.get(rel, (False, None))
    )


def message(changed_paths: list[str], project_root: Path, what: str) -> str:
    """The failure text, naming the remedy rather than only the symptom."""
    listed = ", ".join(changed_paths[:10])
    more = f" (+{len(changed_paths) - 10} more)" if len(changed_paths) > 10 else ""
    return (
        f"{what} changed SAM build output under {project_root}: {listed}{more}. A "
        f"build or packaging step ran with the cwd set to a directory inside the "
        f"repository instead of a staging directory. `make srt-scan` degrades when "
        f"an .aws-sam directory is present, so this makes the security gate depend "
        f"on whether the test suite ran first — see issue #1491 and the chdir "
        f"section in .claude/skills/testing-qa.md. `make srt-clean` removes it."
    )


def fixture(project_root: Path):
    """A session-scoped autouse fixture a `conftest.py` can bind to a name.

    Session-scoped and autouse rather than opt-in, because the test that leaks is by
    definition one that did not know it was writing — #1491's did not. An assertion
    inside one test's own fixture can only observe its own call, so it cannot catch
    the next instance, which is the whole point of this control.

    Reports through the session fixture's teardown, which pytest surfaces as an
    ERROR rather than a test failure. Still a non-zero exit.
    """
    import pytest

    @pytest.fixture(scope="session", autouse=True)
    def no_build_output():
        before = snapshot(project_root)
        yield
        after = snapshot(project_root)
        moved = changed(before, after)
        assert not moved, message(moved, project_root, "this test session")

    return no_build_output
