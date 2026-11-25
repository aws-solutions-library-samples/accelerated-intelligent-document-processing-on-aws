# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The solution profile stays valid, stays in step with the code, and builds the same
bytes every time.

``scripts/check_solution_profile.py`` (``make solution-profile-check``) is the gate;
this module proves it can go red. Each cross-check is exercised two ways:

* against a **copy of the real source file with one item added** — a hook point in
  the generated reachability module, a preset directory, an extension branch in
  ``_detect_file_type``, a ``--region`` line in the release script — so the test
  fails if the universe derivation stops seeing the kind of change it exists for;
* against the real profile, which must pass as it stands.

The vendored contract is pinned by hash, which is the ratchet behind the vendored
validator's ruff scope exclusion (there is no type-check exclusion: basedpyright reads
it and reports nothing). The exclusion is only honest while the copy is the
contract's own bytes.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE = REPO_ROOT / "solution-profile"


def _load(name: str):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check = _load("check_solution_profile")
build = _load("build_solution_profile")


@pytest.fixture(scope="module")
def profile():
    return check.Profile.load(PROFILE)


def _copy(rel: str, root: Path) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO_ROOT / rel, target)
    return target


def _tree_with(tmp_path: Path, replace: dict[str, str]) -> Path:
    """A repository root that links every top-level entry of this checkout except the
    ones in ``replace``, which are written with the given text. Evidence paths then
    still resolve while VERSION or a source file can differ."""
    root = tmp_path / "repo"
    root.mkdir()
    for entry in REPO_ROOT.iterdir():
        if entry.name in {".git", ".venv", "node_modules"} or entry.name in replace:
            continue
        (root / entry.name).symlink_to(entry)
    for name, text in replace.items():
        (root / name).write_text(text, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# The real profile
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_profile_passes_the_reference_validator_and_the_cross_checks(capsys):
    status = check.main(["--repo-root", str(REPO_ROOT)])
    out = capsys.readouterr().out
    assert status == 0, out


@pytest.mark.unit
def test_the_vendored_contract_is_byte_identical_to_its_pinned_digests():
    readme = (PROFILE / "schema" / "README.md").read_text(encoding="utf-8")
    block = re.search(
        r"<!-- vendored-sha256:start -->(.*?)<!-- vendored-sha256:end -->", readme, re.S
    )
    assert block, "solution-profile/schema/README.md lost its vendored-sha256 markers"
    pinned = dict(
        (path, digest)
        for digest, path in re.findall(r"^([0-9a-f]{64})  (\S+)$", block.group(1), re.M)
    )
    assert set(pinned) == {
        "v1/README.md",
        "v1/solution-profile.schema.json",
        "v1/validate_profile.py",
    }
    for rel, digest in pinned.items():
        actual = hashlib.sha256((PROFILE / "schema" / rel).read_bytes()).hexdigest()
        assert actual == digest, (
            f"solution-profile/schema/{rel} differs from the vendored contract. It must stay "
            f"byte-identical: its ruff scope exclusion rests on that. Re-copy it from "
            f"the contract, or update the digest only when the contract itself changed."
        )


@pytest.mark.unit
def test_the_manifest_does_not_carry_build_stamped_fields():
    manifest = yaml.safe_load(
        (PROFILE / "solution-profile.yaml").read_text(encoding="utf-8")
    )
    assert set(manifest["source"]) == {"repository"}, (
        "source.tag/commit are stamped by the build only"
    )


# ---------------------------------------------------------------------------
# Version conversion and the release rules
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("pep440", "contract", "final"),
    [
        ("0.6.16.dev1", "0.6.16-dev1", False),
        ("0.6.15.rc1", "0.6.15-rc1", False),
        ("0.6.15rc1", "0.6.15-rc1", False),
        ("0.6.15", "0.6.15", True),
        ("1.0.0.post1", "1.0.0-post1", False),
    ],
)
def test_contract_version(pep440, contract, final):
    assert check.contract_version(pep440) == contract
    assert check.is_final_release(pep440) is final


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["v0.6.15", "0.6", "0.6.15 beta", ""])
def test_contract_version_rejects_a_malformed_version(bad):
    with pytest.raises(ValueError):
        check.contract_version(bad)


@pytest.mark.unit
def test_stamp_manifest_version_rewrites_only_the_version_line(tmp_path):
    manifest = tmp_path / "solution-profile.yaml"
    shutil.copyfile(PROFILE / "solution-profile.yaml", manifest)
    before = manifest.read_text(encoding="utf-8")
    assert check.stamp_manifest_version(manifest, "9.8.7.dev3")
    after = manifest.read_text(encoding="utf-8")
    assert yaml.safe_load(after)["version"] == "9.8.7-dev3"
    changed = [line for line in after.splitlines() if line not in before.splitlines()]
    assert changed == ["version: 9.8.7-dev3"]


@pytest.mark.unit
def test_make_version_refuses_a_version_without_a_contract_form_before_writing(
    tmp_path,
):
    """`1.0` is valid PEP 440 but has no X.Y.Z contract form. The refusal must come
    before the recipe stamps anything, or the tree is left half-bumped."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "tests"))
    import test_make_version_lockfile as bump

    root = bump._scratch_tree(tmp_path)
    before = {rel: (root / rel).read_bytes() for rel in bump._stamped_paths()}
    result = subprocess.run(  # noqa: S603
        ["make", "version", "V=1.0", f"PYTHON={sys.executable}"],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "cannot carry this version" in result.stdout + result.stderr
    assert "Updating version" not in result.stdout
    assert {rel: (root / rel).read_bytes() for rel in bump._stamped_paths()} == before


@pytest.mark.unit
def test_stamp_version_reports_a_bad_version_without_a_traceback(tmp_path, capsys):
    manifest = tmp_path / "solution-profile.yaml"
    shutil.copyfile(PROFILE / "solution-profile.yaml", manifest)
    before = manifest.read_bytes()
    assert check.main(["--profile-dir", str(tmp_path), "--stamp-version", "1.0"]) == 1
    assert "ERROR: could not stamp" in capsys.readouterr().out
    assert manifest.read_bytes() == before


@pytest.mark.unit
def test_a_final_version_applies_the_release_rules_and_says_what_to_do(
    tmp_path, capsys
):
    """A release-prep commit (VERSION final, profile not yet reviewed for it) fails
    with the remedy spelled out."""
    manifest_text = (PROFILE / "solution-profile.yaml").read_text(encoding="utf-8")
    root = _tree_with(tmp_path, {"VERSION": "9.9.9\n", "solution-profile": ""})
    (root / "solution-profile").unlink()
    shutil.copytree(PROFILE, root / "solution-profile", symlinks=True)
    (root / "solution-profile" / "solution-profile.yaml").write_text(
        re.sub(r"^version: .*$", "version: 9.9.9", manifest_text, flags=re.M),
        encoding="utf-8",
    )
    status = check.main(["--repo-root", str(root), "--contract-only"])
    out = capsys.readouterr().out
    assert status == 1
    assert "release rules ON" in out
    assert (
        "reviewed_for_version" in out and "changes: no entry for version 9.9.9" in out
    )
    assert (
        "update-solution-profile/SKILL.md" in out
        and "set reviewed_for_version: 9.9.9" in out
    )


@pytest.mark.unit
def test_a_development_version_does_not_apply_the_release_rules(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.delenv("SOLUTION_PROFILE_RELEASE", raising=False)
    version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if check.is_final_release(version):
        pytest.skip("VERSION is a final release on this commit")
    check.main(["--repo-root", str(REPO_ROOT), "--contract-only"])
    assert "release rules off" in capsys.readouterr().out


@pytest.mark.unit
def test_the_environment_forces_the_release_rules(capsys, monkeypatch):
    monkeypatch.setenv("SOLUTION_PROFILE_RELEASE", "1")
    check.main(["--repo-root", str(REPO_ROOT), "--contract-only"])
    assert "release rules ON" in capsys.readouterr().out


@pytest.mark.unit
def test_a_version_the_manifest_does_not_carry_fails(tmp_path, capsys):
    root = _tree_with(tmp_path, {"VERSION": "0.0.1.dev1\n"})
    assert check.main(["--repo-root", str(root), "--contract-only"]) == 1
    assert "does not equal" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Cross-checks: each fails when the code gains an item the profile lacks
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_new_hook_point_fails_until_the_profile_has_one(tmp_path, profile):
    module = _copy(check.HOOK_REACHABILITY, tmp_path)
    text = module.read_text(encoding="utf-8")
    text = text.replace(
        "ALL_HOOK_POINTS = frozenset(\n    {\n",
        'ALL_HOOK_POINTS = frozenset(\n    {\n        "postAssessment",\n',
        1,
    )
    text = text.replace(
        '    "pipeline": frozenset(\n        {\n',
        '    "pipeline": frozenset(\n        {\n            "postAssessment",\n',
        1,
    )
    module.write_text(text, encoding="utf-8")
    universe = check.hook_point_modes(tmp_path)
    assert universe["postAssessment"] == {"pipeline"}
    problems = check.check_hook_points(profile, universe)
    assert problems and all("postAssessment" in p for p in problems)
    assert "hook-point:postAssessment" in problems[0] and "['pipeline']" in problems[0]


@pytest.mark.unit
def test_a_hook_point_with_the_wrong_modes_fails(profile):
    universe = check.hook_point_modes(REPO_ROOT)
    universe["postOcr"] = {"bda", "pipeline"}
    problems = check.check_hook_points(profile, universe)
    assert len(problems) == 1 and "postOcr" in problems[0] and "modes" in problems[0]


@pytest.mark.unit
def test_a_new_preset_fails_until_a_capability_names_it(tmp_path, profile):
    for name in check.preset_names(REPO_ROOT):
        (tmp_path / check.PRESETS_DIR / name).mkdir(parents=True)
        (tmp_path / check.PRESETS_DIR / name / "config.yaml").write_text("{}\n")
    new = tmp_path / check.PRESETS_DIR / "insurance-claims-sample"
    new.mkdir()
    (new / "config.yaml").write_text("{}\n")
    problems = check.check_presets(profile, check.preset_names(tmp_path))
    assert len(problems) == 1
    assert (
        "preset:insurance-claims-sample" in problems[0]
        and "config_library/unified" in problems[0]
    )


@pytest.mark.unit
def test_a_new_file_format_fails_until_a_capability_names_it(tmp_path, profile):
    service = _copy(check.OCR_SERVICE, tmp_path)
    text = service.read_text(encoding="utf-8")
    anchor = '    elif ext in ["pdf"]:\n'
    assert anchor in text, "the mutation anchor moved; update this test"
    service.write_text(
        text.replace(
            anchor,
            '    elif ext == "heic":\n        return "heic"\n' + anchor,
            1,
        )
    )
    universe = check.routed_file_extensions(tmp_path)
    assert "heic" in universe
    problems = check.check_file_formats(profile, universe)
    assert len(problems) == 1 and "format:heic" in problems[0]


@pytest.mark.unit
def test_a_new_model_fails_until_a_capability_names_it(profile):
    universe = check.selectable_models(REPO_ROOT) | {"anthropic.claude-test-9"}
    problems = check.check_models(profile, universe)
    assert len(problems) == 1 and "model:anthropic.claude-test-9" in problems[0]


@pytest.mark.unit
def test_the_model_universe_agrees_with_the_model_surface_gate():
    """The check carries its own small template walker so the lint gate imports no
    test module; this pins it to the walker the model-surface gate maintains."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "tests"))
    import test_model_surface_consistency as surface

    offered: dict[str, set[str]] = {}
    for rel in surface._tracked_templates():
        # The parse is shared (both loaders map CloudFormation tags the same way);
        # what this compares is the two walks and the two template discoveries.
        doc = yaml.load(  # noqa: S506 - a yaml.SafeLoader subclass
            (REPO_ROOT / rel).read_text(encoding="utf-8"),
            Loader=check._CfnLoader,  # nosec B506 - a yaml.SafeLoader subclass
        )
        surface._walk_enums(doc, rel, offered)
    theirs = {
        check.MODEL_VARIANT_SUFFIX.sub("", surface._base_model(m))
        for m in offered
        if m not in surface.NON_MODEL_CHOICES
    }
    assert check.selectable_models(REPO_ROOT) == theirs


SCRATCH_TEMPLATE = """AWSTemplateFormatVersion: "2010-09-09"
Parameters:
  ExtractionModel:
    Type: String
    Default: us.anthropic.claude-scratch-1
    AllowedValues: [us.anthropic.claude-scratch-1, eu.amazon.nova-scratch-v1:0:flex]
Resources:
  Bucket:
    Type: AWS::S3::Bucket
    Properties:
      BucketName: !Sub "${AWS::StackName}-x"
"""


@pytest.mark.unit
def test_repo_root_decides_which_templates_offer_models(tmp_path, capsys):
    """``--repo-root`` names the tree whose templates are read, not this checkout."""
    root = _tree_with(tmp_path, {"scratch-template.yaml": SCRATCH_TEMPLATE})
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)  # noqa: S607
    subprocess.run(["git", "add", "scratch-template.yaml"], cwd=root, check=True)  # noqa: S607
    assert check.selectable_models(root) == {
        "anthropic.claude-scratch-1",
        "amazon.nova-scratch-v1:0",
    }
    assert check.main(["--repo-root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "no capability is tagged 'model:anthropic.claude-scratch-1'" in out
    assert "'model:amazon.nova-scratch-v1:0'" in out
    assert "which no tracked template offers any more" in out


@pytest.mark.unit
def test_a_model_no_longer_offered_fails(profile):
    universe = check.selectable_models(REPO_ROOT)
    removed = sorted(universe)[0]
    problems = check.check_models(profile, universe - {removed})
    assert len(problems) == 1 and "no tracked template offers" in problems[0]


@pytest.mark.unit
def test_a_new_launch_region_fails_until_the_deployment_option_lists_it(
    tmp_path, profile
):
    script = _copy(check.RELEASE_SCRIPT, tmp_path)
    script.write_text(
        script.read_text(encoding="utf-8")
        + "idp-cli publish --source-dir . --bucket-basename b --prefix p --region ap-southeast-2 --public\n"
        + "# idp-cli publish --region eu-west-9 is commented out and must not count\n",
        encoding="utf-8",
    )
    universe = check.release_regions(tmp_path)
    assert "ap-southeast-2" in universe and "eu-west-9" not in universe
    problems = check.check_regions(profile, universe)
    assert len(problems) == 1 and "ap-southeast-2" in problems[0]


@pytest.mark.unit
def test_a_new_release_fails_until_changes_has_an_entry(profile):
    releases = [
        ("9.9.9", "- **Thing.** **Action:** re-run setup.\n")
    ] + check.changelog_releases(REPO_ROOT)
    problems = check.check_changes(profile, releases)
    assert len(problems) == 1 and "no entry for 9.9.9" in problems[0]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("section", "expected"),
    [
        ("- x. **Action:** none.\n", False),
        ("- x. **Action:** none; existing stacks are unaffected.\n", False),
        (
            "- x. **Action:** none for existing stacks, except that a scoped user is refused.\n",
            True,
        ),
        ("- x. **Action:** set `EnableMCP` to true to keep it.\n", True),
        ("- x. ⚠️ **Action, for 0.6.10 only: re-check the row counts.**\n", True),
        ("- x. **Action Required** before upgrading, redeploy.\n", True),
        ("- nothing to act on here.\n", False),
    ],
)
def test_has_action_item(section, expected):
    assert check.has_action_item(section) is expected


@pytest.mark.unit
def test_an_action_item_with_no_action_required_fails(profile):
    releases = check.changelog_releases(REPO_ROOT)
    version = next(
        v for v, s in releases[: check.RELEASES_COVERED] if check.has_action_item(s)
    )
    stripped = check.Profile(
        releases=[
            {**r, "action_required": []} if r.get("version") == version else r
            for r in profile.releases
        ]
    )
    problems = check.check_changes(stripped, releases)
    assert any(version in p and "action_required is empty" in p for p in problems)


@pytest.mark.unit
def test_an_evidence_anchor_that_names_no_heading_fails(tmp_path):
    profile_dir = tmp_path / "solution-profile"
    shutil.copytree(PROFILE, profile_dir)
    path = profile_dir / "limitations.yaml"
    text = path.read_text(encoding="utf-8")
    good = "docs/extraction-scaling-guide.md#2-request-too-large-refused-at-66-pages"
    assert good in text, "the mutation anchor moved; update this test"
    path.write_text(
        text.replace(good, "docs/extraction-scaling-guide.md#no-such-heading", 1)
    )
    assert check.check_evidence_anchors(REPO_ROOT, PROFILE) == []
    problems = check.check_evidence_anchors(REPO_ROOT, profile_dir)
    assert len(problems) == 1
    assert (
        "#no-such-heading" in problems[0]
        and "simple-extraction-page-limit" in problems[0]
    )


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------

COMMIT = "1234abcd" * 5


def _build_into(tmp: Path, repo_root: Path = REPO_ROOT) -> bytes:
    path = build.build(repo_root, tmp, tag="v9.9.9", commit=COMMIT)
    return path.read_bytes()


@pytest.mark.unit
def test_the_build_is_byte_identical_across_runs_and_file_mtimes(tmp_path):
    first = _build_into(tmp_path / "a")
    copy_root = tmp_path / "repo"
    copy_root.mkdir()
    shutil.copytree(PROFILE, copy_root / "solution-profile")
    for path in (copy_root / "solution-profile").rglob("*"):
        os.utime(path, (1_900_000_000, 1_900_000_000))
    second = _build_into(tmp_path / "b", copy_root)
    assert first == second
    name = next((tmp_path / "a").glob("*.tar.gz")).name
    digest = (tmp_path / "a" / f"{name}.sha256").read_text(encoding="utf-8")
    assert digest == f"{hashlib.sha256(first).hexdigest()}  {name}\n"


@pytest.mark.unit
def test_the_asset_layout(tmp_path):
    data = _build_into(tmp_path)
    manifest = yaml.safe_load(
        (PROFILE / "solution-profile.yaml").read_text(encoding="utf-8")
    )
    name = next(tmp_path.glob("*.tar.gz")).name
    assert name == f"solution-profile-{manifest['id']}-{manifest['version']}.tar.gz"
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
        names = [m.name for m in members]
        assert names == sorted(names)
        assert {n.split("/")[0] for n in names} == {"solution-profile"}
        assert not any(n.startswith("solution-profile/schema") for n in names)
        assert "solution-profile/skills/idp-solution-fit/SKILL.md" in names
        for member in members:
            assert (
                member.mtime,
                member.uid,
                member.gid,
                member.uname,
                member.gname,
            ) == (0, 0, 0, "", "")
            assert not member.pax_headers
        info = json.loads(tar.extractfile("solution-profile/build.json").read())
        stamped = yaml.safe_load(
            tar.extractfile("solution-profile/solution-profile.yaml").read()
        )
    assert info == {
        "id": manifest["id"],
        "version": manifest["version"],
        "tag": "v9.9.9",
        "commit": COMMIT,
        "schema_version": 1,
    }
    assert stamped["source"] == {
        "repository": manifest["source"]["repository"],
        "tag": "v9.9.9",
        "commit": COMMIT,
    }
    assert data[4:8] == b"\x00\x00\x00\x00", "the gzip header must carry mtime 0"


@pytest.mark.unit
def test_the_unpacked_asset_passes_the_reference_validator(tmp_path):
    tarball = build.build(REPO_ROOT, tmp_path, tag="v9.9.9", commit=COMMIT)
    assert (
        build.validate_unpacked(
            REPO_ROOT, tarball, False, build.expected_version(REPO_ROOT)
        )
        == 0
    )


@pytest.mark.unit
def test_the_unpacked_asset_fails_against_a_version_it_does_not_carry(tmp_path, capsys):
    tarball = build.build(REPO_ROOT, tmp_path, tag="v9.9.9", commit=COMMIT)
    assert build.validate_unpacked(REPO_ROOT, tarball, False, "0.0.1") == 1
    assert "does not equal" in capsys.readouterr().out


@pytest.mark.unit
def test_stamp_source_refuses_a_manifest_that_already_carries_a_commit():
    text = (
        "source:\n  repository: https://example.invalid/r\n  commit: "
        + COMMIT
        + "\nsummary: x\n"
    )
    with pytest.raises(ValueError, match="must not carry"):
        build.stamp_source(text, "v1.0.0", COMMIT)


@pytest.mark.unit
def test_stamp_source_refuses_a_short_commit():
    with pytest.raises(ValueError, match="40-character"):
        build.stamp_source(
            "source:\n  repository: https://example.invalid/r\n", "v1.0.0", "abc123"
        )
