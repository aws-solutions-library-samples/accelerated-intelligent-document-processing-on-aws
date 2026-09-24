# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which objects a publish puts in the feature bucket, under which keys, and what
gets baked into the template on the way — `FeaturePublisher._upload_version_artifacts`
and the build/package steps around it.

The host reads a published feature entirely through keys it derives itself. The
ui-deployer custom resource fetches the UI bundle at
`<base>/<version>/ui-bundle.js`; it fetches a vertical-product config preset at
`<base>/<version>/<the path feature.yaml declared>`; the agent source zip at
`<base>/<version>/agent-source.zip`; and the template it deploys reads its own
bucket and prefix from values *baked into the file* rather than from CloudFormation
parameters, because the console's "Update stack" wizard drops parameters when the
template changes. Every one of those is a string agreement with code in another
repository tree, and getting one wrong produces a feature that installs cleanly
and then does nothing: a config preset that is never applied, a nav entry whose
bundle 404s, an agent with no source.

Three of those agreements are asserted here in the form that would break them.
The config preset must land under the **version** prefix, because two versions of
a pack otherwise overwrite each other's presets and an upgrade silently applies
the old one. The `licenseMode` token must bake to `none` when the manifest says
nothing, since that field shipped once as a literal placeholder on this exact
code path — one of the two publish paths learned about a newly-added token and
the other did not. And the missing-token warnings are the only signal an author
gets that their template will not survive a version bump, so a template with the
tokens must produce *no* warning and a template without them must produce one
naming the token.

The uploads run against moto and the template-baking path is driven directly with
an already-packaged template, so these tests do not shell out to the SAM CLI.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from textwrap import dedent

import boto3
import pytest
from rich.console import Console

from idp_feature_sdk.bundle import validate_bundle
from idp_feature_sdk.manifest import (
    AgentSourceSpec,
    ConfigPresetSpec,
    FeatureManifest,
    load_manifest,
)
from idp_feature_sdk.publisher import FeaturePublisher

pytestmark = pytest.mark.unit

_FEATURE_ID = "demo-feature"
_VERSION = "1.2.3"
_BASE = f"extensions/{_FEATURE_ID}"
_VERSION_PREFIX = f"{_BASE}/{_VERSION}/"


@dataclasses.dataclass
class _Upload:
    """One call captured from the publisher's artifact upload."""

    manifest: FeatureManifest
    artifacts: list
    console_text: str
    bucket: str


def _run_upload(
    project: Path,
    bucket: str,
    *,
    manifest: FeatureManifest | None = None,
    make_public: bool = False,
    template_path: Path | None = None,
) -> _Upload:
    """Drive ``_upload_version_artifacts`` with a template already 'packaged'.

    Calling this rather than ``publish()`` keeps the SAM CLI out of the loop; the
    method is the one that decides every key, content type and baked token, and
    ``publish()`` only chooses which template file to hand it.
    """
    manifest = manifest or load_manifest(project)
    bundle_info = validate_bundle(
        project / manifest.ui.bundlePath, manifest.featureId, manifest.version
    )
    console = Console(record=True, width=400)
    publisher = FeaturePublisher(project, console=console)
    artifacts = publisher._upload_version_artifacts(
        s3=boto3.client("s3", region_name="us-east-1"),
        bucket=bucket,
        extension_base=_BASE,
        version_prefix=_VERSION_PREFIX,
        manifest=manifest,
        bundle_info=bundle_info,
        make_public=make_public,
        packaged_template_path=template_path or (project / manifest.template.path),
    )
    return _Upload(manifest, artifacts, console.export_text(), bucket)


def _keys(bucket: str) -> set[str]:
    s3 = boto3.client("s3", region_name="us-east-1")
    return {o["Key"] for o in s3.list_objects_v2(Bucket=bucket).get("Contents", [])}


def _get(bucket: str, key: str) -> bytes:
    s3 = boto3.client("s3", region_name="us-east-1")
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read()


def _content_type(bucket: str, key: str) -> str:
    s3 = boto3.client("s3", region_name="us-east-1")
    return s3.head_object(Bucket=bucket, Key=key)["ContentType"]


def _add_config_preset(project: Path, path: str, body: str) -> None:
    preset = project / path
    preset.parent.mkdir(parents=True, exist_ok=True)
    preset.write_text(body, encoding="utf-8")
    manifest = project / "feature.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + f"\nconfigPreset:\n  path: {path}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# The baseline artifact set
# ---------------------------------------------------------------------------


def test_the_four_always_present_artifacts_land_where_the_host_looks(
    demo_feature_project: Path, feature_bucket: str
) -> None:
    """Template version-free at the base, everything else under `<version>/`.
    A template published under the version prefix would make the catalog's
    `templateKey` — which carries no version — point at nothing."""
    _run_upload(demo_feature_project, feature_bucket)
    assert _keys(feature_bucket) == {
        f"{_BASE}/template.yaml",
        f"{_VERSION_PREFIX}ui-bundle.js",
        f"{_VERSION_PREFIX}manifest.json",
        f"{_VERSION_PREFIX}sha256.txt",
    }


def test_each_artifact_carries_the_content_type_its_reader_needs(
    demo_feature_project: Path, feature_bucket: str
) -> None:
    """The UI bundle is fetched by the browser via a script tag; served as
    `binary/octet-stream` (S3's default when none is set) some browsers refuse to
    execute it, and the feature's nav entry renders blank."""
    _run_upload(demo_feature_project, feature_bucket)
    assert _content_type(feature_bucket, f"{_BASE}/template.yaml") == (
        "application/x-yaml"
    )
    assert _content_type(feature_bucket, f"{_VERSION_PREFIX}ui-bundle.js") == (
        "application/javascript"
    )
    assert _content_type(feature_bucket, f"{_VERSION_PREFIX}manifest.json") == (
        "application/json"
    )
    assert _content_type(feature_bucket, f"{_VERSION_PREFIX}sha256.txt") == "text/plain"


def test_the_returned_artifact_list_matches_what_was_uploaded(
    demo_feature_project: Path, feature_bucket: str
) -> None:
    """The returned list is what the CLI prints and what the sha256 manifest is
    built from, so a size or digest computed from the wrong file would be
    reported as the published artifact's."""
    result = _run_upload(demo_feature_project, feature_bucket)
    for entry in result.artifacts:
        body = _get(feature_bucket, entry["key"])
        assert entry["size"] == len(body), entry["key"]
        assert entry["sha256"] == hashlib.sha256(body).hexdigest(), entry["key"]


def test_the_sha256_file_names_every_artifact_by_basename(
    demo_feature_project: Path, feature_bucket: str
) -> None:
    """One line per artifact, `<digest>  <filename>` — the shape `sha256sum -c`
    reads. The sha256.txt object itself is written after the list is built, so it
    is the one artifact not in it."""
    result = _run_upload(demo_feature_project, feature_bucket)
    lines = _get(feature_bucket, f"{_VERSION_PREFIX}sha256.txt").decode().splitlines()
    assert [line.split("  ", 1)[1] for line in lines] == [
        "template.yaml",
        "ui-bundle.js",
        "manifest.json",
    ]
    digests = [line.split("  ", 1)[0] for line in lines]
    # The manifest covers the artifacts uploaded before it; sha256.txt is itself
    # appended to the returned list afterwards and cannot contain its own digest.
    assert digests == [a["sha256"] for a in result.artifacts[:-1]]
    assert result.artifacts[-1]["key"].endswith("sha256.txt")
    assert all(len(d) == 64 for d in digests)


def test_the_temporary_baked_files_are_removed_from_the_project(
    demo_feature_project: Path, feature_bucket: str
) -> None:
    """The baked template, manifest and sha256 are written into the project
    directory before upload. Leaving them behind puts generated files into the
    author's working tree, and a subsequent `sam build` would package them."""
    _run_upload(demo_feature_project, feature_bucket)
    leftovers = sorted(p.name for p in demo_feature_project.glob(".idp-feature-sdk-*"))
    assert leftovers == []


def test_make_public_tags_every_upload_public_read(
    demo_feature_project: Path, feature_bucket: str
) -> None:
    """A cross-account install fetches these over plain HTTPS. One artifact left
    private is enough to fail the install — and it fails in the *installing*
    account, which the publisher never sees."""
    _run_upload(demo_feature_project, feature_bucket, make_public=True)
    s3 = boto3.client("s3", region_name="us-east-1")
    for key in _keys(feature_bucket):
        grants = s3.get_object_acl(Bucket=feature_bucket, Key=key)["Grants"]
        assert any(
            g.get("Grantee", {}).get("URI", "").endswith("AllUsers")
            and g.get("Permission") == "READ"
            for g in grants
        ), key


def test_private_is_the_default(
    demo_feature_project: Path, feature_bucket: str
) -> None:
    _run_upload(demo_feature_project, feature_bucket)
    s3 = boto3.client("s3", region_name="us-east-1")
    grants = s3.get_object_acl(Bucket=feature_bucket, Key=f"{_BASE}/template.yaml")[
        "Grants"
    ]
    assert not any(
        g.get("Grantee", {}).get("URI", "").endswith("AllUsers") for g in grants
    )


# ---------------------------------------------------------------------------
# Config preset — the vertical-product payload
# ---------------------------------------------------------------------------


def test_the_config_preset_lands_under_the_version_prefix_at_its_declared_path(
    demo_feature_project: Path, feature_bucket: str
) -> None:
    """Two properties in one key. Under the **version** prefix, because a
    version-free preset key means publishing v2 overwrites v1's preset and a host
    still on v1 applies v2's config on its next install. And at the **declared
    relative path**, because that is the key the ui-deployer custom resource
    builds from the manifest it read — flattening it to a basename makes the
    fetch 404, the install still succeeds, and the pack's config is never
    applied."""
    _add_config_preset(
        demo_feature_project,
        "config/claims/config.yaml",
        "classes:\n  - name: Claim\n",
    )
    _run_upload(demo_feature_project, feature_bucket)

    expected = f"{_VERSION_PREFIX}config/claims/config.yaml"
    assert expected in _keys(feature_bucket)
    assert _get(feature_bucket, expected).decode() == "classes:\n  - name: Claim\n"
    # Not at the version-free base, and not flattened.
    assert f"{_BASE}/config/claims/config.yaml" not in _keys(feature_bucket)
    assert f"{_VERSION_PREFIX}config.yaml" not in _keys(feature_bucket)


@pytest.mark.parametrize(
    ("path", "expected_type"),
    [
        ("preset.yaml", "application/x-yaml"),
        ("preset.yml", "application/x-yaml"),
        ("preset.YAML", "application/x-yaml"),
        ("preset.json", "application/json"),
    ],
)
def test_the_preset_content_type_follows_its_suffix(
    demo_feature_project: Path, feature_bucket: str, path: str, expected_type: str
) -> None:
    """A preset served as the wrong type is still downloadable, but the suffix
    check is also what decides whether an uppercase `.YAML` is treated as YAML —
    a case-sensitive comparison would label it JSON and any consumer that trusts
    the header would fail to parse it."""
    _add_config_preset(demo_feature_project, path, "{}\n")
    _run_upload(demo_feature_project, feature_bucket)
    assert _content_type(feature_bucket, f"{_VERSION_PREFIX}{path}") == expected_type


def test_the_preset_is_listed_in_the_sha256_manifest(
    demo_feature_project: Path, feature_bucket: str
) -> None:
    _add_config_preset(demo_feature_project, "config/preset.yaml", "a: 1\n")
    result = _run_upload(demo_feature_project, feature_bucket)
    keys = [a["key"] for a in result.artifacts]
    assert f"{_VERSION_PREFIX}config/preset.yaml" in keys
    sha_body = _get(feature_bucket, f"{_VERSION_PREFIX}sha256.txt").decode()
    assert "preset.yaml" in sha_body


def test_a_feature_without_a_preset_uploads_none_and_warns_about_nothing(
    demo_feature_project: Path, feature_bucket: str
) -> None:
    """Absent is a normal state — most features are not vertical products. It
    must not be reported as a missing file."""
    result = _run_upload(demo_feature_project, feature_bucket)
    assert not any("config" in k for k in _keys(feature_bucket))
    assert "configPreset" not in result.console_text


def test_a_declared_preset_that_is_not_on_disk_is_skipped_with_a_warning(
    demo_feature_project: Path, feature_bucket: str
) -> None:
    """Defence in depth: `load_manifest` already refuses a manifest whose
    `configPreset.path` is missing, so reaching here means the manifest was built
    some other way. The upload must then skip the object and say which path it
    could not find, rather than raising a bare FileNotFoundError from inside the
    upload loop with no mention of the manifest field."""
    manifest = dataclasses.replace(
        load_manifest(demo_feature_project),
        configPreset=ConfigPresetSpec(path="config/never-written.yaml"),
    )
    result = _run_upload(demo_feature_project, feature_bucket, manifest=manifest)
    assert not any("never-written" in k for k in _keys(feature_bucket))
    assert "config/never-written.yaml" in result.console_text
    assert "not found" in result.console_text
    # The rest of the artifact set still published.
    assert f"{_BASE}/template.yaml" in _keys(feature_bucket)


# ---------------------------------------------------------------------------
# Agent source zip
# ---------------------------------------------------------------------------


def test_the_agent_source_zip_is_uploaded_under_a_fixed_name(
    demo_feature_project: Path, feature_bucket: str
) -> None:
    """Unlike the config preset, the agent zip's key is a fixed
    `agent-source.zip` — the host builds that key itself and does not read the
    manifest's `artifactPath`, so the local filename must not leak into the key."""
    artifact = demo_feature_project / "build" / "agent-bundle.zip"
    artifact.parent.mkdir()
    artifact.write_bytes(b"PK\x03\x04 pretend zip")
    manifest = dataclasses.replace(
        load_manifest(demo_feature_project),
        agentSource=AgentSourceSpec(artifactPath="build/agent-bundle.zip"),
    )
    _run_upload(demo_feature_project, feature_bucket, manifest=manifest)

    key = f"{_VERSION_PREFIX}agent-source.zip"
    assert key in _keys(feature_bucket)
    assert _get(feature_bucket, key) == b"PK\x03\x04 pretend zip"
    assert _content_type(feature_bucket, key) == "application/zip"
    assert f"{_VERSION_PREFIX}agent-bundle.zip" not in _keys(feature_bucket)


def test_a_declared_agent_artifact_that_is_absent_is_skipped_with_a_warning(
    demo_feature_project: Path, feature_bucket: str
) -> None:
    manifest = dataclasses.replace(
        load_manifest(demo_feature_project),
        agentSource=AgentSourceSpec(artifactPath="build/never-built.zip"),
    )
    result = _run_upload(demo_feature_project, feature_bucket, manifest=manifest)
    assert f"{_VERSION_PREFIX}agent-source.zip" not in _keys(feature_bucket)
    assert "build/never-built.zip" in result.console_text


# ---------------------------------------------------------------------------
# Baking — and the warnings that are the author's only signal
# ---------------------------------------------------------------------------


def test_all_five_tokens_are_baked_and_the_bucket_becomes_the_parameter_default(
    demo_feature_project: Path, feature_bucket: str
) -> None:
    _run_upload(demo_feature_project, feature_bucket)
    text = _get(feature_bucket, f"{_BASE}/template.yaml").decode()
    assert "_TOKEN>" not in text, text
    assert f"demo feat v{_VERSION}" in text
    assert f"ArtifactPrefix: '{_BASE}'" in text
    assert f"Default: {feature_bucket}" in text
    assert "ProductCode: 'prod-demo'" in text
    assert "prodview-XYZ" in text


def test_an_undeclared_license_mode_bakes_to_none_rather_than_a_placeholder(
    demo_feature_project: Path, feature_bucket: str
) -> None:
    """The regression this token was added for. `licenseMode` is what the host
    checks the feature's subscription against, and a template shipping the
    literal `<FEATURE_LICENSE_MODE_TOKEN>` registers a feature whose declared
    authority is an unparseable string. Absent means `none` — "I enforce
    nothing" — because an extension that says nothing is not claiming to
    enforce, and the host's own default is deliberately the strict one."""
    _run_upload(demo_feature_project, feature_bucket)
    text = _get(feature_bucket, f"{_BASE}/template.yaml").decode()
    assert "<FEATURE_LICENSE_MODE_TOKEN>" not in text
    assert "LicenseMode: 'none'" in text


def test_a_declared_license_mode_is_baked_verbatim(
    demo_feature_project: Path, feature_bucket: str
) -> None:
    """And the `none` default must not overwrite a declared value — that would
    turn a paid extension into one the host never entitlement-checks."""
    manifest_path = demo_feature_project / "feature.yaml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "  productCode: prod-demo",
            "  productCode: prod-demo\n  licenseMode: marketplace-live",
        ),
        encoding="utf-8",
    )
    _run_upload(demo_feature_project, feature_bucket)
    text = _get(feature_bucket, f"{_BASE}/template.yaml").decode()
    assert "LicenseMode: 'marketplace-live'" in text
    assert "LicenseMode: 'none'" not in text


def test_a_non_marketplace_feature_bakes_empty_identity_fields(
    demo_feature_project: Path, feature_bucket: str
) -> None:
    """Empty string, not the placeholder. An OSS feature has no product code, and
    the host's InstalledFeatures row must record the absence rather than a literal
    `<FEATURE_PRODUCT_CODE_TOKEN>` it would later try to look up."""
    manifest = dataclasses.replace(
        load_manifest(demo_feature_project),
        marketplace=dataclasses.replace(
            load_manifest(demo_feature_project).marketplace,
            productCode=None,
            listingUrl=None,
        ),
    )
    _run_upload(demo_feature_project, feature_bucket, manifest=manifest)
    text = _get(feature_bucket, f"{_BASE}/template.yaml").decode()
    assert "_TOKEN>" not in text
    assert "ProductCode: ''" in text
    assert "ListingUrl: ''" in text


def test_a_template_carrying_both_required_tokens_produces_no_warning(
    demo_feature_project: Path, feature_bucket: str
) -> None:
    """The negative half of the warning pair. If this ever warns, the warning
    stops meaning anything and the next author ignores it."""
    result = _run_upload(demo_feature_project, feature_bucket)
    assert "<FEATURE_VERSION_TOKEN>" not in result.console_text
    assert "<FEATURE_ARTIFACT_PREFIX_TOKEN>" not in result.console_text


def test_a_template_missing_the_version_token_is_warned_about_by_name(
    demo_feature_project: Path, feature_bucket: str, tmp_path: Path
) -> None:
    """The warning is the author's only signal that a version bump applied through
    the console's Update-stack wizard will not take effect — the version is baked,
    not parameterised, so a template with no placeholder keeps whatever version
    string it was written with forever."""
    stripped = tmp_path / "no-version-token.yaml"
    stripped.write_text(
        (demo_feature_project / "template.yaml")
        .read_text(encoding="utf-8")
        .replace("<FEATURE_VERSION_TOKEN>", "1.0.0"),
        encoding="utf-8",
    )
    result = _run_upload(demo_feature_project, feature_bucket, template_path=stripped)
    text = " ".join(result.console_text.split())
    assert "<FEATURE_VERSION_TOKEN>" in text
    assert "Version-bump-via-Update flow may not work" in text
    # The other token is present, so only one warning fires.
    assert "<FEATURE_ARTIFACT_PREFIX_TOKEN>" not in text
    # And the template is still published, verbatim.
    assert f"{_BASE}/template.yaml" in _keys(feature_bucket)


def test_a_template_missing_the_prefix_token_is_warned_about_by_name(
    demo_feature_project: Path, feature_bucket: str, tmp_path: Path
) -> None:
    """A template with no artifact-prefix placeholder makes the ui-deployer build
    its bundle key from whatever the parameter happens to hold at Update time —
    which the console blanks, producing `s3://bucket//1.2.3/ui-bundle.js`."""
    stripped = tmp_path / "no-prefix-token.yaml"
    stripped.write_text(
        (demo_feature_project / "template.yaml")
        .read_text(encoding="utf-8")
        .replace("<FEATURE_ARTIFACT_PREFIX_TOKEN>", "extensions/demo-feature"),
        encoding="utf-8",
    )
    result = _run_upload(demo_feature_project, feature_bucket, template_path=stripped)
    text = " ".join(result.console_text.split())
    assert "<FEATURE_ARTIFACT_PREFIX_TOKEN>" in text
    assert "bad artifact key" in text
    assert "<FEATURE_VERSION_TOKEN>" not in text


def test_the_template_uploaded_is_the_packaged_one_when_given(
    demo_feature_project: Path, feature_bucket: str, tmp_path: Path
) -> None:
    """`sam package` rewrites local `CodeUri:` paths to `s3://` URIs, and
    CloudFormation runs the SAM transform server-side and rejects local paths.
    Publishing the raw source instead of the packaged output is a deploy-time
    failure in the installing account."""
    packaged = tmp_path / "packaged.yaml"
    packaged.write_text(
        dedent("""
            AWSTemplateFormatVersion: '2010-09-09'
            Description: packaged v<FEATURE_VERSION_TOKEN>
            Metadata:
              ArtifactPrefix: '<FEATURE_ARTIFACT_PREFIX_TOKEN>'
            Resources:
              Fn:
                Type: AWS::Serverless::Function
                Properties:
                  CodeUri: s3://staging/abc123
        """).strip()
        + "\n",
        encoding="utf-8",
    )
    _run_upload(demo_feature_project, feature_bucket, template_path=packaged)
    text = _get(feature_bucket, f"{_BASE}/template.yaml").decode()
    assert "s3://staging/abc123" in text
    assert "Description: packaged v1.2.3" in text


# ---------------------------------------------------------------------------
# manifest.json — the public form the host reads
# ---------------------------------------------------------------------------


def test_the_published_manifest_carries_the_default_parameters_verbatim(
    demo_feature_project: Path, feature_bucket: str
) -> None:
    """`getFeatureLaunchUrl` populates the console's CFN parameter fields from
    this. A dropped or renamed key leaves the admin a blank required field, and a
    wrong value is pre-filled and accepted."""
    _run_upload(demo_feature_project, feature_bucket)
    mf = json.loads(_get(feature_bucket, f"{_VERSION_PREFIX}manifest.json"))
    assert mf["defaultParameters"] == {"LogLevel": "INFO"}
    assert mf["featureId"] == _FEATURE_ID
    assert mf["version"] == _VERSION
    assert mf["capabilities"] == ["custom-api"]
    assert mf["marketplace"] == {
        "productCode": "prod-demo",
        "listingUrl": "https://aws.amazon.com/marketplace/pp/prodview-XYZ",
    }


def test_the_published_manifest_is_stable_byte_for_byte(
    demo_feature_project: Path, feature_bucket: str
) -> None:
    """Sorted keys and a fixed indent, so republishing an unchanged feature does
    not produce a different object — which would make the sha256 manifest change
    for no reason and defeat any content comparison between two publishes."""
    _run_upload(demo_feature_project, feature_bucket)
    first = _get(feature_bucket, f"{_VERSION_PREFIX}manifest.json")
    _run_upload(demo_feature_project, feature_bucket)
    assert _get(feature_bucket, f"{_VERSION_PREFIX}manifest.json") == first
    assert list(json.loads(first)) == sorted(json.loads(first))


# ---------------------------------------------------------------------------
# latest.json
# ---------------------------------------------------------------------------


def test_latest_json_is_written_public_when_asked(
    demo_feature_project: Path, feature_bucket: str
) -> None:
    """latest.json is read anonymously by a host in another account resolving the
    current version. Left private, the host reads a 403 and shows the feature as
    having no published version."""
    manifest = load_manifest(demo_feature_project)
    bundle_info = validate_bundle(
        demo_feature_project / manifest.ui.bundlePath, _FEATURE_ID, _VERSION
    )
    s3 = boto3.client("s3", region_name="us-east-1")
    FeaturePublisher(demo_feature_project)._update_latest_json(
        s3=s3,
        bucket=feature_bucket,
        latest_key=f"{_BASE}/latest.json",
        manifest=manifest,
        bundle_info=bundle_info,
        make_public=True,
    )
    grants = s3.get_object_acl(Bucket=feature_bucket, Key=f"{_BASE}/latest.json")[
        "Grants"
    ]
    assert any(g.get("Grantee", {}).get("URI", "").endswith("AllUsers") for g in grants)
    payload = json.loads(_get(feature_bucket, f"{_BASE}/latest.json"))
    assert payload["bundleSha256"] == bundle_info.sha256


# ---------------------------------------------------------------------------
# build(): agent-source packaging
# ---------------------------------------------------------------------------


def _declare_agent_source(project: Path, body: str) -> None:
    manifest = project / "feature.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + body, encoding="utf-8")


def test_build_runs_the_agent_package_steps_and_accepts_the_artifact(
    demo_feature_project: Path,
) -> None:
    """The packaging step is the author's own command; `build` must run it and
    then confirm the artifact it was supposed to produce exists. Skipping the
    existence check publishes a feature whose agent zip is a stale copy — or
    absent, with the upload silently skipped."""
    _declare_agent_source(
        demo_feature_project,
        dedent("""
            agentSource:
              artifactPath: build/agent.zip
              package:
                - argv: ['mkdir', '-p', 'build']
                - argv: ['sh', '-c', 'printf zip > build/agent.zip']
        """),
    )
    console = Console(record=True, width=400)
    FeaturePublisher(demo_feature_project, console=console).build()
    assert (demo_feature_project / "build" / "agent.zip").read_bytes() == b"zip"
    assert "Agent source packaged" in console.export_text()


def test_build_refuses_when_the_packaging_step_produced_no_artifact(
    demo_feature_project: Path,
) -> None:
    """The step exited 0 and wrote nothing — the failure mode that would
    otherwise reach the upload, be skipped with a yellow warning, and ship a
    feature whose agent has no source."""
    _declare_agent_source(
        demo_feature_project,
        dedent("""
            agentSource:
              artifactPath: build/agent.zip
              package:
                - argv: ['true']
        """),
    )
    with pytest.raises(RuntimeError, match="Agent source artifact not found"):
        FeaturePublisher(demo_feature_project).build()


def test_build_runs_a_legacy_agent_package_command_with_a_deprecation_notice(
    demo_feature_project: Path,
) -> None:
    _declare_agent_source(
        demo_feature_project,
        dedent("""
            agentSource:
              artifactPath: build/agent.zip
              packageCommand: 'mkdir -p build && printf zip > build/agent.zip'
        """),
    )
    console = Console(record=True, width=400)
    FeaturePublisher(demo_feature_project, console=console).build()
    text = " ".join(console.export_text().split())
    assert "agentSource.packageCommand is deprecated" in text
    assert (demo_feature_project / "build" / "agent.zip").exists()


def test_a_failing_legacy_shell_command_aborts_the_build(
    demo_feature_project: Path,
) -> None:
    manifest = demo_feature_project / "feature.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "ui:\n  bundlePath: feature-ui/dist/ui-bundle.js",
            "ui:\n  bundlePath: feature-ui/dist/ui-bundle.js\n  buildCommand: 'exit 3'",
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match=r"ui\.buildCommand failed with exit code 3"):
        FeaturePublisher(demo_feature_project).build()


# ---------------------------------------------------------------------------
# SAM plumbing
# ---------------------------------------------------------------------------


def test_a_missing_sam_cli_is_reported_with_the_reason_it_is_required(
    demo_feature_project: Path, monkeypatch
) -> None:
    """Not an optional nicety: CloudFormation runs the SAM transform server-side
    when deploying via TemplateURL and rejects local `CodeUri` paths, so a
    publish without `sam package` produces a template that cannot deploy."""
    monkeypatch.setattr("idp_feature_sdk.publisher.shutil.which", lambda _n: None)
    with pytest.raises(RuntimeError, match="AWS SAM CLI"):
        FeaturePublisher(demo_feature_project)._sam_build_and_package(
            manifest=load_manifest(demo_feature_project),
            artifact_bucket="b",
            artifact_prefix="p",
            region="us-east-1",
        )


def test_a_sam_failure_surfaces_the_captured_output(
    demo_feature_project: Path, monkeypatch
) -> None:
    """`check=True` plus `capture_output=True` would bury the real `sam` error
    inside a CalledProcessError repr. The author needs the actual cause, which is
    usually a template or dependency error several lines long."""
    import subprocess

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "Error: Template file not found"

    monkeypatch.setattr(
        "idp_feature_sdk.publisher.subprocess.run", lambda *a, **k: _Result()
    )
    assert subprocess  # the real module is still importable; only run is swapped
    with pytest.raises(RuntimeError, match="Template file not found") as exc:
        FeaturePublisher(demo_feature_project)._run_sam(
            ["sam", "build"], step="sam build"
        )
    assert "sam build failed (exit 1)" in str(exc.value)


def test_a_sam_failure_with_only_stdout_still_surfaces_it(
    demo_feature_project: Path, monkeypatch
) -> None:
    class _Result:
        returncode = 2
        stdout = "Build Failed: no such runtime"
        stderr = ""

    monkeypatch.setattr(
        "idp_feature_sdk.publisher.subprocess.run", lambda *a, **k: _Result()
    )
    with pytest.raises(RuntimeError, match="no such runtime"):
        FeaturePublisher(demo_feature_project)._run_sam(
            ["sam", "package"], step="sam package"
        )


def test_sam_package_that_writes_no_template_is_an_error(
    demo_feature_project: Path, monkeypatch
) -> None:
    """A `sam package` that exits 0 without producing `.aws-sam/packaged.yaml`
    would otherwise fall through to reading a file that is not there, or — worse,
    if a stale one exists — publish the previous build's template."""
    monkeypatch.setattr(
        "idp_feature_sdk.publisher.shutil.which", lambda _n: "/usr/bin/sam"
    )
    monkeypatch.setattr(FeaturePublisher, "_run_sam", lambda self, cmd, *, step: None)
    with pytest.raises(RuntimeError, match="sam package did not"):
        FeaturePublisher(demo_feature_project)._sam_build_and_package(
            manifest=load_manifest(demo_feature_project),
            artifact_bucket="b",
            artifact_prefix="p",
            region="us-east-1",
        )


def test_sam_package_is_told_the_bucket_prefix_and_region_it_must_use(
    demo_feature_project: Path, monkeypatch
) -> None:
    """The code zips must go to the same bucket and version prefix as the rest of
    the artifacts, or the public-ACL pass afterwards never reaches them and a
    cross-account deploy 403s fetching the Lambda layer."""
    calls: list[list[str]] = []

    monkeypatch.setattr(
        "idp_feature_sdk.publisher.shutil.which", lambda _n: "/usr/bin/sam"
    )

    def _fake_run_sam(self, cmd, *, step):
        calls.append(cmd)
        out = self.project_dir / ".aws-sam" / "packaged.yaml"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("Resources: {}\n", encoding="utf-8")

    monkeypatch.setattr(FeaturePublisher, "_run_sam", _fake_run_sam)
    result = FeaturePublisher(demo_feature_project)._sam_build_and_package(
        manifest=load_manifest(demo_feature_project),
        artifact_bucket="feature-bucket",
        artifact_prefix=f"{_VERSION_PREFIX}sam-objects",
        region="eu-west-1",
    )
    assert calls[0] == ["sam", "build"]
    package = calls[1]
    assert package[package.index("--s3-bucket") + 1] == "feature-bucket"
    assert package[package.index("--s3-prefix") + 1] == (
        f"{_VERSION_PREFIX}sam-objects"
    )
    assert package[package.index("--region") + 1] == "eu-west-1"
    assert result == demo_feature_project / ".aws-sam" / "packaged.yaml"


# ---------------------------------------------------------------------------
# Simulator registration (the publish-time variant)
# ---------------------------------------------------------------------------


def test_registering_with_the_simulator_posts_the_product_and_a_dimension(
    demo_feature_project: Path, monkeypatch
) -> None:
    captured: list = []

    class _Resp:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def _urlopen(req, timeout=None):
        captured.append(req)
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    console = Console(record=True, width=400)
    FeaturePublisher(demo_feature_project, console=console)._register_with_simulator(
        simulator_url="http://127.0.0.1:8080/",
        product_code="prod-demo",
        manifest=load_manifest(demo_feature_project),
    )
    (req,) = captured
    assert req.full_url == "http://127.0.0.1:8080/admin/products"
    assert json.loads(req.data.decode()) == {
        "productCode": "prod-demo",
        "displayName": "Demo Feature",
        "dimensions": [{"key": "users", "description": "Users"}],
    }
    assert "Registered product" in console.export_text()


def test_an_offline_simulator_does_not_fail_the_publish(
    demo_feature_project: Path, monkeypatch
) -> None:
    """The simulator is a local dev convenience. A publish that aborts because it
    is not running would have already uploaded every artifact and flipped
    latest.json — a failure reported for a publish that succeeded."""
    import urllib.error

    def _urlopen(_req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    console = Console(record=True, width=400)
    FeaturePublisher(demo_feature_project, console=console)._register_with_simulator(
        simulator_url="http://127.0.0.1:8080",
        product_code="prod-demo",
        manifest=load_manifest(demo_feature_project),
    )
    assert "Could not register with simulator" in console.export_text()


def test_a_slow_simulator_times_out_rather_than_hanging_the_publish(
    demo_feature_project: Path, monkeypatch
) -> None:
    """A TimeoutError is handled alongside URLError; without that, a simulator
    that accepts the connection and never answers would hang the CLI."""

    def _urlopen(_req, timeout=None):
        assert timeout is not None, "the request must carry a timeout"
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    console = Console(record=True, width=400)
    FeaturePublisher(demo_feature_project, console=console)._register_with_simulator(
        simulator_url="http://127.0.0.1:8080",
        product_code="prod-demo",
        manifest=load_manifest(demo_feature_project),
    )
    assert "Could not register with simulator" in console.export_text()


def test_a_missing_packaged_template_leaves_no_half_baked_file_behind(
    demo_feature_project: Path, feature_bucket: str, tmp_path: Path
) -> None:
    """The baked template is written into the project directory and removed in a
    `finally`. If the *source* read fails the baked file was never created, so the
    cleanup has to tolerate its absence — otherwise the real error (a packaged
    template that is not there) is replaced by a FileNotFoundError from the
    cleanup, pointing at the wrong file."""
    with pytest.raises(FileNotFoundError) as exc:
        _run_upload(
            demo_feature_project,
            feature_bucket,
            template_path=tmp_path / "packaged-that-was-never-written.yaml",
        )
    assert "packaged-that-was-never-written.yaml" in str(exc.value)
    assert list(demo_feature_project.glob(".idp-feature-sdk-*")) == []
    # And nothing was uploaded, so latest.json is never flipped to this version.
    assert _keys(feature_bucket) == set()


def test_publish_registers_with_the_simulator_when_asked(
    demo_feature_project: Path, feature_bucket: str, monkeypatch
) -> None:
    """The one place `_register_with_simulator` is wired in. The default product
    code is derived from the featureId, so a feature that declares no
    `--simulator-product-code` still seeds something the host can match — and the
    step runs only when the flag is given."""
    seen: list = []
    monkeypatch.setattr(
        FeaturePublisher,
        "_register_with_simulator",
        lambda self, **kw: seen.append(kw),
    )
    # Skip the SAM round trip: publish() only needs a packaged template path, and
    # the raw source already carries every publish-time token.
    monkeypatch.setattr(
        FeaturePublisher,
        "_sam_build_and_package",
        lambda self, **_kw: self.project_dir / "template.yaml",
    )

    result = FeaturePublisher(demo_feature_project).publish(
        feature_bucket=feature_bucket,
        region="us-east-1",
        register_with_simulator="http://127.0.0.1:8080",
    )
    assert result.version == _VERSION
    assert len(seen) == 1
    assert seen[0]["simulator_url"] == "http://127.0.0.1:8080"
    assert seen[0]["product_code"] == f"prod-{_FEATURE_ID}"


def test_publish_does_not_register_with_a_simulator_by_default(
    demo_feature_project: Path, feature_bucket: str, monkeypatch
) -> None:
    """Absent means absent. A publish that always POSTed somewhere would reach out
    to the network on every release build."""
    seen: list = []
    monkeypatch.setattr(
        FeaturePublisher,
        "_register_with_simulator",
        lambda self, **kw: seen.append(kw),
    )
    monkeypatch.setattr(
        FeaturePublisher,
        "_sam_build_and_package",
        lambda self, **_kw: self.project_dir / "template.yaml",
    )
    FeaturePublisher(demo_feature_project).publish(
        feature_bucket=feature_bucket, region="us-east-1"
    )
    assert seen == []


def test_an_explicit_simulator_product_code_overrides_the_derived_one(
    demo_feature_project: Path, feature_bucket: str, monkeypatch
) -> None:
    seen: list = []
    monkeypatch.setattr(
        FeaturePublisher,
        "_register_with_simulator",
        lambda self, **kw: seen.append(kw),
    )
    monkeypatch.setattr(
        FeaturePublisher,
        "_sam_build_and_package",
        lambda self, **_kw: self.project_dir / "template.yaml",
    )
    FeaturePublisher(demo_feature_project).publish(
        feature_bucket=feature_bucket,
        region="us-east-1",
        register_with_simulator="http://127.0.0.1:8080",
        simulator_product_code="prod-explicit",
    )
    assert seen[0]["product_code"] == "prod-explicit"


def test_a_public_publish_reaches_the_objects_sam_package_uploaded(
    demo_feature_project: Path, feature_bucket: str, monkeypatch
) -> None:
    """The post-upload ACL pass, exercised through `publish()` rather than by
    calling it directly.

    `sam package` uploads the Lambda code and layer zips itself, under
    `<base>/<version>/sam-objects/`, and it takes no ACL flag — so the per-upload
    ACL the publisher applies to its *own* objects never touches them. Without the
    sweep those zips stay private, every artifact the publisher uploaded looks
    correctly public, and the failure appears only in a *deploying* account as an
    S3 403 when Lambda fetches the layer.

    That is why this drives `publish(make_public=True)` with an already-private
    object planted under the extension base: asserting only the publisher's own
    uploads are public cannot distinguish a working sweep from no sweep at all.
    (Measured: disabling the sweep in `publish()` left the whole suite green
    before this test existed.)
    """
    s3 = boto3.client("s3", region_name="us-east-1")
    layer_key = f"{_VERSION_PREFIX}sam-objects/deadbeefcafe"
    s3.put_object(Bucket=feature_bucket, Key=layer_key, Body=b"layer-zip")

    def _is_public(key: str) -> bool:
        return any(
            g.get("Grantee", {}).get("URI", "").endswith("AllUsers")
            and g.get("Permission") == "READ"
            for g in s3.get_object_acl(Bucket=feature_bucket, Key=key)["Grants"]
        )

    assert not _is_public(layer_key), "the planted object must start private"

    # Skip the SAM round trip; the raw source template already carries the tokens.
    monkeypatch.setattr(
        FeaturePublisher,
        "_sam_build_and_package",
        lambda self, **_kw: self.project_dir / "template.yaml",
    )
    FeaturePublisher(demo_feature_project).publish(
        feature_bucket=feature_bucket, region="us-east-1", make_public=True
    )

    assert _is_public(layer_key)
    # latest.json, written by put_object rather than upload_file, is covered too.
    assert _is_public(f"{_BASE}/latest.json")


def test_a_private_publish_leaves_the_sam_objects_private(
    demo_feature_project: Path, feature_bucket: str, monkeypatch
) -> None:
    """The sweep is opt-in. Running it unconditionally would make every
    same-account publish world-readable — the default is private for a reason."""
    s3 = boto3.client("s3", region_name="us-east-1")
    layer_key = f"{_VERSION_PREFIX}sam-objects/deadbeefcafe"
    s3.put_object(Bucket=feature_bucket, Key=layer_key, Body=b"layer-zip")

    monkeypatch.setattr(
        FeaturePublisher,
        "_sam_build_and_package",
        lambda self, **_kw: self.project_dir / "template.yaml",
    )
    FeaturePublisher(demo_feature_project).publish(
        feature_bucket=feature_bucket, region="us-east-1"
    )

    grants = s3.get_object_acl(Bucket=feature_bucket, Key=layer_key)["Grants"]
    assert not any(
        g.get("Grantee", {}).get("URI", "").endswith("AllUsers") for g in grants
    )
