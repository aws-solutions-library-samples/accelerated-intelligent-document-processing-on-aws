# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for `IDPPublisher`'s layer build, layer discovery and lint gates.

Three groups of behaviour live here, all in the second half of `publish.py`.

**Lambda layer construction.** `build_lambda_layer` shells out to `uv pip
install` with a pinned target platform, copies `idp_sdk` in beside the installed
tree, deletes the packages the Lambda runtime already provides, and zips the
result under a name that embeds a hash of the first-party source. Every one of
those steps has a wrong version that still produces a plausible zip: a missing
`--python-platform` gives a layer of the developer's own architecture, a missing
runtime-package purge blows the 250 MB limit, and a name that does not track the
source hash means a stale layer is reused forever. The `uv` call is the one thing
that cannot run here, so it is replaced by a stand-in that **records the argv and
materialises a realistic installed tree**; every assertion after that reads the
zip and the directory that actually resulted.

**Layer discovery.** `_discover_existing_layer_zips` and
`_verify_layer_zips_exist` decide whether a publish may skip the layer build.
They are the paths that turn a cache hit into a shipped artifact, so the tests
build a `.aws-sam/layers` tree by hand — including decoys that must not be picked
up — and assert the discovery result against it. S3 interaction goes through
`moto`, so the "not in S3, upload it" branch is checked by reading the object
back out of the fake bucket.

**The lint and syntax gates.** `_validate_python_syntax` gets a file with a real
`SyntaxError`. `_validate_python_linting` and `_validate_cfn_lint` get a
stand-in `subprocess` whose recorded argv lists are asserted on, because "which
command did it run" is the entire content of those methods — and because a gate
that runs the wrong command, or stops after the first of two, passes a broken
tree.

Two tests pin defects rather than intended behaviour; see their docstrings.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import zipfile

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from rich.console import Console

from idp_sdk._core import publish as publish_mod
from idp_sdk._core.publish import IDPPublisher

_BUCKET = "artifacts-bucket"
_PREFIX = "idp"
_VERSION = "0.6.9"
_REGION = "us-east-1"

_EXPECTED_LAYERS = ("base", "reporting", "agents", "multi_document_discovery")


def _publisher(s3=None):
    pub = IDPPublisher(verbose=False)
    pub.bucket = _BUCKET
    pub.prefix = _PREFIX
    pub.version = _VERSION
    pub.prefix_and_version = f"{_PREFIX}/{_VERSION}"
    pub.main_template = "idp-main.yaml"
    pub.region = _REGION
    pub.console = Console(file=io.StringIO(), width=300, no_color=True)
    pub.s3_client = s3
    return pub


def _text(pub):
    return pub.console.file.getvalue()


def _write(root, rel_path, content="x\n"):
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


class _Completed:
    """The three attributes `publish.py` reads off a `CompletedProcess`."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _SubprocessShim:
    """Stands in for the `subprocess` module as `publish.py` sees it.

    Installed as the module-level `subprocess` name rather than by patching the
    stdlib module itself, so nothing outside `publish.py` changes behaviour for
    the duration of a test. Unknown attributes fall through to the real module.
    """

    def __init__(self, handler):
        self._handler = handler
        self.calls = []

    def run(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        return self._handler(list(cmd))

    def __getattr__(self, name):
        return getattr(subprocess, name)


class _ShutilShim:
    """`shutil` with a fixed `which` answer; everything else is the real module."""

    def __init__(self, which_result):
        self._which_result = which_result

    def which(self, name):
        return self._which_result

    def __getattr__(self, name):
        return getattr(shutil, name)


def _first_party_tree(root):
    """`lib/idp_common_pkg` + `lib/idp_sdk`, the two sources the layer hash covers."""
    _write(root, "lib/idp_common_pkg/setup.py", "setup()\n")
    _write(root, "lib/idp_common_pkg/idp_common/__init__.py", "VERSION = '1'\n")
    _write(root, "lib/idp_sdk/setup.py", "setup()\n")
    _write(root, "lib/idp_sdk/idp_sdk/__init__.py", "SDK = 1\n")
    _write(root, "lib/idp_sdk/idp_sdk/_core/publish.py", "class P: pass\n")


def _expected_source_hash(pub_factory=None):
    """The `{source_hash}` the layer zip name must carry.

    Recomputed the same way `build_lambda_layer` does, from a *fresh* publisher
    so the instance cache cannot serve a stale value. This mirrors the
    implementation deliberately: what is under test here is that the **name the
    build writes** tracks the source, and that discovery later matches it — the
    checksum function itself is covered in `test_publish_rebuild_detection.py`.
    """
    import hashlib

    pub = (pub_factory or _publisher)()
    common = pub.get_source_files_checksum("./lib/idp_common_pkg")[:8]
    sdk = pub.get_source_files_checksum("./lib/idp_sdk")[:8]
    return hashlib.sha256(f"{common}{sdk}".encode()).hexdigest()[:8]


def _fake_uv_install(cmd):
    """Populate the `--target` directory the way a real `uv pip install` would.

    Includes the runtime packages the build is supposed to delete and a
    `.dist-info` directory, so the purge and the zip filter can be measured
    against a tree that actually contains what they claim to remove.
    """
    target = cmd[cmd.index("--target") + 1]
    layout = {
        "idp_common/__init__.py": "VERSION = '1'\n",
        "idp_common-0.1.0.dist-info/METADATA": "Name: idp-common\n",
        "boto3/__init__.py": "import botocore\n",
        "boto3-1.40.0.dist-info/METADATA": "Name: boto3\n",
        "botocore/__init__.py": "pass\n",
        "urllib3/__init__.py": "pass\n",
        "jmespath/__init__.py": "pass\n",
        "third_party/mod.py": "VALUE = 1\n",
        "third_party/__pycache__/mod.cpython-312.pyc": "bytecode\n",
        # Bytecode outside `__pycache__`, which the directory prune does not
        # reach, so the per-file suffix filter is what has to exclude it.
        "third_party/legacy.pyc": "bytecode\n",
        "third_party/legacy.pyo": "bytecode\n",
    }
    for rel, content in layout.items():
        path = os.path.join(target, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(content)
    return _Completed(returncode=0, stdout="Installed 8 packages\n")


def _zip_names(zip_path):
    with zipfile.ZipFile(zip_path) as zf:
        return set(zf.namelist())


# ---------------------------------------------------------------------------
# build_lambda_layer
# ---------------------------------------------------------------------------


def test_build_lambda_layer_pins_the_lambda_target_platform_in_its_install_command(
    monkeypatch, tmp_path
):
    """The argv is the whole contract with `uv`, so it is asserted exactly.

    `--python-platform x86_64-manylinux_2_28` and `--python-version 3.12` are
    what make the layer loadable by the Lambda runtime; without them a build on
    an ARM Mac produces a layer that imports and then segfaults in Lambda.
    `--only-binary=:all:` with `--no-binary idp-common` is the pair that takes
    wheels for every dependency but builds the first-party package from the
    local checkout.
    """
    monkeypatch.chdir(tmp_path)
    _first_party_tree(tmp_path)
    shim = _SubprocessShim(_fake_uv_install)
    monkeypatch.setattr(publish_mod, "subprocess", shim)

    pub = _publisher()
    pub.build_lambda_layer("base", ["docs_service", "image"])

    assert shim.calls == [
        [
            "uv",
            "pip",
            "install",
            "./lib/idp_common_pkg[docs_service,image]",
            "--python-platform",
            "x86_64-manylinux_2_28",
            "--python-version",
            "3.12",
            "--only-binary=:all:",
            "--no-binary",
            "idp-common",
            "--target",
            os.path.join(".aws-sam", "layers", "base-build", "python"),
            "--upgrade",
        ]
    ]


def test_build_lambda_layer_installs_without_extras_when_none_are_requested(
    monkeypatch, tmp_path
):
    """No extras means a bare path, not `pkg[]` — which `uv` would reject."""
    monkeypatch.chdir(tmp_path)
    _first_party_tree(tmp_path)
    shim = _SubprocessShim(_fake_uv_install)
    monkeypatch.setattr(publish_mod, "subprocess", shim)

    _publisher().build_lambda_layer("bare", [])

    assert shim.calls[0][3] == "./lib/idp_common_pkg"


def test_build_lambda_layer_produces_a_zip_named_for_the_first_party_source_hash(
    monkeypatch, tmp_path
):
    """The name is the cache key every later step matches on.

    `_discover_existing_layer_zips` reuses a layer only when the hash in its
    filename equals the current source hash, so if the build stopped folding
    `idp_sdk` (or `idp_common_pkg`) into that hash, an edit to the unhashed side
    would leave the old zip eligible for reuse and ship stale library code.
    """
    monkeypatch.chdir(tmp_path)
    _first_party_tree(tmp_path)
    monkeypatch.setattr(publish_mod, "subprocess", _SubprocessShim(_fake_uv_install))

    expected_hash = _expected_source_hash()
    zip_path, zip_name = _publisher().build_lambda_layer("base", ["image"])

    assert zip_name == f"idp-common-base-{expected_hash}.zip"
    assert zip_path == os.path.join(".aws-sam", "layers", zip_name)
    assert (tmp_path / zip_path).is_file()


def test_the_layer_zip_name_moves_when_only_the_sdk_source_changes(
    monkeypatch, tmp_path
):
    """Both first-party trees feed the hash, not just `idp_common_pkg`.

    A build that hashed only `idp_common_pkg` would give the same name here, and
    the discovery path would then reuse a zip whose bundled `idp_sdk` predates
    the edit. This is the discriminating case: nothing under `idp_common_pkg` is
    touched.
    """
    monkeypatch.chdir(tmp_path)
    _first_party_tree(tmp_path)
    monkeypatch.setattr(publish_mod, "subprocess", _SubprocessShim(_fake_uv_install))

    _, before = _publisher().build_lambda_layer("base", [])

    _write(tmp_path, "lib/idp_sdk/idp_sdk/_core/publish.py", "class P:\n    NEW = 1\n")
    _, after = _publisher().build_lambda_layer("base", [])

    assert before != after


def test_the_layer_zip_carries_the_sdk_and_drops_the_lambda_runtime_packages(
    monkeypatch, tmp_path
):
    """What ends up inside the zip is read back out of the zip.

    The runtime purge is worth ~100 MB per layer and is the difference between a
    layer Lambda accepts and one it rejects for size. `idp_sdk` is copied in
    rather than installed, so its absence would be a silent `ImportError` in
    every Lambda that uses the layer.
    """
    monkeypatch.chdir(tmp_path)
    _first_party_tree(tmp_path)
    monkeypatch.setattr(publish_mod, "subprocess", _SubprocessShim(_fake_uv_install))

    zip_path, _ = _publisher().build_lambda_layer("base", [])
    names = _zip_names(tmp_path / zip_path)

    # The first-party payload is present, under the `python/` prefix Lambda
    # requires for a layer.
    assert "python/idp_sdk/__init__.py" in names
    assert "python/idp_sdk/_core/publish.py" in names
    assert "python/idp_common/__init__.py" in names
    assert "python/third_party/mod.py" in names

    # Everything the Lambda runtime already provides is gone, directories and
    # their dist-info alike.
    assert not [n for n in names if n.startswith("python/boto3")]
    assert not [n for n in names if n.startswith("python/botocore")]
    assert not [n for n in names if n.startswith("python/urllib3")]
    assert not [n for n in names if n.startswith("python/jmespath")]

    # Bytecode is filtered by the zip walk, both by pruning `__pycache__` and by
    # the per-file suffix check that catches `.pyc`/`.pyo` living elsewhere.
    assert not [n for n in names if "__pycache__" in n]
    assert "python/third_party/legacy.pyc" not in names
    assert "python/third_party/legacy.pyo" not in names


def test_the_dist_info_exclusion_in_the_zip_walk_never_matches(monkeypatch, tmp_path):
    """DEFECT: `.dist-info` directories are not excluded from the layer zip.

    publish.py:3564 filters directories with
    `[d for d in dirs if d not in {"__pycache__", "*.dist-info"}]`. That is a
    set-membership test against a literal `"*.dist-info"`, not a glob, so it can
    only ever match a directory named exactly `*.dist-info`. The companion file
    filter on the next line has the same shape — `file.endswith(".dist-info")`
    matches a *file* with that suffix, which a dist-info directory does not
    produce. So every package's `.dist-info` metadata directory ships inside
    every layer.

    Observable consequence: the layer zips are larger than the code intends, and
    the four layers carry metadata the runtime never reads. It is a size and
    tidiness defect rather than a correctness one — `importlib.metadata` in a
    Lambda would actually break if this were "fixed" naively, which is probably
    the reason nobody noticed.

    Note the contrast with `__pycache__` in the test above: that entry *is* a
    plain directory name and is correctly excluded, so this test is measuring
    the glob specifically and not a filter that does nothing at all.
    """
    monkeypatch.chdir(tmp_path)
    _first_party_tree(tmp_path)
    monkeypatch.setattr(publish_mod, "subprocess", _SubprocessShim(_fake_uv_install))

    zip_path, _ = _publisher().build_lambda_layer("base", [])
    names = _zip_names(tmp_path / zip_path)

    assert "python/idp_common-0.1.0.dist-info/METADATA" in names


def test_a_leftover_staging_directory_does_not_leak_into_the_new_layer(
    monkeypatch, tmp_path
):
    """The staging tree is deleted before the install, not merged into.

    A previous run killed between the install and the zip leaves
    `.aws-sam/layers/<name>-build/python` full of a different layer's extras. If
    that survived, the new layer would ship whatever the old one installed —
    a layer larger than intended and carrying code its Lambdas do not expect.
    """
    monkeypatch.chdir(tmp_path)
    _first_party_tree(tmp_path)
    _write(
        tmp_path,
        ".aws-sam/layers/base-build/python/stale_pkg/leftover.py",
        "FROM_A_PREVIOUS_RUN = 1\n",
    )
    monkeypatch.setattr(publish_mod, "subprocess", _SubprocessShim(_fake_uv_install))

    zip_path, _ = _publisher().build_lambda_layer("base", [])
    names = _zip_names(tmp_path / zip_path)

    assert "python/stale_pkg/leftover.py" not in names
    assert "python/idp_common/__init__.py" in names


def test_a_stale_sdk_copy_inside_the_installed_tree_is_replaced_not_merged(
    monkeypatch, tmp_path
):
    """`idp_sdk` is copied in fresh, so a module deleted upstream disappears.

    `copytree` refuses an existing destination, so the destination is removed
    first. Merging instead would keep a module that was deleted from the SDK —
    the layer would carry a stale import target that resolves at runtime and
    shadows nothing, which is the hardest kind of stale artifact to notice.
    """
    monkeypatch.chdir(tmp_path)
    _first_party_tree(tmp_path)

    def install_with_stale_sdk(cmd):
        result = _fake_uv_install(cmd)
        target = cmd[cmd.index("--target") + 1]
        stale = os.path.join(target, "idp_sdk", "deleted_upstream.py")
        os.makedirs(os.path.dirname(stale), exist_ok=True)
        with open(stale, "w") as handle:
            handle.write("REMOVED_IN_A_LATER_RELEASE = 1\n")
        return result

    monkeypatch.setattr(
        publish_mod, "subprocess", _SubprocessShim(install_with_stale_sdk)
    )

    zip_path, _ = _publisher().build_lambda_layer("base", [])
    names = _zip_names(tmp_path / zip_path)

    assert "python/idp_sdk/deleted_upstream.py" not in names
    assert "python/idp_sdk/__init__.py" in names


def test_the_runtime_purge_removes_a_matching_path_that_is_a_plain_file(
    monkeypatch, tmp_path
):
    """The purge is indifferent to whether a match is a directory or a file.

    This is a defensive branch — a normal `uv pip install --target` writes
    `boto3-1.40.0.dist-info` as a directory — but a partially extracted wheel or
    a hand-patched layer can leave a file with that name, and a purge that only
    handled directories would ship it. Asserted through the zip because "was it
    removed" is only answerable from what ended up inside.
    """
    monkeypatch.chdir(tmp_path)
    _first_party_tree(tmp_path)

    def install_with_a_stray_file(cmd):
        result = _fake_uv_install(cmd)
        target = cmd[cmd.index("--target") + 1]
        with open(os.path.join(target, "s3transfer-0.10.0.dist-info"), "w") as handle:
            handle.write("Name: s3transfer\n")
        return result

    monkeypatch.setattr(
        publish_mod, "subprocess", _SubprocessShim(install_with_a_stray_file)
    )

    pub = _publisher()
    pub.verbose = True
    zip_path, _ = pub.build_lambda_layer("base", [])

    assert not [n for n in _zip_names(tmp_path / zip_path) if "s3transfer" in n]
    assert "Removed Lambda runtime packages" in _text(pub)
    assert "s3transfer-0.10.0.dist-info" in _text(pub)


def test_build_lambda_layer_clears_stale_setuptools_artifacts_and_its_build_dir(
    monkeypatch, tmp_path
):
    """Leftover `build/` and `dist/` trees make the install fail with "File exists".

    They are cleaned before the install, and the layer's own staging directory is
    removed after zipping so a later run cannot pick up half of a previous one.
    """
    monkeypatch.chdir(tmp_path)
    _first_party_tree(tmp_path)
    _write(tmp_path, "lib/idp_common_pkg/build/lib/idp_common/old.py", "STALE = 1\n")
    _write(tmp_path, "lib/idp_common_pkg/dist/idp_common-0.0.1.tar.gz", "tar\n")
    monkeypatch.setattr(publish_mod, "subprocess", _SubprocessShim(_fake_uv_install))

    _publisher().build_lambda_layer("base", [])

    assert not (tmp_path / "lib" / "idp_common_pkg" / "build").exists()
    assert not (tmp_path / "lib" / "idp_common_pkg" / "dist").exists()
    assert not (tmp_path / ".aws-sam" / "layers" / "base-build").exists()


def test_rebuilding_an_unchanged_layer_leaves_the_existing_zip_byte_identical(
    monkeypatch, tmp_path
):
    """A second build with the same source reuses the zip rather than rewriting it.

    Reuse is keyed on the filename, so the check also confirms the name is
    stable across two runs — if it were not, the `.aws-sam/layers` directory
    would accumulate a zip per publish and `_discover_existing_layer_zips` would
    be choosing between them.

    The install still runs: the early return is after the `uv` call, so the work
    is repeated and only the zipping is skipped. That is recorded here rather
    than asserted as desirable.
    """
    monkeypatch.chdir(tmp_path)
    _first_party_tree(tmp_path)
    shim = _SubprocessShim(_fake_uv_install)
    monkeypatch.setattr(publish_mod, "subprocess", shim)

    zip_path, zip_name = _publisher().build_lambda_layer("base", [])
    first_bytes = (tmp_path / zip_path).read_bytes()

    pub = _publisher()
    again_path, again_name = pub.build_lambda_layer("base", [])

    assert (again_path, again_name) == (zip_path, zip_name)
    assert (tmp_path / zip_path).read_bytes() == first_bytes
    assert "already built with same source" in _text(pub)
    assert len(shim.calls) == 2


def test_a_failed_install_exits_the_publish_and_reports_the_installer_stderr(
    monkeypatch, tmp_path
):
    """A layer that cannot be built must stop the run, not produce an empty zip.

    The `uv` stderr has to reach the operator: a resolution conflict is the
    commonest cause and is unintelligible without it.
    """
    monkeypatch.chdir(tmp_path)
    _first_party_tree(tmp_path)
    monkeypatch.setattr(
        publish_mod,
        "subprocess",
        _SubprocessShim(
            lambda cmd: _Completed(returncode=1, stderr="No solution found for pyarrow")
        ),
    )

    pub = _publisher()
    with pytest.raises(SystemExit) as excinfo:
        pub.build_lambda_layer("base", [])

    assert excinfo.value.code == 1
    text = _text(pub)
    assert "Failed to build layer 'base'" in text
    assert "No solution found for pyarrow" in text
    assert not list((tmp_path / ".aws-sam" / "layers").glob("*.zip"))


# ---------------------------------------------------------------------------
# build_all_lambda_layers
# ---------------------------------------------------------------------------


def _stub_layer_builder(root, source_hash="abcd1234"):
    """Replace `build_lambda_layer` with a recorder that writes a real zip.

    A real file is written because the caller uploads it: a `MagicMock` here
    would let a wrong path through, whereas `moto`'s `upload_file` will not.
    """
    calls = []

    def build(layer_name, layer_extras):
        calls.append((layer_name, list(layer_extras)))
        zip_name = f"idp-common-{layer_name}-{source_hash}.zip"
        zip_path = os.path.join(".aws-sam", "layers", zip_name)
        (root / ".aws-sam" / "layers").mkdir(parents=True, exist_ok=True)
        (root / zip_path).write_bytes(f"zip-for-{layer_name}".encode())
        return zip_path, zip_name

    return build, calls


@mock_aws
def test_build_all_lambda_layers_builds_the_four_layers_and_uploads_each(
    monkeypatch, tmp_path, aws_credentials
):
    """The set of layers, their extras, and the S3 keys they land on.

    The keys are read back out of the fake bucket rather than asserted against a
    recorded call, because the template tokens name these paths and a wrong
    prefix produces a stack whose Lambdas reference a nonexistent layer object.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    pub = _publisher(s3)
    build, calls = _stub_layer_builder(tmp_path)
    monkeypatch.setattr(pub, "build_lambda_layer", build)

    result = pub.build_all_lambda_layers()

    assert [name for name, _ in calls] == list(_EXPECTED_LAYERS)
    assert dict(calls) == {
        "base": ["docs_service", "image"],
        "reporting": ["reporting"],
        "agents": ["agents"],
        "multi_document_discovery": ["multi_document_discovery"],
    }

    for layer in _EXPECTED_LAYERS:
        info = result[layer]
        assert info["zip_name"] == f"idp-common-{layer}-abcd1234.zip"
        assert info["hash"] == "abcd1234"
        assert info["s3_key"] == f"{_PREFIX}/{_VERSION}/layers/{info['zip_name']}"
        body = s3.get_object(Bucket=_BUCKET, Key=info["s3_key"])["Body"].read()
        assert body == f"zip-for-{layer}".encode()

    # `.aws-sam/layers` is created even when it did not exist.
    assert (tmp_path / ".aws-sam" / "layers").is_dir()


@mock_aws
def test_build_all_lambda_layers_does_not_re_upload_a_layer_already_in_s3(
    monkeypatch, tmp_path, aws_credentials
):
    """An existing key is left alone, which is why the body is checked, not a call.

    Layer zips run to tens of megabytes, so the skip is a real saving; the risk
    it carries is overwriting nothing when the local zip differs, which is
    handled by the source hash being part of the key.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    existing_key = f"{_PREFIX}/{_VERSION}/layers/idp-common-base-abcd1234.zip"
    s3.put_object(Bucket=_BUCKET, Key=existing_key, Body=b"already-uploaded")

    pub = _publisher(s3)
    build, _ = _stub_layer_builder(tmp_path)
    monkeypatch.setattr(pub, "build_lambda_layer", build)
    pub.build_all_lambda_layers()

    assert s3.get_object(Bucket=_BUCKET, Key=existing_key)["Body"].read() == (
        b"already-uploaded"
    )
    # The other three were uploaded normally.
    assert (
        s3.get_object(
            Bucket=_BUCKET,
            Key=f"{_PREFIX}/{_VERSION}/layers/idp-common-agents-abcd1234.zip",
        )["Body"].read()
        == b"zip-for-agents"
    )


@mock_aws
def test_an_s3_error_other_than_a_missing_key_aborts_the_layer_upload(
    monkeypatch, tmp_path, aws_credentials
):
    """Only a 404 means "upload it"; anything else must propagate.

    Swallowing, say, an AccessDenied here would leave the publish reporting
    success with no layer object in the bucket at all.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    # Bucket deliberately not created, so head_object answers NoSuchBucket.
    pub = _publisher(s3)
    build, _ = _stub_layer_builder(tmp_path)
    monkeypatch.setattr(pub, "build_lambda_layer", build)

    with pytest.raises(ClientError) as excinfo:
        pub.build_all_lambda_layers()
    assert excinfo.value.response["Error"]["Code"] != "404"


# ---------------------------------------------------------------------------
# _verify_layer_zips_exist
# ---------------------------------------------------------------------------


def test_verify_layer_zips_reports_a_rebuild_when_the_directory_or_a_layer_is_absent(
    monkeypatch, tmp_path
):
    """`True` means "rebuild needed", and each absence must produce it.

    The three states are distinguished because they arise for different reasons:
    no directory at all (a fresh clone, or `clean_checksums`), a directory with
    no zips (an interrupted build), and a directory missing exactly one layer (a
    build that died partway through the four).
    """
    monkeypatch.chdir(tmp_path)
    pub = _publisher()

    assert pub._verify_layer_zips_exist() is True
    assert "Layers directory missing" in _text(pub)

    (tmp_path / ".aws-sam" / "layers").mkdir(parents=True)
    pub = _publisher()
    assert pub._verify_layer_zips_exist() is True
    assert "No layer zips found" in _text(pub)

    for layer in _EXPECTED_LAYERS[:-1]:
        _write(tmp_path, f".aws-sam/layers/idp-common-{layer}-abcd1234.zip", "zip\n")
    pub = _publisher()
    assert pub._verify_layer_zips_exist() is True
    assert "Layer zip for 'multi_document_discovery' missing" in _text(pub)

    _write(
        tmp_path,
        ".aws-sam/layers/idp-common-multi_document_discovery-abcd1234.zip",
        "zip\n",
    )
    assert _publisher()._verify_layer_zips_exist() is False


def test_verify_layer_zips_ignores_files_that_are_not_layer_zips(monkeypatch, tmp_path):
    """Decoys in `.aws-sam/layers` must not count as layers.

    A leftover `.zip.bak`, a build staging directory and an unrelated archive
    all live in this directory in practice. If any of them satisfied the check,
    a publish would skip the layer build with no usable zip present.
    """
    monkeypatch.chdir(tmp_path)
    for name in (
        "idp-common-base-abcd1234.zip.bak",
        "idp-common-reporting-abcd1234.tar.gz",
        "common-agents-abcd1234.zip",
        "readme.txt",
    ):
        _write(tmp_path, f".aws-sam/layers/{name}", "decoy\n")
    (tmp_path / ".aws-sam" / "layers" / "base-build").mkdir()

    assert _publisher()._verify_layer_zips_exist() is True


def test_verify_layer_zips_accepts_any_hash_because_staleness_is_checked_elsewhere(
    monkeypatch, tmp_path
):
    """This check is about presence only; `_discover_existing_layer_zips` judges age.

    The asymmetry is deliberate and is worth pinning in both directions: a zip
    with a long-stale hash satisfies *this* check, and the test below shows
    discovery rejecting the same file.
    """
    monkeypatch.chdir(tmp_path)
    for layer in _EXPECTED_LAYERS:
        _write(tmp_path, f".aws-sam/layers/idp-common-{layer}-deadbeef.zip", "zip\n")

    assert _publisher()._verify_layer_zips_exist() is False


# ---------------------------------------------------------------------------
# _discover_existing_layer_zips
# ---------------------------------------------------------------------------


def _seed_layer_zips(root, source_hash, layers=_EXPECTED_LAYERS):
    for layer in layers:
        _write(
            root,
            f".aws-sam/layers/idp-common-{layer}-{source_hash}.zip",
            f"zip-for-{layer}\n",
        )


@mock_aws
def test_layer_discovery_returns_nothing_when_there_is_no_layers_directory(
    monkeypatch, tmp_path, aws_credentials
):
    monkeypatch.chdir(tmp_path)
    pub = _publisher(boto3.client("s3", region_name=_REGION))
    assert pub._discover_existing_layer_zips() == {}
    assert "Layers directory not found" in _text(pub)


@mock_aws
def test_layer_discovery_matches_the_current_source_hash_and_ignores_decoys(
    monkeypatch, tmp_path, aws_credentials
):
    """Discovery must pick exactly the four zips whose hash is current.

    The decoys are the shapes that actually appear in a `.aws-sam/layers`
    directory: a previous build's zip under the old hash, a partial download, a
    similarly named archive from a different tool. Accepting any of them is how
    a stale layer gets into a release, so the assertion is on the exact result
    dict rather than on its size.
    """
    monkeypatch.chdir(tmp_path)
    _first_party_tree(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    current = _publisher().get_source_files_checksum("./lib/idp_common_pkg")[:8]
    _seed_layer_zips(tmp_path, current)
    for decoy in (
        "idp-common-base-0badf00d.zip",
        f"idp-common-base-{current}.zip.part",
        f"common-base-{current}.zip",
        f"idp-common-evaluation-{current}.zip",
    ):
        _write(tmp_path, f".aws-sam/layers/{decoy}", "decoy\n")

    for layer in _EXPECTED_LAYERS:
        s3.put_object(
            Bucket=_BUCKET,
            Key=f"{_PREFIX}/{_VERSION}/layers/idp-common-{layer}-{current}.zip",
            Body=b"in-s3",
        )

    pub = _publisher(s3)
    result = pub._discover_existing_layer_zips()

    assert set(result) == set(_EXPECTED_LAYERS)
    assert result["base"] == {
        "zip_path": os.path.join(
            ".aws-sam", "layers", f"idp-common-base-{current}.zip"
        ),
        "zip_name": f"idp-common-base-{current}.zip",
        "hash": current,
        "s3_key": f"{_PREFIX}/{_VERSION}/layers/idp-common-base-{current}.zip",
    }
    assert "Discovered 4 existing layer zips" in _text(pub)


@mock_aws
def test_layer_discovery_rejects_a_stale_hash_and_names_it(
    monkeypatch, tmp_path, aws_credentials
):
    """A zip built from older source is not eligible, and the operator is told why.

    This is the direction that matters: the checksum files can say `lib` is
    current while the zips on disk predate an edit (a rebased branch, a restored
    cache). Reporting the old hash alongside the new one is what makes the
    resulting rebuild explicable.
    """
    monkeypatch.chdir(tmp_path)
    _first_party_tree(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    current = _publisher().get_source_files_checksum("./lib/idp_common_pkg")[:8]
    _seed_layer_zips(tmp_path, current, layers=("reporting", "agents"))
    _seed_layer_zips(tmp_path, "0badf00d", layers=("base",))
    for layer in ("reporting", "agents"):
        s3.put_object(
            Bucket=_BUCKET,
            Key=f"{_PREFIX}/{_VERSION}/layers/idp-common-{layer}-{current}.zip",
            Body=b"in-s3",
        )

    pub = _publisher(s3)
    result = pub._discover_existing_layer_zips()

    assert set(result) == {"reporting", "agents"}
    text = _text(pub)
    assert f"stale source hash (0badf00d != {current})" in text
    assert "No existing layer zip found for 'multi_document_discovery'" in text
    assert "Only 2/4 layers have matching source hash" in text


@mock_aws
def test_layer_discovery_uploads_a_layer_that_is_present_locally_but_not_in_s3(
    monkeypatch, tmp_path, aws_credentials
):
    """A version bump moves the S3 path without changing the source hash.

    `prefix_and_version` carries the release version, so publishing 0.6.9 from a
    tree whose layers were built for 0.6.8 finds the zips locally and nothing at
    the new key. Uploading rather than rebuilding is the right answer, and the
    object is read back to prove the local bytes are what landed.
    """
    monkeypatch.chdir(tmp_path)
    _first_party_tree(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    current = _publisher().get_source_files_checksum("./lib/idp_common_pkg")[:8]
    _seed_layer_zips(tmp_path, current)

    pub = _publisher(s3)
    result = pub._discover_existing_layer_zips()

    assert set(result) == set(_EXPECTED_LAYERS)
    for layer in _EXPECTED_LAYERS:
        key = f"{_PREFIX}/{_VERSION}/layers/idp-common-{layer}-{current}.zip"
        assert s3.get_object(Bucket=_BUCKET, Key=key)["Body"].read() == (
            f"zip-for-{layer}\n".encode()
        )
    assert "not in S3 at current version path - uploading" in _text(pub)


@mock_aws
def test_layer_discovery_propagates_an_s3_error_that_is_not_a_missing_key(
    monkeypatch, tmp_path, aws_credentials
):
    """Only a 404 means "upload it". Anything else must stop the publish.

    Treating an AccessDenied or a missing bucket as "not there, upload it" would
    attempt the upload, fail again, and report the second error instead of the
    first — or, worse, succeed against the wrong bucket.
    """
    monkeypatch.chdir(tmp_path)
    _first_party_tree(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    # Bucket deliberately absent, so head_object answers NoSuchBucket.
    current = _publisher().get_source_files_checksum("./lib/idp_common_pkg")[:8]
    _seed_layer_zips(tmp_path, current)

    pub = _publisher(s3)
    with pytest.raises(ClientError) as excinfo:
        pub._discover_existing_layer_zips()
    assert excinfo.value.response["Error"]["Code"] != "404"


# ---------------------------------------------------------------------------
# _verify_packaged_templates_exist
# ---------------------------------------------------------------------------


def test_packaged_template_verification_adds_only_components_that_lack_a_package(
    monkeypatch, tmp_path
):
    """The backfill skips `main`/`lib`, skips what is already queued, adds the rest.

    `main` and `lib` have no `packaged.yaml` by construction, so a check that did
    not special-case them would force a full rebuild on every single publish —
    the failure mode that looks like "smart detection does nothing".
    """
    monkeypatch.chdir(tmp_path)
    for component in (
        "nested/api-resolvers",
        "nested/multi-doc-discovery",
        "feature-platform/main-stack-extensions",
    ):
        _write(tmp_path, f"{component}/.aws-sam/packaged.yaml", "Resources: {}\n")
    # A decoy inside the right directory but under the wrong filename.
    _write(tmp_path, "patterns/unified/.aws-sam/packaged.yml", "Resources: {}\n")

    pending = [
        {
            "component": "nested/bedrockkb",
            "dependencies": ["nested/bedrockkb/src"],
            "changed_dependencies": ["nested/bedrockkb/src"],
            "checksum_file": "nested/bedrockkb/.checksum",
            "current_checksum": "seeded",
            "current_dep_checksums": {},
        }
    ]

    pub = _publisher()
    pub._verify_packaged_templates_exist(pending)

    assert [item["component"] for item in pending] == [
        "nested/bedrockkb",
        "patterns/unified",
    ]
    added = pending[1]
    assert added["changed_dependencies"] == ["packaged.yaml missing"]
    assert added["checksum_file"] == "patterns/unified/.checksum"
    assert added["current_checksum"]
    # The already-queued component was not duplicated or rewritten.
    assert pending[0]["current_checksum"] == "seeded"
    assert "patterns/unified/packaged.yaml missing" in _text(pub)


def test_packaged_template_verification_records_checksums_for_the_paths_it_finds(
    monkeypatch, tmp_path
):
    """The backfilled entry must carry usable checksums, or the retry is wasted.

    `update_component_checksum` writes whatever is in `current_dep_checksums`,
    so an entry with an empty map would persist a checksum describing nothing
    and the component would rebuild again on the next publish forever.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "nested/bedrockkb/template.yaml", "Resources: {}\n")
    _write(tmp_path, "nested/bedrockkb/src/handler.py", "def h(): pass\n")

    pending = []
    _publisher()._verify_packaged_templates_exist(pending)

    bedrockkb = next(i for i in pending if i["component"] == "nested/bedrockkb")
    assert set(bedrockkb["current_dep_checksums"]) == {
        "nested/bedrockkb/src",
        "nested/bedrockkb/template.yaml",
    }
    assert all(len(v) == 64 for v in bedrockkb["current_dep_checksums"].values())
    # A component whose dependencies are entirely absent records none of them.
    unified = next(i for i in pending if i["component"] == "patterns/unified")
    assert unified["current_dep_checksums"] == {}


# ---------------------------------------------------------------------------
# _validate_python_syntax
# ---------------------------------------------------------------------------


def test_python_syntax_validation_accepts_a_tree_of_valid_modules(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path, "src/lambda/one/index.py", "def handler(event, ctx):\n    return 1\n"
    )
    _write(tmp_path, "src/lambda/two/index.py", "import os\n\nVALUE = os.sep\n")
    _write(tmp_path, "src/lambda/two/notes.txt", "this is not python\n")

    assert _publisher()._validate_python_syntax("src") is True


def test_python_syntax_validation_reports_the_offending_file(monkeypatch, tmp_path):
    """The path has to be in the output or the operator cannot act on it.

    A real `SyntaxError` is used rather than a stubbed compiler: the value of
    this gate is that `sam build` would otherwise package the broken handler and
    the failure would surface as a Lambda import error after deployment.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "src/lambda/good/index.py", "VALUE = 1\n")
    _write(tmp_path, "src/lambda/bad/index.py", "def broken(:\n    pass\n")

    pub = _publisher()
    assert pub._validate_python_syntax("src") is False

    text = _text(pub)
    assert os.path.join("src", "lambda", "bad", "index.py") in text
    assert "Python syntax error" in text


def test_python_syntax_validation_of_a_missing_directory_passes(monkeypatch, tmp_path):
    """`os.walk` over an absent path yields nothing, so the gate is vacuous there.

    Recorded because it means a typo in the directory name turns this gate off
    silently rather than failing.
    """
    monkeypatch.chdir(tmp_path)
    assert _publisher()._validate_python_syntax("no-such-directory") is True


# ---------------------------------------------------------------------------
# _validate_python_linting
# ---------------------------------------------------------------------------


def test_python_linting_runs_ruff_check_then_ruff_format_check(monkeypatch, tmp_path):
    """Both ruff invocations must run, in that order, with no extra arguments.

    These are the two commands CI's `lint-cicd` runs, and the publisher exists
    partly to catch locally what CI would otherwise catch after a push. A
    publisher that ran only `ruff check` would let a formatting failure through
    to CI.
    """
    monkeypatch.chdir(tmp_path)
    shim = _SubprocessShim(lambda cmd: _Completed(returncode=0))
    monkeypatch.setattr(publish_mod, "subprocess", shim)

    pub = _publisher()
    assert pub._validate_python_linting() is True
    assert shim.calls == [["ruff", "check"], ["ruff", "format", "--check"]]
    assert "Python linting passed" in _text(pub)


def test_a_ruff_check_failure_stops_before_the_format_check(monkeypatch, tmp_path):
    """Short-circuiting is asserted through the recorded argv list.

    If the format check ran anyway its output would be appended to the report,
    and the operator would see two failures for one cause.
    """
    monkeypatch.chdir(tmp_path)
    shim = _SubprocessShim(
        lambda cmd: _Completed(returncode=1, stdout="app.py:1:1: F401 unused import")
    )
    monkeypatch.setattr(publish_mod, "subprocess", shim)

    pub = _publisher()
    assert pub._validate_python_linting() is False
    assert shim.calls == [["ruff", "check"]]
    assert "F401 unused import" in _text(pub)


def test_a_format_check_failure_is_reported_separately_from_a_lint_failure(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)

    def handler(cmd):
        if cmd == ["ruff", "check"]:
            return _Completed(returncode=0)
        return _Completed(returncode=1, stdout="Would reformat: publish.py")

    shim = _SubprocessShim(handler)
    monkeypatch.setattr(publish_mod, "subprocess", shim)

    pub = _publisher()
    assert pub._validate_python_linting() is False
    assert len(shim.calls) == 2
    text = _text(pub)
    assert "formatting check failed" in text
    assert "Would reformat: publish.py" in text


def test_linting_is_skipped_entirely_when_disabled(monkeypatch, tmp_path):
    """`--no-lint` must not run ruff at all, not merely ignore its result."""
    monkeypatch.chdir(tmp_path)
    shim = _SubprocessShim(lambda cmd: _Completed(returncode=1))
    monkeypatch.setattr(publish_mod, "subprocess", shim)

    pub = _publisher()
    pub.lint_enabled = False
    assert pub._validate_python_linting() is True
    assert shim.calls == []


# ---------------------------------------------------------------------------
# _validate_cfn_lint
# ---------------------------------------------------------------------------


def _packaged_template_tree(root):
    """Packaged templates in the three places `_validate_cfn_lint` looks.

    Includes the decoys that must not be linted: a nested directory with no
    `.aws-sam`, and an `.aws-sam` holding a differently named yaml.
    """
    _write(root, ".aws-sam/idp-main.yaml", "Resources: {}\n")
    _write(root, "nested/api-resolvers/.aws-sam/packaged.yaml", "Resources: {}\n")
    _write(root, "nested/bedrockkb/.aws-sam/packaged.yaml", "Resources: {}\n")
    _write(root, "nested/multi-doc-discovery/template.yaml", "Resources: {}\n")
    _write(root, "nested/multi-doc-discovery/.aws-sam/build/template.yaml", "R: {}\n")
    _write(root, "patterns/unified/.aws-sam/packaged.yaml", "Resources: {}\n")


def test_cfn_lint_lints_the_packaged_templates_it_can_find_and_nothing_else(
    monkeypatch, tmp_path
):
    """Discovery is by exact packaged path, asserted against a tree with decoys.

    `nested/multi-doc-discovery` has an `.aws-sam` directory but no
    `packaged.yaml`, which is exactly the state a component in mid-build is in;
    linting its build-directory template instead would report errors about an
    intermediate artifact.
    """
    monkeypatch.chdir(tmp_path)
    _packaged_template_tree(tmp_path)
    shim = _SubprocessShim(lambda cmd: _Completed(returncode=0))
    monkeypatch.setattr(publish_mod, "subprocess", shim)
    monkeypatch.setattr(publish_mod, "shutil", _ShutilShim("/usr/local/bin/cfn-lint"))

    pub = _publisher()
    assert pub._validate_cfn_lint() is True

    linted = [cmd[1] for cmd in shim.calls]
    assert all(cmd[0] == "cfn-lint" for cmd in shim.calls)
    assert set(linted) == {
        ".aws-sam/idp-main.yaml",
        os.path.join("nested", "api-resolvers", ".aws-sam", "packaged.yaml"),
        os.path.join("nested", "bedrockkb", ".aws-sam", "packaged.yaml"),
        os.path.join("patterns", "unified", ".aws-sam", "packaged.yaml"),
    }
    assert "4 templates checked" in _text(pub)


def test_cfn_lint_is_skipped_when_the_linter_is_not_installed(monkeypatch, tmp_path):
    """A missing `cfn-lint` is a skip with an install hint, not a failure.

    The publisher runs on developer machines that may not have it; failing would
    make the tool unusable, so the gate degrades. Worth knowing when reading a
    green publish log: "passed" and "not installed" are different lines.
    """
    monkeypatch.chdir(tmp_path)
    _packaged_template_tree(tmp_path)
    shim = _SubprocessShim(lambda cmd: _Completed(returncode=1))
    monkeypatch.setattr(publish_mod, "subprocess", shim)
    monkeypatch.setattr(publish_mod, "shutil", _ShutilShim(None))

    pub = _publisher()
    assert pub._validate_cfn_lint() is True
    assert shim.calls == []
    assert "cfn-lint not installed" in _text(pub)


def test_cfn_lint_passes_vacuously_when_no_packaged_template_exists(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    shim = _SubprocessShim(lambda cmd: _Completed(returncode=0))
    monkeypatch.setattr(publish_mod, "subprocess", shim)
    monkeypatch.setattr(publish_mod, "shutil", _ShutilShim("/usr/local/bin/cfn-lint"))

    pub = _publisher()
    assert pub._validate_cfn_lint() is True
    assert shim.calls == []
    assert "No packaged templates found to lint" in _text(pub)


def test_cfn_lint_fails_on_an_error_line_and_tolerates_a_warning_line(
    monkeypatch, tmp_path
):
    """`E####` at the start of a line fails the publish; `W####` does not.

    The repository carries ~112 pre-existing cfn-lint warnings, so treating them
    as failures would block every publish. The classification is by line prefix,
    which is what the next test probes for its edges.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, ".aws-sam/idp-main.yaml", "Resources: {}\n")
    monkeypatch.setattr(publish_mod, "shutil", _ShutilShim("/usr/local/bin/cfn-lint"))

    warn_only = _SubprocessShim(
        lambda cmd: _Completed(
            returncode=1, stdout="W1030 Empty default for parameter Foo\n"
        )
    )
    monkeypatch.setattr(publish_mod, "subprocess", warn_only)
    pub = _publisher()
    assert pub._validate_cfn_lint() is True
    assert "found 1 warnings (continuing)" in _text(pub)

    errored = _SubprocessShim(
        lambda cmd: _Completed(
            returncode=1,
            stdout="E3003 Required property Handler missing\nW1030 Empty default\n",
        )
    )
    monkeypatch.setattr(publish_mod, "subprocess", errored)
    pub = _publisher()
    assert pub._validate_cfn_lint() is False
    text = _text(pub)
    assert "E3003 Required property Handler missing" in text


def test_long_cfn_lint_output_is_truncated_with_a_count_of_what_was_hidden(
    monkeypatch, tmp_path
):
    """Errors cap at ten and warnings at five, each with a remainder count.

    The repository carries ~112 pre-existing warnings, so printing them all
    buried the lines that mattered. The count of hidden findings is the part that
    has to survive truncation — without it, the operator cannot tell a
    ten-error template from a two-hundred-error one.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, ".aws-sam/idp-main.yaml", "Resources: {}\n")
    monkeypatch.setattr(publish_mod, "shutil", _ShutilShim("/usr/local/bin/cfn-lint"))

    errors = "".join(f"E300{i % 10} error number {i}\n" for i in range(14))
    monkeypatch.setattr(
        publish_mod,
        "subprocess",
        _SubprocessShim(lambda cmd: _Completed(returncode=1, stdout=errors)),
    )
    pub = _publisher()
    assert pub._validate_cfn_lint() is False
    assert "... and 4 more errors" in _text(pub)

    warnings = "".join(f"W103{i % 10} warning number {i}\n" for i in range(8))
    monkeypatch.setattr(
        publish_mod,
        "subprocess",
        _SubprocessShim(lambda cmd: _Completed(returncode=1, stdout=warnings)),
    )
    pub = _publisher()
    assert pub._validate_cfn_lint() is True
    assert "found 8 warnings (continuing)" in _text(pub)
    assert "... and 3 more warnings" in _text(pub)


def test_a_rule_code_that_is_not_at_the_start_of_a_line_is_not_a_finding(
    monkeypatch, tmp_path
):
    """The classifier anchors on the line start, so resource types do not match.

    A line like `AWS::EC2::Instance E3003 ...` contains a rule code but is not a
    finding line; classifying it as an error would fail publishes over their own
    log formatting. Five digits (`E30031`) must not match either — the `\\b` is
    what stops a longer code being read as a shorter one.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, ".aws-sam/idp-main.yaml", "Resources: {}\n")
    monkeypatch.setattr(publish_mod, "shutil", _ShutilShim("/usr/local/bin/cfn-lint"))
    monkeypatch.setattr(
        publish_mod,
        "subprocess",
        _SubprocessShim(
            lambda cmd: _Completed(
                returncode=1,
                stdout=(
                    "  AWS::EC2::Instance E3003 mentioned mid-line\n"
                    "E30031 five digits\n"
                    "\n"
                    "Some prose about W1030 in the middle\n"
                ),
            )
        ),
    )

    pub = _publisher()
    assert pub._validate_cfn_lint() is True
    text = _text(pub)
    assert "found errors" not in text
    assert "warnings (continuing)" not in text


def test_a_nonzero_cfn_lint_exit_with_unclassifiable_output_is_reported_as_a_pass(
    monkeypatch, tmp_path
):
    """DEFECT: cfn-lint failing for a reason other than a finding is read as success.

    `_validate_cfn_lint` (publish.py:3341-3363) ignores the return code except
    as a trigger for parsing, and only lines matching `^E\\d{4}\\b` or
    `^W\\d{4}\\b` are collected. A cfn-lint invocation that exits nonzero
    *without* producing a finding line — an unreadable file, a bad argument, an
    internal traceback, a version whose output format changed — contributes
    nothing to either list, so the method reaches its final branch and prints
    "CloudFormation linting passed".

    Observable consequence: the publish reports the template as linted when it
    was never successfully linted. The gate turns itself off, and the only
    evidence is the absence of findings, which is indistinguishable from a clean
    template. This is the same shape as the repository's stated recurring defect
    class — a control that exists but produces no signal.

    The test pins current behaviour, including the misleading success line.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, ".aws-sam/idp-main.yaml", "Resources: {}\n")
    monkeypatch.setattr(publish_mod, "shutil", _ShutilShim("/usr/local/bin/cfn-lint"))
    monkeypatch.setattr(
        publish_mod,
        "subprocess",
        _SubprocessShim(
            lambda cmd: _Completed(
                returncode=2,
                stderr=(
                    "Traceback (most recent call last):\n"
                    "  File cfnlint/__main__.py, line 1\n"
                    "RuntimeError: could not read template\n"
                ),
            )
        ),
    )

    pub = _publisher()
    assert pub._validate_cfn_lint() is True
    assert "CloudFormation linting passed (1 templates checked)" in _text(pub)
    # The traceback is not even shown, so there is nothing in the log to notice.
    assert "RuntimeError" not in _text(pub)


def test_headless_mode_skips_the_main_template_until_it_has_been_transformed(
    monkeypatch, tmp_path
):
    """In headless mode the main template is linted only in its transformed form.

    The untransformed template still declares CloudFront/Cognito/UI resources
    that the headless transformer strips, so linting it would report errors about
    resources the release does not ship.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, ".aws-sam/idp-main.yaml", "Resources: {}\n")
    shim = _SubprocessShim(lambda cmd: _Completed(returncode=0))
    monkeypatch.setattr(publish_mod, "subprocess", shim)
    monkeypatch.setattr(publish_mod, "shutil", _ShutilShim("/usr/local/bin/cfn-lint"))

    pub = _publisher()
    pub.headless = True
    assert pub._validate_cfn_lint() is True
    assert shim.calls == []
    assert "headless transformation runs later" in _text(pub)

    # Once the transformed template exists, that is what gets linted.
    _write(tmp_path, ".aws-sam/idp-headless.yaml", "Resources: {}\n")
    shim = _SubprocessShim(lambda cmd: _Completed(returncode=0))
    monkeypatch.setattr(publish_mod, "subprocess", shim)
    pub = _publisher()
    pub.headless = True
    assert pub._validate_cfn_lint() is True
    assert [cmd[1] for cmd in shim.calls] == [".aws-sam/idp-headless.yaml"]


def test_govcloud_mode_skips_the_main_template_but_still_lints_the_nested_ones(
    monkeypatch, tmp_path
):
    """GovCloud defers the main template and has no transformed file to substitute."""
    monkeypatch.chdir(tmp_path)
    _packaged_template_tree(tmp_path)
    shim = _SubprocessShim(lambda cmd: _Completed(returncode=0))
    monkeypatch.setattr(publish_mod, "subprocess", shim)
    monkeypatch.setattr(publish_mod, "shutil", _ShutilShim("/usr/local/bin/cfn-lint"))

    pub = _publisher()
    pub.govcloud = True
    assert pub._validate_cfn_lint() is True

    linted = [cmd[1] for cmd in shim.calls]
    assert ".aws-sam/idp-main.yaml" not in linted
    assert len(linted) == 3
    assert "GovCloud transformation runs later" in _text(pub)


def test_the_headless_nested_skip_names_a_directory_that_no_longer_exists(
    monkeypatch, tmp_path
):
    """DEFECT: the headless nested-stack skip list was not renamed with the stack.

    publish.py:3307 reads `headless_skip_nested = {"appsync"} if self.headless
    else set()`, and the comment two lines above it says the stack to skip is
    "currently: nested/api-resolvers, which contains AWS::AppSync::*". The
    comment and the value disagree: the directory was renamed from
    `nested/appsync` to `nested/api-resolvers`, and the set still holds the old
    leaf name. There is no `nested/appsync` in the repository, so the skip
    matches nothing and `nested/api-resolvers` is linted in headless mode.

    Observable consequence: whichever way round it should be, the code and its
    own comment cannot both be right. If the skip is still needed, headless
    publishes lint a template whose resources the transformer removes; if it is
    not (AppSync having been removed from the product entirely), the set is dead
    code that will silently start skipping a real directory if anything is ever
    named `appsync` again.

    This test pins the current behaviour — `nested/api-resolvers` IS linted in
    headless mode — so that fixing it in either direction is a deliberate,
    visible change.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, ".aws-sam/idp-headless.yaml", "Resources: {}\n")
    _write(tmp_path, "nested/api-resolvers/.aws-sam/packaged.yaml", "Resources: {}\n")
    _write(tmp_path, "nested/appsync/.aws-sam/packaged.yaml", "Resources: {}\n")
    shim = _SubprocessShim(lambda cmd: _Completed(returncode=0))
    monkeypatch.setattr(publish_mod, "subprocess", shim)
    monkeypatch.setattr(publish_mod, "shutil", _ShutilShim("/usr/local/bin/cfn-lint"))

    pub = _publisher()
    pub.headless = True
    pub._validate_cfn_lint()

    linted = [cmd[1] for cmd in shim.calls]
    assert (
        os.path.join("nested", "api-resolvers", ".aws-sam", "packaged.yaml") in linted
    )
    # The skip only bites on the old directory name.
    assert os.path.join("nested", "appsync", ".aws-sam", "packaged.yaml") not in linted


def test_cfn_lint_is_skipped_entirely_when_linting_is_disabled(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _packaged_template_tree(tmp_path)
    shim = _SubprocessShim(lambda cmd: _Completed(returncode=1))
    monkeypatch.setattr(publish_mod, "subprocess", shim)

    pub = _publisher()
    pub.lint_enabled = False
    assert pub._validate_cfn_lint() is True
    assert shim.calls == []


# ---------------------------------------------------------------------------
# build_main_template
# ---------------------------------------------------------------------------

_TEMPLATE_WITH_TOKENS = """\
Description: IDP <VERSION> built <BUILD_DATE_TIME>
Parameters:
  ArtifactBucket:
    Default: <ARTIFACT_BUCKET_TOKEN>
  ArtifactPrefix:
    Default: <ARTIFACT_PREFIX_TOKEN>
  PublicArtifactsBucket:
    Default: <PUBLIC_ARTIFACTS_BUCKET_TOKEN>
  PublicArtifactsPrefix:
    Default: <PUBLIC_ARTIFACTS_PREFIX_TOKEN>
Resources:
  WebUI:
    Properties:
      Zip: <WEBUI_ZIPFILE_TOKEN>
  Unified:
    Properties:
      Source: <UNIFIED_SOURCE_ZIPFILE_TOKEN>
      ImageVersion: <UNIFIED_IMAGE_VERSION>
  BaseLayer:
    Properties:
      Key: layers/<IDP_COMMON_BASE_LAYER_ZIP>
  ReportingLayer:
    Properties:
      Key: layers/<IDP_COMMON_REPORTING_LAYER_ZIP>
  AgentsLayer:
    Properties:
      Key: layers/<IDP_COMMON_AGENTS_LAYER_ZIP>
  DiscoveryLayer:
    Properties:
      Key: layers/<IDP_COMMON_MULTI_DOC_DISCOVERY_LAYER_ZIP>
  Config:
    Properties:
      Files: '<CONFIG_FILES_LIST_TOKEN>'
  Samples:
    Properties:
      Files: '<SAMPLE_FILES_LIST_TOKEN>'
  SampleFeatures:
    Properties:
      Hash: <SAMPLE_FEATURES_HASH_TOKEN>
      List: '<SAMPLE_FEATURES_LIST_TOKEN>'
"""


def _main_template_tree(root):
    _write(root, ".aws-sam/build/template.yaml", _TEMPLATE_WITH_TOKENS)
    _write(root, "src/lambda/other/index.py", "def handler(e, c): return 1\n")
    _write(root, "config_library/pattern/config.yaml", "classes: []\n")
    _write(root, "config_library/pricing.yaml", "models: []\n")


def _sam_stub(root, ok=True):
    """Stand in for `run_subprocess_with_logging`, writing SAM's output file.

    `sam package` is the step that produces `.aws-sam/idp-main.yaml`, and the
    upload afterwards is asserted on that file's bytes — so the stub has to
    create it, or the test would pass against a publisher that uploaded nothing.
    """
    calls = []

    def run(cmd, component_name, cwd=None, realtime=False):
        calls.append((list(cmd), component_name))
        if ok and "--output-template-file" in cmd:
            out = cmd[cmd.index("--output-template-file") + 1]
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            with open(out, "w") as handle:
                handle.write("PACKAGED BY SAM\n")
        return ok, _Completed(returncode=0 if ok else 1)

    return run, calls


@mock_aws
def test_build_main_template_substitutes_every_token_and_uploads_both_keys(
    monkeypatch, tmp_path, aws_credentials
):
    """Token substitution is what the main template build is *for*.

    Each token below drives something at deploy time: the artifact bucket and
    prefix tell every nested stack and Lambda where to fetch code from, the four
    layer zip names are the objects the Lambdas attach, and the config/sample
    file lists are consumed by custom resources that copy those files into the
    stack's own buckets. A token left unsubstituted deploys a stack that
    references the literal string `<ARTIFACT_BUCKET_TOKEN>`.

    The two S3 keys are asserted by reading them back: the versioned copy is
    what the Web UI's update indicator points at, and publishing it under the
    wrong name is invisible until an operator tries to upgrade.
    """
    monkeypatch.chdir(tmp_path)
    _main_template_tree(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    pub = _publisher(s3)
    pub.skip_validation = True
    pub._layer_arns = {
        "base": {"zip_name": "idp-common-base-aaaa1111.zip"},
        "reporting": {"zip_name": "idp-common-reporting-aaaa1111.zip"},
        "agents": {"zip_name": "idp-common-agents-aaaa1111.zip"},
        "multi_document_discovery": {
            "zip_name": "idp-common-multi_document_discovery-aaaa1111.zip"
        },
    }
    run, calls = _sam_stub(tmp_path)
    monkeypatch.setattr(pub, "run_subprocess_with_logging", run)

    pub.build_main_template(
        "webui-cafe0001.zip",
        "unified-source-beef0002.zip",
        [{"component": "main"}],
        sample_features_hash="feat0003",
        sample_features_list=["sample-feature"],
    )

    built = (tmp_path / ".aws-sam" / "build" / "idp-main.yaml").read_text()
    # Nothing of the form `<SOMETHING>` may survive: an unresolved placeholder is
    # deployed verbatim as a bucket name, an S3 key or a JSON list.
    assert re.findall(r"<[A-Za-z_0-9]+>", built) == []

    assert f"Description: IDP {_VERSION} built " in built
    assert f"Default: {_BUCKET}" in built
    # The two prefix tokens must not collapse into each other: the artifact
    # prefix carries the version, the public-artifacts prefix deliberately does
    # not (the update-check resolver lists sibling versions under it).
    assert f"Default: {_PREFIX}/{_VERSION}\n" in built
    assert f"Default: {_PREFIX}\n" in built
    assert "Zip: webui-cafe0001.zip" in built
    assert "Source: unified-source-beef0002.zip" in built
    # The image version is the hash carved out of the source zip filename.
    assert "ImageVersion: beef0002" in built
    assert "Key: layers/idp-common-base-aaaa1111.zip" in built
    assert "Key: layers/idp-common-multi_document_discovery-aaaa1111.zip" in built
    assert "Hash: feat0003" in built
    assert "List: '[\"sample-feature\"]'" in built
    # The config file list is the sorted relative paths under config_library.
    assert '\'["pattern/config.yaml", "pricing.yaml"]\'' in built

    # Both templates land in S3 carrying the packaged bytes.
    for key in (
        f"{_PREFIX}/idp-main.yaml",
        f"{_PREFIX}/idp-main_{_VERSION}.yaml",
    ):
        assert s3.get_object(Bucket=_BUCKET, Key=key)["Body"].read() == (
            b"PACKAGED BY SAM\n"
        )
    # And the version pointer the update indicator reads.
    s3.head_object(Bucket=_BUCKET, Key=f"{_PREFIX}/idp-main-latest.json")

    # sam build then sam package, in that order, against the right files.
    assert [name for _, name in calls] == [
        "Main template SAM build",
        "Main template SAM package",
    ]
    build_cmd = calls[0][0]
    assert build_cmd == [
        "sam",
        "build",
        "--parallel",
        "--template-file",
        "template.yaml",
    ]
    package_cmd = calls[1][0]
    assert package_cmd[:4] == [
        "sam",
        "package",
        "--template-file",
        "idp-main.yaml",
    ]
    assert package_cmd[package_cmd.index("--s3-bucket") + 1] == _BUCKET
    assert package_cmd[package_cmd.index("--s3-prefix") + 1] == f"{_PREFIX}/{_VERSION}"


@mock_aws
def test_the_container_flag_is_appended_to_the_sam_build_only_when_set(
    monkeypatch, tmp_path, aws_credentials
):
    """`--use-container` changes what SAM builds against, so its presence matters."""
    monkeypatch.chdir(tmp_path)
    _main_template_tree(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    pub = _publisher(s3)
    pub.skip_validation = True
    pub.use_container_flag = "--use-container"
    run, calls = _sam_stub(tmp_path)
    monkeypatch.setattr(pub, "run_subprocess_with_logging", run)

    pub.build_main_template(
        "webui.zip", "unified-source-x.zip", [{"component": "main"}]
    )

    assert calls[0][0][-1] == "--use-container"


@mock_aws
def test_an_up_to_date_main_template_is_uploaded_without_being_rebuilt(
    monkeypatch, tmp_path, aws_credentials
):
    """With nothing to rebuild, SAM is never invoked and the existing package ships.

    This is the fast path most publishes take. It still has to upload — a
    template present locally but absent from S3 (a new version prefix) must be
    put — so the check is that the object appears without `sam` running.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, ".aws-sam/idp-main.yaml", "ALREADY PACKAGED\n")
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    pub = _publisher(s3)
    pub.skip_validation = True
    run, calls = _sam_stub(tmp_path)
    monkeypatch.setattr(pub, "run_subprocess_with_logging", run)

    pub.build_main_template("webui.zip", "unified-source-x.zip", [])

    assert calls == []
    assert "Main template is up to date" in _text(pub)
    assert (
        s3.get_object(Bucket=_BUCKET, Key=f"{_PREFIX}/idp-main.yaml")["Body"].read()
        == b"ALREADY PACKAGED\n"
    )


@mock_aws
def test_a_failed_build_deletes_the_main_checksum_before_exiting(
    monkeypatch, tmp_path, aws_credentials
):
    """The checksum must not survive a failure, or the next run skips the rebuild.

    This is the most consequential error path in the method: `.checksum` left in
    place after a failed build is precisely the state in which the *following*
    publish believes `main` is current and uploads whatever stale
    `.aws-sam/idp-main.yaml` happens to be on disk.
    """
    monkeypatch.chdir(tmp_path)
    _main_template_tree(tmp_path)
    _write(tmp_path, ".checksum", '{"combined": "stale"}')
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    pub = _publisher(s3)
    run, calls = _sam_stub(tmp_path, ok=False)
    monkeypatch.setattr(pub, "run_subprocess_with_logging", run)

    with pytest.raises(SystemExit) as excinfo:
        pub.build_main_template(
            "webui.zip", "unified-source-x.zip", [{"component": "main"}]
        )

    assert excinfo.value.code == 1
    assert not (tmp_path / ".checksum").exists()
    assert "SAM build failed" in _text(pub)
    # It stopped at the build; packaging was never attempted.
    assert [name for _, name in calls] == ["Main template SAM build"]


@mock_aws
def test_a_failed_sam_package_deletes_the_checksum_after_the_build_succeeded(
    monkeypatch, tmp_path, aws_credentials
):
    """Packaging is a separate failure from building, and reaches the same handler.

    Worth separating because the build having succeeded means `.aws-sam/build`
    now holds a plausible tree; leaving `.checksum` in place would let the next
    publish package *that* tree without rebuilding it, so the checksum deletion
    matters more here than on a build failure, not less.
    """
    monkeypatch.chdir(tmp_path)
    _main_template_tree(tmp_path)
    _write(tmp_path, ".checksum", '{"combined": "stale"}')
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    calls = []

    def run(cmd, component_name, cwd=None, realtime=False):
        calls.append(component_name)
        return component_name != "Main template SAM package", _Completed()

    pub = _publisher(s3)
    monkeypatch.setattr(pub, "run_subprocess_with_logging", run)

    with pytest.raises(SystemExit) as excinfo:
        pub.build_main_template(
            "webui.zip", "unified-source-x.zip", [{"component": "main"}]
        )

    assert excinfo.value.code == 1
    assert calls == ["Main template SAM build", "Main template SAM package"]
    assert not (tmp_path / ".checksum").exists()
    assert "SAM package failed" in _text(pub)
    # The token-substituted template was written before packaging was attempted.
    assert (tmp_path / ".aws-sam" / "build" / "idp-main.yaml").is_file()


@mock_aws
def test_a_successful_package_that_produced_no_output_file_is_caught(
    monkeypatch, tmp_path, aws_credentials
):
    """A "successful" package with no packaged template must not upload nothing.

    `sam package` reporting success without writing its output file is what a
    disk-full or a permission problem in `.aws-sam/` looks like. Without this
    check the upload would raise a less intelligible error from boto3, and on an
    S3 client that tolerated it the release would advertise a key that was never
    written.
    """
    monkeypatch.chdir(tmp_path)
    _main_template_tree(tmp_path)
    _write(tmp_path, ".checksum", '{"combined": "stale"}')
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    pub = _publisher(s3)
    pub.skip_validation = True
    # Reports success but writes no `--output-template-file`.
    monkeypatch.setattr(
        pub,
        "run_subprocess_with_logging",
        lambda cmd, component_name, cwd=None, realtime=False: (True, _Completed()),
    )

    with pytest.raises(SystemExit) as excinfo:
        pub.build_main_template(
            "webui.zip", "unified-source-x.zip", [{"component": "main"}]
        )

    assert excinfo.value.code == 1
    assert not (tmp_path / ".checksum").exists()
    text = _text(pub)
    assert "Packaged template not found at .aws-sam/idp-main.yaml" in text


@mock_aws
def test_a_crash_inside_the_sam_build_step_still_deletes_the_checksum(
    monkeypatch, tmp_path, aws_credentials
):
    """An exception is as fatal as a nonzero exit, and must clear the checksum too.

    `run_subprocess_with_logging` raising — `sam` not on PATH, an OSError
    spawning it — takes a different route out of the method than a failed exit
    code, and that route has to reach the same handler or a crashed build would
    leave `main` marked current.
    """
    monkeypatch.chdir(tmp_path)
    _main_template_tree(tmp_path)
    _write(tmp_path, ".checksum", '{"combined": "stale"}')
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    def exploding(cmd, component_name, cwd=None, realtime=False):
        raise FileNotFoundError("sam: command not found")

    pub = _publisher(s3)
    monkeypatch.setattr(pub, "run_subprocess_with_logging", exploding)

    with pytest.raises(SystemExit) as excinfo:
        pub.build_main_template(
            "webui.zip", "unified-source-x.zip", [{"component": "main"}]
        )

    assert excinfo.value.code == 1
    assert not (tmp_path / ".checksum").exists()
    assert "sam: command not found" in _text(pub)


@mock_aws
def test_the_uploaded_template_url_is_the_one_handed_to_cloudformation(
    monkeypatch, tmp_path, aws_credentials
):
    """Validation runs against the *uploaded* unversioned key, over HTTPS.

    CloudFormation fetches the template itself, so the URL has to name the
    bucket, the region and the unversioned key — a versioned URL would validate
    the wrong object on a republish, and a path-style URL for the wrong region
    would 301.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, ".aws-sam/idp-main.yaml", "ALREADY PACKAGED\n")
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    validated = []

    class _AcceptingCloudFormation:
        def validate_template(self, **kwargs):
            validated.append(kwargs)
            return {"Parameters": []}

    pub = _publisher(s3)
    pub.cf_client = _AcceptingCloudFormation()
    pub.build_main_template("webui.zip", "unified-source-x.zip", [])

    assert validated == [
        {
            "TemplateURL": (
                f"https://s3.{_REGION}.amazonaws.com/{_BUCKET}/{_PREFIX}/idp-main.yaml"
            )
        }
    ]
    assert "Template validation passed" in _text(pub)


@mock_aws
def test_a_python_syntax_error_in_src_stops_the_main_build(
    monkeypatch, tmp_path, aws_credentials
):
    """The syntax gate runs before SAM, so a broken handler never gets packaged."""
    monkeypatch.chdir(tmp_path)
    _main_template_tree(tmp_path)
    _write(tmp_path, "src/lambda/bad/index.py", "def broken(:\n    pass\n")
    _write(tmp_path, ".checksum", '{"combined": "stale"}')
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    pub = _publisher(s3)
    run, calls = _sam_stub(tmp_path)
    monkeypatch.setattr(pub, "run_subprocess_with_logging", run)

    with pytest.raises(SystemExit):
        pub.build_main_template(
            "webui.zip", "unified-source-x.zip", [{"component": "main"}]
        )

    assert calls == []
    assert not (tmp_path / ".checksum").exists()
    assert "Python syntax validation failed" in _text(pub)


@mock_aws
def test_a_cloudformation_validation_error_deletes_the_checksum_and_exits(
    monkeypatch, tmp_path, aws_credentials
):
    """A template CloudFormation rejects must not be recorded as a good build.

    `validate_template` is reached only when `--skip-validation` is off, and its
    `ClientError` has its own handler — the one that distinguishes "your
    template is invalid" from every other failure in the method.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, ".aws-sam/idp-main.yaml", "ALREADY PACKAGED\n")
    _write(tmp_path, ".checksum", '{"combined": "stale"}')
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    class _RejectingCloudFormation:
        def validate_template(self, **kwargs):
            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationError",
                        "Message": "Template format error: unresolved token",
                    }
                },
                "ValidateTemplate",
            )

    pub = _publisher(s3)
    pub.cf_client = _RejectingCloudFormation()

    with pytest.raises(SystemExit) as excinfo:
        pub.build_main_template("webui.zip", "unified-source-x.zip", [])

    assert excinfo.value.code == 1
    assert not (tmp_path / ".checksum").exists()
    text = _text(pub)
    assert "CloudFormation template validation failed" in text
    assert "unresolved token" in text


@mock_aws
def test_skip_validation_reports_the_skip_rather_than_silently_omitting_it(
    monkeypatch, tmp_path, aws_credentials
):
    """`--skip-validation` must be visible in the log; a silent skip reads as a pass."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, ".aws-sam/idp-main.yaml", "ALREADY PACKAGED\n")
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    pub = _publisher(s3)
    pub.skip_validation = True
    pub.cf_client = None  # would raise if validation were attempted
    pub.build_main_template("webui.zip", "unified-source-x.zip", [])

    assert "Skipping CloudFormation template validation" in _text(pub)
