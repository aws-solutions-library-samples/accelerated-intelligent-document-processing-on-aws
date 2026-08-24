# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AgentCore Runtime entrypoint hosting the SEED generator for config bootstrap.

Runs as an HTTP server implementing the AgentCore Runtime service contract
(``/invocations`` POST + ``/ping`` GET on port 8080) via ``BedrockAgentCoreApp``.
WeasyPrint, augraphy, opencv and their native libraries exceed Lambda's package
limits and need a full Debian base, so the generator is hosted on an AgentCore
Runtime rather than a Lambda.

The invocation payload carries the SynthesisJob fields plus the bootstrap
identifiers (jobId, testSetId). A single generation run takes minutes, so the
work runs on a background thread tracked with ``add_async_task``: ``/ping``
reports ``HealthyBusy`` while it runs, keeping the runtime session alive, and
``/invocations`` returns immediately with an acknowledgement. Terminal status is
written to the extension's BootstrapTrackingTable, and a watchdog fails the job
if generation exceeds its time budget.
"""

import logging
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

app = BedrockAgentCoreApp()


def _download_schema_dir(bucket, prefix, dest_dir):
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel = key[len(prefix) :].lstrip("/")
            if not rel:
                continue
            local_path = os.path.join(dest_dir, rel)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            s3.download_file(bucket, key, local_path)


def _run_job(payload):
    """Generate a labeled test set from a staged schema_dir.

    Runs on a background thread; never raises into the caller — terminal status
    is written to the tracking table via _post_status.
    """
    from idp_common.synthesis import engine, packet_io

    job_id = payload["jobId"]
    test_set_id = payload["testSetId"]
    working_bucket = payload["workingBucket"]
    schema_prefix = payload["schemaPrefix"]
    test_set_bucket = payload["testSetBucket"]
    count = int(payload.get("count", 3))
    threshold = int(payload.get("threshold", 7))
    augment = bool(payload.get("augment", False))
    append = bool(payload.get("append", False))
    extra = payload.get("scenario") or payload.get("extra", "")
    model_id = payload.get("modelId") or os.environ.get("GENERATOR_MODEL_ID")
    allowed_field_names = set(payload.get("allowedFieldNames", []))

    work_dir = tempfile.mkdtemp(prefix="synthesis-runtime-")
    try:
        schema_dir = os.path.join(work_dir, "schema")
        out_dir = os.path.join(work_dir, "out")
        os.makedirs(schema_dir, exist_ok=True)
        _download_schema_dir(working_bucket, schema_prefix, schema_dir)

        job = engine.SynthesisJob(
            schema_dir=schema_dir,
            out_dir=out_dir,
            count=count,
            threshold=threshold,
            augment=augment,
            extra=extra,
            model_id=model_id,
        )

        def _status(pct, msg):
            logger.info("[%s] %.0f%% %s", job_id, pct, msg)
            _post_status(payload, job_id, "IN_PROGRESS", f"{pct:.0f}% {msg}")

        result = engine.synthesize(job, status_cb=_status)
        if not result.success or not result.packet_dir:
            _post_status(payload, job_id, "FAILED", result.error or "Generation failed")
            _fail_test_set(test_set_id, append, test_set_bucket)
            return

        documents = packet_io.read_packet(result.packet_dir)
        if allowed_field_names:
            removed = packet_io.prune_documents_to_allowed_fields(
                documents, allowed_field_names
            )
            if removed:
                logger.info(
                    "[%s] pruned %d extra field(s) not in schema from baseline",
                    job_id,
                    removed,
                )

        uploaded = packet_io.upload_packet_to_test_set(
            documents, test_set_id, test_set_bucket, name_prefix=f"{job_id[:8]}_"
        )
        total = _test_set_input_count(test_set_bucket, test_set_id)
        _post_status(
            payload,
            job_id,
            "COMPLETED",
            f"{uploaded} document(s) in test set {test_set_id}",
            usage=result.usage.as_dict() if result.usage else None,
            run_config={
                "docCount": count,
                "threshold": threshold,
                "augment": augment,
                "modelId": model_id,
            },
        )
        _update_test_set(test_set_id, "COMPLETED", file_count=total or uploaded)
    except Exception as e:
        logger.exception("Synthesis job %s failed", job_id)
        _post_status(payload, job_id, "FAILED", str(e))
        _fail_test_set(test_set_id, append, test_set_bucket)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# Absolute wall-clock ceiling for a single generation run, defaulting to just
# under the AgentCore Runtime max session lifetime (8h). Legitimate runs — even
# large batches — finish well within this; the watchdog exists only to catch a
# wedged run (e.g. augraphy looping on an over-noisy render) so it fails cleanly
# instead of the session dying at the AgentCore limit and leaving the job stuck
# IN_PROGRESS forever. Tunable via env; not scaled by doc count (SEED generates
# documents concurrently, so wall-clock tracks the slowest doc, not the sum).
_GENERATION_TIMEOUT_S = int(os.environ.get("GENERATION_TIMEOUT_S", str(8 * 3600 - 300)))


@app.entrypoint
def invoke(payload, context=None):
    """AgentCore Runtime entrypoint.

    Kicks off generation on a background thread and returns immediately. The
    task is tracked so ``/ping`` reports ``HealthyBusy`` until it completes,
    keeping the runtime session alive for the full (up to multi-hour) run. A
    watchdog writes FAILED and releases the task only if generation exceeds the
    absolute ceiling (so a wedged run does not stay IN_PROGRESS forever).
    """
    job_id = payload.get("jobId")
    task_id = app.add_async_task("synthesis", {"jobId": job_id})
    timeout_s = _GENERATION_TIMEOUT_S

    def _worker():
        # Pulses while the job runs so a stage that completes no documents still
        # proves the runtime is alive. This is the only signal that survives the
        # container itself dying: the watchdog below runs *inside* the runtime, so a
        # container killed mid-stage leaves the job IN_PROGRESS with nothing left to
        # fail it — the UI then shows "busy" forever. Anything reconciling from
        # outside keys on heartbeatAt going stale.
        stop_heartbeat = threading.Event()
        _start_heartbeat(payload, job_id, stop_heartbeat)
        try:
            job_thread = threading.Thread(target=_run_job, args=(payload,), daemon=True)
            job_thread.start()
            job_thread.join(timeout_s)
            if job_thread.is_alive():
                logger.error(
                    "Synthesis job %s exceeded %ds; marking FAILED and abandoning "
                    "the wedged worker thread.",
                    job_id,
                    timeout_s,
                )
                _post_status(
                    payload,
                    job_id,
                    "FAILED",
                    f"Generation timed out after {timeout_s}s",
                )
        finally:
            stop_heartbeat.set()
            app.complete_async_task(task_id)

    threading.Thread(target=_worker, daemon=True).start()

    return {"accepted": True, "jobId": job_id, "testSetId": payload.get("testSetId")}


def _test_set_input_count(bucket, test_set_id):
    # True document count under the test set's input/ prefix, so appends report
    # the total (getTestSets reads fileCount from the record). Best-effort.
    try:
        s3 = boto3.client("s3")
        paginator = s3.get_paginator("list_objects_v2")
        count = 0
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{test_set_id}/input/"):
            count += sum(
                1 for o in page.get("Contents", []) if not o["Key"].endswith("/")
            )
        return count
    except Exception:  # noqa: BLE001 — best-effort
        logger.warning(
            "Failed to count test set inputs for %s", test_set_id, exc_info=True
        )
        return None


def _fail_test_set(test_set_id, append, bucket):
    if append:
        _update_test_set(
            test_set_id,
            "COMPLETED",
            file_count=_test_set_input_count(bucket, test_set_id),
        )
    else:
        _update_test_set(test_set_id, "FAILED")


def _update_test_set(test_set_id, status, file_count=None):
    # Flip the host test-set registration record (written QUEUED by the
    # processor) so it shows correctly in the Test Studio list. Best-effort.
    table_name = os.environ.get("HOST_TRACKING_TABLE")
    if not (table_name and test_set_id):
        return
    attrs = {"status": status}
    if file_count is not None:
        attrs["fileCount"] = file_count
    try:
        boto3.resource("dynamodb").Table(table_name).update_item(
            Key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
            UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in attrs),
            ExpressionAttributeNames={f"#{k}": k for k in attrs},
            ExpressionAttributeValues={f":{k}": v for k, v in attrs.items()},
        )
    except Exception:  # noqa: BLE001 — best-effort
        logger.warning("Failed to update test set %s", test_set_id, exc_info=True)


def _decimalize(value):
    """DynamoDB has no float type, so coerce numerics on the way in."""
    if isinstance(value, float):
        return Decimal(str(round(value, 4)))
    if isinstance(value, dict):
        return {k: _decimalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimalize(v) for v in value]
    return value


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _start_heartbeat(payload, job_id, stop_event, interval_s=None):
    """Pulse the job record while generation runs.

    Progress is reported per completed document, so a long stage that finishes none —
    augmentation on a batch, most obviously — looks identical to a dead runtime. The
    in-runtime watchdog cannot cover that case either: it dies with the container. A
    heartbeat is what lets something outside decide the run is gone.
    """
    if interval_s is None:
        interval_s = int(os.environ.get("HEARTBEAT_INTERVAL_S", "60"))

    started = time.monotonic()

    def _pulse():
        failures = 0
        table_name = os.environ.get("BOOTSTRAP_TRACKING_TABLE")
        if not (table_name and job_id):
            return
        table = boto3.resource("dynamodb").Table(table_name)
        while not stop_event.wait(interval_s):
            elapsed_min = int((time.monotonic() - started) / 60)
            try:
                # Only ever touches the heartbeat fields, and only while still running,
                # so it can never resurrect or overwrite a terminal status.
                #
                # elapsedMinutes rides along because a percentage alone is ambiguous:
                # "5% Starting generation" looked identical at one minute and at
                # sixty-eight, which is what made a dead runtime hard to spot.
                table.update_item(
                    Key={"jobId": job_id},
                    UpdateExpression=("SET heartbeatAt = :h, elapsedMinutes = :m"),
                    ConditionExpression="#s = :running",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":h": _now_iso(),
                        ":m": elapsed_min,
                        ":running": "IN_PROGRESS",
                    },
                )
                failures = 0
            except Exception:  # noqa: BLE001 — a heartbeat must never fail a run
                failures += 1
                # Escalate rather than staying at debug. Sustained heartbeat failures
                # are not cosmetic: past the reaper's window the job is failed, and
                # then the real COMPLETED is refused by the "not already FAILED"
                # condition on the status write — so a healthy run with generated
                # documents reports that its runtime died. Silent at debug, that is
                # undiagnosable.
                if failures in (3, 10) or failures % 30 == 0:
                    logger.warning(
                        "heartbeat write has failed %d consecutive time(s) for %s; "
                        "if this continues the job will be reaped as dead despite "
                        "the run being healthy",
                        failures,
                        job_id,
                        exc_info=True,
                    )
                else:
                    logger.debug("heartbeat write failed for %s", job_id, exc_info=True)

    thread = threading.Thread(target=_pulse, name="heartbeat", daemon=True)
    thread.start()
    return thread


def _post_status(payload, job_id, status, message, usage=None, run_config=None):
    # The processor invokes this runtime asynchronously and returns, so the
    # runtime writes terminal status to BootstrapTrackingTable itself. Best-effort.
    logger.info("synthesis job %s: %s — %s", job_id, status, message)
    table_name = os.environ.get("BOOTSTRAP_TRACKING_TABLE")
    if not (table_name and job_id):
        return
    # Every write carries a heartbeat. Without one, "job has not changed in an hour"
    # cannot distinguish a healthy-but-slow stage from a runtime that died: progress
    # only advances as documents COMPLETE, and augmentation can take an hour with no
    # document finishing. Reconciliation outside the runtime keys on this.
    attrs = {"status": status, "heartbeatAt": _now_iso()}
    if message:
        attrs["statusMessage"] = message
    if status == "FAILED" and message:
        attrs["errorMessage"] = message
    # Tokens, pipeline attempts and mean critic score, plus the inputs that drive
    # them. Recorded so a cost/duration estimate can be calibrated on observed
    # runs rather than constants; unrecoverable once the run ends.
    if usage is not None:
        attrs["usage"] = _decimalize(usage)
    if run_config is not None:
        attrs["runConfig"] = _decimalize(run_config)
    kwargs = {
        "Key": {"jobId": job_id},
        "UpdateExpression": "SET " + ", ".join(f"#{k} = :{k}" for k in attrs),
        "ExpressionAttributeNames": {f"#{k}": k for k in attrs},
        "ExpressionAttributeValues": {f":{k}": v for k, v in attrs.items()},
    }
    if status != "FAILED":
        kwargs["ConditionExpression"] = (
            "attribute_not_exists(#status) OR #status <> :failed"
        )
        kwargs["ExpressionAttributeNames"]["#status"] = "status"
        kwargs["ExpressionAttributeValues"][":failed"] = "FAILED"
    try:
        boto3.resource("dynamodb").Table(table_name).update_item(**kwargs)
    except boto3.client("dynamodb").exceptions.ConditionalCheckFailedException:
        logger.info("job %s already FAILED (timed out); not overwriting", job_id)
    except Exception:  # noqa: BLE001 — status is best-effort
        logger.warning("Failed to write job status for %s", job_id, exc_info=True)


if __name__ == "__main__":
    app.run()
