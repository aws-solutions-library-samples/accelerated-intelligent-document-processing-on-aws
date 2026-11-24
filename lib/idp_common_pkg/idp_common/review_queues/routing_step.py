# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routing decision: where a document goes after policy rule validation.

``decide`` evaluates the profile's routing table against the document and answers
one of three things: hold the document in a review queue, let the run continue,
or end it as rejected. It is the routing step of the state machine, for both
processing modes.

The table is a decision table (``idp_common.decision_tables``) whose inputs come
from the document (``routing_inputs``). The built-in rules hold a document
exactly when review queues held it before routing rules existed.

A document that is held gets the reason its reviewer reads: the matched routing
rule's reason with the document's values in place of its placeholders
(``idp_common.config.routing_reason``), or a text built from the rule's
conditions when it has none. It also gets the policy rules it failed, whichever
routing rule placed it. The reason can hold extracted values, so it is stored on
the review item and never logged.

``decide`` never raises for a configuration or a document it cannot evaluate: an
unreviewed document then goes to the Default queue, which is the safe side. A
storage failure does raise, so the state retries.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Mapping, Optional

from pydantic import BaseModel

from idp_common.config.feature_flags import review_routing_enabled
from idp_common.config.routing_models import (
    STAGE_AFTER_POLICY_RULE_VALIDATION,
    RoutingCondition,
    RoutingConfig,
    RoutingRule,
    RoutingTable,
)
from idp_common.config.routing_reason import (
    CONFIDENCE_THRESHOLD,
    KIND_CONDITION_VALUE,
    MAX_RENDERED_REASON_BYTES,
    SOURCE_ADMIN,
    SOURCE_FALLBACK,
    Placeholder,
    cut_bytes,
    format_value,
    render_reason,
)
from idp_common.config.routing_validation import routing_input
from idp_common.decision_tables import evaluate
from idp_common.decision_tables.models import DecisionResult
from idp_common.decision_tables.sfeel import display_values

from .api_models import RoutingTestResult
from .models import PolicyFailure, ReviewItemRecord, RoutedBy
from .queue_defs import DEFAULT_QUEUE_ID, QueueDefinition, production_queue_lookup
from .reasons import build_reasons, hitl_threshold
from .routing import (
    _is_finalize_run,
    _iso,
    _withdraw,
    default_queue,
    is_test_studio_document,
    place_for_review,
    queue_definitions,
)
from .routing_dmn import (
    KIND_QUEUE,
    OUTPUT_OUTCOME,
    OUTPUT_QUEUE,
    OUTPUT_REASON,
    expression_for,
    to_decision_table,
)
from .routing_inputs import MAX_WORKERS, InputReader, build_inputs
from .statuses import LIVE_ITEM_STATES, MACHINE_RESULT_DISPOSITIONS, QueueItemState

logger = logging.getLogger(__name__)

__all__ = [
    "CONTINUE",
    "HOLD",
    "REJECTED",
    "RoutingDecision",
    "decide",
    "evaluate_table",
    "fallback_text",
    "routing_table",
    "test_table",
]

HOLD: Literal["hold"] = "hold"
CONTINUE: Literal["continue"] = "continue"
REJECTED: Literal["rejected"] = "rejected"

# Why a decision was made (``RoutingDecision.reason``).
REASON_RULE = "rule"
REASON_DEFAULT_OUTCOME = "default_outcome"
REASON_TEST_STUDIO = "test_studio"
REASON_NO_REVIEW_STORE = "no_review_store"
REASON_HITL_OFF = "hitl_off"
REASON_DEFAULT_QUEUE_OFF = "default_queue_off"
REASON_REVIEW_SKIPPED = "review_skipped"
REASON_REVIEW_REJECTED = "review_rejected"
REASON_UNPLANNED_FINALIZE = "unplanned_finalize"
REASON_PLACEMENT_LIMIT = "placement_limit"
REASON_EVALUATION_ERROR = "evaluation_error"

# Why a document was placed in the Default queue instead of the queue a rule named
# (``RoutedBy.fallback_reason``). This is a different field from
# ``RoutingDecision.reason`` above, and each is compared only with its own
# constants; ``evaluation_error`` is a value of both. The values are stored on the
# work item and listed in docs/review-routing.md.
FALLBACK_QUEUE_MISSING = "queue_missing"
FALLBACK_QUEUE_DISABLED = "queue_disabled"
FALLBACK_EVALUATION_ERROR = "evaluation_error"

#: ``RoutedBy.inputs`` holds at most this many values, each cut to this many bytes.
MAX_ROUTED_INPUTS = 20
MAX_INPUT_VALUE_BYTES = 200

#: ``RoutedBy.policy_failures`` holds at most this many failed policy rules, each
#: with a reason of at most this many bytes and at most this many pages.
MAX_POLICY_FAILURES = 10
MAX_POLICY_FAILURE_REASON_BYTES = 500
MAX_POLICY_FAILURE_PAGES = 10

# Why a reviewer reads the generated reason and not an Admin's (the ``Cause``
# dimension of the ``RoutingReasonFallback`` metric).
_CAUSE_NO_REASON = "no_reason"
_CAUSE_RENDER_ERROR = "render_error"

_AGGREGATE_ANY = "any"

_COMPLETE = "complete"
_DEFAULT_MAX_PLACEMENTS = 3


class RoutingDecision(BaseModel):
    """What the run does next.

    The handler logs this, so it holds no text a reviewer reads: the reason of a
    placement is on the review item (``RoutedBy.reason``).
    """

    decision: Literal["hold", "continue", "rejected"]
    #: Why: a ``REASON_*`` value.
    reason: str
    #: The queue the document was placed in, when held.
    queue: str = ""
    #: The review cycle the placement opened, when held.
    cycle_id: str = ""
    #: The rule that matched; empty when none did.
    rule_id: str = ""


def routing_table(config: Any) -> RoutingTable:
    """The table evaluated after policy rule validation.

    The profile's, or the built-in table for a configuration that has none.
    """
    routing = getattr(config, "routing", None) if config is not None else None
    for table in getattr(routing, "tables", None) or []:
        if (
            getattr(table, "stage", STAGE_AFTER_POLICY_RULE_VALIDATION)
            == STAGE_AFTER_POLICY_RULE_VALIDATION
        ):
            return table
    return RoutingTable()


def _max_placements(config: Any) -> int:
    routing = getattr(config, "routing", None) if config is not None else None
    value = getattr(routing, "max_consecutive_placements", None)
    return value if isinstance(value, int) and value >= 1 else _DEFAULT_MAX_PLACEMENTS


def evaluate_table(table: RoutingTable, inputs: Mapping[str, Any]) -> DecisionResult:
    """Evaluate a routing table against input values (``RoutingInputs.values``).

    The result's outputs are ``outcome`` (``queue`` or ``no_review``), ``queue``
    and ``reason``. ``testRoutingTable`` uses this too, so a test of a table and a
    run decide alike.
    """
    return evaluate(to_decision_table(table), inputs, engine=table.engine)


def _bounded_inputs(
    values: Mapping[str, Any], expressions: List[str]
) -> Dict[str, str]:
    """The values a decision read, each as JSON text, kept small enough to store."""
    bounded: Dict[str, str] = {}
    for expression in expressions:
        if expression in bounded or expression not in values:
            continue
        text = json.dumps(
            values[expression], default=str, separators=(",", ":"), ensure_ascii=False
        )
        cut = text.encode("utf-8")[:MAX_INPUT_VALUE_BYTES]
        bounded[expression] = cut.decode("utf-8", errors="ignore")
        if len(bounded) == MAX_ROUTED_INPUTS:
            break
    return bounded


def _expressions(rules: List[RoutingRule]) -> List[str]:
    return [expression_for(c) for rule in rules for c in rule.conditions]


def _rule(table: RoutingTable, rule_id: str) -> Optional[RoutingRule]:
    return next((r for r in table.rules if r.id == rule_id), None) if rule_id else None


def _metric(name: str, dimensions: Optional[list] = None) -> None:
    from .service import _put_metric

    _put_metric(name, dimensions=dimensions)


def _history(
    store: Any,
    item: ReviewItemRecord,
    event: str,
    details: Mapping[str, Any],
    now: datetime,
) -> None:
    """A history entry on ``item``'s cycle. Best effort: the decision stands."""
    try:
        store.put_history(
            item.doc_key,
            item.cycle_id,
            event,
            None,
            details,
            item.expires_after,
            now_ms=int(now.timestamp() * 1000),
            now_iso=_iso(now),
        )
    except Exception:  # noqa: BLE001
        logger.warning("event=review_history_put_failed", exc_info=True)


def _placed_queue(
    store: Any, outcome_queue: str, fallback: QueueDefinition
) -> tuple[QueueDefinition, str]:
    """The queue an outcome places a document in, and why it is not the one named.

    The queue the outcome names, or ``fallback`` (the Default queue) when that
    one does not exist or is disabled.
    """
    if not outcome_queue or outcome_queue == DEFAULT_QUEUE_ID:
        return fallback, ""
    named = queue_definitions(store).get(outcome_queue)
    if named is None:
        return fallback, FALLBACK_QUEUE_MISSING
    if not named.enabled:
        return fallback, FALLBACK_QUEUE_DISABLED
    return named, ""


def _condition_text(condition: RoutingCondition, values: Mapping[str, Any]) -> str:
    """One condition as a reader sees it, with the value the document had."""
    entry = routing_input(condition.input)
    parts = [entry.label if entry is not None else condition.input]
    if condition.path:
        parts.append(condition.path)
    if entry is not None and entry.is_list and condition.aggregate != _AGGREGATE_ANY:
        parts.append(f"({condition.aggregate})")
    parts.append(condition.test)
    found = format_value(values.get(expression_for(condition)))
    return f"{' '.join(parts)} (found {found})"


def fallback_text(
    rule: Optional[RoutingRule],
    values: Mapping[str, Any],
    *,
    evaluation_error: bool = False,
) -> str:
    """The reason a reviewer reads when the routing rule has none of its own.

    ``rule`` is the routing rule that matched, ``None`` when the table's default
    outcome applied, and ``values`` what the table read (``RoutingInputs.values``).

    - A routing rule: its label (its id when it has none), then each condition
      with the value found, such as
      ``Invoices over 10,000: Field value Invoice:Total > 10000 (found 12000)``.
    - A routing rule with no condition: that it applies to every document.
    - The default outcome: that no routing rule matched.
    - ``evaluation_error``: that the routing rules could not be evaluated.

    At most ``MAX_RENDERED_REASON_BYTES`` bytes. Never raises.
    """
    if evaluation_error:
        return (
            "The routing rules could not be evaluated, so the document is in the "
            "Default queue."
        )
    if rule is None:
        return "No routing rule matched."
    label = rule.label or rule.id
    if not rule.conditions:
        return cut_bytes(
            f"{label}: this routing rule applies to every document",
            MAX_RENDERED_REASON_BYTES,
        )
    conditions = "; ".join(_condition_text(c, values) for c in rule.conditions)
    return cut_bytes(f"{label}: {conditions}", MAX_RENDERED_REASON_BYTES)


def _placeholder_value(
    placeholder: Placeholder,
    rule: Optional[RoutingRule],
    values: Mapping[str, Any],
    reader: InputReader,
    config: Any,
) -> Any:
    """The value a placeholder of ``rule``'s reason stands for.

    An input the table read is taken from ``values``; any other is read through
    ``reader``, which reads no object twice. A condition value is what the
    routing rule's condition compares with.

    Raises:
        IndexError: the placeholder names a condition the routing rule does not
            have (a save refuses it).
        UnaryTestSyntaxError: the condition's test does not parse.

    ``_reason_for`` catches both and shows the fallback.
    """
    if placeholder.kind == CONFIDENCE_THRESHOLD:
        return hitl_threshold(config)
    if placeholder.kind == KIND_CONDITION_VALUE:
        if rule is None:
            raise IndexError("the default outcome has no condition")
        return display_values(rule.conditions[placeholder.index - 1].test)
    condition = RoutingCondition(
        input=placeholder.input, path=placeholder.path, aggregate=_AGGREGATE_ANY
    )
    expression = expression_for(condition)
    if expression in values:
        return values[expression]
    return reader.value(condition)


def _reason_for(
    rule: Optional[RoutingRule],
    template: str,
    values: Mapping[str, Any],
    reader: InputReader,
    config: Any,
    *,
    fallback_reason: str,
    count_fallback: bool = True,
) -> tuple[str, str]:
    """The reason a reviewer reads, and where it came from.

    ``template`` is the reason of the outcome that applied, as written. With one,
    the result is that text with its placeholders filled (``SOURCE_ADMIN``).
    With none, or when it cannot be rendered, the result is ``fallback_text``
    (``SOURCE_FALLBACK``), and the ``RoutingReasonFallback`` metric counts it by
    cause. A test of a table (``test_table``) passes ``count_fallback=False``:
    the metric counts placements, and a test places nothing. Never raises: a
    reason never fails or delays a placement.
    """
    cause = _CAUSE_NO_REASON
    if template.strip():
        try:
            rendered = render_reason(
                template,
                lambda p: _placeholder_value(p, rule, values, reader, config),
            )
            return rendered, SOURCE_ADMIN
        except Exception as error:  # noqa: BLE001 — the fallback is shown instead
            # The error's type only: its message can quote an extracted value.
            logger.warning(
                "event=routing_reason_render_failed rule=%s error=%s",
                rule.id if rule is not None else "-",
                type(error).__name__,
            )
            cause = _CAUSE_RENDER_ERROR
    if count_fallback:
        _metric("RoutingReasonFallback", [{"Name": "Cause", "Value": cause}])
    try:
        text = fallback_text(
            rule,
            values,
            evaluation_error=fallback_reason == FALLBACK_EVALUATION_ERROR,
        )
    except Exception as error:  # noqa: BLE001 — a placement is never lost to its text
        logger.warning(
            "event=routing_reason_fallback_failed rule=%s error=%s",
            rule.id if rule is not None else "-",
            type(error).__name__,
        )
        text = ""
    return text, SOURCE_FALLBACK


def _policy_failures(reader: InputReader) -> tuple[List[PolicyFailure], int]:
    """The failed policy rules stored with a placement, and how many there are."""
    try:
        failed, count = reader.failed_policy_rules(
            limit=MAX_POLICY_FAILURES,
            reason_bytes=MAX_POLICY_FAILURE_REASON_BYTES,
            max_pages=MAX_POLICY_FAILURE_PAGES,
        )
        return (
            [
                PolicyFailure(
                    rule_id=rule.rule_id,
                    policy=rule.policy,
                    name=rule.name,
                    reason=rule.reason,
                    pages=list(rule.pages),
                )
                for rule in failed
            ],
            count,
        )
    except Exception as error:  # noqa: BLE001 — the placement stands without them
        logger.warning(
            "event=routing_policy_failures_failed error=%s", type(error).__name__
        )
        return [], 0


def test_table(
    document: Any,
    config: Any,
    routing: Optional[RoutingConfig] = None,
    *,
    mode: Literal["pipeline", "bda"],
    store: Any,
    s3: Any = None,
) -> RoutingTestResult:
    """What a routing table decides for a document, without placing it anywhere.

    ``routing`` is a routing section to try in place of the configuration's own,
    such as one an Admin has not saved yet; ``None`` tests the configuration as
    it is. A section that is given is validated first, as a save validates it.

    The inputs are built and the table evaluated as ``decide`` does, and the
    queue and the reason are resolved the same way, so a test and a run agree:
    an outcome that holds the document comes with the reason its reviewer would
    read. Nothing is written: no review item, no history, no status object. The
    answer is about the table: whether a run reaches it (Review Routing on, the
    Default queue enabled, not a Test Studio document) is not part of the test.

    Raises:
        ConfigurationValidationError: when ``routing`` is given and a save would
            refuse it.
    """
    if routing is not None:
        from idp_common.config.routing_validation import validate_routing

        config = config.model_copy(update={"routing": routing})
        validate_routing(
            config, queue_lookup=production_queue_lookup(queue_definitions(store))
        )
    if s3 is None:
        import idp_common.s3 as s3_module

        s3 = s3_module
    table = routing_table(config)
    fallback = default_queue(store)
    fallback_id = fallback.id if fallback is not None else DEFAULT_QUEUE_ID
    reader = InputReader(document, config, mode, s3, MAX_WORKERS)
    built = build_inputs(document, config, table, mode=mode, reader=reader)
    try:
        result = evaluate_table(table, built.values)
    except Exception as error:  # noqa: BLE001 — a table that cannot be evaluated
        logger.warning(
            "event=routing_test_evaluation_failed table=%s error=%s", table.id, error
        )
        # A run places an unreviewed document in the Default queue.
        reason, reason_source = _reason_for(
            None,
            "",
            built.values,
            reader,
            config,
            fallback_reason=FALLBACK_EVALUATION_ERROR,
            count_fallback=False,
        )
        return RoutingTestResult(
            decision=HOLD,
            outcome_kind=KIND_QUEUE,
            outcome_queue=DEFAULT_QUEUE_ID,
            placed_queue=fallback_id,
            fallback_reason=FALLBACK_EVALUATION_ERROR,
            inputs=json.dumps(
                _bounded_inputs(
                    built.values, _expressions([r for r in table.rules if r.enabled])
                ),
                ensure_ascii=False,
            ),
            stale_references=list(built.stale),
            reason=reason,
            reason_source=reason_source,
        )
    rule = _rule(table, result.matched_rule_id)
    used = _expressions(
        [rule] if rule is not None else [r for r in table.rules if r.enabled]
    )
    outcome_kind = str(result.outputs.get(OUTPUT_OUTCOME) or "")
    holds = outcome_kind == KIND_QUEUE
    outcome_queue = str(result.outputs.get(OUTPUT_QUEUE) or "")
    placed, fallback_reason = None, ""
    reason: Optional[str] = None
    reason_source: Optional[str] = None
    if holds:
        if fallback is None:
            placed = fallback_id
        else:
            queue, fallback_reason = _placed_queue(store, outcome_queue, fallback)
            placed = queue.id
        reason, reason_source = _reason_for(
            rule,
            str(result.outputs.get(OUTPUT_REASON) or ""),
            built.values,
            reader,
            config,
            fallback_reason=fallback_reason,
            count_fallback=False,
        )
    return RoutingTestResult(
        decision=HOLD if holds else CONTINUE,
        matched_rule_id=rule.id if rule is not None else None,
        rule_label=rule.label if rule is not None else None,
        outcome_kind=outcome_kind,
        outcome_queue=(outcome_queue or DEFAULT_QUEUE_ID) if holds else None,
        placed_queue=placed,
        fallback_reason=fallback_reason,
        inputs=json.dumps(_bounded_inputs(built.values, used), ensure_ascii=False),
        # A placeholder can name a reference the conditions do not.
        stale_references=list(reader.stale),
        reason=reason,
        reason_source=reason_source,
    )


# The name says what the operation does. This keeps pytest from collecting it as
# a test case in a test module that imports it by name.
test_table.__test__ = False  # type: ignore[attr-defined]


def _done(
    decision: Literal["hold", "continue", "rejected"],
    reason: str,
    *,
    queue: str = "",
    cycle_id: str = "",
    rule_id: str = "",
    reason_source: str = "",
) -> RoutingDecision:
    # ``reason`` is why the decision was made (a ``REASON_*`` value) and
    # ``reason_source`` where the reviewer's reason came from. The reviewer's
    # reason itself is not logged: it can hold extracted values.
    logger.info(
        "event=routing_decision decision=%s reason=%s rule=%s queue=%s "
        "reason_source=%s",
        decision,
        reason,
        rule_id or "-",
        queue or "-",
        reason_source or "-",
    )
    _metric("RoutingDecisions", [{"Name": "Decision", "Value": decision}])
    return RoutingDecision(
        decision=decision,
        reason=reason,
        queue=queue,
        cycle_id=cycle_id,
        rule_id=rule_id,
    )


def decide(
    document: Any,
    config: Any,
    *,
    mode: Literal["pipeline", "bda"],
    execution_arn: str,
    run_plan: Optional[Mapping[str, Any]],
    store: Any,
    s3: Any = None,
    document_service: Any = None,
    output_bucket: Optional[str] = None,
    now: Optional[datetime] = None,
) -> RoutingDecision:
    """Decide what the run does after policy rule validation.

    ``run_plan`` is the ``runPlan`` block of the execution input, or ``None``
    for a run that has none.

    In this order:

    1. A Test Studio document continues (``test_studio``): it is never placed
       in a queue.
    2. A stack with no review table continues (``no_review_store``).
    3. The finalize run of a review that was removed or skipped continues, one
       that was rejected ends as rejected, and one that was completed continues
       when the run has no plan (it did not apply the review in a step of its
       own, so the document does not read as reviewed).
    4. With Review Routing off for the profile (``hitl.enabled``), the run
       continues (``hitl_off``).
    5. With the Default queue missing or disabled, the run continues
       (``default_queue_off``). A queue definition that could not be read for
       a transient cause raises ``TransientError`` instead, so the state
       retries.
    6. The routing table is evaluated. ``no_review`` continues, withdrawing a
       live review a run of the same document left behind.
    7. A queue outcome places the document: in the named queue, or in the Default
       queue when that one is missing or disabled. A document whose review was
       just completed can be placed again, up to
       ``routing.max_consecutive_placements`` placements in a row; above it the
       run continues. The placement records the reason the reviewer reads and
       the policy rules the document failed; neither is read for a document
       that is not placed.

    A table or a document that cannot be evaluated places an unreviewed document
    in the Default queue, and lets a document whose review was just completed
    continue.
    """
    if is_test_studio_document(document):
        return _done(CONTINUE, REASON_TEST_STUDIO)
    if store is None:
        return _done(CONTINUE, REASON_NO_REVIEW_STORE)
    if s3 is None:
        import idp_common.s3 as s3_module

        s3 = s3_module
    now = now or datetime.now(timezone.utc)
    doc_key = document.input_key
    out_bucket = output_bucket or getattr(document, "output_bucket", None) or ""
    item: Optional[ReviewItemRecord] = store.get_item(doc_key)
    finalizing = _is_finalize_run(item, execution_arn)

    if finalizing and item is not None:
        if item.disposition in MACHINE_RESULT_DISPOSITIONS:
            # Removed from review or skipped: the document is not held again.
            return _done(CONTINUE, REASON_REVIEW_SKIPPED)
        if item.disposition != _COMPLETE:
            # A rejected review. A planned run starts at the rejected end and
            # never gets here; this is a finalize message that carried no plan.
            return _done(REJECTED, REASON_REVIEW_REJECTED)
        if not run_plan:
            return _done(CONTINUE, REASON_UNPLANNED_FINALIZE)

    if not review_routing_enabled(config):
        return _done(CONTINUE, REASON_HITL_OFF)
    # Strict: a queue definition that could not be read for a moment is retried,
    # not taken for a queue that is off.
    fallback_queue = default_queue(store, strict=True)
    if fallback_queue is None or not fallback_queue.enabled:
        return _done(CONTINUE, REASON_DEFAULT_QUEUE_OFF)

    table = routing_table(config)
    # One reader for the whole decision: what the table reads also serves the
    # reason's placeholders and the failed policy rules.
    reader = InputReader(document, config, mode, s3, MAX_WORKERS)
    values: Mapping[str, Any] = {}
    rule: Optional[RoutingRule] = None
    fallback_reason = ""
    reason_template = ""
    try:
        values = build_inputs(document, config, table, mode=mode, reader=reader).values
        result = evaluate_table(table, values)
        rule = _rule(table, result.matched_rule_id)
        outcome_kind = result.outputs.get(OUTPUT_OUTCOME)
        outcome_queue = str(result.outputs.get(OUTPUT_QUEUE) or "")
        reason_template = str(result.outputs.get(OUTPUT_REASON) or "")
    except Exception as error:  # noqa: BLE001 — a table or a document it cannot read
        logger.error(
            "event=routing_evaluation_failed table=%s error=%s",
            table.id,
            error,
            exc_info=True,
        )
        if finalizing and item is not None:
            # The review was just completed: do not hold the document for a
            # second one because the table could not be read.
            _history(
                store,
                item,
                "routing_evaluation_failed",
                {"table_id": table.id, "error": type(error).__name__},
                now,
            )
            return _done(CONTINUE, REASON_EVALUATION_ERROR)
        outcome_kind, outcome_queue = KIND_QUEUE, DEFAULT_QUEUE_ID
        fallback_reason = FALLBACK_EVALUATION_ERROR

    rule_id = rule.id if rule is not None else ""
    reason = REASON_RULE if rule is not None else REASON_DEFAULT_OUTCOME
    live = item is not None and item.state in {s.value for s in LIVE_ITEM_STATES}

    if outcome_kind != KIND_QUEUE:
        if live and not finalizing and item is not None:
            _withdraw(
                item,
                store,
                s3,
                out_bucket,
                document_service,
                "superseded_no_review",
                now,
            )
        return _done(CONTINUE, reason, rule_id=rule_id)

    queue: QueueDefinition = fallback_queue
    if not fallback_reason:
        queue, fallback_reason = _placed_queue(store, outcome_queue, fallback_queue)

    # A retry of this execution's routing step finds the item it wrote: the
    # placement is the same one, not one more.
    retried = (
        item is not None
        and item.state == QueueItemState.HOLDING.value
        and item.phase1_execution_arn == execution_arn
    )
    if retried and item is not None:
        placement_count = item.placement_count
        carried = item.carried_metering_json
    elif finalizing and item is not None:
        placement_count = item.placement_count + 1
        carried = item.phase1_metering_json
    else:
        placement_count, carried = 1, None

    limit = _max_placements(config)
    if placement_count > limit and item is not None:
        _history(
            store,
            item,
            "placement_limit_reached",
            {"limit": limit, "rule_id": rule_id, "queue": queue.id},
            now,
        )
        _metric("RoutingPlacementLimit")
        return _done(CONTINUE, REASON_PLACEMENT_LIMIT, rule_id=rule_id)

    # The document is placed. Only now is anything read for what the reviewer
    # sees: the values of the matched routing rule's placeholders, and the
    # summary of policy rule validation.
    review_reason, reason_source = _reason_for(
        rule,
        reason_template,
        values,
        reader,
        config,
        fallback_reason=fallback_reason,
    )
    failures, failure_count = _policy_failures(reader)

    used = (
        _expressions([rule])
        if rule is not None
        else _expressions([r for r in table.rules if r.enabled])
    )
    routed_by = RoutedBy(
        table_id=table.id,
        rule_id=rule_id,
        rule_label=rule.label if rule is not None else "",
        profile=getattr(document, "config_version", None) or "default",
        config_revision=getattr(document, "config_revision", None),
        outcome_queue=outcome_queue or DEFAULT_QUEUE_ID,
        placed_queue=queue.id,
        fallback_reason=fallback_reason,
        inputs=_bounded_inputs(values, used),
        evaluated_at=_iso(now),
        reason=review_reason,
        reason_template=reason_template,
        reason_source=reason_source,
        policy_failures=failures,
        policy_failure_count=failure_count,
    )
    placed = place_for_review(
        document,
        config,
        mode=mode,
        execution_arn=execution_arn,
        store=store,
        item=item,
        queue=queue,
        reasons=build_reasons(document, config, mode=mode),
        routed_by=routed_by,
        placement_count=placement_count,
        carried_metering_json=carried,
        previous_event="rerouted" if finalizing else "superseded",
        s3=s3,
        output_bucket=out_bucket,
        now=now,
    )
    return _done(
        HOLD,
        REASON_EVALUATION_ERROR
        if fallback_reason == FALLBACK_EVALUATION_ERROR
        else reason,
        queue=queue.id,
        cycle_id=placed.cycle_id or "",
        rule_id=rule_id,
        reason_source=reason_source,
    )
