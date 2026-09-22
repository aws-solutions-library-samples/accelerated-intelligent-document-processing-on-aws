# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Structural assertions for #787: the extraction and assessment tasks retry
TRANSIENT failures — including the ones a handler surfaces as ``TransientError`` —
and never blanket-retry every function error.

Step Functions matches ``Retry.ErrorEquals`` against the Lambda-reported
``errorType`` (the Python exception class name). ``TransientError`` is the one name
``idp_common.utils.transient_errors`` re-raises transient causes under; listing it
here is what makes the handler-side classification effective. ``States.TaskFailed``
and ``States.ALL`` in a Retry would retry deterministic failures eight times for a
document that can never succeed, which is the behaviour this change removes from
ShardExtractionStep and deliberately does not add to the others.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ASL_PATH = Path(__file__).resolve().parents[1] / "statemachine" / "workflow.asl.json"
# Anchored on the key's CLOSING QUOTE. A bare ``:\s*\$\{…\}`` also matches inside
# quoted values — the nine task resources are
# ``"arn:${Partition}:states:::lambda:invoke"`` — and would rewrite all nine to
# ``"arn: 1:states:::lambda:invoke"``, so the parsed
# document would no longer be a faithful copy of what deploys. See
# ``scripts/tests/test_asl_placeholder_substitution.py``.
_UNQUOTED_PLACEHOLDER_RE = re.compile(r'"\s*:\s*\$\{[^}]+\}')

# Every task whose Lambda handler re-raises TransientError: the in-process
# extraction task, the assessment task, all THREE shard-runtime tasks (plan,
# shard, merge share one handler, so all three must list the name), and the three
# rule-validation tasks (#1101).
TASKS = (
    "ExtractionStep",
    "AssessmentStep",
    "ExtractionPlanStep",
    "ShardExtractionStep",
    "ExtractionMergeStep",
    "PolicyClassificationStep",
    "RuleValidationStep",
    "RuleValidationOrchestration",
)

# Handler sources that must surface a transient cause under the listed name, one
# per distinct Lambda behind ``TASKS``. Kept next to ``TASKS`` because the two are
# one claim: the ASL entry is dead config without the handler, and the handler's
# re-raise is a document failure without the ASL entry.
HANDLER_SOURCES = (
    # each wraps its WHOLE handler, so loads before the main call count too
    "extraction_function/index.py",
    "extraction_function/sfn_runtime_handler.py",
    "assessment_function/index.py",
    "rule-validation-policy-classification-function/index.py",
    "rule-validation-function/index.py",
    "rule-validation-orchestration-function/index.py",
)


def _find_state(states: dict, name: str) -> dict:
    for key, st in states.items():
        if key == name:
            return st
        for branch in st.get("Branches", []):
            found = _find_state(branch["States"], name)
            if found:
                return found
        for sub in ("ItemProcessor", "Iterator"):
            if sub in st:
                found = _find_state(st[sub]["States"], name)
                if found:
                    return found
    return {}


@pytest.fixture(scope="module")
def states() -> dict:
    raw = ASL_PATH.read_text(encoding="utf-8")
    return json.loads(_UNQUOTED_PLACEHOLDER_RE.sub('": 1', raw))["States"]


@pytest.mark.parametrize("task", TASKS)
def test_transient_error_is_retried(states, task):
    st = _find_state(states, task)
    assert st, f"{task} not found"
    names = {n for r in st["Retry"] for n in r["ErrorEquals"]}
    assert "TransientError" in names, (
        f"{task} must retry TransientError — the name "
        "idp_common.utils.transient_errors re-raises transient causes under"
    )
    for n in ("ThrottlingException", "ServiceUnavailableException"):
        assert n in names, f"{task} lost {n}"


@pytest.mark.parametrize("task", TASKS)
def test_no_blanket_retry_of_every_function_error(states, task):
    st = _find_state(states, task)
    names = {n for r in st["Retry"] for n in r["ErrorEquals"]}
    assert "States.TaskFailed" not in names and "States.ALL" not in names, (
        f"{task} would retry deterministic failures (bad schema, unparseable "
        "document) eight times for a document that can never succeed"
    )


@pytest.mark.parametrize("task", TASKS)
def test_the_deterministic_tool_use_failure_is_not_retried_by_name(states, task):
    """#895: the "Model produced invalid sequence as part of ToolUse" outcome
    reproduces on retry with the same request, so neither the Bedrock code that
    carries it nor the exception the extraction path translates it into may appear in
    a Retry list.

    Classification in ``idp_common.utils.transient_errors`` is what stops these
    retries, and it only works because the state machine never lists the names
    directly — a ``ModelStreamErrorException`` entry here would retry the failure up
    to eight times per shard task (``MaxAttempts: 8``) regardless of how the handler
    classifies it.
    """
    st = _find_state(states, task)
    names = {n.lower() for r in st["Retry"] for n in r["ErrorEquals"]}
    for forbidden in ("modelstreamerrorexception", "modelinvalidtoolusesequence"):
        assert forbidden not in names, (
            f"{task} lists {forbidden} — a model that cannot emit a valid tool-use "
            "sequence would be retried instead of failing fast (#895)"
        )


@pytest.mark.parametrize("rel", HANDLER_SOURCES)
def test_the_handlers_actually_raise_the_listed_name(rel):
    """The ASL name is only useful if the handler re-raises under it."""
    src_dir = ASL_PATH.parents[1] / "src"
    text = (src_dir / rel).read_text(encoding="utf-8")
    assert "transient_errors" in text and (
        "raise_if_transient" in text or "TransientError(" in text
    ), f"{rel} does not surface transient failures as TransientError"


def test_the_rule_validation_service_classifies_before_it_swallows():
    """#1101: the two ``except`` blocks in the rule-validation service that do NOT
    re-raise must consult the classifier first.

    This is the half a handler wrapper cannot cover. ``validate_document_async``
    returns a FAILED document rather than raising, and ``_process_rule_question``
    returns a fabricated "Information Not Found" verdict — so by the time either
    reaches ``rule-validation-function``'s wrapper there is no exception left to
    classify, and in the second case no failure at all. Both must call
    ``raise_if_transient`` while the original exception is still in hand.
    """
    service = (
        ASL_PATH.parents[3]
        / "lib"
        / "idp_common_pkg"
        / "idp_common"
        / "rule_validation"
        / "service.py"
    )
    text = service.read_text(encoding="utf-8")
    assert text.count("raise_if_transient(") >= 2, (
        "rule_validation/service.py must classify in BOTH non-raising except "
        "blocks — the per-rule fallback and the document-level one"
    )
    # The fabricated verdict must not be returned without the classifier having
    # had its say first.
    verdict_at = text.index('"recommendation": "Information Not Found"')
    assert "raise_if_transient(" in text[:verdict_at], (
        "the per-rule fallback returns a real verdict; a transient Bedrock fault "
        "must be re-raised before it can be answered with one"
    )


# ---------------------------------------------------------------------------
# Universe closure (#1142): which states SHOULD carry the name, derived
# ---------------------------------------------------------------------------
#
# ``TASKS`` above checks the states it names, in both directions, and has no closure:
# a new Bedrock-calling state can be added, list nothing, and every gate stays green.
# What follows derives the universe instead, so an omission fails here.
#
# The chain is derived end to end, no leg hardcoded:
#
#     ASL ``Resource`` (or ``Parameters.FunctionName``)
#       -> ``${Placeholder}``
#       -> the template's ``DefinitionSubstitutions`` entry
#       -> the Lambda's logical id
#       -> that resource's own IAM grants
#
# ⚠️ **The predicate is the IAM grant, not the handler source, and that is the whole
# point.** A source grep is unsound in BOTH directions, measured:
# ``evaluation_function`` mentions Bedrock zero times yet reaches it through
# ``LLMComparator``, which ``idp_common.evaluation`` resolves behind a module-level
# ``__getattr__`` — so a static import walk misses it too, and misses precisely the
# lazy exports, which is where this package puts its heavy dependencies. Meanwhile
# ``processresults_function`` carries two Bedrock mentions and is not affected.
#
# ⚠️ **The grants are read out of ``Action:`` position, never out of the block's raw
# text.** A regex over the text matches an ARN as readily as an action, and this
# template is full of ARNs whose REGION field is a wildcard —
# ``arn:${AWS::Partition}:bedrock:*::foundation-model/*`` — twelve of them, and not one
# ``bedrock:*`` action anywhere. Matching raw text put ``InvokeBDAFunction`` in this
# universe on the strength of an ARN region, when its only Bedrock action is
# ``bedrock:InvokeDataAutomationAsync`` and it cannot invoke a foundation model at all.
#
# Two properties, each verified against this template rather than assumed:
#
#   * A function may attach permissions inline OR through ``Role: !GetAtt SomeRole``,
#     and both occur: ``PipelineHooksDispatcherFunction`` uses an external role and
#     backs seven task states. So the walk reads the referenced role's block too, and
#     ``test_every_task_target_role_is_readable`` iterates EVERY task target rather
#     than the selected universe — a function whose grants move out of reach must fail
#     rather than leave the universe unexamined.
#   * The predicate answers "permitted to invoke a model", which is what a closure
#     wants; it is not "does invoke one". It is also looser than that in one direction
#     recorded in the registry: it matches a model-invoking ACTION, so a grant of
#     ``bedrock:*`` would qualify (correctly), but so would any future action whose
#     name begins ``InvokeModel``. Over-inclusion is the safe direction — an
#     over-included state must be named with a reason instead of passing by absence.
#
# The closure is ONE-directional on purpose. A state that is not Bedrock-capable may
# still list ``TransientError`` legitimately — ``PolicyClassificationStep`` does, and
# its function grants no Bedrock action at all and its handler references none, so it
# converts DynamoDB and S3 transients rather than model ones. Requiring the converse
# would delete that.
MODEL_INVOKE_ACTION = re.compile(r"^bedrock:(InvokeModel\w*|Converse\w*|\*)$")


def _granted_actions(body: str) -> set[str]:
    """Bedrock actions in ``Action:`` position within one resource block.

    Parsed rather than grepped: an ARN's wildcard region is not an action, and reading
    it as one is what put a data-automation-only function in this universe.
    """
    found: set[str] = set()
    in_action = False
    for line in body.split("\n"):
        stripped = line.strip()
        if re.match(r"^-?\s*Action:\s*$", stripped):
            in_action = True
            continue
        inline = re.match(r"^-?\s*Action:\s*(\S+)\s*$", stripped)
        if inline:
            found.update(re.findall(r"bedrock:[A-Za-z*]+", inline.group(1)))
            in_action = False
            continue
        if in_action:
            if stripped.startswith("- "):
                found.update(re.findall(r"bedrock:[A-Za-z*]+", stripped))
                continue
            in_action = False
    return found


def _can_invoke_a_model(logical_id: str, blocks: dict) -> bool:
    """Whether this function's own block, or a role it references, grants a model call."""
    body = blocks.get(logical_id, "")
    bodies = [body]
    for role in re.findall(r"Role:\s*!GetAtt\s+([A-Za-z0-9]+)\.Arn", body):
        bodies.append(blocks.get(role, ""))
    return any(
        MODEL_INVOKE_ACTION.match(action)
        for one in bodies
        for action in _granted_actions(one)
    )


#: Bedrock-capable states that deliberately do NOT convert transient failures yet.
#: One entry per state, each with its own reason, because an absence is what let this
#: go unnoticed. Both halves of a conversion have to land together for a given state
#: (adding ``raise_if_transient`` to a handler CONVERTS a modeled ThrottlingException
#: into TransientError, so doing that without adding the name to the state's Retry
#: list would REMOVE retryability that exists today), and neither half is in this
#: change.
TRANSIENT_CONVERSION_EXEMPT = {
    "OCRStep": (
        "Calls Bedrock only when ocr.backend is 'bedrock'; on Textract it makes no "
        "model call at all, so the conversion changes behaviour for one backend and "
        "not the other and wants measuring per backend before it lands. Handler "
        "raises rather than swallowing, so a throttle is visible and diagnosed and "
        "what is lost is the retry. Deferred in #1142."
    ),
    "ClassificationStep": (
        "One model call per page on the page-level path and one per document on the "
        "holistic one, so a throttle here costs a whole classification pass rather "
        "than one section and the retry is worth more than on the per-section states. "
        "Handler raises. Deferred in #1142 because the Retry entry and the handler's "
        "raise_if_transient have to land together."
    ),
    "SummarizationStep": (
        "A single model call over the whole document at the end of the pipeline, so a "
        "throttle wastes every stage before it. Handler raises. Deferred in #1142 "
        "because the Retry entry and the handler's raise_if_transient have to land "
        "together."
    ),
    "EvaluationStep": (
        "Reaches Bedrock through the LLM-judge comparators rather than directly, which "
        "is why a source scan does not see it and why its conversion needs the "
        "comparator path checked rather than just the handler. Evaluation is advisory, "
        "so a lost retry costs a report rather than a document. Deferred in #1142."
    ),
    "RecordEvaluationFailure": (
        "Same Lambda as EvaluationStep but on a deliberately shorter transient tier "
        "(5/3/2.0 against the 10/8/2.5 the others carry), because it runs on the "
        "failure path and must not extend an already-failing execution. That shorter "
        "ladder is the reason it is listed separately here. Deferred with it in #1142."
    ),
}


def _placeholder(state: dict) -> str | None:
    """The ``${Name}`` a task state invokes, from either shape the ASL uses."""
    for candidate in (
        state.get("Resource"),
        (state.get("Parameters") or {}).get("FunctionName"),
    ):
        m = re.fullmatch(r"\$\{([A-Za-z0-9]+)\}", str(candidate or ""))
        if m:
            return m.group(1)
    return None


def _walk(states: dict, path: str = ""):
    for name, defn in states.items():
        if not isinstance(defn, dict):
            continue
        yield f"{path}{name}", defn
        for key in ("ItemProcessor", "Iterator"):
            inner = defn.get(key)
            if isinstance(inner, dict) and isinstance(inner.get("States"), dict):
                yield from _walk(inner["States"], f"{path}{name}/")
        for branch in defn.get("Branches") or []:
            if isinstance(branch, dict) and isinstance(branch.get("States"), dict):
                yield from _walk(branch["States"], f"{path}{name}/")


TEMPLATE_PATH = ASL_PATH.resolve().parents[1] / "template.yaml"


@pytest.fixture(scope="module")
def template_text() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def substitutions(template_text: str) -> dict:
    """``DefinitionSubstitutions``: ``${Placeholder}`` -> Lambda logical id."""
    return dict(
        re.findall(
            r"^\s{8}([A-Za-z0-9]+):\s*!GetAtt\s+([A-Za-z0-9]+)\.Arn\s*$",
            template_text,
            re.M,
        )
    )


@pytest.fixture(scope="module")
def resource_blocks(template_text: str) -> dict:
    """Each top-level template resource's own body, by logical id."""
    blocks: dict[str, list[str]] = {}
    current = None
    for line in template_text.split("\n"):
        m = re.match(r"^  ([A-Za-z0-9]+):\s*$", line)
        if m:
            current = m.group(1)
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return {k: "\n".join(v) for k, v in blocks.items()}


@pytest.fixture(scope="module")
def task_targets(states, substitutions) -> dict:
    """Every task state -> the Lambda logical id it invokes."""
    targets = {}
    for name, defn in _walk(states):
        if defn.get("Type") != "Task":
            continue
        placeholder = _placeholder(defn)
        if not placeholder:
            continue
        logical_id = substitutions.get(placeholder)
        if logical_id:
            targets[name] = logical_id
    return targets


@pytest.fixture(scope="module")
def bedrock_capable_states(task_targets, resource_blocks) -> dict:
    """Task state -> logical id, for every state permitted to invoke a model."""
    return {
        name: logical_id
        for name, logical_id in task_targets.items()
        if _can_invoke_a_model(logical_id, resource_blocks)
    }


def test_every_task_state_resolves_to_a_lambda(states, substitutions, resource_blocks):
    """The check that makes the closure trustworthy.

    A state whose placeholder, substitution or resource block does not resolve drops
    silently out of the universe — which is how a hardcoded list stays green while
    missing a member. Each leg is asserted separately so a failure names which one.
    """
    unresolved = {"placeholder": [], "substitution": [], "resource": []}
    for name, defn in _walk(states):
        if defn.get("Type") != "Task":
            continue
        placeholder = _placeholder(defn)
        if not placeholder:
            unresolved["placeholder"].append(name)
            continue
        logical_id = substitutions.get(placeholder)
        if not logical_id:
            unresolved["substitution"].append((name, placeholder))
            continue
        if logical_id not in resource_blocks:
            unresolved["resource"].append((name, logical_id))
    assert not any(unresolved.values()), unresolved


def test_the_universe_is_not_vacuous(bedrock_capable_states):
    """If the derivation returns nothing, every assertion below passes for free."""
    assert len(bedrock_capable_states) >= 8, sorted(bedrock_capable_states)


def test_the_predicate_excludes_a_state_that_cannot_invoke_a_model(
    substitutions, resource_blocks, bedrock_capable_states
):
    """Both directions, or the predicate is just "every task state".

    ``PolicyClassificationStep``'s function grants no Bedrock action and its handler
    references none, so it must NOT be in the universe — while still being allowed to
    list ``TransientError``, which it does, for DynamoDB and S3 transients.
    """
    assert "PolicyClassificationStep" not in bedrock_capable_states
    assert "PolicyClassificationStep" in TASKS


def test_every_task_target_role_is_readable(task_targets, resource_blocks):
    """Every task target's grants must be reachable — checked over ALL targets.

    Iterating the selected universe instead would be circular: a function whose grants
    moved out of reach simply leaves the universe and is never examined, so the check
    that makes the predicate safe would pass by not looking. Both attachment styles
    occur here — ``PipelineHooksDispatcherFunction`` uses an external role and backs
    seven task states — so what has to hold is that any role a target references is a
    block this walk can read.
    """
    unreadable = []
    for name, logical_id in sorted(task_targets.items()):
        body = resource_blocks.get(logical_id)
        if body is None:
            unreadable.append((name, logical_id, "no resource block"))
            continue
        for role in re.findall(r"Role:\s*!GetAtt\s+([A-Za-z0-9]+)\.Arn", body):
            if role not in resource_blocks:
                unreadable.append((name, logical_id, f"role {role} not readable"))
    assert not unreadable, (
        "these task targets attach permissions this walk cannot read, so their Bedrock "
        f"grants are invisible and they would drop out of the universe: {unreadable}"
    )


def test_an_external_role_is_followed(resource_blocks):
    """Not vacuous: the role-following branch must actually be exercised.

    If no target used an external role, the branch above would be dead code and the
    predicate would be one template edit away from silently under-reporting.
    """
    external = [
        lid
        for lid, body in resource_blocks.items()
        if lid.endswith("Function")
        and re.search(r"Role:\s*!GetAtt\s+[A-Za-z0-9]+\.Arn", body)
    ]
    assert external, (
        "no function attaches an external role, so _can_invoke_a_model's "
        "role-following branch is untested"
    )


def test_an_arn_region_wildcard_is_not_read_as_an_action(resource_blocks):
    """The specific unsoundness this predicate was changed to avoid.

    ``arn:${AWS::Partition}:bedrock:*::foundation-model/*`` is an ARN whose REGION is a
    wildcard; there is no ``bedrock:*`` action in this template. A predicate matching
    raw block text reads the former as the latter, which put a data-automation-only
    function in the universe.
    """
    arn_carriers = [lid for lid, body in resource_blocks.items() if "bedrock:*" in body]
    assert arn_carriers, "no ARN wildcard region present; this test is now vacuous"
    for lid in arn_carriers:
        assert "bedrock:*" not in _granted_actions(resource_blocks[lid]), (
            f"{lid}: an ARN's wildcard region was read as a granted action"
        )


def test_every_bedrock_capable_state_converts_or_is_registered(
    states, bedrock_capable_states
):
    """The closure itself: no member may be in neither set.

    A new Bedrock-calling task state that lists nothing fails here, which is the
    direction a hardcoded list cannot check.
    """
    gaps = []
    for name in sorted(bedrock_capable_states):
        listed = "TransientError" in {
            n
            for r in (_find_state(states, name.split("/")[-1]).get("Retry") or [])
            for n in r["ErrorEquals"]
        }
        if not listed and name not in TRANSIENT_CONVERSION_EXEMPT:
            gaps.append(name)
    assert not gaps, (
        "these states are permitted to invoke a Bedrock model and neither retry "
        "TransientError nor carry a reason in TRANSIENT_CONVERSION_EXEMPT. Add the "
        "name to the state's Retry list AND raise_if_transient to its handler "
        f"together, or register why not: {gaps}"
    )


def test_no_exemption_is_dead(bedrock_capable_states, states):
    """Staleness, both ways.

    An entry naming a state that is no longer Bedrock-capable, or that now converts,
    is shielding nothing and pre-exempts whatever next takes the name.
    """
    not_in_universe = sorted(
        set(TRANSIENT_CONVERSION_EXEMPT) - set(bedrock_capable_states)
    )
    assert not not_in_universe, (
        f"exempted but not Bedrock-capable, so the entry is dead: {not_in_universe}"
    )
    now_converting = [
        name
        for name in TRANSIENT_CONVERSION_EXEMPT
        if "TransientError"
        in {
            n
            for r in (_find_state(states, name.split("/")[-1]).get("Retry") or [])
            for n in r["ErrorEquals"]
        }
    ]
    assert not now_converting, (
        f"these now convert, so delete their exemption entries: {now_converting}"
    )


def test_every_exemption_carries_a_reason():
    """A reason per member, not one attached to the set wearing per-member clothing.

    Length and a substring are not enough: six identical strings would satisfy both,
    which is exactly the shape this registry exists to prevent. So distinctness is
    asserted too.
    """
    for name, reason in TRANSIENT_CONVERSION_EXEMPT.items():
        assert len(reason) > 60, f"{name}'s reason is too short to be one: {reason!r}"
        assert "1142" in reason, f"{name}'s reason should say where it is tracked"
    reasons = list(TRANSIENT_CONVERSION_EXEMPT.values())
    assert len(set(reasons)) == len(reasons), (
        "two members share a byte-identical reason, so it is a set-level justification "
        "rather than a per-member one: "
        + str(sorted({r for r in reasons if reasons.count(r) > 1}))
    )
