# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""FeaturePublisher — end-to-end validate → build → upload → latest.json flow.

Publishes one version of a feature to the feature bucket in the VERSION-FREE
layout the main-stack Lambdas expect (identical to the bundled publisher,
idp_sdk/_core/publish.py):

    s3://<feature-bucket>/[<prefix>/]extensions/<id>/template.yaml   # version-free
    s3://<feature-bucket>/[<prefix>/]extensions/<id>/latest.json     # version-free
    s3://<feature-bucket>/[<prefix>/]extensions/<id>/<version>/ui-bundle.js
    s3://<feature-bucket>/[<prefix>/]extensions/<id>/<version>/manifest.json
    s3://<feature-bucket>/[<prefix>/]extensions/<id>/<version>/sha256.txt

The `<prefix>` is optional (default empty): with no prefix the layout is the bare
`extensions/<id>/...` the catalog's `templateKey` records.

The template is version-free (newest publish overwrites it); both the version
and the artifact base prefix are BAKED into the template at publish time
(<FEATURE_VERSION_TOKEN> / <FEATURE_ARTIFACT_PREFIX_TOKEN>) rather than passed
as CFN parameters — the CFN console "Update stack" wizard drops parameters on a
template change. `latest.json` is updated only after all per-version artifacts
upload successfully so a failed publish leaves the prior latest.json alone.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import boto3
from rich.console import Console

from .bundle import BundleInfo, validate_bundle
from .manifest import (
    CommandStep,
    FeatureManifest,
    bake_marketplace_identity,
    load_manifest,
)


@dataclass
class PublishResult:
    feature_id: str
    version: str
    template_url: str
    bundle_url: str
    manifest_url: str
    latest_json_url: str
    launch_url: Optional[str] = None
    artifacts: List[Dict[str, Any]] = field(default_factory=list)


class FeaturePublisher:
    """Publishes a feature project to an S3 'feature bucket'."""

    def __init__(
        self,
        project_dir: Path | str,
        *,
        console: Optional[Console] = None,
        s3_client: Any = None,
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.console = console or Console()
        self._s3 = s3_client  # lazy-created if None

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------
    def validate(self) -> FeatureManifest:
        """Validate the manifest + surrounding files. No build, no network."""
        manifest = load_manifest(self.project_dir)
        self.console.log(
            f"[green]✓[/green] Validated feature.yaml ({manifest.featureId} v{manifest.version})"
        )
        return manifest

    def build(self, manifest: Optional[FeatureManifest] = None) -> BundleInfo:
        """Run the UI build steps (if any), then statically validate the bundle."""
        manifest = manifest or self.validate()

        if manifest.ui.build:
            self._run_steps(manifest.ui.build, step_name="ui.build")
            self.console.log("[green]✓[/green] UI build finished")
        elif manifest.ui.buildCommand:
            self._run_legacy_shell_command(
                manifest.ui.buildCommand, step_name="ui.buildCommand"
            )
            self.console.log("[green]✓[/green] UI build finished")

        bundle_path = self.project_dir / manifest.ui.bundlePath
        info = validate_bundle(bundle_path, manifest.featureId, manifest.version)
        self.console.log(
            f"[green]✓[/green] Validated UI bundle "
            f"({info.size_bytes:,} bytes, sha256 {info.sha256[:12]}…)"
        )

        if manifest.agentSource:
            if manifest.agentSource.package:
                self._run_steps(
                    manifest.agentSource.package, step_name="agentSource.package"
                )
            elif manifest.agentSource.packageCommand:
                self._run_legacy_shell_command(
                    manifest.agentSource.packageCommand,
                    step_name="agentSource.packageCommand",
                )
            artifact = self.project_dir / manifest.agentSource.artifactPath
            if not artifact.exists():
                raise RuntimeError(
                    f"Agent source artifact not found at {artifact} after packaging"
                )
            self.console.log(
                f"[green]✓[/green] Agent source packaged ({artifact.stat().st_size:,} bytes)"
            )

        return info

    def _run_steps(self, steps: List[CommandStep], *, step_name: str) -> None:
        """Run structured manifest steps with ``shell=False`` — argv is exec'd
        directly, so there is no shell metacharacter surface (the B602 class of
        issue the legacy string forms have)."""
        for i, step in enumerate(steps):
            cwd = self.project_dir / step.cwd if step.cwd else self.project_dir
            self.console.log(
                f"[cyan]▸[/cyan] {step_name}[{i}]: {' '.join(step.argv)}"
                + (f"  (cwd={step.cwd})" if step.cwd else "")
            )
            result = subprocess.run(
                step.argv,
                shell=False,
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                self.console.print(result.stdout)
                self.console.print(result.stderr)
                raise RuntimeError(
                    f"{step_name}[{i}] ({step.argv[0]}) failed with exit code "
                    f"{result.returncode}"
                )

    def _run_legacy_shell_command(self, command: str, *, step_name: str) -> None:
        """Run a legacy shell-string manifest command (deprecated).

        Kept for backward compatibility with existing feature.yaml files; new
        manifests should use the structured `ui.build` / `agentSource.package`
        step lists, which run without a shell."""
        self.console.log(
            f"[yellow]![/yellow] {step_name} is deprecated (runs through a shell). "
            f"Migrate to the structured step-list form — see the feature SDK README."
        )
        self.console.log(f"[cyan]▸[/cyan] {step_name}: {command}")
        result = subprocess.run(
            command,
            shell=True,  # nosec B602 - legacy manifest form; command is the feature author's own build/package string run on their machine, not attacker-controlled input. Structured shell-free form is preferred.
            cwd=self.project_dir,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            self.console.print(result.stdout)
            self.console.print(result.stderr)
            raise RuntimeError(f"{step_name} failed with exit code {result.returncode}")

    def publish(
        self,
        *,
        feature_bucket: str,
        region: str = "us-east-1",
        s3_prefix: str = "",
        make_public: bool = False,
        register_with_simulator: Optional[str] = None,
        simulator_product_code: Optional[str] = None,
    ) -> PublishResult:
        """Validate → build → upload → update latest.json."""
        manifest = self.validate()
        bundle_info = self.build(manifest)

        s3 = self._s3_client(region)
        # VERSION-FREE extension base; the template lives at the base and the
        # versioned artifacts under a <version>/ subkey. This matches the
        # bundled publisher (idp_sdk/_core/publish.py) exactly so both paths
        # produce an identical layout. Keeping the version out of the
        # template's key/path is essential: the version (and this base prefix)
        # are baked into the template at publish time, NOT passed as CFN
        # parameters — the CFN console "Update stack" wizard drops parameters on
        # a template change, which previously left an empty prefix and a
        # `s3://bucket//<version>/...` bad key.
        #
        # The prefix is OPTIONAL: an empty/blank prefix yields the bare
        # `extensions/<id>/...` layout the catalog's `templateKey` convention
        # expects (no leading segment, no leading slash). A non-empty prefix is
        # joined with a single separator (no `//` from a trailing slash).
        clean_prefix = s3_prefix.strip("/")
        extension_base = (
            f"{clean_prefix}/extensions/{manifest.featureId}"
            if clean_prefix
            else f"extensions/{manifest.featureId}"
        )
        version_prefix = f"{extension_base}/{manifest.version}/"
        latest_key = f"{extension_base}/latest.json"

        # 0. SAM build + package the feature template so the Lambda functions'
        #    local `CodeUri:` paths are rewritten to `s3://...` URIs and the
        #    code zips are uploaded. CloudFormation runs the SAM transform
        #    SERVER-SIDE when deploying via TemplateURL, and it rejects local
        #    CodeUri paths with "'CodeUri' is not a valid S3 Uri". We bake the
        #    publish-time tokens into THIS packaged template (not the raw
        #    source). Mirrors the bundled publisher
        #    (idp_sdk/_core/publish.py:_upload_sample_feature_artifacts).
        packaged_template = self._sam_build_and_package(
            manifest=manifest,
            artifact_bucket=feature_bucket,
            artifact_prefix=f"{version_prefix}sam-objects",
            region=region,
        )

        # 1. Upload per-version artifacts (template version-free at the base;
        #    ui-bundle/config-preset/manifest under <base>/<version>/).
        artifacts = self._upload_version_artifacts(
            s3=s3,
            bucket=feature_bucket,
            extension_base=extension_base,
            version_prefix=version_prefix,
            manifest=manifest,
            bundle_info=bundle_info,
            make_public=make_public,
            packaged_template_path=packaged_template,
        )

        # 2. Flip latest.json
        self._update_latest_json(
            s3=s3,
            bucket=feature_bucket,
            latest_key=latest_key,
            manifest=manifest,
            bundle_info=bundle_info,
            make_public=make_public,
        )

        # 2b. When publishing public, set public-read on EVERY object under the
        #     extension base. The per-upload ACL (ExtraArgs in _upload) only
        #     covers the publisher's OWN uploads (template/ui-bundle/manifest);
        #     the Lambda + LAYER code zips under <version>/sam-objects/ are
        #     uploaded by `sam package`, which takes no ACL flag — so without
        #     this pass they stay private and a cross-account deploy fails with
        #     S3 403 when Lambda fetches the layer/code zip. Mirrors the bundled
        #     publisher (idp_sdk/_core/publish.py:set_public_acls).
        if make_public:
            self._set_public_acls(s3=s3, bucket=feature_bucket, prefix=extension_base)
        self.console.log(
            f"[green]✓[/green] Updated s3://{feature_bucket}/{latest_key} "
            f"→ {manifest.version}"
        )

        # 3. Compose helpful URLs. The template is VERSION-FREE (at the base);
        #    the versioned artifacts live under <base>/<version>/.
        template_url = self._https_url(
            feature_bucket, region, f"{extension_base}/template.yaml"
        )
        bundle_url = self._https_url(
            feature_bucket, region, f"{version_prefix}ui-bundle.js"
        )
        manifest_url = self._https_url(
            feature_bucket, region, f"{version_prefix}manifest.json"
        )
        latest_json_url = self._https_url(feature_bucket, region, latest_key)

        launch_url = self._build_launch_url(
            manifest=manifest, region=region, template_url=template_url
        )

        # 4. (Optional) register with the simulator
        if register_with_simulator:
            self._register_with_simulator(
                simulator_url=register_with_simulator,
                product_code=simulator_product_code or f"prod-{manifest.featureId}",
                manifest=manifest,
            )

        return PublishResult(
            feature_id=manifest.featureId,
            version=manifest.version,
            template_url=template_url,
            bundle_url=bundle_url,
            manifest_url=manifest_url,
            latest_json_url=latest_json_url,
            launch_url=launch_url,
            artifacts=artifacts,
        )

    # ---------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------
    def _s3_client(self, region: str) -> Any:
        if self._s3 is None:
            self._s3 = boto3.client("s3", region_name=region)
        return self._s3

    def _set_public_acls(self, *, s3: Any, bucket: str, prefix: str) -> None:
        """Set `public-read` on every object under ``prefix`` (the extension
        base, including the ``sam-objects/`` zips uploaded by ``sam package``).

        Required for cross-account public distribution: the deploying account's
        Lambda/CloudFormation fetches the layer + code zips over plain HTTPS, so
        they must be world-readable. The per-upload ACL only covers the
        publisher's own uploads. Mirrors ``set_public_acls`` in the bundled
        publisher (idp_sdk/_core/publish.py).
        """
        paginator = s3.get_paginator("list_objects_v2")
        count = 0
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                s3.put_object_acl(Bucket=bucket, Key=obj["Key"], ACL="public-read")
                count += 1
        self.console.log(
            f"[green]✓[/green] Set public-read on {count} object(s) under "
            f"s3://{bucket}/{prefix}/"
        )

    def _sam_build_and_package(
        self,
        *,
        manifest: FeatureManifest,
        artifact_bucket: str,
        artifact_prefix: str,
        region: str,
    ) -> Path:
        """Run `sam build` + `sam package` so the template's local `CodeUri:`
        paths are rewritten to `s3://...` and the code zips uploaded. Returns
        the path to the packaged template (`.aws-sam/packaged.yaml`).
        """
        if not shutil.which("sam"):
            raise RuntimeError(
                "`sam` (AWS SAM CLI) not found in PATH. It is required to "
                "publish a feature: CloudFormation runs the SAM transform "
                "server-side when deploying via TemplateURL and rejects local "
                "CodeUri paths. Install the AWS SAM CLI and retry."
            )
        self.console.log("[cyan]▸[/cyan] Running sam build…")
        self._run_sam(["sam", "build"], step="sam build")
        out = self.project_dir / ".aws-sam" / "packaged.yaml"
        self.console.log("[cyan]▸[/cyan] Running sam package…")
        self._run_sam(
            [
                "sam",
                "package",
                "--s3-bucket",
                artifact_bucket,
                "--s3-prefix",
                artifact_prefix,
                "--output-template-file",
                str(out),
                "--region",
                region,
            ],
            step="sam package",
        )
        if not out.is_file():
            raise RuntimeError(
                f"Expected packaged template at {out} but sam package did not "
                f"produce it."
            )
        self.console.log(f"[green]✓[/green] SAM package complete → {out}")
        return out

    def _run_sam(self, cmd: List[str], *, step: str) -> None:
        """Run a SAM subprocess, surfacing captured stdout/stderr on failure.

        `check=True` + `capture_output=True` alone would bury the real `sam`
        error inside CalledProcessError; we re-raise a RuntimeError that
        includes the captured output so the user sees the actual cause.
        """
        result = subprocess.run(
            cmd, cwd=self.project_dir, capture_output=True, text=True
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"{step} failed (exit {result.returncode}).\n{detail}")

    def _upload_version_artifacts(
        self,
        *,
        s3: Any,
        bucket: str,
        extension_base: str,
        version_prefix: str,
        manifest: FeatureManifest,
        bundle_info: BundleInfo,
        make_public: bool,
        packaged_template_path: Optional[Path] = None,
    ) -> List[Dict[str, Any]]:
        artifacts: List[Dict[str, Any]] = []

        def _upload(local_path: Path, key: str, content_type: str) -> None:
            extra: Dict[str, Any] = {"ContentType": content_type}
            if make_public:
                extra["ACL"] = "public-read"
            s3.upload_file(
                str(local_path),
                bucket,
                key,
                ExtraArgs=extra,
            )
            sha = _sha256(local_path)
            size = local_path.stat().st_size
            artifacts.append(
                {"key": key, "size": size, "sha256": sha, "contentType": content_type}
            )
            self.console.log(
                f"[green]✓[/green] Uploaded s3://{bucket}/{key} ({size:,} bytes)"
            )

        # Bake the publish-time tokens into the (version-free) template before
        # upload, so the published template is self-contained:
        #   <FEATURE_VERSION_TOKEN>          -> manifest.version
        #   <FEATURE_ARTIFACT_PREFIX_TOKEN>  -> extension_base
        #   <FEATURE_BUCKET_TOKEN>           -> bucket
        # Baking (rather than passing CFN parameters) is essential because the
        # CloudFormation console's "Update stack" wizard drops/blanks parameters
        # on a template change — which previously left an empty prefix and a
        # `s3://bucket//<version>/...` bad key. The bucket is still a real CFN
        # parameter (the host's getFeatureLaunchUrl / an installer who re-staged
        # artifacts overrides it at deploy), but baking it as the parameter's
        # DEFAULT means a console Update that drops the param falls back to the
        # publish bucket instead of an empty string. The template lives at the
        # VERSION-FREE base (`<extension_base>/template.yaml`); newest publish
        # overwrites it. This mirrors the bundled publisher
        # (idp_sdk/_core/publish.py:_upload_sample_feature_artifacts) exactly.
        # Prefer the SAM-packaged template (CodeUri rewritten to s3://...); fall
        # back to the raw source only if packaging was skipped (e.g. a feature
        # with no SAM functions). Without the packaged template, CloudFormation
        # rejects the server-side SAM transform with "'CodeUri' is not a valid
        # S3 Uri".
        template_local = (
            packaged_template_path
            if packaged_template_path is not None
            else self.project_dir / manifest.template.path
        )
        baked_template = self.project_dir / ".idp-feature-sdk-template-baked.yaml"
        try:
            template_text = template_local.read_text(encoding="utf-8")
            # Also bake the marketplace identity so it travels with the feature
            # into the host's InstalledFeatures row at install time:
            #   <FEATURE_PRODUCT_CODE_TOKEN>  -> marketplace.productCode
            #   <FEATURE_LISTING_URL_TOKEN>   -> marketplace.listingUrl
            #   <FEATURE_LICENSE_MODE_TOKEN>  -> marketplace.licenseMode (or "none")
            # All are OPTIONAL (empty for non-marketplace features) — no warning
            # if the placeholders are absent, unlike the required tokens above.
            # Shared with pack.py via one helper: these were two independent
            # replace-chains, and adding the third token taught only one of them,
            # which shipped an unbaked literal on this path.
            baked_text = bake_marketplace_identity(
                template_text.replace("<FEATURE_VERSION_TOKEN>", manifest.version)
                .replace("<FEATURE_ARTIFACT_PREFIX_TOKEN>", extension_base)
                .replace("<FEATURE_BUCKET_TOKEN>", bucket),
                manifest.marketplace,
            )
            if "<FEATURE_VERSION_TOKEN>" not in template_text:
                self.console.log(
                    "[yellow]![/yellow] template.yaml has no <FEATURE_VERSION_TOKEN> "
                    "placeholder — uploading verbatim. Version-bump-via-Update flow "
                    "may not work for this feature. See feature-template/template.yaml."
                )
            if "<FEATURE_ARTIFACT_PREFIX_TOKEN>" not in template_text:
                self.console.log(
                    "[yellow]![/yellow] template.yaml has no "
                    "<FEATURE_ARTIFACT_PREFIX_TOKEN> placeholder — the ui-deployer "
                    "may build a bad artifact key on a stack Update. See "
                    "feature-template/template.yaml."
                )
            baked_template.write_text(baked_text, encoding="utf-8")
            # Template is VERSION-FREE: at the extension base, not under <version>/.
            _upload(
                baked_template,
                f"{extension_base}/template.yaml",
                "application/x-yaml",
            )
        finally:
            try:
                baked_template.unlink()
            except FileNotFoundError:
                pass
        _upload(
            bundle_info.path, f"{version_prefix}ui-bundle.js", "application/javascript"
        )

        # manifest.json — serialised, public form of feature.yaml the host reads.
        published = self._public_manifest(manifest)
        mf_local = self.project_dir / ".idp-feature-sdk-manifest.json"
        mf_local.write_text(
            json.dumps(published, indent=2, sort_keys=True), encoding="utf-8"
        )
        try:
            _upload(mf_local, f"{version_prefix}manifest.json", "application/json")
        finally:
            mf_local.unlink(missing_ok=True)

        # Agent source zip — if feature.yaml defines agentSource with a packageCommand.
        agent_source = getattr(manifest, "agentSource", None)
        if agent_source and hasattr(agent_source, "artifactPath"):
            artifact_path = self.project_dir / agent_source.artifactPath
            if artifact_path.exists():
                _upload(
                    artifact_path,
                    f"{version_prefix}agent-source.zip",
                    "application/zip",
                )
            else:
                self.console.log(
                    f"[yellow]![/yellow] agentSource.artifactPath "
                    f"'{agent_source.artifactPath}' not found — skipping agent source upload"
                )

        # Vertical-product config preset — if feature.yaml defines configPreset.
        # Uploaded under the version prefix at the same relative path so the
        # ui-deployer custom resource can fetch it with a stable key.
        config_preset = getattr(manifest, "configPreset", None)
        if config_preset and getattr(config_preset, "path", None):
            preset_path = self.project_dir / config_preset.path
            if preset_path.exists():
                content_type = (
                    "application/x-yaml"
                    if preset_path.suffix.lower() in (".yaml", ".yml")
                    else "application/json"
                )
                _upload(
                    preset_path,
                    f"{version_prefix}{config_preset.path}",
                    content_type,
                )
            else:
                self.console.log(
                    f"[yellow]![/yellow] configPreset.path '{config_preset.path}' "
                    f"not found — skipping config preset upload"
                )

        # sha256 file — one artifact per line.
        sha_lines = "\n".join(
            f"{a['sha256']}  {a['key'].rsplit('/', 1)[-1]}" for a in artifacts
        )
        sha_local = self.project_dir / ".idp-feature-sdk-sha256.txt"
        sha_local.write_text(sha_lines + "\n", encoding="utf-8")
        try:
            _upload(sha_local, f"{version_prefix}sha256.txt", "text/plain")
        finally:
            sha_local.unlink(missing_ok=True)

        return artifacts

    def _update_latest_json(
        self,
        *,
        s3: Any,
        bucket: str,
        latest_key: str,
        manifest: FeatureManifest,
        bundle_info: BundleInfo,
        make_public: bool,
    ) -> None:
        payload = {
            "featureId": manifest.featureId,
            "version": manifest.version,
            "displayName": manifest.displayName,
            "bundleSha256": bundle_info.sha256,
            "publishedAt": _now_iso(),
        }
        extra: Dict[str, Any] = {"ContentType": "application/json"}
        if make_public:
            extra["ACL"] = "public-read"
        s3.put_object(
            Bucket=bucket,
            Key=latest_key,
            Body=json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
            **extra,
        )

    @staticmethod
    def _public_manifest(manifest: FeatureManifest) -> Dict[str, Any]:
        """The manifest form stored in the per-version S3 object. Includes
        `defaultParameters` (used by `getFeatureLaunchUrl` to populate CFN parameters).
        """
        return {
            "featureId": manifest.featureId,
            "displayName": manifest.displayName,
            "version": manifest.version,
            "description": manifest.description,
            "iconUrl": manifest.iconUrl,
            "capabilities": list(manifest.capabilities),
            "defaultParameters": dict(manifest.defaultParameters),
            "marketplace": {
                "productCode": manifest.marketplace.productCode,
                "listingUrl": manifest.marketplace.listingUrl,
            },
        }

    @staticmethod
    def _https_url(bucket: str, region: str, key: str) -> str:
        return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"

    def _build_launch_url(
        self,
        *,
        manifest: FeatureManifest,
        region: str,
        template_url: str,
    ) -> str:
        """Helper launch URL that the `idp-feature-cli publish` output prints.

        Note: this URL uses a placeholder MainStackName (`MAINSTACKNAME`) that the
        admin must substitute in. In production this URL is generated server-side
        by the main stack's `getFeatureLaunchUrl` resolver (Phase A) which knows
        the actual stack name and checks the admin's role.
        """
        # NOTE: there is intentionally no `param_FeatureVersion=…` in this
        # URL. The version is baked into the template at publish time
        # (substituted into `<FEATURE_VERSION_TOKEN>`); it is no longer a
        # CFN parameter on feature stacks. Including it here would yield a
        # `Parameters: [FeatureVersion] do not exist in the template`
        # validation error in the CloudFormation console.
        parts = [
            f"templateURL={quote(template_url, safe='')}",
            f"stackName={quote('idp-feature-' + manifest.featureId, safe='')}",
            "param_MainStackName=MAINSTACKNAME",
        ]
        for k, v in manifest.defaultParameters.items():
            parts.append(f"param_{quote(k, safe='')}={quote(str(v), safe='')}")
        return (
            f"https://console.aws.amazon.com/cloudformation/home?region={region}"
            f"#/stacks/quickcreate?" + "&".join(parts)
        )

    def _register_with_simulator(
        self,
        *,
        simulator_url: str,
        product_code: str,
        manifest: FeatureManifest,
    ) -> None:
        """POST to the local marketplace-simulator's /admin to create a product
        + (optionally) a public offer. No-op if the simulator doesn't respond —
        we don't want publish to fail because the simulator is offline.
        """
        import urllib.error
        import urllib.request

        body = json.dumps(
            {
                "productCode": product_code,
                "displayName": manifest.displayName,
                "dimensions": [{"key": "users", "description": "Users"}],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{simulator_url.rstrip('/')}/admin/products",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310 - operator-supplied simulator URL in a publish-time CLI, not request input
                resp.read()
            self.console.log(
                f"[green]✓[/green] Registered product [bold]{product_code}[/bold] with simulator"
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            self.console.log(
                f"[yellow]![/yellow] Could not register with simulator at {simulator_url}: {exc}"
            )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
