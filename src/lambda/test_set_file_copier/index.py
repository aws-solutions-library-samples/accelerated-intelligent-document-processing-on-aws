# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import logging
import os
from datetime import datetime, timezone

import boto3
from idp_common.s3 import find_matching_files  # type: ignore

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")


def handler(event, context):
    """Process test set file copy jobs from SQS"""
    logger.info(f"Test set file copier invoked with {len(event['Records'])} messages")

    for record in event["Records"]:
        try:
            message = json.loads(record["body"])

            test_set_id = message["testSetId"]
            file_pattern = message["filePattern"]
            bucket_type = message["bucketType"]
            tracking_table = message["trackingTable"]
            mode = message.get("mode", "create")
            modified_after = message.get("modifiedAfter")

            # Get environment variables
            input_bucket = os.environ["INPUT_BUCKET"]
            test_set_bucket = os.environ["TEST_SET_BUCKET"]
            baseline_bucket = os.environ["BASELINE_BUCKET"]

            # Determine source bucket based on bucket type
            if bucket_type == "input":
                source_bucket = input_bucket
            elif bucket_type == "testset":
                source_bucket = test_set_bucket
            else:
                raise ValueError(f"Invalid bucket type: {bucket_type}")

            logger.info(
                f"Processing test set {test_set_id} (mode={mode}) with pattern '{file_pattern}' from {bucket_type} bucket"
            )

            # Find matching files in source bucket
            matching_files = find_matching_files(
                source_bucket, file_pattern, modified_after=modified_after
            )

            if not matching_files:
                raise ValueError(f"No files found matching pattern: {file_pattern}")

            logger.info(
                f"Found {len(matching_files)} files matching pattern, matching files {matching_files}"
            )

            # Validate baseline folders exist for all input files before copying anything
            missing_baselines = []
            for file_key in matching_files:
                try:
                    if bucket_type == "testset":
                        # For testset bucket, baseline is in the same bucket under baseline/ path
                        path_parts = file_key.split("/")
                        if len(path_parts) >= 3 and path_parts[1] == "input":
                            test_set_name = path_parts[0]
                            file_name = "/".join(path_parts[2:])
                            baseline_prefix = f"{test_set_name}/baseline/{file_name}/"
                            baseline_check_bucket = source_bucket
                        else:
                            missing_baselines.append(file_key)
                            continue
                    else:
                        # For input bucket, baseline is in separate baseline bucket
                        baseline_prefix = f"{file_key}/"
                        baseline_check_bucket = baseline_bucket

                    # Check if baseline folder exists by listing objects with prefix
                    response = s3.list_objects_v2(
                        Bucket=baseline_check_bucket, Prefix=baseline_prefix, MaxKeys=1
                    )

                    if "Contents" not in response or len(response["Contents"]) == 0:
                        missing_baselines.append(file_key)

                except Exception as e:
                    logger.error(f"Error checking baseline folder {file_key}: {e}")
                    missing_baselines.append(file_key)

            # Handle missing baselines based on bucket type
            total_matched = len(matching_files)
            if missing_baselines:
                if bucket_type == "input":
                    # Input bucket: skip files without baselines (partial ground truth is expected)
                    files_to_copy = [
                        f for f in matching_files if f not in set(missing_baselines)
                    ]
                    if not files_to_copy:
                        raise ValueError(
                            f"None of the {total_matched} matching files have baseline data"
                        )
                    logger.info(
                        f"Skipping {len(missing_baselines)} files without baselines, copying {len(files_to_copy)} files"
                    )
                else:
                    # Test set bucket: strict validation
                    raise ValueError(
                        f"Missing baseline folders for: {', '.join(missing_baselines)}"
                    )
            else:
                files_to_copy = matching_files

            # Update status to COPYING before starting file operations
            if mode == "create":
                _update_test_set_status(tracking_table, test_set_id, "COPYING")

            # Copy input files to test set bucket
            if bucket_type == "testset":
                _copy_input_files_from_test_set_bucket(test_set_id, files_to_copy)
            else:
                _copy_input_files_from_input_bucket(test_set_id, files_to_copy)

            # Copy baseline folders to test set bucket
            if bucket_type == "testset":
                _copy_baseline_from_testset(test_set_id, files_to_copy)
            else:
                _copy_baseline_from_baseline_bucket(test_set_id, files_to_copy)

            logger.info(
                f"Copied {len(files_to_copy)} input files and {len(files_to_copy)} baseline folders"
            )

            # Recount total files in test set for accurate fileCount
            file_count = _count_test_set_files(test_set_id)

            # Build result message
            skipped_count = len(missing_baselines)
            last_add_result = None
            if skipped_count > 0:
                action = "Added" if mode == "append" else "Created with"
                last_add_result = f"{action} {len(files_to_copy)} of {total_matched} files ({skipped_count} excluded - no baseline data)"
            elif mode == "append":
                last_add_result = f"Added {len(files_to_copy)} files"

            # Update test set record with completion status and file count
            _update_test_set_status(
                tracking_table,
                test_set_id,
                "COMPLETED",
                file_count=file_count,
                last_add_result=last_add_result,
            )

            logger.info(
                f"Test set {test_set_id} file copying completed successfully: {len(files_to_copy)} files (total: {file_count})"
            )

        except Exception as e:
            logger.error(f"Error processing test set {test_set_id}: {str(e)}")
            if "test_set_id" in locals() and "tracking_table" in locals():
                _update_test_set_status(
                    tracking_table, test_set_id, "FAILED", error=str(e)
                )

    return {"statusCode": 200}


def _copy_input_files_from_input_bucket(test_set_id, files):
    """Copy input files from input bucket to test set bucket"""
    input_bucket = os.environ["INPUT_BUCKET"]
    test_set_bucket = os.environ["TEST_SET_BUCKET"]

    for file_key in files:
        dest_key = f"{test_set_id}/input/{file_key}"

        s3.copy_object(
            CopySource={"Bucket": input_bucket, "Key": file_key},
            Bucket=test_set_bucket,
            Key=dest_key,
        )

        logger.info(f"Copied input file: {file_key} -> {test_set_bucket}/{dest_key}")


def _copy_input_files_from_test_set_bucket(test_set_id, files):
    """Copy input files from test set bucket to new test set folder"""
    test_set_bucket = os.environ["TEST_SET_BUCKET"]

    for file_key in files:
        # Extract actual file path from test_set/input/file_path
        path_parts = file_key.split("/")
        if len(path_parts) >= 3 and path_parts[1] == "input":
            actual_file_path = "/".join(path_parts[2:])
            dest_key = f"{test_set_id}/input/{actual_file_path}"
        else:
            dest_key = f"{test_set_id}/input/{file_key}"

        s3.copy_object(
            CopySource={"Bucket": test_set_bucket, "Key": file_key},
            Bucket=test_set_bucket,
            Key=dest_key,
        )

        logger.info(f"Copied input file: {file_key} -> {test_set_bucket}/{dest_key}")


def _copy_baseline_from_baseline_bucket(test_set_id, files):
    """Copy baseline folders from baseline bucket to test set bucket"""
    baseline_bucket = os.environ["BASELINE_BUCKET"]
    test_set_bucket = os.environ["TEST_SET_BUCKET"]

    for file_key in files:
        baseline_prefix = f"{file_key}/"
        dest_prefix = f"{test_set_id}/baseline/{file_key}/"

        paginator = s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=baseline_bucket, Prefix=baseline_prefix)

        for page in pages:
            if "Contents" in page:
                for obj in page["Contents"]:
                    source_key = obj["Key"]
                    dest_key = source_key.replace(baseline_prefix, dest_prefix, 1)

                    s3.copy_object(
                        CopySource={"Bucket": baseline_bucket, "Key": source_key},
                        Bucket=test_set_bucket,
                        Key=dest_key,
                    )

                    logger.info(
                        f"Copied baseline file: {source_key} -> {test_set_bucket}/{dest_key}"
                    )


def _copy_baseline_from_testset(test_set_id, files):
    """Copy baseline files from testset bucket where baselines are in test_set/baseline/ path"""
    test_set_bucket = os.environ["TEST_SET_BUCKET"]

    for file_key in files:
        # Extract test set name and file name from path (format: test_set_name/input/file_name)
        path_parts = file_key.split("/")
        if len(path_parts) >= 3 and path_parts[1] == "input":
            source_test_set_name = path_parts[0]
            file_name = "/".join(path_parts[2:])  # Get full path after 'input/'

            # Source baseline path in testset bucket
            source_baseline_prefix = f"{source_test_set_name}/baseline/{file_name}/"
            # Destination baseline path
            dest_baseline_prefix = f"{test_set_id}/baseline/{file_name}/"

            # List all objects in the source baseline folder
            paginator = s3.get_paginator("list_objects_v2")
            pages = paginator.paginate(
                Bucket=test_set_bucket, Prefix=source_baseline_prefix
            )

            for page in pages:
                if "Contents" in page:
                    for obj in page["Contents"]:
                        source_key = obj["Key"]
                        # Replace the source baseline prefix with dest baseline prefix
                        dest_key = source_key.replace(
                            source_baseline_prefix, dest_baseline_prefix, 1
                        )

                        # Copy file
                        s3.copy_object(
                            CopySource={"Bucket": test_set_bucket, "Key": source_key},
                            Bucket=test_set_bucket,
                            Key=dest_key,
                        )

                        logger.info(
                            f"Copied testset baseline file: {source_key} -> {test_set_bucket}/{dest_key}"
                        )
        else:
            logger.warning(
                f"Unexpected file path format for testset baseline: {file_key}"
            )


def _count_test_set_files(test_set_id):
    """Count total input files in a test set by listing S3 objects"""
    test_set_bucket = os.environ["TEST_SET_BUCKET"]
    prefix = f"{test_set_id}/input/"
    count = 0

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=test_set_bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith("/"):
                count += 1

    logger.info(f"Counted {count} total input files in test set {test_set_id}")
    return count


def _update_test_set_status(
    tracking_table,
    test_set_id,
    status,
    error=None,
    file_count=None,
    last_add_result=None,
):
    """Update test set status in tracking table"""
    table = dynamodb.Table(tracking_table)  # type: ignore

    try:
        # statusUpdatedAt is what makes an abandoned copy reapable: the host resolver
        # cannot otherwise tell a slow copy from one whose worker died.
        update_expression = "SET #status = :status, statusUpdatedAt = :now"
        expression_values = {
            ":status": status,
            ":now": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        expression_names = {"#status": "status"}

        if error:
            update_expression += ", #error = :error"
            expression_values[":error"] = error
            expression_names["#error"] = "error"

        if file_count is not None:
            update_expression += ", fileCount = :count"
            expression_values[":count"] = file_count

        # Never persisted. This message used to be stored on the record and rendered
        # by the test-set list, which made it immortal: dismissing the alert only
        # cleared client state, the next poll re-read the attribute, and nothing ever
        # deleted it — sets announced completed work from earlier sessions
        # indefinitely. The UI now detects the UPDATING -> COMPLETED transition and
        # shows one transient message, the same way synthetic-generation jobs report
        # completion. Logged for operators; always cleared so stale values from before
        # this change disappear the next time a set is touched.
        if last_add_result:
            logger.info(f"Test set {test_set_id}: {last_add_result}")
        # REMOVE contentSignature so the resolver's warm-container memo
        # (in `_reconcile_test_set_tracking_entry`) doesn't skip the next
        # reconcile via TTL match: our fileCount write invalidated whatever
        # signature was there, and the next reconcile needs to rebuild it.
        # Without this, a direct-S3 add landing right after the copier
        # completes could go unnoticed for up to the memo TTL.
        update_expression += " REMOVE lastAddResult, contentSignature"

        table.update_item(
            Key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
            UpdateExpression=update_expression,
            ExpressionAttributeNames=expression_names,
            ExpressionAttributeValues=expression_values,
        )

        logger.info(
            f"Updated test set {test_set_id} status to {status}"
            + (f" with {file_count} files" if file_count else "")
        )

    except Exception as e:
        logger.error(f"Failed to update test set status for {test_set_id}: {e}")  # nosec B608 - log message f-string, not a SQL query
