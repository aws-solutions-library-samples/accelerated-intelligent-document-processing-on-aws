# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Batch Pre-Processor Lambda

Extracts ZIP files from staging bucket and uploads individual files to the
input bucket for processing. Updates the job record with per-file status.

Safety bounds (configurable via env vars):
  * MAX_UNCOMPRESSED_BYTES — cap on the SUM of declared uncompressed sizes
    across the whole zip (default: 20 GB). Enforced before any upload begins
    so a malicious/oversized zip cannot exhaust Lambda memory or run the
    function to timeout before the caller sees a status.
  * MAX_ENTRIES — cap on the number of non-directory entries in the zip
    (default: 10,000).

Entries are streamed (zip_ref.open + s3.upload_fileobj) instead of loaded
whole into memory.

Per-entry failures (single-file upload errors) are isolated: the offending
filename is recorded with Status.FAILED on the job record and processing
continues with the remaining entries, so downstream JobTracker can converge
the job to PARTIALLY_SUCCEEDED / FAILED / SUCCEEDED as appropriate.
"""

import logging
import os
import tempfile
import zipfile
from typing import Dict, List, Optional, Tuple

import boto3
from idp_common.job_service import create_job_service
from idp_common.models import Status
from idp_common.utils.log_sanitizer import sanitize_event_for_logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

s3 = boto3.client("s3")

INPUT_BUCKET = os.environ.get("INPUT_BUCKET_NAME", "")
TRACKING_TABLE = os.environ.get("TRACKING_TABLE", "")

# 20 GiB default — well above the 5 GB compressed cap enforced on the
# presigned POST in the API, but small enough that a runaway inflation
# won't take down the Lambda before the bound check fires.
MAX_UNCOMPRESSED_BYTES = int(
    os.environ.get("MAX_UNCOMPRESSED_BYTES", str(20 * 1024**3))
)
MAX_ENTRIES = int(os.environ.get("MAX_ENTRIES", "10000"))

job_service = create_job_service()


class ZipBoundsExceeded(ValueError):
    """Raised when a zip violates the configured safety bounds."""


def handler(event, context):
    """Process EventBridge events for uploaded ZIP files."""
    logger.info(f"Processing event: {sanitize_event_for_logging(event)}")

    # EventBridge event format
    bucket = event["detail"]["bucket"]["name"]
    key = event["detail"]["object"]["key"]

    # Expected key format: jobs/{uuid}/{filename}.zip
    if not key.endswith(".zip"):
        logger.warning(f"Skipping non-ZIP file: {key}")
        return {"statusCode": 200}

    # Extract job_id (uuid) from key
    parts = key.split("/")
    if len(parts) != 3 or parts[0] != "jobs":
        logger.warning(f"Unexpected key format: {key}")
        return {"statusCode": 200}

    job_id = parts[1]  # uuid
    logger.info(f"Processing ZIP for job: {job_id}")

    # Read configuration version from job record (if set by POST /jobs)
    config_version = None
    try:
        job_record = job_service.get_job_record(job_id) if job_service else None
        if job_record:
            config_version = job_record.get("ConfigurationVersion")
            if config_version:
                logger.info(f"Job {job_id} uses config version: {config_version}")
    except Exception as e:
        logger.warning(f"Could not read job record for config version: {e}")

    try:
        succeeded, failed = extract_and_upload(bucket, key, job_id, config_version)
    except ZipBoundsExceeded as e:
        # Bound violation is a terminal outcome for the whole job; write a
        # FAILED marker so the API's GET /jobs/{id} surfaces a human-readable
        # reason instead of hanging in PENDING_UPLOAD / IN_PROGRESS forever.
        logger.error(
            "Rejecting job %s: %s", job_id, e, exc_info=True
        )
        _mark_job_rejected(job_id, str(e))
        # Not re-raising: the zip is in terminal FAILED state from the
        # caller's perspective, and retrying the Lambda won't help.
        return {"statusCode": 200}
    except Exception as e:
        logger.error(f"Error processing {key}: {e}", exc_info=True)
        raise

    # Update job record with per-file status. Succeeded entries begin
    # IN_PROGRESS (JobTracker advances them to terminal states); failed
    # entries go straight to FAILED.
    if job_service and (succeeded or failed):
        file_status: Dict[str, Status] = {}
        for f in succeeded:
            file_status[f] = Status.IN_PROGRESS
        for f in failed:
            file_status[f] = Status.FAILED
        job_service.update_job_files(job_id, file_status)
        logger.info(
            "Updated job %s: %d succeeded, %d failed",
            job_id,
            len(succeeded),
            len(failed),
        )

    return {"statusCode": 200}


def extract_and_upload(
    staging_bucket: str, zip_key: str, job_id: str, config_version: Optional[str] = None
) -> Tuple[List[str], List[str]]:
    """
    Extract ZIP and upload individual files to the input bucket.

    Args:
        staging_bucket: S3 bucket containing the uploaded zip
        zip_key: S3 key of the zip file
        job_id: Job identifier
        config_version: Optional configuration version to set as S3 metadata
            on each extracted file. The pipeline reads this to select the
            processing configuration.

    Returns:
        (succeeded_filenames, failed_filenames) — per-entry outcomes suitable
        for feeding into JobService.update_job_files. Failed filenames have
        already had their error logged; the caller is responsible for
        reflecting them on the job record.

    Raises:
        ZipBoundsExceeded: If the zip exceeds MAX_ENTRIES or the declared
            total uncompressed size exceeds MAX_UNCOMPRESSED_BYTES. Raised
            BEFORE any uploads begin.
    """
    succeeded: List[str] = []
    failed: List[str] = []
    seen_names: Dict[str, int] = {}

    with tempfile.NamedTemporaryFile() as temp_file:
        s3.download_fileobj(staging_bucket, zip_key, temp_file)
        temp_file.seek(0)

        with zipfile.ZipFile(temp_file, "r") as zip_ref:
            entries = [i for i in zip_ref.infolist() if not i.is_dir()]

            # --- Pre-flight bound checks ----------------------------------
            if len(entries) > MAX_ENTRIES:
                raise ZipBoundsExceeded(
                    f"Zip has {len(entries)} entries, exceeding MAX_ENTRIES={MAX_ENTRIES}"
                )

            total_declared = sum(max(0, i.file_size) for i in entries)
            if total_declared > MAX_UNCOMPRESSED_BYTES:
                raise ZipBoundsExceeded(
                    f"Zip declared uncompressed size {total_declared} bytes exceeds "
                    f"MAX_UNCOMPRESSED_BYTES={MAX_UNCOMPRESSED_BYTES}"
                )
            # --- End bound checks -----------------------------------------

            for file_info in entries:
                basename = os.path.basename(file_info.filename)
                if not basename:
                    continue

                # Handle filename collisions deterministically.
                if basename in seen_names:
                    seen_names[basename] += 1
                    name, ext = os.path.splitext(basename)
                    filename = f"{name}_{seen_names[basename]}{ext}"
                else:
                    seen_names[basename] = 0
                    filename = basename

                dest_key = f"jobs/{job_id}/{filename}"

                # Stream entry content to S3 without materializing it in
                # Lambda memory. ZipExtFile is file-like and supports the
                # read(size) interface that upload_fileobj expects.
                try:
                    extra_args = (
                        {"Metadata": {"config-version": config_version}}
                        if config_version
                        else None
                    )
                    with zip_ref.open(file_info, "r") as entry_stream:
                        s3.upload_fileobj(
                            entry_stream,
                            INPUT_BUCKET,
                            dest_key,
                            ExtraArgs=extra_args,
                        )
                    logger.info(f"Uploaded {filename} to {dest_key}")
                    succeeded.append(filename)
                except Exception:
                    # Isolate per-entry failures (bad zip member, S3 error,
                    # whatever) so a single rotten entry doesn't leave the
                    # rest of the batch orphaned. Log with stack trace; the
                    # handler will reflect FAILED on the job record.
                    logger.exception(
                        "Failed to upload %s from job %s", filename, job_id
                    )
                    failed.append(filename)

    return succeeded, failed


def _mark_job_rejected(job_id: str, reason: str) -> None:
    """
    Mark a whole-job failure on the tracking table when the zip is rejected
    before any per-entry work begins (e.g., bound violations).

    Writes a single synthetic "__rejected__" file entry with Status.FAILED so
    compute_job_status (in the API Lambda) converges to FAILED rather than
    sitting in PENDING_UPLOAD. The reason is logged but not surfaced via the
    API today; callers see HTTP-level status values only.
    """
    if not job_service:
        logger.warning(
            "Cannot mark job %s rejected — job_service unavailable. Reason: %s",
            job_id,
            reason,
        )
        return
    try:
        job_service.update_job_files(job_id, {"__rejected__": Status.FAILED})
    except Exception:
        logger.exception(
            "Failed to mark job %s as rejected (reason: %s)", job_id, reason
        )
