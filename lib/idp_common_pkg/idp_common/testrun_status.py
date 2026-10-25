# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Derivation of a Test Studio run's status from the documents it holds.

This module is the **single** home for two things that used to be spelled out
inside the read resolver: how a document's ``EvaluationStatus`` is classified,
and how a run's overall status follows from the resulting tally.

⚠️ **Why it is here rather than in the resolver.** The classification had two
copies — one in the resolver and one in the abort path — and a value present in
neither pinned runs at ``EVALUATING`` forever. That happened twice in one
release cycle (``TIMED_OUT``, then ``DISABLED``; see
`#1330 <https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1330>`_),
and the remedy adopted then was a closure test over the copies. A closure test
keeps the copies honest; it does not stop a third consumer being written against
its own copy. The derivation now has one definition and every consumer calls it.

The functions here are **pure**: they take already-fetched DynamoDB items and
return verdicts. Fetching stays with the caller, because the callers legitimately
differ — the resolver reads one document at a time on a user's request, and the
sweeper batches. Keeping I/O out is also what makes the derivation testable
without mocking DynamoDB at all.
"""

import gzip
import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)


# Statuses that mean "processing is over" AND that aggregate metrics are
# expected for. ABORTED is terminal too but is deliberately absent: an aborted
# run is not eligible for aggregation, so it must keep reporting ABORTED rather
# than being masked as EVALUATING.
METRICS_ELIGIBLE_STATUSES = frozenset({"COMPLETE", "PARTIAL_COMPLETE"})


# A document's ``EvaluationStatus``, partitioned into the three answers the run
# status needs. Every value ANY writer can put in that attribute must appear in
# exactly one of these, because a value in none of them is counted as in-flight
# and a run holding one such document never reaches a terminal status.
#
# ⚠️ The attribute has THREE writers, not one, and the two beyond the pipeline
# are easy to miss because they describe a different activity: promoting a
# document to an evaluation baseline writes ``BASELINE_COPYING`` /
# ``BASELINE_AVAILABLE`` / ``BASELINE_ERROR`` over whatever evaluation left
# there (``copy_to_baseline_resolver``, and ``idp_sdk``'s evaluation processor
# for the programmatic route).
#
# Terminal and not a failure: the document is as finished as it will ever be.
# ``NO_BASELINE`` and ``DISABLED`` produce no metrics by construction (no ground
# truth to score against; no evaluation asked for), which is a legitimate
# outcome rather than an error.
#
# ⚠️ **All three ``BASELINE_*`` values are settled, and the honest reason is that
# the run can no longer be told anything by them — not that they are good news.**
# A promotion *overwrites* this attribute, so whatever evaluation recorded is
# gone, and two things follow that are worth knowing before reading a run's
# figures. **A promoted document whose evaluation had FAILED stops counting as a
# failed file**, so a run can move from PARTIAL_COMPLETE to COMPLETE with
# ``failedFiles`` 0 while its cached metrics still cover only the documents that
# were scored; the information needed to prevent that was destroyed by the
# overwrite, and separating the two would take a second attribute.
#
# A third consequence, older than any of this and listed because it is in the
# same family: the aggregation selects documents by ``EvaluationStatus ==
# "COMPLETED"``, so a promoted document drops out of a run's metrics on any
# *re*-aggregation even when its evaluation had succeeded — and a release that
# adds a metrics key re-aggregates every historical run once. The cause is the
# overwrite rather than anything here.
#
# ``BASELINE_COPYING`` is settled **despite naming work in progress**,
# because the copy is invoked as an async Lambda whose terminal write is
# best-effort — an invocation that never lands leaves the attribute there with
# nothing to reconcile it. Waiting on it is unbounded, and it would be a wait for
# an action taken on the document *after* the run finished.
EVAL_STATUS_SETTLED = frozenset(
    {
        "COMPLETED",
        "NO_BASELINE",
        "DISABLED",
        "BASELINE_AVAILABLE",
        "BASELINE_ERROR",
        "BASELINE_COPYING",
    }
)
# Terminal and a failure. ``TIMED_OUT`` is the status the single-attempt timeout
# policy stamps (#917); it means the same thing to a run as ``FAILED``.
EVAL_STATUS_UNSUCCESSFUL = frozenset({"FAILED", "TIMED_OUT"})
# Genuinely still working, and able to leave that state on its own.
EVAL_STATUS_IN_FLIGHT = frozenset({"RUNNING"})

# The union, exposed so a consumer can assert closure over its own writers
# without restating the three sets.
EVAL_STATUS_CLASSIFIED = (
    EVAL_STATUS_SETTLED | EVAL_STATUS_UNSUCCESSFUL | EVAL_STATUS_IN_FLIGHT
)

# ``ObjectStatus`` values meaning the pipeline will not touch the document again.
# Used only to decide whether a run can still change; the per-document
# classification above is what decides the run's status.
DOC_STATUS_TERMINAL = frozenset({"COMPLETED", "FAILED", "ABORTED"})

# Run statuses that no reconciliation can move. A run in one of these is skipped
# by the sweeper entirely.
RUN_STATUS_TERMINAL = frozenset({"COMPLETE", "PARTIAL_COMPLETE", "ABORTED", "FAILED"})


# The run-record attribute that throttles metrics aggregation, and the window it
# enforces. Both consumers that enqueue an aggregation — the read resolver and
# the sweeper — write this same attribute under a conditional expression, so the
# two collapse to one enqueue instead of racing. Aggregation re-reads every
# document's results.json from S3, so it can take minutes on a large run; the
# window keeps concurrent readers from stacking up redundant work while still
# letting a run whose aggregation genuinely failed retry later.
CACHE_UPDATE_QUEUED_AT_ATTR = "CacheUpdateQueuedAt"
CACHE_UPDATE_THROTTLE_SECONDS = 300

#: How many times a background sweep has enqueued an aggregation for a run that
#: still has no metrics. Incremented only by the sweeper, and read only to back
#: its own retries off.
CACHE_UPDATE_ATTEMPTS_ATTR = "CacheUpdateAttempts"

#: The sweeper's first retry interval, and the ceiling its backoff reaches.
#:
#: ⚠️ **It cannot be ``CACHE_UPDATE_THROTTLE_SECONDS``, and the reason is a
#: property of the two callers rather than a preference.** That window was sized
#: against a browser polling every 5 seconds, where 300s means "collapse sixty
#: polls into one aggregation". A sweep runs every 300s, so the same window
#: collapses nothing: every sweep clears it and enqueues again.
#:
#: That only matters for a run whose aggregation never completes.
#: :func:`awaiting_metrics` deliberately admits a run that will never have
#: metrics, on the grounds that the cache writer records every key whatever the
#: aggregation found and so the condition clears on one pass — which holds for an
#: empty aggregation but not for one that fails *before* writing, whether by
#: raising or by exhausting the queue's redrive policy. Such a run stays
#: admissible indefinitely, and at one aggregation per sweep that is a few
#: hundred a day, each re-reading every document's results and running the Athena
#: cost and split-classification queries. Backing off turns an unbounded retry
#: loop into a handful of attempts a day.
SWEEP_AGGREGATION_BASE_SECONDS = 900
SWEEP_AGGREGATION_MAX_SECONDS = 86400


def sweep_aggregation_throttle(attempts) -> int:
    """How long a sweep should wait before re-enqueueing this run's aggregation.

    Doubles per recorded attempt from :data:`SWEEP_AGGREGATION_BASE_SECONDS` up
    to :data:`SWEEP_AGGREGATION_MAX_SECONDS`. An unreadable or negative count
    reads as zero attempts, so a corrupt value makes the sweep *more* eager
    rather than silently stopping it — the failure this Lambda exists to remove
    is work that does not happen.
    """
    try:
        n = int(attempts or 0)
    except (TypeError, ValueError):
        n = 0
    n = max(0, min(n, 32))
    return min(SWEEP_AGGREGATION_BASE_SECONDS * (2**n), SWEEP_AGGREGATION_MAX_SECONDS)


def normalized_eval_status(doc_item: Mapping[str, Any]) -> Optional[str]:
    """A document's ``EvaluationStatus``, upper-cased, or None if not recorded.

    Casing is normalized because writers have historically been inconsistent
    about it, and here an unexpected spelling is not a cosmetic problem: it lands
    in the unrecognised branch and holds the run short of a terminal status. A
    blank string is treated as "not recorded", which is what it means.
    """
    raw = doc_item.get("EvaluationStatus")
    if raw is None:
        return None
    normalized = str(raw).strip().upper()
    return normalized or None


def captured_config_of(run_item: Mapping[str, Any]) -> Dict[str, Any]:
    """The configuration a run captured, whether stored compressed or inline.

    Runs record their configuration as a gzip Binary attribute; runs created
    before that stored the body inline under ``Config``.

    Uses ``parse_float=Decimal`` for the compressed path so non-integer numbers
    land as ``Decimal``, matching the legacy-inline path where DynamoDB's
    resource client returns them as ``Decimal`` already.
    """
    if run_item.get("_config_storage") == "compressed":
        blob = run_item.get("_compressed_config")
        if blob is None:
            return {}
        raw = blob if isinstance(blob, bytes) else bytes(blob)
        try:
            return json.loads(gzip.decompress(raw).decode("utf-8"), parse_float=Decimal)
        except Exception as e:
            logger.error(f"Failed to decompress captured test run config: {e}")
            return {}
    config = run_item.get("Config", {})
    return config if isinstance(config, dict) else {}


def evaluation_disabled_in_config(config: Any) -> bool:
    """Does this configuration switch evaluation off?

    The reading must agree with the pipeline's, and the pipeline's goes through
    pydantic's ``EvaluationConfig.enabled: bool``, which accepts the string
    spellings below as well as real booleans — a stored ``"false"`` genuinely
    disables evaluation. Anything else, including a value pydantic would reject,
    reads as enabled: this function only answers True when evaluation
    demonstrably did not run, since the cost of a wrong True is a run reported
    complete while results are still coming.
    """
    if not isinstance(config, dict):
        return False
    evaluation = config.get("evaluation")
    if not isinstance(evaluation, dict) or "enabled" not in evaluation:
        # The default is enabled, so an absent section is not a disabled run.
        return False
    enabled = evaluation["enabled"]
    if isinstance(enabled, bool):
        return not enabled
    if isinstance(enabled, str):
        # Not stripped, deliberately: pydantic does not strip either, so
        # `" false"` is a configuration it REJECTS — every pipeline step fails to
        # load it — and reading that as "evaluation was disabled" would be this
        # function claiming a run finished on the strength of a configuration
        # that never ran. Matching pydantic's exact token set is what keeps the
        # two readings the same one.
        return enabled.lower() in {"0", "off", "f", "false", "n", "no"}
    if isinstance(enabled, (int, float, Decimal)):
        # ``float`` is unreachable through either storage shape today and is
        # listed because pydantic reads 0.0 as false, so omitting it would be a
        # disagreement waiting for a third storage shape.
        return enabled == 0
    return False


def ran_with_evaluation_disabled(run_item: Mapping[str, Any]) -> bool:
    """Did this run process its documents with evaluation switched off?

    Answered from the configuration the run captured, because that is the input
    that decided it. Documents processed since #1330 also say so themselves
    (``EvaluationStatus=DISABLED``), and that is the preferred signal where it
    exists — this covers the two cases where it does not: a run that predates the
    stamp, and one whose stamp could not be written.

    Decompresses the captured config, so call it only when the cheap signals are
    exhausted — and deliberately **not** from :func:`awaiting_metrics`, whose
    answer has to be the same for every caller and so must come off the run
    record; see the warning there.
    """
    return evaluation_disabled_in_config(captured_config_of(run_item))


def is_draft_labeling_run(run_item: Mapping[str, Any]) -> bool:
    """True for a run that CREATES a baseline rather than being scored against one.

    Such a run has no baseline by construction — that is what it is producing —
    so the copier skips baseline staging and evaluation never runs. It therefore
    has no aggregate metrics to wait for, ever.

    Prefers the persisted ``Purpose``. Falls back to the Context string only for
    runs created before Purpose was written, which is why the fallback is exact
    rather than a substring match.
    """
    purpose = run_item.get("Purpose")
    if purpose:
        return purpose == "draft-labeling"
    return run_item.get("Context") == "Draft labeling run"


def awaiting_metrics(run_item: Mapping[str, Any], status: str) -> bool:
    """Is this run finished but still missing its cached aggregate metrics?

    The single definition of the condition behind the ``EVALUATING`` badge.

    ⚠️ **Everything this answer depends on must be readable from the run record
    alone**, because the callers do not all see the same things: only the
    per-run status check reads the run's documents. An exclusion one caller can
    evaluate and another cannot splits the badge in two — a run reporting
    COMPLETE on one surface and EVALUATING on another at the same time — and
    because the two disagree about whether an aggregation is outstanding,
    neither enqueues one, so the split never resolves. The draft-labeling
    exclusion is safe for precisely this reason: ``Purpose`` is on the record.

    A run that will never have metrics is therefore NOT excluded here. It does
    not need to be: the aggregation is enqueued, the cache writer writes every
    key whatever the aggregation found, and the badge clears on that one pass.
    """
    if is_draft_labeling_run(run_item):
        return False
    return status in METRICS_ELIGIBLE_STATUSES and not run_item.get("testRunResult")


@dataclass
class DocumentTally:
    """How a run's documents divide up, as the overall status needs them."""

    completed: int = 0
    failed: int = 0
    evaluating: int = 0
    queued: int = 0
    #: Documents counted as evaluating only because their ``EvaluationStatus``
    #: matched none of the three classified sets. Non-zero means a writer gained
    #: a value nobody taught this module about, and the run cannot finish while
    #: it is there — so it is surfaced rather than folded into ``evaluating``.
    unrecognised: int = 0

    @property
    def accounted(self) -> int:
        return self.completed + self.failed + self.evaluating + self.queued


def tally_documents(
    files: Iterable[str],
    load_document: Callable[[str], Optional[Mapping[str, Any]]],
    run_item: Mapping[str, Any],
) -> DocumentTally:
    """Classify every document of a run into a :class:`DocumentTally`.

    ``load_document`` is called once per file key and returns the document item
    or None if it does not exist yet. Supplying it is the caller's job so that a
    user-facing read can fetch one at a time while a background sweep batches.

    ``run_item`` is consulted at most once, and only if some document turns out
    to carry no evaluation status at all — resolving that decompresses the run's
    captured configuration, which is not worth doing on a poll where every
    document already says what happened to it.
    """
    tally = DocumentTally()
    evaluation_off: Optional[bool] = None

    for file_key in files:
        doc_item = load_document(file_key)
        if doc_item is None:
            # Not created yet. Counting it as queued is what lets a run that has
            # only just started report QUEUED rather than looking short of files.
            tally.queued += 1
            continue

        doc_status = doc_item.get("ObjectStatus", "QUEUED")
        eval_status = normalized_eval_status(doc_item)

        if doc_status == "COMPLETED":
            if eval_status in EVAL_STATUS_SETTLED:
                tally.completed += 1
            elif eval_status in EVAL_STATUS_UNSUCCESSFUL:
                tally.failed += 1
            elif eval_status in EVAL_STATUS_IN_FLIGHT:
                tally.evaluating += 1
            elif eval_status is None:
                # Nothing recorded. Either evaluation has not reached this
                # document yet, or it never will because the run's configuration
                # switched evaluation off — and before #1330 those were read as
                # the first case unconditionally, which is why such a run sat at
                # EVALUATING forever.
                if evaluation_off is None:
                    evaluation_off = ran_with_evaluation_disabled(run_item)
                if evaluation_off:
                    tally.completed += 1
                else:
                    tally.evaluating += 1
            else:
                # A status none of the three sets names. Counting it as
                # evaluating keeps the run short of a terminal status for as long
                # as it is there, so this is a "someone added an
                # EvaluationStatus and did not come here" alarm, not a routine
                # branch — hence WARNING and its own counter.
                tally.evaluating += 1
                tally.unrecognised += 1
                logger.warning(
                    f"Unrecognised EvaluationStatus {eval_status!r} on {file_key}; "
                    "counting as evaluating, which will hold this run short of a "
                    "terminal status"
                )
        elif doc_status in ("FAILED", "ABORTED"):
            tally.failed += 1
        elif doc_status == "QUEUED":
            tally.queued += 1
        # Anything else is a live pipeline stage (OCR, EXTRACTING, ...) and is
        # deliberately left unaccounted, which is what makes the run read
        # RUNNING below rather than terminal.

    return tally


def derive_overall_status(
    tally: DocumentTally,
    files_count: int,
    baseline_failed_files: int,
    stored_status: str,
) -> str:
    """The run status implied by a tally.

    ``baseline_failed_files`` is set once by the copier and never updated, so it
    is added to the tally's failures rather than being rediscovered here.

    Returns ``stored_status`` when the tally implies nothing — which is the
    honest answer for a shape none of the branches describe, and keeps this
    function from inventing a transition.
    """
    total_failed = baseline_failed_files + tally.failed

    if tally.completed == files_count and files_count > 0 and total_failed == 0:
        return "COMPLETE"
    if (
        total_failed > 0
        and (tally.completed + total_failed + tally.evaluating) == files_count
    ):
        return "PARTIAL_COMPLETE"
    if tally.evaluating > 0:
        return "EVALUATING"
    # No ``files_count > 0`` guard, deliberately: this mirrors the derivation it
    # replaces, which reports QUEUED for a zero-file run. Adding one here would
    # change that degenerate run's status as a side effect of a refactor.
    if tally.queued == files_count:
        return "QUEUED"
    if (tally.accounted + baseline_failed_files) < files_count:
        return "RUNNING"
    return stored_status


def total_failed_files(tally: DocumentTally, baseline_failed_files: int) -> int:
    """Failures a run should report: those found in the tally plus the copier's."""
    return baseline_failed_files + tally.failed


def latest_completion_time(doc_items: Iterable[Mapping[str, Any]]) -> Optional[str]:
    """The newest ``CompletionTime`` across a run's documents, or None.

    This is what a run's ``CompletedAt`` is set to when it first reaches a
    terminal status. ``+00:00`` is rewritten to ``Z`` so the comparison — which
    is lexicographic on the ISO string, not on parsed datetimes — is made between
    values in one spelling.
    """
    latest = None
    for doc_item in doc_items:
        completion_time = doc_item.get("CompletionTime")
        if not completion_time:
            continue
        completion_time = str(completion_time).replace("+00:00", "Z")
        if latest is None or completion_time > latest:
            latest = completion_time
    return latest


def needs_reconciliation(run_item: Mapping[str, Any]) -> bool:
    """Could a reconciliation pass change anything about this run?

    The sweeper's admission test, and deliberately cheap: it reads the run record
    only, so deciding whether to look at a run costs nothing per document.

    Two reasons to look at a run: its stored status is not terminal, so the
    documents may have moved on without anything noticing; or it is terminal and
    eligible for metrics that were never cached, which is the state that renders
    as a permanent ``EVALUATING`` badge.
    """
    stored_status = run_item.get("Status", "RUNNING")
    if stored_status not in RUN_STATUS_TERMINAL:
        return True
    return awaiting_metrics(run_item, stored_status)
