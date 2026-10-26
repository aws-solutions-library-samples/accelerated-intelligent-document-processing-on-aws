# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import contextlib
import html
import json
import logging
import mimetypes
import os

import boto3
import document_scope
import key_scope
import s3_targets
from botocore.config import Config
from botocore.exceptions import ClientError
from log_sanitizer import sanitize_event_for_logging

# Set up logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger('idp_common.bedrock.client').setLevel(os.environ.get("BEDROCK_LOG_LEVEL", "INFO"))
# Get LOG_LEVEL from environment variable with INFO as default


class ReportedServerError(RuntimeError):
    """A 500 whose message is written for the caller, so the dispatcher passes it on.

    Declared locally because this resolver does not import ``idp_common``; the
    dispatcher matches the class by name. The canonical class, and the rule for
    when to raise it, is ``idp_common.api_adapter.ReportedServerError``.
    """


# Force Signature Version 4 for presigned URLs. The IDP buckets are encrypted
# with SSE-KMS, and S3 rejects SigV2-signed requests for KMS-encrypted objects
# ("Requests specifying Server Side Encryption with AWS KMS managed keys require
# AWS Signature Version 4"). Also pin the regional endpoint so the signed host
# matches the bucket's region.
s3_client = boto3.client(
    "s3",
    region_name=os.environ.get("AWS_REGION"),
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)


# Bucket allow-list for get_file_contents.
# --------------------------------------------
# The schema for getFileContents accepts an arbitrary `s3Uri` from any
# authenticated caller holding a group, and this function's execution role can read
# several IDP buckets. The allow-list is what stops the operation being a generic
# S3-read gadget for whatever that role reaches.
#
# The rule itself lives in `s3_targets`, shared byte-for-byte with the write paths in
# upload_resolver and discovery_upload_resolver — one allow-list, not three that
# drift. `test_s3_targets_vendored.py` fails if a copy diverges. This path checks the
# BUCKET only: the write-once key rule in that module is for writes, and reading a
# revision body or a run manifest is legitimate.
#
# The bucket is not the whole question. Two of the allow-listed buckets are
# partitioned per user — the Configuration bucket by configuration profile, the Test
# Set bucket by test set — and the partition is in the KEY, so `key_scope` applies the
# caller's `allowedConfigVersions` / `allowedTestSets` to it. See that module for why
# a bucket-only check is the negation of both scope axes.
_ALLOWED_BUCKETS_ENV = s3_targets.BUCKET_ENV_NAMES
ALLOWED_BUCKETS = s3_targets.resolve_allowed_buckets()

# The two per-user-partitioned buckets, by name. Read at import like
# `ALLOWED_BUCKETS`, from the same environment the template wires. An unset name
# matches no bucket rather than every bucket — see `key_scope.scope_subject`.
CONFIGURATION_BUCKET = (os.environ.get("CONFIGURATION_BUCKET") or "").strip()
TEST_SET_BUCKET = (os.environ.get("TEST_SET_BUCKET") or "").strip()

# The two DOCUMENT buckets, by name, read the same way. `key_scope` has nothing to
# say about either — `scope_subject` returns None for every bucket but the two above,
# so `assert_key_in_scope` is a no-op on an Input- or Output-bucket key — which is
# why `document_scope` exists and why it is applied here rather than inside
# `key_scope`: it answers a different question (which DOCUMENT owns this key) using a
# different store (the tracking table).
INPUT_BUCKET, OUTPUT_BUCKET = document_scope.resolve_bucket_names()

# The tracking table, which records a document's `ConfigVersion`. An unset name is a
# wiring regression rather than a deployment without RBAC, and it denies: with no
# table there is no way to identify the document a key belongs to, and reading that
# as "unscoped" would make the per-document check removable by dropping an
# environment variable. See `_document_resolver`.
TRACKING_TABLE_NAME = (os.environ.get("TRACKING_TABLE_NAME") or "").strip()

# Per-container scope cache for the caller's `allowedConfigVersions`, same shape and
# TTL as `key_scope`'s. Separate from that module's because the two are consulted on
# disjoint bucket sets, so neither ever warms the other.
_user_scope_cache: dict = {}
_USER_SCOPE_CACHE_TTL = 60  # seconds

_dynamodb = None


def _dynamodb_resource():
    """The DynamoDB resource, built on first use.

    Not at import time, for the reason `key_scope` gives: the module is imported by a
    test suite that installs moto around it, and a resource built before the mock is
    in place talks to the real endpoint.
    """
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


@contextlib.contextmanager
def _budget_refusal():
    """Turn a lookup-budget overrun into the ordinary opaque refusal.

    ⚠️ **It is tempting to report this as a 400, and that is an oracle.** The request
    really is too broad and telling the caller so would be helpful — but how many
    tracking-table round trips a request costs depends on **which candidates exist**,
    because the walk stops at the first row it finds. So a caller who pads a batch to
    the budget boundary can read "400 too broad" versus "403 refused" as one bit about
    whether an arbitrary object key is a tracked document, for documents entirely
    outside their scope. That is exactly the distinction every refusal message here is
    worded to avoid, and it needs no timing analysis because the two answers are
    different status classes.

    So the caller gets the same string every other document-scope denial produces, and
    the log line is where the difference is recorded. Wrapped at both call sites rather
    than caught in `handler`, so a single-object field cannot report it as a 500 —
    which it did, while only the batch field converted it.

    ⚠️ The same reasoning fixes the exception CLASS, not just the status and the
    string. The class reaches the client as `errors[0].authzControl`, so raising the
    unattributed `PermissionError` here while `assert_document_in_scope` declares
    itself would put the bit back into the label: `resolver` on the budget overrun
    and `resource` on an ordinary denial is the same oracle in a different field.
    """
    try:
        yield
    except document_scope.DocumentLookupBudgetExceeded as exc:
        logger.warning(
            "Refusing a document-bucket read: %s. This is reported to the caller as "
            "an ordinary authorization refusal on purpose — see _budget_refusal.",
            exc,
        )
        raise s3_targets.ResourceAccessDenied(document_scope.REFUSAL) from exc


class _DocumentScope:
    """The caller's profile scope and the document lookups for ONE invocation.

    Both halves are lazy and both are shared across every key in a batch, which is
    what makes the per-document check affordable: a document's N page keys resolve
    the caller's scope once and walk their common ancestors once, so a 27-page view
    costs one UsersTable read (itself cached per container) and a handful of
    tracking-table reads rather than 27 of each.

    The scope is resolved on the first **document-bucket** key and not before, so a
    Configuration- or Test-Set-bucket read pays nothing for a check that does not
    apply to it.
    """

    def __init__(self, event):
        self._event = event
        self._resolved = False
        self._allowed = None
        self._resolver = None

    def _resolve(self):
        if not self._resolved:
            self._allowed = document_scope.resolve_caller_scope(
                self._event,
                users_table_name=os.environ.get("USERS_TABLE_NAME", ""),
                # The FACTORY, not a resource: an Admin, and a request carrying no
                # usable identity, return before any table is read, and passing
                # `_dynamodb_resource()` here built a client for them anyway — which
                # raises outright where no region is configured.
                dynamodb_factory=_dynamodb_resource,
                cache=_user_scope_cache,
                cache_ttl=_USER_SCOPE_CACHE_TTL,
            )
            self._resolved = True
        return self._allowed

    def _document_resolver(self):
        if self._resolver is None:
            if not TRACKING_TABLE_NAME:
                logger.error(
                    "Refusing a document-bucket object read: TRACKING_TABLE_NAME is "
                    "not configured, so the document that owns the key cannot be "
                    "identified."
                )
                # Deliberately the UNATTRIBUTED class. An unwired table is a template
                # fault rather than a decision about the object the caller named —
                # the same reading `s3_targets` gives an empty bucket allow-list — so
                # `authzControl: "resolver"` is true here, and it is read strictly,
                # which is what keeps a deployment fault visible instead of filed
                # under a per-object control working as intended.
                raise PermissionError(document_scope.REFUSAL)
            self._resolver = document_scope.DocumentResolver(
                _dynamodb_resource(), TRACKING_TABLE_NAME
            )
        return self._resolver

    def prime(self, targets) -> None:
        """Resolve every document a request's keys could belong to, in one go.

        An optimisation and never a correctness requirement: `assert_allowed` resolves
        whatever it still needs. What this buys is round trips — a document's N page
        keys share all but their two deepest ancestors, so priming the union turns
        a 269-page view's ~540 candidates into six `BatchGetItem` calls instead of
        269 separate ones.

        Called with every ``(bucket, key)`` the request names, **before** the first
        key is judged. A refusal still refuses the whole request, so priming does not
        change what work a refused request does.
        """
        lists = []
        for bucket, key in targets:
            candidates = document_scope.document_key_candidates(
                bucket, key, input_bucket=INPUT_BUCKET, output_bucket=OUTPUT_BUCKET
            )
            if candidates:
                lists.append(candidates)
        if not lists:
            return
        if self._resolve() is None:
            # Unrestricted caller: no document is looked up at all, so there is
            # nothing to prime. Decided here as well as in `assert_allowed` so that
            # priming cannot become the thing that makes an unscoped request read the
            # table.
            return
        with _budget_refusal():
            self._document_resolver().prime(lists)

    def assert_allowed(self, bucket: str, key: str) -> None:
        """Refuse unless the caller's `allowedConfigVersions` covers this key's
        document.

        A no-op for a bucket no document partitions, decided before anything is
        resolved so the cheap case stays cheap, and a no-op for a caller whose
        `allowedConfigVersions` is unset — scoping is opt-in per user, which is the
        same answer `listDocuments` gives. See `document_scope` for what that does
        and does not close.
        """
        if not document_scope.is_document_bucket(
            bucket, input_bucket=INPUT_BUCKET, output_bucket=OUTPUT_BUCKET
        ):
            return
        allowed = self._resolve()
        with _budget_refusal():
            document_scope.assert_document_in_scope(
                bucket,
                key,
                allowed,
                self._document_resolver() if allowed is not None else None,
                input_bucket=INPUT_BUCKET,
                output_bucket=OUTPUT_BUCKET,
            )


def _validate_bucket(bucket: str) -> None:
    """Reject the request if `bucket` is not one this deployment owns.

    Raises `PermissionError` -> HTTP 403 `Unauthorized`. See `s3_targets` for why the
    exception type and the message prefix both matter, and why an empty allow-list
    fails closed.
    """
    s3_targets.assert_bucket_allowed(bucket, ALLOWED_BUCKETS, logger=logger)


def _validate_key_scope(bucket: str, key: str, event) -> None:
    """Reject the request unless the caller's scope covers this key.

    Enforces the caller's `allowedConfigVersions` on a Configuration-bucket
    `config_revisions/<profile>/` key, and their `allowedTestSets` on a
    Test-Set-bucket `<test_set_id>/` key. Both come from the UsersTable, via the
    canonical fail-closed lookups in `key_scope`; a scope that cannot be *evaluated*
    denies rather than reading as unrestricted.

    A no-op for every key no scope axis partitions. Raises `PermissionError` ->
    HTTP 403 `Unauthorized` otherwise, with a message that names neither the object
    nor the scope subject, so a refusal is not an existence oracle — the same
    contract `_validate_bucket` keeps.

    The rule itself is deliberately NOT here. `key_scope` imports
    `config_scope.scope_allows` and `testset_scope.assert_can_access_test_set`
    unchanged, so this path matches on the same code as the configuration resolver,
    both document-list resolvers and the test-set resolver. What a static reference
    check over this file can establish is that this operation's own code path consults
    the scope; that it *enforces* it is pinned by test_key_scope.py.
    """
    key_scope.assert_key_in_scope(
        bucket,
        key,
        event,
        configuration_bucket=CONFIGURATION_BUCKET,
        test_set_bucket=TEST_SET_BUCKET,
    )


# Presigned GET URLs expire after this many seconds. Short-lived: the UI
# fetches immediately after receiving the URL.
_PRESIGNED_URL_EXPIRY_SECONDS = 300


def _parse_and_validate_uri(event, scope=None):
    """Parse `s3Uri`/`versionId` from the GraphQL args, validate, and return
    ``(bucket, key, version_id)``. Shared by all three handler branches.

    ``scope`` is the invocation's :class:`_DocumentScope`. It is optional only so the
    two single-object branches can be called without threading one through; a missing
    one is built here, which keeps every call site authorized rather than making the
    per-document check something a caller can omit. The batch branch passes its own
    so one scope resolution and one set of document lookups serve every key.
    """
    s3_uri = event['arguments']['s3Uri']
    # Optional S3 object VersionId — used to fetch the exact bytes of a
    # prior document version (see document version history). None fetches
    # the current object.
    version_id = event['arguments'].get('versionId')
    return _parse_and_validate_one(
        s3_uri, version_id, event, scope if scope is not None else _DocumentScope(event)
    )


def _parse_uri(s3_uri, version_id):
    """Parse one ``s3://bucket/key`` into ``(bucket, key, version_id)``.

    Parsing only — **no authorization happens here**, and nothing may be done with
    the result but hand it to :func:`_parse_and_validate_one`. It is separate solely
    so the batch field can learn which documents a request could touch before the
    first tracking-table round trip; every check still runs per key, in the one
    function all three fields go through.

    ⚠️ Do not grow a second caller that acts on this return value. A parse function
    that looks like a validate function is how a path ends up reaching S3 with no
    check in front of it, which is the shape #1033 was about.
    """
    if not isinstance(s3_uri, str):
        raise ValueError("Invalid S3 URI: expected s3://<bucket>/<key>")
    logger.info(f"Processing S3 URI: {s3_uri}"
                + (f" (versionId={version_id})" if version_id else ""))

    # Parse S3 URI to get bucket and key. The URI must be of the
    # form `s3://<bucket>/<key>`. We intentionally do NOT accept
    # virtual-hosted-style HTTPS URIs here because they require a
    # completely different parsing path.
    #
    # Parse with a plain string split rather than urllib.parse.urlparse:
    # object keys may contain '#' (e.g. "Borrowing_Notice_#2.pdf/pages/1/
    # result.json"), which urlparse treats as a URL fragment delimiter and
    # silently truncates, producing a wrong key and a NoSuchKey error.
    #
    # `ValueError`, not a bare `Exception`: the dispatcher maps `ValueError` to
    # **400 BadRequest** and everything it does not recognise to 500
    # `InternalError`. A malformed argument is the caller's, and reporting it as a
    # server fault both misleads whoever is debugging it and pollutes the 5xx rate.
    if not s3_uri.startswith("s3://"):
        raise ValueError("Invalid S3 URI: expected s3://<bucket>/<key>")
    # Strip scheme, then split once into bucket and key.
    parts = s3_uri[len("s3://"):].split("/", 1)
    if len(parts) < 2 or not parts[0]:
        raise ValueError("Invalid S3 URI: expected s3://<bucket>/<key>")
    bucket, key = parts
    if not key:
        raise ValueError("Invalid S3 URI: key is required")

    if version_id == "null":
        version_id = None
    return bucket, key, version_id


def _parse_and_validate_one(s3_uri, version_id, event, scope):
    """Parse and authorize one ``s3://bucket/key``, returning
    ``(bucket, key, version_id)``.

    Every authorization control this resolver has is here, in the one function all
    three fields reach before touching S3: the bucket allow-list, the per-user key
    scope, and the per-document scope. ``getFilePresignedUrl`` and its batch form
    mint capabilities that carry their own authorization for the life of the
    signature, so a check either field could be added without is a check that will
    eventually be missing from it.
    """
    bucket, key, version_id = _parse_uri(s3_uri, version_id)

    # Enforce that the requested bucket belongs to this IDP stack's
    # known bucket set — prevents use of this resolver as a generic
    # S3-read gadget.
    _validate_bucket(bucket)

    # Then enforce KEY-level scope, for the two buckets whose contents are
    # partitioned per user. Both authorization checks live here, in the one function
    # both resolver fields call before touching S3, rather than in the two handlers:
    # `getFilePresignedUrl` mints a capability that carries its own authorization for
    # the life of the signature, so a check it could be added without is a check that
    # will eventually be missing from it. Nothing below this line runs for a refused
    # request, so a denial never reaches S3 and cannot be timed against one that does.
    _validate_key_scope(bucket, key, event)

    # Then enforce per-DOCUMENT scope, for the Input and Output buckets. `key_scope`
    # has nothing to say about either — `scope_subject` returns None for every bucket
    # but the Configuration and Test Set ones, so the call above is a no-op on a
    # document key — which is why this is a separate control rather than another
    # branch in there: it answers "which document owns this key" from the tracking
    # table, against the caller's `allowedConfigVersions`. See `document_scope` for
    # what it closes (the cross-document read for a scoped caller) and what it
    # deliberately does not (anything for an unscoped one; scoping is opt-in).
    scope.assert_allowed(bucket, key)

    return bucket, key, version_id


# Content types a browser renders natively; anything else is left to download.
_INLINE_RENDERABLE_TYPES = ("application/pdf",)
_INLINE_RENDERABLE_PREFIXES = ("image/", "text/")

# Types a browser renders by EXECUTING them. Served inline from the bucket origin,
# an uploaded evil.html or evil.svg would run script there instead of downloading —
# so these are forced to download even though their prefixes are renderable. The
# bucket is a separate origin from the app and an authenticated uploader is
# required, which is why this is defence in depth rather than a live hole.
_NEVER_INLINE_TYPES = frozenset(
    {
        "text/html",
        "text/xml",
        "application/xhtml+xml",
        "image/svg+xml",
        "text/javascript",
    }
)


def _is_executable_type(content_type):
    """True when a browser would run script while rendering this type."""
    if not content_type:
        return False
    return content_type.split(";")[0].strip().lower() in _NEVER_INLINE_TYPES


def _is_inline_renderable(content_type):
    """True when a browser can display this type in-page without executing it."""
    if not content_type:
        return False
    if _is_executable_type(content_type):
        return False
    lowered = content_type.split(";")[0].strip().lower()
    return lowered in _INLINE_RENDERABLE_TYPES or lowered.startswith(
        _INLINE_RENDERABLE_PREFIXES
    )


def _handle_presigned_url(event):
    """Return a short-lived presigned GET URL for the requested object.

    Used for files too large to return through :func:`_handle_file_contents`
    (Lambda's synchronous response is capped at 6 MB). The browser fetches the
    URL directly from S3, so the file bytes never traverse this Lambda.
    """
    bucket, key, version_id = _parse_and_validate_uri(event)
    logger.info(f"Generating presigned URL for bucket: {bucket}, key: {key}")

    # HEAD the object first so we (a) surface NoSuchKey as a clean error before
    # minting a URL, and (b) return size/contentType the UI uses to decide how
    # to handle the content.
    head_kwargs = {"Bucket": bucket, "Key": key}
    if version_id:
        head_kwargs["VersionId"] = version_id
    head = s3_client.head_object(**head_kwargs)

    content_type = head.get('ContentType', '')
    if not content_type or content_type in ('binary/octet-stream', 'application/octet-stream'):
        content_type = mimetypes.guess_type(key)[0] or 'text/plain'

    presign_params = {"Bucket": bucket, "Key": key}
    if version_id:
        presign_params["VersionId"] = version_id

    # Override the response headers instead of trusting stored metadata: objects
    # uploaded without a ContentType are binary/octet-stream, which a browser
    # downloads rather than renders. Overriding here also repairs objects already
    # stored with the wrong type.
    presign_params["ResponseContentType"] = content_type
    if _is_inline_renderable(content_type):
        presign_params["ResponseContentDisposition"] = "inline"
    elif _is_executable_type(content_type):
        # Must be explicit: with no disposition the browser decides from the
        # Content-Type alone, so text/html would still render — merely declining to
        # say "inline" is not the same as saying "attachment".
        presign_params["ResponseContentDisposition"] = "attachment"

    presigned_url = s3_client.generate_presigned_url(
        "get_object",
        Params=presign_params,
        ExpiresIn=_PRESIGNED_URL_EXPIRY_SECONDS,
    )

    logger.info(f"Generated presigned URL (size={head['ContentLength']}, "
                f"contentType={content_type})")
    return {
        'presignedUrl': presigned_url,
        'contentType': content_type,
        'size': head['ContentLength'],
    }


# The most objects one `getFilePresignedUrls` call may name.
#
# Why there is a number at all: the argument is a list the caller supplies, and an
# uncapped list is a request amplifier — one API call that signs an arbitrary number
# of URLs, walks an arbitrary number of tracking-table ancestors and returns an
# arbitrary number of bytes. A cap is the only thing that bounds all three, and it has
# to be enforced rather than documented.
#
# Why 1000. Three bounds, measured offline against botocore rather than estimated:
#
#   * **Response size.** The binding cap is the dispatcher's synchronous invoke
#     response, 6 MiB (API Gateway's payload quota is 10 MB, so it is not the
#     constraint). One entry is ~1.46 KB with an 800-character session token and
#     ~1.86 KB with a 1,200-character one, so 1,000 entries is 1.4-1.8 MiB — 23-30%
#     of the cap. The largest document in `samples/` is 27 pages
#     (`lending_package-long.pdf`), which is 38.5 KiB, 0.6%.
#   * **Time.** Signing is local SigV4 and costs nothing per key over the network:
#     1,000 signings measured at 0.357 s total (0.357 ms each). This handler issues no
#     `head_object`, so there is no per-key S3 round trip to multiply — see the
#     docstring below. The dispatcher abandons a resolver at 20 s, and 1,000 keys is
#     well inside that even allowing for a slower Lambda CPU.
#   * **Document lookups.** Bounded separately and more tightly by
#     `document_scope.MAX_DOCUMENT_LOOKUP_BATCHES`, because those are DynamoDB round
#     trips rather than CPU. A batch over one document collapses to a handful of them
#     however many pages it names, because its keys share all but their two deepest
#     ancestors and the whole request's candidates are resolved 100 per round trip.
#
# What happens past it: the request is **refused** with a 400 naming the cap, not
# silently truncated — a truncated list would return fewer URLs than asked for and
# the caller would render blank pages with no error. The UI chunks its requests well
# below this (`PRESIGN_BATCH_SIZE` in `src/ui/src/api/presign-batch.ts`), so a
# document with more pages than the cap still loads; the cap is what stops a
# hand-built request, not what bounds a document.
MAX_BATCH_OBJECTS = 1000


def _batch_requests(event):
    """The ``files`` argument, validated into ``[(s3_uri, version_id), ...]``.

    Raises ``ValueError`` -> **400** for every malformed shape. The dispatcher's
    schema-derived validator checks that the argument is a list of objects and
    nothing deeper (`validation._scalar_type_ok` validates an input-object type
    shallowly by design), so the per-entry rules are this function's to apply.
    """
    files = event.get('arguments', {}).get('files')
    if not isinstance(files, list):
        raise ValueError("Invalid request: 'files' must be a list")
    if not files:
        raise ValueError("Invalid request: 'files' must name at least one object")
    if len(files) > MAX_BATCH_OBJECTS:
        raise ValueError(
            f"Invalid request: 'files' names {len(files)} objects, more than the "
            f"maximum of {MAX_BATCH_OBJECTS} per call. Split the request."
        )

    requests = []
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid request: 'files'[{index}] must be an object")
        s3_uri = entry.get('s3Uri')
        if not isinstance(s3_uri, str) or not s3_uri:
            raise ValueError(
                f"Invalid request: 'files'[{index}].s3Uri is required and must be "
                "a string"
            )
        version_id = entry.get('versionId')
        if version_id is not None and not isinstance(version_id, str):
            raise ValueError(
                f"Invalid request: 'files'[{index}].versionId must be a string"
            )
        requests.append((s3_uri, version_id))
    return requests


def _handle_presigned_urls(event):
    """Return a short-lived presigned GET URL for each of several objects.

    One call for a whole document's page images instead of one per page. The client
    signed these itself until the Identity Pool's authenticated role stopped granting
    the document buckets; routing them through here is what puts the group floor, the
    bucket allow-list and the per-document scope check in front of every document
    byte, and batching is what keeps the cost of that constant in page count rather
    than linear (issue #1033).

    Two deliberate differences from :func:`_handle_presigned_url`.

    **No ``head_object``.** The single form HEADs each object to surface a missing key
    as a clean 400 and to return the stored ``contentType`` and ``size``. The
    consumers of this field — the page-image viewer and the thumbnail hook — keep
    only the URL and discard both, and a per-key HEAD is the one per-key *network*
    cost in the whole operation, so N serial HEADs is what would make a batch slow
    rather than the signing. The content type is derived from the key's extension
    instead, which is what the single form already falls back to for an object stored
    as ``binary/octet-stream``, and a missing object surfaces to the browser as a
    failed image load, which both surfaces already handle (a placeholder SVG and a
    ``null`` entry respectively).

    **A refusal fails the WHOLE batch**, rather than being reported per entry. A
    per-entry refusal would make one call an N-key authorization oracle: the caller
    would learn, key by key, which documents are in their scope. The single form's
    refusal text already says nothing about the object; the batch keeps that property
    by having exactly one outcome for the request. Authorization also runs over every
    entry **before** the first URL is minted, so no partial result exists to leak.
    """
    requests = _batch_requests(event)
    scope = _DocumentScope(event)

    # Pass 0 — parse every URI, then resolve the documents they could belong to in
    # one go. Parsing is separated from authorizing only so the whole request's
    # candidates are known before the first tracking-table round trip; every
    # authorization check still happens per key in pass 1, through the same function
    # the single-object fields use.
    parsed = [_parse_uri(s3_uri, version_id) for s3_uri, version_id in requests]
    scope.prime([(bucket, key) for bucket, key, _version in parsed])

    # Pass 1 — authorize everything. A refusal here aborts the whole request.
    targets = [
        _parse_and_validate_one(s3_uri, version_id, event, scope)
        for s3_uri, version_id in requests
    ]

    logger.info("Signing %d presigned URLs in one batch", len(targets))

    # Pass 2 — sign. Local HMAC only; no S3 call is made by any line below.
    results = []
    for (bucket, key, version_id), (s3_uri, _requested_version) in zip(
        targets, requests
    ):
        content_type = mimetypes.guess_type(key)[0] or 'application/octet-stream'
        presign_params = {"Bucket": bucket, "Key": key}
        if version_id:
            presign_params["VersionId"] = version_id
        presign_params["ResponseContentType"] = content_type
        if _is_inline_renderable(content_type):
            presign_params["ResponseContentDisposition"] = "inline"
        elif _is_executable_type(content_type):
            # Same explicit `attachment` the single form sets, and for the same
            # reason: declining to say "inline" is not the same as saying
            # "attachment", and a `.svg` or `.html` key reaches this branch.
            presign_params["ResponseContentDisposition"] = "attachment"
        results.append(
            {
                's3Uri': s3_uri,
                'presignedUrl': s3_client.generate_presigned_url(
                    "get_object",
                    Params=presign_params,
                    ExpiresIn=_PRESIGNED_URL_EXPIRY_SECONDS,
                ),
                'contentType': content_type,
            }
        )
    return {'results': results}


def _handle_file_contents(event):
    """Read a file's contents from S3 and return them inline (subject to
    Lambda's 6 MB synchronous response cap)."""
    bucket, key, version_id = _parse_and_validate_uri(event)

    logger.info(f"Fetching from bucket: {bucket}, key: {key}")

    # Get object from S3 (optionally a specific version)
    get_kwargs = {"Bucket": bucket, "Key": key}
    if version_id:
        get_kwargs["VersionId"] = version_id
    response = s3_client.get_object(**get_kwargs)

    # Get content type from S3 response or infer from file extension
    content_type = response.get('ContentType', '')
    if not content_type or content_type == 'binary/octet-stream' or content_type == 'application/octet-stream':
        content_type = mimetypes.guess_type(key)[0] or 'text/plain'

    logger.info(f"File content type: {content_type}")
    logger.info(f"File size: {response['ContentLength']}")

    # Read file content with error handling for different encodings
    try:
        # First try UTF-8
        file_content = response['Body'].read().decode('utf-8')
    except UnicodeDecodeError:
        # If UTF-8 fails, try with error handling
        try:
            response['Body'].seek(0)  # Reset the file pointer
            file_content = response['Body'].read().decode('utf-8', errors='replace')
            logger.warning("File content contained invalid UTF-8 characters that were replaced")
        except Exception as decode_error:
            # Last resort - if it's a binary file format with text extension
            logger.error(f"Failed to decode content with error handling: {str(decode_error)}")
            return {
                'content': "This file contains binary content that cannot be displayed as text.",
                'contentType': content_type,
                'size': response['ContentLength'],
                'isBinary': True
            }

    # For HTML content, escape the HTML to prevent XSS
    if content_type.startswith('text/html') or content_type.startswith('application/xhtml+xml'):
        file_content = html.escape(file_content)

    # Return both content and metadata
    return {
        'content': file_content,
        'contentType': content_type,
        'size': response['ContentLength'],
        'isBinary': False
    }


def handler(event, context):
    """Resolver for ``getFileContents``, ``getFilePresignedUrl`` and
    ``getFilePresignedUrls``.

    Routes on the GraphQL ``fieldName``:
      - ``getFilePresignedUrls`` -> return one presigned GET URL per named object
        (no `head_object`, whole-batch refusal; see that handler).
      - ``getFilePresignedUrl`` -> return a presigned GET URL (no size limit;
        browser fetches bytes directly from S3).
      - ``getFileContents`` (default) -> return the file bytes inline (capped
        at Lambda's 6 MB synchronous response limit).

    Parameters:
        event (dict): Lambda event data containing GraphQL arguments
        context (object): Lambda context

    Returns:
        dict: Field-shaped response (see the three handlers above).

    Raises:
        PermissionError: the requested bucket is outside this deployment's
            allow-list, or the requested KEY is outside the caller's configuration-
            profile / test-set scope, or the DOCUMENT the key belongs to is outside
            their `allowedConfigVersions` (including when either scope could not be
            evaluated, or the document could not be identified, both of which deny).
            The dispatcher reports all of these as **403 Unauthorized**, with a
            message that discloses nothing about the object.
        ValueError: the `s3Uri` is malformed, or names an object that is not there;
            or, for the batch field, the `files` list is empty, malformed, longer
            than `MAX_BATCH_OBJECTS`, or spans more documents than one call may look
            up. The dispatcher reports these as **400 BadRequest**.
        Exception: a genuine server-side fault (the resolver's own role, the bucket
            policy or the KMS key refused it, or S3 failed). **500 InternalError**.
    """
    try:
        logger.info(f"Received event: {json.dumps(sanitize_event_for_logging(event))}")

        field_name = (event.get('info') or {}).get('fieldName', 'getFileContents')
        if field_name == 'getFilePresignedUrls':
            return _handle_presigned_urls(event)
        if field_name == 'getFilePresignedUrl':
            return _handle_presigned_url(event)
        return _handle_file_contents(event)

    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_message = e.response['Error']['Message']

        # NoSuchKey (get_object) and 404 (head_object) both mean "missing object",
        # which is the caller naming a key that is not there — a 400, not a 500.
        if error_code in ('NoSuchKey', '404', 'NoSuchVersion'):
            logger.info("Requested object is absent: %s", error_code)
            raise ValueError("File not found") from e

        # Everything else is the DEPLOYMENT failing to read an object the caller was
        # allowed to ask for: the caller's own credentials never touch S3 here, so an
        # AccessDenied is this function's role, the bucket policy or the KMS key —
        # a server fault, and correctly a 500.
        #
        # ⚠️ S3 answers a missing key with 403 AccessDenied rather than 404 when the
        # reader lacks s3:ListBucket on the bucket, so an absent object can land here
        # and be reported as a fault. That is not hypothetical in this deployment:
        # REPORTING_BUCKET is in the allow-list above but the function's Policies
        # block grants it no S3ReadPolicy, and s3:GetObjectVersion is granted only on
        # the output bucket — so every read there fails this way whether or not the
        # object exists. The log line below is what distinguishes it; the message to
        # the caller deliberately cannot, because guessing would be worse.
        #
        # The raw S3 message is NOT returned. It distinguished NoSuchBucket from
        # AccessDenied from Forbidden, which made this a working existence oracle
        # over the allow-listed buckets, and it read as an authorization problem to
        # the UI (S3's text for a denial is literally "Access Denied").
        logger.error(
            "S3 refused or failed a read this deployment is expected to be able to "
            "do: %s - %s. Check this function's S3 read policy, the bucket policy "
            "and the KMS key grant. NOTE: S3 also reports a MISSING key as "
            "AccessDenied/403 when s3:ListBucket is absent, so this may be an "
            "absent object rather than a permissions fault.",
            error_code,
            error_message,
        )
        raise ReportedServerError(
            "This deployment could not read the requested file. "
            "Contact an administrator."
        ) from e

    except (PermissionError, ValueError):
        # Re-raised unchanged, deliberately. The catch-all below used to wrap every
        # exception as `Exception(f"Error fetching file: {e}")`, which destroyed the
        # class name AND moved the "Unauthorized" prefix off the front of the
        # message — the two things the dispatcher uses to choose a status. So the
        # bucket allow-list refusal, every malformed-URI rejection and every
        # missing-object report all arrived as 500 `InternalError`. The refusals are
        # already logged at their raise site; re-logging them here as "Unexpected
        # error" is what made a deliberate denial indistinguishable from a crash in
        # CloudWatch and in any alarm watching ERROR.
        raise

    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}", exc_info=True)
        raise Exception(f"Error fetching file: {str(e)}") from e