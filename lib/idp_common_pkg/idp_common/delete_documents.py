# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Document deletion utilities for IDP.

This module provides robust document deletion functions that handle:
- S3 input/output file deletion
- DynamoDB tracking record deletion
- List entry cleanup with timestamp-aware shard handling
"""

import fnmatch
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError
from botocore.retries.standard import (
    RetryContext,
    ThrottledRetryableChecker,
    TransientRetryableChecker,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


# Retrying a throttled delete, and where that retry lives
# -------------------------------------------------------
# Every AWS call on this path takes a caller-supplied resource — a DynamoDB ``Table``
# and an S3 client — so this module CANNOT set ``Config(retries=...)`` on the client
# underneath it. The retry therefore lives here, around each call, which is also the
# reading a new caller cannot bypass: a caller that constructs its resource with retries
# off still gets this ladder, whereas a ``Config`` at one call site protects only that
# call site.
#
# ⚠️ **It is not the only retry layer, and each attempt here is a whole client call.**
# Measured against the installed botocore (1.42.97): with no ``retries`` override a
# client runs in ``legacy`` mode, and legacy's DynamoDB entry in botocore's own
# ``_retry.json`` is ``max_attempts: 10`` with exponential backoff from a 0.05 s base —
# 25.5 s of sleeps inside ONE call to ``table.delete_item``. So a bare
# ``boto3.resource("dynamodb")`` already retries a throttle beneath this ladder, and an
# attempt count here multiplies that: four attempts is up to 103 s, which outlives the
# 60 s timeout on the delete resolver and the 29 s on its dispatcher. Two stacked ladders
# is how a retry becomes an outage (the note in ``idp_common.bedrock.client`` records the
# same defect costing 900 s inside a 900 s function), and a ladder that outlives its
# caller reports nothing at all — the opposite of the point of this change.
#
# Hence **two** limits, and the seconds are the one that binds when the client below is
# slow: no further attempt STARTS once ``RETRY_MAX_ELAPSED_SECONDS`` of this ladder's own
# clock is spent, so what this layer adds is that budget **plus at most one more attempt**
# — not a multiple of the client's ladder. The attempt count and backoff shape the
# ordinary case, where a momentary throttle clears in well under a second.
#
# Two consequences worth stating rather than leaving to be inferred. Under *sustained*
# throttling with a bare client, the first attempt alone spends the client's 25.5 s, so
# this ladder contributes one attempt and no sleeps: the retrying is botocore's, and this
# layer's value there is that it stops rather than multiplies. And the bound is on attempt
# *starts*, so an attempt that runs long after the budget was checked still runs to
# completion — four fast attempts followed by one slow one exceeds 5 s of added time.
#
# No jitter: this is a single-document cleanup path, not a fleet of writers synchronising
# on one clock, and the client-level ladder below it already spreads its own attempts.
# Pure exponential keeps the worst case a number that can be stated rather than sampled.
#: Attempts, not retries — 4 means the first call plus 3 further attempts.
RETRY_MAX_ATTEMPTS = 4
RETRY_INITIAL_BACKOFF_SECONDS = 0.2
RETRY_BACKOFF_GROWTH = 2.0
RETRY_MAX_BACKOFF_SECONDS = 2.0
#: Wall clock this ladder may add on top of the call it is retrying. 5 s leaves the
#: 4-attempt ladder's own 1.4 s of sleeps untouched, and cuts the ladder short when the
#: client underneath is itself spending tens of seconds per attempt.
RETRY_MAX_ELAPSED_SECONDS = 5.0

# Which faults are worth another attempt is botocore's answer, not a list written here.
# ``ThrottledRetryableChecker`` carries the throttling error codes botocore itself
# retries (``ThrottlingException``, ``ProvisionedThroughputExceededException``,
# ``RequestLimitExceeded``, ``SlowDown`` and the rest) and ``TransientRetryableChecker``
# the transient codes, the 5xx status codes and the connection-error classes. A
# hand-written list in this module would be a second, staler copy of both.
_THROTTLED_CHECKER = ThrottledRetryableChecker()
_TRANSIENT_CHECKER = TransientRetryableChecker()


class _HttpStatus:
    """The one attribute ``TransientRetryableChecker`` reads off an HTTP response.

    ``is_retryable`` consults ``context.http_response.status_code`` to recognise a 5xx,
    and a ``ClientError`` carries that status in ``ResponseMetadata`` rather than as a
    response object. Without this the 500-class faults the DynamoDB model declares
    (``InternalServerError``) would be judged non-retryable — measured: with the status
    supplied ``InternalServerError`` is retryable, without it the same error is not.
    """

    __slots__ = ("status_code",)

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _is_retryable_fault(exc: BaseException) -> bool:
    """Is ``exc`` a transient AWS fault that another attempt might get past?

    ``True`` for a throttle, a 5xx and a connection failure; ``False`` for anything
    whose answer will not change — ``ValidationException``, ``AccessDeniedException``,
    ``ResourceNotFoundException`` — and for any exception that is not an AWS fault at
    all, since retrying a bug in this module just delays the traceback.
    """
    if isinstance(exc, ClientError):
        response = exc.response or {}
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        context = RetryContext(
            attempt_number=1,
            parsed_response=response,
            http_response=_HttpStatus(status) if isinstance(status, int) else None,
            caught_exception=exc,
        )
    elif isinstance(exc, BotoCoreError):
        context = RetryContext(attempt_number=1, caught_exception=exc)
    else:
        return False
    try:
        return _THROTTLED_CHECKER.is_retryable(
            context
        ) or _TRANSIENT_CHECKER.is_retryable(context)
    except Exception:
        # `RetryContext` is botocore-internal, and the transient check reads an
        # `http_response` attribute off it. If a future botocore reads one more attribute
        # than `_HttpStatus` carries, the service fault would be replaced by an
        # `AttributeError` — the real error reachable only through `__context__`. Degrade
        # to "do not retry" instead: the caller still gets the fault it needs to report.
        logger.debug("Could not classify %r for retry; treating as final", exc)
        return False


def _call_with_retry(description: str, call: Callable[..., T], **kwargs: Any) -> T:
    """Issue one AWS call, retrying a transient fault, and raise once the budget is out.

    The last attempt's exception is raised unchanged — same class, same
    ``response["Error"]["Code"]`` — so a caller that inspects it sees the real fault and
    not a wrapper. Nothing here converts a fault into a value: an exhausted retry is a
    failure, and the point of the ladder is that the failure it reports is a sustained
    one rather than a momentary throttle.

    ⚠️ **The budget is in seconds as well as attempts, because the attempt count alone
    does not bound the wall clock.** Each attempt here is a whole *client* call, and a
    client in botocore's default ``legacy`` mode spends up to 25.5 s of its own retries
    inside one of them for DynamoDB — so four attempts is up to 103 s, against a 60 s
    Lambda timeout on the delete resolver and 29 s on the dispatcher in front of it. A
    ladder that runs past the caller's timeout reports nothing at all, which is worse than
    the failure it was trying to avoid. So no further attempt **starts** once
    ``RETRY_MAX_ELAPSED_SECONDS`` is spent. Note precisely what that bounds: the budget
    plus at most one more attempt, since an attempt already under way is not interrupted —
    four fast attempts followed by a slow one can still exceed the budget. The ordinary
    case is untouched: a momentary throttle clears on the first or second attempt.

    Args:
        description: What the call was doing, for the log line on each retry.
        call: The bound AWS call (``table.delete_item``, ``s3.delete_object``, …).
        **kwargs: Passed straight through to ``call``.

    Returns:
        Whatever ``call`` returns.

    Raises:
        Exception: the final attempt's exception, whatever it was.
    """
    backoff = RETRY_INITIAL_BACKOFF_SECONDS
    started = time.monotonic()
    for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
        try:
            return call(**kwargs)
        except Exception as exc:
            if attempt >= RETRY_MAX_ATTEMPTS or not _is_retryable_fault(exc):
                raise
            elapsed = time.monotonic() - started
            if elapsed + backoff > RETRY_MAX_ELAPSED_SECONDS:
                logger.warning(
                    "%s still faulting after %.1fs of retries (attempt %d/%d); "
                    "giving up rather than outliving the caller: %s",
                    description,
                    elapsed,
                    attempt,
                    RETRY_MAX_ATTEMPTS,
                    exc,
                )
                raise
            logger.warning(
                "%s failed with a transient fault (attempt %d/%d), "
                "retrying in %.2fs: %s",
                description,
                attempt,
                RETRY_MAX_ATTEMPTS,
                backoff,
                exc,
            )
            time.sleep(backoff)
            backoff = min(backoff * RETRY_BACKOFF_GROWTH, RETRY_MAX_BACKOFF_SECONDS)
    # Unreachable: the loop either returns or raises.
    raise AssertionError("retry loop ended without a result")


# Both selectors below feed a delete, so each is written as a named predicate rather
# than inline. A selector in front of an irreversible operation is worth reading on its
# own, and the two answer different questions: a batch is a CONTAINER of documents,
# while a document's outputs include the document's own key.


def _is_in_batch(object_key: str, batch_id: str) -> bool:
    """Is ``object_key`` a document belonging to batch ``batch_id``?

    A batch id is a leading **path segment**, not a substring. Every site that puts a
    batch id into an S3 key writes ``f"{batch_id}/..."`` — the batch processor's copy,
    upload and metadata paths all do — so the delimiter is what the key layout means by
    "in this batch".

    ⚠️ **A substring test, and even a bare ``startswith``, over-match here, and this
    feeds a delete.** ``batch_id in object_key`` also selected ``batch-10/b.pdf`` and
    ``archive/batch-1x/c.pdf`` for ``batch-1``; ``object_key.startswith(batch_id)``
    still selects ``batch-10/b.pdf``, so matching the old docstring's word "prefix"
    literally would not have fixed the reported case. Single-digit batch ids are the
    common case in manual use, which is where the collision is most likely.

    A caller who genuinely wants substring behaviour has ``get_documents_by_pattern``,
    which takes an explicit ``*batch-1*``; narrowing here removes no capability.
    """
    return object_key.startswith(f"{batch_id.rstrip('/')}/")


def _is_document_output(key: str, object_key: str) -> bool:
    """Is ``key`` an output object belonging to the document at ``object_key``?

    Outputs are written **beneath** ``{input_key}/`` — ``sections/``, ``summary/``,
    ``rule_validation/``, ``runs/`` and so on — which is the layout
    ``idp_common.document_versions`` documents and builds its own prefixes from. The
    document's own key is included as well, so nothing is missed if anything is ever
    written at it directly.

    ⚠️ **An S3 ``Prefix`` is a byte prefix, not a path segment**, which is why this is a
    client-side predicate rather than a narrower ``Prefix=``. Purging ``invoice.pdf``
    with a bare prefix also enumerated ``invoice.pdf.bak/...`` and ``invoice.pdf-v2/...``
    — and this path deletes **all object versions and delete markers** it finds, so a
    prefix collision destroyed a sibling document's entire output history rather than
    just its current objects. The listing still passes ``Prefix=object_key`` so the
    server does the coarse narrowing; this decides what is actually deleted.
    """
    if key == object_key:
        return True
    return key.startswith(f"{object_key.rstrip('/')}/")


def _require_selector(name: str, value: object) -> str:
    """Refuse a selector that names nothing, ahead of any table scan.

    Both public selectors return ``List[str]``, and that return type has no room to
    report a failure: ``[]`` is the ordinary, correct answer for "that batch holds no
    documents". So a selector that cannot name anything has to be refused by raising,
    and what the shipped callers do with each outcome is what settles it — a raise
    becomes something the caller can act on (the SDK re-raises it as
    ``IDPProcessingError``; the CLI prints it and exits 1), while ``[]`` becomes a
    reported **success** in both (``BatchDeletionResult(success=True,
    deleted_count=0)``, and "No documents found for batch: …" with exit 0).

    ``None`` is the shape this arrives in — ``batch_id=record.get("id")`` where the id
    is absent — and it used to be raised inside the handler that wrapped the filter
    loop, swallowed there, and returned as an empty selection that the deleter then
    reported as a successful no-op.

    **The empty string is refused too**, because neither reading of it is what a caller
    meant: it selected *every* document in the table before the selectors were narrowed
    (the empty string is a substring of every key) and selects *none* after. A caller
    who wants every document says so explicitly with ``pattern="*"``, where the breadth
    is visible in what they wrote.

    Args:
        name: Parameter name to quote back to the caller.
        value: The selector as given.

    Returns:
        ``value``, once it is known to be a usable selector.

    Raises:
        TypeError: ``value`` is not a ``str``.
        ValueError: ``value`` is the empty string.
    """
    if not isinstance(value, str):
        raise TypeError(
            f"{name} must be a str naming the documents to select, "
            f"got {type(value).__name__}"
        )
    if not value:
        raise ValueError(
            f"{name} must name the documents to select and cannot be empty; "
            'to select every document pass pattern="*"'
        )
    return value


def calculate_shard(timestamp: str) -> Tuple[str, str]:
    """
    Calculate shard information from timestamp.

    Args:
        timestamp: ISO timestamp string (e.g., "2025-09-10T12:03:27.256164+00:00")

    Returns:
        tuple: (date_part, shard_str) where shard_str is 2-digit padded

    Raises:
        ValueError: If timestamp format is invalid
    """
    if not timestamp or not isinstance(timestamp, str):
        raise ValueError(
            f"Invalid timestamp: must be a non-empty string, got {type(timestamp)}"
        )

    if "T" not in timestamp:
        raise ValueError(
            f"Invalid timestamp format: missing 'T' separator, got {timestamp}"
        )

    try:
        date_part = timestamp.split("T")[0]  # e.g., 2025-09-10
        time_part = timestamp.split("T")[1]

        if ":" not in time_part:
            raise ValueError(
                f"Invalid time format: missing ':' separator, got {time_part}"
            )

        hour_part = int(time_part.split(":")[0])  # e.g., 12

        # Validate hour range
        if not 0 <= hour_part <= 23:
            raise ValueError(f"Invalid hour: must be 0-23, got {hour_part}")

        # Calculate shard (6 shards per day = 4 hours each)
        hours_in_shard = 24 / 6
        shard = int(hour_part / hours_in_shard)
        shard_str = f"{shard:02d}"  # Format with leading zero

        return date_part, shard_str

    except (ValueError, IndexError) as e:
        if "Invalid" in str(e):
            raise  # Re-raise our custom validation errors
        raise ValueError(f"Invalid timestamp format: {timestamp}, error: {str(e)}")


def _try_exact_list_deletion(
    tracking_table, list_pk: str, list_sk: str, object_key: str
) -> bool:
    """
    Attempt to delete list entry with exact timestamp match.

    Args:
        tracking_table: DynamoDB table resource
        list_pk: Primary key of list entry
        list_sk: Sort key of list entry
        object_key: Document object key for logging

    Returns:
        bool: True if an entry was deleted, False if there was none to delete.
        ``False`` means absence and nothing else — a fault raises.

    Raises:
        botocore.exceptions.ClientError: the delete was rejected, or was still
            throttled after ``RETRY_MAX_ATTEMPTS`` attempts.
        botocore.exceptions.BotoCoreError: the delete could not be issued at all.
    """
    logger.debug(f"Trying exact deletion - PK={list_pk}, SK={list_sk}")
    result = _call_with_retry(
        f"exact list-entry delete for {object_key}",
        tracking_table.delete_item,
        Key={"PK": list_pk, "SK": list_sk},
        ReturnValues="ALL_OLD",
    )

    if "Attributes" in result:
        logger.info(f"Deleted list entry with exact match: PK={list_pk}")
        return True
    logger.debug(f"No list entry found with exact match: PK={list_pk}, SK={list_sk}")
    return False


def _query_shard_for_object_key(
    tracking_table, list_pk: str, object_key: str
) -> List[Dict[str, Any]]:
    """
    Query a shard for any list entries containing the specified object key.
    Uses DynamoDB filter expressions for efficiency.

    Args:
        tracking_table: DynamoDB table resource
        list_pk: Primary key of the shard
        object_key: Document object key to search for

    Returns:
        List[Dict]: the matching DynamoDB items. An empty list means the shard holds
        no entry for this document, and nothing else.

    Raises:
        botocore.exceptions.ClientError: the query was rejected, or was still throttled
            after ``RETRY_MAX_ATTEMPTS`` attempts.
        botocore.exceptions.BotoCoreError: the query could not be issued at all.

    ⚠️ **``[]`` is the answer for "no entry here", so a failure cannot use it.** This
    list decides which list entries get deleted, and the caller reports "no entry was
    deleted" without an error — so a throttled query that returned ``[]`` was reported
    to the user as a **successful delete** while the row stayed in the document list,
    with nothing anywhere saying so (#1238). A raise is the only outcome the caller can
    tell apart from absence, and it cannot widen the delete: no rows are selected.
    """
    logger.debug(f"Querying shard {list_pk} for ObjectKey: {object_key}")

    matching_items: List[Dict[str, Any]] = []
    exclusive_start_key = None
    while True:
        query_kwargs: Dict[str, Any] = {
            # Use a filter expression to return only matching items
            "KeyConditionExpression": Key("PK").eq(list_pk),
            "FilterExpression": "ObjectKey = :obj_key OR contains(SK, :obj_id)",
            "ExpressionAttributeValues": {
                ":obj_key": object_key,
                ":obj_id": f"#id#{object_key}",
            },
        }
        if exclusive_start_key:
            query_kwargs["ExclusiveStartKey"] = exclusive_start_key
        response = _call_with_retry(
            f"list shard query {list_pk} for {object_key}",
            tracking_table.query,
            **query_kwargs,
        )
        matching_items.extend(response.get("Items", []))
        # ⚠️ Membership, not ``.get()``, and the reason is the test doubles rather than
        # DynamoDB: ``MagicMock().get("LastEvaluatedKey")`` auto-creates a **truthy**
        # attribute, so a paginating loop written the other way never terminates against
        # a bare mock — measured: `bool(mock.get(...))` is `True`, `"x" in mock` is
        # `False`. An unbounded delete loop is an expensive way to find that out.
        if "LastEvaluatedKey" not in response:
            break
        exclusive_start_key = response["LastEvaluatedKey"]

    logger.debug(f"Found {len(matching_items)} matching entries in shard {list_pk}")
    return matching_items


def _get_adjacent_shards(date_part: str, shard_str: str) -> List[str]:
    """
    Get adjacent shard identifiers for edge case handling.

    Args:
        date_part: Date string (e.g., "2025-09-10")
        shard_str: Shard string (e.g., "03")

    Returns:
        List[str]: List of adjacent shard PKs
    """
    try:
        current_shard = int(shard_str)
        adjacent_shards = []

        # Previous shard
        if current_shard > 0:
            prev_shard = f"{current_shard - 1:02d}"
            adjacent_shards.append(f"list#{date_part}#s#{prev_shard}")

        # Next shard
        if current_shard < 5:  # Max shard is 05
            next_shard = f"{current_shard + 1:02d}"
            adjacent_shards.append(f"list#{date_part}#s#{next_shard}")

        return adjacent_shards

    except Exception as e:
        logger.error(f"Error calculating adjacent shards: {str(e)}")
        return []


def _is_key(entry: Dict[str, Any], key: Optional[Dict[str, str]]) -> bool:
    """Is ``entry`` the item at ``key``? ``False`` for ``None``, which names no item."""
    if key is None:
        return False
    return entry.get("PK") == key["PK"] and entry.get("SK") == key["SK"]


def _delete_found_list_entry(
    tracking_table, entry: Dict[str, Any], object_key: str
) -> bool:
    """Delete one list entry a shard query found, and say whether it was really there.

    ``ReturnValues="ALL_OLD"`` is what distinguishes a delete from a no-op: DynamoDB's
    ``DeleteItem`` succeeds on a key that does not exist, so without the returned
    attributes a delete of an entry another pass already removed would read as a
    deletion.

    Raises:
        botocore.exceptions.ClientError: the delete was rejected, or was still throttled
            after ``RETRY_MAX_ATTEMPTS`` attempts.
        botocore.exceptions.BotoCoreError: the delete could not be issued at all.
    """
    result = _call_with_retry(
        f"list-entry delete for {object_key}",
        tracking_table.delete_item,
        Key={"PK": entry["PK"], "SK": entry["SK"]},
        ReturnValues="ALL_OLD",
    )
    return "Attributes" in result


def delete_list_entries_robust(
    tracking_table, object_key: str, document_metadata: Optional[Dict[str, Any]]
) -> bool:
    """
    Robustly delete list entries for the given object key.
    Uses multiple strategies: exact match, shard query, adjacent shard search.

    Args:
        tracking_table: DynamoDB table resource
        object_key: Document object key
        document_metadata: Optional document metadata containing timestamp info

    Returns:
        bool: True if any entries were deleted. ``False`` means there was no entry to
        delete — a failure raises rather than returning ``False``.

    Raises:
        botocore.exceptions.ClientError: a query or delete was rejected, or was still
            throttled after ``RETRY_MAX_ATTEMPTS`` attempts.
        botocore.exceptions.BotoCoreError: a call could not be issued at all.

    ⚠️ **Nothing in the three strategies is swallowed except a malformed timestamp**,
    and the narrowing is the whole point rather than tidying. ``False`` is what the
    caller records as ``list_entries: False`` **with no error**, so any handler in here
    that logged a fault and carried on reported an orphaned list row as a completed
    delete. That made fixing the query alone inert — its raise landed in the handler one
    frame up and came back out as the same wrong answer (#1238). The one exception is
    ``ValueError`` from :func:`calculate_shard`: an unparseable ``QueuedTime`` is a
    property of the record rather than a fault, it cannot be got past by retrying, and
    aborting the surrounding document delete over it would leave more behind than it
    cleaned up.

    A fault in **strategy 1** is held rather than raised on the spot, and the fallbacks
    still run. The exact delete is a guess at one key, the fallbacks look for the row
    wherever it actually is, and the row being gone is the outcome that matters — so a
    fault on the guess is only worth reporting if nothing else found the entry. Raising
    immediately would have made a single ``TransactionConflictException`` on the exact key
    skip the two strategies that exist precisely because the exact key can miss.
    """
    deleted_any = False
    strategy_one_fault: Optional[BaseException] = None
    faulted_key: Optional[Dict[str, str]] = None
    faulted_key_removed = False

    # Strategy 1: Try exact timestamp match if we have document metadata
    if document_metadata:
        event_time = None
        if "QueuedTime" in document_metadata and document_metadata["QueuedTime"]:
            event_time = document_metadata["QueuedTime"]
        elif (
            "InitialEventTime" in document_metadata
            and document_metadata["InitialEventTime"]
        ):
            event_time = document_metadata["InitialEventTime"]

        if event_time:
            try:
                date_part, shard_str = calculate_shard(event_time)
            except ValueError as e:
                logger.error(f"Error in exact timestamp deletion: {str(e)}")
            else:
                list_pk = f"list#{date_part}#s#{shard_str}"
                list_sk = f"ts#{event_time}#id#{object_key}"

                try:
                    if _try_exact_list_deletion(
                        tracking_table, list_pk, list_sk, object_key
                    ):
                        return True  # Success, no need for fallback
                except (ClientError, BotoCoreError) as e:
                    # Held, not raised: the fallbacks below look for the row wherever it
                    # is, and if one of them removes THIS key the fault changed nothing.
                    # The key is held with it — see the discard condition at the end.
                    strategy_one_fault = e
                    faulted_key = {"PK": list_pk, "SK": list_sk}
                    logger.warning(
                        "Exact list-entry delete for %s faulted (%s); "
                        "falling back to the shard queries",
                        object_key,
                        e,
                    )

    # Strategy 2: Query calculated shard for any entries with matching ObjectKey
    if document_metadata:
        event_time = document_metadata.get("QueuedTime") or document_metadata.get(
            "InitialEventTime"
        )
        if event_time:
            try:
                date_part, shard_str = calculate_shard(event_time)
            except ValueError as e:
                # Unreachable with a strategy-1 fault held: strategy 1 gates on the same
                # `event_time` and calls the same `calculate_shard`, so if it got as far as
                # issuing a delete this cannot raise.
                logger.error(f"Error in shard query strategies: {str(e)}")
                return deleted_any

            list_pk = f"list#{date_part}#s#{shard_str}"
            for entry in _query_shard_for_object_key(
                tracking_table, list_pk, object_key
            ):
                if _delete_found_list_entry(tracking_table, entry, object_key):
                    deleted_any = True
                    if _is_key(entry, faulted_key):
                        faulted_key_removed = True

            # Strategy 3: Check adjacent shards for edge cases
            if not deleted_any:
                for adj_pk in _get_adjacent_shards(date_part, shard_str):
                    for entry in _query_shard_for_object_key(
                        tracking_table, adj_pk, object_key
                    ):
                        if _delete_found_list_entry(tracking_table, entry, object_key):
                            deleted_any = True
                            if _is_key(entry, faulted_key):
                                faulted_key_removed = True

    if strategy_one_fault is not None and not faulted_key_removed:
        # ⚠️ The witness is **that key**, not `deleted_any`. Strategy 2's filter matches
        # every list row for the document, and a document re-uploaded under a fresh
        # `QueuedTime` can hold more than one — so a fallback removing a *different* row
        # says nothing about the row strategy 1 failed on, and discarding the fault on that
        # basis reports a possibly-orphaned row as a completed delete: #1238's own shape,
        # re-created inside the fix for it.
        raise strategy_one_fault
    return deleted_any


def _delete_run_records(tracking_table, object_key: str) -> int:
    """
    Delete all document version (run) items for a document.

    Run items are keyed PK=doc#<key>, SK=run#<run_id>. Returns the number of
    run items deleted. Idempotent — safe on documents with no runs, and safe to run
    again over a document whose run items a previous pass already removed.

    Raises:
        botocore.exceptions.ClientError: the query, or a run item's delete, was rejected
            or was still throttled after ``RETRY_MAX_ATTEMPTS`` attempts.
        botocore.exceptions.BotoCoreError: a call could not be issued at all.

    ⚠️ **An AWS fault propagates; any other exception from one item's delete is logged
    and the remaining items are still attempted.** The two directions have different
    costs. Run items are numerous and independent, so abandoning the rest of them over
    one unexpected error leaves more behind than it cleans up — but a fault is a fault
    for the next item too, and the count this returns is the only thing the caller
    records, so swallowing a throttle reports a partial cleanup as a complete one.
    """
    doc_pk = f"doc#{object_key}"
    deleted = 0
    exclusive_start_key = None
    while True:
        query_kwargs: Dict[str, Any] = {
            "KeyConditionExpression": Key("PK").eq(doc_pk)
            & Key("SK").begins_with("run#"),
            "ProjectionExpression": "PK, SK",
        }
        if exclusive_start_key:
            query_kwargs["ExclusiveStartKey"] = exclusive_start_key
        response = _call_with_retry(
            f"run-record query for {object_key}", tracking_table.query, **query_kwargs
        )
        for item in response.get("Items", []):
            try:
                _call_with_retry(
                    f"run-record delete for {object_key}",
                    tracking_table.delete_item,
                    Key={"PK": item["PK"], "SK": item["SK"]},
                )
                deleted += 1
            except (ClientError, BotoCoreError):
                raise
            except Exception as e:
                logger.error(f"Error deleting run item {item.get('SK')}: {str(e)}")
        # Membership rather than ``.get()``, for the reason given in
        # ``_query_shard_for_object_key``: against a bare mock the other form never ends.
        if "LastEvaluatedKey" not in response:
            break
        exclusive_start_key = response["LastEvaluatedKey"]
    if deleted:
        logger.info(f"Deleted {deleted} run records for {object_key}")
    return deleted


def delete_single_document(
    object_key: str,
    tracking_table,
    s3_client,
    input_bucket: str,
    output_bucket: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Delete a single document and all its associated data.

    Args:
        object_key: Document object key (S3 path)
        tracking_table: DynamoDB table resource
        s3_client: boto3 S3 client
        input_bucket: Input S3 bucket name
        output_bucket: Output S3 bucket name
        dry_run: If True, only report what would be deleted

    Returns:
        Dict with deletion results:
        - success: bool
        - object_key: str
        - deleted: Dict with counts of deleted items
        - errors: List of error messages

    Every step records its own failure and the rest still run, so ``success`` is
    ``len(errors) == 0`` rather than "the function returned". One ordering rule is load
    bearing: **the document record is kept when list-entry cleanup failed**, because
    every list-entry strategy is reached only through that record's timestamp. Deleting
    it after a failed cleanup left the orphaned list row unreachable, so the retry the
    reported failure asks for found no metadata, ran no strategy, and reported
    ``success=True`` with the row still in the list.
    """
    result = {
        "success": True,
        "object_key": object_key,
        "deleted": {
            "input_file": False,
            "output_files": 0,
            "list_entries": False,
            "document_record": False,
            "run_records": 0,
        },
        "errors": [],
    }

    # Get document metadata first
    doc_pk = f"doc#{object_key}"
    document_metadata = None
    try:
        response = _call_with_retry(
            f"document metadata read for {object_key}",
            tracking_table.get_item,
            Key={"PK": doc_pk, "SK": "none"},
        )
        if "Item" in response:
            document_metadata = response["Item"]
            logger.debug(f"Found document metadata for {object_key}")
        else:
            logger.warning(f"Document metadata not found for {object_key}")
    except Exception as e:
        error_msg = f"Error getting document metadata: {str(e)}"
        logger.error(error_msg)
        result["errors"].append(error_msg)

    if dry_run:
        logger.info(f"[DRY RUN] Would delete document: {object_key}")
        return result

    # Delete from input bucket
    try:
        logger.debug(f"Deleting from input bucket: {input_bucket}/{object_key}")
        # S3 answers a throttle with ``SlowDown``, which is in the same botocore
        # throttling table the DynamoDB codes come from, so the same ladder applies.
        _call_with_retry(
            f"input object delete for {object_key}",
            s3_client.delete_object,
            Bucket=input_bucket,
            Key=object_key,
        )
        result["deleted"]["input_file"] = True
    except Exception as e:
        error_msg = f"Error deleting from input bucket: {str(e)}"
        logger.error(error_msg)
        result["errors"].append(error_msg)

    # Delete from output bucket. The output bucket is versioning-enabled, and
    # document version history pins prior runs' output bytes as noncurrent
    # object versions. A versionless delete_object would only add delete markers
    # and leak those pinned bytes forever, so on a full document delete we purge
    # ALL versions (and any delete markers) under the prefix to reclaim storage.
    try:
        paginator = s3_client.get_paginator("list_object_versions")
        deleted_output_count = 0

        for page in paginator.paginate(Bucket=output_bucket, Prefix=object_key):
            entries = page.get("Versions", []) + page.get("DeleteMarkers", [])
            objects = [
                {"Key": e["Key"], "VersionId": e["VersionId"]}
                for e in entries
                if e.get("VersionId") and _is_document_output(e["Key"], object_key)
            ]
            # delete_objects accepts up to 1000 keys per call
            for i in range(0, len(objects), 1000):
                batch = objects[i : i + 1000]
                _call_with_retry(
                    f"output version purge for {object_key}",
                    s3_client.delete_objects,
                    Bucket=output_bucket,
                    Delete={"Objects": batch, "Quiet": True},
                )
                deleted_output_count += len(batch)

        result["deleted"]["output_files"] = deleted_output_count
        logger.debug(f"Deleted {deleted_output_count} output object versions")
    except Exception as e:
        error_msg = f"Error deleting from output bucket: {str(e)}"
        logger.error(error_msg)
        result["errors"].append(error_msg)

    # Delete list entries
    list_entries_failed = False
    try:
        deletion_success = delete_list_entries_robust(
            tracking_table, object_key, document_metadata
        )
        result["deleted"]["list_entries"] = deletion_success
    except Exception as e:
        list_entries_failed = True
        error_msg = f"Error in list entry deletion: {str(e)}"
        logger.error(error_msg)
        result["errors"].append(error_msg)

    # Delete document version (run) records. The pinned output object versions
    # and run manifests they reference were already purged above (the
    # all-versions prefix delete covers the runs/ prefix too), so only the run
    # tracking items remain.
    try:
        run_count = _delete_run_records(tracking_table, object_key)
        result["deleted"]["run_records"] = run_count
    except Exception as e:
        error_msg = f"Error deleting run records: {str(e)}"
        logger.error(error_msg)
        result["errors"].append(error_msg)

    # Delete document record. Kept when list-entry cleanup FAILED: this record's
    # QueuedTime is the only route to the shard the orphaned list row sits in, so
    # deleting it here is what made the retry the reported failure asks for unable to
    # finish the job — and that retry then reported success with the row still listed.
    # An S3 failure does not hold it back, only a list-entry failure does.
    if document_metadata and not list_entries_failed:
        try:
            _call_with_retry(
                f"document record delete for {object_key}",
                tracking_table.delete_item,
                Key={"PK": doc_pk, "SK": "none"},
            )
            result["deleted"]["document_record"] = True
            logger.debug(f"Deleted document record for {object_key}")
        except Exception as e:
            error_msg = f"Error deleting document record: {str(e)}"
            logger.error(error_msg)
            result["errors"].append(error_msg)
    elif document_metadata:
        logger.warning(
            "Keeping the document record for %s: list-entry cleanup failed, and the "
            "record is what a retry needs to locate the remaining list entry",
            object_key,
        )

    result["success"] = len(result["errors"]) == 0
    return result


def delete_documents(
    object_keys: List[str],
    tracking_table,
    s3_client,
    input_bucket: str,
    output_bucket: str,
    dry_run: bool = False,
    continue_on_error: bool = True,
) -> Dict[str, Any]:
    """
    Delete multiple documents and all their associated data.

    Args:
        object_keys: List of document object keys (S3 paths)
        tracking_table: DynamoDB table resource
        s3_client: boto3 S3 client
        input_bucket: Input S3 bucket name
        output_bucket: Output S3 bucket name
        dry_run: If True, only report what would be deleted
        continue_on_error: If True, continue deleting other documents on error

    Returns:
        Dict with deletion results:
        - success: bool (True if all deleted successfully)
        - deleted_count: int
        - failed_count: int
        - results: List[Dict] with per-document results
    """
    results = []
    deleted_count = 0
    failed_count = 0

    for object_key in object_keys:
        try:
            result = delete_single_document(
                object_key=object_key,
                tracking_table=tracking_table,
                s3_client=s3_client,
                input_bucket=input_bucket,
                output_bucket=output_bucket,
                dry_run=dry_run,
            )
            results.append(result)

            if result["success"]:
                deleted_count += 1
            else:
                failed_count += 1
                if not continue_on_error:
                    break

        except Exception as e:
            logger.error(f"Error deleting document {object_key}: {str(e)}")
            results.append(
                {"success": False, "object_key": object_key, "errors": [str(e)]}
            )
            failed_count += 1
            if not continue_on_error:
                break

    return {
        "success": failed_count == 0,
        "deleted_count": deleted_count,
        "failed_count": failed_count,
        "total_count": len(object_keys),
        "results": results,
        "dry_run": dry_run,
    }


def _scan_all_document_keys(
    tracking_table, status_filter: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Scan all document items from the tracking table.

    Args:
        tracking_table: DynamoDB table resource
        status_filter: Optional status filter ('COMPLETED', 'FAILED', 'PROCESSING', etc.)

    Returns:
        List of DynamoDB items with PK starting with 'doc#'
    """
    from boto3.dynamodb.conditions import Attr

    items: List[Dict[str, Any]] = []
    filter_expr = Attr("PK").begins_with("doc#")
    if status_filter:
        filter_expr = filter_expr & Attr("Status").eq(status_filter)

    scan_kwargs: Dict[str, Any] = {"FilterExpression": filter_expr}

    while True:
        # A throttled scan raises rather than returning a short selection (#1187), so
        # the same ladder applies here: a momentary throttle should not become a failed
        # delete for the whole batch.
        response = _call_with_retry("document scan", tracking_table.scan, **scan_kwargs)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    return items


# ⚠️ Neither selector below catches anything, and that is a decision per exception
# class rather than a missing handler. Both used to wrap the scan and the filter loop in
# `except Exception: log; return object_keys`, and every class that can arise in there
# is one whose answer must not be an empty selection:
#
# * Everything the scan can fail with. The DynamoDB service model declares five error
#   shapes for `Scan` — `ProvisionedThroughputExceededException`, `ThrottlingException`,
#   `RequestLimitExceeded`, `ResourceNotFoundException`, `InternalServerError` — and
#   with the generic ones (`AccessDeniedException`, `ValidationException`) they are all
#   `ClientError`; the client-side family (`EndpointConnectionError` and its siblings)
#   is `BotoCoreError`. Returning `[]` for any of them hands the caller a result it
#   cannot tell apart from "that batch is empty", and both shipped callers turn that
#   into a reported success. A throttled scan therefore read as a completed delete.
# * `TypeError` / `ValueError` / `AttributeError` from a selector the caller should not
#   have passed. Those are the caller's own bug, they are refused by
#   `_require_selector` ahead of the scan, and the handler is what used to hide them.
#
# Two shapes of the old behaviour were worse than a single wrong answer. A fault
# *mid-pagination* discarded every page already collected and returned `[]`. A fault
# *mid-loop* returned the keys matched so far, so a **partial** selection went to the
# deleter and was reported as a complete delete.
#
# Propagating cannot widen a delete — a raise selects nothing, so the safe direction
# the handler existed for is kept. What changes is that the caller hears about it.


def get_documents_by_batch(
    tracking_table, batch_id: str, status_filter: Optional[str] = None
) -> List[str]:
    """
    Get all document object keys for a batch.

    Args:
        tracking_table: DynamoDB table resource
        batch_id: Batch ID. Matched as a leading path segment, so ``batch-1``
            selects ``batch-1/a.pdf`` and does **not** select ``batch-10/b.pdf``.
            For substring or wildcard selection use
            :func:`get_documents_by_pattern`.
        status_filter: Optional status filter ('COMPLETED', 'FAILED', 'PROCESSING', etc.)

    Returns:
        List of object keys. An empty list means the batch holds no matching
        documents, and nothing else: a failure raises rather than returning ``[]``.

    Raises:
        TypeError: ``batch_id`` is not a string.
        ValueError: ``batch_id`` is empty.
        botocore.exceptions.ClientError: the table scan was rejected or throttled.
        botocore.exceptions.BotoCoreError: the scan could not be issued at all.
        AttributeError: a scanned record's ``ObjectKey`` is not a string — a
            ``Decimal`` is what an attribute written as ``N`` deserializes to.
            Raised rather than skipped: this list feeds a delete, and a record the
            predicate cannot read is a record whose membership is unknown.
    """
    batch_id = _require_selector("batch_id", batch_id)

    object_keys = []
    for item in _scan_all_document_keys(tracking_table, status_filter):
        object_key = item.get("ObjectKey", "")
        if _is_in_batch(object_key, batch_id):
            object_keys.append(object_key)

    return object_keys


def get_documents_by_pattern(
    tracking_table, pattern: str, status_filter: Optional[str] = None
) -> List[str]:
    """
    Get all document object keys matching a wildcard pattern.

    Uses fnmatch-style patterns (*, ?, [seq], [!seq]).

    Examples:
        - ``"batch-123/*"`` — all docs in batch-123
        - ``"*/invoice*.pdf"`` — any invoice PDF in any batch
        - ``"*2025*"`` — any doc with 2025 in the key

    Args:
        tracking_table: DynamoDB table resource
        pattern: Wildcard pattern to match against object keys. ``"*"`` is the
            explicit way to select every document.
        status_filter: Optional status filter ('COMPLETED', 'FAILED', 'PROCESSING', etc.)

    Returns:
        List of matching object keys. An empty list means nothing matched, and
        nothing else: a failure raises rather than returning ``[]``.

    Raises:
        TypeError: ``pattern`` is not a string, or a scanned record's ``ObjectKey``
            is not one — ``fnmatch`` answers the same class for both, so read the
            message to tell the caller's bug from a malformed record. The record is
            not skipped: this list feeds a delete, and one the predicate cannot read
            is one whose membership is unknown.
        ValueError: ``pattern`` is empty.
        botocore.exceptions.ClientError: the table scan was rejected or throttled.
        botocore.exceptions.BotoCoreError: the scan could not be issued at all.
    """
    pattern = _require_selector("pattern", pattern)

    object_keys = []
    for item in _scan_all_document_keys(tracking_table, status_filter):
        object_key = item.get("ObjectKey", "")
        if fnmatch.fnmatch(object_key, pattern):
            object_keys.append(object_key)

    return object_keys
