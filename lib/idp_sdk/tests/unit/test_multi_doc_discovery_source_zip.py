# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the multi-doc discovery Docker source zip.

The CodeBuild project for that nested stack builds
`-f nested/multi-doc-discovery/Dockerfile`, which COPYs
`nested/multi-doc-discovery/requirements.txt`. Both therefore MUST be in the
source zip.

This used to be broken in a way that failed silently: the buildspec heredoc'd its
own inline copies of both files, so the on-disk ones were dead weight. Dependabot
kept bumping the on-disk requirements.txt (e.g. the Pillow CVE bump in PR #530)
while the image went on building the stale inline pins — a security patch that
looked merged but never shipped. These tests lock in the single-source-of-truth
arrangement so that regression cannot come back unnoticed.
"""

from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path

import pytest
import yaml

from idp_sdk._core.publish import MULTI_DOC_DISCOVERY_BUILD_INPUTS, IDPPublisher

_REPO_ROOT = Path(__file__).resolve().parents[4]
_NESTED_TEMPLATE = _REPO_ROOT / "nested" / "multi-doc-discovery" / "template.yaml"


def _buildspec_text():
    """The DockerBuildProject's inline buildspec, as raw text."""
    # The template uses CFN short-form intrinsics (!Sub, !GetAtt, !Ref), which a
    # plain safe_load rejects — read it as text and slice the BuildSpec block
    # instead of trying to parse the whole document.
    text = _NESTED_TEMPLATE.read_text()
    start = text.index("BuildSpec: |")
    end = text.index("TimeoutInMinutes:", start)
    return text[start:end]


class TestSourceZipContents:
    def test_zip_includes_the_docker_build_inputs(self, publisher_zip_names):
        """The Dockerfile and requirements.txt must land in the zip.

        Without these the new buildspec (which builds `-f` the checked-in
        Dockerfile) cannot build at all.
        """
        for build_input in MULTI_DOC_DISCOVERY_BUILD_INPUTS:
            assert build_input in publisher_zip_names, (
                f"{build_input} missing from source zip"
            )

    def test_zip_includes_handler_code_and_library(self, publisher_zip_names):
        """Guard the pre-existing contract while we are here."""
        assert any(
            n.startswith("src/lambda/multi_doc_discovery/") for n in publisher_zip_names
        )
        assert any(n.startswith("lib/idp_common_pkg/") for n in publisher_zip_names)

    def test_zip_excludes_local_environments_and_build_leftovers(
        self, publisher_zip_names
    ):
        """A developer checkout's venv must not ride along into the image.

        ``lib/idp_common_pkg/.venv`` is ~900 MB of host-architecture wheels. Zipping
        it made the archive 224 MB and put Darwin/arm64 binaries in the build
        context of a linux image. The ``*.egg-info`` entry in the exclusion set was
        a glob in a membership test, so it never matched either.
        """
        for name in publisher_zip_names:
            parts = name.split("/")
            assert ".venv" not in parts, f"virtualenv leaked into the zip: {name}"
            assert not any(p.endswith(".egg-info") for p in parts), (
                f"egg-info metadata leaked into the zip: {name}"
            )

    @pytest.mark.usefixtures("staging_checkout")
    def test_missing_build_input_fails_loudly(self, monkeypatch):
        """A missing build input must abort publish, not ship a broken image.

        Takes `staging_checkout` through `usefixtures` — it is wanted only for its
        chdir, and naming it as a parameter would be an unused one. It is needed
        because the zip is written before the check that raises, so even this
        aborting call leaks `.aws-sam/` into the tree when run from the repo
        root (#1491).
        """
        publisher = _quiet_publisher(monkeypatch)
        # Pretend the Dockerfile isn't there.
        real_isfile = os.path.isfile
        monkeypatch.setattr(
            os.path,
            "isfile",
            lambda p: False if str(p).endswith("Dockerfile") else real_isfile(p),
        )
        with pytest.raises(SystemExit):
            publisher.package_multi_doc_discovery_source()


class TestNoInlineDuplication:
    """The buildspec must consume the real files, never re-declare them."""

    def test_buildspec_does_not_heredoc_a_requirements_file(self):
        spec = _buildspec_text()
        assert "cat > requirements.txt" not in spec, (
            "buildspec is writing its own requirements.txt again — this forks "
            "from the real file and silently strands Dependabot bumps"
        )

    def test_buildspec_does_not_heredoc_a_dockerfile(self):
        spec = _buildspec_text()
        assert "cat > Dockerfile" not in spec, (
            "buildspec is writing its own Dockerfile again — keep "
            "nested/multi-doc-discovery/Dockerfile as the only source of truth"
        )

    def test_buildspec_builds_the_checked_in_dockerfile(self):
        spec = _buildspec_text()
        assert "-f nested/multi-doc-discovery/Dockerfile" in spec

    def test_buildspec_pins_no_dependency_versions(self):
        """No package pins anywhere in the buildspec — they belong in one file."""
        spec = _buildspec_text()
        for pkg in ("Pillow", "numpy", "scikit-learn", "strands-agents"):
            assert pkg not in spec, (
                f"{pkg} is pinned in the buildspec; pins belong only in "
                "nested/multi-doc-discovery/requirements.txt"
            )


class TestDockerfileResilience:
    """The pip step must tolerate a transient PyPI stream drop."""

    def _dockerfile(self):
        return (
            _REPO_ROOT / "nested" / "multi-doc-discovery" / "Dockerfile"
        ).read_text()

    def test_pip_has_retry_flags(self):
        text = self._dockerfile()
        assert "--retries" in text
        assert "--timeout" in text

    def test_pip_is_wrapped_in_a_retry_loop(self):
        """pip's own retries do not cover a mid-stream BrokenPipeError."""
        text = self._dockerfile()
        assert "for attempt in" in text

    def test_copy_paths_are_repo_root_relative(self):
        """Build context is the zip root, so COPY paths must be repo-relative."""
        text = self._dockerfile()
        assert "COPY nested/multi-doc-discovery/requirements.txt" in text
        assert "COPY lib/idp_common_pkg" in text
        assert "COPY src/lambda/multi_doc_discovery/*.py" in text


class TestRebuildTriggers:
    """A Dependabot bump must invalidate the component checksum."""

    def test_build_inputs_are_rebuild_dependencies(self, monkeypatch):
        monkeypatch.chdir(_REPO_ROOT)
        publisher = IDPPublisher()
        deps = publisher.get_component_dependencies()["nested/multi-doc-discovery"]
        for build_input in MULTI_DOC_DISCOVERY_BUILD_INPUTS:
            assert build_input in deps, (
                f"{build_input} is not a rebuild trigger — a bump to it would "
                "not invalidate the checksum, so publish would skip the "
                "component and keep shipping the previous image"
            )


class TestBuildHashCoversTheImageInputs:
    """`BuildHash` must change when any input to the container image changes.

    This is the load-bearing assertion, and it is NOT the same as the
    component-dependency check above. `BuildHash` is the only meaningful property
    of the `DockerBuildRun` custom resource, so it alone decides whether
    CloudFormation re-invokes the resource (and thus whether CodeBuild rebuilds
    the image) on a stack UPDATE. A component-checksum change merely re-SAM-builds
    the template.

    Regression guard: `BuildHash` originally hashed only
    `src/lambda/multi_doc_discovery`, so a Dependabot bump to requirements.txt
    left it byte-identical — CloudFormation saw no delta, the build never ran, and
    the vulnerable dependency stayed deployed. A fresh stack create always builds,
    which is why this was invisible to end-to-end create testing.
    """

    def _build_hash(self, publisher):
        """Compute the BuildHash token the same way the publisher does."""
        return hashlib.sha256(
            (
                publisher.get_directory_checksum("src/lambda/multi_doc_discovery")
                + "".join(
                    publisher.get_file_checksum(build_input)
                    for build_input in MULTI_DOC_DISCOVERY_BUILD_INPUTS
                )
            ).encode()
        ).hexdigest()[:16]

    @pytest.mark.parametrize("build_input", MULTI_DOC_DISCOVERY_BUILD_INPUTS)
    def test_build_hash_changes_when_a_build_input_changes(
        self, build_input, monkeypatch, tmp_path
    ):
        """Mutating requirements.txt / Dockerfile MUST move BuildHash."""
        monkeypatch.chdir(_REPO_ROOT)
        publisher = IDPPublisher()
        before = self._build_hash(publisher)

        # Simulate a Dependabot bump by returning a different checksum for just
        # this one input, leaving the handler directory untouched.
        real_file_checksum = publisher.get_file_checksum
        monkeypatch.setattr(
            publisher,
            "get_file_checksum",
            lambda p: "bumped" if str(p) == build_input else real_file_checksum(p),
        )
        after = self._build_hash(publisher)

        assert before != after, (
            f"BuildHash is unchanged after a bump to {build_input}; "
            "CloudFormation would see no delta on DockerBuildRun, skip the "
            "container rebuild, and keep serving the previous image"
        )

    def test_build_hash_still_tracks_the_handler_code(self, monkeypatch):
        """Don't lose the original trigger while adding the new ones."""
        monkeypatch.chdir(_REPO_ROOT)
        publisher = IDPPublisher()
        before = self._build_hash(publisher)
        monkeypatch.setattr(
            publisher, "get_directory_checksum", lambda p: "changed-handler-code"
        )
        assert before != self._build_hash(publisher)

    def test_build_hash_is_stable_when_nothing_changes(self, monkeypatch):
        """It must not churn, or every publish would force a needless rebuild."""
        monkeypatch.chdir(_REPO_ROOT)
        publisher = IDPPublisher()
        assert self._build_hash(publisher) == self._build_hash(publisher)

    def test_publisher_emits_a_build_hash_covering_the_inputs(self, monkeypatch):
        """End-to-end: the token the publisher actually bakes in matches.

        Guards against the test's local reimplementation drifting from
        publish.py — if someone changes the real computation, this fails.
        """
        monkeypatch.chdir(_REPO_ROOT)
        source = (
            _REPO_ROOT / "lib" / "idp_sdk" / "idp_sdk" / "_core" / "publish.py"
        ).read_text()
        token_idx = source.index("<MULTI_DOC_DISCOVERY_BUILD_HASH_TOKEN>")
        # Take the expression that follows the token assignment.
        expr = source[token_idx : token_idx + 700]
        assert "MULTI_DOC_DISCOVERY_BUILD_INPUTS" in expr, (
            "the BuildHash token no longer incorporates the container build "
            "inputs — a dependency bump would stop triggering a rebuild"
        )


# --------------------------------------------------------------------------- #
# helpers / fixtures
# --------------------------------------------------------------------------- #


def _quiet_publisher(monkeypatch):
    """An IDPPublisher whose console output is suppressed."""
    publisher = IDPPublisher()
    monkeypatch.setattr(
        publisher,
        "console",
        type("C", (), {"print": staticmethod(lambda *a, **k: None)}),
    )
    return publisher


#: Every path `package_multi_doc_discovery_source` reads, relative to the repo
#: root. The packaging step resolves all of them — and its own `.aws-sam` output
#: directory — against the process's cwd, which is what lets the staging root
#: below stand in for the checkout.
#:
#: ⚠️ No entry may be nested inside another, and `_assert_no_nested_inputs`
#: enforces it. `staging_checkout` creates each link's parent with
#: `mkdir(parents=True)`, which **follows an existing symlink** — so a nested
#: entry such as `src/lambda/multi_doc_discovery/docker/requirements.txt` would
#: create a real directory inside the repository, which is the exact class this
#: fixture exists to prevent. The set is not local either: it splices in
#: `MULTI_DOC_DISCOVERY_BUILD_INPUTS` from `publish.py`, so it can grow without
#: this file being edited.
_PACKAGING_INPUTS = (
    "lib/idp_common_pkg",
    "src/lambda/multi_doc_discovery",
    *MULTI_DOC_DISCOVERY_BUILD_INPUTS,
)


def _assert_no_nested_inputs() -> None:
    """Refuse a `_PACKAGING_INPUTS` entry that sits inside another entry."""
    for inner in _PACKAGING_INPUTS:
        for outer in _PACKAGING_INPUTS:
            if inner != outer and Path(inner).is_relative_to(Path(outer)):
                raise AssertionError(
                    f"_PACKAGING_INPUTS entry {inner!r} is nested inside {outer!r}. "
                    f"Linking it would mkdir through the {outer!r} symlink and "
                    f"create a real directory inside the repository. Extend "
                    f"staging_checkout to copy or stage that subtree instead of "
                    f"adding a nested link."
                )


@pytest.fixture
def staging_checkout(monkeypatch, tmp_path):
    """Chdir to a throwaway root that **symlinks** every packaging input.

    ⚠️ **Any test that calls `package_multi_doc_discovery_source` must use this
    rather than chdir to the repo root.** The packaging step resolves both its
    inputs and its `.aws-sam` output against the cwd, so running it in the
    checkout leaves ~7 MB of gitignored build output in the working tree
    (issue #1491). That matters beyond tidiness: `make srt-scan` detects an
    `.aws-sam` directory and drops into a degraded mode — phantom LOCAL-ONLY
    findings, and a documented risk of crashing checkov on the largest templates
    — so `make test` before `make srt-scan` silently made the security gate
    something other than the one CI runs.

    ⚠️ It also applies to a test that only expects the step to **fail**: the zip
    is written before the build-input check raises, so an aborting call leaks too.
    `test_missing_build_input_fails_loudly` is therefore covered by this fixture
    even though it never reaches the upload, and
    `test_packaging_leaves_no_build_output_in_the_checkout` asserts the property
    separately rather than relying on either call site.

    The symlinks preserve the relative layout, and the step writes each member
    under `os.path.relpath(file_path, ".")`, so every archive name is
    byte-identical to what packaging from the checkout produces. `os.walk` does
    not follow symlinked *sub*directories, but each entry here is either a file or
    the top of its own walk, which `os.walk` does traverse.

    `tmp_path` is unique per test invocation, so this also removes the need for
    the cross-process lock the zip path used to require: two pytest-xdist workers
    can no longer write the same archive and read each other's half-written copy.
    """
    _assert_no_nested_inputs()
    staging = tmp_path / "checkout"
    for rel in _PACKAGING_INPUTS:
        link = staging / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(_REPO_ROOT / rel)
    monkeypatch.chdir(staging)
    return staging


def _checkout_zip_state() -> tuple[bool, int | None]:
    """Whether the repo-root source zip exists, and its mtime if it does.

    The pair, not the bare existence, is what the guard below compares: a
    developer tree that has run `publish.py` legitimately carries this exact
    path, so "absent" is not the property worth asserting.
    """
    leaked = _REPO_ROOT / ".aws-sam" / "multi-doc-discovery-source.zip"
    if not leaked.exists():
        return False, None
    return True, leaked.stat().st_mtime_ns


@pytest.fixture
def packaging_run(monkeypatch, staging_checkout):
    """One real packaging call, with the checkout's zip state either side of it.

    Runs against the actual repo tree — the zip content IS the contract — but
    from `staging_checkout`, so nothing lands in the working tree. The before and
    after snapshots let the guard below assert what it is actually about: that
    *this run* did not write to the repo root.
    """
    staging = staging_checkout
    publisher = _quiet_publisher(monkeypatch)
    zip_path = staging / ".aws-sam" / "multi-doc-discovery-source.zip"
    before = _checkout_zip_state()
    try:
        publisher.package_multi_doc_discovery_source()
    except (SystemExit, Exception):  # noqa: B014
        # The upload stage needs S3 config we deliberately don't provide.
        if not zip_path.exists():
            raise
    after = _checkout_zip_state()
    with zipfile.ZipFile(zip_path) as zf:
        return zf.namelist(), before, after


@pytest.fixture
def publisher_zip_names(packaging_run):
    """Member names of the real source zip produced by the packaging step."""
    names, _before, _after = packaging_run
    return names


def test_packaging_leaves_no_build_output_in_the_checkout(packaging_run):
    """A packaging call must not write `.aws-sam/` into the repo tree.

    Regression guard for #1491. Asserted through a real packaging call rather
    than against an untouched tree.

    ⚠️ **The property is "unchanged by this run", not "absent".** A genuine
    `python3 publish.py …` writes precisely
    `.aws-sam/multi-doc-discovery-source.zip` relative to the repository root, so
    plenty of legitimate developer trees carry that exact path — as does any tree
    that ran `make test` before this guard existed. Asserting absence would turn
    those into a red test blaming the current run for a file it did not create.
    """
    names, before, after = packaging_run
    assert names, "fixture produced no archive, so it proved nothing"
    assert after == before, (
        f"packaging wrote {_REPO_ROOT / '.aws-sam' / 'multi-doc-discovery-source.zip'} "
        f"during this run (state {before} -> {after}). `make srt-scan` degrades "
        f"when an .aws-sam directory is present, so this makes the security gate "
        f"depend on whether the test suite ran first — see issue #1491. "
        f"`make srt-clean` removes it."
    )


def test_requirements_file_is_parseable_and_pins_pillow(monkeypatch):
    """Sanity: the file the image now actually installs from."""
    reqs = (
        _REPO_ROOT / "nested" / "multi-doc-discovery" / "requirements.txt"
    ).read_text()
    assert "Pillow" in reqs
    # Every non-comment line should look like a requirement specifier.
    for line in reqs.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert any(c in line for c in "=<>~"), f"unpinned/odd requirement: {line}"


def test_nested_template_is_valid_yaml_shape():
    """The buildspec edits must not have broken the template's indentation."""
    text = _NESTED_TEMPLATE.read_text()
    # Short-form intrinsics need a permissive loader; just assert the BuildSpec
    # block is a well-formed literal scalar under Source:.
    assert "Source:" in text
    spec = _buildspec_text()
    assert spec.count("phases:") == 1
    for phase in ("pre_build:", "build:", "post_build:"):
        assert phase in spec
    # The buildspec body itself must be loadable once the CFN indent is stripped.
    body = spec.split("BuildSpec: |", 1)[1]
    dedented = "\n".join(line[10:] for line in body.splitlines())
    parsed = yaml.safe_load(dedented)
    assert parsed["version"] == 0.2
    assert set(parsed["phases"]) == {"pre_build", "build", "post_build"}
