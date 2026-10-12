# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import logging
import os

import boto3
from idp_common.job_completion import (
    JOB_KEY_PREFIX,
    complete_job_results,
    finalize_job_file,
    parse_job_document_id,
)
from idp_common.job_service import create_job_service
from idp_common.models import Status
from idp_common.utils.log_sanitizer import sanitize_event_for_logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

OUTPUT_BUCKET = os.environ["OUTPUT_BUCKET"]

s3 = boto3.client("s3")
job_service = create_job_service()


def handler(event, context):
    """Record a job file's outcome when its workflow execution ends.

    This is one of two writers that can finalize a job file; the other is the
    ingest malware gate in ``queue_sender``, which refuses documents that never
    start an execution and so never reach this handler. Both go through
    ``idp_common.job_completion.finalize_job_file`` so the all-files-terminal
    check and the results zip exist once.

    It also answers a second event shape, ``{"idpJobCompletion": {"jobId": ...}}``,
    which that gate sends when its refusal was the *last* file in a batch to go
    terminal. Only this function can serve it: building the zip needs read and
    write on the output bucket plus the memory, timeout and ephemeral storage
    declared on this resource, none of which the 128 MB per-upload ingest
    function has. See ``_delegate_job_results`` in ``queue_sender``.
    """
    logger.info(f"Processing event: {json.dumps(sanitize_event_for_logging(event))}")

    # Keyed on the key being PRESENT, not on its contents being truthy. An empty
    # or malformed payload is still a delegation and must be reported as one;
    # falling through would hand it to the Step Functions branch, which raises
    # KeyError on `detail` and so reports a malformed delegation as a crash.
    completion = event.get("idpJobCompletion") if isinstance(event, dict) else None
    if completion is not None:
        job_id = (completion or {}).get("jobId", "")
        if not job_id:
            logger.error("idpJobCompletion event carries no jobId")
            return {"statusCode": 400, "body": "Missing jobId"}
        result = complete_job_results(
            job_id=job_id,
            job_service=job_service,
            s3_client=s3,
            output_bucket=OUTPUT_BUCKET,
        )
        return {"statusCode": 200, "body": json.dumps(result)}

    # Extract document ID from Step Functions event
    input_data = json.loads(event["detail"]["input"])
    doc_id = input_data.get("document", {}).get("document_id", "")

    # Two distinct answers, deliberately kept apart: a document outside jobs/ is
    # the ordinary case and nothing to report, whereas a jobs/ key that does not
    # name a file is malformed and should be visible as a client error.
    if not doc_id.startswith(JOB_KEY_PREFIX):
        logger.info(f"Not a job document: {doc_id}")
        return {"statusCode": 200, "body": "Not a job document"}
    parsed = parse_job_document_id(doc_id)
    if parsed is None:
        logger.error(f"Invalid job document path: {doc_id}")
        return {"statusCode": 400, "body": "Invalid job document path"}
    job_id, filename = parsed

    workflow_status = event["detail"]["status"]

    # Map workflow status to file status
    if workflow_status == "SUCCEEDED":
        file_status = Status.COMPLETED
    elif workflow_status == "ABORTED":
        file_status = Status.ABORTED
    else:
        file_status = Status.FAILED

    result = finalize_job_file(
        job_id=job_id,
        filename=filename,
        status=file_status,
        job_service=job_service,
        s3_client=s3,
        output_bucket=OUTPUT_BUCKET,
    )

    if not result["found"]:
        return {"statusCode": 404, "body": "Job not found"}
    if not result["job_complete"]:
        return {"statusCode": 200, "body": "Job still processing"}
    return {"statusCode": 200, "body": f"ZIP created for job {job_id}"}
