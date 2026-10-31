# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the Python half of `scripts/generate-dep-manifest.sh`.

`make dep-audit` audits exactly the `name==version` lines this generator writes,
so what the generator reads and what it keeps decide what the gate can see. Two
properties are pinned here:

- **Discovery is the repository's, not the disk's.** `uv.lock` and
  `requirements*.txt` files are taken from `git ls-files` (tracked plus
  untracked-but-not-ignored), so a gitignored copy — a staged build tree, a
  scratch worktree under the checkout — is not audited as if it shipped, and a
  local run reads what CI reads.
- **Every distinct (name, version) pair is kept.** An exact `==` pin in a
  requirements file is the version that file's Lambda installs, so it is
  audited at that version even when a lockfile carries another version of the
  same package, and even when no lockfile carries the package at all.

The generator's Python lives in a heredoc inside the shell script, so the tests
lift it out and run that exact text against a throwaway git repository. The
shell half (`uv pip compile` over the loose names) is not run: it needs the
network, and the names it is handed are asserted directly instead.
"""

import os
import pathlib
import re
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
GENERATOR = REPO_ROOT / "scripts" / "generate-dep-manifest.sh"


def generator_python() -> str:
    text = GENERATOR.read_text(encoding="utf-8")
    m = re.search(r"<< 'PYTHON_SCRIPT'\n(.*?)\nPYTHON_SCRIPT\n", text, re.DOTALL)
    assert m, "generate-dep-manifest.sh no longer carries the PYTHON_SCRIPT heredoc"
    return m.group(1)


def lock(*pairs: tuple) -> str:
    return "version = 1\n\n" + "\n".join(
        f'[[package]]\nname = "{n}"\nversion = "{v}"\n' for n, v in pairs
    )


def write(root: pathlib.Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )


def run_generator(root: pathlib.Path, out_dir: pathlib.Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = out_dir / "python-packages.txt"
    proc = subprocess.run(
        [sys.executable, "-", str(root), str(manifest)],
        input=generator_python(),
        capture_output=True,
        text=True,
    )
    return proc, manifest, out_dir / ".additional-reqs.txt"


@pytest.fixture
def repo(tmp_path):
    """A repository with tracked, untracked and gitignored dependency files."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    write(root, ".gitignore", "staged/\n")
    # Tracked lockfile: the one CI reads.
    write(root, "lib/a/uv.lock", lock(("foo", "2.0"), ("bar", "1.0")))
    # A gitignored copy of a lockfile, with a different version of a shared
    # package and a package only the copy has.
    write(root, "staged/a/uv.lock", lock(("foo", "9.9"), ("ignored-only", "1.0")))
    write(root, "staged/requirements.txt", "ignored-req==1.0\n")
    # Tracked requirements: a pin of a locked package at another version, a pin
    # of a package no lock has, a loose spec, extras and a marker.
    write(
        root,
        "svc/requirements.txt",
        "foo==1.5\n"
        "pin-only==0.3\n"
        "loose-pkg>=1.0\n"
        "extra-pkg[speedups]==4.1  # trailing comment\n"
        "marked-pkg==5.0 ; python_version >= '3.10'\n"
        "wild-pkg==1.*\n"
        "../../lib/a\n",
    )
    # A tracked file under a directory whose name contains "deps".
    write(root, "deps-tools/requirements-dev.txt", "deps-dir-pkg==1.0\n")
    git(root, "add", ".gitignore", "lib", "svc", "deps-tools")
    # Untracked but not ignored: a file a developer has not `git add`-ed yet is
    # part of their change, so it is read.
    write(root, "new/requirements.txt", "new-pkg==1.0\n")
    return root


def manifest_lines(manifest: pathlib.Path) -> set:
    return {line for line in manifest.read_text().splitlines() if line}


class TestDiscovery:
    def test_gitignored_lockfiles_and_requirements_are_not_read(self, repo, tmp_path):
        proc, manifest, additional = run_generator(repo, tmp_path / "out")
        assert proc.returncode == 0, proc.stderr
        lines = manifest_lines(manifest)
        assert "foo==9.9" not in lines
        assert not any(line.startswith("ignored-only") for line in lines)
        assert not any(line.startswith("ignored-req") for line in lines)
        assert "ignored-req" not in additional.read_text()

    def test_tracked_and_untracked_unignored_files_are_read(self, repo, tmp_path):
        proc, manifest, additional = run_generator(repo, tmp_path / "out")
        assert proc.returncode == 0, proc.stderr
        assert {"foo==2.0", "bar==1.0"} <= manifest_lines(manifest)
        # Requirements names reach the resolver whatever else happens to them.
        assert {"new-pkg", "deps-dir-pkg", "pin-only"} <= set(
            additional.read_text().split()
        )

    def test_outside_a_git_checkout_it_fails_rather_than_reading_nothing(
        self, tmp_path
    ):
        root = tmp_path / "not-a-repo"
        write(root, "lib/a/uv.lock", lock(("foo", "2.0")))
        proc, manifest, _ = run_generator(root, tmp_path / "out")
        assert proc.returncode != 0
        assert "git ls-files" in proc.stderr
