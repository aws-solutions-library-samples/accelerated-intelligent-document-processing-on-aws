# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Base models and enums for IDP SDK."""

from enum import Enum


class StackState(str, Enum):
    """CloudFormation stack state."""

    CREATE_IN_PROGRESS = "CREATE_IN_PROGRESS"
    CREATE_COMPLETE = "CREATE_COMPLETE"
    CREATE_FAILED = "CREATE_FAILED"
    UPDATE_IN_PROGRESS = "UPDATE_IN_PROGRESS"
    UPDATE_COMPLETE = "UPDATE_COMPLETE"
    UPDATE_FAILED = "UPDATE_FAILED"
    DELETE_IN_PROGRESS = "DELETE_IN_PROGRESS"
    DELETE_COMPLETE = "DELETE_COMPLETE"
    DELETE_FAILED = "DELETE_FAILED"
    ROLLBACK_IN_PROGRESS = "ROLLBACK_IN_PROGRESS"
    ROLLBACK_COMPLETE = "ROLLBACK_COMPLETE"
    UPDATE_ROLLBACK_IN_PROGRESS = "UPDATE_ROLLBACK_IN_PROGRESS"
    UPDATE_ROLLBACK_COMPLETE = "UPDATE_ROLLBACK_COMPLETE"


class DocumentState(str, Enum):
    """Document processing state."""

    # MUST stay a superset of idp_common.models.Status: this enum validates the
    # ObjectStatus read straight out of the tracking table, so any runtime status
    # missing here makes `idp-cli status` / `run-inference --monitor` die with a
    # pydantic ValidationError ("Input should be 'QUEUED', ...") rather than
    # reporting progress. Four were missing, and two of them are on ordinary
    # paths: PREPROCESSING is set for EVERY document whenever a preprocessing
    # hook is registered (so every PII Anonymization user hit it), and
    # RULE_VALIDATION_POLICY_CLASSIFICATION for every rule-validation document.
    PENDING_UPLOAD = "PENDING_UPLOAD"
    QUEUED = "QUEUED"
    STARTED = "STARTED"
    RUNNING = "RUNNING"
    PREPROCESSING = "PREPROCESSING"
    OCR = "OCR"
    CLASSIFYING = "CLASSIFYING"
    EXTRACTING = "EXTRACTING"
    ASSESSING = "ASSESSING"
    RULE_VALIDATION_POLICY_CLASSIFICATION = "RULE_VALIDATION_POLICY_CLASSIFICATION"
    RULE_VALIDATION = "RULE_VALIDATION"
    RULE_VALIDATION_ORCHESTRATOR = "RULE_VALIDATION_ORCHESTRATOR"
    SUMMARIZING = "SUMMARIZING"
    HITL_IN_PROGRESS = "HITL_IN_PROGRESS"
    EVALUATING = "EVALUATING"
    POSTPROCESSING = "POSTPROCESSING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"
    # Terminal: a preprocessing hook replaced this original with a redacted copy.
    # Listed in the monitor's terminal-state sets too, or monitoring would wait
    # forever for a document that will never reach COMPLETED.
    REDACTED_SUPERSEDED = "REDACTED_SUPERSEDED"
    NOT_FOUND = "NOT_FOUND"
    UNKNOWN = "UNKNOWN"


class DocumentBucket(str, Enum):
    """The four progress buckets every `DocumentState` is reported under.

    `idp_sdk`'s progress monitor and `idp-cli`'s display layer both group
    documents into exactly these four, and they are the keys of the
    `status_data` dict `idp_cli/display.py` consumes.
    """

    COMPLETED = "completed"
    RUNNING = "running"
    QUEUED = "queued"
    FAILED = "failed"


#: Terminal and successful. `REDACTED_SUPERSEDED` belongs here rather than under
#: `FAILED`: a preprocessing hook deliberately replaced the original with a
#: redacted copy, which is the requested outcome and not a processing failure,
#: and it must count as done or a batch containing one never reaches 100%.
SUCCESS_DOCUMENT_STATES = frozenset(
    {
        DocumentState.COMPLETED,
        DocumentState.REDACTED_SUPERSEDED,
    }
)

#: Terminal and not successful. `NOT_FOUND` is here because a document id with no
#: row in the tracking table will never acquire one by waiting.
FAILED_DOCUMENT_STATES = frozenset(
    {
        DocumentState.FAILED,
        DocumentState.ABORTED,
        DocumentState.NOT_FOUND,
    }
)

#: Accepted, or not locatable, but not being worked on. `UNKNOWN` means the status
#: lookup itself did not produce an answer.
NOT_STARTED_DOCUMENT_STATES = frozenset(
    {
        DocumentState.QUEUED,
        DocumentState.PENDING_UPLOAD,
        DocumentState.UNKNOWN,
    }
)

#: Being worked on right now. Every one of these is a real stage the pipeline
#: writes to `ObjectStatus`, and each is named rather than reached by an `else`:
#: an `else` branch absorbs a newly added state into whichever bucket it happens
#: to fall into, and both halves of that mistake have shipped. `PREPROCESSING` is
#: set for *every* document whenever a preprocessing hook is registered, so on a
#: PII-anonymization stack it decides what the whole run's running count reads.
IN_FLIGHT_DOCUMENT_STATES = frozenset(
    {
        DocumentState.STARTED,
        DocumentState.RUNNING,
        DocumentState.PREPROCESSING,
        DocumentState.OCR,
        DocumentState.CLASSIFYING,
        DocumentState.EXTRACTING,
        DocumentState.ASSESSING,
        DocumentState.RULE_VALIDATION_POLICY_CLASSIFICATION,
        DocumentState.RULE_VALIDATION,
        DocumentState.RULE_VALIDATION_ORCHESTRATOR,
        DocumentState.SUMMARIZING,
        DocumentState.HITL_IN_PROGRESS,
        DocumentState.EVALUATING,
        DocumentState.POSTPROCESSING,
        DocumentState.IN_PROGRESS,
    }
)

#: A document in one of these will never change again, so a monitor can stop
#: polling it. Derived, so it cannot drift from the two sets it is the union of.
TERMINAL_DOCUMENT_STATES = SUCCESS_DOCUMENT_STATES | FAILED_DOCUMENT_STATES

_DOCUMENT_STATE_BUCKETS: "dict[DocumentBucket, frozenset[DocumentState]]" = {
    DocumentBucket.COMPLETED: SUCCESS_DOCUMENT_STATES,
    DocumentBucket.FAILED: FAILED_DOCUMENT_STATES,
    DocumentBucket.QUEUED: NOT_STARTED_DOCUMENT_STATES,
    DocumentBucket.RUNNING: IN_FLIGHT_DOCUMENT_STATES,
}


def document_state_partition_faults(
    states,
    buckets,
) -> "list[str]":
    """Describe every way `buckets` fails to partition `states`, as English lines.

    Separated from the check below so a test can drive it with a state set this
    module does not define — the only way to measure what happens to a *future*
    `DocumentState` member, which by construction cannot be enumerated here.

    Args:
        states: The members that must each land in exactly one bucket.
        buckets: Bucket name -> the members assigned to it.

    Returns:
        One line per fault, empty when `buckets` is a true partition of `states`.
    """
    faults: "list[str]" = []

    assigned: "set[object]" = set()
    for name, members in buckets.items():
        overlap = assigned & set(members)
        if overlap:
            faults.append(
                f"{sorted(str(m) for m in overlap)} are in more than one bucket "
                f"(seen again in {name})"
            )
        assigned |= set(members)

    unassigned = set(states) - assigned
    if unassigned:
        faults.append(
            f"{sorted(str(m) for m in unassigned)} are in no bucket, so they would "
            f"be reported under whichever bucket a fallback happens to choose"
        )

    unknown = assigned - set(states)
    if unknown:
        faults.append(
            f"{sorted(str(m) for m in unknown)} are bucketed but are not members, "
            f"so the bucket names something that no longer exists"
        )

    for name, members in buckets.items():
        if not members:
            faults.append(f"{name} is empty")

    return faults


# Checked at import, and deliberately NOT with `assert` -- `assert` is stripped
# under `python -O` / PYTHONOPTIMIZE=1, which is exactly the configuration where
# a silent misbucketing would be least noticed. The comparison is between two
# literals in this one module, so it cannot fail for any reason other than an
# inconsistent edit to this file: whoever adds a `DocumentState` member without
# deciding which bucket it belongs in gets a message naming the member instead of
# a progress display that quietly reports it as queued forever.
_PARTITION_FAULTS = document_state_partition_faults(
    set(DocumentState), _DOCUMENT_STATE_BUCKETS
)
if _PARTITION_FAULTS:  # pragma: no cover - an inconsistent edit to this module
    raise RuntimeError(
        "DocumentState is not partitioned by the bucket sets in "
        "idp_sdk/models/base.py: " + "; ".join(_PARTITION_FAULTS)
    )


def classify_document_state(status) -> DocumentBucket:
    """Return the progress bucket a document status is reported under.

    Total over `DocumentState` by construction: the lookup is against a mapping
    whose union the import-time check above proves equal to the enum, so there is
    no fallback for a member and no member can be absorbed silently.

    Args:
        status: A `DocumentState`, its string value, or a status string read
            straight out of the tracking table.

    Returns:
        The `DocumentBucket` to report the document under.
    """
    try:
        member = DocumentState(status or DocumentState.UNKNOWN)
    except ValueError:
        # A string this enum does not define. It cannot arrive through
        # `DocumentStatus`, whose `status` field pydantic validates against
        # `DocumentState`; it can arrive from a raw tracking-table read on a
        # stack running a newer pipeline than this SDK. Report it as in flight:
        # the set of *terminal* statuses is small and closed, so an unrecognised
        # one is overwhelmingly a mid-pipeline stage, and calling it queued is
        # the mistake that made ABORTED read as "IN PROGRESS" forever.
        return DocumentBucket.RUNNING

    for bucket, members in _DOCUMENT_STATE_BUCKETS.items():
        if member in members:
            return bucket

    # Unreachable while the import-time check above holds, and raising rather
    # than guessing is the point: a guess here is the defect this function
    # exists to remove.
    raise RuntimeError(  # pragma: no cover - the import-time check prevents this
        f"DocumentState.{member.name} is in no progress bucket"
    )


class Pattern(str, Enum):
    """IDP processing patterns."""

    PATTERN_1 = "pattern-1"
    PATTERN_2 = "pattern-2"


class RerunStep(str, Enum):
    """Pipeline steps for rerun operations."""

    CLASSIFICATION = "classification"
    EXTRACTION = "extraction"
