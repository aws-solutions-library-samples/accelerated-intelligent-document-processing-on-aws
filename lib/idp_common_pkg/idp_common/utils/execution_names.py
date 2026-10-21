# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Idempotent Step Functions execution names.

Shared by ``queue_processor`` (which names every execution after its SQS message,
or after a ``ReviewFinalizeToken``) and ``idp_common.review_queues`` (which must
predict the name of a finalize execution).
"""

import hashlib
import re
from typing import Optional

# Step Functions execution names: 1-80 chars, no whitespace, brackets, wildcards
# or the characters " # % \ ^ | ~ ` $ & , ; : /. The name also becomes the last
# ARN segment, which document_versions.build_run_id and the classification cache
# key both take with ``split(":")[-1]``, so it is kept to a set that is safe in
# an S3 key and a DynamoDB sort key as well.
_EXECUTION_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_EXECUTION_NAME_MAX_LEN = 80


def execution_name_for(input_key: str, message_id: str) -> Optional[str]:
    """Derive the idempotent execution name for one SQS message.

    SQS is at-least-once: a message whose StartExecution succeeded is redelivered
    whenever the invocation that handled it dies before it can report the batch
    outcome (issue #904 saw one message delivered 24 times and started 6 times).
    Step Functions refuses a second StartExecution with the same name for 90
    days, so naming the execution after the message turns every redelivery into
    a harmless ``ExecutionAlreadyExists``.

    The message id, not the document, is the key: the same object re-uploaded,
    reprocessed from the UI or re-run through the SDK is a NEW message and must
    get a new execution, while every redelivery of one message carries the same
    id. A human-readable prefix from the object's basename is kept so the
    Step Functions console still reads like a document list.

    Returns None when there is no message id (a direct invocation), in which case
    Step Functions assigns a name and duplicate protection does not apply.
    """
    if not message_id:
        return None
    token = message_id
    if _EXECUTION_NAME_UNSAFE.search(token) or len(token) > 64:
        token = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]
    basename = (input_key or "").rsplit("/", 1)[-1]
    prefix = _EXECUTION_NAME_UNSAFE.sub("-", basename).strip("-.")
    room = _EXECUTION_NAME_MAX_LEN - len(token) - 1
    prefix = prefix[:room].rstrip("-.")
    # Preserve the "looks like a document list" property in the Step
    # Functions console even for basenames that yield no allowed chars
    # (e.g. all-emoji filenames, extension-only basenames like ``.pdf``).
    # A bare token — 32-char sha1 or 36-char UUID — is uninformative and
    # loses the browse-by-name affordance the prefix exists for. Fall
    # back to a generic ``doc-`` prefix so the execution row still reads
    # as "some document" rather than "some hash".
    return f"{prefix}-{token}" if prefix else f"doc-{token}"


def execution_arn_for(state_machine_arn: str, execution_name: str) -> str:
    """The ARN Step Functions gives an execution of ``state_machine_arn`` by name."""
    return (
        state_machine_arn.replace(":stateMachine:", ":execution:", 1)
        + f":{execution_name}"
    )
