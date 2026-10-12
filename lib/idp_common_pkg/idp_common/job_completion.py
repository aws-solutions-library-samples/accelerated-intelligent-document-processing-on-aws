# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Driving a /Jobs API batch to completion from more than one writer.

``job_tracker`` used to own this entirely, and it is driven by **Step Functions
execution status change** events. That is fine as long as every file in a job
reaches a terminal state by running a workflow, which stopped being true when the
ingest malware gate was added: a quarantined or unscannable ``jobs/<id>/<file>``
document never starts an execution, so ``job_tracker`` never hears about it, the
``Files`` map keeps it non-terminal forever, ``results.zip`` is never written, and
``GET /jobs/<id>`` reports ``IN_PROGRESS`` for good. A job that hangs silently is
worse than one that reports a failure.

So the completion tail lives here and both writers call it: ``job_tracker`` when
an execution ends, and ``queue_sender`` when the gate refuses a document.

**What the gate writes into the ``Files`` map is ``Status.FAILED``,** not the
document's own ``QUARANTINED`` / ``MALWARE_SCAN_FAILED``. The map is the job-level
view, and the terminal-status set that reads it is spelled out twice —
:data:`TERMINAL_FILE_STATUSES` here, which ``job_tracker`` and the gate both go
through, and the ``terminal`` literal in ``api_handler.compute_job_status`` — so a
value neither knows would hang the job in a new way. The precise reason stays on
the document row, where the UI shows it.
"""

import logging
from io import BytesIO
from typing import Any, Dict, Optional, Tuple
from zipfile import ZIP_DEFLATED, ZipFile

from idp_common.models import Status

logger = logging.getLogger(__name__)

#: The file statuses that end a job file's life, as the ``Files`` map stores them.
#: Mirrors ``api_handler.compute_job_status``'s own set deliberately: this one
#: decides when to build the zip, that one decides what to report, and they are
#: reached by different code paths.
TERMINAL_FILE_STATUSES = frozenset(
    {Status.COMPLETED.value, Status.FAILED.value, Status.ABORTED.value}
)

#: Key prefix identifying a document submitted through the /Jobs API.
JOB_KEY_PREFIX = "jobs/"


def parse_job_document_id(document_id: str) -> Optional[Tuple[str, str]]:
    """Split a ``jobs/<job_id>/<filename>`` document id, or return ``None``.

    ``None`` means "not a job document", which is the common case — most
    documents are ordinary uploads. A ``jobs/`` key with no filename component is
    also ``None``: it cannot name a file in the ``Files`` map, so there is nothing
    to finalize.

    ``maxsplit=2`` so a filename containing ``/`` survives intact.
    """
    if not document_id or not document_id.startswith(JOB_KEY_PREFIX):
        return None
    parts = document_id.split("/", 2)
    if len(parts) < 3 or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def finalize_job_file(
    job_id: str,
    filename: str,
    status: Status,
    job_service: Any,
    s3_client: Any,
    output_bucket: str,
    build_results: bool = True,
) -> Dict[str, Any]:
    """Record one job file's terminal status and build the zip if that was the last.

    Args:
        job_id: The job the file belongs to.
        filename: The file's key within the job, as it appears in ``Files``.
        status: Terminal status to record. Must be a member of
            :data:`TERMINAL_FILE_STATUSES`, or the job can never complete.
        job_service: A ``JobDynamoDBService``.
        s3_client: boto3 S3 client used to read outputs and write the zip.
        output_bucket: Bucket holding ``jobs/<job_id>/`` outputs.
        build_results: Whether to build ``results.zip`` and set ``ResultsReady``
            when this call completes the job. ``False`` is for a caller that is
            not resourced to do it — the ingest malware gate runs in
            ``queue_sender``, a 128 MB/30 s function invoked once per upload,
            whereas zipping a job's outputs is why ``job_tracker`` carries
            3008 MB, a 900 s timeout and 5 GB of ephemeral storage. Such a
            caller must act on ``job_complete`` in the result and delegate, or
            the job stays at ``IN_PROGRESS``: ``api_handler`` holds a
            SUCCEEDED/PARTIALLY_SUCCEEDED job there until ``ResultsReady``.

    Returns:
        ``{"found": bool, "files": dict | None, "job_complete": bool,
        "results_ready": bool}``, plus ``"results_deferred": True`` when
        ``build_results`` was False and this call completed the job.
        ``found`` is False when the job row is gone
        (expired by TTL, or deleted) — not an error for the caller, which has
        already recorded the document's own status.

    Raises:
        Whatever S3 raises while building the zip. Deliberately not swallowed:
        ``mark_results_ready`` must not run if the zip is missing, or the API
        would answer SUCCEEDED and send a client to a 404.
    """
    if status.value not in TERMINAL_FILE_STATUSES:
        # Refused rather than stored: a non-terminal value here is the exact
        # defect this module exists to fix, and it would be invisible until a
        # customer reported a job stuck at IN_PROGRESS.
        raise ValueError(
            f"finalize_job_file needs a terminal status, got {status.value!r}; "
            f"expected one of {sorted(TERMINAL_FILE_STATUSES)}"
        )

    files = job_service.update_file_status(job_id, filename, status)
    if not files:
        logger.error("Job not found while finalizing %s: %s", filename, job_id)
        return {
            "found": False,
            "files": None,
            "job_complete": False,
            "results_ready": False,
        }

    logger.info("Job %s file %s -> %s", job_id, filename, status.value)

    if not all(s in TERMINAL_FILE_STATUSES for s in files.values()):
        logger.info("Job %s still processing: %s", job_id, files)
        return {
            "found": True,
            "files": files,
            "job_complete": False,
            "results_ready": False,
        }

    if not build_results:
        # The caller is not resourced to build the zip and will delegate. Said
        # plainly because the two return shapes differ in a way that matters:
        # `job_complete` is True and `results_ready` is False, and a caller that
        # ignores the first leaves the job at IN_PROGRESS for ever.
        logger.info(
            "All files terminal for job %s; leaving the results zip to the "
            "delegate, as this caller cannot build it",
            job_id,
        )
        return {
            "found": True,
            "files": files,
            "job_complete": True,
            "results_ready": False,
            "results_deferred": True,
        }

    logger.info("All files terminal for job %s, creating results zip", job_id)
    wrote_zip = create_results_zip(job_id, s3_client, output_bucket)
    # Marked ready only if a zip was actually written, and `wrote_zip` rather than
    # a literal `True` is the whole point: `api_handler` holds a job at
    # IN_PROGRESS while SUCCEEDED/PARTIALLY_SUCCEEDED and not ResultsReady, so
    # claiming ready with no zip hands a client a results URL that 404s.
    #
    # This does NOT reintroduce the hang this module exists to fix. The only job
    # that completes with no outputs is one whose every file was refused, and
    # `compute_job_status` scores that **FAILED** — a status the ResultsReady gate
    # does not apply to, so it still reports terminal. A mixed batch has outputs
    # from its successful files, so the zip exists and the flag is true. What is
    # withheld is only the genuinely anomalous case: a job that succeeded and
    # produced nothing.
    job_service.mark_results_ready(job_id, wrote_zip)
    return {
        "found": True,
        "files": files,
        "job_complete": True,
        "results_ready": wrote_zip,
    }


def complete_job_results(
    job_id: str, job_service: Any, s3_client: Any, output_bucket: str
) -> Dict[str, Any]:
    """Build ``results.zip`` for an already-all-terminal job and mark it ready.

    The second half of a ``finalize_job_file(..., build_results=False)`` call,
    run by whoever is resourced for it. Idempotent in the way that matters: the
    zip is rebuilt from whatever is under ``jobs/<job_id>/`` and the flag is
    rewritten, so a duplicate delivery costs one wasted zip rather than a wrong
    answer.

    Deliberately does **not** re-check that every file is terminal. The caller
    that delegated here already established that, and re-reading the job row to
    confirm it would make a lost delegation indistinguishable from a job that
    went non-terminal again — which cannot happen, since the ``Files`` map only
    moves toward terminal.
    """
    wrote_zip = create_results_zip(job_id, s3_client, output_bucket)
    job_service.mark_results_ready(job_id, wrote_zip)
    logger.info(
        "Completed deferred results for job %s (zip written: %s)", job_id, wrote_zip
    )
    return {"job_id": job_id, "results_ready": wrote_zip}


def create_results_zip(job_id: str, s3_client: Any, output_bucket: str) -> bool:
    """Zip every output object under ``jobs/<job_id>/`` into ``results.zip``.

    Returns True when a zip was written, False when the job produced no outputs
    at all — which is the normal shape of a job whose every file was refused at
    the ingest gate, so it is reported rather than raised.

    Paginated: ``list_objects_v2`` caps at 1000 keys per call, and a job of a few
    hundred multi-page documents exceeds that, which would silently zip a prefix
    of the results.

    ⚠️ **The loop advances on the continuation token, never on ``IsTruncated``.**
    Those look interchangeable and are not: the token is the only field whose
    *absence* can terminate the loop, whereas a truthy non-boolean ``IsTruncated``
    spins forever. The property to rely on is the *type* of the token (a string),
    not its truthiness — a bare ``MagicMock`` returns a truthy object for every
    field, so a loop guarded on truthiness alone becomes a hang the moment a
    test misses a configuration, which is the most expensive shape a test
    failure has.
    """
    # ⚠️ The trailing slash is load-bearing. `list_objects_v2` matches on a
    # string prefix, so "jobs/12" also matches every key under "jobs/123/", and
    # job 12's results.zip would carry job 123's extracted documents — a
    # cross-job disclosure through a presigned URL, not merely a wrong file
    # count. Pre-existing in `job_tracker` before this module was extracted; job
    # ids are UUIDs in practice, so one is not a prefix of another, which is why
    # it had never been seen.
    prefix = f"{JOB_KEY_PREFIX}{job_id}/"
    keys: list = []
    continuation: Optional[str] = None
    while True:
        kwargs: Dict[str, Any] = {"Bucket": output_bucket, "Prefix": prefix}
        if continuation:
            kwargs["ContinuationToken"] = continuation
        response = s3_client.list_objects_v2(**kwargs)
        contents = response.get("Contents") or []
        keys.extend(
            obj["Key"]
            for obj in contents
            # Never zip a previous results.zip into the new one.
            if not obj["Key"].endswith("/results.zip")
        )
        next_token = response.get("NextContinuationToken")
        if not isinstance(next_token, str) or not next_token:
            break
        continuation = next_token

    if not keys:
        logger.warning("No output files found for job %s; no results zip", job_id)
        return False

    zip_buffer = BytesIO()
    with ZipFile(zip_buffer, "w", ZIP_DEFLATED) as zip_file:
        for key in keys:
            body = s3_client.get_object(Bucket=output_bucket, Key=key)["Body"].read()
            zip_file.writestr(key, body)

    zip_key = f"jobs/{job_id}/results.zip"
    s3_client.put_object(Bucket=output_bucket, Key=zip_key, Body=zip_buffer.getvalue())
    logger.info("Uploaded results zip to s3://%s/%s", output_bucket, zip_key)
    return True
