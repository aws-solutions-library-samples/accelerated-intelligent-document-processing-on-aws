# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The four `idp-feature-cli` subcommands that touch no AWS account: `validate`,
`build`, `show-schema` and `init`.

These are the commands a feature author runs in a loop before ever publishing,
so their job is to be the place a mistake stops. The failure that matters is the
inverse of the usual one: not a crash, but a **clean exit on a broken project**.
A `validate` that exits 0 for a manifest whose template file is missing, or a
`build` that exits 0 when the bundle does not register the declared version,
sends the author on to `publish` — and the resulting feature installs into a host
stack and then does nothing, because the host looks up the bundle by the
featureId and version the manifest claimed.

So every test here asserts the exit code together with the part of the message
that tells the author what to fix, and the success cases assert that the printed
identity matches the manifest rather than merely that something was printed.
`init` is checked for the one destructive thing it could do — overwrite work in
an existing directory — and for producing a project the other three accept.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest
from click.testing import CliRunner

from idp_feature_sdk.cli import main

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_prints_the_identity_from_the_manifest(
    demo_feature_project: Path,
) -> None:
    result = CliRunner().invoke(main, ["validate", str(demo_feature_project)])
    assert result.exit_code == 0, result.output
    # The identity a host install keys on. A validate that printed a different
    # id or version than the manifest holds would mislead about which bundle the
    # host will go looking for.
    assert "demo-feature" in result.output
    assert "1.2.3" in result.output
    assert "Demo Feature" in result.output


def test_validate_reports_the_marketplace_product_code_when_declared(
    demo_feature_project: Path,
) -> None:
    result = CliRunner().invoke(main, ["validate", str(demo_feature_project)])
    assert "prod-demo" in result.output
    assert "custom-api" in result.output


def test_validate_omits_the_marketplace_line_for_a_non_marketplace_feature(
    demo_feature_project: Path,
) -> None:
    """Absent is not the same as empty here. A feature with no `marketplace:`
    block is an OSS feature; printing an empty productCode line would suggest it
    is a paid listing awaiting a code."""
    text = (demo_feature_project / "feature.yaml").read_text(encoding="utf-8")
    text = text.replace(
        "marketplace:\n  productCode: prod-demo\n"
        "  listingUrl: https://aws.amazon.com/marketplace/pp/prodview-XYZ\n",
        "",
    )
    assert "prod-demo" not in text, "fixture shape changed; rewrite this edit"
    (demo_feature_project / "feature.yaml").write_text(text, encoding="utf-8")

    result = CliRunner().invoke(main, ["validate", str(demo_feature_project)])
    assert result.exit_code == 0, result.output
    assert "productCode" not in result.output


def test_validate_refuses_a_manifest_whose_template_file_is_missing(
    demo_feature_project: Path,
) -> None:
    """The check that has to happen here: `template.path` naming a file that is
    not there. CloudFormation would only find out at publish time, after the SAM
    build, and the message there is about a packaging step rather than a typo."""
    (demo_feature_project / "template.yaml").unlink()
    result = CliRunner().invoke(main, ["validate", str(demo_feature_project)])
    assert result.exit_code == 1
    assert "template file not found" in result.output


def test_validate_refuses_an_unparseable_feature_id(
    demo_feature_project: Path,
) -> None:
    text = (demo_feature_project / "feature.yaml").read_text(encoding="utf-8")
    (demo_feature_project / "feature.yaml").write_text(
        text.replace("featureId: demo-feature", "featureId: Demo Feature!"),
        encoding="utf-8",
    )
    result = CliRunner().invoke(main, ["validate", str(demo_feature_project)])
    assert result.exit_code == 1
    assert "featureId" in result.output


def test_validate_refuses_a_directory_with_no_manifest(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["validate", str(tmp_path)])
    assert result.exit_code == 1
    assert "feature.yaml not found" in result.output


def test_validate_rejects_a_nonexistent_directory(tmp_path: Path) -> None:
    """click's own `exists=True` guard — exit code 2, not 1, because it is a
    usage error rather than a manifest error."""
    result = CliRunner().invoke(main, ["validate", str(tmp_path / "nope")])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def test_build_accepts_a_project_whose_bundle_matches_the_manifest(
    demo_feature_project: Path,
) -> None:
    result = CliRunner().invoke(main, ["build", str(demo_feature_project)])
    assert result.exit_code == 0, result.output


def test_build_refuses_a_bundle_registering_the_wrong_version(
    demo_feature_project: Path,
) -> None:
    """The defect this catches is the one that looks like success: the bundle
    builds, uploads, and the host's loader then cannot match it to the version
    latest.json advertises, so the feature's nav entry renders nothing."""
    bundle = demo_feature_project / "feature-ui" / "dist" / "ui-bundle.js"
    bundle.write_text(
        bundle.read_text(encoding="utf-8").replace("'1.2.3'", "'9.9.9'"),
        encoding="utf-8",
    )
    result = CliRunner().invoke(main, ["build", str(demo_feature_project)])
    assert result.exit_code == 1
    assert "1.2.3" in result.output
    assert "version literal" in result.output


def test_build_refuses_a_bundle_registering_the_wrong_feature_id(
    demo_feature_project: Path,
) -> None:
    bundle = demo_feature_project / "feature-ui" / "dist" / "ui-bundle.js"
    bundle.write_text(
        bundle.read_text(encoding="utf-8").replace("'demo-feature'", "'other-feature'"),
        encoding="utf-8",
    )
    result = CliRunner().invoke(main, ["build", str(demo_feature_project)])
    assert result.exit_code == 1
    assert "featureId literal" in result.output


def test_build_surfaces_a_failing_build_step_rather_than_validating_a_stale_bundle(
    demo_feature_project: Path,
) -> None:
    """A build step that exits non-zero must stop the command. Carrying on to
    validate whatever bundle happens to be on disk would pass against the
    previous build's output."""
    manifest = demo_feature_project / "feature.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "ui:\n  bundlePath: feature-ui/dist/ui-bundle.js",
            "ui:\n"
            "  bundlePath: feature-ui/dist/ui-bundle.js\n"
            "  build:\n"
            "    - argv: ['false']\n",
        ),
        encoding="utf-8",
    )
    result = CliRunner().invoke(main, ["build", str(demo_feature_project)])
    assert result.exit_code == 1
    assert "ui.build[0]" in result.output


# ---------------------------------------------------------------------------
# show-schema
# ---------------------------------------------------------------------------


def test_show_schema_emits_the_packaged_schema_as_parseable_json() -> None:
    """The output is meant to be piped into an editor's JSON-schema config, so it
    has to be machine-readable JSON with no Rich markup or wrapping — and it has
    to be the schema the loader actually validates against, not a copy."""
    from idp_feature_sdk.manifest import _SCHEMA

    result = CliRunner().invoke(main, ["show-schema"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == _SCHEMA
    assert "featureId" in _SCHEMA["properties"]


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@pytest.fixture
def cwd_with_feature_template(tmp_path: Path, monkeypatch) -> Path:
    """A checkout-shaped tree with a minimal `feature-template/` that `init`
    discovers by walking upward, plus the cwd set inside it."""
    root = tmp_path / "repo"
    template = root / "feature-platform" / "feature-template"
    template.mkdir(parents=True)
    (template / "feature.yaml").write_text(
        dedent("""
            featureId: my-feature
            displayName: My Feature
            version: 0.1.0
            template:
              path: template.yaml
            ui:
              bundlePath: feature-ui/dist/ui-bundle.js
        """).strip()
        + "\n",
        encoding="utf-8",
    )
    (template / "template.yaml").write_text(
        "Description: my-feature v0.1.0\n", encoding="utf-8"
    )
    (template / "node_modules").mkdir()
    (template / "node_modules" / "junk.js").write_text("x", encoding="utf-8")
    work = root / "work" / "deeper"
    work.mkdir(parents=True)
    monkeypatch.chdir(work)
    return root


def test_init_substitutes_the_placeholders_throughout(
    cwd_with_feature_template: Path, tmp_path: Path
) -> None:
    """Every occurrence of the template's own id/name/version must be replaced.
    A leftover `my-feature` in template.yaml or feature.yaml is the classic
    "installs and does nothing" cause: the bundle registers under one id and the
    host's catalog row names another."""
    target = tmp_path / "new-feature"
    result = CliRunner().invoke(
        main,
        [
            "init",
            str(target),
            "--feature-id",
            "claims-review",
            "--display-name",
            "Claims Review",
            "--version",
            "2.0.0",
        ],
    )
    assert result.exit_code == 0, result.output

    manifest = (target / "feature.yaml").read_text(encoding="utf-8")
    assert "featureId: claims-review" in manifest
    assert "displayName: Claims Review" in manifest
    assert "version: 2.0.0" in manifest
    assert "my-feature" not in manifest
    assert "My Feature" not in manifest

    template = (target / "template.yaml").read_text(encoding="utf-8")
    assert template.strip() == "Description: claims-review v2.0.0"

    # node_modules is skipped rather than copied.
    assert not (target / "node_modules").exists()


def test_init_defaults_the_version_and_prints_the_next_steps(
    cwd_with_feature_template: Path, tmp_path: Path
) -> None:
    target = tmp_path / "defaulted"
    result = CliRunner().invoke(
        main,
        [
            "init",
            str(target),
            "--feature-id",
            "abc",
            "--display-name",
            "ABC",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "version: 0.1.0" in (target / "feature.yaml").read_text(encoding="utf-8")
    # The printed follow-up commands are how an author gets from here to a
    # publish; they name the scaffolded directory, not the template's.
    assert "idp-feature-cli validate" in result.output
    assert str(target) in result.output


def test_init_refuses_to_overwrite_an_existing_directory(
    cwd_with_feature_template: Path, tmp_path: Path
) -> None:
    """The one irreversible thing this command could do. The guard must fire on a
    directory that merely exists, before anything is copied into it."""
    target = tmp_path / "already-there"
    target.mkdir()
    (target / "my-work.txt").write_text("precious", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["init", str(target), "--feature-id", "abc", "--display-name", "ABC"],
    )
    assert result.exit_code == 1
    assert (target / "my-work.txt").read_text(encoding="utf-8") == "precious"
    assert not (target / "feature.yaml").exists()


def test_init_reports_a_missing_feature_template_rather_than_a_traceback(
    tmp_path: Path, monkeypatch
) -> None:
    """Run outside a checkout there is nothing to copy. The message has to say
    that, since the template ships in the repository and not in the wheel."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        main,
        [
            "init",
            str(tmp_path / "x"),
            "--feature-id",
            "abc",
            "--display-name",
            "ABC",
        ],
    )
    assert result.exit_code == 1
    assert "feature-template" in result.output


def test_init_produces_a_project_that_validate_accepts(
    cwd_with_feature_template: Path, tmp_path: Path
) -> None:
    """The end-to-end contract between the two commands: whatever `init` writes,
    `validate` must accept without the author editing anything first."""
    target = tmp_path / "round-trip"
    assert (
        CliRunner()
        .invoke(
            main,
            [
                "init",
                str(target),
                "--feature-id",
                "round-trip",
                "--display-name",
                "Round Trip",
            ],
        )
        .exit_code
        == 0
    )
    # The scaffolded manifest declares a bundle path that is built later, so
    # give it a bundle registering the scaffolded identity.
    bundle = target / "feature-ui" / "dist" / "ui-bundle.js"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_text(
        "window.IdpFeatures.register('round-trip', {version: '0.1.0'});",
        encoding="utf-8",
    )
    result = CliRunner().invoke(main, ["validate", str(target)])
    assert result.exit_code == 0, result.output
    assert "round-trip" in result.output
