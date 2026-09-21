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
from idp_common.extraction.failure import persist_section_after_extraction_failure
from idp_common.models import Document, Status
from idp_common.utils import calculate_lambda_metering, merge_metering_data
from idp_common.utils.bedrock_utils import set_lambda_deadline_epoch
from idp_common.utils.transient_errors import raise_if_transient

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


def handler(event, context):
    """Plan / run one shard / merge — see ``_handle``.

    #787: ShardExtractionStep used to retry ``States.TaskFailed`` — EVERY function
    error, eight times at 2x backoff, including deterministic ones that fail the
    same way on attempt 8. It now retries the transient names only, so a transient
    failure that surfaces as a plain Python exception has to be re-raised under the
    one name the state lists. Hard errors keep their own name and are not retried;
    completed shards are still preserved by ``S3ShardPersistence`` for the retries
    that do happen.
    """
    try:
        return _handle(event, context)
    except Exception as e:
        raise_if_transient(
            e,
            where=f"shard runtime {event.get('mode', 'plan')} section {event.get('section_id')}",
        )
        raise


def _handle(event, context):
    mode = event.get("mode", "plan")
    section_id = event["section_id"]
    execution_arn = event.get("execution_arn", "")
    logger.info("SFN runtime handler mode=%s section=%s", mode, section_id)
    working_bucket, full_document, config = _load(event)
    service = extraction.ExtractionService(config=config)

    # Absolute epoch deadline for the in-shard/merge confidence self-healing ladder
    # so a truncation-retry storm on a small-cap confidence model can't run this
    # Lambda into its 900s wall. The ladder keeps every row it already recovered and
    # reports assessment_incomplete naming the time budget if any row is still
    # unscored; assessment_deadline_reached (warning) only when coverage completed
    # anyway. None in local invocations without a real Lambda context.
    deadline_epoch = None
    try:
        deadline_epoch = time.time() + (context.get_remaining_time_in_millis() / 1000.0)
    except Exception:
        deadline_epoch = None
    # Publish it for the Bedrock retry decorators too. This is the shard-per-Lambda
    # runtime, so it is the handler where a long agent backoff most directly wastes
    # an invocation — and it was the one that computed the deadline without sharing it.
    set_lambda_deadline_epoch(deadline_epoch)

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
        # #1049: the merge shares _save_results with the in-process path, so it
        # shares its raising failures too — including ExtractionOutputIncomplete,
        # whose whole point is that the diagnosis is durable before the raise.
        # The section write below sat after this call, so a failed merge left the
        # section's DynamoDB record untouched and the Sections panel blank.
        # merge_section_shards mutates `full_document` in place and returns it, so
        # `section` here is the object the service recorded onto.
        #
        # THE PER-SHARD RESULTS ARE NEVER DELETED FROM THIS HANDLER, on success or
        # on failure. `merge_section_shards` RE-LOADS every shard from S3 on entry
        # and raises if any is absent, so for this state the shards are a
        # PRECONDITION rather than an optimisation — and ExtractionMergeStep retries
        # the transient families (TransientError, Lambda.ServiceException,
        # throttling), so a retry can follow a merge that already SUCCEEDED: the
        # tail of this branch serialises the document, which always writes to S3
        # (size_threshold_kb defaults to 0) and is not wrapped, so a SlowDown or a
        # read timeout there surfaces as TransientError. Deleting the shards
        # anywhere in here would therefore risk converting a fully paid-for,
        # successful extraction into a permanent "shard(s) have no persisted result"
        # failure on the next attempt.
        #
        # There is no safe point to delete them at, because whether the STATE
        # succeeded is not observable from inside this Lambda and that is what
        # decides whether a retry follows. So they are left to the working bucket's
        # lifecycle rule, which expires every object on the stack's retention
        # schedule. Nothing else reads them, and a reprocess from the UI starts a new
        # execution and therefore a new key prefix, so "start clean" needs no
        # deletion either. patterns/unified/tests/test_shard_retention.py pins this.
        try:
            section_document = service.merge_section_shards(
                document=full_document,
                section_id=section_id,
                persistence=_persistence(working_bucket, execution_arn),
                deadline_epoch=deadline_epoch,
            )
            if section_document.status == Status.FAILED:
                raise Exception(f"Merge failed for section {section_id}")
        except Exception as error:
            persist_section_after_extraction_failure(
                document_service=create_document_service(),
                document=full_document,
                section_id=section_id,
                section_index=section_index,
                error=error,
            )
            raise
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
