# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for `IDPPublisher`'s smart-rebuild detection.

`publish.py` does not rebuild every component on every run. It hashes each
component's declared dependencies, compares the result against a `.checksum`
file written by the previous successful publish, and rebuilds only what moved.
That makes the checksum functions the load-bearing correctness boundary of the
whole publisher: **a checksum that fails to notice a changed file means the run
skips the rebuild and re-publishes the previous artifact**, which is how a stale
Lambda zip or a stale container image reaches a release. The failure is silent —
the publish reports success — so nothing downstream catches it.

The family under test here is `get_source_files_checksum`,
`get_component_checksum`, `compute_directory_hash`,
`get_component_dependencies`, `get_components_needing_rebuild`,
`update_component_checksum`, `_delete_checksum_file`, `clear_component_cache`
and `smart_rebuild_detection`.

What shaped these tests is the need to make each one *discriminating*. A test
that only asserts "the hash changed when I changed something" passes against
several wrong implementations, so the cases below are chosen so that a plausibly
wrong implementation gives a different answer from the real one:

* A **rename with byte-identical content** must change the hash. An
  implementation that hashed contents only — the obvious simplification — would
  return the same value, and a renamed handler would not be redeployed.
* A change **two directories deep** must change the hash, so a non-recursive
  walk is visible.
* Moving a file **between subdirectories** with identical content must change
  the hash, for the same reason as the rename.
* The files the source-checksum deliberately *ignores* (`__pycache__`,
  `test_*.py`, `tests/`, `.md`, dotfiles) must **not** change it, while
  `compute_directory_hash` — which walks everything — **must** see them. If both
  functions agreed on every input, one of these tests would be proving nothing
  about the other.
* Every measurement uses a **fresh publisher**, because
  `get_source_files_checksum` memoises per instance; reusing one would make
  every "changed" assertion measure the cache rather than the tree.

Two tests are marked as pinning defects rather than desired behaviour; see their
docstrings.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess

import pytest
from rich.console import Console

from idp_sdk._core import publish as publish_mod
from idp_sdk._core.publish import IDPPublisher

_BUCKET = "artifacts-bucket"
_PREFIX = "idp"
_VERSION = "0.6.9"
_REGION = "us-east-1"

# Every component `get_component_dependencies` knows about, and which of them
# carry `./lib/idp_common_pkg/idp_common` (LIB_DEPENDENCY) as a dependency. The
# second set is what makes an edit inside the shared library fan out.
_ALL_COMPONENTS = {
    "main",
    "nested/api-resolvers",
    "nested/bedrockkb",
    "nested/multi-doc-discovery",
    "patterns/unified",
    "feature-platform/main-stack-extensions",
    "lib",
}
_COMPONENTS_THAT_USE_LIB = {
    "main",
    "nested/api-resolvers",
    "nested/multi-doc-discovery",
    "patterns/unified",
    "lib",
}


def _publisher(capture=False):
    """A publisher with the deployment context populated and nothing else.

    `publish.py`'s checksums fold bucket/prefix/region in deliberately, so those
    have to be set for any checksum comparison to mean anything.
    """
    pub = IDPPublisher(verbose=False)
    pub.bucket = _BUCKET
    pub.prefix = _PREFIX
    pub.version = _VERSION
    pub.prefix_and_version = f"{_PREFIX}/{_VERSION}"
    pub.region = _REGION
    if capture:
        pub.console = Console(file=io.StringIO(), width=200, no_color=True)
    return pub


def _console_text(pub):
    return pub.console.file.getvalue()


class _UndeletableShutil:
    """`shutil` whose `rmtree` always fails; everything else is the real module.

    Stands in for the two states that actually break a cache clear: a broken
    symlink inside the tree (which `rmtree` refuses) and a file held open by
    another process.
    """

    def rmtree(self, path, **kwargs):
        raise OSError(39, "Directory not empty", str(path))

    def __getattr__(self, name):
        return getattr(shutil, name)


class _RecordingSubprocess:
    """`subprocess` that records argv instead of running anything."""

    def __init__(self):
        self.calls = []

    def run(self, cmd, **kwargs):
        self.calls.append(list(cmd))

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    def __getattr__(self, name):
        return getattr(subprocess, name)


def _write(root, rel_path, content="x\n"):
    """Write `content` to `root/rel_path`, creating parents."""
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _source_checksum(directory):
    """Checksum `directory` through a *fresh* publisher.

    `get_source_files_checksum` caches on the instance, so measuring twice from
    one publisher returns the first answer regardless of the tree. Every
    before/after comparison in this file goes through here.
    """
    return _publisher().get_source_files_checksum(directory)


def _build_repo_tree(root):
    """A minimal tree containing every path `get_component_dependencies` names.

    Deliberately not flat: several dependencies are directories with nested
    content, so a non-recursive walk would produce a constant for them.
    """
    files = {
        "template.yaml": "Resources: {}\n",
        "Dockerfile.optimized": "FROM public.ecr.aws/lambda/python:3.12\n",
        "src/lambda/multi_doc_discovery/index.py": "def handler(e, c): return 1\n",
        "src/lambda/other/handler.py": "def handler(e, c): return 2\n",
        "config_library/pattern/config.yaml": "classes: []\n",
        "lib/idp_common_pkg/setup.py": "setup()\n",
        "lib/idp_common_pkg/idp_common/__init__.py": "VERSION = '1'\n",
        "lib/idp_common_pkg/idp_common/ocr/service.py": "class OcrService: pass\n",
        "nested/api-resolvers/template.yaml": "Resources: {}\n",
        "nested/api-resolvers/src/resolver.py": "def resolve(): pass\n",
        "nested/bedrockkb/template.yaml": "Resources: {}\n",
        "nested/bedrockkb/src/handler.py": "def handler(): pass\n",
        "nested/multi-doc-discovery/template.yaml": "Resources: {}\n",
        "nested/multi-doc-discovery/Dockerfile": "FROM scratch\n",
        "nested/multi-doc-discovery/requirements.txt": "boto3==1.40.0\n",
        "nested/multi-doc-discovery/docker_build_lambda/index.py": "def h(): pass\n",
        "patterns/unified/template.yaml": "Resources: {}\n",
        "patterns/unified/src/ocr/index.py": "def handler(): pass\n",
        "patterns/unified/statemachine/workflow.asl.json": '{"StartAt": "A"}\n',
        "patterns/unified/buildspec.yml": "version: 0.2\n",
        "feature-platform/main-stack-extensions/template.yaml": "Resources: {}\n",
        "feature-platform/main-stack-extensions/lambdas/index.py": "def h(): pass\n",
        "feature-platform/main-stack-extensions/appsync/schema.graphql": "type Q\n",
    }
    for rel, content in files.items():
        _write(root, rel, content)


def _rebuild_names(pub=None):
    """The set of component names `get_components_needing_rebuild` returns."""
    pub = pub or _publisher(capture=True)
    return {item["component"] for item in pub.get_components_needing_rebuild()}


# ---------------------------------------------------------------------------
# get_source_files_checksum
# ---------------------------------------------------------------------------


def test_source_checksum_of_a_missing_directory_is_the_empty_string(
    monkeypatch, tmp_path
):
    """An absent dependency contributes a known-empty value, not an exception.

    `get_components_needing_rebuild` calls this for every declared dependency,
    including ones a trimmed checkout may not have, so raising here would break
    the publish on a partial tree.
    """
    monkeypatch.chdir(tmp_path)
    assert _source_checksum("does/not/exist") == ""


def test_source_checksum_changes_when_a_file_s_contents_change(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "pkg/a.py", "VALUE = 1\n")
    _write(tmp_path, "pkg/b.py", "OTHER = 2\n")

    before = _source_checksum("pkg")
    _write(tmp_path, "pkg/a.py", "VALUE = 2\n")
    after = _source_checksum("pkg")

    assert before != after


def test_source_checksum_changes_on_a_rename_that_keeps_the_content(
    monkeypatch, tmp_path
):
    """The relative path is part of the hashed material, not just the bytes.

    A failure here means an implementation that hashes contents alone: renaming
    a Lambda handler (or swapping two files' names) would leave the checksum
    byte-identical, the component would be judged up to date, and the previously
    published artifact — with the old filename — would stay deployed.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "pkg/handler_old.py", "def handler(): pass\n")
    before = _source_checksum("pkg")

    (tmp_path / "pkg" / "handler_old.py").rename(tmp_path / "pkg" / "handler_new.py")
    after = _source_checksum("pkg")

    assert before != after
    # And the bytes really were identical, so content hashing alone could not
    # have distinguished them.
    assert (tmp_path / "pkg" / "handler_new.py").read_text() == "def handler(): pass\n"


def test_source_checksum_changes_when_a_file_moves_between_subdirectories(
    monkeypatch, tmp_path
):
    """Same content, different directory, must still be a different checksum."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "pkg/one/mod.py", "SHARED = 1\n")
    _write(tmp_path, "pkg/two/keep.py", "KEEP = 1\n")
    before = _source_checksum("pkg")

    (tmp_path / "pkg" / "two" / "mod.py").write_text("SHARED = 1\n")
    (tmp_path / "pkg" / "one" / "mod.py").unlink()
    after = _source_checksum("pkg")

    assert before != after


def test_source_checksum_changes_when_a_file_is_added_or_removed(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "pkg/a.py", "A = 1\n")
    _write(tmp_path, "pkg/b.py", "B = 1\n")
    baseline = _source_checksum("pkg")

    _write(tmp_path, "pkg/c.py", "C = 1\n")
    with_added = _source_checksum("pkg")
    assert with_added != baseline

    (tmp_path / "pkg" / "c.py").unlink()
    (tmp_path / "pkg" / "b.py").unlink()
    with_removed = _source_checksum("pkg")
    assert with_removed != baseline
    assert with_removed != with_added


def test_source_checksum_sees_a_change_two_directories_deep(monkeypatch, tmp_path):
    """The walk is recursive.

    A non-recursive implementation would return a constant for a dependency
    whose only content is nested — which is the shape of every real dependency
    in `get_component_dependencies` (`./src`, `patterns/unified/src`, ...).
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "pkg/deep/deeper/mod.py", "LEVEL = 1\n")
    before = _source_checksum("pkg")

    _write(tmp_path, "pkg/deep/deeper/mod.py", "LEVEL = 2\n")
    after = _source_checksum("pkg")

    assert before != after


def test_source_checksum_ignores_build_artifacts_tests_and_non_source_files(
    monkeypatch, tmp_path
):
    """Only source-shaped files count, so noise does not force a rebuild.

    Each addition below is individually excluded by the implementation. Adding
    them all at once and asserting the checksum is *unchanged* is what pins the
    exclusion set; if any one of them started counting, a `pytest` run or a
    README edit would force a full rebuild of the whole publisher.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "pkg/mod.py", "REAL = 1\n")
    baseline = _source_checksum("pkg")

    _write(tmp_path, "pkg/__pycache__/mod.cpython-312.pyc", "bytecode\n")
    _write(tmp_path, "pkg/mod.pyc", "bytecode\n")
    _write(tmp_path, "pkg/.hidden.py", "HIDDEN = 1\n")
    _write(tmp_path, "pkg/.ruff_cache/cache.json", "{}\n")
    _write(tmp_path, "pkg/test_mod.py", "def test_x(): pass\n")
    _write(tmp_path, "pkg/mod_test.py", "def test_y(): pass\n")
    _write(tmp_path, "pkg/tests/real_suite.py", "def test_z(): pass\n")
    _write(tmp_path, "pkg/test/other_suite.py", "def test_w(): pass\n")
    _write(tmp_path, "pkg/README.md", "# docs\n")
    _write(tmp_path, "pkg/data.csv", "a,b\n")
    _write(tmp_path, "pkg/.checksum", "deadbeef\n")
    _write(tmp_path, "pkg/build/artifact.py", "BUILT = 1\n")
    _write(tmp_path, "pkg/node_modules/left-pad/index.js", "module.exports = 1\n")
    _write(tmp_path, "pkg/thing.egg-info/PKG-INFO", "Name: thing\n")

    assert _source_checksum("pkg") == baseline


@pytest.mark.parametrize(
    "filename",
    ["Dockerfile", "Makefile", "requirements.txt", "setup.py", "my-template.xyz"],
)
def test_source_checksum_counts_extensionless_build_inputs_and_template_names(
    monkeypatch, tmp_path, filename
):
    """A `Dockerfile` has no source extension but is still a build input.

    `my-template.xyz` covers the separate `"template" in name.lower()` rule: a
    template is hashed whatever its extension, because a CloudFormation
    template is the single most consequential file a component owns.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "pkg/mod.py", "REAL = 1\n")
    baseline = _source_checksum("pkg")

    _write(tmp_path, f"pkg/{filename}", "content\n")
    assert _source_checksum("pkg") != baseline


def test_a_dependency_path_that_is_a_file_where_a_directory_was_expected_is_skipped(
    monkeypatch, tmp_path
):
    """An unreadable or non-directory path yields the empty-tree hash, not a crash.

    `os.scandir` on a regular file raises `NotADirectoryError`, and on a
    directory the process cannot enter raises `PermissionError`; both are
    `OSError` and both are swallowed. So pointing a declared dependency at the
    wrong kind of path silently contributes a constant — the publish continues
    rather than failing, which is the trade this `except` makes and worth knowing
    when a component mysteriously never rebuilds.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "template.yaml", "Resources: {}\n")

    import hashlib

    empty_tree = hashlib.sha256(b"").hexdigest()
    assert _source_checksum("template.yaml") == empty_tree

    # A real directory with the same single file does not collapse to that value.
    _write(tmp_path, "pkg/template.yaml", "Resources: {}\n")
    assert _source_checksum("pkg") != empty_tree


def test_verbose_mode_reports_how_many_source_files_were_counted(monkeypatch, tmp_path):
    """The reported count is an independent read on the exclusion rules.

    Asserting the number, rather than that a line was printed, makes this a
    second measurement of which files count: four source files here, and six
    further files that are all excluded.
    """
    monkeypatch.chdir(tmp_path)
    for rel in ("pkg/a.py", "pkg/b.yaml", "pkg/deep/c.json", "pkg/Dockerfile"):
        _write(tmp_path, rel)
    for rel in (
        "pkg/README.md",
        "pkg/test_a.py",
        "pkg/a_test.py",
        "pkg/.hidden.py",
        "pkg/a.pyc",
        "pkg/tests/suite.py",
    ):
        _write(tmp_path, rel)

    pub = _publisher(capture=True)
    pub.verbose = True
    pub.get_source_files_checksum("pkg")

    assert "Checksummed 4 source files in pkg" in _console_text(pub)


def test_source_checksum_is_memoised_per_publisher_instance(monkeypatch, tmp_path):
    """One publisher answers from cache, so a mid-run edit is not seen.

    This is intended — a single publish must not see a directory change between
    the detection phase and the checksum-writing phase, or it would write a
    checksum that does not describe what it built. It is recorded here because
    it is also the trap that makes a naive test of any of the above pass
    vacuously.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "pkg/a.py", "A = 1\n")

    pub = _publisher()
    first = pub.get_source_files_checksum("pkg")
    _write(tmp_path, "pkg/a.py", "A = 999\n")
    assert pub.get_source_files_checksum("pkg") == first
    # A new publisher sees the edit.
    assert _source_checksum("pkg") != first


# ---------------------------------------------------------------------------
# get_component_checksum
# ---------------------------------------------------------------------------


def test_component_checksum_combines_files_and_directories(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "template.yaml", "Resources: {}\n")
    _write(tmp_path, "pkg/mod.py", "A = 1\n")

    baseline = _publisher().get_component_checksum("template.yaml", "pkg")

    _write(tmp_path, "template.yaml", "Resources: {Foo: {}}\n")
    assert _publisher().get_component_checksum("template.yaml", "pkg") != baseline

    _write(tmp_path, "template.yaml", "Resources: {}\n")
    _write(tmp_path, "pkg/mod.py", "A = 2\n")
    assert _publisher().get_component_checksum("template.yaml", "pkg") != baseline


def test_component_checksum_ignores_a_path_that_is_neither_file_nor_directory(
    monkeypatch, tmp_path
):
    """A missing path contributes nothing at all, rather than an empty marker.

    So `get_component_checksum("pkg")` and
    `get_component_checksum("pkg", "gone")` are the same value. Worth knowing
    because it means the *appearance* of a previously absent dependency is what
    changes the answer, not its absence.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "pkg/mod.py", "A = 1\n")

    with_missing = _publisher().get_component_checksum("pkg", "absent.yaml")
    without = _publisher().get_component_checksum("pkg")
    assert with_missing == without

    _write(tmp_path, "absent.yaml", "now: here\n")
    assert _publisher().get_component_checksum("pkg", "absent.yaml") != without


@pytest.mark.parametrize(
    "attribute, value",
    [
        ("bucket", "a-different-bucket"),
        ("prefix_and_version", "idp/9.9.9"),
        ("region", "us-west-2"),
    ],
)
def test_component_checksum_folds_in_the_deployment_target(
    monkeypatch, tmp_path, attribute, value
):
    """Publishing the same source to a different place must rebuild.

    The artifacts carry the bucket, prefix and region baked into template
    tokens, so reusing a build made for another target would publish templates
    pointing at the previous bucket.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "pkg/mod.py", "A = 1\n")
    baseline = _publisher().get_component_checksum("pkg")

    pub = _publisher()
    setattr(pub, attribute, value)
    assert pub.get_component_checksum("pkg") != baseline


def test_component_checksum_is_order_dependent_but_its_cache_key_is_not(
    monkeypatch, tmp_path
):
    """DEFECT: `get_component_checksum` is not a function of its arguments.

    The cache key sorts the paths (`tuple(sorted(paths))`) while the computation
    concatenates their checksums in the order given. So the same call, made
    twice with the arguments transposed, returns two different values on a cold
    cache and the *first* value both times on a warm one.

    Observable consequence: whether a component is judged up to date can depend
    on the order in which a caller happened to list its dependencies, and on
    whether some earlier call had already warmed the cache with the other
    order. Nothing in the current publisher calls it with two different orders,
    so this is latent rather than active; the test pins the behaviour so a
    future caller does not discover it the hard way.

    publish.py:3007 (cache key) vs publish.py:3017 (computation).
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "one.yaml", "one\n")
    _write(tmp_path, "two.yaml", "two\n")

    forward = _publisher().get_component_checksum("one.yaml", "two.yaml")
    reverse = _publisher().get_component_checksum("two.yaml", "one.yaml")
    assert forward != reverse, "order-dependent concatenation"

    # On one instance the sorted cache key collapses the two orders, so the
    # second call reports a value it would not have computed.
    warm = _publisher()
    assert warm.get_component_checksum("one.yaml", "two.yaml") == forward
    assert warm.get_component_checksum("two.yaml", "one.yaml") == forward


# ---------------------------------------------------------------------------
# compute_directory_hash
# ---------------------------------------------------------------------------


def test_compute_directory_hash_returns_an_eight_character_hex_digest(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    assert _publisher().compute_directory_hash("missing") == ""

    _write(tmp_path, "pkg/a.py", "A = 1\n")
    digest = _publisher().compute_directory_hash("pkg")
    assert len(digest) == 8
    assert all(c in "0123456789abcdef" for c in digest)


def test_compute_directory_hash_sees_a_rename_and_a_nested_edit(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "pkg/deep/a.py", "A = 1\n")
    baseline = _publisher().compute_directory_hash("pkg")

    _write(tmp_path, "pkg/deep/a.py", "A = 2\n")
    edited = _publisher().compute_directory_hash("pkg")
    assert edited != baseline

    (tmp_path / "pkg" / "deep" / "a.py").rename(tmp_path / "pkg" / "deep" / "b.py")
    assert _publisher().compute_directory_hash("pkg") != edited


def test_compute_directory_hash_counts_what_the_source_checksum_ignores(
    monkeypatch, tmp_path
):
    """The two hashes have deliberately different scopes, and must not converge.

    `compute_directory_hash` versions *layer content* — the bytes that end up
    inside a zip — so it walks everything, including bytecode and test files.
    `get_source_files_checksum` decides whether to *rebuild*, so it ignores
    them. This test is the one that makes the exclusion test above meaningful:
    if both functions agreed, one of the two would be measuring the wrong thing.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "pkg/mod.py", "REAL = 1\n")
    source_baseline = _source_checksum("pkg")
    content_baseline = _publisher().compute_directory_hash("pkg")

    _write(tmp_path, "pkg/__pycache__/mod.cpython-312.pyc", "bytecode\n")
    _write(tmp_path, "pkg/test_mod.py", "def test_x(): pass\n")
    _write(tmp_path, "pkg/README.md", "# docs\n")

    assert _source_checksum("pkg") == source_baseline
    assert _publisher().compute_directory_hash("pkg") != content_baseline


# ---------------------------------------------------------------------------
# get_component_dependencies / get_components_needing_rebuild
# ---------------------------------------------------------------------------


def test_the_dependency_map_covers_every_component_and_its_build_inputs():
    """Every component names its template, and the container inputs are present.

    The two `MULTI_DOC_DISCOVERY_BUILD_INPUTS` entries are the reason this
    assertion exists: a Dependabot bump to that `requirements.txt` used to leave
    the component's checksum unchanged, so the vulnerable image stayed
    published.
    """
    deps = _publisher().get_component_dependencies()
    assert set(deps) == _ALL_COMPONENTS

    assert "nested/multi-doc-discovery/Dockerfile" in deps["nested/multi-doc-discovery"]
    assert (
        "nested/multi-doc-discovery/requirements.txt"
        in deps["nested/multi-doc-discovery"]
    )
    # The handler source is a separate tree from the nested stack directory.
    assert "src/lambda/multi_doc_discovery" in deps["nested/multi-doc-discovery"]

    lib_dependents = {
        name
        for name, paths in deps.items()
        if "./lib/idp_common_pkg/idp_common" in paths or "./lib/idp_common_pkg" in paths
    }
    assert lib_dependents == _COMPONENTS_THAT_USE_LIB


def test_every_component_needs_a_rebuild_when_no_checksum_file_exists(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    _build_repo_tree(tmp_path)

    pub = _publisher(capture=True)
    items = pub.get_components_needing_rebuild()

    assert {item["component"] for item in items} == _ALL_COMPONENTS
    for item in items:
        # With no previous build, every dependency is reported as changed.
        assert item["changed_dependencies"] == item["dependencies"]
    assert "new/no previous build" in _console_text(pub)
    assert pub._is_lib_changed is True


def test_writing_checksums_makes_a_fresh_publisher_see_nothing_to_rebuild(
    monkeypatch, tmp_path
):
    """The round trip is the property the whole mechanism rests on.

    If `update_component_checksum` wrote anything other than what
    `get_components_needing_rebuild` compares against, either every run would
    rebuild everything (slow but safe) or — depending on which side drifted —
    a changed component could be judged current. A fresh publisher is used for
    the second read so the answer comes from the files, not the instance cache.
    """
    monkeypatch.chdir(tmp_path)
    _build_repo_tree(tmp_path)

    first = _publisher(capture=True)
    items = first.get_components_needing_rebuild()
    first.update_component_checksum(items)

    assert _rebuild_names() == set()


def test_the_checksum_file_records_the_combined_hash_and_each_dependency(
    monkeypatch, tmp_path
):
    """Per-dependency checksums are what lets the next run *name* what changed."""
    monkeypatch.chdir(tmp_path)
    _build_repo_tree(tmp_path)

    pub = _publisher(capture=True)
    items = pub.get_components_needing_rebuild()
    pub.update_component_checksum(items)

    bedrockkb = next(i for i in items if i["component"] == "nested/bedrockkb")
    assert bedrockkb["checksum_file"] == "nested/bedrockkb/.checksum"

    stored = json.loads((tmp_path / "nested" / "bedrockkb" / ".checksum").read_text())
    assert stored["combined"] == bedrockkb["current_checksum"]
    assert set(stored["dependencies"]) == {
        "nested/bedrockkb/src",
        "nested/bedrockkb/template.yaml",
    }
    # `main` and `lib` use the two special-cased checksum paths.
    assert (tmp_path / ".checksum").is_file()
    assert (tmp_path / "lib" / ".checksum").is_file()


def test_editing_one_component_s_source_rebuilds_only_that_component(
    monkeypatch, tmp_path
):
    """The point of the whole mechanism: an isolated edit is an isolated rebuild.

    `nested/bedrockkb` is chosen precisely because it is the one component whose
    dependency list does *not* include the shared library, so a change inside it
    cannot fan out.
    """
    monkeypatch.chdir(tmp_path)
    _build_repo_tree(tmp_path)
    seed = _publisher(capture=True)
    seed.update_component_checksum(seed.get_components_needing_rebuild())

    _write(tmp_path, "nested/bedrockkb/src/handler.py", "def handler(): return 'new'\n")

    pub = _publisher(capture=True)
    items = pub.get_components_needing_rebuild()
    assert {i["component"] for i in items} == {"nested/bedrockkb"}
    assert items[0]["changed_dependencies"] == ["nested/bedrockkb/src"]
    assert pub._is_lib_changed is False
    text = _console_text(pub)
    assert "nested/bedrockkb needs rebuild (changed)" in text


def test_editing_the_shared_library_fans_out_to_every_dependent_component(
    monkeypatch, tmp_path
):
    """One file in `idp_common` invalidates five of the seven components.

    Under-reporting here is the expensive direction: a Lambda layer built from
    stale library source is indistinguishable from a good one at deploy time.
    `_is_lib_changed` is the flag that forces the layer rebuild, so it is
    asserted alongside.
    """
    monkeypatch.chdir(tmp_path)
    _build_repo_tree(tmp_path)
    seed = _publisher(capture=True)
    seed.update_component_checksum(seed.get_components_needing_rebuild())

    _write(
        tmp_path,
        "lib/idp_common_pkg/idp_common/ocr/service.py",
        "class OcrService:\n    NEW = True\n",
    )

    pub = _publisher(capture=True)
    items = pub.get_components_needing_rebuild()
    assert {i["component"] for i in items} == _COMPONENTS_THAT_USE_LIB
    assert pub._is_lib_changed is True

    # The named dependency differs between the library component and its
    # consumers: `lib` watches the whole package, the rest watch the inner dir.
    by_name = {i["component"]: i["changed_dependencies"] for i in items}
    assert by_name["lib"] == ["./lib/idp_common_pkg"]
    assert by_name["patterns/unified"] == ["./lib/idp_common_pkg/idp_common"]


def test_a_changed_container_build_input_rebuilds_multi_doc_discovery(
    monkeypatch, tmp_path
):
    """A dependency bump in `requirements.txt` must invalidate the component.

    This is the regression the two `MULTI_DOC_DISCOVERY_BUILD_INPUTS` entries
    exist for: without them the checksum covered only the handler directory, so
    a CVE patch to the image's dependencies left the component "up to date" and
    the old image deployed.
    """
    monkeypatch.chdir(tmp_path)
    _build_repo_tree(tmp_path)
    seed = _publisher(capture=True)
    seed.update_component_checksum(seed.get_components_needing_rebuild())

    _write(tmp_path, "nested/multi-doc-discovery/requirements.txt", "boto3==1.41.0\n")

    pub = _publisher(capture=True)
    items = pub.get_components_needing_rebuild()
    assert {i["component"] for i in items} == {"nested/multi-doc-discovery"}
    assert items[0]["changed_dependencies"] == [
        "nested/multi-doc-discovery/requirements.txt"
    ]


def test_deleting_a_dependency_file_is_detected_as_a_change(monkeypatch, tmp_path):
    """A removed template must not read as "unchanged"."""
    monkeypatch.chdir(tmp_path)
    _build_repo_tree(tmp_path)
    seed = _publisher(capture=True)
    seed.update_component_checksum(seed.get_components_needing_rebuild())

    (tmp_path / "nested" / "bedrockkb" / "template.yaml").unlink()

    items = _publisher(capture=True).get_components_needing_rebuild()
    assert {i["component"] for i in items} == {"nested/bedrockkb"}
    assert items[0]["changed_dependencies"] == ["nested/bedrockkb/template.yaml"]


def test_changing_the_target_bucket_rebuilds_everything(monkeypatch, tmp_path):
    """Valid checksum files do not survive a change of publish destination."""
    monkeypatch.chdir(tmp_path)
    _build_repo_tree(tmp_path)
    seed = _publisher(capture=True)
    seed.update_component_checksum(seed.get_components_needing_rebuild())
    assert _rebuild_names() == set()

    other = _publisher(capture=True)
    other.bucket = "some-other-bucket"
    assert _rebuild_names(other) == _ALL_COMPONENTS


def test_an_unparseable_checksum_file_forces_a_rebuild_of_that_component(
    monkeypatch, tmp_path
):
    """Corruption must fail safe — rebuild — rather than be read as current."""
    monkeypatch.chdir(tmp_path)
    _build_repo_tree(tmp_path)
    seed = _publisher(capture=True)
    seed.update_component_checksum(seed.get_components_needing_rebuild())

    # Truncated mid-write: the commonest corruption shape.
    (tmp_path / "nested" / "bedrockkb" / ".checksum").write_text('{"combined": "abc')

    pub = _publisher(capture=True)
    items = pub.get_components_needing_rebuild()
    assert {i["component"] for i in items} == {"nested/bedrockkb"}
    # All dependencies are reported, because none could be compared.
    assert items[0]["changed_dependencies"] == items[0]["dependencies"]


def test_a_checksum_file_holding_valid_json_that_is_not_an_object_crashes(
    monkeypatch, tmp_path
):
    """DEFECT: the corrupt-checksum recovery path does not cover every corruption.

    The handler at publish.py:3144 catches `json.JSONDecodeError` and
    `KeyError`, with the comment "Old format or corrupted - rebuild and show all
    deps". A `.checksum` holding valid JSON that is not an object — `null`, a
    bare number, a list — decodes fine and then fails at
    `stored_data.get("combined", "")` with `AttributeError`, which nothing
    catches. The exception propagates out of `get_components_needing_rebuild`,
    out of `smart_rebuild_detection`, and is caught only by `run`'s outermost
    handler, so the publish aborts with a traceback.

    Observable consequence: one malformed byte sequence in a cache file — a file
    the publisher itself treats as disposable, and which `clean_checksums`
    exists to delete — turns a recoverable "rebuild everything" into a failed
    publish. The safe behaviour is the one the adjacent `JSONDecodeError` branch
    already implements.

    This test pins the current behaviour; it is not an endorsement of it.
    """
    monkeypatch.chdir(tmp_path)
    _build_repo_tree(tmp_path)
    seed = _publisher(capture=True)
    seed.update_component_checksum(seed.get_components_needing_rebuild())

    (tmp_path / "nested" / "bedrockkb" / ".checksum").write_text("null")

    with pytest.raises(AttributeError):
        _publisher(capture=True).get_components_needing_rebuild()


# ---------------------------------------------------------------------------
# _delete_checksum_file
# ---------------------------------------------------------------------------


def test_delete_checksum_file_accepts_either_a_directory_or_the_file_itself(
    monkeypatch, tmp_path
):
    """Both call shapes are live in `publish.py`, so both are pinned.

    `build_main_template`'s failure handler passes the file path `".checksum"`;
    other callers pass a component directory. Getting this wrong in either
    direction would leave a checksum describing a build that failed, so the
    next run would skip the rebuild.
    """
    monkeypatch.chdir(tmp_path)
    pub = _publisher()

    _write(tmp_path, ".checksum", "{}")
    pub._delete_checksum_file(".checksum")
    assert not (tmp_path / ".checksum").exists()

    _write(tmp_path, "nested/bedrockkb/.checksum", "{}")
    pub._delete_checksum_file("nested/bedrockkb")
    assert not (tmp_path / "nested" / "bedrockkb" / ".checksum").exists()
    # The directory itself survives.
    assert (tmp_path / "nested" / "bedrockkb").is_dir()

    # Absent is a no-op, not an error.
    pub._delete_checksum_file("nested/bedrockkb")
    pub._delete_checksum_file("never/existed/.checksum")


# ---------------------------------------------------------------------------
# clear_component_cache
# ---------------------------------------------------------------------------


def test_clearing_the_main_cache_preserves_the_lambda_layer_zips(monkeypatch, tmp_path):
    """`.aws-sam/layers` must survive, because rebuilding a layer costs minutes.

    `main`'s SAM build output and the layer zips share the `.aws-sam` parent, so
    the naive `rmtree(".aws-sam")` would delete the layers on every main
    rebuild. A failure here means every publish pays for a full layer
    reinstall — and, worse, the layer-discovery path would find nothing and the
    run would silently change shape.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, ".aws-sam/build/template.yaml", "Resources: {}\n")
    _write(tmp_path, ".aws-sam/layers/idp-common-base-abcd1234.zip", "zip bytes\n")
    _write(tmp_path, ".aws-sam/idp-main.yaml", "Resources: {}\n")

    _publisher().clear_component_cache("main")

    assert not (tmp_path / ".aws-sam" / "build").exists()
    assert (tmp_path / ".aws-sam" / "layers" / "idp-common-base-abcd1234.zip").is_file()
    assert (tmp_path / ".aws-sam" / "idp-main.yaml").is_file()


def test_clearing_a_nested_component_cache_removes_its_whole_aws_sam_tree(
    monkeypatch, tmp_path
):
    """A nested component owns its `.aws-sam` outright, so all of it goes."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "nested/bedrockkb/.aws-sam/build/template.yaml", "Resources: {}\n")
    _write(tmp_path, "nested/bedrockkb/.aws-sam/packaged.yaml", "Resources: {}\n")
    _write(tmp_path, "nested/bedrockkb/template.yaml", "Resources: {}\n")

    _publisher().clear_component_cache("nested/bedrockkb")

    assert not (tmp_path / "nested" / "bedrockkb" / ".aws-sam").exists()
    # The source template is untouched.
    assert (tmp_path / "nested" / "bedrockkb" / "template.yaml").is_file()


def test_a_cache_clear_that_cannot_delete_the_main_build_dir_warns_and_continues(
    monkeypatch, tmp_path
):
    """A failed cache clear must not abort the publish.

    SAM will overwrite most of what is left behind, so continuing is the right
    call; the warning is the only trace, which is why it is asserted rather than
    just the absence of an exception.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, ".aws-sam/build/template.yaml", "Resources: {}\n")
    monkeypatch.setattr(publish_mod, "shutil", _UndeletableShutil())

    pub = _publisher(capture=True)
    pub.verbose = True
    pub.clear_component_cache("main")

    assert "Error clearing SAM cache" in _console_text(pub)
    assert (tmp_path / ".aws-sam" / "build").exists()


def test_a_failed_nested_cache_clear_falls_back_to_rm_minus_rf(monkeypatch, tmp_path):
    """The fallback exists for broken symlinks, which `shutil.rmtree` refuses.

    A nested component's `.aws-sam` can contain symlinks into a Docker build
    context that no longer resolves; `rm -rf` removes those where `rmtree`
    raises. The argv is asserted because the whole content of the fallback is
    which path it is pointed at — clearing the wrong one would delete a
    component's source.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "patterns/unified/.aws-sam/packaged.yaml", "Resources: {}\n")
    monkeypatch.setattr(publish_mod, "shutil", _UndeletableShutil())
    recorder = _RecordingSubprocess()
    monkeypatch.setattr(publish_mod, "subprocess", recorder)

    pub = _publisher(capture=True)
    pub.verbose = True
    pub.clear_component_cache("patterns/unified")

    assert recorder.calls == [
        ["rm", "-rf", os.path.join("patterns/unified", ".aws-sam")]
    ]
    assert "may already be deleted" in _console_text(pub)


def test_the_cache_clear_fallback_failing_too_is_logged_rather_than_raised(
    monkeypatch, tmp_path
):
    """Both deletion routes failing still must not stop the publish."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "patterns/unified/.aws-sam/packaged.yaml", "Resources: {}\n")
    monkeypatch.setattr(publish_mod, "shutil", _UndeletableShutil())

    class _BrokenSubprocess(_RecordingSubprocess):
        def run(self, cmd, **kwargs):
            raise FileNotFoundError("rm: command not found")

    monkeypatch.setattr(publish_mod, "subprocess", _BrokenSubprocess())

    pub = _publisher(capture=True)
    pub.verbose = True
    pub.clear_component_cache("patterns/unified")

    assert "Alternative cleanup also failed" in _console_text(pub)


def test_clearing_a_cache_that_does_not_exist_is_a_no_op(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    pub = _publisher()
    pub.clear_component_cache("main")
    pub.clear_component_cache("patterns/unified")
    assert not (tmp_path / ".aws-sam").exists()


# ---------------------------------------------------------------------------
# smart_rebuild_detection
# ---------------------------------------------------------------------------


def test_smart_rebuild_detection_forces_a_lib_rebuild_when_layer_zips_are_gone(
    monkeypatch, tmp_path
):
    """A current `lib/.checksum` is not evidence that the layer zips still exist.

    Somebody deleting `.aws-sam/` by hand, or a CI cache miss, leaves the
    checksum files in place and the zips absent. Without this check the run
    would skip the layer build and then have no zip to upload or to name in the
    template tokens.
    """
    monkeypatch.chdir(tmp_path)
    _build_repo_tree(tmp_path)
    seed = _publisher(capture=True)
    seed.update_component_checksum(seed.get_components_needing_rebuild())

    pub = _publisher(capture=True)
    assert pub._is_lib_changed is False
    result = pub.smart_rebuild_detection()

    assert pub._is_lib_changed is True
    text = _console_text(pub)
    assert "Layers directory missing" in text
    # Nothing in the *source* changed, so the only work is the layer rebuild
    # plus the packaged-template backfill below.
    assert "main" not in {i["component"] for i in result}


def test_smart_rebuild_detection_reports_nothing_when_the_tree_is_fully_current(
    monkeypatch, tmp_path
):
    """The all-clear path: checksums current, layer zips present, templates packaged.

    All three safety checks have to agree for this to return an empty list, so
    it is the one case that proves they are not each unconditionally forcing a
    rebuild.
    """
    monkeypatch.chdir(tmp_path)
    _build_repo_tree(tmp_path)
    for layer in ("base", "reporting", "agents", "multi_document_discovery"):
        _write(tmp_path, f".aws-sam/layers/idp-common-{layer}-abcd1234.zip", "zip\n")
    for component in _ALL_COMPONENTS - {"main", "lib"}:
        _write(tmp_path, f"{component}/.aws-sam/packaged.yaml", "Resources: {}\n")

    seed = _publisher(capture=True)
    seed.update_component_checksum(seed.get_components_needing_rebuild())

    pub = _publisher(capture=True)
    assert pub.smart_rebuild_detection() == []
    assert "No components need rebuilding" in _console_text(pub)
    assert pub._is_lib_changed is False


def test_smart_rebuild_detection_backfills_components_whose_package_is_missing(
    monkeypatch, tmp_path
):
    """A current checksum plus a missing `packaged.yaml` still means rebuild.

    The parent template references `<component>/.aws-sam/packaged.yaml` by URL,
    so a component judged current whose packaged template was deleted produces a
    main template pointing at a nested stack that was never uploaded.
    """
    monkeypatch.chdir(tmp_path)
    _build_repo_tree(tmp_path)
    for layer in ("base", "reporting", "agents", "multi_document_discovery"):
        _write(tmp_path, f".aws-sam/layers/idp-common-{layer}-abcd1234.zip", "zip\n")
    # Every component except bedrockkb has its packaged template.
    for component in _ALL_COMPONENTS - {"main", "lib", "nested/bedrockkb"}:
        _write(tmp_path, f"{component}/.aws-sam/packaged.yaml", "Resources: {}\n")

    seed = _publisher(capture=True)
    seed.update_component_checksum(seed.get_components_needing_rebuild())

    pub = _publisher(capture=True)
    result = pub.smart_rebuild_detection()

    assert {i["component"] for i in result} == {"nested/bedrockkb"}
    assert result[0]["changed_dependencies"] == ["packaged.yaml missing"]
    assert "packaged.yaml missing - forcing rebuild" in _console_text(pub)
