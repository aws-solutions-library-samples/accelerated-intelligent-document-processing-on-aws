# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# src/lambda/upload_resolver/index.py

import json
import logging
import os

import boto3
from botocore.config import Config

from idp_common import s3_targets
from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.prefix_mappings import (
    PrefixMappingStore,
    canonical_key,
    resolve_config_assignment,
)
from idp_common.config_scope import (
    ScopeLookupError,
    caller_email_from_claims,
    caller_sub_from_claims,
    resolve_allowed_config_versions,
)
from idp_common.utils.log_sanitizer import sanitize_event_for_logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger("idp_common.bedrock.client").setLevel(
    os.environ.get("BEDROCK_LOG_LEVEL", "INFO")
)
# Get LOG_LEVEL from environment variable with INFO as default

# Two S3 clients: one for the presigned URLs handed to the browser, one for this
# resolver's own S3 API calls. See the long note in test_set_resolver/index.py —
# same reasoning, same failure mode. S3_ENDPOINT_URL is the S3 interface VPC
# endpoint host; presigning with it is correct (offline signing, the URL is for
# the browser), but calling S3 with it from this NON-VPC-attached function hangs
# on private addresses it cannot route to, which the 29s REST API Gateway
# integration ceiling turns into an unexplained 504.
#
# Data-plane calls here are listSampleDocuments / uploadSampleDocument
# (get_object on the manifest, list_objects_v2, copy_object).
_s3_endpoint_url = os.environ.get("S3_ENDPOINT_URL") or None
_s3_addressing = "virtual" if _s3_endpoint_url else "path"

# Browser-facing presigned POST/GET URLs only.
s3_presign_config = Config(
    signature_version="s3v4",
    s3={"addressing_style": _s3_addressing},
)
s3_presign_client = boto3.client(
    "s3", endpoint_url=_s3_endpoint_url, config=s3_presign_config
)

# Bounds for the data-plane client, applied ONLY where there is private-network
# S3 configuration to get wrong (see the fuller note in test_set_resolver). A
# public deployment has no endpoint or route to misconfigure and keeps botocore's
# stock timeouts, so this change cannot introduce a new way for it to fail.
# 2 total attempts, worst case 2 x (3s connect + 8s read) = 22s, inside the 29s
# API Gateway integration budget.
_S3_DATAPLANE_BOUNDS = (
    {
        "connect_timeout": 3,
        "read_timeout": 8,
        "retries": {"mode": "standard", "max_attempts": 1},
    }
    if _s3_endpoint_url
    else {}
)

# Every actual S3 API call this resolver makes.
s3_config = Config(
    signature_version="s3v4",
    s3={"addressing_style": "path"},
    **_S3_DATAPLANE_BOUNDS,
)
s3_client = boto3.client("s3", config=s3_config)

# Which bucket and key an upload may target. `bucket` and `prefix` are request
# arguments and this function's role holds write on every bucket the deployment uses,
# so the request is bounded here or not at all. One allow-list shared byte-for-byte
# with the read path in get_file_contents_resolver and the write path in
# discovery_upload_resolver — see s3_targets, and test_s3_targets_vendored.py, which
# fails if a copy diverges.
ALLOWED_BUCKETS = s3_targets.resolve_allowed_buckets()


def _caller_in_groups(event, allowed):
    """Defense-in-depth RBAC check against the caller's Cognito groups.

    The schema restricts this field via @aws_cognito_user_pools(cognito_groups),
    but we also enforce the group server-side so the operation is never reachable
    by an unauthorized caller even if the schema directive is missing or
    misconfigured (e.g. the prior @aws_auth directive, which AppSync silently
    ignores on a multi-auth API).
    """
    groups = (event.get("identity") or {}).get("claims", {}).get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return bool(set(allowed).intersection(groups))


_dynamodb = boto3.resource("dynamodb")

# Per-container scope cache. The UI calls uploadDocument once per file, so a
# multi-file upload would otherwise cost one UsersTable query per file. Only
# successful lookups are cached; a failure is never remembered as an answer.
_user_scope_cache: dict = {}


def _prefix_mappings():
    """The deployment's config prefix mappings, or ``[]``.

    Fails open on a read error, deliberately, and the asymmetry with the scope check
    below is the point. A mapping that cannot be read means a destination is
    *unmapped* as far as this request is concerned, which is the state the
    deployment was in before the feature existed; a *scope* that cannot be read
    means the caller cannot be placed, and that denies (see
    ``_allowed_config_versions``). Refusing every upload because a routing table is
    unreadable would make a transient DynamoDB error an outage of the UI's primary
    action.
    """
    table_name = os.environ.get("CONFIGURATION_TABLE_NAME")
    if not table_name:
        return []
    try:
        return PrefixMappingStore(_dynamodb.Table(table_name)).list()
    except Exception as e:  # noqa: BLE001
        logger.error(
            "Could not read the configuration prefix mappings (%s); treating this "
            "destination as unmapped",
            e,
        )
        return []


def _allowed_config_versions(event):
    """The caller's configuration scope, or a refusal.

    Fail-closed: a scope that cannot be evaluated is not a caller without
    restrictions. ``resolve_allowed_config_versions`` raises ``ScopeLookupError`` for
    that case and this turns it into a ``PermissionError``, which the dispatcher maps
    to HTTP 403.
    """
    claims = (event.get("identity") or {}).get("claims", {}) or {}
    try:
        return resolve_allowed_config_versions(
            caller_email_from_claims(claims),
            users_table_name=os.environ.get("USERS_TABLE_NAME", ""),
            dynamodb=_dynamodb,
            caller_sub=caller_sub_from_claims(claims),
            cache=_user_scope_cache,
        )
    except ScopeLookupError:
        logger.warning(
            "Denying an upload: the caller's configuration scope could not be evaluated"
        )
        raise PermissionError(
            "Unauthorized: your configuration scope could not be determined."
        )


def _profile_exists(manager, cache=None):
    """A callable answering "does this profile head item exist?", or ``None``.

    ``ProjectionExpression`` matters: a configuration body is tens to hundreds of KB
    gzipped into the same item.

    ``cache`` memoizes the answer for one request. A batch sample expands to many
    files and the whole loop runs inside API Gateway's 29s integration ceiling, so
    without it the cost is two DynamoDB round trips per file for answers that
    cannot change within the call.
    """
    memo = {} if cache is None else cache

    def exists(profile):
        if profile not in memo:
            item = manager.table.get_item(
                Key={"Configuration": f"Config#{profile}"},
                ProjectionExpression="Configuration",
            ).get("Item")
            memo[profile] = bool(item)
        return memo[profile]

    return exists


def _published_revision(manager, cache=None):
    """``resolve_published_revision``, memoized for one request. See above."""
    memo = {} if cache is None else cache

    def published(profile):
        if profile not in memo:
            memo[profile] = manager.resolve_published_revision(profile)
        return memo[profile]

    return published


def _configuration_manager():
    """A ConfigurationManager over this deployment's table.

    ⚠️ **Refuses rather than degrading when the table is not wired.** Returning
    ``None`` here would leave ``active_profile`` unanswerable, and an upload that
    names no profile then resolves to no profile — which the scope guard cannot
    compare, so a scoped caller is *not refused*. That is fail-OPEN on the
    commonest upload shape there is, and it would silently disable the check this
    resolver exists to apply.

    The same reasoning as ``_allowed_config_versions``, which raises on an
    unreadable users table: the resolver and the environment variables that
    configure it are one CloudFormation resource deployed together, so an unset
    one is a template fault, and reading a template fault as "no restrictions" is
    how the fault becomes the vulnerability. ``queue_sender`` is the deliberate
    opposite and fails open, because it handles an S3 event with no caller to
    scope — there is nothing there for a refusal to protect.
    """
    table_name = os.environ.get("CONFIGURATION_TABLE_NAME")
    if not table_name:
        logger.error(
            "CONFIGURATION_TABLE_NAME is not set; refusing uploads rather than "
            "resolving a destination whose configuration scope cannot be checked. "
            "This is a deployment fault — the template wires it unconditionally."
        )
        raise PermissionError(
            "Unauthorized: uploads are not configured for this deployment."
        )
    return ConfigurationManager(table_name=table_name)


def resolve_destination(
    event,
    object_key,
    version=None,
    revision=None,
    mappings=None,
    exists_cache=None,
    revision_cache=None,
):
    """What configuration an upload to ``object_key`` would process under.

    This is the control that closes a gap predating prefix mappings:
    ``uploadDocument`` checked the caller's Cognito *group* and the target *bucket*,
    but never their ``allowedConfigVersions``. The ``version`` argument was stamped
    into the presigned POST verbatim, so a scoped Author could already put a document
    into any Configuration Profile in the deployment — which is the
    document-visibility partition for every scoped user — simply by naming it.

    Prefix mappings add a second route to that same gap: with a mapping in place, the
    *destination prefix* chooses the profile, with no metadata involved at all. So
    the check has to be on the **resolved** profile rather than on the requested one.
    Checking the request would leave the prefix route wide open; checking the
    resolution covers both with one rule, and keeps covering any future route that
    goes through ``resolve_config_assignment``.

    Returns the ``ConfigAssignment``. Raises ``PermissionError`` (HTTP 403) when the
    resolved profile is outside the caller's scope, and ``ValueError`` (HTTP 400)
    when a prefix mapping in ``reject`` conflict mode would refuse the upload at
    ingest — refusing here is strictly better than minting a URL for an upload that
    is going to fail.

    ⚠️ **Every seam ``resolve_config_assignment`` accepts must be supplied here, and
    with the same meaning the ingest path gives it.** This function answers the same
    question ``queue_sender`` will answer about the same object a moment later, and
    the two answers diverge the instant the seam sets differ — the pure resolver is
    deterministic, so a disagreement can only come from the inputs:

    * Omitting ``published_revision`` makes an unpinned request's effective revision
      ``None``, which then "disagrees" with a mapping pinned to the revision that
      profile has in fact published. A ``reject`` mapping would refuse an upload
      here that ingest accepts — a 400 on a legitimate upload the preview said was
      fine.
    * Omitting ``active_profile`` leaves the resolved profile ``None`` whenever no
      mapping matches and no ``version`` was named, and a ``None`` profile skips the
      scope guard entirely. That is the most common upload shape there is, so the
      scope check this function exists for would not run on it — the gap would be
      *reported* closed while the widest route stayed open.
    * Omitting ``profile_exists`` lets a mapping naming a deleted profile resolve to
      a phantom, where ingest would fall through and flag it.

    ``scripts/tests/test_prefix_mapping_call_sites.py`` asserts this across all four
    callers, because the resolver's own suite cannot see a caller by construction.
    """
    allowed = _allowed_config_versions(event)
    manager = _configuration_manager()
    assignment = resolve_config_assignment(
        object_key,
        metadata_profile=version,
        metadata_revision=revision,
        mappings=_prefix_mappings() if mappings is None else mappings,
        active_profile=manager.resolve_active_version,
        published_revision=_published_revision(manager, revision_cache),
        profile_exists=_profile_exists(manager, exists_cache),
        # `allowed` is empty for an unscoped caller, which `scope_allows` reads as
        # unrestricted — so this adds no restriction to the common case.
        allowed_profiles=list(allowed) if allowed else None,
    )
    if assignment.scope_denied:
        logger.warning(
            "Denying an upload to %r: the resolved Configuration Profile is outside "
            "the caller's scope %s",
            object_key,
            sorted(allowed),
        )
        # The message names neither the profile nor the scope. A 403 that reports
        # which profile it refused is a profile-name enumeration oracle, and profile
        # names are themselves access-controlled (getConfigVersions is
        # scope-filtered for exactly this reason). The detail goes to the log for
        # whoever triages it.
        raise PermissionError(
            "Unauthorized: that destination is governed by a Configuration Profile "
            "outside your allowed configuration scope."
        )
    if assignment.rejected:
        raise ValueError(assignment.reason)
    return assignment


def handler(event, context=None):
    """Dispatch upload-related resolver operations by GraphQL field name.

    Serves ``uploadDocument`` (presigned POST for local uploads),
    ``listSampleDocuments`` (read the bundled samples manifest), and
    ``uploadSampleDocument`` (server-side copy of a bundled sample into the
    InputBucket). ``uploadDocument`` remains the default when no field name is
    present so existing callers are unaffected.
    """
    logger.info(f"Received event: {json.dumps(sanitize_event_for_logging(event))}")

    field_name = (event.get("info") or {}).get("fieldName") or "uploadDocument"

    if field_name == "listSampleDocuments":
        return _handle_list_sample_documents(event)
    if field_name == "uploadSampleDocument":
        return _handle_upload_sample_document(event)
    return _handle_upload_document(event)


def _handle_upload_document(event):
    """Generate a presigned POST URL for a local-file S3 upload."""
    try:
        # Defense-in-depth: uploadDocument is an Admin+Author operation.
        if not _caller_in_groups(event, ("Admin", "Author")):
            raise PermissionError(
                "Unauthorized: uploadDocument requires Admin or Author group"
            )

        # Extract variables from the event
        arguments = event.get("arguments", {})
        file_name = arguments.get("fileName")
        content_type = arguments.get("contentType", "application/octet-stream")
        prefix = arguments.get("prefix", "")
        version = arguments.get("version")  # Optional version parameter
        revision = arguments.get("revision")  # Optional revision of that version

        if not file_name:
            raise ValueError("fileName is required")

        # Get bucket from arguments or fallback to INPUT_BUCKET if needed by patterns
        bucket_name = arguments.get("bucket")

        if not bucket_name and os.environ.get("INPUT_BUCKET"):
            # Support legacy pattern usage that relies on INPUT_BUCKET
            bucket_name = os.environ.get("INPUT_BUCKET")
            logger.info(f"Using INPUT_BUCKET fallback: {bucket_name}")
        elif not bucket_name:
            raise ValueError(
                "bucket parameter is required when INPUT_BUCKET is not configured"
            )

        # Sanitize file name to avoid URL encoding issues
        sanitized_file_name = file_name.replace(" ", "_")

        # Build the object key - only use prefix if provided, canonicalizing the
        # prefix so the key this POST is signed for is the same form everything
        # downstream reports.
        #
        # This is NOT what stops a non-canonical prefix evading a mapping:
        # `find_match` canonicalizes the object key itself, so `/finance/x.pdf`
        # resolves to the same mapping as `finance/x.pdf` whatever this site does,
        # and a `reject` mapping fires either way. What it buys is that the object
        # lands at the canonical key rather than at a near-duplicate differing only
        # in slashes — so the document's key, the `ConfigMappingPrefix` recorded
        # against it and what an operator sees in the bucket all agree, and a
        # re-upload of "the same" path cannot silently become a second object.
        prefix = canonical_key(prefix or "").rstrip("/")
        if prefix:
            object_key = f"{prefix}/{sanitized_file_name}"
        else:
            object_key = sanitized_file_name

        # Constrain the target. `bucket` and `prefix` both come from the request, and
        # this function's role holds write on every bucket the deployment uses, so the
        # request is bounded here or not at all. Same allow-list the read path uses,
        # plus the write-once key rule, which only write paths consult.
        # Raises PermissionError -> HTTP 403.
        s3_targets.assert_write_target_allowed(
            bucket_name, object_key, ALLOWED_BUCKETS, logger=logger
        )

        # Which CONFIGURATION this destination implies, and whether this caller may
        # put a document into it. Covers the `version` argument and the destination
        # prefix with one check -- see resolve_destination. Only for the Input
        # bucket: the other four write targets (ground-truth baselines, page images,
        # exports) are not documents and no mapping governs them.
        if bucket_name == os.environ.get("INPUT_BUCKET"):
            resolve_destination(event, object_key, version, revision)
            # Note what this deliberately does NOT do: it does not rewrite `version`
            # to the resolved profile. The metadata keeps recording what was
            # REQUESTED, and queue_sender records what happened and why
            # (ConfigSource, ConfigMappingPrefix) on the tracking row. Rewriting it
            # here would make the two agree at ingest, so a mapping that overrode a
            # user's selection would stop being a conflict -- costing the
            # PrefixMappingConflict metric and the UI's conflict badge on exactly
            # the case an operator wants to see. The UI already warns before upload,
            # so nothing is a surprise to the person uploading.

        # Generate a presigned POST URL for uploading
        logger.info(
            f"Generating presigned POST data for: {object_key} with content type: {content_type}"
        )

        # Prepare fields and conditions
        fields = {"Content-Type": content_type}
        conditions = [
            ["content-length-range", 1, 104857600],  # 1 Byte to 100 MB
            {"Content-Type": content_type},
        ]

        # Add version as metadata
        if version:
            fields["x-amz-meta-config-version"] = version
            conditions.append({"x-amz-meta-config-version": version})
            # A revision only means something in the context of a profile, so it
            # is only stamped when one was chosen. The queue processor pins the
            # profile's current revision when this is absent.
            if revision is not None:
                fields["x-amz-meta-config-revision"] = str(revision)
                conditions.append({"x-amz-meta-config-revision": str(revision)})

        # Presign client: this URL goes to the browser, so it must carry the
        # VPC-endpoint host in private deployments.
        presigned_post = s3_presign_client.generate_presigned_post(
            Bucket=bucket_name,
            Key=object_key,
            Fields=fields,
            Conditions=conditions,
            ExpiresIn=900,  # 15 minutes
        )

        logger.info(f"Generated presigned POST data: {json.dumps(presigned_post)}")

        # Return the presigned POST data and object key.
        # usePostMethod is a STRING ("true") per the schema (PresignedUploadUrl.
        # usePostMethod: String!) and the UI parses it via
        # usePostMethod.toLowerCase() === 'true'. AppSync used to coerce a bool
        # to a string; the REST dispatcher passes JSON through verbatim, so a
        # bool here reaches the UI as `true` and breaks .toLowerCase(). Keep it
        # a string.
        return {
            "presignedUrl": json.dumps(presigned_post),
            "objectKey": object_key,
            "usePostMethod": "true",
        }

    except Exception as e:
        logger.error(f"Error generating presigned URL: {str(e)}")
        raise


# Samples manifest key within the ConfigurationBucket (mirrors the publish-time
# _SAMPLES_MANIFEST_FILE / SAMPLES_MANIFEST_KEY default).
_SAMPLES_MANIFEST_KEY = os.environ.get(
    "SAMPLES_MANIFEST_KEY", "config_library/samples-manifest.json"
)


def _load_samples_manifest():
    """Read and parse config_library/samples-manifest.json from the ConfigurationBucket."""
    config_bucket = os.environ.get("CONFIGURATION_BUCKET")
    if not config_bucket:
        raise ValueError("CONFIGURATION_BUCKET is not configured")
    obj = s3_client.get_object(Bucket=config_bucket, Key=_SAMPLES_MANIFEST_KEY)
    manifest = json.loads(obj["Body"].read())
    return config_bucket, manifest.get("samples", [])


def _handle_list_sample_documents(event):
    """Return the bundled sample documents from the manifest (Admin/Author/Viewer)."""
    try:
        if not _caller_in_groups(event, ("Admin", "Author", "Viewer")):
            raise PermissionError(
                "Unauthorized: listSampleDocuments requires Admin, Author, or Viewer group"
            )
        _, samples = _load_samples_manifest()
        return {"success": True, "samples": samples, "error": None}
    except PermissionError:
        raise
    except Exception as e:
        logger.error(f"Error listing sample documents: {str(e)}")
        return {"success": False, "samples": None, "error": str(e)}


def _handle_upload_sample_document(event):
    """Copy a bundled sample from the ConfigurationBucket into the InputBucket.

    For ``kind=document`` copies the single object; for ``kind=batch`` copies
    every document under the sample's prefix. Stamps the config version as
    object metadata (config-version) so downstream processing selects the right
    configuration, mirroring the presigned-POST x-amz-meta-config-version path.
    The InputBucket "Object Created" EventBridge rule then drives processing.
    """
    try:
        if not _caller_in_groups(event, ("Admin", "Author")):
            raise PermissionError(
                "Unauthorized: uploadSampleDocument requires Admin or Author group"
            )

        arguments = event.get("arguments", {})
        sample_id = arguments.get("sampleId")
        # Same canonicalization as uploadDocument, so the two paths really are
        # bounded alike rather than only commented as such: `canonical_key` does
        # more than strip slashes (it drops `.` segments and collapses repeats),
        # and a future change to it would otherwise silently split the two.
        prefix = canonical_key(arguments.get("prefix") or "").rstrip("/")
        version = arguments.get("version")
        revision = arguments.get("revision")
        if not sample_id:
            raise ValueError("sampleId is required")

        input_bucket = os.environ.get("INPUT_BUCKET")
        if not input_bucket:
            raise ValueError("INPUT_BUCKET is not configured")

        config_bucket, samples = _load_samples_manifest()
        sample = next((s for s in samples if s.get("id") == sample_id), None)
        if sample is None:
            raise ValueError(f"Unknown sampleId: {sample_id}")

        s3_key = sample.get("s3Key", "")
        kind = sample.get("kind", "document")

        # Resolve the source object keys within the ConfigurationBucket.
        if kind == "batch":
            source_prefix = s3_key.rstrip("/") + "/"
            paginator = s3_client.get_paginator("list_objects_v2")
            source_keys = [
                obj["Key"]
                for page in paginator.paginate(
                    Bucket=config_bucket, Prefix=source_prefix
                )
                for obj in page.get("Contents", [])
                if not obj["Key"].endswith("/")
            ]
        else:
            source_keys = [s3_key]

        if not source_keys:
            raise ValueError(f"No source files found for sample: {sample_id}")

        extra_args = {}
        if version:
            extra_args["Metadata"] = {"config-version": version}
            if revision is not None:
                extra_args["Metadata"]["config-revision"] = str(revision)
            extra_args["MetadataDirective"] = "REPLACE"

        # Read the mapping set ONCE, outside the loop. A batch sample expands to
        # many files and this path runs inside API Gateway's 29s integration
        # ceiling, so a strongly-consistent GetItem per file is latency this
        # operation did not previously spend. The scope lookup is already
        # per-container cached.
        mappings = _prefix_mappings()
        # Memoized across the loop for the same reason `mappings` is hoisted: the
        # answers cannot change within one request, and a batch sample pays per
        # file otherwise.
        exists_cache: dict = {}
        revision_cache: dict = {}

        object_keys = []
        for source_key in source_keys:
            base_name = os.path.basename(source_key)
            target_key = f"{prefix}/{base_name}" if prefix else base_name
            # `prefix` is caller-supplied here too, so the copy destination is bounded
            # the same way as the presigned upload above -- including the
            # configuration-scope check on whatever profile the destination resolves
            # to. This path always writes to the Input bucket, so there is no
            # bucket condition to apply as there is on uploadDocument.
            s3_targets.assert_write_target_allowed(
                input_bucket, target_key, ALLOWED_BUCKETS, logger=logger
            )
            resolve_destination(
                event,
                target_key,
                version,
                revision,
                mappings=mappings,
                exists_cache=exists_cache,
                revision_cache=revision_cache,
            )
            s3_client.copy_object(
                CopySource={"Bucket": config_bucket, "Key": source_key},
                Bucket=input_bucket,
                Key=target_key,
                **extra_args,
            )
            object_keys.append(target_key)

        logger.info(
            f"Copied {len(object_keys)} sample file(s) for '{sample_id}' to "
            f"{input_bucket}"
        )
        return {"success": True, "objectKeys": object_keys, "error": None}
    except PermissionError:
        raise
    except Exception as e:
        logger.error(f"Error uploading sample document: {str(e)}")
        return {"success": False, "objectKeys": None, "error": str(e)}
