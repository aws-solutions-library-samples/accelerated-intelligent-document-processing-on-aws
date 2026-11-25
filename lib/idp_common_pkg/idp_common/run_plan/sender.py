# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The one sender of the document queue.

Every message the product puts on the document queue goes through ``send_run``,
with a run plan. The queue processor reads the plan from the ``RunPlan`` message
attribute and writes it into the execution input.

The caller clears what the run will produce again before it sends
(``idp_common.run_plan.clearing``); this module only builds and sends the
message.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Mapping, Optional, Tuple

from .models import RUN_PLAN_ATTRIBUTE, RunPlan, RunPlanError, encode
from .steps import RESUME

logger = logging.getLogger(__name__)

__all__ = [
    "EVENT_DOCUMENT_QUEUED",
    "EVENT_DOCUMENT_REPROCESSED",
    "EVENT_DOCUMENT_RUN_REQUESTED",
    "EVENT_REVIEW_FINALIZE",
    "EVENT_REVIEW_SKIP_RESUBMIT",
    "EVENT_TYPES",
    "EVENT_TYPE_ATTRIBUTE",
    "OBJECT_KEY_ATTRIBUTE",
    "build_run_message",
    "send_run",
]

#: A new upload, or a document the malware gate admitted.
EVENT_DOCUMENT_QUEUED = "DocumentQueued"
#: A document a person asked to run again.
EVENT_DOCUMENT_REPROCESSED = "DocumentReprocessed"
#: The run that follows a review cycle.
EVENT_REVIEW_FINALIZE = "ReviewFinalize"
#: The run that follows a review skipped outside the review queues.
EVENT_REVIEW_SKIP_RESUBMIT = "ReviewSkipResubmit"
#: Any other run: the senders that named no event type before run plans.
EVENT_DOCUMENT_RUN_REQUESTED = "DocumentRunRequested"

#: The ``EventType`` values a message on the document queue carries.
EVENT_TYPES = (
    EVENT_DOCUMENT_QUEUED,
    EVENT_DOCUMENT_REPROCESSED,
    EVENT_REVIEW_FINALIZE,
    EVENT_REVIEW_SKIP_RESUBMIT,
    EVENT_DOCUMENT_RUN_REQUESTED,
)

EVENT_TYPE_ATTRIBUTE = "EventType"
OBJECT_KEY_ATTRIBUTE = "ObjectKey"

_RESERVED_ATTRIBUTES = frozenset(
    {EVENT_TYPE_ATTRIBUTE, OBJECT_KEY_ATTRIBUTE, RUN_PLAN_ATTRIBUTE}
)


def _string(value: str) -> Dict[str, str]:
    return {"DataType": "String", "StringValue": value}


def build_run_message(
    *,
    document: Any,
    plan: RunPlan,
    event_type: str,
    working_bucket: Optional[str] = None,
    prefix: str = "run",
    extra_attributes: Optional[Mapping[str, str]] = None,
) -> Tuple[str, Dict[str, Dict[str, str]]]:
    """The body and the attributes of the message ``send_run`` sends.

    The plan is encoded, and every argument checked, before the document is
    stored. With a ``working_bucket`` this stores the document there.

    Raises:
        RunPlanError, ValueError: as ``send_run``.
    """
    encoded = encode(plan)
    if plan.start == RESUME:
        raise RunPlanError(
            "A plan that is sent names the step the run starts at; resolve "
            "'resume' against the document first"
        )
    if event_type not in EVENT_TYPES:
        raise ValueError(
            f"Unknown event type '{event_type}'; use one of {', '.join(EVENT_TYPES)}"
        )
    extras = dict(extra_attributes or {})
    reserved = sorted(_RESERVED_ATTRIBUTES.intersection(extras))
    if reserved:
        raise ValueError(
            f"{', '.join(reserved)} is set by send_run and cannot be an extra attribute"
        )

    attributes = {
        EVENT_TYPE_ATTRIBUTE: _string(event_type),
        OBJECT_KEY_ATTRIBUTE: _string(document.input_key),
        RUN_PLAN_ATTRIBUTE: _string(encoded),
    }
    for name, value in extras.items():
        attributes[name] = _string(value)

    if working_bucket:
        body = json.dumps(
            document.serialize_document(working_bucket, prefix), default=str
        )
    else:
        body = document.to_json()
    return body, attributes


def send_run(
    sqs_client: Any,
    queue_url: str,
    *,
    document: Any,
    plan: RunPlan,
    event_type: str,
    working_bucket: Optional[str] = None,
    prefix: str = "run",
    extra_attributes: Optional[Mapping[str, str]] = None,
) -> str:
    """Queue a document for a run, and return the message id.

    The message body is the document: stored in ``working_bucket`` with a small
    reference in the message when a bucket is given (``prefix`` names the
    stored object), and the document's JSON otherwise. The message attributes
    are ``EventType``, ``ObjectKey``, ``RunPlan`` and ``extra_attributes``
    (String attributes, such as a de-duplication token).

    The plan is encoded, and every argument checked, before anything is stored
    or sent.

    Raises:
        RunPlanError: the encoded plan is larger than ``MAX_RUN_PLAN_BYTES``, or
            the plan's start is ``resume``, which is resolved before a plan is
            sent.
        ValueError: ``event_type`` is not in ``EVENT_TYPES``, or an extra
            attribute uses a name this function sets.
    """
    body, attributes = build_run_message(
        document=document,
        plan=plan,
        event_type=event_type,
        working_bucket=working_bucket,
        prefix=prefix,
        extra_attributes=extra_attributes,
    )
    response = sqs_client.send_message(
        QueueUrl=queue_url, MessageBody=body, MessageAttributes=attributes
    )
    message_id = str(response.get("MessageId", ""))
    logger.info(
        "Queued %s: kind=%s start=%s event=%s message=%s",
        document.input_key,
        plan.kind.value,
        plan.start,
        event_type,
        message_id,
    )
    return message_id
