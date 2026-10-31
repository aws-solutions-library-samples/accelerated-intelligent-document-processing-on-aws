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

    def test_without_git_it_fails_with_a_message_not_a_traceback(self, repo, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        proc = subprocess.run(
            [sys.executable, "-", str(repo), str(out / "python-packages.txt")],
            input=generator_python(),
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": str(tmp_path / "empty")},
        )
        assert proc.returncode != 0
        assert "git is not on PATH" in proc.stderr
        assert "Traceback" not in proc.stderr


class TestEveryPinIsAudited:
    def test_a_requirements_pin_of_a_locked_package_is_kept_beside_the_lock(
        self, repo, tmp_path
    ):
        proc, manifest, _ = run_generator(repo, tmp_path / "out")
        assert proc.returncode == 0, proc.stderr
        assert {"foo==2.0", "foo==1.5"} <= manifest_lines(manifest)

    def test_a_pin_of_an_unlocked_package_is_audited_at_the_pin(self, repo, tmp_path):
        proc, manifest, additional = run_generator(repo, tmp_path / "out")
        assert proc.returncode == 0, proc.stderr
        lines = manifest_lines(manifest)
        assert {"pin-only==0.3", "extra-pkg==4.1", "marked-pkg==5.0"} <= lines
        # Still handed to the resolver by name, so its transitive dependencies
        # are found; the shell merge skips the name itself, which is now present.
        names = set(additional.read_text().split())
        assert {"pin-only", "loose-pkg", "wild-pkg"} <= names

    def test_non_exact_specs_are_not_recorded_as_pins(self, repo, tmp_path):
        proc, manifest, _ = run_generator(repo, tmp_path / "out")
        assert proc.returncode == 0, proc.stderr
        lines = manifest_lines(manifest)
        assert not any(line.startswith(("loose-pkg", "wild-pkg")) for line in lines)

    def test_each_pair_appears_once(self, repo, tmp_path):
        write(repo, "svc2/requirements.txt", "foo==1.5\nFOO==2.0\n")
        git(repo, "add", "svc2")
        proc, manifest, _ = run_generator(repo, tmp_path / "out")
        assert proc.returncode == 0, proc.stderr
        foo = [
            line
            for line in manifest.read_text().splitlines()
            if re.match(r"(?i)foo==", line)
        ]
        assert sorted(foo) == ["foo==1.5", "foo==2.0"]

    def test_a_pin_that_is_not_a_pep_440_version_is_kept_not_fatal(
        self, repo, tmp_path
    ):
        write(repo, "svc3/requirements.txt", "odd-pkg==1.0\nodd-pkg==vendor-build\n")
        git(repo, "add", "svc3")
        proc, manifest, _ = run_generator(repo, tmp_path / "out")
        assert proc.returncode == 0, proc.stderr
        assert {"odd-pkg==1.0", "odd-pkg==vendor-build"} <= manifest_lines(manifest)


class TestRequirementShapes:
    def test_hash_pinned_lines_are_exact_pins(self, repo, tmp_path):
        write(
            repo,
            "hashed/requirements.txt",
            "hashed-a==1.0 \\\n"
            "    --hash=sha256:aaaa \\\n"
            "    --hash=sha256:bbbb\n"
            "hashed-b==2.0 --hash=sha256:cccc\n"
            "hashed-c==3.0 --hash sha256:dddd\n",
        )
        git(repo, "add", "hashed")
        proc, manifest, _ = run_generator(repo, tmp_path / "out")
        assert proc.returncode == 0, proc.stderr
        assert {"hashed-a==1.0", "hashed-b==2.0", "hashed-c==3.0"} <= manifest_lines(
            manifest
        )

    def test_a_comment_ending_in_a_backslash_does_not_swallow_the_next_line(
        self, repo, tmp_path
    ):
        write(repo, "cm/requirements.txt", "# see notes \\\nafter-comment==1.0\n")
        git(repo, "add", "cm")
        proc, manifest, _ = run_generator(repo, tmp_path / "out")
        assert proc.returncode == 0, proc.stderr
        assert "after-comment==1.0" in manifest_lines(manifest)

    def test_option_lines_are_not_package_names(self, repo, tmp_path):
        write(
            repo,
            "op/requirements.txt",
            "-r ../svc/requirements.txt\n-c constraints.txt\n"
            "--index-url https://example.invalid/simple\n",
        )
        git(repo, "add", "op")
        proc, manifest, additional = run_generator(repo, tmp_path / "out")
        assert proc.returncode == 0, proc.stderr
        text = manifest.read_text() + additional.read_text()
        assert "-r" not in text and "constraints" not in text and "-c" not in text

    def test_pins_are_written_with_normalized_names(self, repo, tmp_path):
        write(repo, "odd/requirements.txt", "Mixed_Case.Pkg==3.0\n")
        git(repo, "add", "odd")
        proc, manifest, _ = run_generator(repo, tmp_path / "out")
        assert proc.returncode == 0, proc.stderr
        assert "mixed-case-pkg==3.0" in manifest_lines(manifest)

    def test_first_party_names_reach_neither_the_manifest_nor_the_resolver(
        self, repo, tmp_path
    ):
        write(repo, "fp/requirements.txt", "idp-common==0.1\nidp_sdk>=1.0\n")
        git(repo, "add", "fp")
        proc, manifest, additional = run_generator(repo, tmp_path / "out")
        assert proc.returncode == 0, proc.stderr
        text = manifest.read_text() + additional.read_text()
        assert "idp" not in text.replace("_", "-")

    def test_a_locked_name_is_not_sent_to_the_resolver(self, repo, tmp_path):
        write(repo, "lk/requirements.txt", "bar>=0.5\n")
        git(repo, "add", "lk")
        proc, _, additional = run_generator(repo, tmp_path / "out")
        assert proc.returncode == 0, proc.stderr
        names = set(additional.read_text().split())
        assert "bar" not in names and "foo" not in names

    def test_a_tracked_file_deleted_from_the_working_tree_is_skipped(
        self, repo, tmp_path
    ):
        (repo / "svc" / "requirements.txt").unlink()
        proc, manifest, _ = run_generator(repo, tmp_path / "out")
        assert proc.returncode == 0, proc.stderr
        assert "pin-only==0.3" not in manifest_lines(manifest)

    def test_pyproject_dependencies_are_read_the_same_way(self, repo, tmp_path):
        write(
            repo,
            "lib/b/pyproject.toml",
            '[project]\nname = "b"\nversion = "1"\n'
            'dependencies = ["pyp-loose>=1", "pyp-pin==2.0", "idp-common"]\n'
            '[project.optional-dependencies]\nx = ["../a", "pyp-opt>=1"]\n',
        )
        git(repo, "add", "lib/b")
        proc, manifest, additional = run_generator(repo, tmp_path / "out")
        assert proc.returncode == 0, proc.stderr
        assert "pyp-pin==2.0" in manifest_lines(manifest)
        names = set(additional.read_text().split())
        assert {"pyp-loose", "pyp-opt", "pyp-pin"} <= names
        assert not {"idp-common", "../a"} & names


FAKE_UV = """#!/usr/bin/env bash
# Stand-in for uv: `uv run ... python3 ARGS` runs the real interpreter, and
# `uv pip compile IN ... --output-file OUT` copies IN to $FAKE_SEEN and writes
# $FAKE_RESOLVED to OUT, so the merge can be driven without the network.
if [ "$1" = run ]; then
    while [ "$1" != python3 ]; do shift; done
    shift
    exec "$REAL_PYTHON" "$@"
fi
if [ "$1" = pip ]; then
    cp "$3" "$FAKE_SEEN"
    while [ $# -gt 0 ]; do
        [ "$1" = --output-file ] && out=$2
        shift
    done
    printf '%s' "$FAKE_RESOLVED" > "$out"
    exit 0
fi
exit 1
"""


class TestShellMerge:
    """The whole script, with `uv` replaced so the resolver's answer is chosen."""

    def run_script(self, repo, tmp_path, resolved):
        (repo / "scripts").mkdir(exist_ok=True)
        (repo / "scripts" / GENERATOR.name).write_text(GENERATOR.read_text())
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        (fake_bin / "uv").write_text(FAKE_UV)
        (fake_bin / "uv").chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "REAL_PYTHON": sys.executable,
            "FAKE_RESOLVED": resolved,
            "FAKE_SEEN": str(tmp_path / "seen.txt"),
        }
        proc = subprocess.run(
            ["bash", str(repo / "scripts" / GENERATOR.name)],
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        manifest = repo / "dist" / "manifests" / "python-packages.txt"
        return [line for line in manifest.read_text().splitlines() if line]

    def test_the_resolver_adds_what_ships_and_nothing_else(self, repo, tmp_path):
        write(
            repo,
            "a1/requirements.txt",
            "shared-loose==4.0\nMixed_Case==1.0\nnear-miss==2.0.1\n",
        )
        # Loose names spelled un-normalised, and a loose name that merely
        # extends a pinned-only one.
        write(
            repo,
            "a2/requirements.txt",
            "Shared_Loose>=4.0\nNear.Miss>=2\npin-only-ext>=1\n",
        )
        git(repo, "add", "a1", "a2")
        lines = self.run_script(
            repo,
            tmp_path,
            "foo==3.0\n"  # locked: the lock's version ships
            "pin-only==9.0\n"  # only ever pinned: the pin ships
            "mixed-case==9.0\n"  # pinned under another spelling
            "loose-pkg==2.0\n"  # asked for loosely: this is what it gets
            "shared-loose==5.0\n"  # pinned in one file, loose in another
            "new-trans==1.0\n"  # a transitive dependency nothing names
            "near-miss==2.0\n"  # a prefix of the pinned 2.0.1, still distinct
            "pin-only-ext==1.0\n"
            "direct-ref @ https://example.invalid/d.whl\n",
        )
        assert len(lines) == len(set(lines)), lines
        assert {
            "loose-pkg==2.0",
            "shared-loose==4.0",
            "shared-loose==5.0",
            "new-trans==1.0",
            "pin-only==0.3",
            "mixed-case==1.0",
            "near-miss==2.0",
            "near-miss==2.0.1",
            "pin-only-ext==1.0",
            "direct-ref @ https://example.invalid/d.whl",
        } <= set(lines)
        assert not {"foo==3.0", "pin-only==9.0", "mixed-case==9.0"} & set(lines)

    def test_a_resolved_pair_already_listed_is_not_repeated(self, repo, tmp_path):
        write(repo, "a1/requirements.txt", "shared-loose==4.0\n")
        write(repo, "a2/requirements.txt", "shared-loose>=4.0\n")
        git(repo, "add", "a1", "a2")
        lines = self.run_script(repo, tmp_path, "shared-loose==4.0\n")
        assert lines.count("shared-loose==4.0") == 1

    def test_the_scratch_files_are_removed(self, repo, tmp_path):
        self.run_script(repo, tmp_path, "loose-pkg==2.0\n")
        left = {p.name for p in (repo / "dist" / "manifests").iterdir()}
        assert left == {"python-packages.txt", "node-packages.txt"}
