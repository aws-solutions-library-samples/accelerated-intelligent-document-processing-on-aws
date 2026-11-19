# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Config-bootstrap SQS processor.

Consumes bootstrap jobs, authors/resolves a document-class schema (cheap, in
this Lambda), creates a config version, then — when document generation is
requested and available — stages the schema_dir to the working bucket and
invokes the Synthesis AgentCore Runtime to generate a labeled test set. Status
is recorded in the feature-owned BootstrapTrackingTable (read via the FeatureApi).
"""

import json
import logging
import os
import shutil
import tempfile
import uuid
from decimal import Decimal

import boto3

from idp_common.synthesis import bootstrap as bootstrap_mod
from idp_common.synthesis import engine, schema_bridge

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

WORKING_BUCKET = os.environ.get("WORKING_BUCKET")
TEST_SET_BUCKET = os.environ.get("TEST_SET_BUCKET")
SYNTHESIS_RUNTIME_ARN = os.environ.get("SYNTHESIS_RUNTIME_ARN")
CONFIGURATION_TABLE_NAME = os.environ.get("CONFIGURATION_TABLE_NAME")
BOOTSTRAP_TRACKING_TABLE = os.environ.get("BOOTSTRAP_TRACKING_TABLE")
HOST_TRACKING_TABLE = os.environ.get("HOST_TRACKING_TABLE")
#: The host record attribute naming the job a new set was claimed for. Written by
#: the feature API's ``_reserve_test_set``; the two must stay equal.
CLAIM_ATTRIBUTE = "generatorJobId"

_ddb = boto3.resource("dynamodb")


def _class_id(class_dict: dict) -> str:
    return (
        class_dict.get("x-aws-idp-document-type")
        or class_dict.get("$id")
        or class_dict.get("title")
        or ""
    )


def _decimalize(value):
    """DynamoDB has no float type, so coerce numerics on the way in."""
    if isinstance(value, float):
        return Decimal(str(round(value, 4)))
    if isinstance(value, dict):
        return {k: _decimalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimalize(v) for v in value]
    return value


def _status(
    job_id,
    status,
    message=None,
    error=None,
    config_version=None,
    test_set_id=None,
    usage=None,
    run_config=None,
):
    """Record job status in the feature's own tracking table.

    The AppSync transport was removed from the host, so per-job status lives in
    this feature-owned table (BootstrapTrackingTable) and is read back through
    the FeatureApi (GET /jobs/{id}) — the host has no status channel to post to.
    """
    if not (BOOTSTRAP_TRACKING_TABLE and job_id):
        return
    attrs = {"status": status}
    # Seed the heartbeat here, not only from the runtime. The processor writes
    # IN_PROGRESS and then invokes the runtime, so everything between those two
    # points — an image pull that fails, an OOM during import — leaves a job that the
    # reaper skips, because "no heartbeat" is deliberately treated as "predates this
    # release" rather than "dead". Seeding it means a job that never gets a container
    # is still reapable, and keeps the absent case meaning only what it says.
    if status == "IN_PROGRESS":
        import datetime as _dt

        attrs["heartbeatAt"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    if message is not None:
        attrs["statusMessage"] = message
    if error is not None:
        attrs["errorMessage"] = error
    if config_version is not None:
        attrs["configVersion"] = config_version
    if test_set_id is not None:
        attrs["testSetId"] = test_set_id
    # Tokens, pipeline attempts and mean critic score for the run. Recorded so a
    # cost or duration estimate can be calibrated on observed runs instead of
    # constants; none of it is recoverable once the run ends.
    if usage is not None:
        attrs["usage"] = _decimalize(usage)
    # The inputs that drive cost — threshold gates critic acceptance, so it
    # determines how many render passes a document takes, and augmentation adds
    # local processing time. Without them a completed run is an uninterpretable
    # data point.
    if run_config is not None:
        attrs["runConfig"] = _decimalize(run_config)
    expr = "SET " + ", ".join(f"#{k} = :{k}" for k in attrs)
    try:
        _ddb.Table(BOOTSTRAP_TRACKING_TABLE).update_item(
            Key={"jobId": job_id},
            UpdateExpression=expr,
            ExpressionAttributeNames={f"#{k}": k for k in attrs},
            ExpressionAttributeValues={f":{k}": v for k, v in attrs.items()},
        )
    except Exception as exc:  # noqa: BLE001 — status is best-effort
        logger.warning("Could not write job status for %s: %s", job_id, exc)


def _register_test_set(
    test_set_id, status, test_set_name=None, document_class_type=None, claim_job=None
):
    """Register/refresh the host test-set record without clobbering an existing
    set (getTestSets reads ItemType='testset' records).

    Uses update_item and if_not_exists for name/createdAt/description so appending
    to an existing test set preserves its metadata; only status/id (and, for a
    new set, the defaults) are written by the generator.

    ``claim_job`` is set for a new set the feature API claimed for this job: the
    write is then conditional on the record still carrying that job's claim, in
    GENERATING, and False is returned when it does not. A redelivered job must not take over a set
    created under the same id after its own record was deleted. True otherwise,
    including when the best-effort write fails for another reason.
    """
    if not (HOST_TRACKING_TABLE and test_set_id):
        return True
    import datetime

    now = (
        datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    )
    names = {
        "#status": "status",
        # When the status was last written, so the host can tell a long-running
        # generation from an abandoned one. GENERATING is cleared by the runtime; if
        # the runtime dies — or the extension is uninstalled — nothing else ever
        # would, and the set shows a spinner in Test Studio permanently.
        "#statusUpdatedAt": "statusUpdatedAt",
        "#itemType": "ItemType",
        "#id": "id",
        "#name": "name",
        "#createdAt": "createdAt",
        "#iet": "InitialEventTime",
        "#description": "description",
    }
    values = {
        ":status": status,
        ":itemType": "testset",
        ":id": test_set_id,
        ":name": test_set_name or test_set_id,
        ":createdAt": now,
        ":description": "Generated by Test Set Generator",
    }
    set_clauses = [
        "#status = :status",
        "#statusUpdatedAt = :createdAt",
        "#itemType = :itemType",
        "#id = :id",
        "#name = if_not_exists(#name, :name)",
        "#createdAt = if_not_exists(#createdAt, :createdAt)",
        "#iet = if_not_exists(#iet, :createdAt)",
        "#description = if_not_exists(#description, :description)",
    ]
    if document_class_type:
        names["#dct"] = "documentClassType"
        values[":dct"] = document_class_type
        set_clauses.append("#dct = if_not_exists(#dct, :dct)")
    condition = {}
    if claim_job:
        names["#claim"] = CLAIM_ATTRIBUTE
        values[":claim"] = claim_job
        values[":generating"] = "GENERATING"
        # GENERATING too: a duplicate delivery after the set completed, or a late
        # retry after the host reaper failed the claim, must not generate again.
        condition["ConditionExpression"] = "#claim = :claim AND #status = :generating"
    try:
        _ddb.Table(HOST_TRACKING_TABLE).update_item(
            Key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
            UpdateExpression="SET " + ", ".join(set_clauses),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            **condition,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if claim_job and code == "ConditionalCheckFailedException":
            return False
        logger.warning("Could not register test set %s: %s", test_set_id, exc)
    return True


def _release_reserved_test_set(body, error):
    """Fail the host record the feature API created for a new set, if still GENERATING.

    The API claims a new set's id by writing its record before it enqueues the job
    (``_reserve_test_set``), so a job that ends without starting the generator would
    otherwise leave that record GENERATING, with no documents, until the host's
    reaper ages it out hours later — and until then the name can be neither reused
    nor deleted. Only for a set claimed for this job (``claimed``, not ``append``):
    an existing set's record is not this job's to fail. Conditional on GENERATING
    and on the record still carrying this job's claim, so a status anything else
    wrote since, or a set created under the id since, is kept. Called only for an
    exit SQS does not retry; see ``handler``. Best-effort, like the other host
    writes here.
    """
    test_set_id = body.get("testSetId")
    job_id = body.get("jobId")
    if body.get("append") or not body.get("claimed"):
        return
    if not (HOST_TRACKING_TABLE and test_set_id and job_id):
        return
    import datetime

    now = (
        datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    )
    try:
        _ddb.Table(HOST_TRACKING_TABLE).update_item(
            Key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
            UpdateExpression="SET #s = :failed, #e = :error, #u = :now",
            ConditionExpression="#s = :generating AND #claim = :claim",
            ExpressionAttributeNames={
                "#s": "status",
                "#e": "error",
                "#u": "statusUpdatedAt",
                "#claim": CLAIM_ATTRIBUTE,
            },
            ExpressionAttributeValues={
                ":failed": "FAILED",
                ":generating": "GENERATING",
                ":error": error,
                ":now": now,
                ":claim": job_id,
            },
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("Could not release test set %s: %s", test_set_id, exc)


_NOT_GENERATED = (
    "The generation job ended before any documents were generated; its status in "
    "Test Set Generator says why."
)


def handler(event, context):
    logger.info("Received event: %s", json.dumps(event))
    batch_item_failures = []

    for record in event.get("Records", []):
        job_id = None
        try:
            body = json.loads(record["body"])
            job_id = body.get("jobId")
            invoked = _process_job(job_id, body)
        except Exception as e:
            logger.error("Error processing bootstrap job: %s", e, exc_info=True)
            batch_item_failures.append({"itemIdentifier": record["messageId"]})
            if job_id:
                _status(job_id, "FAILED", error=str(e))
            # Not released: SQS redelivers this message, and the claim is what the
            # retry generates into. A job that never succeeds leaves its claim
            # GENERATING until the host reaper ages it out.
            continue
        if not invoked:
            # Finished without generating and not retried, so the claim is unused.
            _release_reserved_test_set(body, _NOT_GENERATED)

    return {"batchItemFailures": batch_item_failures}


def _process_job(job_id, body):
    """Run one job. True when the generator runtime was invoked, else False."""
    # The processor reads/writes config versions in the HOST's Configuration
    # DynamoDB table (gzip-compressed Binary storage format). That storage
    # contract lives in idp_common.config.ConfigurationManager, which this image
    # installs via idp_common[synthesis] (see agent-source/requirements.txt) — so
    # the extension shares the host's exact read/write format rather than
    # vendoring or round-tripping through a host mutation.
    from idp_common.config.configuration_manager import ConfigurationManager

    _status(job_id, "IN_PROGRESS", message="Authoring schema")

    config_manager = ConfigurationManager()

    request = bootstrap_mod.BootstrapRequest(
        prompt=body["prompt"],
        class_name=body.get("className"),
        field_hints=body.get("fieldHints", []),
        config_version=body.get("configVersion"),
        target_version=body.get("targetVersion"),
        doc_count=int(body.get("docCount", 3)),
        quality_threshold=int(body.get("threshold", 7)),
        augment=bool(body.get("augment", False)),
        model_id=body.get("modelId") or os.environ.get("GENERATOR_MODEL_ID"),
        example_doc_keys=body.get("exampleDocKeys", []),
        scenario=body.get("scenario") or "",
    )

    preauthored = body.get("preauthoredSchema")
    from_existing = body.get("fromExistingConfig")
    if preauthored:
        schema, tier = preauthored, "preauthored"
    elif from_existing and request.config_version and request.class_name:
        raw = config_manager.get_raw_configuration("Config", request.config_version)
        classes = (raw or {}).get("classes", [])
        target = next((c for c in classes if _class_id(c) == request.class_name), None)
        if target is None:
            _status(
                job_id,
                "FAILED",
                error=f"Class '{request.class_name}' not found in version "
                f"'{request.config_version}'",
            )
            return False
        schema = schema_bridge.config_class_to_generator_schema(target)
        tier = "existing-config"
    else:
        config_classes = []
        if request.config_version:
            raw = config_manager.get_raw_configuration("Config", request.config_version)
            if raw:
                config_classes = list(raw.get("classes", []))

        schema, tier, matched = bootstrap_mod.resolve_schema(
            request,
            config_classes=config_classes,
            status_cb=lambda pct, msg: _status(job_id, "IN_PROGRESS", message=msg),
        )
        if schema is None:
            _status(
                job_id,
                "FAILED",
                error=(
                    "Could not author a valid schema from the description after "
                    "retries (see processor logs for the model error). Try a more "
                    "specific description or generate from an existing "
                    f"configuration. (tier={tier})"
                ),
            )
            return False

    target_version = request.target_version or bootstrap_mod._default_version_name(
        schema
    )
    bootstrap_mod.merge_class_into_version(
        schema, target_version, config_manager=config_manager
    )
    _status(
        job_id,
        "IN_PROGRESS",
        message=f"Config version '{target_version}' created (tier={tier})",
        config_version=target_version,
    )

    want_generation = request.doc_count > 0 and body.get("generateDocs", True)

    if not want_generation:
        _status(
            job_id,
            "COMPLETED",
            message="Config version created (no generation requested)",
            config_version=target_version,
        )
        return False

    if not SYNTHESIS_RUNTIME_ARN:
        _status(
            job_id,
            "COMPLETED",
            message=(
                "Config version created. Document generation unavailable; "
                "upload example documents to build a test set. " + engine.INSTALL_HINT
            ),
            config_version=target_version,
        )
        return False

    # The test set is a distinct identity from the config version: the config
    # version records the class schema, the test set holds the generated docs.
    # Fall back to target_version for old-shape messages that predate the split.
    test_set_id = body.get("testSetId") or target_version
    test_set_name = body.get("testSetName") or test_set_id
    append = bool(body.get("append", False))

    schema_prefix = f"bootstrap/{job_id}/schema/"
    _stage_schema_dir(schema, schema_prefix)
    claim_job = job_id if body.get("claimed") and not append else None
    if not _register_test_set(
        test_set_id, "GENERATING", test_set_name=test_set_name, claim_job=claim_job
    ):
        _status(
            job_id,
            "FAILED",
            error=(
                f"Test set '{test_set_id}' was deleted, replaced or already finished "
                "before generation started; nothing was generated into it."
            ),
        )
        return False
    _status(
        job_id,
        "IN_PROGRESS",
        message="Invoking generator",
        config_version=target_version,
        test_set_id=test_set_id,
    )

    payload = {
        "jobId": job_id,
        "testSetId": test_set_id,
        "append": append,
        "workingBucket": WORKING_BUCKET,
        "schemaPrefix": schema_prefix,
        "testSetBucket": TEST_SET_BUCKET,
        "count": request.doc_count,
        "threshold": request.quality_threshold,
        "augment": request.augment,
        "extra": request.scenario or request.prompt,
        "modelId": request.model_id,
        "allowedFieldNames": schema_bridge.field_names(schema),
    }
    # AgentCore Runtime sessions must be 33-256 chars.
    session_id = f"bootstrap-{job_id}-{uuid.uuid4().hex}"
    boto3.client("bedrock-agentcore").invoke_agent_runtime(
        agentRuntimeArn=SYNTHESIS_RUNTIME_ARN,
        runtimeSessionId=session_id,
        contentType="application/json",
        payload=json.dumps(payload).encode("utf-8"),
    )
    return True


def _stage_schema_dir(schema, prefix):
    work_dir = tempfile.mkdtemp(prefix="bootstrap-schema-")
    try:
        bootstrap_mod._write_schema_dir(schema, work_dir)
        s3 = boto3.client("s3")
        for fname in os.listdir(work_dir):
            fpath = os.path.join(work_dir, fname)
            if os.path.isfile(fpath):
                s3.upload_file(fpath, WORKING_BUCKET, f"{prefix}{fname}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
