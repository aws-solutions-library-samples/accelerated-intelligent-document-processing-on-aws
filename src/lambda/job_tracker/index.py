# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import boto3
import json
import os
import logging
import zipfile
from idp_common.job_service import create_job_service
from idp_common.models import Status
from idp_common.utils.log_sanitizer import sanitize_event_for_logging
from io import BytesIO

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

OUTPUT_BUCKET = os.environ["OUTPUT_BUCKET"]
TERMINAL_STATUSES = {Status.COMPLETED.value, Status.FAILED.value, Status.ABORTED.value}

s3 = boto3.client("s3")
job_service = create_job_service()


def handler(event, context):
    logger.info(f"Processing event: {json.dumps(sanitize_event_for_logging(event))}")

    # Extract document ID from Step Functions event
    input_data = json.loads(event["detail"]["input"])
    doc_id = input_data.get("document", {}).get("document_id", "")

    # Check if this is a job document
    if not doc_id.startswith("jobs/"):
        logger.info(f"Not a job document: {doc_id}")
        return {"statusCode": 200, "body": "Not a job document"}

    # Parse job_id and filename from jobs/{job_id}/{filename}
    parts = doc_id.split("/", 2)
    if len(parts) < 3:
        logger.error(f"Invalid job document path: {doc_id}")
        return {"statusCode": 400, "body": "Invalid job document path"}

    job_id, filename = parts[1], parts[2]
    workflow_status = event["detail"]["status"]

    # Map workflow status to file status
    if workflow_status == "SUCCEEDED":
        file_status = Status.COMPLETED
    elif workflow_status == "ABORTED":
        file_status = Status.ABORTED
    else:
        file_status = Status.FAILED

    # Update job file status and get updated Files map
    files = job_service.update_file_status(job_id, filename, file_status)
    if not files:
        logger.error(f"Job not found: {job_id}")
        return {"statusCode": 404, "body": "Job not found"}

    logger.info(f"Updated job {job_id} file {filename} to {file_status.value}")

    # Check if all files are in terminal state
    if not all(s in TERMINAL_STATUSES for s in files.values()):
        logger.info(f"Job {job_id} still processing: {files}")
        return {"statusCode": 200, "body": "Job still processing"}

    # All files complete - create ZIP
    logger.info(f"All files complete for job {job_id}, creating ZIP")
    try:
        create_results_zip(job_id)
        # Mark the job's results as ready only after the zip is successfully
        # uploaded. The API handler uses this flag to gate SUCCEEDED/
        # PARTIALLY_SUCCEEDED so clients never race into a 404 on download.
        job_service.mark_results_ready(job_id, True)
        return {"statusCode": 200, "body": f"ZIP created for job {job_id}"}
    except Exception as e:
        logger.error(f"Failed to create ZIP for job {job_id}: {e}")
        raise


def create_results_zip(job_id: str):
    """Create zip of all result files for a job."""
    prefix = f"jobs/{job_id}"
    response = s3.list_objects_v2(Bucket=OUTPUT_BUCKET, Prefix=prefix)
    objects = response.get("Contents", [])

    if not objects:
        logger.warning(f"No output files found for job {job_id}")
        return

    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for obj in objects:
            file_data = s3.get_object(Bucket=OUTPUT_BUCKET, Key=obj["Key"])[
                "Body"
            ].read()
            zip_file.writestr(obj["Key"], file_data)

    zip_key = f"jobs/{job_id}/results.zip"
    s3.put_object(Bucket=OUTPUT_BUCKET, Key=zip_key, Body=zip_buffer.getvalue())
    logger.info(f"Uploaded ZIP to s3://{OUTPUT_BUCKET}/{zip_key}")
