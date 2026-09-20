# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An event cannot supply the identity the group check reads (issue #978).

``idp_common.api_adapter.normalize_event`` is what every group check on the API
path ultimately trusts: the resolvers and the dispatcher's ``authz.py`` read
``identity.claims['cognito:groups']`` off the event that function returns. It used
to return an event that carried its own top-level ``arguments`` and ``identity``
**unchanged**, so for any invocation of that shape the caller stated its own group
membership and the authorization decision was made against the caller's own claim.

AppSync was removed in release 0.6.0, so that shape no longer arrives from a
transport that authenticated anybody. The only caller that can present one is a
principal holding ``lambda:InvokeFunction`` on the function directly — which makes
this a privilege-escalation amplifier rather than an open door, and exactly the
kind of thing that should not be left to a comment.

Two properties are asserted, and both are derived from the tree at test time
rather than restated here:

* **The behaviour.** The adapter is loaded *by path* and driven directly: an event
  that asserts its own identity is refused, an event whose assertion contradicts
  the verified claims is refused, and the IAM-gated backend shape (a null
  ``identity``) still passes through untouched. Loading by path rather than with
  ``import idp_common`` matters — an editable install in the environment can
  resolve to a different checkout, which would assert against someone else's file.

* **Every consumer renders the refusal as a denial.** The consumers are discovered
  by parsing the tree for calls to ``normalize_event``, so a second entry point
  added later is covered the moment it exists. A consumer that lets the refusal
  escape would turn a 403 into an unhandled exception (a bodiless 502 through API
  Gateway), which is a worse answer to the same request.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest
from repo_files import tracked_paths

pytestmark = pytest.mark.unit

_ADAPTER_REL = Path("lib/idp_common_pkg/idp_common/api_adapter.py")

# Exception names that, caught around a normalize_event call, mean the refusal is
# turned into an authorization denial rather than escaping. CallerIdentityRefused
# subclasses PermissionError, so catching either is enough.
_HANDLES_REFUSAL = frozenset(
    {"CallerIdentityRefused", "PermissionError", "Exception", "BaseException"}
)

# Directories with no deployed code in them: vendored dependencies, build output,
# and the UI's node modules. A hit in one of these says nothing about what runs.
# `scratch/` and `.claude/` are gitignored local work, and both routinely hold whole
# copies of this tree: `scratch/` collects verification worktrees and mutation-test
# mutants (`scratch/p806_verify/mutants/*/idp_common/api_adapter.py`), and
# `.claude/worktrees/` is where the assistant's agent worktrees live. A copy under
# either describes nothing that ships, but it parses like a consumer, so scanning
# them turns local debris into a red gate — 19 mutants failed this rule on the
# maintainer's tree, and 157 failures across four sibling gates came from
# `.claude/worktrees/`.
#
# `repo_files.tracked_paths` already excludes all of these, because every one is
# gitignored. The set is kept as a second filter on the **repo-relative** path so
# that the fallback walk (used only against a synthetic tree outside a checkout) and
# the git listing agree, and so a directory that is added to the tree but not to
# `.gitignore` is still skipped. It must never be matched against the ABSOLUTE path:
# an agent worktree lives at `<checkout>/.claude/worktrees/agent-*`, so from inside
# one, `.claude` appears in every absolute path and the filter discards the entire
# checkout — leaving `test_every_consumer_turns_the_refusal_into_a_denial` to trip
# its own "discovery is broken" self-guard. See repo_files.py.
_SKIP_DIR_PARTS = frozenset(
    {
        ".git",
        ".aws-sam",
        ".venv",
        "node_modules",
        "build",
        "dist",
        "__pycache__",
        "site-packages",
        "scratch",
        ".claude",
    }
)


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / _ADAPTER_REL).is_file():
            return parent
    raise RuntimeError(f"Could not locate a repo root containing {_ADAPTER_REL}")


_REPO = _repo_root()


@pytest.fixture(scope="module")
def adapter():
    """``api_adapter`` loaded from THIS checkout, not from whatever is installed."""
    path = _REPO / _ADAPTER_REL
    spec = importlib.util.spec_from_file_location("_api_adapter_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# the behaviour
# --------------------------------------------------------------------------- #
def _resolver_shaped(groups):
    """The legacy resolver event shape, with the caller asserting its own groups."""
    return {
        "arguments": {},
        "identity": {
            "claims": {"cognito:groups": list(groups), "email": "self@example.com"},
            "username": "self@example.com",
        },
        "info": {"fieldName": "listUsers"},
    }


def _gateway_shaped(groups, field="listUsers"):
    """A REST proxy event with the claims where the Cognito authorizer puts them."""
    return {
        "resource": "/op/{field}",
        "httpMethod": "POST",
        "pathParameters": {"field": field},
        "body": '{"arguments": {}}',
        "requestContext": {
            "resourcePath": "/op/{field}",
            "httpMethod": "POST",
            "authorizer": {
                "claims": {
                    "sub": "11111111-2222-3333-4444-555555555555",
                    "email": "user@example.com",
                    "cognito:groups": ",".join(groups),
                }
            },
        },
    }


def test_an_event_that_asserts_its_own_identity_is_refused(adapter):
    with pytest.raises(PermissionError):
        adapter.normalize_event(_resolver_shaped(["Admin"]))


def test_an_assertion_that_contradicts_the_verified_claims_is_refused(adapter):
    event = _gateway_shaped(["Viewer"])
    event["arguments"] = {}
    event["identity"] = {"claims": {"cognito:groups": ["Admin"]}}
    with pytest.raises(PermissionError):
        adapter.normalize_event(event)


def test_the_verified_claims_are_what_the_group_check_ends_up_reading(adapter):
    """The positive half: a real caller's groups still arrive as a list."""
    out = adapter.normalize_event(_gateway_shaped(["Admin", "Author"]))
    assert out["identity"]["claims"]["cognito:groups"] == ["Admin", "Author"]


def test_the_iam_gated_backend_shape_still_passes_through(adapter):
    """A null identity is the backend marker; refusing it would break those paths."""
    event = {"arguments": {}, "identity": None, "info": {"fieldName": "getDocument"}}
    assert adapter.normalize_event(event) is event


# --------------------------------------------------------------------------- #
# every consumer renders the refusal as a denial
# --------------------------------------------------------------------------- #
def _is_staged_library_copy(path: Path, root: Path) -> bool:
    """True for a build-staging copy of ``idp_common_pkg`` outside ``lib/``.

    ``feature-platform/idp-data-generator`` stages the library into its own build
    context (``package_agent_source.sh``), because a Docker build cannot reference
    ``lib/`` by relative path. Those copies are gitignored and regenerated from
    ``lib/`` at build time, so a stale one on a developer's tree describes nothing
    that ships — what ships is whatever ``lib/`` says when the image is built. The
    canonical file is excluded separately, by exact path.
    """
    canonical = (root / _ADAPTER_REL).parents[1]  # lib/idp_common_pkg
    for parent in path.parents:
        if parent.name == "idp_common_pkg" and parent != canonical:
            return True
    return False


def _python_sources(root: Path | None = None):
    """Every deployed ``.py`` file in the checkout at ``root``.

    ``root`` is a parameter so the sweep can be driven against a synthetic tree —
    ``test_repo_walk_guards_prune_local_work.py`` points it at a checkout whose path
    contains ``.claude`` and asserts the result is not empty, which is the failure
    this discovery had.
    """
    root = (root or _REPO).resolve()
    for path in tracked_paths(root, "*.py"):
        parts = set(path.relative_to(root).parts)
        if parts & _SKIP_DIR_PARTS:
            continue
        if path.name.startswith("test_") or "tests" in parts:
            continue
        if path.resolve() == (root / _ADAPTER_REL).resolve():
            continue
        if _is_staged_library_copy(path, root):
            continue
        yield path


def _normalize_event_calls(tree: ast.AST):
    """Every ``normalize_event(...)`` call in a module, with its ancestor chain."""
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name != "normalize_event":
            continue
        chain = []
        cur: ast.AST | None = node
        while cur is not None:
            chain.append(cur)
            cur = parents.get(cur)
        yield node, chain


def _refusal_is_handled(chain) -> bool:
    """True if the call sits in a ``try`` body whose handlers catch the refusal."""
    for child, parent in zip(chain, chain[1:]):
        if not isinstance(parent, ast.Try):
            continue
        if child not in parent.body:
            continue  # in an except/else/finally clause of that try, not its body
        for handler in parent.handlers:
            if handler.type is None:
                return True
            names = (
                [handler.type]
                if not isinstance(handler.type, ast.Tuple)
                else list(handler.type.elts)
            )
            for name_node in names:
                label = (
                    name_node.id
                    if isinstance(name_node, ast.Name)
                    else name_node.attr
                    if isinstance(name_node, ast.Attribute)
                    else ""
                )
                if label in _HANDLES_REFUSAL:
                    return True
    return False


def test_every_consumer_turns_the_refusal_into_a_denial():
    """Discovered by parsing the tree, so a new entry point is covered on arrival."""
    consumers: dict[str, list[int]] = {}
    unhandled: list[str] = []

    for path in _python_sources():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for call, chain in _normalize_event_calls(tree):
            rel = str(path.relative_to(_REPO))
            consumers.setdefault(rel, []).append(call.lineno)
            if not _refusal_is_handled(chain):
                unhandled.append(f"{rel}:{call.lineno}")

    assert consumers, (
        "no consumer of api_adapter.normalize_event was found anywhere in the "
        "tree — the discovery in this guard is broken, so it proves nothing"
    )
    assert not unhandled, (
        "normalize_event refuses an invocation that asserts its own identity by "
        "raising CallerIdentityRefused (a PermissionError). These call sites do "
        f"not catch it, so the refusal would escape as a 5xx: {sorted(unhandled)}. "
        f"Consumers found: {sorted(consumers)}"
    )
