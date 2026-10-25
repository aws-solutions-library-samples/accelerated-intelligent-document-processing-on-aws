# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Test-run sweeper: advances Test Studio run records without a client present.

A Test Studio run's documents are processed and evaluated by the pipeline on
their own. The **run record** — its ``Status``, ``CompletedAt``,
``CompletedFiles``, ``FailedFiles`` and cached ``testRunResult`` metrics — is
derived state, and until this Lambda existed its only writers were the *read*
resolvers: ``getTestRunStatus`` and ``getTestRun``. A run that finished while no
browser was polling and no ``idp-cli --wait`` was running therefore stayed
non-terminal indefinitely, with its metrics never aggregated
(`#1620 <https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1620>`_).

This sweep is the server-side driver. Every five minutes it reconciles the runs
that could still move, using the **same** derivation the resolver uses
(``idp_common.testrun_status``) so the two cannot disagree — a second copy of
that derivation is how #1330 recurred twice.

⚠️ **The sweep is bounded by a trailing window, not by the whole table.** A run
whose ``InitialEventTime`` is older than ``SWEEP_WINDOW_DAYS`` is not visited.
That is a cost decision and it has a consequence worth stating: runs that were
already stalled when this Lambda was first deployed are only recovered if they
fall inside the window. Anything older still needs one view in the UI, which is
the pre-existing behaviour rather than a regression. A run that finishes from now
on is always inside the window, because the sweep interval is minutes and the
window is days.

Within the window there are **two** bounds, and they are separate on purpose:
``MAX_RUNS_EXAMINED`` caps how many run records are read (cheap — a share of a
``BatchGetItem``) and ``MAX_RUNS_RECONCILED`` caps how many get the per-document
fan-out (expensive). Collapsing them into one cap is what let a run that stalled
days ago hide behind newer settled ones; see :func:`_iter_runs_in_window`.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

from idp_common import testrun_status as trs

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Module scope so warm invocations reuse them.
sqs = boto3.client("sqs")
cloudwatch = boto3.client("cloudwatch")

_CLIENT = {}


def _client():
    """The tracking-table client, built on first use.

    Deferred because ``DynamoDBClient`` constructs its boto3 resource eagerly and
    raises when ``TRACKING_TABLE`` is unset — which would turn a missing
    environment variable into an import error rather than a handled one.
    """
    if "c" not in _CLIENT:
        from idp_common.dynamodb import DynamoDBClient

        _CLIENT["c"] = DynamoDBClient()
    return _CLIENT["c"]


def _int_env(name, default):
    """An int from the environment, falling back on anything unreadable.

    A malformed override must not take the sweep out — it runs unattended, and
    the failure mode this Lambda exists to fix is exactly "nothing ran and
    nobody noticed".
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(f"{name}={raw!r} is not an integer; using {default}")
        return default
    if value <= 0:
        logger.warning(f"{name}={value} is not positive; using {default}")
        return default
    return value


# How many run records to ask for per round trip. Also the GSI page size, so one
# page of keys becomes one BatchGetItem.
_EXAMINE_CHUNK = 100

# Exactly what ``needs_reconciliation`` reads, plus the keys and the throttle
# state the enqueue decision needs. ``Status`` is a DynamoDB reserved word, hence
# the alias. Anything added to the admission test has to be added here too, or it
# reads as absent — which for ``testRunResult`` would mean admitting every
# terminal run on every sweep.
_ADMISSION_PROJECTION = (
    "PK, SK, #s, testRunResult, Purpose, #ctx, CacheUpdateQueuedAt, CacheUpdateAttempts"
)
_ADMISSION_NAMES = {"#s": "Status", "#ctx": "Context"}


def _iter_runs_in_window(window_days, max_examined):
    """Hydrated run metadata records inside the window, newest first, in chunks.

    Queries the ``TypeDateIndex`` GSI rather than scanning, the same way the
    Executions list does. The GSI projects only a few *document* attributes, so
    it cannot answer :func:`needs_reconciliation`, and each page of keys is
    therefore read back with ``BatchGetItem``.

    ⚠️ **Projected, not hydrated.** Admission reads four attributes; a full item
    carries the run's whole ``Files`` list — thousands of keys on a large test
    set — and its gzipped captured configuration. Reading those for every run in
    the window to decide whether to look at it is the dominant cost of a sweep
    that finds nothing to do, which is most sweeps. The caller loads the full
    record for the runs it actually reconciles.

    ⚠️ **Yields chunks rather than one list, because examining a run and
    reconciling it have very different costs and have to be bounded
    separately.** One cap over both would be sized by the expensive half and so
    would truncate the cheap half: on a stack holding 1,076 runs in the window, a
    single cap of 100 reaches only the newest 9%, which hides a run that stalled
    days ago behind newer settled ones — the condition this Lambda exists to
    clear. So examination is bounded by ``max_examined`` and the invocation
    deadline, and the per-document fan-out by the caller's reconcile budget.
    """
    from boto3.dynamodb.conditions import Key

    client = _client()
    now = datetime.now(timezone.utc)
    start_iso = (now - timedelta(days=window_days)).isoformat()
    end_iso = now.isoformat()

    query_kwargs = {
        "IndexName": "TypeDateIndex",
        "KeyConditionExpression": Key("ItemType").eq("testrun")
        & Key("InitialEventTime").between(start_iso, end_iso),
        # Newest first: a run that just finished is the one most likely to need
        # advancing, so it is seen before any cap can bite.
        "ScanIndexForward": False,
        "ProjectionExpression": "PK, SK",
        "Limit": _EXAMINE_CHUNK,
    }

    examined = 0
    while examined < max_examined:
        response = client.table.query(**query_kwargs)
        page = response.get("Items", [])
        if page:
            page = page[: max_examined - examined]
            examined += len(page)
            keys = [{"PK": item["PK"], "SK": item["SK"]} for item in page]
            yield client.batch_get_items(
                keys,
                projection_expression=_ADMISSION_PROJECTION,
                expression_attribute_names=_ADMISSION_NAMES,
            )
        if "LastEvaluatedKey" not in response:
            return
        query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def _load_run(test_run_id):
    """The full run record, for a run admission has already selected.

    Discovery projects only the admission attributes, so ``reconcile_run`` needs
    the rest — ``Files``, ``FilesCount``, ``BaselineFailedFiles``, ``CompletedAt``
    and the captured configuration. Paid per *due* run rather than per run in the
    window, which is the whole point of projecting.

    Returns None if the record has gone between discovery and now; a run deleted
    mid-sweep is not an error.
    """
    response = _client().table.get_item(
        Key={"PK": f"testrun#{test_run_id}", "SK": "metadata"}
    )
    return response.get("Item")


def _load_documents(test_run_id, files):
    """Every document item of a run, keyed by file key.

    Batched, unlike the resolver's per-file ``get_item`` loop: a sweep visits
    many runs per invocation, and a 2,000-document test set would otherwise cost
    2,000 sequential round trips per run.

    A key the table does not hold is simply absent from the result, which
    :func:`idp_common.testrun_status.tally_documents` reads as "not created
    yet".
    """
    if not files:
        return {}
    keys = [{"PK": f"doc#{test_run_id}/{file_key}", "SK": "none"} for file_key in files]
    items = _client().batch_get_items(keys)
    by_key = {}
    for item in items:
        pk = item.get("PK", "")
        prefix = f"doc#{test_run_id}/"
        if pk.startswith(prefix):
            by_key[pk[len(prefix) :]] = item
    return by_key


def _claim_cache_update_slot(test_run_id, throttle_seconds):
    """Atomically claim the right to enqueue a cache update for this run.

    The same conditional write the read resolver makes, on the same attribute, so
    a sweep and a browser poll arriving together collapse to one aggregation
    instead of racing — and it also records an attempt, which is what the
    sweeper's own backoff is computed from.

    Fails *open* (returns True) on any unexpected DynamoDB error: a broken
    throttle must never be able to block a legitimate recompute.
    """
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=throttle_seconds)).isoformat()
    attr = trs.CACHE_UPDATE_QUEUED_AT_ATTR
    attempts_attr = trs.CACHE_UPDATE_ATTEMPTS_ATTR
    try:
        _client().table.update_item(
            Key={"PK": f"testrun#{test_run_id}", "SK": "metadata"},
            UpdateExpression=f"SET {attr} = :now ADD {attempts_attr} :one",
            ConditionExpression=(f"attribute_not_exists({attr}) OR {attr} < :cutoff"),
            ExpressionAttributeValues={
                ":now": now.isoformat(),
                ":cutoff": cutoff,
                ":one": 1,
            },
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        logger.warning(
            f"Cache-update throttle check failed for {test_run_id}, "
            f"enqueueing anyway: {e}"
        )
        return True
    except Exception as e:
        logger.warning(
            f"Cache-update throttle check failed for {test_run_id}, "
            f"enqueueing anyway: {e}"
        )
        return True


def _queue_cache_update(test_run_id, prior_attempts=0):
    """Enqueue an async metrics aggregation. Best-effort.

    Returns True only when a message was actually sent, so the sweep's own
    counters report work done rather than work attempted.

    ``prior_attempts`` comes off the run record and sets the throttle: a run
    whose aggregation keeps not completing is retried on a widening interval
    rather than once per sweep. See
    :func:`idp_common.testrun_status.sweep_aggregation_throttle`.
    """
    queue_url = os.environ.get("TEST_RESULT_CACHE_UPDATE_QUEUE_URL")
    if not queue_url:
        logger.warning(
            f"TEST_RESULT_CACHE_UPDATE_QUEUE_URL not set; cannot queue cache "
            f"update for {test_run_id}"
        )
        return False
    throttle = trs.sweep_aggregation_throttle(prior_attempts)
    if not _claim_cache_update_slot(test_run_id, throttle):
        logger.info(
            f"Cache update for {test_run_id} already enqueued inside its "
            f"{throttle}s throttle window (attempts={prior_attempts}); skipping"
        )
        return False
    try:
        sqs.send_message(
            QueueUrl=queue_url, MessageBody=json.dumps({"testRunId": test_run_id})
        )
        logger.info(f"Queued cache update for test run: {test_run_id}")
        return True
    except Exception as e:
        logger.warning(f"Failed to queue cache update for {test_run_id}: {e}")
        return False


def _persist_status(run_item, test_run_id, overall_status, completed, failed, docs):
    """Write a reconciled status back to the run record.

    Mirrors the resolver's write, including setting ``CompletedAt`` from the
    documents' own completion times the first time a run reaches a terminal
    status.
    """
    completed_at = run_item.get("CompletedAt")
    if overall_status in trs.METRICS_ELIGIBLE_STATUSES and not completed_at:
        completed_at = trs.latest_completion_time(docs.values())

    _client().table.update_item(
        Key={"PK": f"testrun#{test_run_id}", "SK": "metadata"},
        UpdateExpression=(
            "SET #status = :status, #completedAt = :completedAt, "
            "CompletedFiles = :completedFiles, FailedFiles = :failedFiles"
        ),
        ExpressionAttributeNames={
            "#status": "Status",
            "#completedAt": "CompletedAt",
        },
        ExpressionAttributeValues={
            ":status": overall_status,
            ":completedAt": completed_at,
            ":completedFiles": completed,
            ":failedFiles": failed,
        },
    )


def reconcile_run(run_item):
    """Reconcile one run. Returns a dict describing what the sweep did to it.

    The verdict shape is deliberately explicit rather than a bare bool: the three
    outcomes a sweep can have for a run — moved it, enqueued metrics for it, or
    found nothing to do — are what the emitted metrics and the log line are built
    from, and collapsing them would make a sweep that finds nothing
    indistinguishable from one that ran out of runs to look at.
    """
    test_run_id = run_item["PK"].split("testrun#", 1)[1]
    stored_status = run_item.get("Status", "RUNNING")
    files = run_item.get("Files", []) or []
    files_count = run_item.get("FilesCount", 0) or 0
    baseline_failed = run_item.get("BaselineFailedFiles", 0) or 0

    verdict = {
        "testRunId": test_run_id,
        "storedStatus": stored_status,
        "status": stored_status,
        "statusChanged": False,
        "metricsQueued": False,
        "unrecognisedEvalStatuses": 0,
    }

    # An aborted run is never recalculated — the abort is the authoritative
    # outcome, and recomputing it from documents that are still finishing would
    # take it back out of a terminal state.
    if stored_status == "ABORTED":
        return verdict

    docs = _load_documents(test_run_id, files)

    # ⚠️ A tally over documents none of which exist says "nothing has started",
    # which is indistinguishable from "they were here and have since expired".
    # For a run whose stored status is already terminal the second reading is the
    # likely one — the `doc#` items carry the tracking table's TTL while the
    # `testrun#` metadata item outlives them — and acting on the first would
    # regress a finished run to QUEUED and write it back. On a timer, unattended,
    # with no signal. The default window sits well inside the TTL so this is
    # latent today, but the window is an environment variable and widening it
    # past the TTL would make it live, so the refusal is here rather than being
    # left to the window's default.
    #
    # A genuinely new run is unaffected: its stored status is not terminal, and
    # deriving QUEUED from QUEUED changes nothing.
    if files and not docs and stored_status in trs.RUN_STATUS_TERMINAL:
        logger.warning(
            f"Test run {test_run_id} is {stored_status} and none of its "
            f"{len(files)} document(s) could be read; leaving its status alone "
            "rather than deriving a fresh one from an empty tally"
        )
        return verdict

    tally = trs.tally_documents(files, docs.get, run_item)
    overall_status = trs.derive_overall_status(
        tally, int(files_count), int(baseline_failed), stored_status
    )
    failed = trs.total_failed_files(tally, int(baseline_failed))

    verdict["status"] = overall_status
    verdict["unrecognisedEvalStatuses"] = tally.unrecognised

    if overall_status != stored_status:
        _persist_status(
            run_item, test_run_id, overall_status, tally.completed, failed, docs
        )
        verdict["statusChanged"] = True
        logger.info(
            f"Test run {test_run_id}: {stored_status} -> {overall_status} "
            f"(completed={tally.completed}, failed={failed}, "
            f"evaluating={tally.evaluating}, queued={tally.queued})"
        )

    # ``awaiting_metrics`` is asked about the run record as it now reads, so the
    # status just written is passed in rather than re-fetched. ``testRunResult``
    # is not touched by the write above, so the record in hand is still current
    # for the only other field the answer depends on.
    if trs.awaiting_metrics(run_item, overall_status):
        verdict["metricsQueued"] = _queue_cache_update(
            test_run_id, run_item.get(trs.CACHE_UPDATE_ATTEMPTS_ATTR, 0)
        )

    return verdict


def _emit_metrics(namespace, counters):
    """Publish the sweep's own counters. Best-effort.

    Everything the sweep can get wrong has to be here, not only what it found.
    A sweep that raised on every run it attempted, or one permanently held at its
    budget so the oldest due runs are never reached, is indistinguishable in
    CloudWatch from a clean sweep that had nothing to do — and this Lambda exists
    because a silent no-op is the failure nobody notices.

    ``TestRunsUnrecognisedEvalStatus`` is the one with an alarm on it: it is
    non-zero only when a writer has gained an ``EvaluationStatus`` the shared
    derivation does not classify, which is the defect that pinned runs at
    EVALUATING twice before.
    """
    if not namespace:
        return
    try:
        cloudwatch.put_metric_data(
            Namespace=namespace,
            MetricData=[
                {"MetricName": name, "Value": value, "Unit": "Count"}
                for name, value in counters.items()
            ],
        )
    except Exception as e:
        logger.warning(f"Failed to emit sweep metrics: {e}")


def _override(event, name, fallback):
    """A positive int from the invocation event, else ``fallback``.

    Lets a one-off manual invocation widen the sweep —
    ``{"windowDays": 90}`` — which is the documented way to recover runs that
    stalled before this Lambda was deployed and now sit outside the scheduled
    window. The scheduled rule sends no overrides.
    """
    if not isinstance(event, dict):
        return fallback
    raw = event.get(name)
    if raw is None:
        return fallback
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(f"Ignoring {name}={raw!r} in event; using {fallback}")
        return fallback
    if value <= 0:
        logger.warning(f"Ignoring {name}={value} in event; using {fallback}")
        return fallback
    return value


# Stop starting new runs with less than this much of the invocation left. One
# run's reconciliation is a BatchGetItem fan-out plus up to two writes, so this
# is a generous floor rather than a measured one — the cost of being wrong is a
# timeout partway through a write, and the sweep runs again in five minutes.
_RECONCILE_RESERVE_MS = 15_000


def _running_out_of_time(context):
    """Is there too little of the invocation left to start another run?

    Reads the remaining time defensively: a test double or a direct invocation
    may not carry ``get_remaining_time_in_millis``, and treating its absence as
    "no time left" would make the sweep do nothing at all — the exact failure
    this Lambda exists to prevent. Absent or unreadable therefore means "keep
    going".
    """
    getter = getattr(context, "get_remaining_time_in_millis", None)
    if not callable(getter):
        return False
    try:
        return int(getter()) < _RECONCILE_RESERVE_MS
    except Exception:
        return False


def handler(event, context):
    """Reconcile every test run that could still move."""
    window_days = _override(event, "windowDays", _int_env("SWEEP_WINDOW_DAYS", 7))
    # Two separate bounds, because examining a run costs a share of one
    # BatchGetItem while reconciling it costs a document fan-out. Collapsing them
    # into one cap is what hid older stalled runs behind newer settled ones.
    max_examined = _override(event, "maxExamined", _int_env("MAX_RUNS_EXAMINED", 5000))
    max_reconciled = _override(
        event, "maxReconciled", _int_env("MAX_RUNS_RECONCILED", 50)
    )

    examined = 0
    due_seen = 0
    #: Every run ``reconcile_run`` was called for, whatever came back — and the
    #: quantity the budget charges, because the cost being bounded is the
    #: per-document fan-out and that is paid on entry: a run whose status turns
    #: out not to move has already read every one of its documents. Charging for
    #: status *changes* instead would bound the outcome rather than the work, and
    #: leave a window of status-preserving runs — the ordinary shape on a healthy
    #: stack — doing an unbounded number of fan-outs under any budget.
    attempted = 0
    reconciled = 0
    queued = 0
    unrecognised = 0
    failures = 0
    deferred = 0
    stopped_early = None

    try:
        for chunk in _iter_runs_in_window(window_days, max_examined):
            examined += len(chunk)
            for run_item in chunk:
                if not trs.needs_reconciliation(run_item):
                    continue
                due_seen += 1

                # Leave the invocation enough room to finish the run it is on. A
                # sweep that times out mid-write is the one way this Lambda could
                # leave a run record worse than it found it, and whatever it
                # skips is picked up by the next sweep — admission is recomputed
                # from scratch each time, so there is no progress to lose.
                if _running_out_of_time(context):
                    stopped_early = "deadline"
                elif attempted >= max_reconciled:
                    stopped_early = "budget"
                if stopped_early:
                    deferred += 1
                    continue

                # One run's failure must not end the sweep: the next run may be
                # the one that is stuck, and the sweep is the only thing that
                # will look at it.
                attempted += 1
                try:
                    full = _load_run(run_item["PK"].split("testrun#", 1)[1])
                    if full is None:
                        logger.info(
                            f"{run_item.get('PK')} went away between discovery "
                            "and reconciliation; skipping"
                        )
                        continue
                    verdict = reconcile_run(full)
                except Exception as e:
                    failures += 1
                    logger.error(
                        f"Failed to reconcile {run_item.get('PK')}: {e}",
                        exc_info=True,
                    )
                    continue
                if verdict["statusChanged"]:
                    reconciled += 1
                if verdict["metricsQueued"]:
                    queued += 1
                unrecognised += verdict["unrecognisedEvalStatuses"]
            if stopped_early:
                # Stop paging on either bound. Continuing would query and read
                # every remaining record in the window — up to ``max_examined``
                # of them — solely to increment ``deferred``, which is the
                # expense the projection above exists to avoid.
                #
                # ⚠️ ``deferred`` is therefore a FLOOR once this fires: the runs
                # on pages never fetched are not counted. ``stoppedEarly`` is
                # what tells a reader which it is, and the next sweep recomputes
                # admission from scratch so nothing is lost.
                break
    except Exception as e:
        # Discovery failing must not look like a sweep with nothing to do. A
        # green exit on an unreadable index is indistinguishable from success,
        # which is the shape of defect this Lambda exists to remove.
        logger.error(f"Test run sweep failed: {e}", exc_info=True)
        raise

    logger.info(
        f"Test run sweep: examined {examined} run(s) in the last {window_days}d, "
        f"{due_seen} needed reconciliation, attempted {attempted}, "
        f"advanced {reconciled}, "
        f"queued {queued} aggregation(s), deferred {deferred}"
        + (f" (stopped early: {stopped_early})" if stopped_early else "")
    )

    _emit_metrics(
        os.environ.get("METRIC_NAMESPACE"),
        {
            # Each name matches the response field whose value it carries, so
            # an alarm on a metric means what a reader of the response would
            # take it to mean. A name that describes the sweep in general
            # ("swept") rather than one counter invites being read as whichever
            # count the reader had in mind.
            "TestRunsExamined": examined,
            "TestRunsDue": due_seen,
            "TestRunsReconcileAttempted": attempted,
            "TestRunsReconciled": reconciled,
            "TestRunMetricsQueued": queued,
            "TestRunsReconcileFailed": failures,
            "TestRunsDeferred": deferred,
            "TestRunsUnrecognisedEvalStatus": unrecognised,
        },
    )

    return {
        "runsExamined": examined,
        "runsDue": due_seen,
        "runsReconcileAttempted": attempted,
        "statusesAdvanced": reconciled,
        "metricsQueued": queued,
        "unrecognisedEvalStatuses": unrecognised,
        "failures": failures,
        "deferredToNextSweep": deferred,
        "stoppedEarly": stopped_early,
    }
