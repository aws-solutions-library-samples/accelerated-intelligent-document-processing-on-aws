# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every operation that admits `Annotator` must also confine it per object.

`Annotator` is the one role in this API whose authorization has two halves. The
field-level group policy decides which operations the role reaches; a per-object
control inside the resolver then decides which *objects* it reaches, by matching the
document's originating test set against the caller's `allowedTestSets`. Holding the
group is not holding the object, and the two halves are written in different files.

The defect this module exists for is a **missing member of a set**, not a mistake in
any one resolver. `updateDocumentSections` was given the group policy of
`completeSectionReview` — the sibling that edits a section's content — on the
reasoning that re-grouping a packet is the same kind of annotation work. The group
list was copied across and the per-object check was not, and nothing anywhere
compared the two: the operation admitted the role and confined nothing, while its
three siblings in the same policy confined it. Fixing that one resolver leaves the
next operation given the same group list in exactly the same state, so what is
asserted here is the property over the whole set.

The set is **derived** from `scripts/api_rbac_expectations.yaml` at run time —
every operation whose `groups:` admit `Annotator` — rather than listed. A list of
four names would have been satisfied by the fix and would say nothing about the
fifth operation to admit the role.

**How "applies the check" is decided.** By reusing `scan_api_rbac.op_scope_source`,
which is the scanner's own reader: it resolves the operation's entry point (its
dispatch branch, or the function named in `scope_enforced_in:`) and returns the
transitive closure of module-level functions that entry point can call. Reading the
whole `enforced_in` FILE instead is what let a false `scope_filtered` declaration
pass the scanner for months on the strength of a sibling operation's string in the
same module, and eight of the operations below share one file — so a whole-file read
here would be satisfied for all eight by any one of them.

**What this does not claim.** It is a reference check over source, not dataflow: it
establishes that the code an operation can reach calls the scope rule, not that the
refusal is on the right branch or that the answer is used. That is what the per-site
suites assert — `tests/unit/lambdas/test_update_document_sections.py` for this
operation (an out-of-scope annotator refused, an in-scope one admitted, a failed
lookup refused), and the live matrix's `object_scoped_roles` cells for the deployed
article. What this catches, and did, is an operation that calls the rule **nowhere**.

No AWS and no network.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]
_EXPECTATIONS = _REPO / "scripts" / "api_rbac_expectations.yaml"
_SCANNER = _REPO / "scripts" / "sdlc" / "scan_api_rbac.py"

# The role with two halves to its authorization.
ANNOTATOR = "Annotator"

# The canonical per-object rule, `idp_common.testset_scope.assert_can_access_test_set`.
# One spelling, because there is deliberately one implementation: a scope matcher that
# differs between call sites admits somewhere what it denies elsewhere. A resolver that
# reached the rule under some other name would fail here, which is the right outcome —
# the remedy is to call the shared rule, not to widen this.
TESTSET_RULE = "assert_can_access_test_set"

# The helper that derives the scope subject from a *document* rather than from a test
# set the caller named: it reads the named document's `TestSetId` off its tracking-table
# record. Operations whose check is this one cannot be given an argument the control
# admits without a real document in a real test set, which is why they are the ones that
# declare `object_scoped_roles:` for the live matrix. Both files that define it use this
# name.
DOCUMENT_SUBJECT_HELPER = "_assert_annotator_scope"

# The scanner's own patterns for the OTHER scope axis, `allowedConfigVersions`. Imported
# from the scanner below rather than restated; named here only so the reason this module
# consults a second axis at all is written down: one Annotator-admitting operation is a
# configuration read, and a configuration read is partitioned per caller by profile, not
# by test set. See `test_a_read_scoped_on_the_configuration_axis_is_scoped_per_object`.
_CONFIG_AXIS = "allowedConfigVersions"

# Annotators also work production review-queue items (HITL review queues). Those
# items belong to no test set, so `allowedTestSets` has nothing to match; what
# confines the role per object is queue membership — the queue's eligible
# Annotators list, intersected with `allowedConfigVersions`, plus claim ownership
# for drafts and dispositions. The resolver hands every such operation to
# `ReviewQueueService`, so the axis is measured there: the service methods an
# operation calls must reach one of these, and each is asserted below to call both
# halves of the membership rule.
_REVIEW_QUEUE_RESOLVER = (
    "nested/api-resolvers/src/lambda/review_queues_resolver/index.py"
)
_REVIEW_QUEUE_SERVICE = _REPO / "lib/idp_common_pkg/idp_common/review_queues/service.py"
_REVIEW_QUEUE_SERVICE_CLASS = "ReviewQueueService"
REVIEW_QUEUE_MEMBERSHIP_RULES = ("_check_access", "_caller_refs")
# Each rule's two halves: the caller's `allowedConfigVersions`, and the queue's
# eligible Annotators. An item is scoped by its own profile (`_sees`, over
# `scope_allows`); a queue reference, which has no profile, by `in_scope`.
_MEMBERSHIP_HALVES = {
    "_check_access": ("self._sees(", "self.directory.is_member("),
    "_caller_refs": ("self.directory.in_scope(", "self.directory.is_member("),
}
_ITEM_SCOPE_RULE = "_sees"

# Test Studio queue reads an Annotator reaches across sets, which a single
# `testSetId` cannot confine: the queue list is filtered to the sets whose queue
# lists the caller, and My Work › Tasks to the caller's own assignments, keyed on
# the verified-claims email. Read axes only: a write to a test-set document is
# still required to reach the test-set rule.
TEST_SET_QUEUE_LISTING_RULE = "_queue_annotators("
OWN_ASSIGNMENTS_RULE = ("caller_email(event)", ".assigned_to(caller)")

# Axes that confine a WRITE per object: the document's test set, or the review
# queue item's queue membership and ownership.
_MUTATION_AXES = {"test-set", "review-queue"}


def _load_scanner():
    """The scanner module, loaded by path.

    `scripts/sdlc/` is not a package, and the scanner is a CLI script; loading it by
    path is what the sibling suites in this directory do. Nothing in it runs at import
    beyond constant and function definitions.
    """
    spec = importlib.util.spec_from_file_location("_scan_api_rbac_for_scope", _SCANNER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_scan_api_rbac_for_scope"] = mod
    spec.loader.exec_module(mod)
    return mod


scanner = _load_scanner()


def _operations() -> dict:
    return yaml.safe_load(_EXPECTATIONS.read_text())["operations"]


def _annotator_operations(ops: dict) -> dict:
    """Every operation whose field-level policy admits `Annotator`.

    `groups:` is a list for a group-gated operation and a bare marker (`ANY`,
    `IAM_ONLY`) otherwise; only the list form can name a role.
    """
    return {
        name: cfg
        for name, cfg in ops.items()
        if isinstance(cfg.get("groups"), list) and ANNOTATOR in cfg["groups"]
    }


def _reachable_source(name: str, cfg: dict, ops: dict) -> tuple[str | None, str]:
    """`(error, source)` for the code `name` can reach — the scanner's own reader."""
    src = _REPO / cfg["enforced_in"]
    if not src.exists():
        return f"enforced_in does not exist: {cfg['enforced_in']}", ""
    return scanner.op_scope_source(
        src.read_text(),
        name,
        declared_entry=cfg.get("scope_enforced_in"),
        sole_op=scanner.operations_per_enforced_file(ops)[cfg["enforced_in"]] == 1,
    )


def _service_methods() -> dict[str, ast.FunctionDef]:
    """`ReviewQueueService`'s methods, by name."""
    tree = ast.parse(_REVIEW_QUEUE_SERVICE.read_text())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == _REVIEW_QUEUE_SERVICE_CLASS:
            return {
                item.name: item
                for item in node.body
                if isinstance(item, ast.FunctionDef)
            }
    raise AssertionError(f"{_REVIEW_QUEUE_SERVICE_CLASS} not found in service.py")


def _self_calls(node: ast.AST) -> set[str]:
    return {
        sub.func.attr
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Attribute)
        and isinstance(sub.func.value, ast.Name)
        and sub.func.value.id == "self"
    }


def _service_closure(entry: str, methods: dict[str, ast.FunctionDef]) -> set[str]:
    """Every service method `entry` can reach through `self.<method>(...)` calls."""
    seen: set[str] = set()
    pending = [entry]
    while pending:
        name = pending.pop()
        if name in seen or name not in methods:
            continue
        seen.add(name)
        pending.extend(_self_calls(methods[name]))
    return seen


def _review_queue_field_source(name: str) -> str:
    """The resolver function the `_FIELDS` table maps `name` to, with its closure."""
    text = (_REPO / _REVIEW_QUEUE_RESOLVER).read_text()
    for node in ast.parse(text).body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id == "_FIELDS"):
            continue
        assert isinstance(node.value, ast.Dict)
        for key, value in zip(node.value.keys, node.value.values):
            if isinstance(key, ast.Constant) and key.value == name:
                assert isinstance(value, ast.Name), f"{name}: dispatch is not a name"
                error, source = scanner.op_scope_source(
                    text, name, declared_entry=value.id, sole_op=False
                )
                assert not error, f"{name}: {error}"
                return source
    return ""


def _reaches_review_queue_membership(name: str, cfg: dict) -> bool:
    if cfg["enforced_in"] != _REVIEW_QUEUE_RESOLVER:
        return False
    called = set(re.findall(r"_SERVICE\.(\w+)\(", _review_queue_field_source(name)))
    methods = _service_methods()
    reached = set().union(*(_service_closure(m, methods) for m in called))
    return bool(reached & set(REVIEW_QUEUE_MEMBERSHIP_RULES))


def _scope_axes(name: str, cfg: dict, ops: dict) -> tuple[str | None, set[str]]:
    """`(error, axes)` — which per-object scope axes this operation's code reaches."""
    error, source = _reachable_source(name, cfg, ops)
    if error:
        return error, set()
    axes = set()
    if TESTSET_RULE in source:
        axes.add("test-set")
    if any(pattern in source for pattern in scanner.SCOPE_PATTERNS):
        axes.add("config-version")
    if _reaches_review_queue_membership(name, cfg):
        axes.add("review-queue")
    if TEST_SET_QUEUE_LISTING_RULE in source:
        axes.add("test-set-queue-listing")
    if all(rule in source for rule in OWN_ASSIGNMENTS_RULE):
        axes.add("own-assignments")
    return None, axes


# ---------------------------------------------------------------------------
# The derivation itself, before anything is asserted about its members
# ---------------------------------------------------------------------------
def test_the_derivation_produces_the_operations_that_share_the_review_policy():
    """A silently empty derivation would make every test below vacuously true.

    So the derivation is asserted first, and asserted by NAME rather than by count:
    a count is the brittle half (an operation removed from the API moves it) and
    the weak half (any four names satisfy it). These four share one group policy —
    `[Admin, Reviewer, Annotator]` — and are the set the copied group list came
    from, so a derivation that stops producing them has stopped reading `groups:`.
    """
    derived = _annotator_operations(_operations())

    assert derived, (
        "no operation in the expectations file admits Annotator, which cannot be "
        "true while the role exists — the derivation is reading the wrong key and "
        "every assertion over it is vacuous"
    )
    assert {
        "completeSectionReview",
        "claimReview",
        "releaseReview",
        "updateDocumentSections",
    } <= set(derived), (
        "the four operations sharing the [Admin, Reviewer, Annotator] policy are not "
        f"all in the derived set (got {sorted(derived)})"
    )


# ---------------------------------------------------------------------------
# The property: admitting the role means confining it
# ---------------------------------------------------------------------------
def test_every_operation_admitting_an_annotator_is_scoped_per_object():
    """The closure. No operation admits the role and confines nothing.

    Stated over both axes because one member is a configuration read, which is
    partitioned per caller by profile rather than by test set. The axis each
    operation uses is MEASURED from its reachable code, not declared, so an
    operation cannot satisfy this by naming an axis it does not reach.
    """
    ops = _operations()
    unscoped = []
    unreadable = []
    for name, cfg in sorted(_annotator_operations(ops).items()):
        error, axes = _scope_axes(name, cfg, ops)
        if error:
            unreadable.append(f"{name}: {error}")
        elif not axes:
            unscoped.append(f"{name} ({cfg['enforced_in']})")

    assert not unreadable, (
        "the entry point could not be determined for an operation that admits "
        "Annotator, so whether it confines the role is unknown — add "
        "`scope_enforced_in: <function>` to its entry:\n  " + "\n  ".join(unreadable)
    )
    assert not unscoped, (
        "these operations admit Annotator and nothing they can reach calls a "
        "per-object scope rule, so the role is confined by its group and by nothing "
        "else:\n  " + "\n  ".join(unscoped)
    )


def test_every_mutation_admitting_an_annotator_is_scoped_on_the_test_set_axis():
    """A write by an Annotator is confined by the object it writes, specifically.

    The axis matters, not merely that there is one. A test-set document is
    partitioned by `allowedTestSets`; a production review-queue item belongs to no
    test set and is partitioned by the queue's eligible Annotators and its claim
    owner (`ReviewQueueService._check_access` / `_caller_refs`, both of which also
    apply `allowedConfigVersions`). Every mutation the role can reach writes one of
    those two kinds of object. Satisfying the test above on the configuration axis
    alone would mean a write confined by which configuration profiles the caller may
    read, which answers a different question; a test-set queue listing alone would
    leave a write to a test-set document unconfined by `allowedTestSets`.

    This is the assertion `updateDocumentSections` failed: a mutation, admitting the
    role, reaching no test-set rule at all.
    """
    ops = _operations()
    offenders = []
    for name, cfg in sorted(_annotator_operations(ops).items()):
        if cfg.get("kind") != "mutation":
            continue
        error, axes = _scope_axes(name, cfg, ops)
        assert not error, f"{name}: {error}"
        if not axes & _MUTATION_AXES:
            offenders.append(
                f"{name} ({cfg['enforced_in']}) reaches {sorted(axes) or 'no'} scope "
                f"axis but never calls {TESTSET_RULE} (or, for a review-queue item, "
                f"one of {REVIEW_QUEUE_MEMBERSHIP_RULES})"
            )

    assert not offenders, (
        "a mutation an Annotator may call must confine the role to its "
        "allowedTestSets, or to the review queues that list it:\n  "
        + "\n  ".join(offenders)
    )


def test_the_review_queue_membership_rules_check_scope_and_membership():
    """The review-queue axis rests on code, so the code is asserted too.

    Each rule credited as the review-queue axis must call both halves of the rule:
    the caller's `allowedConfigVersions` (`_sees` for an item, by its own profile;
    `in_scope` for a queue reference) and the queue's eligible Annotators
    (`is_member`). A rule that lost either half would still be credited
    by the closure check above, so it is pinned here. And the axis must be reached by
    at least one Annotator mutation, or it credits nothing and this is vacuous.
    """
    methods = _service_methods()
    for rule in REVIEW_QUEUE_MEMBERSHIP_RULES:
        assert rule in methods, f"{_REVIEW_QUEUE_SERVICE_CLASS}.{rule} not found"
        body = ast.unparse(methods[rule])
        for half in _MEMBERSHIP_HALVES[rule]:
            assert half in body, f"{rule} no longer calls {half}"
    # The item rule compares the item's own profile with `scope_allows`.
    assert _ITEM_SCOPE_RULE in methods, (
        f"{_REVIEW_QUEUE_SERVICE_CLASS}.{_ITEM_SCOPE_RULE} not found"
    )
    item_rule = ast.unparse(methods[_ITEM_SCOPE_RULE])
    assert "scope_allows(caller.allowed_versions, record.profile)" in item_rule, (
        f"{_ITEM_SCOPE_RULE} no longer compares the item's own profile with scope_allows"
    )

    ops = _operations()
    credited = [
        name
        for name, cfg in _annotator_operations(ops).items()
        if cfg.get("kind") == "mutation" and _reaches_review_queue_membership(name, cfg)
    ]
    assert credited, (
        "no Annotator mutation reaches the review-queue membership rules, so the "
        "review-queue axis credits nothing — the dispatch table or the service "
        "method names have changed"
    )


def test_a_read_scoped_on_the_configuration_axis_is_scoped_per_object():
    """The one Annotator-admitting operation on the other axis, and why it holds.

    `getConfigVersion` admits Annotator so the annotate view can show the class list
    needed to correct a misclassified section. It names a configuration profile, not
    a document, so `allowedTestSets` has nothing to match and the axis that confines
    it is `allowedConfigVersions`.

    That reasoning is one operation's worth, and it is **computed rather than
    asserted in prose**: any Annotator-admitting operation that reaches the
    configuration axis and not the test-set one must be a `read` (a write confined by
    read scope is caught by the test above), must declare the configuration axis in
    `scope_checked`/`scope_filtered` so the scanner's S4 check evaluates it too, and
    must reach the axis in code. A future operation that cannot meet all three fails
    here instead of inheriting this one's reason.
    """
    ops = _operations()
    checked = 0
    for name, cfg in sorted(_annotator_operations(ops).items()):
        error, axes = _scope_axes(name, cfg, ops)
        assert not error, f"{name}: {error}"
        if axes != {"config-version"}:
            continue
        checked += 1
        assert cfg.get("kind") == "read", (
            f"{name} is a {cfg.get('kind')} confined only by the caller's "
            f"{_CONFIG_AXIS}, which is read scope"
        )
        assert cfg.get("scope_checked") or cfg.get("scope_filtered"), (
            f"{name} is confined only by the caller's {_CONFIG_AXIS} but declares "
            "neither scope_checked nor scope_filtered, so the scanner's S4 check "
            "does not evaluate the one axis it has"
        )

    assert checked, (
        "no Annotator-admitting operation is confined on the configuration axis "
        "alone, so the premises above hold over nothing — if getConfigVersion has "
        "gained a test-set check or stopped admitting Annotator, delete this test "
        "rather than leaving it to pass vacuously"
    )


# ---------------------------------------------------------------------------
# The declaration side: what the live matrix is told to assert
# ---------------------------------------------------------------------------
def test_a_document_subject_check_is_declared_for_the_live_matrix():
    """An operation whose check reads the DOCUMENT's test set declares the cell.

    `object_scoped_roles:` is what makes the live matrix assert the per-object
    refusal rather than score the cell as a group-floor failure. It is not wanted
    everywhere: an operation the matrix can hand an in-scope argument should get one
    instead. These cannot be handed one, and the reason is a property of the check
    they use — `_assert_annotator_scope` resolves the subject from the named
    document's tracking-table record, so the only admitting argument is a real
    document in a real test set, which the matrix would then have Admin and Reviewer
    perform a real review mutation against.

    So the membership is derived from the check, not from a list of operations: every
    operation reaching that helper declares the cell. `updateDocumentSections` is the
    member that was missing — the declaration omitted alongside the check itself, so
    the live matrix scored its Annotator cell as an ordinary allowed-role pass.
    """
    ops = _operations()
    missing = []
    found = []
    for name, cfg in sorted(_annotator_operations(ops).items()):
        error, source = _reachable_source(name, cfg, ops)
        assert not error, f"{name}: {error}"
        if DOCUMENT_SUBJECT_HELPER not in source:
            continue
        found.append(name)
        if ANNOTATOR not in (cfg.get("object_scoped_roles") or []):
            missing.append(
                f"{name} ({cfg['enforced_in']}) refuses an out-of-scope Annotator via "
                f"{DOCUMENT_SUBJECT_HELPER}, but does not declare "
                "`object_scoped_roles: [Annotator]`, so the live matrix reads its "
                "refusal as a group-floor failure or its admission as a pass"
            )

    assert found, (
        f"no operation reaches {DOCUMENT_SUBJECT_HELPER}, so this test asserts "
        "nothing — the helper has been renamed, or the derivation is reading the "
        "wrong operations"
    )
    assert not missing, "\n  ".join([""] + missing)
