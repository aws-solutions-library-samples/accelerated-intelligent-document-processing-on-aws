# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pack publish + deploy helpers.

A "pack" is a vertical-product feature (e.g. claims-pack, loans-pack)
shipped with a single-template CFN wrapper that creates a host stack
plus auto-installs the feature stack.

`publish-pack` publishes the feature artifacts by delegating to
``FeaturePublisher`` — the SAME publisher (and therefore the same
``extensions/<id>/`` version-free layout, the same five baked publish-time
tokens, and the same private/`--public` semantics) used by ``publish``.
It then *bakes* the publish bucket + version-free prefix + version into the
wrapper template's parameter defaults and uploads the baked wrapper. The
feature stack reads its artifacts IN PLACE from the publish bucket at deploy
time (via IAM), exactly like a normal ``deploy`` — there is no seller bucket
and no pre-stage copy. The published wrapper URL is shareable as a
Quick-Create URL (which requires ``--public``).

`deploy-pack` reads the published wrapper URL, calls
cloudformation:CreateStack with only the minimum operator inputs
(stack name, admin email).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import boto3
from rich.console import Console

from .manifest import FeatureManifest, bake_marketplace_identity, load_manifest
from .publisher import FeaturePublisher


@dataclass(frozen=True)
class PackPublishResult:
    feature_id: str
    version: str
    artifact_bucket: str
    artifact_prefix: str
    feature_template_url: str
    host_template_url: str
    wrapper_template_url: str
    quick_create_url: str
    deploy_command: str


def _public_https_url(bucket: str, region: str, key: str) -> str:
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def _bake_wrapper_tokens(
    wrapper_yaml: str,
    *,
    manifest: FeatureManifest,
    artifact_bucket: str,
    artifact_prefix: str,
) -> str:
    """Substitute the publish-time ``<FEATURE_*_TOKEN>`` placeholders in the
    wrapper text, mirroring how FeaturePublisher bakes the SAME tokens into the
    feature template (publisher.py). Lets the wrapper use the version (etc.) in
    places CloudFormation forbids intrinsics — most notably the top-level
    ``Description``. All tokens are OPTIONAL: a placeholder that isn't present
    is simply a no-op, so wrappers that don't use them are unaffected.
    """
    return bake_marketplace_identity(
        wrapper_yaml.replace("<FEATURE_VERSION_TOKEN>", manifest.version)
        .replace("<FEATURE_ARTIFACT_PREFIX_TOKEN>", artifact_prefix)
        .replace("<FEATURE_BUCKET_TOKEN>", artifact_bucket),
        manifest.marketplace,
    )


def _bake_wrapper_defaults(
    wrapper_yaml: str,
    *,
    param_defaults: Dict[str, str],
) -> str:
    """Set Default: <value> on the named parameters in the wrapper YAML.

    We treat the YAML as text rather than parsing/dumping because:
      - CloudFormation YAML uses short-form intrinsics (!Ref, !Sub) which
        most YAML libraries strip or break on round-trip.
      - The change is localized: insert/replace a single Default: line
        per parameter.
    """
    lines = wrapper_yaml.split("\n")
    out: List[str] = []
    i = 0
    in_parameters_block = False
    parameters_indent = -1
    current_param: Optional[str] = None
    current_param_indent = -1
    pending_default_to_set: Dict[str, str] = dict(param_defaults)

    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        if not in_parameters_block:
            if stripped == "Parameters:":
                in_parameters_block = True
                parameters_indent = indent
            out.append(line)
            i += 1
            continue

        # Inside Parameters block. Detect end-of-block: next top-level key
        # at the same indent as 'Parameters:'.
        if stripped and indent <= parameters_indent and stripped != "Parameters:":
            # Flush remaining defaults: any parameter we never saw, just
            # ignore (the caller may pass keys that don't exist — log only).
            in_parameters_block = False
            current_param = None
            out.append(line)
            i += 1
            continue

        # Detect a new parameter header: 'Name:' line at one indent level
        # deeper than 'Parameters:'.
        if (
            stripped.endswith(":")
            and indent > parameters_indent
            and not stripped.startswith("-")
        ):
            # Could be the parameter header OR a nested key inside the
            # current parameter (e.g. Properties:, AllowedValues:). The
            # parameter header is at exactly parameters_indent + 2 (the
            # CFN convention used in our templates).
            if current_param_indent == -1:
                current_param_indent = indent
            if indent == current_param_indent:
                current_param = stripped[:-1]  # strip trailing colon
            out.append(line)
            i += 1
            continue

        # Inside a parameter; check if this line is `Default: <value>`.
        if current_param and current_param in pending_default_to_set:
            value = pending_default_to_set[current_param]
            if stripped.startswith("Default:"):
                # Replace existing Default in place, preserve indent.
                out.append(" " * indent + f"Default: '{value}'")
                pending_default_to_set.pop(current_param)
                i += 1
                continue

        out.append(line)
        i += 1

    # For any pending parameter, insert a Default line after the parameter
    # header. We didn't catch them in the first pass because they had no
    # existing Default line.
    if pending_default_to_set:
        result = "\n".join(out)
        for param_name, value in pending_default_to_set.items():
            # Match the parameter header line and append "  Default: '<value>'"
            # immediately after it. Use the same indent the wrapper uses
            # (4 spaces nested under Parameters by convention).
            pattern = re.compile(
                rf"^(\s+){re.escape(param_name)}:\s*$",
                re.MULTILINE,
            )

            def _replace(match: re.Match[str]) -> str:
                indent = match.group(1)
                # Property indent = parameter indent + 2 (CFN convention).
                # Our templates indent properties with 6 spaces under a
                # 4-space parameter indent.
                prop_indent = indent + "  "
                return f"{match.group(0)}\n{prop_indent}Default: '{value}'"

            new_result, n = pattern.subn(_replace, result, count=1)
            if n == 0:
                # Parameter not found in wrapper — caller likely passed
                # the wrong featureBucketParam/prefixParam/versionParam name.
                raise ValueError(
                    f"Wrapper template has no parameter named {param_name!r}; "
                    "check feature.yaml `pack.wrapperParameters` keys."
                )
            result = new_result
        return result

    return "\n".join(out)


class PackPublisher:
    """Builds + uploads pack artifacts and the baked wrapper template."""

    def __init__(self, project_dir: Path, console: Optional[Console] = None) -> None:
        self.project_dir = project_dir.resolve()
        self.console = console or Console()

    def _log(self, msg: str) -> None:
        self.console.log(msg)

    def publish(
        self,
        *,
        artifacts_bucket: str,
        artifacts_prefix: str,
        host_template_url: str,
        region: str,
        make_public: bool = False,
    ) -> PackPublishResult:
        manifest = load_manifest(self.project_dir)
        if manifest.pack is None:
            raise RuntimeError(
                "feature.yaml has no `pack:` section — this feature is not a "
                "vertical-product pack. Add `pack.wrapperTemplatePath` "
                "pointing at your deploy.yaml."
            )
        wrapper_path = self.project_dir / manifest.pack.wrapperTemplatePath
        if not wrapper_path.is_file():
            raise FileNotFoundError(
                f"Wrapper template {wrapper_path} not found "
                f"(feature.yaml -> pack.wrapperTemplatePath)."
            )

        s3 = boto3.client("s3", region_name=region)
        feature_id = manifest.featureId
        version = manifest.version

        # 1-3. Publish the feature artifacts by DELEGATING to FeaturePublisher.
        #
        # A pack's feature artifacts are published exactly like any other
        # feature: same `extensions/<id>/` version-free layout, the SAME five
        # publish-time tokens baked into the template (VERSION, ARTIFACT_PREFIX,
        # BUCKET, PRODUCT_CODE, LISTING_URL), the same SAM build/package (with
        # captured stderr surfaced on failure), and the same public-ACL pass
        # when --public is set. Sharing one publisher is what keeps pack and
        # feature publishing from drifting apart (issue #375). The pack then
        # reads these artifacts IN PLACE from this bucket at deploy time — there
        # is no seller bucket and no pre-stage copy.
        feature_publisher = FeaturePublisher(
            self.project_dir, console=self.console, s3_client=s3
        )
        # validate → build (ui.build steps) → upload, identical to `publish`.
        feat_result = feature_publisher.publish(
            feature_bucket=artifacts_bucket,
            region=region,
            s3_prefix=artifacts_prefix,
            make_public=make_public,
        )

        # Version-free artifact base the feature stack reads from (matches
        # FeaturePublisher's `extension_base`): [<prefix>/]extensions/<id>.
        clean_prefix = artifacts_prefix.strip("/")
        feat_prefix = (
            f"{clean_prefix}/extensions/{feature_id}"
            if clean_prefix
            else f"extensions/{feature_id}"
        )
        feature_template_url = feat_result.template_url

        # 4. Bake artifact-locating defaults into the wrapper template. The
        # wrapper passes these straight to the feature stack (FeatureBucket +
        # version-free prefix + version); the feature stack reads its artifacts
        # in place via IAM — no seller bucket, no anonymous fetch.
        wrapper_yaml = wrapper_path.read_text(encoding="utf-8")
        wp = manifest.pack.wrapperParameters
        defaults = {wp.hostTemplateUrlParam: host_template_url}
        if wp.featureBucketParam:
            defaults[wp.featureBucketParam] = artifacts_bucket
        if wp.prefixParam:
            defaults[wp.prefixParam] = feat_prefix
        if wp.versionParam:
            defaults[wp.versionParam] = version
        # Also bake the publish-time tokens into the wrapper text, the SAME way
        # FeaturePublisher bakes them into the feature template. The wrapper
        # parameter Defaults above cover values referenced via !Ref/!Sub, but
        # CloudFormation forbids intrinsics in a few places (e.g. the top-level
        # `Description`), so a wrapper that wants the version in such a field
        # uses `<FEATURE_VERSION_TOKEN>` and relies on this substitution. Tokens
        # are optional — absent placeholders are simply left untouched.
        baked_wrapper = _bake_wrapper_tokens(
            wrapper_yaml,
            manifest=manifest,
            artifact_bucket=artifacts_bucket,
            artifact_prefix=feat_prefix,
        )
        baked_wrapper = _bake_wrapper_defaults(baked_wrapper, param_defaults=defaults)
        wrapper_key = f"{feat_prefix}/deploy.yaml"
        # Mirror FeaturePublisher's per-object publicness: with --public, tag
        # the wrapper public-read just like every feature artifact (the bucket
        # policy from _resolve_bucket's auto-bucket path covers it too, but the
        # ACL keeps an explicit-basename public bucket working identically to
        # `publish`). Without --public the wrapper stays private (same-account).
        wrapper_extra: Dict[str, str] = {"ContentType": "application/x-yaml"}
        if make_public:
            wrapper_extra["ACL"] = "public-read"
        s3.put_object(
            Bucket=artifacts_bucket,
            Key=wrapper_key,
            Body=baked_wrapper.encode("utf-8"),
            **wrapper_extra,
        )
        wrapper_url = _public_https_url(artifacts_bucket, region, wrapper_key)
        self._log(
            f"[green]✓[/green] uploaded wrapper s3://{artifacts_bucket}/{wrapper_key}"
        )

        # 4b. When publishing public (opt-in, cross-account), sanity-check the
        # wrapper is publicly readable. A Quick-Create / cross-account deploy
        # fetches the wrapper template via plain HTTPS — if the object isn't
        # public (per-object ACL blocked, or account-level S3 Block Public
        # Access overriding the policy) the deploy fails late with HTTP 403.
        # Catch that here, before CFN starts spinning up.
        #
        # Without `--public` the artifacts are private (same-account flow):
        # an anonymous HEAD would legitimately 403, so the check is skipped.
        if make_public:
            self._assert_publicly_readable(wrapper_url)
        else:
            self._log(
                "[dim]Artifacts published privately (same-account). Pass "
                "--public to grant anonymous read for cross-account / "
                "Quick-Create pack deploys.[/dim]"
            )

        # 5. Quick-Create URL for one-click deploy via the AWS Console.
        quick_create = (
            f"https://console.aws.amazon.com/cloudformation/home?region={region}"
            f"#/stacks/quickcreate?templateURL={wrapper_url}"
            f"&stackName=idp-{feature_id}"
        )
        deploy_cmd = (
            f"idp-feature-cli deploy-pack --wrapper-url {wrapper_url} "
            f"--stack-name <stack-name> --admin-email <email>"
        )

        return PackPublishResult(
            feature_id=feature_id,
            version=version,
            artifact_bucket=artifacts_bucket,
            artifact_prefix=feat_prefix,
            feature_template_url=feature_template_url,
            host_template_url=host_template_url,
            wrapper_template_url=wrapper_url,
            quick_create_url=quick_create,
            deploy_command=deploy_cmd,
        )

    def _assert_publicly_readable(self, url: str) -> None:
        """Anonymous HEAD the published wrapper to confirm `--public` actually
        made it world-readable, failing fast (before CFN spins up) if not."""
        import urllib.error
        import urllib.request

        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=15) as r:  # nosec B310 - anonymous HEAD of the https artifact this tool just published
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"Published wrapper at {url} is not publicly reachable "
                f"(HTTP {exc.code}). The object did not become public — either "
                f"the per-object public-read ACL was rejected or account-level "
                f"S3 Block Public Access is overriding the bucket policy. "
                f"Disable BlockPublicPolicy + RestrictPublicBuckets at the "
                f"account level, or pass --bucket-basename pointing at a bucket "
                f"where you control BPA."
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"Couldn't HEAD the published wrapper at {url}: {exc}. "
                f"Cross-account pack deploys won't work until this is fixed."
            ) from exc


def deploy_pack(
    *,
    wrapper_url: str,
    stack_name: str,
    admin_email: str,
    region: str,
    extra_parameters: Optional[Dict[str, str]] = None,
    capabilities: Optional[List[str]] = None,
    wait: bool = True,
    console: Optional[Console] = None,
) -> str:
    """Deploy the published wrapper, **create-or-update**. Returns the stack ARN.

    Reads the wrapper template's parameter list and submits values for
    AdminEmail + a sensible default HostStackName. All other parameters
    inherit their (publish-time-baked) defaults.

    Create-if-absent, update-if-present — delegating to
    ``create_or_update_stack`` so ``deploy-pack`` matches the rest of the
    tooling (``deploy``, ``idp-cli deploy``, ``idp-mp-sim deploy``). Re-running
    against an existing wrapper stack performs a CloudFormation **update**
    rather than failing with ``AlreadyExistsException``; a no-op update (no
    changes) is treated as success.

    For pack wrappers whose feature install is a nested
    ``AWS::CloudFormation::Stack`` (e.g. claims-pack's ``CLAIMSFEATUREPACK``),
    an in-place wrapper update with a bumped ``FeatureVersion`` cascades into
    the nested stack and picks up the republished version-pinned artifacts —
    the desired way to push pack changes to an already-deployed wrapper. The
    wrapper's ``HostStack`` custom resource treats ``Update`` as a no-op, so a
    wrapper update won't disturb the running host.
    """
    cons = console or Console()
    cfn = boto3.client("cloudformation", region_name=region)

    # Inspect the wrapper to know what parameters it defines.
    try:
        validation = cfn.validate_template(TemplateURL=wrapper_url)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to validate wrapper at {wrapper_url}: {exc}"
        ) from exc

    expected_params = {p["ParameterKey"] for p in validation.get("Parameters") or []}
    submitted: List[Dict[str, str]] = []

    def _maybe_submit(key: str, value: str) -> None:
        if key in expected_params:
            submitted.append({"ParameterKey": key, "ParameterValue": value})

    _maybe_submit("AdminEmail", admin_email)
    # HostStackName is required (no default) on our wrapper. Default to a
    # safe ≤25-char prefix derived from the wrapper stack name.
    derived_host = f"{stack_name}-IDPAccelerator"[:25]
    _maybe_submit("HostStackName", derived_host)
    for k, v in (extra_parameters or {}).items():
        _maybe_submit(k, v)

    caps = capabilities or _CAPABILITIES

    return create_or_update_stack(
        cfn=cfn,
        stack_name=stack_name,
        template_url=wrapper_url,
        parameters=submitted,
        capabilities=caps,
        wait=wait,
        console=cons,
    )


def _first_failure_reason(cfn: Any, stack_arn: str) -> Optional[str]:
    """Return the human-readable reason for the failure in the CURRENT stack
    operation. Returns None if no failure event found or the API call errors.

    Scoped to the latest operation (scans newest-first only until the most
    recent "User Initiated" stack event) so a stale failure from a PRIOR
    create/update isn't misreported. Catches two kinds of failure:

      * a resource event with a *_FAILED status, and
      * a SAM/transform failure, which surfaces as a stack-level
        *_IN_PROGRESS event whose reason text contains "failed" (e.g.
        "Transform AWS::Serverless-2016-10-31 failed with: ...") — NOT a
        *_FAILED status, which is why the old check missed it.
    """
    try:
        events = cfn.describe_stack_events(StackName=stack_arn).get("StackEvents") or []
    except Exception:
        return None

    for ev in events:  # newest-first
        status = ev.get("ResourceStatus") or ""
        reason = ev.get("ResourceStatusReason") or ""
        logical = ev.get("LogicalResourceId") or ""

        if status.endswith("FAILED") or "failed" in reason.lower():
            return f"{logical}: {reason}".strip(": ").strip()

        # Boundary: the current operation begins at the most recent
        # "User Initiated" stack-level event. Stop before walking into a
        # previous operation's history.
        if "User Initiated" in reason:
            break
    return None


# A stack in one of these states is a failed CREATE that rolled back; it cannot
# be updated and must be deleted before re-deploying.
_CREATE_FAILED_DELETE_FIRST = {"ROLLBACK_COMPLETE", "ROLLBACK_FAILED"}
_CAPABILITIES = ["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"]


def _describe_one(cfn: Any, stack_name: str) -> Optional[Dict[str, Any]]:
    """Return the single Stack description for `stack_name`, or None if it
    does not exist. Any other error propagates."""
    try:
        return cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    except Exception as exc:  # botocore ClientError
        msg = str(exc)
        if "does not exist" in msg or "ValidationError" in msg:
            return None
        raise


def create_or_update_stack(
    *,
    cfn: Any,
    stack_name: str,
    template_url: str,
    parameters: List[Dict[str, str]],
    wait: bool = True,
    console: Optional[Console] = None,
    capabilities: Optional[List[str]] = None,
) -> str:
    """Create the stack if absent, else update it. Returns the stack ARN.

    Self-contained (boto3 only — no idp_sdk dependency). Handles the
    "No updates are to be performed" no-op as success, refuses to update a
    stack stuck in a CREATE-failed ROLLBACK_COMPLETE (must be deleted first),
    and surfaces the first failure-event reason on a terminal failure.
    """
    cons = console or Console()
    caps = capabilities or _CAPABILITIES

    existing = _describe_one(cfn, stack_name)
    if existing is not None and existing["StackStatus"] in _CREATE_FAILED_DELETE_FIRST:
        raise RuntimeError(
            f"Stack {stack_name} is in {existing['StackStatus']} (a failed "
            f"CREATE that rolled back). CloudFormation cannot update it — delete "
            f"it first:\n    aws cloudformation delete-stack --stack-name "
            f"{stack_name}\nthen re-run deploy."
        )

    is_update = existing is not None
    try:
        if is_update:
            cons.log(f"▸ Updating stack {stack_name}")
            resp = cfn.update_stack(
                StackName=stack_name,
                TemplateURL=template_url,
                Parameters=parameters,
                Capabilities=caps,
            )
            stack_arn = resp["StackId"]
        else:
            cons.log(f"▸ Creating stack {stack_name}")
            resp = cfn.create_stack(
                StackName=stack_name,
                TemplateURL=template_url,
                Parameters=parameters,
                Capabilities=caps,
                OnFailure="DELETE",
            )
            stack_arn = resp["StackId"]
    except Exception as exc:  # botocore ClientError
        msg = str(exc)
        if "No updates are to be performed" in msg:
            cons.log("[green]✓[/green] No changes — stack already up to date")
            return existing["StackId"] if existing else stack_name
        raise RuntimeError(
            f"{'Update' if is_update else 'Create'} failed: {exc}"
        ) from exc

    if not wait:
        return stack_arn

    success_status = "UPDATE_COMPLETE" if is_update else "CREATE_COMPLETE"
    cons.log(f"▸ Waiting for {success_status}…")
    deadline = time.time() + 60 * 60
    last_status: Optional[str] = None
    while time.time() < deadline:
        # Use the StackId (full ARN) — it stays valid even after a failed
        # create with OnFailure=DELETE tears the named stack down.
        desc = _describe_one(cfn, stack_arn)
        if desc is None:
            raise RuntimeError(
                f"Stack {stack_name} no longer exists (a CREATE_FAILED event "
                f"with OnFailure=DELETE likely tore it down). Last observed "
                f"status was {last_status!r}. Check the CloudFormation Console."
            )
        status = desc["StackStatus"]
        last_status = status
        if status == success_status or status in (
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
        ):
            cons.log(f"[green]✓[/green] Stack {status}")
            return stack_arn
        if status.endswith(("FAILED", "ROLLBACK_COMPLETE")):
            reason = _first_failure_reason(cfn, stack_arn)
            detail = f": {reason}" if reason else ""
            raise RuntimeError(f"Stack {stack_name} settled in {status}{detail}")
        time.sleep(15)
    raise RuntimeError(f"Timed out waiting for stack {stack_name}")


# Public-read bucket-policy prefixes for the AUTO-CREATED artifacts bucket
# (applied by ensure_artifacts_bucket on --public). These match the real
# published layout: feature/pack artifacts + the baked wrapper live under
# `extensions/<id>/...` (the same version-free layout `publish` uses), and the
# host accelerator (publish_host_accelerator / `idp-cli publish`) under
# `host/...`. Objects are ALSO tagged public-read per-object (mirroring
# `publish`), which covers any custom `--prefix` that nests the layout under
# `<prefix>/extensions/...`; this policy is the convenience layer for the
# default (unprefixed) auto-bucket case.
_PACK_PUBLIC_PREFIXES = ("extensions/", "host/")

# Sid of the deny-non-TLS statement every IDP-created bucket carries (the
# CloudFormation-managed buckets in template.yaml use the same Sid).
_ENFORCE_SSL_SID = "EnforceSSLOnly"


def _partition(region: str) -> str:
    """ARN partition for ``region`` — mirrors ``arn:${AWS::Partition}:`` in the
    templates so GovCloud/China buckets get correct ARNs."""
    try:
        return boto3.Session().get_partition_for_region(region)
    except Exception:
        if region.startswith("us-gov-"):
            return "aws-us-gov"
        if region.startswith("cn-"):
            return "aws-cn"
        return "aws"


def _enforce_ssl_statement(bucket: str, region: str) -> Dict[str, Any]:
    """Deny every principal all S3 actions on ``bucket`` over plain HTTP."""
    bucket_arn = f"arn:{_partition(region)}:s3:::{bucket}"
    return {
        "Sid": _ENFORCE_SSL_SID,
        "Effect": "Deny",
        "Principal": "*",
        "Action": "s3:*",
        "Resource": [bucket_arn, f"{bucket_arn}/*"],
        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
    }


def _statement_list(policy: Optional[Dict[str, Any]]) -> List[Any]:
    """Return ``policy``'s statements as a list, whatever form they took.

    The IAM grammar allows ``Statement`` to be **either** a single statement
    object **or** an array of them ("The Statement element can contain a single
    statement or an array of individual statements" —
    https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_elements_statement.html).
    Iterating the single-object form directly would yield its *keys*, silently
    replacing an operator's statement with the strings ``"Sid"``, ``"Effect"``,
    … — so normalize before touching it.

    Kept in sync with ``idp_sdk._core.s3_security.statement_list``; this module
    is deliberately self-contained (boto3 only, no idp_sdk dependency), so the
    logic is duplicated rather than imported. Fix both copies together.
    """
    if not policy:
        return []
    raw = policy.get("Statement", [])
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return list(raw)
    raise ValueError(
        f"Unsupported S3 bucket-policy Statement type {type(raw).__name__!r}: "
        f"expected an object or a list of objects."
    )


def apply_enforce_ssl_only(
    s3: Any,
    bucket: str,
    region: str,
    *,
    console: Optional[Console] = None,
    raise_on_error: bool = True,
) -> bool:
    """Add (or refresh) the ``EnforceSSLOnly`` statement on ``bucket``'s policy.

    Strictly additive: any other statement (including the opt-in
    ``PackPublicArtifactsRead`` grant) is preserved, and re-running is
    idempotent. Unlike Block Public Access — which we never touch on a bucket
    we didn't create — this only ever *tightens* access, so it's safe to apply
    to a pre-existing bucket.

    Returns True when the statement is in place. With ``raise_on_error=False``
    a failure logs a warning and returns False, for the pre-existing-bucket
    case where the operator may own the bucket policy.
    """
    cons = console or Console()
    try:
        try:
            existing = json.loads(s3.get_bucket_policy(Bucket=bucket)["Policy"])
        except Exception as exc:
            if "NoSuchBucketPolicy" not in str(exc):
                raise
            existing = None

        desired = _enforce_ssl_statement(bucket, region)
        if existing:
            statements = [
                s
                for s in _statement_list(existing)
                if not (isinstance(s, dict) and s.get("Sid") == _ENFORCE_SSL_SID)
            ]
            statements.append(desired)
            merged = {
                "Version": existing.get("Version", "2012-10-17"),
                "Statement": statements,
            }
        else:
            merged = {"Version": "2012-10-17", "Statement": [desired]}

        if merged != existing:
            s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(merged))
            cons.log(f"[green]✓[/green] applied {_ENFORCE_SSL_SID} policy to {bucket}")
        return True
    except Exception as exc:
        message = (
            f"Could not apply the {_ENFORCE_SSL_SID} bucket policy to {bucket!r}: "
            f"{exc}. This policy denies non-TLS requests to the bucket; add it "
            f"manually if your account restricts s3:PutBucketPolicy."
        )
        if raise_on_error:
            raise RuntimeError(message) from exc
        cons.log(f"[yellow]⚠ {message}[/yellow]")
        return False


def _pack_public_bucket_policy(bucket: str, partition: str = "aws") -> Dict[str, Any]:
    """Bucket policy granting `s3:GetObject` to anyone for the prefixes
    pack publishing uses. Required so cross-account CFN deploys can
    download the wrapper / host / feature templates and Lambda zips.

    This is the auto-bucket convenience layer (applied by
    ``ensure_artifacts_bucket`` when ``--public`` creates/owns the bucket).
    Publicness of the individual objects is ALSO set per-object via
    ``ACL=public-read`` (mirroring ``publish``), so an explicit-basename
    public bucket works the same way without relying on this policy.
    """
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PackPublicArtifactsRead",
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:GetObject",
                # Partition-aware: a hardcoded arn:aws: S3 resource matches
                # nothing in aws-us-gov / aws-cn, so the policy would grant no
                # access there while appearing to apply.
                "Resource": [
                    f"arn:{partition}:s3:::{bucket}/{p}*" for p in _PACK_PUBLIC_PREFIXES
                ],
            }
        ],
    }


def ensure_artifacts_bucket(
    *,
    region: str,
    console: Optional[Console] = None,
    make_public: bool = False,
) -> str:
    """Auto-generate the per-account artifacts bucket name and create it
    if missing. Returns the bucket name.

    Mirrors `idp-cli deploy --from-code` for the bucket name so the same
    bucket can host both flows.

    Security model (matches `idp-cli publish`): **private by default**.

      * Without ``make_public`` (the default), the bucket is treated as a
        same-account artifacts bucket. A freshly-created bucket has all
        four S3 Block Public Access (BPA) flags enabled. A *pre-existing*
        bucket is left untouched — we NEVER weaken its BPA settings, so a
        manual security remediation can't be silently reverted by a
        publish run.
      * Either way the ``EnforceSSLOnly`` bucket-policy statement (deny all
        S3 actions when ``aws:SecureTransport`` is false) is applied — it is
        purely additive hardening, so unlike BPA it's also applied to a
        pre-existing bucket (best-effort there, since the operator may own
        the bucket policy).
      * With ``make_public=True`` (opt-in, e.g. ``--make-public``), the
        bucket's ``BlockPublicPolicy``/``RestrictPublicBuckets`` flags are
        relaxed and a bucket policy granting public read on the
        ``extensions/*`` and ``host/*`` prefixes is applied/merged. This is
        only needed for sharing published artifacts for *cross-account* pack
        deploys,
        where the deploying account's CloudFormation + feature stack fetch
        the wrapper/host/feature templates and Lambda code zips via plain
        HTTPS (object ACLs don't work — modern buckets default to
        BucketOwnerEnforced, which rejects ACLs entirely).
    """
    cons = console or Console()
    sts = boto3.client("sts", region_name=region)
    account_id = sts.get_caller_identity()["Account"]
    bucket = f"idp-accelerator-artifacts-{account_id}-{region}"
    s3 = boto3.client("s3", region_name=region)
    bucket_created = False
    try:
        s3.head_bucket(Bucket=bucket)
        cons.log(
            f"[green]✓[/green] using existing artifacts bucket [bold]{bucket}[/bold]"
        )
    except Exception as exc:
        msg = str(exc)
        if "404" in msg or "NoSuchBucket" in msg or "Not Found" in msg:
            cons.log(f"[yellow]▸ creating artifacts bucket {bucket}[/yellow]")
            try:
                if region == "us-east-1":
                    s3.create_bucket(Bucket=bucket)
                else:
                    s3.create_bucket(
                        Bucket=bucket,
                        CreateBucketConfiguration={"LocationConstraint": region},
                    )
                bucket_created = True
            except Exception as create_exc:
                raise RuntimeError(
                    f"Failed to create artifacts bucket {bucket!r}: {create_exc}"
                ) from create_exc
        else:
            raise RuntimeError(
                f"Cannot access artifacts bucket {bucket!r}: {exc}"
            ) from exc

    # Deny non-TLS access. Additive, so safe on a pre-existing bucket too —
    # but only fatal when we created the bucket (and therefore own its policy).
    apply_enforce_ssl_only(
        s3, bucket, region, console=cons, raise_on_error=bucket_created
    )

    if not make_public:
        # Secure by default. Never weaken Block Public Access on a bucket
        # we didn't just create — that would silently revert any manual
        # security remediation an operator applied.
        if bucket_created:
            secure_pab = {
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            }
            try:
                s3.put_public_access_block(
                    Bucket=bucket,
                    PublicAccessBlockConfiguration=secure_pab,
                )
                cons.log(
                    "[green]✓[/green] enabled S3 Block Public Access on new "
                    "artifacts bucket"
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Couldn't enable Block Public Access on new bucket {bucket}: {exc}"
                ) from exc
        else:
            cons.log(
                "[dim]Block Public Access settings left unchanged on existing "
                "bucket (private same-account deploy). Pass --make-public to "
                "share artifacts for cross-account pack deploys.[/dim]"
            )
        return bucket

    # ---- make_public=True: opt-in public artifacts (cross-account) ----
    apply_public_artifacts_policy(s3, bucket, console=cons)
    return bucket


def apply_public_artifacts_policy(
    s3: Any, bucket: str, *, console: Optional[Console] = None
) -> None:
    """Relax BlockPublicPolicy/RestrictPublicBuckets and apply (or merge) the
    ``extensions/*`` + ``host/*`` public-read bucket policy on ``bucket``.

    Opt-in only — callers invoke this when the operator passed ``--public``.
    It grants anonymous HTTPS read on the published-artifact prefixes so a
    *deploying* account's CloudFormation + feature stack (which fetch the
    wrapper/host/feature templates and Lambda code zips over plain HTTPS, not
    SigV4) can read them during cross-account pack deploys. Same-account/
    private flows never call this, so a bucket's Block Public Access settings
    are left untouched.

    Raises ``RuntimeError`` with an actionable message if the public policy
    cannot be applied (e.g. account-level S3 Block Public Access).
    """
    cons = console or Console()
    # Allow bucket-level public policy (it stays blocked by default on
    # newly-created buckets; pre-existing buckets may also have block-all).
    # We loudly check the result rather than ignoring failures — a bucket
    # with BlockPublicPolicy=True or RestrictPublicBuckets=True will reject
    # the public-prefix policy and cross-account pack deploys will 403 at
    # download time. Better to fail here with a clear message.
    desired_pab = {
        "BlockPublicAcls": True,
        "IgnorePublicAcls": True,
        "BlockPublicPolicy": False,
        "RestrictPublicBuckets": False,
    }
    try:
        s3.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration=desired_pab,
        )
    except Exception as exc:
        raise RuntimeError(
            f"Couldn't update PublicAccessBlock on {bucket}: {exc}. "
            f"Pack deploys need BlockPublicPolicy=False + "
            f"RestrictPublicBuckets=False so the wrapper can be downloaded "
            f"cross-account. Check account-level S3 Block Public Access settings."
        ) from exc

    # Verify the change took effect (account-level block-public-access can
    # silently override; some IAM policies allow PutPublicAccessBlock but
    # the new state still has both flags True).
    try:
        actual = s3.get_public_access_block(Bucket=bucket)[
            "PublicAccessBlockConfiguration"
        ]
    except Exception:
        actual = {}
    if actual.get("BlockPublicPolicy") or actual.get("RestrictPublicBuckets"):
        raise RuntimeError(
            f"Bucket {bucket} has BlockPublicPolicy={actual.get('BlockPublicPolicy')!r} "
            f"or RestrictPublicBuckets={actual.get('RestrictPublicBuckets')!r} after "
            f"PutPublicAccessBlock — the change didn't stick. This usually means the "
            f"AWS account has account-level S3 Block Public Access enabled. Disable "
            f"those settings on account 's3control put-public-access-block', or pass "
            f"--bucket-basename pointing at a bucket where you control the BPA."
        )

    # Set or merge the public-prefix bucket policy.
    try:
        caller_arn = boto3.client("sts").get_caller_identity().get("Arn")
        partition = (caller_arn or "arn:aws:").split(":")[
            1
        ] or "aws"  # arn-partition-ok: fallback used only to PARSE the partition out
    except Exception:  # noqa: BLE001
        partition = "aws"
    desired = _pack_public_bucket_policy(bucket, partition)
    try:
        existing_resp = s3.get_bucket_policy(Bucket=bucket)
        existing = json.loads(existing_resp["Policy"])
        # Normalize first — Statement may legally be a single object, and
        # iterating that would yield its keys rather than the statement.
        existing_stmts = _statement_list(existing)
        # Drop any prior PackPublicArtifactsRead and append the fresh one.
        merged_stmts = [
            s
            for s in existing_stmts
            if not (isinstance(s, dict) and s.get("Sid") == "PackPublicArtifactsRead")
        ]
        merged_stmts.extend(desired["Statement"])
        merged = {"Version": "2012-10-17", "Statement": merged_stmts}
        if merged != existing:
            s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(merged))
            cons.log(
                "[green]✓[/green] merged public-read policy into existing bucket policy"
            )
    except s3.exceptions.from_code("NoSuchBucketPolicy"):
        s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(desired))
        cons.log("[green]✓[/green] applied public-read policy to bucket")
    except Exception as exc:
        raise RuntimeError(
            f"Failed to set public-read policy on {bucket!r}: {exc}"
        ) from exc


def publish_host_accelerator(
    *,
    source_dir: Path,
    artifacts_bucket: str,
    artifacts_prefix: str,
    region: str,
    console: Optional[Console] = None,
) -> str:
    """Run `idp-cli publish` to (re)publish the IDP accelerator artifacts
    under the given bucket/prefix, then make idp-main.yaml plus the version
    directory public-read so cross-account pack deploys can fetch them.
    Returns the public HTTPS URL of idp-main.yaml.

    The caller is expected to have boto3 credentials in the environment.

    Why this lives here rather than inside the host: the host's publish
    flow is the `idp-cli publish` command (the supported successor to the
    now-deprecated `publish.py` script) that operators run once per
    release. This helper just shells out to it when --build
    accelerator|all is requested, so the deploy-pack one-liner can do
    everything end-to-end without changing the host's publish toolchain.
    """
    cons = console or Console()
    if not (source_dir / "publish.py").is_file():
        raise FileNotFoundError(
            f"{source_dir / 'publish.py'} not found — pass --source-dir to "
            f"point at the IDP accelerator repo root."
        )

    # `idp-cli publish --bucket-basename <basename> --prefix <prefix>
    # --region <region>` — region is appended to the basename to form the
    # bucket. We already have the full bucket name, so strip a trailing
    # -<region> if present, else use the full name (idp-cli will look up
    # the bucket by basename + region; if our bucket doesn't follow the
    # convention, the user needs to use a basename-style bucket).
    suffix = f"-{region}"
    bucket_basename = (
        artifacts_bucket[: -len(suffix)]
        if artifacts_bucket.endswith(suffix)
        else artifacts_bucket
    )
    cmd = [
        sys.executable,
        "-m",
        "idp_cli.cli",
        "publish",
        "--source-dir",
        str(source_dir),
        "--bucket-basename",
        bucket_basename,
        "--prefix",
        artifacts_prefix,
        "--region",
        region,
    ]
    cons.log(f"▸ Running {' '.join(cmd[2:])}")
    subprocess.run(cmd, cwd=source_dir, check=True)

    # idp-cli publish wrote the main template to <bucket>/<prefix>/idp-main.yaml
    # plus a content-hashed version dir (e.g. 0.5.12.dev4/) holding nested
    # templates + Lambda zips. Public read is granted by the bucket policy
    # `host/*` prefix set up by ensure_artifacts_bucket — no per-object
    # ACL calls needed (and ACLs would fail on BucketOwnerEnforced buckets
    # anyway). Just compute and return the public URL.
    main_key = f"{artifacts_prefix}/idp-main.yaml"
    url = _public_https_url(artifacts_bucket, region, main_key)
    cons.log(f"[green]✓[/green] Host accelerator published: {url}")
    return url
