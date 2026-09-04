# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import logging
import os
from datetime import datetime
from decimal import Decimal

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
sqs = boto3.client("sqs")

# --- inline log sanitizer ---------------------------------------------------
# Minimal inline redactor. Kept here rather than importing from idp_common to
# avoid adding a Lambda Layer dependency to this resolver. If this file grows
# to need idp_common anyway, promote to
# `from idp_common.utils.log_sanitizer import sanitize_event_for_logging`.
_LOG_SENSITIVE_KEYS = (
    "password",
    "secret",
    "token",
    "authorization",
    "apikey",
    "api_key",
    "cookie",
    "credential",
    "claims",
    "identity",
)


def _sanitize_for_log(obj):
    """Deep-copy `obj` redacting values whose keys match the denylist."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and any(s in k.lower() for s in _LOG_SENSITIVE_KEYS):
                out[k] = "***REDACTED***" if v is not None else None
            else:
                out[k] = _sanitize_for_log(v)
        return out
    if isinstance(obj, list):
        return [_sanitize_for_log(v) for v in obj]
    return obj


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


def handler(event, context):
    logger.info(
        f"Test runner invoked with event: {json.dumps(_sanitize_for_log(event))}"
    )

    try:
        # Defense-in-depth: startTestRun is an Admin+Author operation.
        # Allow direct Lambda invocations (no 'identity' field or identity=None) for CI/automation.
        # AppSync invocations always have 'identity' with non-None value, so RBAC is still enforced for UI users.
        # Security: Direct invocation path is gated by IAM (lambda:InvokeFunction permission on this ARN),
        # not Cognito groups. CI/automation uses IAM credentials; UI users go through AppSync + Cognito.
        is_appsync_invoke = event.get("identity") is not None
        if is_appsync_invoke and not _caller_in_groups(event, ("Admin", "Author")):
            raise Exception(
                "Unauthorized: this operation requires Admin or Author group"
            )

        # Route by GraphQL field. startTestRun is the default/legacy path.
        field_name = event.get("info", {}).get("fieldName", "startTestRun")
        if field_name == "sendTestRunToReview":
            return send_test_run_to_review(event["arguments"])

        input_data = event["arguments"]["input"]
        test_set_id = input_data["testSetId"]
        test_context = input_data.get("context", "")

        # Validate context length
        if test_context and len(test_context) > 500:
            raise Exception("Context cannot exceed 500 characters")

        number_of_files = input_data.get("numberOfFiles")
        # Names exactly which documents to process, where numberOfFiles takes the
        # first N of the set.
        object_keys = input_data.get("objectKeys") or []
        document_class = input_data.get("documentClass")
        config_version = input_data.get("configVersion")
        # Revision of that profile to score against. Pinning it is what makes two
        # runs of the same profile comparable — otherwise a run records only which
        # profile it used, and a later save silently changes what that meant.
        config_revision = input_data.get("configRevision")
        # A draft-labeling run produces ground truth rather than being scored
        # against it, so it is never evaluated. Carried as its own flag; the
        # free-text `context` is a user-facing label and must not be load-bearing.
        purpose = "draft-labeling" if input_data.get("draftLabeling") else "scoring"
        tracking_table = os.environ["TRACKING_TABLE"]
        config_table = os.environ["CONFIG_TABLE"]

        # Get test set
        test_set = _get_test_set(tracking_table, test_set_id)
        if not test_set:
            raise ValueError(f"Test set with ID '{test_set_id}' not found")

        # Determine actual file count to process
        test_set_file_count = test_set["fileCount"]
        files_to_process = test_set_file_count

        if object_keys:
            if len(object_keys) > test_set_file_count:
                raise ValueError(
                    f"objectKeys ({len(object_keys)}) cannot exceed test set file "
                    f"count ({test_set_file_count})"
                )
            files_to_process = len(object_keys)

        if number_of_files is not None:
            if number_of_files <= 0:
                raise ValueError("numberOfFiles must be greater than 0")
            if number_of_files > test_set_file_count:
                raise ValueError(
                    f"numberOfFiles ({number_of_files}) cannot exceed test set file count ({test_set_file_count})"
                )
            files_to_process = min(number_of_files, files_to_process)

        # Create test run identifier using test set name
        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        test_run_id = f"{test_set['name']}-{timestamp}"

        # Resolve first, then capture, so the version is recorded on the run whether
        # or not the caller named one. Passing the resolved name through also keeps
        # this to a single scan of the config table.
        effective_config_version = config_version or _active_config_version(
            config_table
        )
        # Resolve the profile's current revision when the caller did not name one,
        # for the same reason the version is resolved here: the run must record
        # which configuration it actually ran, not "whatever was current".
        effective_config_revision = config_revision
        if effective_config_revision is None and effective_config_version:
            effective_config_revision = _published_revision(
                config_table, effective_config_version
            )
        config = _capture_config(
            config_table, effective_config_version, effective_config_revision
        )

        # Which labels this run is scored against. Default: the set's CURRENT labels,
        # including any annotation in progress. Defaulting to the last published
        # version instead made the ordinary loop — correct twenty documents, run, see
        # the improvement — silently score the labels from before the corrections,
        # while the review-effort panel invited exactly that loop. Pinning is explicit,
        # as it is for the configuration revision above, and is what keeps two runs
        # comparable once the ground truth has moved. The copier stages a pinned
        # version's snapshot and the live baselines otherwise.
        test_set_version = input_data.get("testSetVersion")
        test_set_draft_version = None
        if test_set_version is not None:
            test_set_version = int(test_set_version)
            latest_version = int(test_set.get("latestVersion") or 0)
            if test_set_version < 1 or test_set_version > latest_version:
                raise ValueError(
                    f"testSetVersion {test_set_version} does not exist for test set "
                    f"'{test_set_id}' (latest is {latest_version})"
                )
        elif test_set.get("draftVersion") is not None:
            test_set_draft_version = int(test_set["draftVersion"])

        # Store initial test run metadata
        _store_test_run_metadata(
            tracking_table,
            test_run_id,
            test_set_id,
            test_set["name"],
            config,
            [],
            test_context,
            files_to_process,
            effective_config_version,
            test_set_version,
            purpose=purpose,
            config_revision=effective_config_revision,
            test_set_draft_version=test_set_draft_version,
        )

        # Send file copying job to SQS queue
        queue_url = os.environ["FILE_COPY_QUEUE_URL"]

        message_body = {
            "testRunId": test_run_id,
            "testSetId": test_set_id,
            "trackingTable": tracking_table,
            # Always pass the intended file count (default = test_set.fileCount,
            # override = user's numberOfFiles, or len(objectKeys) when the caller
            # named specific documents). The copier must cap the S3 listing to
            # this count so that Files (the actual copied list) stays aligned
            # with FilesCount (the metadata denominator) even when the
            # underlying S3 test-set folder has drifted past the test set's
            # declared fileCount — e.g. a user uploaded extra samples without
            # bumping fileCount. Without this cap the copier would ingest every
            # object under testset#<id>/input/, poll would report "N/K
            # completed" where N > K, and the run's "Files" list would include
            # documents that were never part of this test set.
            #
            # int() cast is load-bearing: test_set['fileCount'] is a
            # DynamoDB ``Decimal`` (DDB's only numeric type), which
            # ``json.dumps`` rejects with "Object of type Decimal is not JSON
            # serializable" when we serialize the message body.
            "filesToProcess": int(files_to_process),
        }

        # Only include numberOfFiles if it was specified
        if number_of_files is not None:
            message_body["numberOfFiles"] = number_of_files

        if object_keys:
            message_body["objectKeys"] = object_keys

        # Pin the resolved version, not just an explicitly-requested one: the copier
        # stamps this onto each object and the pipeline processes under it, so
        # leaving it unset let the active config change between submit and
        # processing — and the run's recorded ConfigVersion would then name a config
        # the documents were never processed with.
        if effective_config_version is not None:
            message_body["configVersion"] = effective_config_version
        # The copier stamps this onto each object, so the documents process under
        # the exact revision the run says it scored.
        if effective_config_revision is not None:
            message_body["configRevision"] = int(effective_config_revision)
        # The version this run scores against, so the copier stages that version's
        # baseline snapshot rather than the set's current labels. Same reasoning as
        # configVersion above: the run records what it scored against, so it has to read
        # what it recorded.
        if test_set_version is not None:
            message_body["testSetVersion"] = int(test_set_version)

        # The copier must not stage baselines for a draft-labeling run: the baseline
        # is what the run is creating, so scoring against it would compare the
        # extraction to a stale copy of itself.
        if purpose == "draft-labeling":
            message_body["purpose"] = purpose

        # A single-document re-extract after a class correction. Passed straight
        # through to the copier, which stamps it as S3 metadata so the
        # classification step uses it instead of classifying again — the whole
        # point of the request is that the model's own answer was wrong.
        if document_class:
            message_body["documentClass"] = document_class

        sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(message_body))

        logger.info(
            f"Queued test run {test_run_id} for test set {test_set_id} with {files_to_process} files"
        )

        # Return immediately
        return {
            "testRunId": test_run_id,
            "testSetName": test_set["name"],
            "status": "QUEUED",
            "filesCount": files_to_process,
            "completedFiles": 0,
            "createdAt": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }

    except Exception as e:
        logger.error(f"Error in test runner: {str(e)}")
        raise


def send_test_run_to_review(args):
    """Mark a completed test run's documents for HITL review, on demand.

    A test run's inputs are copied into the pipeline under a ``{test_run_id}/``
    prefix, so each becomes a first-class ``doc#`` item with confidence alerts.
    This flips those docs into the review hopper (HITLTriggered /
    HITLStatus=PendingReview) without waiting for the confidence-only
    auto-trigger. Only docs that have confidence alerts are queued.
    """
    test_run_id = args["testRunId"]
    tracking_table = os.environ["TRACKING_TABLE"]
    table = dynamodb.Table(tracking_table)  # type: ignore[attr-defined]

    run = table.get_item(Key={"PK": f"testrun#{test_run_id}", "SK": "metadata"}).get(
        "Item"
    )
    if not run:
        raise ValueError(f"Test run '{test_run_id}' not found")

    files = run.get("Files") or []
    queued = 0
    skipped = 0
    for file_name in files:
        object_key = f"{test_run_id}/{file_name}"
        doc = table.get_item(Key={"PK": f"doc#{object_key}", "SK": "none"}).get("Item")
        if not doc:
            skipped += 1
            continue

        # Never reopen a review that is already completed or skipped.
        alert_count = int(doc.get("ConfidenceAlertCount", 0) or 0)
        status = doc.get("HITLStatus", "")
        if alert_count <= 0 or status in (
            "Review Completed",
            "Review Skipped",
            "Completed",
            "Skipped",
        ):
            skipped += 1
            continue

        table.update_item(
            Key={"PK": f"doc#{object_key}", "SK": "none"},
            UpdateExpression=(
                "SET HITLTriggered = :t, HITLStatus = :s, TestSetId = :tsid"
            ),
            ExpressionAttributeValues={
                ":t": True,
                ":s": "PendingReview",
                ":tsid": run.get("TestSetId"),
            },
        )
        queued += 1

    logger.info(
        f"send_test_run_to_review: run={test_run_id} queued={queued} skipped={skipped}"
    )
    return {
        "testRunId": test_run_id,
        "testSetId": run.get("TestSetId"),
        "queuedCount": queued,
        "skippedCount": skipped,
    }


def _get_test_set(tracking_table, test_set_id):
    """Get test set by ID"""
    table = dynamodb.Table(tracking_table)  # type: ignore[attr-defined]

    try:
        response = table.get_item(
            Key={"PK": f"testset#{test_set_id}", "SK": "metadata"}
        )
        return response.get("Item")
    except Exception as e:
        logger.error(f"Error getting test set {test_set_id}: {e}")
        return None


def _decompress_config_item(item):
    """
    Decompress a DynamoDB config item if it uses compressed storage format.
    Inlined here to avoid dependency on idp_common (not available in this Lambda).
    """
    import gzip as _gzip

    if item.get("_config_storage") != "compressed":
        return item  # Legacy inline format — return as-is

    compressed_data = item.get("_compressed_config")
    if compressed_data is None:
        return item

    raw_bytes = (
        bytes(compressed_data)
        if not isinstance(compressed_data, bytes)
        else compressed_data
    )

    try:
        config_data = json.loads(_gzip.decompress(raw_bytes).decode("utf-8"))
    except Exception as e:
        logger.error(f"Failed to decompress config data: {e}")
        return item

    # Reconstruct: metadata fields + decompressed config data
    metadata_fields = {
        "Configuration",
        "CreatedAt",
        "UpdatedAt",
        "IsActive",
        "Description",
    }
    full_item = {k: v for k, v in item.items() if k in metadata_fields}
    full_item.update(config_data)
    return full_item


# Ceiling on pages walked looking for the active config row. Each page examines
# ~1MB of items and projects a single attribute, so this is far more versions than
# a deployment holds.
_MAX_CONFIG_SCAN_PAGES = 50


def _resolve_active_config_key(table):
    """Return the key ('Config#<version>') of the IsActive=true row, or None.

    Paginates, and projects only the key. DynamoDB applies the 1MB page size to
    the items EXAMINED, not the items matching FilterExpression, so an
    unpaginated scan finds the active row only when it lands in the first page —
    and an unprojected one reads whole config bodies, fitting only a handful of
    versions per page. Missing the active row here captures the WRONG
    configuration into the test run's metadata, so its comparisons are scored
    against a config the documents were not processed under. See #599.
    """
    scan_kwargs = {
        "FilterExpression": (
            "begins_with(Configuration, :config_prefix) AND IsActive = :active"
        ),
        "ExpressionAttributeValues": {":config_prefix": "Config#", ":active": True},
        "ProjectionExpression": "Configuration",
    }
    # Bounded: an unbounded paging loop spins forever if the table ever returns a
    # repeating LastEvaluatedKey, and it hangs rather than fails under a mocked
    # table, which is far harder to diagnose than a wrong answer.
    for _ in range(_MAX_CONFIG_SCAN_PAGES):
        response = table.scan(**scan_kwargs)
        for item in response.get("Items", []):
            return item["Configuration"]
        last_key = response.get("LastEvaluatedKey")
        if not last_key or last_key == scan_kwargs.get("ExclusiveStartKey"):
            return None
        scan_kwargs["ExclusiveStartKey"] = last_key
    logger.warning(
        "No active config version found within %d scan pages; giving up",
        _MAX_CONFIG_SCAN_PAGES,
    )
    return None


def _active_config_version(config_table):
    """Name of the active config version, or None.

    Resolved before capture so the run's ``ConfigVersion`` is recorded even when the
    caller did not name a version. Without it every run started against "Active
    configuration" — the UI default — stored no version at all, which silently
    disables anything comparing a run's config to another's: notably the guard that
    refuses to score a config against the labels that same config drafted.
    """
    key = _resolve_active_config_key(dynamodb.Table(config_table))  # type: ignore[attr-defined]
    if not key or not key.startswith("Config#"):
        return None
    return key[len("Config#") :] or None


def _published_revision(config_table, config_version):
    """The profile's current revision, or None when it has no history.

    None is normal: an older deployment, or a profile untouched since the upgrade
    that introduced revisions. The run then records no revision and the documents
    process under the profile head, exactly as before.
    """
    try:
        table = dynamodb.Table(config_table)  # type: ignore[attr-defined]
        item = (
            table.get_item(
                Key={"Configuration": f"Config#{config_version}"},
                ProjectionExpression="PublishedRevision",
            ).get("Item")
            or {}
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not resolve the published revision: {e}")
        return None
    published = item.get("PublishedRevision")
    if published is None:
        return None
    try:
        return int(published)
    except (TypeError, ValueError):
        logger.warning(f"Ignoring unusable PublishedRevision {published!r}")
        return None


def _pin_revision(config_table, config_version, revision):
    """Mark a revision as pinned so retention cannot prune it.

    A comparison between two runs is only interpretable while both runs'
    configurations still exist, and the default retention window is 20 revisions.
    Best-effort: failing to mark it must not fail the run.
    """
    try:
        from idp_common.config.configuration_manager import ConfigurationManager

        ConfigurationManager(table_name=config_table).mark_revision_pinned(
            config_version, revision
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"Could not pin r{revision} of '{config_version}' against retention: {e}"
        )


def _capture_config(config_table, config_version=None, config_revision=None):
    """Capture configuration - specific revision, specific profile, or active"""
    table = dynamodb.Table(config_table)  # type: ignore[attr-defined]

    config = {}

    # A pinned revision is captured from its stored body, so the run records the
    # configuration it actually scored rather than the profile's current state.
    if config_version and config_revision is not None:
        try:
            from idp_common.config.configuration_manager import ConfigurationManager

            body = ConfigurationManager(table_name=config_table).get_revision(
                config_version, config_revision
            )
            if body is not None:
                # A revision body is JSON, so it carries Python floats (e.g.
                # temperature: 0.0). The captured config is written straight into
                # the run's DynamoDB item, and the DynamoDB resource client
                # rejects floats outright — "Float types are not supported. Use
                # Decimal types instead." — which failed every startTestRun that
                # pinned a revision. The config read from DynamoDB never hit this
                # because it comes back as Decimal already.
                config["Config"] = json.loads(json.dumps(body), parse_float=Decimal)
                _pin_revision(config_table, config_version, config_revision)
                return config
            logger.warning(
                f"Revision r{config_revision} of '{config_version}' is not retained; "
                f"capturing the profile's current configuration instead"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not capture revision r{config_revision}: {e}")

    # Get Config (versioned) - this is what's used for comparisons
    try:
        if config_version:
            key = f"Config#{config_version}"
        else:
            # Get active config version - scan for IsActive=True. The scan only
            # locates the key; the body is read with GetItem below, so the
            # projected scan stays cheap however large the configs are.
            key = _resolve_active_config_key(table)
            if not key:
                logger.warning("No active config version found after a full scan")
                return config
        response = table.get_item(Key={"Configuration": key})
        if "Item" in response:
            config["Config"] = _decompress_config_item(response["Item"])
        else:
            logger.warning(f"Config {key} not found")

    except Exception as e:
        logger.warning(f"Could not retrieve Config: {e}")

    return config


def _store_test_run_metadata(
    tracking_table,
    test_run_id,
    test_set_id,
    test_set_name,
    config,
    files,
    context=None,
    file_count=0,
    config_version=None,
    test_set_version=None,
    purpose="scoring",
    config_revision=None,
    test_set_draft_version=None,
):
    """Store test run metadata in tracking table"""
    table = dynamodb.Table(tracking_table)  # type: ignore[attr-defined]

    try:
        created_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        item = {
            "PK": f"testrun#{test_run_id}",
            "SK": "metadata",
            "ItemType": "testrun",
            "InitialEventTime": created_at,
            "TestSetId": test_set_id,
            "TestSetName": test_set_name,
            "TestRunId": test_run_id,
            "Status": "QUEUED",
            "FilesCount": file_count,
            "CompletedFiles": 0,
            "FailedFiles": 0,
            "Files": files,
            "Config": config,
            "CreatedAt": created_at,
        }

        # Persisted so downstream resolvers can tell a scoring run from one that
        # CREATES the baseline. Without it the only marker was the free-text
        # Context string ("Draft labeling run"), which a user can type themselves
        # and which nothing guarantees.
        #
        # A PARAMETER, not a read of the caller's local. It was written as the
        # latter, which is a NameError on every call — and since the except below
        # re-raises, that failed every test run start outright.
        item["Purpose"] = purpose

        if context:
            item["Context"] = context

        if config_version:
            item["ConfigVersion"] = config_version

        if test_set_version is not None:
            item["TestSetVersion"] = test_set_version
        # Recorded when the run scored current labels while a transition was open,
        # so the result can name the version those labels were heading toward.
        if test_set_draft_version is not None:
            item["TestSetDraftVersion"] = int(test_set_draft_version)

        # Recorded alongside TestSetVersion: together they make a metric delta
        # between two runs attributable to the configuration or the ground truth
        # rather than ambiguous.
        if config_revision is not None:
            item["ConfigRevision"] = int(config_revision)

        table.put_item(Item=item)
        logger.info(f"Stored test run metadata for {test_run_id}")
    except Exception as e:
        logger.error(f"Failed to store test run metadata: {e}")
        raise
