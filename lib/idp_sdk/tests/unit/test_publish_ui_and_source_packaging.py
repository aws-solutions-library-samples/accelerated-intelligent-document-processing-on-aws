# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the UI and Docker-source packaging half of ``IDPPublisher``.

``publish.py`` builds three zip artifacts and uploads each to the artifacts
bucket: the Web UI source (``src-<hash>.zip``, consumed by the stack's CodeBuild
project that builds and deploys the React app), the unified-pattern source
(``unified-source-<hash>.zip``, the Docker build context for all the pattern
Lambda images), and the multi-doc-discovery source. All three share one shape —
name the artifact after a content hash, skip the work when the name already
exists locally or in S3, upload when it does not — so all three are only correct
if the hash actually moves when the content moves, and if the zip's member list
matches what the consumer expects to find.

What shaped these tests is that every failure in this area is silent and
delayed. A zip whose name does not change ships last publish's code; an
over-inclusive walk puts a developer's virtualenv (host-architecture wheels) in
a linux image's build context; an under-inclusive one drops a file CodeBuild
needs and the failure surfaces minutes later inside a container build. So the
assertions here are on the zip's exact member list, on the exact S3 key and
bytes read back out of ``moto``, and on whether a mutation to a given file does
or does not move the artifact name.

Three tests pin behaviour that is currently wrong; each says so in its own
docstring, and none of them is a request to change production code here.
"""

from __future__ import annotations

import concurrent.futures
import os
import zipfile

import boto3
import pytest
from moto import mock_aws

from idp_sdk._core.publish import IDPPublisher

_BUCKET = "idp-publish-artifacts"
_PREFIX_AND_VERSION = "idp/0.6.9"


def _publisher(s3_client=None):
    """A publisher wired for artifact upload, with console output silenced."""
    pub = IDPPublisher(verbose=False)
    pub.bucket = _BUCKET
    pub.prefix = "idp"
    pub.version = "0.6.9"
    pub.prefix_and_version = _PREFIX_AND_VERSION
    pub.region = "us-east-1"
    pub.s3_client = s3_client
    pub.console.quiet = True
    return pub


def _write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _ui_tree(root):
    """A src/ui tree whose shape distinguishes a correct walk from a wrong one.

    Deliberately not degenerate: excluded directories appear both at the top of
    the tree and nested one level down, the excluded dotenv files sit beside
    dotfiles that are *not* excluded, and the included files are nested two deep
    so a walk that only reads the top level fails.
    """
    ui = root / "src" / "ui"
    _write(ui / "package.json", '{"name":"ui"}')
    _write(ui / "vite.config.ts", "export default {}")
    _write(ui / "src" / "App.tsx", "export const App = 1;")
    _write(ui / "src" / "components" / "Nav.tsx", "export const Nav = 2;")
    _write(ui / "public" / "favicon.svg", "<svg/>")
    # Excluded by name at the top level ...
    _write(ui / "node_modules" / "react" / "index.js", "module.exports = 0;")
    _write(ui / "build" / "assets" / "app.js", "built")
    _write(ui / ".aws-sam" / "stale.json", "{}")
    # ... and excluded when nested, because the filter runs at every level.
    _write(ui / "src" / "node_modules" / "vendored" / "dep.js", "dep")
    # Excluded dotenv files.
    _write(ui / ".env", "VITE_X=1")
    _write(ui / ".env.production", "VITE_X=2")
    # A dotfile the exclusion rule does NOT cover (see the dedicated test).
    _write(ui / ".envrc", "export EXAMPLE_VALUE=1")
    return ui


def _zip_names(path):
    with zipfile.ZipFile(path) as zf:
        return sorted(zf.namelist())


# ---------------------------------------------------------------------------
# compute_ui_hash / ui_changed
# ---------------------------------------------------------------------------


def test_compute_ui_hash_is_stable_and_moves_with_a_nested_edit(tmp_path, monkeypatch):
    """The hash names the artifact, so it must track every shipped file.

    A hash that does not move for an edit two directories down leaves
    ``ui_changed`` answering False and ``package_ui`` re-uploading the previous
    bundle — the stale-UI failure the hash exists to prevent.
    """
    monkeypatch.chdir(tmp_path)
    ui = _ui_tree(tmp_path)
    pub = _publisher()

    first = pub.compute_ui_hash()
    assert first == pub.compute_ui_hash(), "the hash churns with no change"

    (ui / "src" / "components" / "Nav.tsx").write_text("export const Nav = 99;")
    assert _publisher().compute_ui_hash() != first


def test_compute_ui_hash_ignores_node_modules_and_logs(tmp_path, monkeypatch):
    """Installed dependencies and log files must not force a UI rebuild.

    ``node_modules`` is not shipped in the zip, so letting it move the hash
    would make every ``npm install`` look like a UI change and rebuild the UI on
    every publish.
    """
    monkeypatch.chdir(tmp_path)
    ui = _ui_tree(tmp_path)
    before = _publisher().compute_ui_hash()

    (ui / "node_modules" / "react" / "index.js").write_text("module.exports = 1;")
    (ui / "build" / "assets" / "app.js").write_text("rebuilt")
    _write(ui / "npm-debug.log", "noise")

    assert _publisher().compute_ui_hash() == before


def test_a_file_rename_does_not_change_the_ui_hash(tmp_path, monkeypatch):
    """DEFECT pinned: renaming a UI file leaves the hash — and the zip — stale.

    ``get_directory_checksum`` (publish.py:738-755, outside this cluster's
    range) concatenates file *contents* only; it never mixes the relative path
    into the digest, unlike ``get_source_files_checksum`` which hashes
    ``"<relpath>:<checksum>"``. So a change that only moves or renames files
    with identical contents produces an identical hash.

    The observable consequence is here, in this cluster: ``ui_changed`` reports
    False, ``package_ui`` finds the existing ``src-<hash>.zip`` and neither
    rebuilds it nor re-uploads it, so the published bundle still contains the
    OLD filename. A UI refactor that only renames components ships the previous
    UI. This test pins the current (wrong) answer; it is not a fix.
    """
    monkeypatch.chdir(tmp_path)
    ui = tmp_path / "src" / "ui"
    _write(ui / "package.json", '{"name":"ui"}')
    _write(ui / "src" / "Widget.tsx", "export const W = 1;")

    before = _publisher().compute_ui_hash()
    changed, zip_path = _publisher().ui_changed()
    assert changed is True
    os.makedirs(".aws-sam", exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("src/Widget.tsx", "export const W = 1;")

    # A pure rename: same bytes, same sort position, different name.
    (ui / "src" / "Widget.tsx").rename(ui / "src" / "Panel.tsx")

    assert _publisher().compute_ui_hash() == before
    changed_after, path_after = _publisher().ui_changed()
    assert changed_after is False
    assert path_after == zip_path
    # The artifact that would be published still names the file that is gone.
    assert _zip_names(zip_path) == ["src/Widget.tsx"]


def test_ui_changed_names_the_zip_after_the_first_16_hash_chars(tmp_path, monkeypatch):
    """The artifact name is ``.aws-sam/src-<first 16 of the hash>.zip``.

    The stack's CodeBuild project is handed this name, so a change to the
    truncation length or the prefix silently breaks the handoff.
    """
    monkeypatch.chdir(tmp_path)
    _ui_tree(tmp_path)
    pub = _publisher()

    ui_hash = pub.compute_ui_hash()
    changed, zip_path = pub.ui_changed()

    assert changed is True
    assert zip_path == os.path.join(".aws-sam", f"src-{ui_hash[:16]}.zip")
    assert len(ui_hash) == 64


def test_ui_changed_deletes_stale_src_zips_and_leaves_other_files(
    tmp_path, monkeypatch
):
    """A hash miss garbage-collects previous UI bundles, and only those.

    ``.aws-sam`` also holds the unified-source zip and SAM's own build output, so
    a cleanup that matched too broadly would delete another artifact mid-publish.
    """
    monkeypatch.chdir(tmp_path)
    _ui_tree(tmp_path)
    sam = tmp_path / ".aws-sam"
    sam.mkdir()
    (sam / "src-0000000000000000.zip").write_bytes(b"old")
    (sam / "src-1111111111111111.zip").write_bytes(b"older")
    (sam / "unified-source-abcd1234.zip").write_bytes(b"other artifact")
    (sam / "src-notazip.txt").write_text("keep")

    changed, zip_path = _publisher().ui_changed()

    assert changed is True
    assert not (sam / "src-0000000000000000.zip").exists()
    assert not (sam / "src-1111111111111111.zip").exists()
    assert (sam / "unified-source-abcd1234.zip").exists()
    assert (sam / "src-notazip.txt").exists()
    assert not os.path.exists(zip_path), "the new zip is not created by ui_changed"


def test_ui_changed_reports_unchanged_and_keeps_stale_siblings(tmp_path, monkeypatch):
    """On a hash hit nothing is deleted — including bundles from older hashes.

    The cleanup lives only on the miss branch, so previous ``src-*.zip`` files
    accumulate in ``.aws-sam`` for as long as the UI is unchanged. That is disk
    housekeeping rather than a correctness problem, but it is the behaviour, and
    a reader of ``ui_changed`` would guess the opposite.
    """
    monkeypatch.chdir(tmp_path)
    _ui_tree(tmp_path)
    pub = _publisher()
    _, zip_path = pub.ui_changed()
    os.makedirs(".aws-sam", exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("package.json", "{}")
    stale = tmp_path / ".aws-sam" / "src-2222222222222222.zip"
    stale.write_bytes(b"previous")

    changed, path_again = _publisher().ui_changed()

    assert changed is False
    assert path_again == zip_path
    assert stale.exists()


def test_ui_changed_treats_a_listed_but_missing_zip_as_changed(tmp_path, monkeypatch):
    """With no ``.aws-sam`` at all the answer is "changed", with no crash.

    ``os.listdir`` is guarded by an existence check, so a first-ever publish (no
    build directory yet) must not raise.
    """
    monkeypatch.chdir(tmp_path)
    _ui_tree(tmp_path)
    assert not os.path.exists(".aws-sam")

    changed, zip_path = _publisher().ui_changed()

    assert changed is True
    assert zip_path.startswith(".aws-sam" + os.sep)


# ---------------------------------------------------------------------------
# start_ui_validation_parallel
# ---------------------------------------------------------------------------


def test_no_validation_thread_when_lint_is_disabled(tmp_path, monkeypatch):
    """``--no-lint`` must not start a thread, or it would run npm anyway."""
    monkeypatch.chdir(tmp_path)
    _ui_tree(tmp_path)
    pub = _publisher()
    pub.lint_enabled = False
    pub.validate_ui_build = lambda: pytest.fail("validation ran with lint disabled")

    assert pub.start_ui_validation_parallel() == (None, None)


def test_no_validation_thread_without_a_ui_directory(tmp_path, monkeypatch):
    """A checkout with no ``src/ui`` skips validation instead of failing."""
    monkeypatch.chdir(tmp_path)
    pub = _publisher()
    pub.validate_ui_build = lambda: pytest.fail("validation ran with no src/ui")

    assert pub.start_ui_validation_parallel() == (None, None)


def test_no_validation_thread_when_the_ui_is_unchanged(tmp_path, monkeypatch):
    """An unchanged UI skips the expensive ``npm ci`` + ``npm run build``.

    This is the cache that makes a no-UI-change publish fast, so it is the one
    that most needs to be wrong-way-safe: it keys off the same ``ui_changed``
    the packaging step uses, exercised here for real rather than stubbed.
    """
    monkeypatch.chdir(tmp_path)
    _ui_tree(tmp_path)
    pub = _publisher()
    _, zip_path = pub.ui_changed()
    os.makedirs(".aws-sam", exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("package.json", "{}")
    pub.validate_ui_build = lambda: pytest.fail("validation ran for an unchanged UI")

    assert pub.start_ui_validation_parallel() == (None, None)


def test_a_changed_ui_is_validated_on_a_worker_thread(tmp_path, monkeypatch):
    """A changed UI hands ``validate_ui_build`` to a single-worker executor.

    The future is what the caller later blocks on before packaging, so it must
    actually carry the validation's result and not, say, None.
    """
    monkeypatch.chdir(tmp_path)
    _ui_tree(tmp_path)
    pub = _publisher()
    pub.validate_ui_build = lambda: "validated"

    future, executor = pub.start_ui_validation_parallel()
    try:
        assert isinstance(future, concurrent.futures.Future)
        assert isinstance(executor, concurrent.futures.ThreadPoolExecutor)
        assert future.result(timeout=30) == "validated"
    finally:
        if executor is not None:
            executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# validate_ui_build
# ---------------------------------------------------------------------------


def _recording_runner(pub, results):
    """Replace ``run_subprocess_with_logging``, recording every invocation."""
    calls = []

    def fake(cmd, component_name, cwd=None, realtime=False):
        calls.append(
            {
                "cmd": list(cmd),
                "component": component_name,
                "cwd": cwd,
                "realtime": realtime,
            }
        )
        return results.pop(0)

    pub.run_subprocess_with_logging = fake
    return calls


def test_validate_ui_build_runs_npm_ci_then_the_build_in_the_ui_dir(
    tmp_path, monkeypatch
):
    """Order and working directory are the contract, not just "npm ran".

    ``npm run build`` is what surfaces an ESLint or Prettier failure, and it can
    only run after a clean install from the lock file; running it from the repo
    root instead of ``src/ui`` would pick up the wrong ``package.json``.
    """
    monkeypatch.chdir(tmp_path)
    _ui_tree(tmp_path)
    pub = _publisher()
    calls = _recording_runner(pub, [(True, "ok"), (True, "ok")])

    assert pub.validate_ui_build() is None
    assert [c["cmd"] for c in calls] == [["npm", "ci"], ["npm", "run", "build"]]
    assert [c["cwd"] for c in calls] == ["src/ui", "src/ui"]
    assert all(c["realtime"] is True for c in calls)


def test_validate_ui_build_skips_silently_without_a_ui_directory(tmp_path, monkeypatch):
    """No ``src/ui`` is a skip, not an error — and runs no subprocess."""
    monkeypatch.chdir(tmp_path)
    pub = _publisher()
    calls = _recording_runner(pub, [])

    assert pub.validate_ui_build() is None
    assert calls == []


def test_a_failed_npm_ci_aborts_publish_before_building(tmp_path, monkeypatch):
    """Install failure must exit 1 and not go on to run the build.

    Running the build against a half-installed ``node_modules`` produces a
    confusing second failure that hides the real one.
    """
    monkeypatch.chdir(tmp_path)
    _ui_tree(tmp_path)
    pub = _publisher()
    calls = _recording_runner(pub, [(False, "npm ci exploded")])

    with pytest.raises(SystemExit) as exc:
        pub.validate_ui_build()

    assert exc.value.code == 1
    assert [c["cmd"] for c in calls] == [["npm", "ci"]]


def test_a_failed_ui_build_aborts_publish(tmp_path, monkeypatch):
    """An ESLint/Prettier failure must stop publish, not ship the bundle."""
    monkeypatch.chdir(tmp_path)
    _ui_tree(tmp_path)
    pub = _publisher()
    calls = _recording_runner(pub, [(True, "ok"), (False, "eslint: 3 problems")])

    with pytest.raises(SystemExit) as exc:
        pub.validate_ui_build()

    assert exc.value.code == 1
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# package_ui
# ---------------------------------------------------------------------------


@mock_aws
def test_package_ui_zips_the_right_members_and_uploads_them(
    tmp_path, monkeypatch, aws_credentials
):
    """The zip is the UI source tree minus dependencies, build output and dotenv.

    The member names are relative to ``src/ui`` (not to the repo root) because
    CodeBuild unpacks the zip and runs ``npm ci`` at its root. Asserting the
    exact list is what catches both directions of error: a missing source file
    breaks the build, and an included ``node_modules`` turns a ~1 MB artifact
    into hundreds of MB of host-platform binaries.
    """
    monkeypatch.chdir(tmp_path)
    _ui_tree(tmp_path)
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=_BUCKET)
    pub = _publisher(s3)

    zipfile_name = pub.package_ui()

    assert zipfile_name.startswith("src-") and zipfile_name.endswith(".zip")
    local = os.path.join(".aws-sam", zipfile_name)
    assert _zip_names(local) == [
        ".envrc",
        "package.json",
        "public/favicon.svg",
        "src/App.tsx",
        "src/components/Nav.tsx",
        "vite.config.ts",
    ]

    key = f"{_PREFIX_AND_VERSION}/{zipfile_name}"
    body = s3.get_object(Bucket=_BUCKET, Key=key)["Body"].read()
    with open(local, "rb") as fh:
        assert body == fh.read(), "the uploaded object is not the zip on disk"


@mock_aws
def test_package_ui_ships_an_envrc(tmp_path, monkeypatch, aws_credentials):
    """DEFECT pinned: the dotenv exclusion does not cover ``.envrc``.

    The filter is ``file == ".env" or file.startswith(".env.")``. A direnv
    ``.envrc`` matches neither — the name has no dot after ``env`` — so it is
    packaged and uploaded to the artifacts bucket along with the UI source. On a
    public publish that bucket's objects are world-readable, and ``.envrc`` is a
    shell script commonly used to export credentials into a developer's shell.

    Nothing in the repository creates ``src/ui/.envrc``, so this is a latent
    hazard rather than a live leak, which is why the test pins the behaviour
    instead of asserting the safe answer. The same reasoning covers
    ``.env.local`` (correctly excluded) versus ``.env-local`` (not).
    """
    monkeypatch.chdir(tmp_path)
    ui = _ui_tree(tmp_path)
    _write(ui / ".env-local", "VITE_X=3")
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=_BUCKET)
    pub = _publisher(s3)

    names = _zip_names(os.path.join(".aws-sam", pub.package_ui()))

    assert ".env" not in names
    assert ".env.production" not in names
    assert ".envrc" in names
    assert ".env-local" in names


@mock_aws
def test_package_ui_leaves_an_object_that_already_exists_untouched(
    tmp_path, monkeypatch, aws_credentials
):
    """A content-addressed key that already exists is never re-uploaded.

    Read back a sentinel body to prove the skip: asserting only that no
    exception was raised would pass even if the object had been overwritten.
    """
    monkeypatch.chdir(tmp_path)
    _ui_tree(tmp_path)
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=_BUCKET)
    pub = _publisher(s3)

    _, zip_path = pub.ui_changed()
    key = f"{_PREFIX_AND_VERSION}/{os.path.basename(zip_path)}"
    s3.put_object(Bucket=_BUCKET, Key=key, Body=b"previously uploaded")

    assert pub.package_ui() == os.path.basename(zip_path)
    assert (
        s3.get_object(Bucket=_BUCKET, Key=key)["Body"].read() == b"previously uploaded"
    )


@mock_aws
def test_package_ui_reuses_a_zip_that_is_already_on_disk(
    tmp_path, monkeypatch, aws_credentials
):
    """An existing local zip is uploaded as-is, not regenerated.

    This is the other half of the staleness story above: because the name is
    derived from the hash, whatever is already at that path is trusted to be its
    content.
    """
    monkeypatch.chdir(tmp_path)
    _ui_tree(tmp_path)
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=_BUCKET)
    pub = _publisher(s3)

    _, zip_path = pub.ui_changed()
    os.makedirs(".aws-sam", exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("only-member.txt", "hand made")

    name = pub.package_ui()

    assert _zip_names(zip_path) == ["only-member.txt"]
    uploaded = s3.get_object(Bucket=_BUCKET, Key=f"{_PREFIX_AND_VERSION}/{name}")
    assert b"only-member.txt" in uploaded["Body"].read()


@mock_aws
def test_package_ui_aborts_when_the_existence_check_itself_fails(
    tmp_path, monkeypatch, aws_credentials
):
    """A non-404 error from ``head_object`` must stop publish.

    Reached here with a real ``moto`` client pointed at a bucket that does not
    exist, which answers ``NoSuchBucket`` rather than ``404`` — the same shape a
    misconfigured bucket name or a denied ``s3:ListBucket`` produces in
    production. Continuing past it would upload nothing and report success.
    """
    monkeypatch.chdir(tmp_path)
    _ui_tree(tmp_path)
    pub = _publisher(boto3.client("s3", region_name="us-east-1"))

    with pytest.raises(SystemExit) as exc:
        pub.package_ui()

    assert exc.value.code == 1


@mock_aws
def test_package_ui_without_a_ui_directory_publishes_an_empty_bundle(
    tmp_path, monkeypatch, aws_credentials
):
    """DEFECT pinned: a checkout with no ``src/ui`` publishes ``src-.zip``.

    ``compute_ui_hash`` returns the empty string for a missing directory, so the
    artifact is named ``src-.zip`` (the truncation of an empty hash) and the
    walk over the absent directory contributes no members. Unlike
    ``start_ui_validation_parallel`` and ``validate_ui_build``, ``package_ui``
    has no existence guard, so instead of refusing it uploads an empty zip under
    a name that is the same for every such build — and the stack's CodeBuild
    project then deploys an empty UI.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=_BUCKET)
    pub = _publisher(s3)

    name = pub.package_ui()

    assert name == "src-.zip"
    assert _zip_names(os.path.join(".aws-sam", name)) == []
    assert s3.head_object(Bucket=_BUCKET, Key=f"{_PREFIX_AND_VERSION}/{name}")


# ---------------------------------------------------------------------------
# package_unified_source
# ---------------------------------------------------------------------------


def _unified_tree(root):
    """A repo-shaped tree for the unified-pattern Docker source zip."""
    _write(root / "Dockerfile.optimized", "FROM public.ecr.aws/lambda/python:3.12\n")
    _write(root / "patterns" / "unified" / "buildspec.yml", "version: 0.2\n")

    lib = root / "lib" / "idp_common_pkg"
    _write(lib / "pyproject.toml", "[project]\nname='idp_common'\n")
    _write(lib / "idp_common" / "__init__.py", "VERSION = 1\n")
    _write(lib / "idp_common" / "ocr" / "service.py", "def run(): pass\n")
    _write(lib / "idp_common" / "compiled.pyc", "bytecode")
    _write(lib / "__pycache__" / "cached.pyc", "bytecode")
    _write(lib / "dist" / "idp_common-1.0.whl", "wheel")
    _write(lib / "build" / "lib" / "copy.py", "copy")

    src = root / "patterns" / "unified" / "src"
    _write(src / "ocr_function" / "index.py", "def handler(e, c): return 1\n")
    _write(src / "ocr_function" / "requirements.txt", "boto3\n")
    _write(src / "ocr_function" / "__pycache__" / "index.pyc", "bytecode")
    # A compiled artifact NOT inside __pycache__. Two independent filters have to
    # hold for this tree to be clean — the directory filter and the per-file
    # suffix test — and only the first is exercised by a `__pycache__` alone.
    _write(src / "ocr_function" / "index.pyo", "bytecode")
    _write(src / ".aws-sam" / "build" / "leftover.py", "leftover")
    return root


@mock_aws
def test_unified_source_zip_members_are_repo_root_relative(
    tmp_path, monkeypatch, aws_credentials
):
    """Members are paths from the repo root, and build leftovers are dropped.

    The CodeBuild buildspec runs ``docker build`` with the zip root as its
    context and COPYs ``lib/idp_common_pkg`` and ``patterns/unified/src``, so a
    member stored relative to anything other than the repo root cannot be found.
    """
    monkeypatch.chdir(tmp_path)
    _unified_tree(tmp_path)
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=_BUCKET)
    pub = _publisher(s3)

    name = pub.package_unified_source()
    names = _zip_names(os.path.join(".aws-sam", name))

    assert names == [
        "Dockerfile.optimized",
        "lib/idp_common_pkg/idp_common/__init__.py",
        "lib/idp_common_pkg/idp_common/ocr/service.py",
        "lib/idp_common_pkg/pyproject.toml",
        "patterns/unified/buildspec.yml",
        "patterns/unified/src/ocr_function/index.py",
        "patterns/unified/src/ocr_function/requirements.txt",
    ]


@mock_aws
def test_unified_source_zip_carries_a_content_hash_in_its_name(
    tmp_path, monkeypatch, aws_credentials
):
    """Editing a Lambda handler must produce a differently-named artifact.

    The name is the only thing CloudFormation and the S3 skip-check compare, so
    a name that does not move means CodeBuild reuses the previous image and the
    handler change never reaches production.
    """
    monkeypatch.chdir(tmp_path)
    _unified_tree(tmp_path)
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=_BUCKET)

    first = _publisher(s3).package_unified_source()
    assert first.startswith("unified-source-") and first.endswith(".zip")
    assert len(first) == len("unified-source-") + 8 + len(".zip")

    (
        tmp_path / "patterns" / "unified" / "src" / "ocr_function" / "index.py"
    ).write_text("def handler(e, c): return 2\n")
    # A fresh publisher: the checksum helpers memoize per instance.
    second = _publisher(s3).package_unified_source()

    assert second != first
    for name in (first, second):
        assert s3.head_object(Bucket=_BUCKET, Key=f"{_PREFIX_AND_VERSION}/{name}")


@mock_aws
def test_unified_source_zip_name_ignores_a_non_source_extension_edit(
    tmp_path, monkeypatch, aws_credentials
):
    """DEFECT pinned: a data file is zipped but does not move the zip's name.

    The name comes from ``get_component_checksum`` → ``get_source_files_checksum``,
    which only digests a fixed allowlist of extensions (``.py``, ``.yaml``,
    ``.json``, ``.txt``, ...). The zip walk excludes only ``.pyc``/``.pyo``, so a
    ``.csv`` (or ``.j2``, ``.md``, ``.html``) under ``lib/idp_common_pkg`` *is*
    packaged while being invisible to the hash.

    The consequence needs a zip already on disk, which is the normal state of a
    developer's ``.aws-sam``: the name is unchanged, so the existing zip is
    reused verbatim and the edited file never reaches the image. CI, which
    starts clean, always regenerates — so this only bites locally, and silently.
    """
    monkeypatch.chdir(tmp_path)
    _unified_tree(tmp_path)
    data = tmp_path / "lib" / "idp_common_pkg" / "idp_common" / "lookup.csv"
    _write(data, "code,label\n1,one\n")
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=_BUCKET)

    first = _publisher(s3).package_unified_source()
    with zipfile.ZipFile(os.path.join(".aws-sam", first)) as zf:
        assert (
            zf.read("lib/idp_common_pkg/idp_common/lookup.csv").decode()
            == "code,label\n1,one\n"
        )

    data.write_text("code,label\n2,two\n", encoding="utf-8")
    second = _publisher(s3).package_unified_source()

    assert second == first, "the hash unexpectedly noticed the .csv edit"
    # Same name, so the stale zip on disk is reused and still holds the old row.
    with zipfile.ZipFile(os.path.join(".aws-sam", second)) as zf:
        assert (
            zf.read("lib/idp_common_pkg/idp_common/lookup.csv").decode()
            == "code,label\n1,one\n"
        )


@mock_aws
def test_unified_source_zip_includes_a_virtualenv_and_egg_info(
    tmp_path, monkeypatch, aws_credentials
):
    """DEFECT pinned: the exclusions fixed for multi-doc discovery are missing here.

    ``package_multi_doc_discovery_source`` excludes ``.venv``/``venv``/``.tox``
    and tests ``d.endswith(".egg-info")``, with a comment recording that a
    developer's virtualenv made that archive 224 MB of host-architecture wheels
    and that the ``"*.egg-info"`` glob in a set-membership test never matched
    anything. ``package_unified_source`` (publish.py:1237-1248) still carries the
    unfixed version of both: its exclusion set is ``{"__pycache__",
    ".pytest_cache", "dist", "build", "*.egg-info"}`` with no suffix test and no
    virtualenv names.

    So a publish from a checkout that has ever run ``make dev`` in
    ``lib/idp_common_pkg`` ships the venv and the egg-info metadata into the
    build context of every unified-pattern Lambda image. The same fix applied to
    the sibling function is what this needs.
    """
    monkeypatch.chdir(tmp_path)
    _unified_tree(tmp_path)
    lib = tmp_path / "lib" / "idp_common_pkg"
    _write(lib / ".venv" / "lib" / "site-packages" / "numpy" / "core.py", "native")
    _write(lib / "idp_common.egg-info" / "PKG-INFO", "Metadata-Version: 2.1")
    _write(lib / ".tox" / "py312" / "marker", "tox")
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=_BUCKET)

    names = _zip_names(
        os.path.join(".aws-sam", _publisher(s3).package_unified_source())
    )

    assert "lib/idp_common_pkg/.venv/lib/site-packages/numpy/core.py" in names
    assert "lib/idp_common_pkg/idp_common.egg-info/PKG-INFO" in names
    assert "lib/idp_common_pkg/.tox/py312/marker" in names


@mock_aws
def test_unified_source_skips_upload_when_the_key_exists(
    tmp_path, monkeypatch, aws_credentials
):
    """An existing content-addressed key is left alone."""
    monkeypatch.chdir(tmp_path)
    _unified_tree(tmp_path)
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=_BUCKET)
    pub = _publisher(s3)

    name = pub.package_unified_source()
    key = f"{_PREFIX_AND_VERSION}/{name}"
    s3.put_object(Bucket=_BUCKET, Key=key, Body=b"sentinel")

    assert _publisher(s3).package_unified_source() == name
    assert s3.get_object(Bucket=_BUCKET, Key=key)["Body"].read() == b"sentinel"


@mock_aws
def test_unified_source_aborts_when_the_existence_check_fails(
    tmp_path, monkeypatch, aws_credentials
):
    """A non-404 error checking S3 stops publish rather than skipping silently."""
    monkeypatch.chdir(tmp_path)
    _unified_tree(tmp_path)
    pub = _publisher(boto3.client("s3", region_name="us-east-1"))

    with pytest.raises(SystemExit) as exc:
        pub.package_unified_source()

    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# package_multi_doc_discovery_source
# ---------------------------------------------------------------------------


def _mdd_tree(root):
    """A repo-shaped tree for the multi-doc discovery Docker source zip."""
    lib = root / "lib" / "idp_common_pkg"
    _write(lib / "pyproject.toml", "[project]\nname='idp_common'\n")
    _write(lib / "idp_common" / "__init__.py", "VERSION = 1\n")
    _write(lib / "idp_common" / "stale.pyc", "bytecode")
    _write(lib / ".venv" / "lib" / "wheel.py", "native")
    _write(lib / "idp_common.egg-info" / "PKG-INFO", "meta")
    _write(lib / ".tox" / "py312" / "marker", "tox")
    _write(lib / ".mypy_cache" / "cache.json", "{}")
    _write(lib / ".ruff_cache" / "cache.bin", "bin")
    _write(lib / "venv" / "bin" / "python", "shim")
    _write(lib / "dist" / "wheel.whl", "wheel")
    _write(lib / "build" / "out.py", "out")

    handler = root / "src" / "lambda" / "multi_doc_discovery"
    _write(handler / "index.py", "def handler(e, c): return 1\n")
    _write(handler / "helpers" / "split.py", "def split(): pass\n")
    _write(handler / "__pycache__" / "index.pyc", "bytecode")
    # As in the unified tree: a stray compiled file outside __pycache__, so the
    # per-file suffix filter is exercised and not just the directory filter.
    _write(handler / "index.pyc", "bytecode")

    nested = root / "nested" / "multi-doc-discovery"
    _write(nested / "Dockerfile", "FROM python:3.12\n")
    _write(nested / "requirements.txt", "Pillow==11.0.0\n")
    return root


@mock_aws
def test_mdd_zip_members_and_exclusions(tmp_path, monkeypatch, aws_credentials):
    """The exact member list, built from a tree carrying every exclusion case.

    The repo-root version of this assertion already exists in
    ``test_multi_doc_discovery_source_zip.py``, but it can only check that the
    excluded shapes are *absent* — a real checkout may not contain a ``.tox`` or
    a ``venv`` at all, so the test passes whether or not the filter works. Here
    every excluded directory is present by construction, so the filter is
    genuinely exercised.
    """
    monkeypatch.chdir(tmp_path)
    _mdd_tree(tmp_path)
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=_BUCKET)
    pub = _publisher(s3)

    name = pub.package_multi_doc_discovery_source()

    assert name == "multi-doc-discovery-source.zip"
    assert _zip_names(os.path.join(".aws-sam", name)) == [
        "lib/idp_common_pkg/idp_common/__init__.py",
        "lib/idp_common_pkg/pyproject.toml",
        "nested/multi-doc-discovery/Dockerfile",
        "nested/multi-doc-discovery/requirements.txt",
        "src/lambda/multi_doc_discovery/helpers/split.py",
        "src/lambda/multi_doc_discovery/index.py",
    ]


@mock_aws
def test_mdd_zip_is_rebuilt_every_time_and_uploaded(
    tmp_path, monkeypatch, aws_credentials
):
    """The name is fixed, so the zip must be regenerated on every publish.

    Unlike the other two artifacts this one is not content-addressed. If it were
    ever skipped when the file already existed, an edited handler could never
    ship — so the "always recreate" behaviour is the correctness property, and a
    hand-written zip at that path must be overwritten.
    """
    monkeypatch.chdir(tmp_path)
    _mdd_tree(tmp_path)
    os.makedirs(".aws-sam", exist_ok=True)
    stale = os.path.join(".aws-sam", "multi-doc-discovery-source.zip")
    with zipfile.ZipFile(stale, "w") as zf:
        zf.writestr("stale.txt", "previous publish")
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=_BUCKET)
    pub = _publisher(s3)

    name = pub.package_multi_doc_discovery_source()

    assert "stale.txt" not in _zip_names(stale)
    key = f"{_PREFIX_AND_VERSION}/{name}"
    with open(stale, "rb") as fh:
        assert s3.get_object(Bucket=_BUCKET, Key=key)["Body"].read() == fh.read()


@mock_aws
def test_mdd_zip_upload_is_skipped_when_the_key_exists(
    tmp_path, monkeypatch, aws_credentials
):
    """DEFECT pinned: a fixed key plus a skip-if-present check strands changes.

    The zip is always rebuilt locally, but the upload is guarded by
    ``head_object`` on a key that carries no content hash
    (``<prefix>/<version>/multi-doc-discovery-source.zip``). So the second and
    every later publish *of the same version* uploads nothing: the freshly-built
    zip is discarded and CodeBuild keeps downloading the first one. Re-publishing
    the same version after a handler fix — which is exactly what an iteration
    loop does — silently ships the old code.

    This is pinned rather than fixed; it needs either a content hash in the key
    or an unconditional upload.
    """
    monkeypatch.chdir(tmp_path)
    _mdd_tree(tmp_path)
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=_BUCKET)
    key = f"{_PREFIX_AND_VERSION}/multi-doc-discovery-source.zip"
    s3.put_object(Bucket=_BUCKET, Key=key, Body=b"the previous publish")

    _publisher(s3).package_multi_doc_discovery_source()

    assert (
        s3.get_object(Bucket=_BUCKET, Key=key)["Body"].read() == b"the previous publish"
    )


@mock_aws
def test_mdd_zip_aborts_when_the_existence_check_fails(
    tmp_path, monkeypatch, aws_credentials
):
    """A non-404 error checking S3 stops publish."""
    monkeypatch.chdir(tmp_path)
    _mdd_tree(tmp_path)
    pub = _publisher(boto3.client("s3", region_name="us-east-1"))

    with pytest.raises(SystemExit) as exc:
        pub.package_multi_doc_discovery_source()

    assert exc.value.code == 1


@mock_aws
def test_mdd_zip_refuses_a_missing_requirements_file(
    tmp_path, monkeypatch, aws_credentials
):
    """A missing build input aborts before anything is uploaded.

    The repo-root sibling test covers the Dockerfile by patching
    ``os.path.isfile``; this one simply omits ``requirements.txt`` from a real
    tree, and additionally checks that nothing reached S3 — an abort that had
    already uploaded a broken zip would leave CodeBuild building from it.
    """
    monkeypatch.chdir(tmp_path)
    _mdd_tree(tmp_path)
    (tmp_path / "nested" / "multi-doc-discovery" / "requirements.txt").unlink()
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=_BUCKET)
    pub = _publisher(s3)

    with pytest.raises(SystemExit) as exc:
        pub.package_multi_doc_discovery_source()

    assert exc.value.code == 1
    assert "Contents" not in s3.list_objects_v2(Bucket=_BUCKET)


@mock_aws
def test_an_upload_failure_escapes_instead_of_exiting(
    tmp_path, monkeypatch, aws_credentials
):
    """DEFECT pinned: the ``except ClientError`` around ``upload_file`` is dead.

    All three packaging methods wrap ``self.s3_client.upload_file(...)`` in
    ``except ClientError`` and call ``sys.exit(1)`` with a red message. But
    ``upload_file`` is boto3's managed-transfer helper, and it re-wraps any
    underlying failure as ``boto3.exceptions.S3UploadFailedError``, which is not
    a ``ClientError``. The handler therefore cannot fire: an upload failure —
    a denied ``s3:PutObject``, a bucket in another region, a vanished bucket —
    surfaces as an unhandled traceback rather than the intended one-line abort.

    Measured here by creating the bucket (so ``head_object`` returns a clean
    404) and deleting it before the upload. The assertion is on the current
    behaviour; the fix is to catch ``Exception`` or
    ``(ClientError, S3UploadFailedError)``, as ``_upload_template_to_s3`` in the
    same file already does.
    """
    from boto3.exceptions import S3UploadFailedError

    monkeypatch.chdir(tmp_path)
    _mdd_tree(tmp_path)
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=_BUCKET)
    pub = _publisher(s3)

    real_upload = s3.upload_file

    def upload_after_the_bucket_is_gone(*args, **kwargs):
        s3.delete_bucket(Bucket=_BUCKET)
        return real_upload(*args, **kwargs)

    pub.s3_client = type(
        "OneShotClient",
        (),
        {
            "head_object": staticmethod(s3.head_object),
            "upload_file": staticmethod(upload_after_the_bucket_is_gone),
        },
    )()

    with pytest.raises(S3UploadFailedError):
        pub.package_multi_doc_discovery_source()
