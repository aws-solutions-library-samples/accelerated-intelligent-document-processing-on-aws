# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Nested Step Functions Distributed Map runtime handler for sharded extraction.

A single Lambda handler with three modes (selected by ``event["mode"]``) that
drive the nested Distributed Map shard runtime. All three reuse the SAME library
primitives in ``idp_common.extraction`` (single source of truth) — this Lambda is
just a thin scheduler adapter, exactly like ``InProcessRuntime`` is for asyncio:

- ``mode == "plan"``  -> ``ExtractionService.plan_section_shards``: decides
  ``shard_mode`` and returns shard descriptors for the Map to iterate.
- ``mode == "shard"`` -> ``ExtractionService.run_one_section_shard``: runs ONE
  shard (one fresh 15-min Lambda per shard) and persists its result to S3
  idempotently — so SFN's native per-iteration retry re-runs only failed shards.
- ``mode == "merge"`` -> ``ExtractionService.merge_section_shards``: loads all
  shard results from S3, merges (page-ordered) + validates + saves the section.

Packaged in the SAME container image as ``index.handler`` (extraction function),
selected via ``ImageConfig.Command: ["sfn_runtime_handler.handler"]`` — no extra
Docker build. The standalone/notebook path never touches this; it uses
``InProcessRuntime`` inside ``process_document_section``.
"""

import logging
import os
import time

import boto3
from idp_common import extraction, get_config
from idp_common.docs_service import create_document_service
from idp_common.models import Document, Status
from idp_common.utils import calculate_lambda_metering, merge_metering_data

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_s3_client = None


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _load(event):
    working_bucket = os.environ.get("WORKING_BUCKET")
    full_document = Document.load_document(
        event.get("document", {}), working_bucket, logger
    )
    config = get_config(
        as_model=True,
        version=getattr(full_document, "config_version", None),
        revision=getattr(full_document, "config_revision", None),
    )
    return working_bucket, full_document, config


def _section_scoped(full_document, section_id):
    section = next(
        (s for s in full_document.sections if s.section_id == section_id), None
    )
    if not section:
        raise ValueError(f"Section {section_id} not found in document")
    section_index = next(
        i for i, s in enumerate(full_document.sections) if s.section_id == section_id
    )
    full_document.sections = [section]
    full_document.metering = {}
    full_document.pages = {
        pid: full_document.pages[pid]
        for pid in section.page_ids
        if pid in full_document.pages
    }
    return section, section_index


def _persistence(working_bucket, execution_arn):
    return extraction.S3ShardPersistence(
        bucket=working_bucket,
        execution_arn=execution_arn,
        s3_client=_get_s3_client(),
    )


def _cleanup_shards(working_bucket, execution_arn, section_id):
    try:
        safe_arn = execution_arn.replace(":", "_").replace("/", "_")
        prefix = f"checkpoints/{safe_arn}/{section_id}/shards/"
        s3 = _get_s3_client()
        resp = s3.list_objects_v2(Bucket=working_bucket, Prefix=prefix)
        keys = [{"Key": o["Key"]} for o in resp.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=working_bucket, Delete={"Objects": keys})
            logger.info("Deleted %d per-shard result(s)", len(keys))
    except Exception as e:
        logger.warning("Failed to clean up per-shard results: %s", e)


def handler(event, context):
    mode = event.get("mode", "plan")
    section_id = event["section_id"]
    execution_arn = event.get("execution_arn", "")
    logger.info("SFN runtime handler mode=%s section=%s", mode, section_id)
    working_bucket, full_document, config = _load(event)
    service = extraction.ExtractionService(config=config)

    # Absolute epoch deadline for the in-shard/merge confidence self-healing ladder
    # so a truncation-retry storm on a small-cap confidence model can't run this
    # Lambda into its 900s wall (the ladder stops with an
    # assessment_deadline_reached warning, keeping recovered rows). None in local
    # invocations without a real Lambda context.
    deadline_epoch = None
    try:
        deadline_epoch = time.time() + (context.get_remaining_time_in_millis() / 1000.0)
    except Exception:
        deadline_epoch = None

    if mode == "plan":
        plan = service.plan_section_shards(
            document=full_document, section_id=section_id
        )
        plan["section_id"] = section_id
        plan["execution_arn"] = execution_arn
        logger.info(
            "Shard plan: shard_mode=%s num_shards=%s",
            plan.get("shard_mode"),
            plan.get("num_shards"),
        )
        return plan

    if mode == "shard":
        _section_scoped(full_document, section_id)
        result = service.run_one_section_shard(
            document=full_document,
            section_id=section_id,
            shard_index=int(event["shard_index"]),
            persistence=_persistence(working_bucket, execution_arn),
            deadline_epoch=deadline_epoch,
        )
        return {
            "section_id": section_id,
            "shard_index": int(event["shard_index"]),
            "status": result.get("status"),
            "page_start": result.get("page_start"),
            "page_end": result.get("page_end"),
        }

    if mode == "merge":
        start_time = time.time()
        section, section_index = _section_scoped(full_document, section_id)
        section_document = service.merge_section_shards(
            document=full_document,
            section_id=section_id,
            persistence=_persistence(working_bucket, execution_arn),
            deadline_epoch=deadline_epoch,
        )
        if section_document.status == Status.FAILED:
            raise Exception(f"Merge failed for section {section_id}")
        _cleanup_shards(working_bucket, execution_arn, section_id)
        try:
            lambda_metering = calculate_lambda_metering(
                "Extraction", context, start_time
            )
            section_document.metering = merge_metering_data(
                section_document.metering, lambda_metering
            )
        except Exception as e:
            logger.warning("Failed to add Lambda metering for merge: %s", e)
        try:
            create_document_service().update_document_section(
                document_id=section_document.input_key,
                section_index=section_index,
                section=section_document.sections[0],
            )
        except Exception as e:
            logger.error("Failed to update section in DynamoDB: %s", e, exc_info=True)
        return {
            "section_id": section_id,
            "document": section_document.serialize_document(
                working_bucket, f"extraction_merge_{section_id}", logger
            ),
        }

    raise ValueError(f"Unknown sfn_runtime_handler mode: {mode}")
