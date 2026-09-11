# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pipeline-hooks dispatcher Lambda.

Invoked by the host's Step Functions workflow at each pipeline extension
point (preprocessing, postOcr, postClassification, postExtraction,
postRuleValidation, postSummarization, postprocessing). Reads the active
configuration version from the host's ConfigurationTable and dispatches to
the hook Lambda(s) registered for that point.

Hooks are stored *inline in the active config version* — under each
section — so that activating a different config version atomically swaps
the hook set:

    Config#<version>
      preprocessing:            # standalone section — runs FIRST, before the
        enabled: true           # BDA/pipeline routing. A SINGLE inline hook:
        arn: <lambda-arn>       # arn/args/onError live directly on the section
        onError: fail           # (no list), so it reads cleanly in the config UI.
        args: [ { key, value }, ... ]   # generic key/value config for the hook
      ocr:
        postHook:               # post-step points keep a LIST of hooks
          - { featureId, arn, order, onError, enabled, args }
      classification:
        postHook: [ ... ]
      extraction:
        postHook: [ ... ]
      rule_validation:
        postHook: [ ... ]
      summarization:
        postHook: [ ... ]
      postprocessing:           # standalone section — runs LAST, after
        enabled: true           # evaluation. Same SINGLE flat-hook shape as
        arn: <lambda-arn>       # `preprocessing`, deliberately symmetrical.
        onError: continue
        args: [ { key, value }, ... ]

The dispatcher's return value includes a top-level `halt` flag (true if any
successful hook returned result.halt == true, at a point where halting is
meaningful — see _HALT_CAPABLE_POINTS) so the workflow's post-hook Choice can
short-circuit the execution via a stable JSONPath.

It ALSO always includes a top-level `document` — the document the next
workflow step should consume. A hook that wants to change the document for
downstream steps returns `{"updatedDocument": <doc>}`; the dispatcher
validates it, threads it into the next hook at the same point, and returns it
here. Hooks that return anything else (the historical, read-only contract) get
their input document echoed back verbatim, so the pipeline is byte-identical
to the pre-mutation behavior. The state machine copies this value into the
canonical path the next step reads via a small `Apply<Point>HookDocument`
Pass state (see statemachine/workflow.asl.json).

Resolution rules:
  1. If the SFN input has `document.config_version`, use it.
  2. Else, scan the table for the row with IsActive=true.
  3. Else, fall back to `Config#default`.

Step 1 is the normal path: the queue processor pins `config_version` on every
document before starting the execution (see src/lambda/queue_processor), and
Document.compress() carries it in the lightweight wrapper specifically so this
dispatcher can honor it without decompressing. Steps 2-3 are a DEFENSIVE
fallback for the narrow cases that still arrive unpinned — a DynamoDB failure
during pinning, or a document queued by an older release. They are not dead
code, but reaching them is worth noticing, which is why step 3 logs at WARNING
and the returned payload always names the version actually used.

Returns immediately when the requested step has no `postHook` entries,
keeping the no-vertical-pack overhead at one DDB GetItem.
"""

from __future__ import annotations

import base64
import copy
import gzip
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_CONFIG_TABLE = os.environ.get("CONFIGURATION_TABLE_NAME", "")
_TRACKING_TABLE = os.environ.get("TRACKING_TABLE", "")
# Needed only to spill a hook-returned INLINE document dict to S3 in the same
# compressed-wrapper shape the step Lambdas use. Hooks that write the document
# themselves and return a compressed reference need nothing here.
_WORKING_BUCKET = os.environ.get("WORKING_BUCKET", "")

# The key a hook sets to hand a modified document to the next workflow step.
# Deliberately NOT "document": no existing hook returns `updatedDocument`, so a
# read-only hook that happens to echo its input under "document" cannot
# accidentally start mutating the pipeline.
_UPDATED_DOC_KEY = "updatedDocument"

# Identity fields a hook may never change. Rewriting these mid-pipeline would
# corrupt the tracking-table row and the output S3 prefixes (both keyed off the
# document id / input key). A hook that needs a DIFFERENT document should spawn
# one and `halt` (the pattern the PII preprocessing hook uses) rather than
# swapping identity underneath the running execution.
_IMMUTABLE_DOC_FIELDS = (
    "id",
    "document_id",
    "input_bucket",
    "input_key",
    "output_bucket",
)

# Ceiling on an inline document dict returned by a hook, before compression.
# Lambda's own 6MB synchronous response limit is the real gate; this bounds the
# JSON we will re-serialize and PutObject.
_MAX_INLINE_DOC_BYTES = 5 * 1024 * 1024

# Workflow CONTROL fields that ride on the document payload but are NOT part of
# the Document model — the queue processor injects them from the resolved config
# (see src/lambda/queue_processor/index.py) purely so the state machine can read
# them via JSONPath:
#
#   $.document.use_bda          -> RouteByProcessingMode Choice
#   $.document.bda_project_arn  -> BDA_InvokeDataAutomation Parameters
#
# Because they are not Document fields, a hook doing the natural
# load -> mutate -> return round-trip through idp_common drops them. Losing
# `use_bda` makes RouteByProcessingMode fail the execution outright
# ("Invalid path '$.document.use_bda'"), so the dispatcher carries them forward
# from the inbound payload onto any hook-returned document. A hook that
# deliberately sets one keeps its value; only absent keys are back-filled.
_CONTROL_FIELDS = ("use_bda", "bda_project_arn")

# S3 key prefix for compressed document state, matching what the step Lambdas
# use (idp_common.models.Document.compress) and what the dispatcher's IAM policy
# grants s3:PutObject on. Both the keys we write and the URIs we accept from a
# hook are constrained to it.
_COMPRESSED_DOC_PREFIX = "compressed_documents/"

# Fields the STATE MACHINE reads straight off the document payload via JSONPath:
#
#   $.document.num_pages                        -> BDA_CheckExistingData Choice
#   $.document.status                           -> (BDA branch)
#   $.ClassificationResult.document.sections    -> ProcessSections Map ItemsPath
#
# The dispatcher's own inline path always emits them, as does idp_common's
# Document.compress(), but a hand-rolled compressed reference need not. An
# absent one is not a hook error the workflow can survive — an unresolvable
# ItemsPath/Choice path fails the execution with States.Runtime — so they are
# back-filled from the previous document rather than left missing.
_REQUIRED_WRAPPER_FIELDS = ("num_pages", "status", "sections")

# Map hook point -> config section under the active config version.
#
# `preprocessing` and `postprocessing` are standalone top-level sections (NOT
# nested under a processing step), and each holds a SINGLE FLAT hook rather than
# a list (see _FLAT_HOOK_POINTS):
#
#   preprocessing  — runs FIRST, before the BDA/pipeline routing decision, so it
#                    fires in both processing modes and even when OCR is
#                    disabled. Operates on the source document.
#   postprocessing — runs LAST, after evaluation and before the workflow's
#                    terminal state, on the shared tail so it too fires in both
#                    processing modes. Operates on the finished document, and a
#                    mutation there reaches the persisted DynamoDB row, the
#                    reporting/Athena rows, and the UI.
#
# Every other point is a POST-STEP point whose hooks live in a
# `<section>.postHook` LIST.
_HOOK_TO_STEP = {
    "preprocessing": "preprocessing",
    "postOcr": "ocr",
    "postClassification": "classification",
    "postExtraction": "extraction",
    # postAssessment removed in v0.6 — confidence assessment is folded into
    # extraction, so its post-step hook point no longer exists.
    "postRuleValidation": "rule_validation",
    "postSummarization": "summarization",
    "postprocessing": "postprocessing",
}

# Points whose config section IS the hook: `arn`/`args`/`onError`/`enabled` live
# directly on the section, with no list. Membership is explicit rather than
# derived from the point name — `postprocessing` is flat but starts with "post",
# so the old `startswith("pre")` heuristic would have looked for a
# `postprocessing.postHook` list that does not exist.
_FLAT_HOOK_POINTS = frozenset({"preprocessing", "postprocessing"})

# Points where a hook's `halt` request means something. `preprocessing` halts
# before any processing happens (the PII-redaction "spawned a redacted copy"
# pattern). At `postprocessing` the document is already finished and there is
# nothing downstream to skip, so a `halt` there is reported and ignored rather
# than silently appearing to do something.
_HALT_CAPABLE_POINTS = frozenset({"preprocessing"})


def _is_flat_hook_point(point: str) -> bool:
    """True when the point's config section is itself a single hook (no list)."""
    return point in _FLAT_HOOK_POINTS


_CONFIG_METADATA_FIELDS = {
    "Configuration",
    "CreatedAt",
    "UpdatedAt",
    "IsActive",
    "Description",
    "Managed",
    "BdaProjectArn",
    "BdaSyncStatus",
    "BdaLastSyncedAt",
}

# The version used when no IsActive row can be resolved. It always exists (the
# host seeds Config#default) but carries no feature hooks, so resolving to it
# unintentionally is the silent-no-hooks failure mode of issue #599.
_FALLBACK_VERSION = "default"

_dynamodb = boto3.resource("dynamodb")
# Hooks run synchronously through this client, so its read timeout must cover
# the longest hook (the PII preprocessing hook budgets 900s); botocore's
# default ~60s read timeout would sever the invoke mid-run. Retries are
# disabled: hooks are not guaranteed idempotent, and the state machine's own
# Retry handles the transient Lambda.* errors.
_lambda = boto3.client(
    "lambda",
    config=boto3.session.Config(
        read_timeout=910, connect_timeout=10, retries={"max_attempts": 0}
    ),
)
_s3 = boto3.client("s3")


def _decompress_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Inline mirror of idp_common.config.configuration_manager._decompress_item.
    Returns the config payload as a plain dict, regardless of whether the
    DDB row was stored compressed (gzip+base64) or inline.
    """
    storage = item.get("_config_storage")
    compressed = item.get("_compressed_config")
    if storage == "compressed" and compressed is not None:
        try:
            raw = compressed.value if hasattr(compressed, "value") else compressed
            if isinstance(raw, str):
                raw = base64.b64decode(raw)
            text = gzip.decompress(raw).decode("utf-8")
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception as exc:  # noqa: BLE001
            logger.warning("Config decompress failed: %s", exc)
            return {}
    return {
        k: v
        for k, v in item.items()
        if k not in _CONFIG_METADATA_FIELDS and not k.startswith("_")
    }


def _resolve_active_version(table: Any, pinned: Optional[str]) -> Optional[str]:
    """Determine which config version's hooks the dispatcher should read.

    Order: explicit pin from the document → IsActive=true row → default.
    Returns the version segment ("claims-pack-v0.1.0", "default", …) or
    None if the active row's key is malformed.

    The scan MUST paginate. DynamoDB applies both `Limit` and the implicit
    1MB page size to the items *examined*, not the items that pass
    `FilterExpression` — so a single scan call returns "no active version"
    whenever the active row happens to sort beyond the first page, which
    gets likelier with every config version an admin saves or feature
    installs. Falling back to `Config#default` then silently disables every
    registered hook while the workflow still succeeds: the exact
    growth-triggered failure in issue #599, observed live with the active row
    at scan position 33 of 35.
    """
    if pinned:
        return pinned
    # The active-profile pointer resolves this in one get_item; the scan below
    # stays as the fallback for a stack that has not activated a profile since
    # the pointer was introduced.
    try:
        pointer = table.get_item(
            Key={"Configuration": "Config#__active"},
            ProjectionExpression="ActiveVersion",
        ).get("Item")
        if pointer and pointer.get("ActiveVersion"):
            return str(pointer["ActiveVersion"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Active-profile pointer read failed (%s); scanning instead", exc)
    scan_kwargs: Dict[str, Any] = {
        "FilterExpression": "begins_with(Configuration, :p) AND IsActive = :t",
        "ExpressionAttributeValues": {":p": "Config#", ":t": True},
        "ProjectionExpression": "Configuration",
    }
    try:
        while True:
            resp = table.scan(**scan_kwargs)
            for item in resp.get("Items") or []:
                key = item["Configuration"]
                return key.split("#", 1)[1] if "#" in key else None
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_kwargs["ExclusiveStartKey"] = last_key
    except Exception as exc:  # noqa: BLE001
        logger.warning("Active-version scan failed: %s", exc)
        return _FALLBACK_VERSION
    # A full scan found no IsActive row. Distinct from the common no-op of
    # "the active version simply has no hooks for this point", so log it at
    # WARNING — this is the shape that hid #599 for over an hour.
    logger.warning(
        "No active Config# version found after a full scan of the "
        "ConfigurationTable; falling back to Config#default. Hooks registered "
        "in a feature's own config version will NOT run."
    )
    return _FALLBACK_VERSION


def _normalize_hook(h: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Validate + normalize one hook entry. Returns None if disabled or arn-less.
    Generic `args` is an optional list of {key, value} string pairs the hook
    reads its own settings from (keeps the platform hook-agnostic)."""
    if not isinstance(h, dict) or h.get("enabled") is False:
        return None
    arn = h.get("arn")
    if not arn:
        return None
    raw_args = h.get("args")
    args = (
        [a for a in raw_args if isinstance(a, dict) and "key" in a]
        if isinstance(raw_args, list)
        else []
    )
    return {
        "featureId": h.get("featureId") or "unknown",
        "arn": arn,
        "order": int(h.get("order", 100)) if h.get("order") is not None else 100,
        "onError": h.get("onError") or "continue",
        "args": args,
        # Admin kill-switch for document mutation on a per-hook basis. Defaults
        # to True (a registered hook is already admin-approved and IAM-gated,
        # and can already rewrite the S3 objects the document points at), but an
        # admin can pin a specific hook to observe-only.
        "allowDocumentUpdate": h.get("allowDocumentUpdate") is not False,
    }


def _read_hooks_from_config(
    table: Any, version: str, point: str
) -> List[Dict[str, Any]]:
    """Read the hooks for a point from Config#<version>, returning enabled,
    normalized entries.

    Two shapes:
      - flat points (`preprocessing`, `postprocessing`): a SINGLE inline hook —
        arn/args/onError/enabled live directly on the section (not a list).
      - post-step points: a `<section>.postHook` LIST.
    """
    step = _HOOK_TO_STEP.get(point)
    if not step:
        logger.warning("Unknown hook point %s", point)
        return []
    try:
        resp = table.get_item(Key={"Configuration": f"Config#{version}"})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Config row read failed for version=%s: %s", version, exc)
        return []
    item = resp.get("Item") or {}
    if not item:
        return []
    payload = _decompress_item(item)
    step_block = payload.get(step) or {}
    if not isinstance(step_block, dict):
        return []

    # Flat points: the section IS the single hook (flattened arn/args/...).
    if _is_flat_hook_point(point):
        h = _normalize_hook(step_block)
        return [h] if h else []

    # Post-step points: a list under <section>.postHook.
    raw = step_block.get("postHook") or []
    if not isinstance(raw, list):
        return []
    valid = [n for n in (_normalize_hook(h) for h in raw) if n]
    valid.sort(key=lambda h: (h["order"], h["featureId"]))
    return valid


def _set_preprocessing_status(document: Any) -> None:
    """Best-effort: flip the doc row's ObjectStatus to PREPROCESSING while a
    preprocessing hook runs, so the UI shows a real step name instead of the
    generic RUNNING (mirrors how OCR/CLASSIFYING/... set per-step statuses).

    Minimal, idp_common-free write (this function deliberately has no layer
    deps). Never fatal — a preprocessing hook must not fail because a status
    cosmetic couldn't be written. The next step (OCR etc.) or the workflow
    tracker overwrites the status, so no reset is needed here."""
    if not _TRACKING_TABLE or not isinstance(document, dict):
        return
    doc_id = document.get("document_id") or document.get("input_key")
    if not doc_id:
        return
    try:
        _dynamodb.Table(_TRACKING_TABLE).update_item(
            Key={"PK": f"doc#{doc_id}", "SK": "none"},
            UpdateExpression="SET #s = :s",
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeNames={"#s": "ObjectStatus"},
            ExpressionAttributeValues={":s": "PREPROCESSING"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("Could not set PREPROCESSING status for %s: %s", doc_id, exc)


def _doc_identity(doc: Any) -> Dict[str, Any]:
    """The immutable identity fields present on a document payload.

    Works on both shapes the workflow passes around: a full document dict
    (`id`/`input_key`/…) and a compressed wrapper (`document_id`/`s3_uri`/…).
    Only fields actually present are returned, so a compressed wrapper (which
    carries `document_id` but not `input_key`) is compared on what it has.
    """
    if not isinstance(doc, dict):
        return {}
    return {f: doc[f] for f in _IMMUTABLE_DOC_FIELDS if f in doc}


def _identity_matches(previous: Any, candidate: Any) -> Optional[str]:
    """None if `candidate` keeps `previous`'s identity, else a reason string.

    A compressed wrapper's `document_id` and a full dict's `id` are the same
    logical value, so they are compared against each other as well — this
    catches a hook that decompresses, changes `id`, and returns an inline dict.
    """
    prev_id = _doc_identity(previous)
    cand_id = _doc_identity(candidate)
    prev_logical = prev_id.get("id") or prev_id.get("document_id")
    cand_logical = cand_id.get("id") or cand_id.get("document_id")
    if prev_logical is not None and cand_logical is not None:
        if str(prev_logical) != str(cand_logical):
            return (
                f"document identity changed: {prev_logical!r} -> {cand_logical!r}"
            )
    for f in _IMMUTABLE_DOC_FIELDS:
        if f in prev_id and f in cand_id and prev_id[f] != cand_id[f]:
            return f"immutable field {f!r} changed: {prev_id[f]!r} -> {cand_id[f]!r}"
    return None


def _validate_compressed_ref(ref: Dict[str, Any]) -> Optional[str]:
    """None if `ref` is a usable compressed-document wrapper, else a reason.

    Two constraints, both load-bearing:

    1. `s3_uri` must name an object under `compressed_documents/` in THIS
       stack's working bucket. Downstream, Document.decompress() parses the URI
       but *discards its bucket*, reading the KEY against the consumer's own
       working bucket — so an unconstrained URI is a key-injection vector: a
       hook could point at any object in the working bucket (e.g. another
       document's `sections/N/result.json`) and have the next step deserialize
       it as this document. The identity check can't catch that, because it
       compares the wrapper's `document_id` field, not the content at the URI.

    2. `sections` must be a list of STRING section ids: the workflow's
       ProcessSections Map iterates that list directly (ItemsPath
       $.ClassificationResult.document.sections), so a malformed value would
       fail the whole execution rather than just the hook.
    """
    s3_uri = ref.get("s3_uri")
    if not isinstance(s3_uri, str) or not s3_uri.startswith("s3://"):
        return f"compressed document reference has invalid s3_uri: {s3_uri!r}"
    bucket, _, key = s3_uri[len("s3://") :].partition("/")
    if not key:
        return f"compressed document reference s3_uri has no key: {s3_uri!r}"
    # Only enforceable when the dispatcher knows its own working bucket. It
    # always does in a deployed stack (the template sets WORKING_BUCKET); the
    # guard keeps the check from rejecting everything if it is ever unset.
    if _WORKING_BUCKET and bucket != _WORKING_BUCKET:
        return (
            f"compressed document reference points at bucket {bucket!r}, not "
            f"the working bucket {_WORKING_BUCKET!r}"
        )
    if not key.startswith(_COMPRESSED_DOC_PREFIX):
        return (
            f"compressed document reference key {key!r} is outside the "
            f"{_COMPRESSED_DOC_PREFIX!r} prefix"
        )
    sections = ref.get("sections")
    if sections is not None:
        if not isinstance(sections, list) or not all(
            isinstance(s, str) for s in sections
        ):
            return (
                "compressed document reference `sections` must be a list of "
                f"section-id strings, got {type(sections).__name__}"
            )
    return None


def _compress_inline_document(
    doc: Dict[str, Any], point: str, feature_id: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Spill a hook-returned INLINE document dict to the working bucket.

    Returns (compressed_wrapper, None) on success or (None, reason) on failure.
    Mirrors idp_common.models.Document.compress() so the wrapper is
    indistinguishable from one a step Lambda produced — the dispatcher stays
    deliberately free of the idp_common layer (see module docstring), so the
    ~15 lines are inlined rather than imported.
    """
    if not _WORKING_BUCKET:
        return None, "WORKING_BUCKET is not configured; cannot store inline document"
    doc_id = doc.get("id") or doc.get("input_key")
    if not doc_id:
        return None, "inline document has no id/input_key"
    try:
        body = json.dumps(doc, default=str).encode("utf-8")
    except Exception as exc:  # noqa: BLE001
        return None, f"inline document is not JSON-serializable: {exc}"
    if len(body) > _MAX_INLINE_DOC_BYTES:
        return None, (
            f"inline document is {len(body)} bytes, over the "
            f"{_MAX_INLINE_DOC_BYTES}-byte limit; return a compressed reference "
            "instead"
        )

    timestamp = str(int(time.time() * 1000))
    key = (
        f"{_COMPRESSED_DOC_PREFIX}{doc_id}/{timestamp}_hook_{point}_"
        f"{feature_id}_state.json"
    )
    try:
        _s3.put_object(
            Bucket=_WORKING_BUCKET,
            Key=key,
            Body=body,
            ContentType="application/json",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to store hook document for %s", doc_id)
        return None, f"could not store inline document in working bucket: {exc}"

    # Section ids only — the Map state iterates this list.
    sections = [
        s.get("section_id")
        for s in (doc.get("sections") or [])
        if isinstance(s, dict) and s.get("section_id") is not None
    ]
    status = doc.get("status")
    return {
        "document_id": doc_id,
        "s3_uri": f"s3://{_WORKING_BUCKET}/{key}",
        "timestamp": timestamp,
        "status": status if isinstance(status, str) else str(status or ""),
        "num_pages": doc.get("num_pages", 0),
        "sections": [str(s) for s in sections],
        "config_version": doc.get("config_version"),
        "compressed": True,
    }, None


def _resolve_updated_document(
    result: Dict[str, Any],
    previous: Any,
    hook: Dict[str, Any],
    point: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Extract + validate a hook's `updatedDocument`, if it returned one.

    Returns (document_to_use, None) when the hook handed back a valid document,
    (None, None) when it returned no document at all (the read-only contract —
    by far the common case), or (None, reason) when it returned one the
    dispatcher refuses. On refusal the CALLER keeps the previous document, so a
    malformed hook response degrades to the historical passive behavior instead
    of corrupting the pipeline.
    """
    payload = result.get("result")
    if not isinstance(payload, dict) or _UPDATED_DOC_KEY not in payload:
        return None, None
    if not hook.get("allowDocumentUpdate", True):
        return None, (
            "hook returned updatedDocument but allowDocumentUpdate=false for "
            f"{hook['featureId']}"
        )

    candidate = payload.get(_UPDATED_DOC_KEY)
    if not isinstance(candidate, dict) or not candidate:
        return None, (
            f"updatedDocument must be a non-empty object, got "
            f"{type(candidate).__name__}"
        )

    reason = _identity_matches(previous, candidate)
    if reason:
        return None, reason

    prev_version = (
        previous.get("config_version") if isinstance(previous, dict) else None
    )
    if candidate.get("compressed") is True:
        reason = _validate_compressed_ref(candidate)
        if reason:
            return None, reason
        resolved = copy.deepcopy(candidate)
    else:
        resolved, reason = _compress_inline_document(
            candidate, point, hook["featureId"]
        )
        if reason or resolved is None:
            return None, reason or "could not process inline document"

    # config_version drives hook resolution for the REST of the pipeline (this
    # dispatcher reads it on every invoke), so a hook must not silently drop or
    # repoint it. Restore the inbound value rather than rejecting the whole
    # update — the hook's real intent is the document content.
    if prev_version is not None and resolved.get("config_version") != prev_version:
        logger.warning(
            "Hook %s changed config_version (%r -> %r); restoring the inbound "
            "value",
            hook["featureId"],
            prev_version,
            resolved.get("config_version"),
        )
        resolved["config_version"] = prev_version

    # Carry forward the state machine's routing control fields, which are not
    # Document model fields and so are dropped by any hook that round-trips the
    # document through idp_common. Without this, losing `use_bda` fails the
    # execution at RouteByProcessingMode.
    #
    # Same treatment for the wrapper fields the state machine reads via JSONPath
    # (num_pages / status / sections): the dispatcher's inline path and
    # Document.compress() both emit them, but a hand-rolled compressed reference
    # need not, and an absent one fails the execution with States.Runtime rather
    # than degrading. Back-fill from the inbound document instead of rejecting —
    # the hook's intent is the content change, not the wrapper metadata.
    if isinstance(previous, dict):
        for field in _CONTROL_FIELDS + _REQUIRED_WRAPPER_FIELDS:
            if field in previous and field not in resolved:
                logger.info(
                    "Back-filling %r on the document returned by hook %s",
                    field,
                    hook["featureId"],
                )
                resolved[field] = previous[field]
    return resolved, None


def _invoke_hook(hook: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        resp = _lambda.invoke(
            FunctionName=hook["arn"],
            InvocationType="RequestResponse",
            Payload=json.dumps(payload, default=str).encode("utf-8"),
        )
        body = resp.get("Payload")
        parsed: Any = None
        if body is not None:
            try:
                parsed = json.loads(body.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                parsed = None
        if resp.get("FunctionError"):
            return {
                "featureId": hook["featureId"],
                "arn": hook["arn"],
                "ok": False,
                "error": parsed or "Unknown FunctionError",
            }
        return {
            "featureId": hook["featureId"],
            "arn": hook["arn"],
            "ok": True,
            "result": parsed,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Hook invocation failed: %s", hook["arn"])
        return {
            "featureId": hook["featureId"],
            "arn": hook["arn"],
            "ok": False,
            "error": str(exc),
        }


def _noop(point: Any, document: Any, version: Optional[str] = None) -> Dict[str, Any]:
    """An empty dispatch result.

    Carries halt=False AND the inbound document unchanged, so both state-machine
    reads — $.HookResults.<point>.Payload.halt (the Choice) and
    ...Payload.document (the Apply Pass state) — resolve unconditionally, even
    when no hook is registered or the point is unknown.

    `version` is echoed as `configVersion` when a version WAS resolved, so the
    execution history distinguishes "the active version has no hooks here" from
    "we resolved the wrong version and found none" — `invoked: 0` alone cannot
    (issue #599). Omitted entirely when resolution never happened (unknown hook
    point, no configuration table), since there is no version to report.
    """
    out: Dict[str, Any] = {
        "hookPoint": point,
        "invoked": 0,
        "halt": False,
        "document": document,
        "results": [],
    }
    if version is not None:
        out["configVersion"] = version
    return out


def lambda_handler(event: Dict[str, Any], _ctx: Any) -> Dict[str, Any]:
    point = event.get("hookPoint")
    inbound_document = event.get("document")
    if point not in _HOOK_TO_STEP:
        logger.warning("Unknown hookPoint=%s — returning empty result", point)
        return _noop(point, inbound_document)
    if not _CONFIG_TABLE:
        logger.info("CONFIGURATION_TABLE_NAME not set — no hooks dispatched")
        return _noop(point, inbound_document)

    table = _dynamodb.Table(_CONFIG_TABLE)
    document = inbound_document or {}
    pinned = document.get("config_version") if isinstance(document, dict) else None
    version = _resolve_active_version(table, pinned)
    if not version:
        logger.info("No config version resolvable; returning no-hooks")
        return _noop(point, inbound_document)

    hooks = _read_hooks_from_config(table, version, point)
    if not hooks:
        logger.info("No hooks registered for %s in Config#%s", point, version)
        return _noop(point, inbound_document, version)

    # Surface the preprocessing step in the document's visible status (the
    # generic RUNNING otherwise persists for the whole — possibly long —
    # redaction pass). Only when a hook will actually run.
    if point == "preprocessing":
        _set_preprocessing_status(document)

    results: List[Dict[str, Any]] = []
    # The document threaded through the chain. Each hook sees the OUTPUT of the
    # previous hook at this point (not the original), so hooks compose; and this
    # is what the dispatcher hands back for the next workflow step.
    current_document: Any = inbound_document
    updated_by: List[str] = []
    for h in hooks:
        # Provide args both as the raw list and a flattened {key: value} map for
        # hook convenience. Values are strings; the hook parses as needed.
        args_list = h.get("args") or []
        args_map = {
            str(a["key"]): a.get("value")
            for a in args_list
            if isinstance(a, dict) and "key" in a
        }
        payload = {
            "hookPoint": point,
            "featureId": h["featureId"],
            "document": current_document,
            "section": event.get("section"),
            "executionArn": event.get("executionArn"),
            "args": args_list,
            "argsMap": args_map,
        }
        r = _invoke_hook(h, payload)
        results.append(r)

        if r["ok"]:
            updated, reason = _resolve_updated_document(
                r, current_document, h, str(point)
            )
            if updated is not None:
                logger.info(
                    "Hook %s returned an updated document for %s", h["featureId"], point
                )
                current_document = updated
                updated_by.append(h["featureId"])
                # Record WHAT the workflow will consume, not the (possibly
                # multi-MB) inline dict the hook returned, keeping the SFN
                # execution history readable.
                r["result"] = {
                    k: v for k, v in r["result"].items() if k != _UPDATED_DOC_KEY
                }
                r["documentUpdated"] = True
            elif reason:
                # Refused: keep the previous document (degrade to passive) and
                # surface why in the execution history.
                logger.warning(
                    "Rejected document update from hook %s at %s: %s",
                    h["featureId"],
                    point,
                    reason,
                )
                r["documentUpdateRejected"] = reason

        if not r["ok"] and h["onError"] == "fail":
            raise RuntimeError(
                f"Pipeline hook {h['featureId']} at {point} failed and onError=fail: {r.get('error')}"
            )
        if not r["ok"] and h["onError"] == "skip-remaining":
            logger.warning(
                "Hook %s reported failure with onError=skip-remaining; stopping",
                h["featureId"],
            )
            break

    # Aggregate a top-level `halt` flag so the state machine's post-hook
    # Choice can read a STABLE path ($.HookResults.<point>.Payload.halt)
    # without indexing into a possibly-empty results array (JSONPath can't
    # do that safely). Any successful hook returning result.halt == true
    # halts the workflow. Used by the preprocessing hook to short-circuit a
    # document whose only purpose was to spawn a redacted copy.
    #
    # Only honored where the workflow can actually act on it. At
    # `postprocessing` the document is finished and there is nothing left to
    # skip, so a `halt` there is reported under `haltIgnored` rather than
    # returned as a `halt` the state machine would not read anyway — the
    # difference between "asked and ignored" and "never asked" is worth having
    # in the execution history.
    halt_requested = any(
        r.get("ok")
        and isinstance(r.get("result"), dict)
        and r["result"].get("halt") is True
        for r in results
    )
    halt = halt_requested and point in _HALT_CAPABLE_POINTS
    if halt_requested and not halt:
        logger.warning(
            "Hook at %s requested halt, which is not supported at this point "
            "(the document is already finished); continuing",
            point,
        )
    out: Dict[str, Any] = {
        "hookPoint": point,
        "configVersion": version,
        "invoked": len(results),
        "halt": halt,
        # Always present. Equals the inbound document unless a hook returned a
        # validated `updatedDocument`. The state machine's Apply<Point>Hook-
        # Document Pass state copies this into the path the next step reads.
        "document": current_document,
        "documentUpdatedBy": updated_by,
        "results": results,
    }
    if halt_requested and not halt:
        out["haltIgnored"] = True
    return out
