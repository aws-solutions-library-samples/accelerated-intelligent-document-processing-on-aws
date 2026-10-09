# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The API boundary must not answer 500 for a resource that does not exist.

The dispatcher picks an HTTP status from the *class name* of the exception a
resolver raised, so a refusal that raises a bare ``Exception`` can only reach the
caller as 500 ``InternalError``. Two consequences, and the second is the one that
made this worth a gate:

* A deliberate not-found is counted as a server fault by the API's 5xx signals
  (API Gateway's ``5XXError``), so ordinary use is indistinguishable from a fault
  in CloudWatch. No alarm in this repository watches that metric today, so this is
  about a signal staying readable rather than an alarm that stops firing.
* The live RBAC harness treats a 5xx as INCONCLUSIVE — correctly, since a 500 says
  nothing about whether the resolver refused this caller before or after doing the
  work. 25 of its positive authorization assertions were unproven for this reason,
  which is how a resolver's choice of exception class became an authorization
  *measurement* problem.

Issue #1304. ``test_set_resolver`` was the bulk of it (15 test-set and one
labeling-job lookup).

This is a **reintroduction guard**: it reports nothing against the tree today, and
it carries no exemption list, so there is nothing here to register in
``scripts/tests/gate_exemptions.json``. What it refuses is a *new* not-found
refusal raised as a bare ``Exception``.

⚠️ Two bounds, stated so a green run is not over-read:

* It reads the **literal text** at the raise site. A message built into a variable
  first (``error_msg = f"... not found"`` then ``raise Exception(error_msg)``) is
  invisible to it, and no textual scan can fix that in general.
* It matches not-found *wording*. A refusal phrased some other way ("no such
  test set", "unknown id") is not caught, and neither is the inverse class — a
  caller-input refusal that should be 400. Those remain a matter of review.

What it does cover exhaustively is the **mechanism**: the class, both exception
mappings, and the one coupling that is easy to break silently — the errorType
string the live harness reads to tell "this deployment lacks the feature" apart
from "that object does not exist".
"""

import ast
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

API_ADAPTER = REPO_ROOT / "lib/idp_common_pkg/idp_common/api_adapter.py"
DISPATCHER = REPO_ROOT / "nested/api-resolvers/src/lambda/http_api_dispatcher/index.py"
RBAC_HARNESS = REPO_ROOT / "scripts/test_api_rbac.py"

# The errorType a resolver-sourced 404 carries. Deliberately NOT "NotFound": the
# dispatcher already answers 404 "NotFound" for an operation that is declared but
# not routable in this deployment, and the harness's conditional-feature probe
# reads a 404 as "the feature is absent, skip this operation's row". Collapsing
# the two would silently convert authorization assertions into skips.
RESOURCE_NOT_FOUND_ERROR_TYPE = "ResourceNotFound"
UNROUTABLE_OPERATION_ERROR_TYPE = "NotFound"

# Wording that makes a message a not-found refusal.
NOT_FOUND_WORDING = re.compile(r"not found|does not exist|no longer exists", re.I)


def _lambda_sources():
    """Every resolver handler reached through the dispatcher.

    Discovered from ``git ls-files`` rather than a hardcoded list, so a new
    resolver is covered by existing, and build trees (``.aws-sam``) cannot
    contribute a stale copy of one.

    Two roots, because "reached through the dispatcher" is not a single
    directory: ``FIELD_FUNCTION_MAP`` (``nested/api-resolvers/template.yaml``)
    routes ``getFeatureLaunchUrl``, ``subscribeFeature`` and
    ``unsubscribeFeature`` to Lambdas under ``feature-platform/``. A glob over
    ``nested/`` alone would therefore read 38 of the 41 handlers while reporting
    on all of them, and the three it missed are three of the four #1304 fixes.
    """
    out = subprocess.run(
        [
            "git",
            "ls-files",
            "nested/api-resolvers/src/lambda/*/index.py",
            "feature-platform/main-stack-extensions/lambdas/*/index.py",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    paths = [REPO_ROOT / p for p in out]
    assert paths, "discovery found no resolver handlers — the glob is wrong"
    return paths


def _bare_exception_raises(path):
    """``(lineno, message_text)`` for every ``raise Exception(<literal...>)``.

    Only a bare ``Exception`` qualifies. A ``PermissionError`` naming a missing
    object is a deliberate refusal that must not reveal whether it exists
    (``get_agent_chat_messages_resolver`` does exactly that), and a ``ValueError``
    or ``ResourceNotFound`` already carries a 4xx mapping.
    """
    tree = ast.parse(path.read_text())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        func = node.exc.func
        if not (isinstance(func, ast.Name) and func.id == "Exception"):
            continue
        # Collect every string constant anywhere inside the arguments, rather than
        # matching a shape. The wording can be assembled four ways that all read
        # identically at the call site — an f-string, `"..." % x`, `"..." + x`,
        # `"...".format(x)` — and a shape-matching extractor that handles only the
        # first two silently passes the others. (Measured: a `%`-formatted probe
        # went undetected until this was recursive.)
        parts = [
            sub.value
            for arg in node.exc.args
            for sub in ast.walk(arg)
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
        ]
        found.append((node.lineno, " ".join(parts)))
    return found


@pytest.mark.unit
def test_no_resolver_reports_a_missing_resource_as_a_bare_exception():
    offenders = []
    for path in _lambda_sources():
        for lineno, message in _bare_exception_raises(path):
            if NOT_FOUND_WORDING.search(message):
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{lineno}: {message.strip()!r}")

    assert not offenders, (
        "A not-found refusal raised as a bare `Exception` reaches the caller as "
        "500 InternalError (the dispatcher maps by exception class name), which "
        "inflates the 5xx error rate and makes the live RBAC harness record the "
        "operation's authorization check as INCONCLUSIVE rather than as a pass.\n"
        "Raise `idp_common.api_adapter.ResourceNotFound` instead — it maps to 404 "
        f"with errorType {RESOURCE_NOT_FOUND_ERROR_TYPE!r}. A resolver with no "
        "idp_common dependency may declare its own class of that name; the "
        "dispatcher matches the name, not the class. See issue #1304.\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.unit
def test_discovery_covers_the_feature_platform_resolvers():
    """Discovery reaches every root the dispatcher routes into, not just one.

    This is the guard for the guard, and it exists because the failure it
    prevents is silent in the worst way: a glob that misses a root still returns
    a non-empty list, the scan above still passes, and the gate reports a clean
    tree for files it never opened. Nothing about a narrowed glob looks wrong.

    So the roots are asserted by name. ``feature-platform/`` is the one that is
    easy to drop, since the other 38 handlers live together under
    ``nested/api-resolvers/`` and these three are reached only through
    ``FIELD_FUNCTION_MAP``.
    """
    discovered = {p.relative_to(REPO_ROOT).as_posix() for p in _lambda_sources()}
    routed_elsewhere = {
        f"feature-platform/main-stack-extensions/lambdas/{name}/index.py"
        for name in (
            "get_feature_launch_url",
            "subscribe_feature",
            "unsubscribe_feature",
        )
    }
    missing = routed_elsewhere - discovered
    assert not missing, (
        "These resolvers are reached through the dispatcher (FIELD_FUNCTION_MAP in "
        "nested/api-resolvers/template.yaml) but are outside the discovery glob, so "
        "the not-found gate above does not read them:\n  "
        + "\n  ".join(sorted(missing))
    )
    # And the original root is still covered — a glob rewritten to fix the above
    # must not trade one blind spot for another.
    assert any(p.startswith("nested/api-resolvers/src/lambda/") for p in discovered), (
        "discovery lost the nested/api-resolvers root"
    )


def _handler_names(node):
    """The exception type names an `ast.Try` catches, in source order."""
    names = []
    for handler in node.handlers:
        if handler.type is None:
            names.append("<bare except>")
        elif isinstance(handler.type, ast.Name):
            names.append(handler.type.id)
        elif isinstance(handler.type, ast.Tuple):
            names += [e.id for e in handler.type.elts if isinstance(e, ast.Name)]
    return names


def _assert_not_found_arm_precedes_catch_all(path):
    """`ResourceNotFound` must be caught before `Exception` in the SAME `try`.

    Compared per-``try`` through the AST rather than by source offset: both files
    contain earlier, unrelated ``except Exception`` blocks, so a whole-file offset
    comparison answers a different question and fails on correct code.
    """
    trees = [
        node
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Try) and "ResourceNotFound" in _handler_names(node)
    ]
    rel = path.relative_to(REPO_ROOT)
    assert trees, f"{rel} catches ResourceNotFound in no try block"
    for node in trees:
        names = _handler_names(node)
        if "Exception" not in names and "<bare except>" not in names:
            continue
        catch_all = min(
            names.index(n) for n in ("Exception", "<bare except>") if n in names
        )
        assert names.index("ResourceNotFound") < catch_all, (
            f"{rel}: the try at line {node.lineno} catches ResourceNotFound after "
            "its catch-all arm, so the catch-all swallows it and the caller gets "
            "500 after all"
        )


@pytest.mark.unit
def test_the_canonical_class_exists_and_api_resolver_maps_it_to_404():
    """The in-process half of the mapping: a resolver behind `api_resolver`."""
    source = API_ADAPTER.read_text()
    assert "class ResourceNotFound" in source, (
        f"{API_ADAPTER.relative_to(REPO_ROOT)} must define the canonical "
        "ResourceNotFound; every other assertion here rests on it"
    )
    assert "except ResourceNotFound as e:" in source, (
        "api_resolver must map ResourceNotFound, or a resolver invoked through it "
        "answers 500 while the same refusal through the dispatcher answers 404 — "
        "the two mappings at this boundary have to agree"
    )
    # The arm must sit above the catch-all, or the catch-all swallows it first.
    _assert_not_found_arm_precedes_catch_all(API_ADAPTER)
    assert "404" in source, "the arm must answer 404"
    assert f'"errorType": "{RESOURCE_NOT_FOUND_ERROR_TYPE}"' in source, (
        "api_resolver's ResourceNotFound arm must carry that errorType"
    )


@pytest.mark.unit
def test_the_dispatcher_maps_a_resolver_reported_not_found_to_404():
    """The cross-Lambda half: matched by class NAME out of the invoke response."""
    source = DISPATCHER.read_text()
    assert f'err_type == "{RESOURCE_NOT_FOUND_ERROR_TYPE}"' in source, (
        "the dispatcher must recognise the resolver's errorType by name — the "
        "exception object does not cross the lambda:Invoke boundary, only "
        "data['errorType'] does"
    )
    assert "except ResourceNotFound as e:" in source, (
        "_invoke_resolver re-raises ResourceNotFound; the handler must catch it, "
        "or it falls through to the catch-all and answers 500 after all"
    )
    _assert_not_found_arm_precedes_catch_all(DISPATCHER)
    assert f'"errorType": "{RESOURCE_NOT_FOUND_ERROR_TYPE}"' in source


@pytest.mark.unit
def test_every_resolver_that_catches_not_found_catches_it_before_its_catch_all():
    """A resolver's own ``except`` order decides whether its 404 survives.

    The two checks above pin the dispatcher and the in-process adapter by name.
    Any resolver may also catch ``ResourceNotFound`` — to log it at warning rather
    than let a catch-all log it as an error — and gets the ordering requirement
    with it: an arm placed after ``except Exception`` never runs, so the catch-all
    re-raises and the status is right while the log line says ERROR, or, if the
    catch-all does not re-raise, the caller gets a 500 after all.

    Applied to whatever the discovery finds, so a resolver that grows such an arm
    is covered without being named here. Selection is by AST and not by substring:
    most resolvers mention ``ResourceNotFound`` only to *raise* it, and
    ``get_feature_launch_url`` is one of them.
    """
    for path in _lambda_sources():
        catches = any(
            isinstance(node, ast.Try) and "ResourceNotFound" in _handler_names(node)
            for node in ast.walk(ast.parse(path.read_text()))
        )
        if catches:
            _assert_not_found_arm_precedes_catch_all(path)


@pytest.mark.unit
def test_the_two_404s_keep_different_error_types():
    """The coupling that breaks silently.

    The dispatcher answers 404 for two unrelated conditions. If a resolver-sourced
    not-found started carrying the unroutable-operation errorType, the live
    harness's conditional-feature probe would read "this object does not exist" as
    "this deployment lacks the feature" and SKIP the operation's whole row — no
    authorization assertion, no finding, nothing printed. That is strictly worse
    than the 500 it replaced, which was at least counted as INCONCLUSIVE.
    """
    assert RESOURCE_NOT_FOUND_ERROR_TYPE != UNROUTABLE_OPERATION_ERROR_TYPE
    dispatcher = DISPATCHER.read_text()
    assert f'"errorType": "{UNROUTABLE_OPERATION_ERROR_TYPE}"' in dispatcher, (
        "the unroutable-operation 404 is the other side of this distinction; if "
        "its errorType changed, re-derive what the harness probe should match"
    )

    harness = RBAC_HARNESS.read_text()
    assert "_is_feature_absent_404" in harness, (
        "the harness must discriminate the two 404s rather than keying on the "
        "status alone"
    )
    assert f'!= "{RESOURCE_NOT_FOUND_ERROR_TYPE}"' in harness, (
        "the feature-absent probe must exclude a resolver-sourced not-found"
    )
    # Neither conditional probe may go back to comparing the bare status.
    assert "if st == 404:" not in harness, (
        "a conditional-feature probe is comparing the status alone again — it "
        "will skip the row for an operation whose feature IS deployed"
    )
