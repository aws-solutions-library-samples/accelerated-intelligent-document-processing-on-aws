# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``update_circuit_breaker_state`` must never supply a value no expression uses.

DynamoDB refuses the whole request, not the unused part::

    ValidationException: 1 validation error detected: Value provided in
    ExpressionAttributeValues unused in expressions: keys: {:opened}

Issue #1358 is the shape to have in mind: ``queue_processor.update_counter``
supplied one expression value on both directions while only one direction's
``ConditionExpression`` referenced it, so every call on the other direction had
never once succeeded — across four releases, with every existing test green.

Why this file exists, and why it is a test rather than a gate finding
--------------------------------------------------------------------

``scripts/tests/test_dynamodb_unused_expression_values.py`` reads that property
out of the source for every request group it can pair up. It cannot pair this
one. The function assembles ``update_expr`` and ``expr_values`` as separate local
names across a series of conditional branches and only then builds the request
dict from them, so the mapping the gate would enumerate holds a ``Name`` rather
than members — pairing the two would mean following data flow, which is a
different instrument (issue #1466). Of the request groups in that blind spot,
this one supplies more of its values conditionally than any other, each behind
its own condition.

So the property is asserted here instead, by **issuing every request the
function's signature can produce** and letting an expression engine judge it.

⚠️ This covers **this** call site, and the blind spot it sits in holds dozens of
other groups. The gate reads none of them. Whether anything *else* does varies
per site and is not knowable from here: a few are driven incidentally by their
own sibling suites, which do catch a value hoisted out of its guard, and most are
not. So measure before assuming either way — hoist a conditionally supplied value
out of its branch at the site in question and run the suites beside it. The reach
note at the top of the gate module describes the category.

The engine is ``moto``, not DynamoDB
------------------------------------

Named rather than implied, because the property under test *is*
expression-language behaviour and that is the one thing a ``moto`` test cannot
establish about itself. What was measured, on ``moto`` 5.1.8, before this file
was written:

* an ``update_item`` carrying an ``ExpressionAttributeValues`` key no expression
  references is refused with ``ValidationException``, with DynamoDB's wording;
* so is an unreferenced ``ExpressionAttributeNames`` key;
* that validation runs **before** the ``ConditionExpression`` is evaluated and
  before overlapping update paths are detected, so no case here can hide the
  finding behind a condition that happened to fail;
* a ``Query`` carrying an unused value is **not** refused, which is why nothing
  in this file leans on the query path;
* an expression citing an ``ExpressionAttributeNames`` key that was never
  supplied raises a bare ``KeyError`` rather than a ``ClientError``. That is the
  one direction where the engine's answer is not service-shaped, so it is the
  ``_pairing_complaint`` read off the captured request that names it; the sweep's
  ``except ClientError`` lets the ``KeyError`` through and the run still goes red,
  which is the right outcome by a less informative route.

What follows from that is bounded: a green run here says the request satisfies
the rule as ``moto`` implements it. That is why every case also asserts the
pairing on the **captured request** itself, which holds whatever the engine does
— the same belt-and-braces ``test_counter_decrement.py`` uses, and what keeps
this file meaningful if the engine is ever swapped or stops enforcing the rule.
"""

from __future__ import annotations

import ast
import importlib.util
import itertools
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

#: Applied at module scope: these are hermetic (``moto`` stands in for DynamoDB,
#: every other client is unused), and without the marker a ``pytest -m unit``
#: selection — the invocation shape this repository's testing standard names —
#: runs none of them.
pytestmark = pytest.mark.unit

_INDEX_PATH = Path(__file__).with_name("index.py")
_MODULE_NAME = "circuit_breaker_manager_expression_pairing_under_test"

#: DynamoDB's own grammar for the two kinds of substitution token.
_VALUE_TOKEN = re.compile(r":[A-Za-z0-9_]+")
_NAME_TOKEN = re.compile(r"#[A-Za-z0-9_]+")

#: Every request member whose value is an expression that may reference a token.
#: ``update_circuit_breaker_state`` sends the first two; the rest are listed so
#: that a future one is compared rather than silently ignored.
_EXPRESSION_KEYS = (
    "UpdateExpression",
    "ConditionExpression",
    "KeyConditionExpression",
    "FilterExpression",
    "ProjectionExpression",
)


def _function_parameters() -> Tuple[str, ...]:
    """The keyword parameters of ``update_circuit_breaker_state``, from source.

    Read with ``ast`` at collection time rather than from the imported module,
    because the case space below has to be compared against the signature before
    anything is imported — and because that comparison is the ratchet: a new
    parameter that conditionally supplies an expression value is covered, or
    ``test_the_case_space_covers_the_whole_signature`` names it.
    """
    tree = ast.parse(_INDEX_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "update_circuit_breaker_state"
        ):
            args = node.args
            return tuple(
                a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)
            )
    raise AssertionError(
        "update_circuit_breaker_state is not defined in index.py; this file "
        "asserts nothing without it"
    )


def _state_constants() -> Dict[str, str]:
    """``STATE_CLOSED``/``STATE_OPEN``/``STATE_HALF_OPEN``, read from source.

    Same reason as above: the case space is built at collection time. Taking the
    literals from the module keeps a renamed state from quietly dropping a third
    of the cases.
    """
    tree = ast.parse(_INDEX_PATH.read_text(encoding="utf-8"))
    found: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.startswith("STATE_"):
                    found[target.id] = node.value.value
    return found


_PARAMETERS = _function_parameters()
_STATES = _state_constants()

#: One axis per parameter, each carrying every behaviourally distinct value.
#: ``None`` and a value for each optional attribute, both booleans for the flag,
#: and every state plus ``None`` for the expectation — which is the whole of what
#: the branching in the function reads.
_AXES: Dict[str, Tuple[object, ...]] = {
    "new_state": tuple(_STATES.values()),
    "failure_count": (None, 0, 7),
    "recovery_attempts": (None, 0, 4),
    "last_error": (None, "", "Bedrock throttled every retry"),
    "clear_last_error": (False, True),
    "expected_state": (None, *_STATES.values()),
}

_CASES: Tuple[Dict[str, object], ...] = tuple(
    dict(zip(_AXES, combination)) for combination in itertools.product(*_AXES.values())
)


def _is_contradictory(case: Dict[str, object]) -> bool:
    """The one combination that asks for an attribute to be set and removed.

    ``last_error`` adds ``SET last_error = :err`` and ``clear_last_error`` adds
    ``REMOVE last_error``; DynamoDB refuses an update expression whose paths
    overlap, for a reason that has nothing to do with unused values. No caller in
    this module supplies both — asserted from source in
    ``test_no_caller_in_this_module_supplies_both`` rather than taken on trust, so
    this carve-out is a computed premise. ``TestTheOneContradictoryCombination``
    holds that test and ``test_the_engine_refuses_it_for_overlapping_paths``,
    which is the other half: the combination really is refused, and for the
    overlap rather than for an unused value.
    """
    return bool(case["last_error"]) and bool(case["clear_last_error"])


@pytest.fixture
def ddb_module(monkeypatch):
    """The real module with a real (``moto``) DynamoDB table behind it.

    Every other client is left as ``moto`` built it at import; nothing in this
    file reaches one. The table is created inside ``mock_aws`` and *before* the
    module is executed, because ``index.py`` binds ``concurrency_table`` at
    import time.
    """
    env = {
        "CONCURRENCY_TABLE": "test-concurrency",
        "ALERTS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:test-topic",
        "RECOVERY_TIMEOUT_SECONDS": "300",
        "ERROR_HANDLER_ARN": "",
        "METRIC_NAMESPACE": "TEST_NS",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",  # nosec B105 - dummy moto credential
        "AWS_SESSION_TOKEN": "testing",  # nosec B105 - dummy moto credential
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # Not projected by anything this file drives, and an unset value sends the
    # module down the SigV4 publish path on a host that happens to export one.
    monkeypatch.delenv("APPSYNC_API_URL", raising=False)

    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName="test-concurrency",
            KeySchema=[{"AttributeName": "counter_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "counter_id", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        spec = importlib.util.spec_from_file_location(_MODULE_NAME, _INDEX_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE_NAME] = module
        monkeypatch.syspath_prepend(str(_INDEX_PATH.parent))
        spec.loader.exec_module(module)

        assert module.concurrency_table.name == "test-concurrency", (
            "the module did not bind the moto table, so nothing below reaches an "
            "expression engine"
        )
        yield module
        sys.modules.pop(_MODULE_NAME, None)


class _Recorder:
    """Captures every ``update_item`` request while still issuing it for real.

    A proxy rather than a ``MagicMock``: ``get_item`` has to keep reaching the
    real table, and a ``MagicMock`` table accepts any arguments at all — which is
    precisely the coverage shape that let #1358 ship.
    """

    def __init__(self, table):
        self._table = table
        self.calls: List[dict] = []

    def __getattr__(self, name):
        return getattr(self._table, name)

    def update_item(self, **kwargs):
        self.calls.append(kwargs)
        return self._table.update_item(**kwargs)


def _record(module) -> _Recorder:
    recorder = _Recorder(module.concurrency_table)
    module.concurrency_table = recorder
    return recorder


def _seed(module, state: Optional[str]) -> None:
    """Put the breaker item in ``state``, or delete it entirely for ``None``."""
    if state is None:
        module.concurrency_table.delete_item(
            Key={"counter_id": module.CIRCUIT_BREAKER_ID}
        )
        return
    module.concurrency_table.put_item(
        Item={"counter_id": module.CIRCUIT_BREAKER_ID, "state": state}
    )


def _has_a_refusing_pass(case: Dict[str, object]) -> bool:
    """Whether this case has a "the condition does not hold" direction at all.

    An unconditional write has none. Defined once and used both by the sweep's
    loop and by the count it asserts, because the two agreeing is the whole of
    what makes that count exact — written out twice, a change to one is a count
    that silently stops matching the loop.
    """
    return _mismatching_state(case["expected_state"]) is not None


def _mismatching_state(expected_state: Optional[str]) -> Optional[str]:
    """A persisted state the ``expected_state`` condition must refuse.

    ``None`` when there is nothing to mismatch. For ``STATE_CLOSED`` the
    condition is ``attribute_not_exists(#state) OR #state = :expected``, so an
    absent item satisfies it and the mismatch has to be a *different* state.
    """
    if expected_state is None:
        return None
    others = [s for s in _STATES.values() if s != expected_state]
    assert others, "every state equals the expected one; the mismatch is vacuous"
    return others[0]


def _tokens(kwargs: dict) -> Tuple[set, set]:
    """``(value tokens, name tokens)`` every expression in the request references."""
    values, names = set(), set()
    for key in _EXPRESSION_KEYS:
        text = kwargs.get(key)
        if not text:
            continue
        values |= set(_VALUE_TOKEN.findall(text))
        names |= set(_NAME_TOKEN.findall(text))
    return values, names


def _pairing_complaint(kwargs: dict) -> Optional[str]:
    """What DynamoDB would refuse this request for, or ``None``.

    Read off the captured request rather than inferred from the engine's answer,
    so this still says something if the engine stops enforcing the rule.
    """
    referenced_values, referenced_names = _tokens(kwargs)
    supplied_values = set(kwargs.get("ExpressionAttributeValues", {}))
    supplied_names = set(kwargs.get("ExpressionAttributeNames", {}))
    complaints = []
    if supplied_values - referenced_values:
        complaints.append(
            f"ExpressionAttributeValues unused in expressions: "
            f"{sorted(supplied_values - referenced_values)}"
        )
    if supplied_names - referenced_names:
        complaints.append(
            f"ExpressionAttributeNames unused in expressions: "
            f"{sorted(supplied_names - referenced_names)}"
        )
    if referenced_values - supplied_values:
        # The mirror defect, which DynamoDB also refuses: "An expression
        # attribute value used in expression is not defined".
        complaints.append(
            f"expression references undefined value(s): "
            f"{sorted(referenced_values - supplied_values)}"
        )
    if referenced_names - supplied_names:
        complaints.append(
            f"expression references undefined name(s): "
            f"{sorted(referenced_names - supplied_names)}"
        )
    return "; ".join(complaints) or None


def _asks_for(argument: Optional[ast.AST]) -> bool:
    """Whether an argument node asks the function to do the thing it guards.

    Both ``last_error`` and ``clear_last_error`` are read for **truthiness** by
    the function, so an omitted argument and an explicitly falsy literal are the
    same request: do nothing. An argument this cannot evaluate counts as asking,
    because such a caller really does produce the behaviour whenever the value is
    truthy at run time — which is the opposite of a false finding.
    """
    if argument is None:
        return False
    if isinstance(argument, ast.Constant):
        return bool(argument.value)
    return True


def _read_call_sites(
    source: str,
) -> Tuple[List[str], List[str], List[str]]:
    """``(every call site, offenders, unresolved)`` for ``update_circuit_breaker_state``.

    An **offender** asks for ``last_error`` and ``clear_last_error`` at once, by
    keyword or positionally. **Unresolved** is a call carrying a ``*args`` or
    ``**kwargs`` spread, whose members are not visible to a source reading; it is
    reported rather than silently treated as clean.

    Separated from the test so that a synthetic battery can drive this exact
    reading over call shapes the tree does not contain — the only way to know the
    reading works rather than that today's callers happen to pass it.
    """
    tree = ast.parse(source)
    parameters = list(_PARAMETERS)
    calls: List[str] = []
    offenders: List[str] = []
    unresolved: List[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "update_circuit_breaker_state"
        ):
            continue
        where = f"index.py:{node.lineno}"
        calls.append(where)
        if any(isinstance(a, ast.Starred) for a in node.args) or any(
            kw.arg is None for kw in node.keywords
        ):
            unresolved.append(where)
            continue
        given: Dict[str, ast.AST] = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        for index, value in enumerate(node.args):
            if index < len(parameters):
                given[parameters[index]] = value
        if _asks_for(given.get("last_error")) and _asks_for(
            given.get("clear_last_error")
        ):
            offenders.append(where)
    return calls, offenders, unresolved


def _describe(case: Dict[str, object], persisted: Optional[str]) -> str:
    args = ", ".join(f"{k}={v!r}" for k, v in case.items())
    return f"update_circuit_breaker_state({args}) with persisted state {persisted!r}"


class TestTheCaseSpaceIsWhatItClaims:
    """Guards on the derivation, because an empty or narrowed case space makes
    every assertion below pass over nothing and a pytest run reports it as green.
    """

    def test_the_case_space_is_not_empty(self):
        assert len(_CASES) > 100, (
            f"the case space holds {len(_CASES)} cases; the assertions below "
            "prove nothing about a signature they do not cover"
        )

    def test_the_case_space_covers_the_whole_signature(self):
        """The ratchet. A parameter added to the function that conditionally
        supplies an expression value is covered here, or this fails naming it."""
        assert set(_AXES) == set(_PARAMETERS), (
            f"the function's parameters are {sorted(_PARAMETERS)} but the case "
            f"space varies {sorted(_AXES)}; an uncovered parameter is an "
            f"untested conditional expression value"
        )

    def test_every_axis_varies(self):
        thin = {name: axis for name, axis in _AXES.items() if len(set(axis)) < 2}
        assert not thin, f"these axes hold one value, so they test nothing: {thin}"

    def test_the_state_constants_were_found(self):
        assert set(_STATES) == {"STATE_CLOSED", "STATE_OPEN", "STATE_HALF_OPEN"}, (
            f"read {sorted(_STATES)} from index.py; the new_state axis is built "
            "from these, so a miss silently narrows it"
        )

    def test_both_halves_of_the_contradiction_predicate_occur(self):
        """Non-vacuity on the carve-out: it must apply to some cases and not
        others, or it is either shielding everything or nothing."""
        contradictory = [c for c in _CASES if _is_contradictory(c)]
        ordinary = [c for c in _CASES if not _is_contradictory(c)]
        assert contradictory and ordinary, (
            f"{len(contradictory)} contradictory and {len(ordinary)} ordinary "
            "cases; the carve-out is vacuous in one direction"
        )


class TestNoValueIsSentThatNoExpressionUses:
    """The property, over every request the signature can produce.

    Driven twice per case — once with the persisted state satisfying any
    ``expected_state`` condition and once with it refusing — because a value and
    the expression referencing it have to pair up on both. The engine validates
    the expressions before it evaluates the condition (measured; see the module
    docstring), so neither pass can hide a finding behind the other.

    The ordering it does *not* have is worth recording, because the carve-out
    below depends on it: overlapping update paths are detected **after** the
    condition, so on the refusing pass the function swallows a
    ``ConditionalCheckFailedException`` and the overlap is never reached. An
    unused value is still refused there — that one is checked first.
    """

    @pytest.mark.parametrize("new_state", sorted(_AXES["new_state"]))
    @pytest.mark.parametrize("condition_holds", [True, False])
    def test_every_supplied_token_is_referenced(
        self, ddb_module, new_state, condition_holds
    ):
        cases = [c for c in _CASES if c["new_state"] == new_state]
        assert cases, f"no case for new_state={new_state!r}"

        failures: List[str] = []
        issued = 0
        for case in cases:
            expected_state = case["expected_state"]
            if condition_holds:
                persisted = expected_state or new_state
            elif not _has_a_refusing_pass(case):
                # Nothing to refuse: an unconditional write has no "condition does
                # not hold" pass. Skipping the case here rather than excluding it
                # from the axis keeps the count below honest.
                continue
            else:
                persisted = _mismatching_state(expected_state)
            _seed(ddb_module, persisted)
            recorder = _record(ddb_module)
            try:
                ddb_module.update_circuit_breaker_state(**case)
                refusal = None
            except ClientError as e:
                refusal = e
            finally:
                ddb_module.concurrency_table = recorder._table

            if not recorder.calls:
                failures.append(f"{_describe(case, persisted)}: issued no UpdateItem")
                continue
            issued += 1
            kwargs = recorder.calls[-1]

            complaint = _pairing_complaint(kwargs)
            if complaint is not None:
                failures.append(
                    f"{_describe(case, persisted)}: {complaint}\n"
                    f"        UpdateExpression: {kwargs.get('UpdateExpression')!r}\n"
                    f"        ConditionExpression: "
                    f"{kwargs.get('ConditionExpression')!r}\n"
                    f"        values: {sorted(kwargs.get('ExpressionAttributeValues', {}))}"
                )

            if refusal is not None:
                # The only refusal this call site is allowed to raise is the
                # overlap, and only on the pass where the condition lets the
                # engine get that far. Anything else — in particular an unused
                # value — is the defect this file is about.
                if not (
                    _is_contradictory(case)
                    and condition_holds
                    and "document paths overlap" in str(refusal)
                ):
                    failures.append(f"{_describe(case, persisted)}: {refusal}")
            elif _is_contradictory(case) and condition_holds:
                failures.append(
                    f"{_describe(case, persisted)}: setting and removing "
                    f"last_error in one update was accepted; the carve-out in "
                    f"the case space is then shielding an unmeasured request"
                )

        # Exact, not a floor. A case that quietly stops issuing an UpdateItem
        # asserts nothing, and "at least half of them did" is a bound loose
        # enough to absorb a whole branch going missing.
        expected = (
            len(cases)
            if condition_holds
            else sum(1 for c in cases if _has_a_refusing_pass(c))
        )
        assert issued == expected, (
            f"{issued} of the {expected} cases that should have reached an "
            f"UpdateItem did; the difference asserted nothing"
        )
        if condition_holds:
            contradictory = sum(1 for c in cases if _is_contradictory(c))
            assert contradictory, (
                "no contradictory case for this state, so the carve-out above is "
                "unexercised and would shield anything that lands in it"
            )
        assert not failures, (
            f"{len(failures)} finding(s) over {issued} request(s) DynamoDB would "
            f"refuse (one case can contribute two: the pairing read off the "
            f"captured request, and the engine's own refusal):\n    "
            + "\n    ".join(failures)
        )


class TestTheExpectedStateConditionActuallyConditions:
    """What no pairing check can see: the condition being wrong.

    A request can satisfy every pairing rule and still guard the wrong thing.
    ``expected_state`` exists so that two concurrent writers do not both apply a
    transition, so both directions are pinned against the persisted value.
    """

    @pytest.mark.parametrize("expected_state", sorted(_AXES["new_state"]))
    def test_a_matching_state_applies_the_write(self, ddb_module, expected_state):
        _seed(ddb_module, expected_state)

        applied = ddb_module.update_circuit_breaker_state(
            ddb_module.STATE_OPEN, expected_state=expected_state
        )

        assert applied is True
        item = ddb_module.concurrency_table.get_item(
            Key={"counter_id": ddb_module.CIRCUIT_BREAKER_ID}, ConsistentRead=True
        )["Item"]
        assert item["state"] == ddb_module.STATE_OPEN

    @pytest.mark.parametrize("expected_state", sorted(_AXES["new_state"]))
    def test_a_stale_expectation_is_refused_and_changes_nothing(
        self, ddb_module, expected_state
    ):
        persisted = _mismatching_state(expected_state)
        _seed(ddb_module, persisted)

        applied = ddb_module.update_circuit_breaker_state(
            ddb_module.STATE_CLOSED, expected_state=expected_state
        )

        assert applied is False
        item = ddb_module.concurrency_table.get_item(
            Key={"counter_id": ddb_module.CIRCUIT_BREAKER_ID}, ConsistentRead=True
        )["Item"]
        assert item["state"] == persisted

    def test_an_absent_item_satisfies_only_the_closed_expectation(self, ddb_module):
        """The first-ever write. ``attribute_not_exists`` is in the CLOSED
        condition only, and it is there so a brand-new deployment can open its
        breaker; every other expectation must refuse an item that is not there."""
        for state in _STATES.values():
            _seed(ddb_module, None)
            applied = ddb_module.update_circuit_breaker_state(
                ddb_module.STATE_OPEN, expected_state=state
            )
            assert applied is (state == ddb_module.STATE_CLOSED), (
                f"expected_state={state!r} against an absent item applied {applied}"
            )

    def test_no_expectation_applies_unconditionally(self, ddb_module):
        for state in _STATES.values():
            _seed(ddb_module, state)
            assert (
                ddb_module.update_circuit_breaker_state(ddb_module.STATE_CLOSED) is True
            )


class TestTheConditionalAttributesAreActuallyConditional:
    """Each optional attribute reaches the item only when it is asked for.

    The pairing property is satisfied by supplying nothing at all, so without
    this the whole file would pass on a function that wrote only ``state``.
    """

    def test_an_omitted_counter_is_preserved(self, ddb_module):
        ddb_module.concurrency_table.put_item(
            Item={
                "counter_id": ddb_module.CIRCUIT_BREAKER_ID,
                "state": ddb_module.STATE_OPEN,
                "failure_count": 9,
                "recovery_attempts": 3,
            }
        )

        assert ddb_module.update_circuit_breaker_state(ddb_module.STATE_HALF_OPEN)

        item = ddb_module.concurrency_table.get_item(
            Key={"counter_id": ddb_module.CIRCUIT_BREAKER_ID}, ConsistentRead=True
        )["Item"]
        assert (item["failure_count"], item["recovery_attempts"]) == (9, 3)

    @pytest.mark.parametrize(
        "kwargs,attribute,expected",
        [
            ({"failure_count": 4}, "failure_count", 4),
            ({"recovery_attempts": 2}, "recovery_attempts", 2),
            ({"last_error": "throttled"}, "last_error", "throttled"),
        ],
    )
    def test_a_supplied_attribute_is_written(
        self, ddb_module, kwargs, attribute, expected
    ):
        _seed(ddb_module, ddb_module.STATE_CLOSED)

        assert ddb_module.update_circuit_breaker_state(ddb_module.STATE_OPEN, **kwargs)

        item = ddb_module.concurrency_table.get_item(
            Key={"counter_id": ddb_module.CIRCUIT_BREAKER_ID}, ConsistentRead=True
        )["Item"]
        assert item[attribute] == expected

    def test_clear_last_error_removes_it(self, ddb_module):
        ddb_module.concurrency_table.put_item(
            Item={
                "counter_id": ddb_module.CIRCUIT_BREAKER_ID,
                "state": ddb_module.STATE_OPEN,
                "last_error": "stale",
            }
        )

        assert ddb_module.update_circuit_breaker_state(
            ddb_module.STATE_CLOSED, clear_last_error=True
        )

        item = ddb_module.concurrency_table.get_item(
            Key={"counter_id": ddb_module.CIRCUIT_BREAKER_ID}, ConsistentRead=True
        )["Item"]
        assert "last_error" not in item

    #: What each transition does to the two episode timestamps. ``"fresh"`` is a
    #: value this update wrote, ``"kept"`` is the seeded value left alone,
    #: ``"gone"`` is removed. Written out per state rather than derived, because
    #: the asymmetry is the point: ``opened_at`` survives the move into
    #: HALF_OPEN — ``handle_health_check`` times the recovery timeout from it —
    #: and is cleared only on the way to CLOSED.
    _EPISODE_OUTCOMES = {
        "STATE_OPEN": {"opened_at": "fresh", "half_open_at": "gone"},
        "STATE_HALF_OPEN": {"opened_at": "kept", "half_open_at": "fresh"},
        "STATE_CLOSED": {"opened_at": "gone", "half_open_at": "gone"},
    }

    @pytest.mark.parametrize("constant", sorted(_EPISODE_OUTCOMES))
    def test_the_episode_timestamps_follow_the_state(self, ddb_module, constant):
        """``opened_at`` and ``half_open_at`` are each supplied on one state and
        removed on others, which is the pair of branches most likely to drift
        apart from the expression that references them."""
        seeded = "2026-01-01T00:00:00+00:00"
        ddb_module.concurrency_table.put_item(
            Item={
                "counter_id": ddb_module.CIRCUIT_BREAKER_ID,
                "state": ddb_module.STATE_CLOSED,
                "opened_at": seeded,
                "half_open_at": seeded,
            }
        )

        assert ddb_module.update_circuit_breaker_state(getattr(ddb_module, constant))

        item = ddb_module.concurrency_table.get_item(
            Key={"counter_id": ddb_module.CIRCUIT_BREAKER_ID}, ConsistentRead=True
        )["Item"]
        for attribute, outcome in self._EPISODE_OUTCOMES[constant].items():
            if outcome == "gone":
                assert attribute not in item, f"{constant} left {attribute} behind"
            elif outcome == "kept":
                assert item.get(attribute) == seeded, (
                    f"{constant} must not touch {attribute}"
                )
            else:
                assert attribute in item and item[attribute] != seeded, (
                    f"{constant} must stamp a fresh {attribute}"
                )


class TestTheOneContradictoryCombination:
    """``last_error`` and ``clear_last_error`` together ask for one attribute to
    be set and removed in the same update.

    DynamoDB refuses that for overlapping document paths — a different rule from
    the one this file is about, and the reason the case above carves it out. It is
    latent rather than live: no caller in this module supplies both. That premise
    is **computed** from the module's own source rather than asserted, so a future
    caller that does supply both fails here instead of at run time.
    """

    def test_the_engine_refuses_it_for_overlapping_paths(self, ddb_module):
        _seed(ddb_module, ddb_module.STATE_OPEN)

        with pytest.raises(ClientError) as raised:
            ddb_module.update_circuit_breaker_state(
                ddb_module.STATE_CLOSED,
                last_error="still failing",
                clear_last_error=True,
            )

        message = str(raised.value)
        assert "document paths overlap" in message, message
        assert "unused in expressions" not in message, (
            "refused for an unused value, not for the overlap; the carve-out in "
            "the case space above is then hiding a finding this file is about"
        )

    def test_no_caller_in_this_module_supplies_both(self):
        """Reads **positional** arguments as well as keywords, and refuses to
        answer about a call it cannot resolve.

        The signature permits both spellings, so a keywords-only reading is a
        premise that holds for the callers as they happen to be written rather
        than for the function. ``last_error`` is parameter index 3 and
        ``clear_last_error`` index 4; a caller passing five positionals supplies
        both without a single keyword, and that shape went straight past the first
        version of this test.

        ⚠️ **Both arguments are judged on falsiness, not on presence.** The
        function branches on ``if last_error:`` and ``if clear_last_error:``, so a
        caller handing either an explicitly falsy literal asks for nothing and
        cannot produce the overlap. Reading presence instead reports
        ``(STATE_CLOSED, 0, 0, None, True)`` — a correct call — as an offender,
        and a false finding here says a working call can never succeed, which is
        the expensive direction. A value this cannot evaluate (a name, an
        expression) **is** reported, and that is not the same mistake: such a
        caller really does produce the overlap whenever the value is truthy.

        Three shapes it does not resolve. Two it reports rather than ignores: a
        ``*args``/``**kwargs`` spread, whose members are not visible here, and —
        implicitly, via the non-empty assertion below — every call site moving
        behind an alias at once. The third is genuinely uncovered: a *single* call
        through an alias, an attribute or a variable, which needs data flow. So is
        a caller in another module, which this function being module-private makes
        a boundary of the premise rather than something checked.
        """
        assert {"last_error", "clear_last_error"} <= set(_PARAMETERS), (
            f"the two parameters this premise is about are not in "
            f"{list(_PARAMETERS)}; the positional reading would then be looking "
            "at the wrong arguments"
        )

        calls, offenders, unresolved = _read_call_sites(
            _INDEX_PATH.read_text(encoding="utf-8")
        )

        assert calls, (
            "no call to update_circuit_breaker_state found by name in index.py, "
            "so this premise is answering about an empty set. Either the callers "
            "moved out of this module or they now go through an alias; the "
            "carve-out in the case space above rests on this and is unsupported "
            "until it is re-established."
        )
        # Reported together, in one assertion. Asserting `unresolved` first meant
        # a single spread anywhere in the module hid every real offender until a
        # second iteration — the run was red, which is right, but it named the
        # wrong thing.
        complaints = []
        if offenders:
            complaints.append(
                f"these call sites may supply last_error and clear_last_error "
                f"together, which DynamoDB refuses for overlapping update paths "
                f"(a value this reading cannot evaluate counts as supplied): "
                f"{offenders}"
            )
        if unresolved:
            complaints.append(
                f"these call sites pass arguments this reading cannot resolve "
                f"(a *args or **kwargs spread), so whether they supply both is "
                f"unknown: {unresolved}"
            )
        assert not complaints, "; ".join(complaints)

    #: ``(label, call text, expected verdict)``. Driven through the real
    #: ``_read_call_sites``, because the tree contains exactly one shape of caller
    #: and a reading that works only on that shape is a premise about today's
    #: source rather than about the function. ``"clean"`` is the direction that
    #: matters most: a false offender asserts that a working call can never
    #: succeed.
    _SHAPES = [
        ("keywords, neither asked for", "update_circuit_breaker_state(S)", "clean"),
        (
            "keywords, both asked for",
            'update_circuit_breaker_state(S, last_error="x", clear_last_error=True)',
            "offender",
        ),
        (
            "five positionals, both asked for",
            'update_circuit_breaker_state(S, 0, 0, "x", True)',
            "offender",
        ),
        (
            "five positionals, last_error explicitly None",
            "update_circuit_breaker_state(S, 0, 0, None, True)",
            "clean",
        ),
        (
            "five positionals, last_error the empty string",
            'update_circuit_breaker_state(S, 0, 0, "", True)',
            "clean",
        ),
        (
            "clear_last_error explicitly False",
            'update_circuit_breaker_state(S, last_error="x", clear_last_error=False)',
            "clean",
        ),
        (
            "last_error a name this cannot evaluate",
            "update_circuit_breaker_state(S, last_error=reason, clear_last_error=True)",
            "offender",
        ),
        (
            "clear_last_error a name this cannot evaluate",
            'update_circuit_breaker_state(S, last_error="x", clear_last_error=flag)',
            "offender",
        ),
        (
            "mixed: last_error positional, clear by keyword",
            'update_circuit_breaker_state(S, 0, 0, "x", clear_last_error=True)',
            "offender",
        ),
        # The battery's one NEGATIVE case: a call the reading must not match. It
        # earns a place where the alias shape below could not, because its verdict
        # is correct under every possible reading — no improvement could make this
        # a call site of the function under test — so it pins no gap. Without it
        # the function-name filter in `_read_call_sites` is unexercised:
        # broadening it to match any named call leaves every other shape here
        # green, which is the realistic silent form of that mistake.
        (
            "a call to a different function entirely",
            'some_other_update(S, last_error="x", clear_last_error=True)',
            "not-a-call-site",
        ),
        (
            "a **kwargs spread",
            'update_circuit_breaker_state(S, last_error="x", **opts)',
            "unresolved",
        ),
        (
            "an *args spread",
            "update_circuit_breaker_state(*args, clear_last_error=True)",
            "unresolved",
        ),
    ]
    #: ⚠️ A call through an alias (``_alias(S, last_error="x",
    #: clear_last_error=True)``) is deliberately **not** in that list, and the
    #: reason is worth stating because an entry for it is the obvious thing to
    #: write. It has no correct verdict to pin. Today's reading does not see it at
    #: all; a reading taught to follow a module-level alias would see a definite
    #: offender, and that would be right. So any expectation written here is
    #: either "today's limitation is required" — which turns this test red for an
    #: improvement, with a message telling the improver they are wrong — or an
    #: assertion so weak it holds for every possible reading. The residual belongs
    #: in the prose above, where it is, and not in a ratchet.

    @pytest.mark.parametrize(
        "label,call,expected", _SHAPES, ids=[s[0] for s in _SHAPES]
    )
    def test_the_reading_answers_each_caller_shape(self, label, call, expected):
        calls, offenders, unresolved = _read_call_sites(f"x = {call}\n")

        if expected == "not-a-call-site":
            assert not calls, (
                f"{label}: read as a call site of update_circuit_breaker_state, "
                f"which it is not — the reading is answering about the wrong "
                f"function, so every verdict above is about the wrong calls"
            )
            return
        assert calls, f"{label}: not read as a call site at all"
        verdict = "offender" if offenders else "unresolved" if unresolved else "clean"
        assert verdict == expected, (
            f"{label}: read as {verdict!r}, expected {expected!r} — "
            f"{'a false offender says a working call can never succeed' if verdict == 'offender' else 'a missed offender leaves the carve-out unsupported'}"
        )

    def test_the_shape_battery_covers_both_verdicts(self):
        """Non-vacuity on the battery itself: a list of only-clean or only-dirty
        shapes would pass against a reading that answers one way always."""
        verdicts = {expected for _, _, expected in self._SHAPES}
        assert {"clean", "offender", "unresolved", "not-a-call-site"} <= verdicts, (
            f"the battery exercises only {sorted(verdicts)}; a reading that "
            "always answers one way would pass it"
        )


class TestTheRecorderItselfReachesTheTable:
    """The probe, not the subject.

    A recorder that swallowed the request, or a fixture whose table was a
    ``MagicMock``, would make every assertion above pass over nothing — which is
    the failure shape #1358 shipped behind.
    """

    def test_a_recorded_write_still_lands(self, ddb_module):
        _seed(ddb_module, ddb_module.STATE_CLOSED)
        recorder = _record(ddb_module)

        ddb_module.update_circuit_breaker_state(ddb_module.STATE_OPEN)

        assert len(recorder.calls) == 1
        item = recorder._table.get_item(
            Key={"counter_id": ddb_module.CIRCUIT_BREAKER_ID}, ConsistentRead=True
        )["Item"]
        assert item["state"] == ddb_module.STATE_OPEN

    def test_the_engine_behind_the_fixture_enforces_the_rule(self, ddb_module):
        """Measured, not assumed. Every assertion in
        ``TestNoValueIsSentThatNoExpressionUses`` about what the *engine* says
        rests on this; if a future ``moto`` stops enforcing it, the pairing read
        off the captured request is all that remains and this says so.

        The offending request is **assembled** rather than written as a literal,
        and not for style: ``scripts/tests/test_dynamodb_unused_expression_values``
        scans every tracked Python file including this one, so a literal here is a
        whole-tree finding against the test that demonstrates the rule. Building
        it through a name puts it in exactly the blind spot this file exists to
        cover, which is the shape of the call site under test.
        """
        _seed(ddb_module, ddb_module.STATE_CLOSED)
        values = {":ts": "now"}
        values[":never_referenced"] = 1
        request = {
            "Key": {"counter_id": ddb_module.CIRCUIT_BREAKER_ID},
            "UpdateExpression": "SET last_checked_at = :ts",
        }
        request["ExpressionAttributeValues"] = values

        with pytest.raises(ClientError) as raised:
            ddb_module.concurrency_table.update_item(**request)

        assert "unused in expressions" in str(raised.value), (
            f"the engine behind this fixture accepted an unused expression "
            f"value, so the engine half of this file proves nothing: "
            f"{raised.value}"
        )
