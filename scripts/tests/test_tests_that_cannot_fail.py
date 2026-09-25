# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Two test shapes that pass whatever the code does, gated at zero.

A test that cannot fail is worse than a missing one: it occupies the place
coverage would occupy and it reports green. #1129 found seven of them inside
suites that run on every pull request, arising from three mechanisms. Two of the
three are unambiguous AST patterns and are gated here. The third is not
detectable and is not gated; see "What this does not catch" below.

**Swallowed assertion.** An ``assert`` (or a ``mock.assert_*`` call) inside a
``try`` whose handler catches ``Exception``/``BaseException``/bare and neither
re-raises nor calls ``pytest.fail``. ``except Exception`` catches
``AssertionError``, so the assertion decides nothing; ``except Exception:
pytest.skip(...)`` is the same defect wearing a nicer hat, because a skip reads
as green. If both outcomes really are acceptable there is nothing to assert, and
the test should say which one it observed in one line instead. The remedy is
almost always to move the assertions *after* the ``try``, so the handler covers
only the call that can legitimately be unavailable.

**Self-referential fixture.** The test builds a ``Mock``, writes an attribute on
it, installs it as the ``return_value`` of a patch mock — which replaces a
constructor in production code, so the factory under test hands that very object
back — and then asserts something about the attribute it wrote. The production
value is never observed, so *any* expected value passes. The instance that
prompted this gated a tool count: the factory passed nine tools, the test
asserted seven against its own seven-element list, and it was green.

Both classes stand at **zero** instances, so this gate carries no baseline and no
exemption list. That is deliberate: a baseline here would be a list of tests
whose results are known to mean nothing, which is not a state to institutionalise
one entry at a time. If a genuinely new case arrives, the argument for it belongs
in review, not in a JSON file.

**What this does not catch**, stated so a green result is not read as more than
it is:

- The general mock-as-subject case. Deciding that a name is bound to a mock at
  the point of an assertion is type inference over test-local flow; the scanner
  written for #1129's sweep got it wrong in both directions, and typing does not
  help because ``MagicMock`` is ``Any``. Only the exact four-condition shape
  above is detected, and it is detected because each condition is a literal
  syntactic fact rather than an inference.
- A test with no assertion at all. That scan returns ~139 hits tree-wide, of
  which the large majority are legitimate "must not raise" smoke tests, so it
  needs an explicit marker convention before it can gate anything.
- A print-only report, or an expectation table nothing compares against. There
  is no syntactic difference between a loop that prints a diagnostic beside its
  assertions and one that prints instead of them.
- Coverage cannot substitute for any of this: these tests *execute* the code, so
  they are covered. They simply do not check it.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest
from repo_files import tracked_paths

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Attributes on a mock that configure the mock itself rather than standing in
#: for a production attribute. Writing one is setup, not seeding an expectation.
_MOCK_PROTOCOL_ATTRS = frozenset({"return_value", "side_effect"})

_MOCK_FACTORIES = frozenset(
    {"MagicMock", "Mock", "AsyncMock", "NonCallableMagicMock", "PropertyMock"}
)

_SWALLOWING_HANDLERS = frozenset({"Exception", "BaseException"})


def _base_name(node: ast.AST) -> str | None:
    """The leftmost ``Name`` of an attribute chain, e.g. ``a`` in ``a.b.c``."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _is_mock_construction(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    return name in _MOCK_FACTORIES


def _test_functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                yield node


def _asserts_within(body) -> list[ast.AST]:
    """Assertion statements and ``x.assert_*(...)`` calls anywhere under ``body``."""
    found = []
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Assert):
                found.append(node)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr.startswith("assert")
            ):
                found.append(node)
    return found


def _handler_catches_broadly(handler: ast.ExceptHandler) -> bool:
    caught = handler.type
    if caught is None:  # bare `except:`
        return True
    if isinstance(caught, ast.Name):
        return caught.id in _SWALLOWING_HANDLERS
    if isinstance(caught, ast.Tuple):
        return any(
            isinstance(elt, ast.Name) and elt.id in _SWALLOWING_HANDLERS
            for elt in caught.elts
        )
    return False


def _handler_propagates(handler: ast.ExceptHandler) -> bool:
    """True if the handler re-raises or fails outright, so nothing is swallowed."""
    for stmt in handler.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Raise):
                return True
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "fail"
            ):
                return True
    return False


def find_swallowed_assertions(source: str, label: str) -> list[str]:
    """Assertions inside a ``try`` whose handler absorbs ``AssertionError``."""
    findings = []
    tree = ast.parse(source)
    for fn in _test_functions(tree):
        for node in ast.walk(fn):
            if not isinstance(node, ast.Try):
                continue
            guarded = _asserts_within(node.body)
            if not guarded:
                continue
            for handler in node.handlers:
                if not _handler_catches_broadly(handler):
                    continue
                if _handler_propagates(handler):
                    continue
                first = min(a.lineno for a in guarded)
                findings.append(
                    f"{label}:{first}: {fn.name} asserts inside a `try` whose "
                    f"handler at line {handler.lineno} catches the AssertionError. "
                    "Move the assertions after the `try`, or assert which outcome "
                    "occurred."
                )
                break
    return findings


def find_self_referential_fixtures(source: str, label: str) -> list[str]:
    """Assertions about an attribute the test itself wrote onto the subject.

    All four conditions must hold inside one test function: a local name is
    bound to a mock; a non-protocol attribute is written on it; that mock is
    installed as some *parameter's* ``return_value`` (a ``@patch`` mock, so a
    production constructor is what was replaced); and an ``assert`` reads the
    written attribute off a different name.
    """
    findings = []
    tree = ast.parse(source)
    for fn in _test_functions(tree):
        params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}

        local_mocks = {
            target.id
            for node in ast.walk(fn)
            if isinstance(node, ast.Assign) and _is_mock_construction(node.value)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        if not local_mocks:
            continue

        seeded: dict[str, int] = {}
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and _base_name(target) in local_mocks
                    and target.attr not in _MOCK_PROTOCOL_ATTRS
                ):
                    seeded.setdefault(target.attr, node.lineno)
        if not seeded:
            continue

        installed = any(
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Name)
            and node.value.id in local_mocks
            and any(
                isinstance(target, ast.Attribute)
                and target.attr == "return_value"
                and _base_name(target) in params
                for target in node.targets
            )
            for node in ast.walk(fn)
        )
        if not installed:
            continue

        for node in ast.walk(fn):
            if not isinstance(node, ast.Assert):
                continue
            for sub in ast.walk(node.test):
                if not isinstance(sub, ast.Attribute) or sub.attr not in seeded:
                    continue
                subject = _base_name(sub)
                if subject is None or subject in local_mocks or subject in params:
                    continue
                findings.append(
                    f"{label}:{node.lineno}: {fn.name} asserts `.{sub.attr}` of "
                    f"`{subject}`, which is the mock this test seeded at line "
                    f"{seeded[sub.attr]} — the production value is never "
                    "observed, so any expected value passes. Assert against the "
                    "recorded call instead."
                )
                break
    return findings


def _test_sources() -> list[tuple[str, str]]:
    """(repo-relative path, source) for every tracked test module.

    Discovery asks for ``*.py`` and filters on the file name rather than passing
    ``test_*.py`` to git. A git pathspec wildcard crosses ``/``, so
    ``test_*.py`` matches only paths that *begin* ``test_`` — six files in this
    tree, none of them the suites that matter.
    """
    this_file = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()
    out = []
    for path in tracked_paths(REPO_ROOT, "*.py"):
        name = path.name
        if not (name.startswith("test_") or name.endswith("_test.py")):
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        # This module's own examples are deliberate specimens of both shapes.
        if rel == this_file:
            continue
        try:
            out.append((rel, path.read_text(encoding="utf-8")))
        except (UnicodeDecodeError, OSError):
            continue
    return out


def test_discovery_finds_the_test_modules():
    """Fail loudly rather than pass vacuously if discovery returns nothing."""
    sources = _test_sources()
    assert len(sources) > 300, (
        f"only {len(sources)} test modules discovered under {REPO_ROOT}; the "
        "scans below would prove nothing"
    )


def test_no_assertion_is_swallowed_by_its_own_except_handler():
    findings = []
    for rel, source in _test_sources():
        try:
            findings.extend(find_swallowed_assertions(source, rel))
        except SyntaxError:
            continue
    assert not findings, "assertions that cannot fail:\n" + "\n".join(
        f"  {f}" for f in findings
    )


def test_no_test_asserts_against_a_fixture_it_seeded_itself():
    findings = []
    for rel, source in _test_sources():
        try:
            findings.extend(find_self_referential_fixtures(source, rel))
        except SyntaxError:
            continue
    assert not findings, "self-referential assertions:\n" + "\n".join(
        f"  {f}" for f in findings
    )


# ---------------------------------------------------------------------------
# The detectors' own tests. A gate whose detector is never shown to fire is
# itself a test that cannot fail, which is the thing this file is about. The
# positive cases are the #1129 spellings verbatim in shape.
# ---------------------------------------------------------------------------

_SWALLOWED_POSITIVES = {
    "except Exception: pass absorbs the assertion": """
        def test_circular_reference_detection():
            try:
                model = build(schema)
                assert model.root.value == "test"
            except Exception:
                pass
    """,
    "except Exception: pytest.skip turns a failure into a skip": """
        def test_global_model_with_flex_suffix(client):
            try:
                response = client.invoke_model(model_id="m")
                assert "output" in response
            except Exception as e:
                pytest.skip(f"not available: {e}")
    """,
    "a bare except absorbs it too": """
        def test_thing():
            try:
                assert compute() == 1
            except:
                logger.warning("oh well")
    """,
    "a mock.assert_called_with is an assertion as well": """
        def test_thing(mock_client):
            try:
                handler({})
                mock_client.put_item.assert_called_once()
            except Exception:
                pass
    """,
}

_SWALLOWED_NEGATIVES = {
    "the handler re-raises": """
        def test_thing():
            try:
                assert compute() == 1
            except Exception:
                logger.warning("context")
                raise
    """,
    "the handler fails outright": """
        def test_thing():
            try:
                assert compute() == 1
            except TypeError:
                pytest.fail("not implemented")
    """,
    "a narrow handler cannot catch AssertionError": """
        def test_thing():
            try:
                assert compute() == 1
            except ValueError:
                pass
    """,
    "the assertions sit after the try, which is the remedy": """
        def test_thing(client):
            try:
                response = client.invoke_model(model_id="m")
            except Exception as e:
                pytest.skip(f"not available: {e}")
            assert "output" in response
    """,
    "nothing is asserted inside the try": """
        def test_thing():
            try:
                build(schema)
            except Exception:
                pass
            assert True
    """,
}

_SELFREF_POSITIVES = {
    "the #1129 tool count": """
        @patch("pkg.mod.strands.Agent")
        def test_create_agent(self, mock_agent_class):
            mock_agent = MagicMock()
            mock_agent.tools = [MagicMock() for _ in range(7)]
            mock_agent_class.return_value = mock_agent

            agent = create_agent()

            assert len(agent.tools) == 7
    """,
    "any attribute, not just a count": """
        @patch("pkg.mod.Client")
        def test_client_region(self, mock_client_class):
            stub = MagicMock()
            stub.region = "eu-west-1"
            mock_client_class.return_value = stub

            client = build_client()

            assert client.region == "eu-west-1"
    """,
}

_SELFREF_NEGATIVES = {
    "the mock is the input and the subject is real": """
        def test_init_custom():
            mock_client = MagicMock()
            mock_client.region = "eu-west-1"
            agent = DiscoveryAgent(bedrock_client=mock_client)
            assert agent.region == "eu-west-1"
    """,
    "the assertion is about the recorded call, which is the remedy": """
        @patch("pkg.mod.strands.Agent")
        def test_create_agent(self, mock_agent_class):
            create_agent()
            passed = mock_agent_class.call_args.kwargs["tools"]
            assert len(passed) == len(tools_module.__all__)
    """,
    "only return_value is configured, so nothing was seeded": """
        @patch("pkg.mod.Client")
        def test_thing(self, mock_client_class):
            stub = MagicMock()
            mock_client_class.return_value = stub
            assert build_client() is stub
    """,
    "the mock is not installed as a patched constructor's return_value": """
        def test_thing():
            stub = MagicMock()
            stub.tools = [1, 2, 3]
            result = transform(stub)
            assert len(result.tools) == 3
    """,
}


@pytest.mark.parametrize("label", sorted(_SWALLOWED_POSITIVES))
def test_the_swallowed_assertion_detector_fires(label):
    findings = find_swallowed_assertions(
        textwrap.dedent(_SWALLOWED_POSITIVES[label]), "probe.py"
    )
    assert findings, f"detector missed: {label}"


@pytest.mark.parametrize("label", sorted(_SWALLOWED_NEGATIVES))
def test_the_swallowed_assertion_detector_stays_quiet(label):
    findings = find_swallowed_assertions(
        textwrap.dedent(_SWALLOWED_NEGATIVES[label]), "probe.py"
    )
    assert not findings, f"false positive on: {label}\n{findings}"


@pytest.mark.parametrize("label", sorted(_SELFREF_POSITIVES))
def test_the_self_referential_detector_fires(label):
    findings = find_self_referential_fixtures(
        textwrap.dedent(_SELFREF_POSITIVES[label]), "probe.py"
    )
    assert findings, f"detector missed: {label}"


@pytest.mark.parametrize("label", sorted(_SELFREF_NEGATIVES))
def test_the_self_referential_detector_stays_quiet(label):
    findings = find_self_referential_fixtures(
        textwrap.dedent(_SELFREF_NEGATIVES[label]), "probe.py"
    )
    assert not findings, f"false positive on: {label}\n{findings}"
