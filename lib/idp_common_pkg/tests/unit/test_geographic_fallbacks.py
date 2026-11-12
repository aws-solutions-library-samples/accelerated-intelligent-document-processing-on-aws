# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No code fallback names a geographic inference profile.

A stored configuration naming a ``us.`` profile is rewritten for the stack's own
geography at deploy time by the ``update_configuration`` function. Nothing rewrites
a model written in code, so a ``us.`` fallback cannot be invoked from an EU or
Canada stack, on the path that runs because configuration was empty or could not
be read. A fallback names its US model through
:func:`idp_common.regional_models.regional_default` instead, or a ``global.``
profile.

What is read, and what is not:

- Every tracked ``.py`` file under ``lib/``, ``src/lambda/``, ``nested/*/src/`` and
  ``patterns/*/src/``, except test modules: a path with a ``tests`` component, or
  a file named ``test_*.py``, ``*_test.py`` or ``conftest.py``. A fixture naming a
  ``us.`` id is input to a test, not a model the code calls.
- Under ``idp_common/agents/`` every model literal is refused, because there the
  configured model comes from configuration and every literal is a fallback.
- Elsewhere a literal is refused in the four positions a fallback takes: a
  parameter default, an operand of ``or`` after the first, the default argument
  of ``.get()``/``getenv()``/``getattr()``, and a ``return`` value. A literal in a
  dict, set, list or tuple display is a table row (the swap tables, capability
  sets, pricing names), and a ``Field(default=...)`` is the shipped configuration
  value the swap rewrites.
- Only strings without whitespace: prose citing an example id is not a model the
  code calls.

What this does **not** catch, measured against synthetic spellings: a module or
class constant read through a fallback position (names are not resolved, so
``return DEFAULT_X`` passes whatever ``DEFAULT_X`` holds); a conditional
expression or an ``if not m: m = ...`` assignment outside a ``return``;
``setdefault``/``pop``/``next`` defaults; dataclass and class-attribute defaults;
a lambda body; a ``model_id=`` keyword argument; a ``us-gov.`` prefix; and
run-time concatenation. None of those shapes holds a geographic literal in scope
today except ``DEFAULT_CLASSIFICATION_MODEL`` in ``config/models.py``, which must
equal the shipped classification default and is outside this change.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

from idp_common import regional_models
from idp_common.discovery import regional_defaults

_REPO = Path(__file__).resolve().parents[4]

#: Unanchored, so an inference-profile ARN carrying the ID is found too, and
#: ``[``/``]`` are allowed for a ``[1m]`` context suffix.
_GEOGRAPHIC_MODEL_LITERAL = re.compile(
    r"(?<![\w-])(us|eu|apac|ca|jp|au)\.[a-z]+\.[\w.:\[\]-]+"
)

_ROOTS = ("lib", "src/lambda", "nested", "patterns")
_AGENTS = "lib/idp_common_pkg/idp_common/agents/"
_GET_NAMES = {"get", "getenv", "getattr"}


def _is_test_module(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return (
        "tests" in parts[:-1]
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


def _in_scope(path: str) -> bool:
    if not path.endswith(".py") or _is_test_module(path):
        return False
    parts = path.split("/")
    if parts[0] == "lib" or parts[:2] == ["src", "lambda"]:
        return True
    return parts[0] in ("nested", "patterns") and len(parts) > 2 and parts[2] == "src"


def _modules() -> list[str]:
    tracked = subprocess.run(
        ["git", "ls-files", *_ROOTS],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return sorted(p for p in tracked if _in_scope(p))


def _is_model_literal(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and not re.search(r"\s", node.value)
        and bool(_GEOGRAPHIC_MODEL_LITERAL.search(node.value))
    )


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _fallback_positions(tree: ast.AST):
    """Every expression in a position a fallback takes."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            yield from node.args.defaults
            yield from (d for d in node.args.kw_defaults if d is not None)
        elif isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            yield from node.values[1:]
        elif isinstance(node, ast.Call) and _call_name(node) in _GET_NAMES:
            if len(node.args) >= (3 if _call_name(node) == "getattr" else 2):
                yield node.args[-1]
            yield from (k.value for k in node.keywords if k.arg == "default")
        elif isinstance(node, ast.Return) and node.value is not None:
            yield node.value


#: Calls whose argument is a model the call translates, not the fallback itself.
_TRANSLATORS = {"regional_default"}


def _walk_values(expr: ast.AST):
    """``ast.walk`` over the values ``expr`` can evaluate to.

    A comparison evaluates to a bool, so a literal inside one (``"us.x" in id``)
    is a test of a model id, not a model; a translator's argument is translated.
    """
    yield expr
    if isinstance(expr, ast.Compare):
        return
    if isinstance(expr, ast.Call) and _call_name(expr) in _TRANSLATORS:
        return
    for child in ast.iter_child_nodes(expr):
        yield from _walk_values(child)


def _findings(path: str, source: str) -> list[str]:
    tree = ast.parse(source)
    if path.startswith(_AGENTS):
        nodes = [n for n in ast.walk(tree) if _is_model_literal(n)]
    else:
        nodes = [
            n
            for expr in _fallback_positions(tree)
            for n in _walk_values(expr)
            if _is_model_literal(n)
        ]
    return sorted({f"{path}:{n.lineno}: {n.value}" for n in nodes})  # type: ignore[attr-defined]


@pytest.mark.unit
def test_the_scan_reads_every_root():
    modules = _modules()
    for root in ("lib/idp_common_pkg/", "lib/idp_sdk/", "src/lambda/", _AGENTS):
        assert any(m.startswith(root) for m in modules), root
    assert any(m.startswith("nested/") for m in modules)
    assert any(m.startswith("patterns/") for m in modules)


@pytest.mark.unit
def test_no_code_fallback_names_a_geographic_profile():
    found = [
        finding
        for path in _modules()
        for finding in _findings(path, (_REPO / path).read_text(encoding="utf-8"))
    ]
    assert not found, found


_US = "us.anthropic.claude-sonnet-4-6"


@pytest.mark.unit
@pytest.mark.parametrize(
    "source",
    [
        f"def f(model='{_US}'): ...",
        f"def f(*, model='{_US}'): ...",
        f"m = cfg or '{_US}'",
        f"m = a or b or '{_US}'",
        f"m = cfg.get('model_id', '{_US}')",
        f"m = os.environ.get('MODEL', default='{_US}')",
        f"m = os.getenv('MODEL', '{_US}')",
        f"m = getattr(cfg, 'model', '{_US}')",
        f"def f():\n    return '{_US}'",
        f"def f():\n    return '{_US}' if eu else 'x'",
        f"m = cfg or 'arn:aws:bedrock:us-east-1:1:inference-profile/{_US}'",
    ],
)
def test_each_fallback_position_is_read(source):
    """The scan is only as good as its positions; each must fire on its own."""
    assert _findings("lib/x/y.py", source), source


@pytest.mark.unit
@pytest.mark.parametrize(
    "source",
    [
        f"TABLE = {{'{_US}': 'eu.anthropic.claude-sonnet-4-6'}}",
        f"model: str = Field(default='{_US}')",
        "m = cfg or 'global.anthropic.claude-sonnet-4-6'",
        "m = cfg or 'anthropic.claude-sonnet-4-6'",
        f"def f():\n    '''For example {_US} is a model.'''",
        f"ok = 'anthropic.claude' in m or '{_US}' in m",
        f"def f():\n    return m == '{_US}'",
        f"m = cfg or regional_default('{_US}', region)",
    ],
)
def test_what_is_not_a_fallback_is_not_reported(source):
    assert not _findings("lib/x/y.py", source), source


@pytest.mark.unit
@pytest.mark.parametrize("prefix", ["us", "eu", "apac", "ca", "jp", "au"])
def test_every_geographic_prefix_is_read(prefix):
    assert _findings("lib/x/y.py", f"m = cfg or '{prefix}.amazon.nova-pro-v1:0'")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("lib/idp_common_pkg/idp_common/testset_scope.py", True),
        ("lib/idp_common_pkg/idp_common/x/contest.py", True),
        ("src/lambda/update_configuration/test_eu_region.py", False),
        ("lib/idp_common_pkg/tests/unit/helpers.py", False),
        ("lib/idp_sdk/idp_sdk/conftest.py", False),
        ("nested/api-resolvers/src/lambda/x/index.py", True),
        ("nested/api-resolvers/template_helpers.py", False),
    ],
)
def test_only_test_modules_are_left_out(path, expected):
    assert _in_scope(path) is expected


@pytest.mark.unit
def test_every_agent_literal_is_read():
    """Under agents/ a literal outside any fallback position is still refused."""
    source = f"MODELS = ['{_US}']"
    assert _findings(_AGENTS + "x.py", source)
    assert not _findings("lib/x/y.py", source)


def _resolve(arg: ast.expr) -> str | None:
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    name = getattr(arg, "id", None) or getattr(arg, "attr", None)
    for module in (regional_models, regional_defaults):
        value = getattr(module, name or "", None)
        if isinstance(value, str):
            return value
    return None


def _translation_calls(path: str, source: str) -> list[tuple[str, str | None]]:
    return [
        (f"{path}:{node.lineno}", _resolve(node.args[0]))
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and _call_name(node) == "regional_default"
        and node.args
    ]


def _unregistered(calls):
    return [c for c in calls if c[1] not in regional_models.FALLBACK_MODEL_IDS]


@pytest.mark.unit
def test_every_regional_default_call_names_a_registered_fallback():
    """``FALLBACK_MODEL_IDS`` is what the swap-parity test in
    ``src/lambda/update_configuration/test_eu_region.py`` iterates, so an id passed
    to :func:`regional_default` outside it would escape that check."""
    calls = [
        call
        for path in _modules()
        for call in _translation_calls(path, (_REPO / path).read_text(encoding="utf-8"))
    ]
    assert len({site.split(":")[0] for site, _ in calls}) >= 5, calls
    assert not _unregistered(calls), _unregistered(calls)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source", "registered"),
    [
        ("regional_default(SONNET_4_6_MODEL_ID, r)", True),
        ("regional_default(DEFAULT_EMBEDDING_MODEL_ID, r)", True),
        (f"regional_default('{regional_models.CHAT_MODEL_ID}', r)", True),
        ("regional_default('us.amazon.nova-lite-v1:0', r)", False),
        ("regional_default(model_id, r)", False),
    ],
)
def test_the_registration_check_tells_the_two_apart(source, registered):
    """Without this the check above could go vacuous and stay green."""
    assert (not _unregistered(_translation_calls("x.py", source))) == registered


@pytest.mark.unit
def test_discovery_delegates_to_the_shared_translation():
    """A second copy of the table in ``regional_defaults`` would escape the
    swap-parity test, which reads ``regional_models`` only."""
    assert regional_defaults.regional_default is regional_models.regional_default
    assert (
        regional_defaults.DEFAULT_ANALYSIS_MODEL_ID
        == regional_models.SONNET_4_6_MODEL_ID
    )
    assert (
        regional_defaults.DEFAULT_EMBEDDING_MODEL_ID
        == regional_models.EMBEDDING_MODEL_ID
    )
