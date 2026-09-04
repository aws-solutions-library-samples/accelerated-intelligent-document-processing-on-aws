# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import logging
import os
import concurrent.futures

import boto3
from botocore.exceptions import ClientError

# Type: ignore for boto3 resource type inference
dynamodb = boto3.resource("dynamodb")  # type: ignore

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")


def handler(event, context):
    """Process file copy jobs from SQS"""
    logger.info(f"File copier invoked with {len(event['Records'])} messages")

    for record in event["Records"]:
        try:
            message = json.loads(record["body"])

            test_run_id = message["testRunId"]
            test_set_id = message["testSetId"]
            number_of_files = message.get("numberOfFiles")  # Optional parameter
            # filesToProcess is the runner's authoritative cap = min(user
            # numberOfFiles, test_set.fileCount). Always honored when present
            # (checked with `is None`, not truthiness, so the cap is respected
            # literally rather than reinterpreting a 0 as "unset"); falls back
            # to numberOfFiles for messages enqueued before the runner started
            # sending it.
            files_to_process = message.get("filesToProcess")
            if files_to_process is None:
                files_to_process = number_of_files
            config_version = message.get("configVersion")  # Optional parameter
            # Revision of that profile the run pinned, stamped onto every copied
            # object so the documents process under exactly what the run recorded.
            config_revision = message.get("configRevision")
            # Which test-set version this run scores against, so it can read that
            # version's baseline snapshot rather than whatever the labels are now.
            test_set_version = message.get("testSetVersion")
            object_keys = message.get("objectKeys") or []
            # "draft-labeling" runs create ground truth; anything else is scored
            # against it. Absent on messages enqueued before this field existed.
            purpose = message.get("purpose") or "scoring"
            # Set only by a single-document re-extract after a reviewer corrected
            # the class; stamped onto the copied object so classification honours
            # it instead of re-deriving the class it already got wrong.
            forced_document_class = message.get("documentClass")
            tracking_table = message["trackingTable"]

            # Get environment variables
            test_set_bucket = os.environ["TEST_SET_BUCKET"]
            input_bucket = os.environ["INPUT_BUCKET"]
            baseline_bucket = os.environ["BASELINE_BUCKET"]

            logger.info(f"Processing test run {test_run_id} for test set {test_set_id}")

            # List files from test set bucket
            input_files = _list_test_set_files(test_set_bucket, test_set_id, "input")
            baseline_folder = _resolve_baseline_folder(
                test_set_bucket, test_set_id, test_set_version
            )
            baseline_files = _list_test_set_files(
                test_set_bucket, test_set_id, baseline_folder
            )

            if not input_files:
                raise ValueError(f"No input files found for test set: {test_set_id}")

            # objectKeys names exact documents, where numberOfFiles takes the first
            # N of the set.
            if object_keys:
                requested = set(object_keys)
                missing = requested - set(input_files)
                if missing:
                    raise ValueError(
                        f"Requested documents not in test set {test_set_id}: "
                        f"{', '.join(sorted(missing)[:5])}"
                        f"{'...' if len(missing) > 5 else ''}"
                    )
                input_files = [f for f in input_files if f in requested]
                logger.info(
                    f"Restricted to {len(input_files)} explicitly requested document(s)"
                )

            # Cap input_files at the runner's intended count. Sort first so the
            # same N files are picked deterministically across repeat runs of
            # the same test set — otherwise S3 list ordering (already
            # lexicographic today, but not contractually stable) could swap
            # which subset of an over-populated folder actually gets processed
            # from run to run and mask baseline drift. Log the drift so an
            # operator whose S3 folder outgrew the test set's fileCount notices
            # instead of silently getting a subset.
            #
            # Sort order note: Python's default ``.sort()`` compares strings by
            # Unicode code point. S3's ``list_objects_v2`` also returns keys in
            # UTF-8 binary sort order, so for ASCII filenames (every test set
            # in this repo today) the pre-fix legacy ``numberOfFiles`` path and
            # the post-fix sorted path select the same subset. For test sets
            # whose filenames contain non-ASCII characters, Python's default
            # sort and S3's binary sort diverge above U+007F — future test
            # sets should either stick to ASCII filenames or move to
            # ``key=lambda s: s.encode('utf-8')`` here to match S3 exactly.
            input_files.sort()
            baseline_files.sort()
            capped = False
            if files_to_process is not None and len(input_files) > files_to_process:
                logger.warning(
                    f"S3 test-set folder for {test_set_id} contains "
                    f"{len(input_files)} input files but this run was scoped to "
                    f"{files_to_process}; processing the first {files_to_process} "
                    f"lexicographically. Update the test set's fileCount if the "
                    f"extra files should be included."
                )
                input_files = input_files[:files_to_process]
                capped = True

            # Keep baselines aligned with the surviving input selection.
            if object_keys or capped:
                input_file_set = set(input_file + "/" for input_file in input_files)
                baseline_files = [
                    bf
                    for bf in baseline_files
                    if any(bf.startswith(prefix) for prefix in input_file_set)
                ]
                logger.info(
                    f"Selected {len(input_files)} input files with "
                    f"{len(baseline_files)} corresponding baseline files"
                )

            logger.info(
                f"Processing {len(input_files)} input files and {len(baseline_files)} baseline files"
            )

            # Update test run with file list and set status to IN_PROGRESS
            _update_tracking_in_progress(tracking_table, test_run_id, input_files)

            # Copy input files from test set bucket to input bucket with test_run_id prefix
            successful_input_files = _copy_files_to_bucket(
                test_set_bucket,
                f"{test_set_id}/input/",
                input_bucket,
                f"{test_run_id}/",
                input_files,
                config_version,
                submission_source="test-studio",
                test_set_id=test_set_id,
                forced_document_class=forced_document_class,
                config_revision=config_revision,
            )

            # Copy baseline files from test set bucket to baseline bucket with
            # test_run_id prefix. Skipped for a draft-labeling run: that run
            # produces the baseline, so staging the existing one makes evaluation
            # score the new extraction against a stale copy of itself, and the
            # resulting accuracy would also feed the confidence-calibration curve
            # recorded for every run carrying a TestSetId.
            if purpose == "draft-labeling":
                logger.info(
                    f"Draft-labeling run {test_run_id}: not staging baselines "
                    f"({len(baseline_files)} present in the test set). The run "
                    f"produces ground truth, so there is nothing to score against."
                )
                baseline_files = []
                successful_baseline_files = []
            else:
                successful_baseline_files = _copy_files_to_bucket(
                    test_set_bucket,
                    f"{test_set_id}/{baseline_folder}/",
                    baseline_bucket,
                    f"{test_run_id}/",
                    baseline_files,
                )

            # Check if all files failed to copy
            if len(successful_input_files) == 0:
                raise ValueError("All input files failed to copy")

            # A set with no baselines is unlabeled, not broken; only a set whose
            # baselines all failed to copy is a failure.
            if baseline_files and len(successful_baseline_files) == 0:
                raise ValueError("All baseline files failed to copy")

            # Update failed files count
            input_failed_count = len(input_files) - len(successful_input_files)

            if input_failed_count > 0:
                _update_test_run_status(
                    tracking_table, test_run_id, None, failed_count=input_failed_count
                )

            logger.info(f"Completed file copying for test run {test_run_id}")

        except Exception as e:
            logger.error(f"Error processing test run {test_run_id}: {str(e)}")
            _update_test_run_status(tracking_table, test_run_id, "FAILED", str(e))

    return {"statusCode": 200}


def _resolve_baseline_folder(test_set_bucket, test_set_id, test_set_version):
    """Which baseline folder this run should score against.

    A run records the test-set version it was measured against, but a version used to be
    a DynamoDB row that copied nothing: annotation wrote straight to
    ``{id}/baseline/``, so the labels a run had scored could change afterwards and the
    recorded version number meant nothing you could go back to.

    Annotation sessions now snapshot the state they move away from to
    ``{id}/versions/{n}/baseline/``. A run pinned to version *n* reads that snapshot, so
    it scores against the labels the version actually names.

    Falls back to the live folder whenever there is no snapshot — an unpinned run, a
    version published before snapshots existed, or a set nobody has annotated. That
    fallback is what keeps every existing set and every historical run behaving exactly
    as before.
    """
    if not test_set_version:
        return "baseline"

    versioned = f"{test_set_id}/versions/{int(test_set_version)}/baseline/"
    listing = s3.list_objects_v2(Bucket=test_set_bucket, Prefix=versioned, MaxKeys=1)
    if listing.get("KeyCount"):
        logger.info(
            f"Test set {test_set_id} run pinned to version {test_set_version}; "
            f"scoring against its baseline snapshot"
        )
        return f"versions/{int(test_set_version)}/baseline"

    logger.info(
        f"Test set {test_set_id} version {test_set_version} has no baseline snapshot; "
        f"using the current baselines"
    )
    return "baseline"


def _list_test_set_files(test_set_bucket, test_set_id, folder_type):
    """List files from test set bucket folder (input or baseline)"""
    try:
        prefix = f"{test_set_id}/{folder_type}/"
        files = []

        # Use paginator to handle more than 1000 files
        paginator = s3.get_paginator("list_objects_v2")
        page_iterator = paginator.paginate(Bucket=test_set_bucket, Prefix=prefix)

        for page in page_iterator:
            if "Contents" in page:
                for obj in page["Contents"]:
                    # Skip folder itself, only get actual files
                    if not obj["Key"].endswith("/"):
                        # Preserve full relative path after the folder_type prefix
                        relative_path = obj["Key"][len(prefix) :]
                        files.append(relative_path)

        logger.info(
            f"Found {len(files)} {folder_type} files for test set {test_set_id}"
        )
        return files

    except Exception as e:
        logger.error(
            f"Error listing {folder_type} files for test set {test_set_id}: {e}"
        )
        return []


def _copy_files_to_bucket(
    source_bucket,
    source_prefix,
    dest_bucket,
    dest_prefix,
    files,
    config_version=None,
    submission_source=None,
    test_set_id=None,
    forced_document_class=None,
    config_revision=None,
):
    """Copy files from source bucket to destination bucket - track failures"""
    successful_files = []

    # An unlabeled test set has no baseline files, and ThreadPoolExecutor rejects
    # max_workers=0, so an empty list must short-circuit.
    if not files:
        return successful_files

    def copy_single_file(filename):
        """Copy a single file and return result"""
        try:
            source_key = f"{source_prefix}{filename}"
            dest_key = f"{dest_prefix}{filename}"

            # Copy file with config version as S3 metadata instead of filename suffix
            copy_args = {
                "CopySource": {"Bucket": source_bucket, "Key": source_key},
                "Bucket": dest_bucket,
                "Key": dest_key,
            }

            # Object metadata carries provenance into the pipeline: the document
            # enters via the ordinary S3 -> EventBridge -> queue-sender path, and
            # Document.from_s3_event already HEADs the object.
            metadata = {}
            if config_version:
                metadata["config-version"] = config_version
                if config_revision is not None:
                    metadata["config-revision"] = str(config_revision)
            if submission_source:
                metadata["submission-source"] = submission_source
            if test_set_id:
                metadata["test-set-id"] = test_set_id
            if forced_document_class:
                # Read by Document.from_s3_event and honoured by the
                # classification step, which skips the model and uses this. Set
                # only for a single-document re-extract after a reviewer
                # corrected the class.
                metadata["document-class"] = forced_document_class
            if metadata:
                copy_args["Metadata"] = metadata
                copy_args["MetadataDirective"] = "REPLACE"

            s3.copy_object(**copy_args)

            logger.info(f"Copied file: {source_key} -> {dest_bucket}/{dest_key}")
            return filename, None

        except Exception as e:
            logger.error(f"Failed to copy file {filename}: {e}")
            return filename, str(e)

    # Use ThreadPoolExecutor for parallel copying
    max_workers = min(10, len(files))  # Limit to 10 threads or number of files
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all copy tasks
        future_to_file = {
            executor.submit(copy_single_file, filename): filename for filename in files
        }

        # Collect results
        for future in concurrent.futures.as_completed(future_to_file):
            filename, error = future.result()
            if error is None:
                successful_files.append(filename)

    return successful_files


def _update_tracking_in_progress(tracking_table, test_run_id, files):
    """Update test run with file list and set status to RUNNING"""
    table = dynamodb.Table(tracking_table)  # type: ignore
    try:
        table.update_item(
            Key={"PK": f"testrun#{test_run_id}", "SK": "metadata"},
            UpdateExpression="SET Files = :files, #status = :status",
            ExpressionAttributeNames={"#status": "Status"},
            ExpressionAttributeValues={":files": files, ":status": "RUNNING"},
        )
        logger.info(
            f"Updated test run {test_run_id} to RUNNING with {len(files)} files"
        )
    except Exception as e:
        logger.error(f"Failed to update tracking for {test_run_id}: {e}")


def _update_test_run_status(
    tracking_table, test_run_id, status, error=None, failed_count=None
):
    """Update test run status in tracking table"""
    try:
        table = dynamodb.Table(tracking_table)  # type: ignore
        update_expression_parts = []
        expression_attribute_names = {}
        expression_attribute_values = {}

        if status:
            update_expression_parts.append("#status = :status")
            expression_attribute_names["#status"] = "Status"
            expression_attribute_values[":status"] = status

        if error:
            update_expression_parts.append("#error = :error")
            expression_attribute_names["#error"] = "Error"
            expression_attribute_values[":error"] = error

        if failed_count is not None:
            update_expression_parts.append("BaselineFailedFiles = :failed_count")
            expression_attribute_values[":failed_count"] = failed_count

        if update_expression_parts:
            update_expression = "SET " + ", ".join(update_expression_parts)

            table.update_item(
                Key={"PK": f"testrun#{test_run_id}", "SK": "metadata"},
                UpdateExpression=update_expression,
                ExpressionAttributeNames=expression_attribute_names
                if expression_attribute_names
                else None,
                ExpressionAttributeValues=expression_attribute_values,
            )
            logger.info(f"Updated test run {test_run_id}")
    except Exception as e:
        logger.error(f"Failed to update test run status: {e}")
