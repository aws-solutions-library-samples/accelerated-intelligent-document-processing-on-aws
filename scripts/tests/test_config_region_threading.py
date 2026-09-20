# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every configuration-layer client outside Lambda is built with an explicit region.

``ConfigurationManager`` / ``ConfigurationReader`` resolve a DynamoDB table by
NAME, and a table name is not region-qualified. A caller that looked the name up
from CloudFormation in one region and then builds one of these without that region
reads and writes the same name in whatever region the ambient credentials resolve
to. On a multi-region account that is a successful write to a *different stack's*
configuration table, reported to the operator as success — which is what
``idp-cli config-upload --region`` did, along with seven sibling ``config-*``
commands, ``bootstrap``, ``discover``, ``config-sync-bda`` and one migration
script.

**Why this is a whole-tree AST gate and not a regex over one module.** The first
version of this check inspected a single module (``idp_sdk.operations.config``)
with a regular expression. It therefore could not see the three callers that
mattered most — ``BdaBlueprintService``, ``ClassesDiscovery`` and
``RulesDiscovery``, each of which builds its own manager in ``idp_common`` — and
its regex tolerated only one level of nested parentheses, so a site written as
``ConfigurationManager(table_name=f(g(x)))`` slipped through. It also only
asserted a MINIMUM number of sites, which catches a replaced one but not an added
one.

So: every construction site in every git-tracked ``.py`` is found by parsing the
AST (exact, no nesting limit), and each one must either pass ``region=`` or be
covered by a rule below. Adding a new site anywhere cannot pass silently.

**The exemption is structural, not a list.** Code that runs only inside a Lambda
correctly omits the region: the runtime always sets ``AWS_REGION``, which is what
``region=None`` defers to. "Runs only inside a Lambda" is decided by DIRECTORY
(``src/lambda/``, ``nested/*/src/lambda/``, ``feature-platform/``), so a new
handler is covered the moment it is added and nobody has to remember to list it.
Four library-internal sites are not in such a directory and are named explicitly —
each with a premise this file asserts.

Reads only ``git ls-files``: walking the filesystem picks up ``.aws-sam`` build
output and other worktrees, producing findings CI cannot reproduce.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The two region-bearing entry points into idp_common's configuration layer.
CONFIG_CTORS = {"ConfigurationManager", "ConfigurationReader"}

# Directories whose code is deployed as a Lambda, where the runtime always sets
# AWS_REGION and `region=None` is the correct value. Structural on purpose: a new
# handler under one of these is covered without anyone editing this file.
LAMBDA_PREFIXES = (
    "src/lambda/",
    "feature-platform/",
)
# `nested/<stack>/src/lambda/...` — matched by substring because the stack name
# varies.
LAMBDA_SUBSTRINGS = ("/src/lambda/",)

# Library-internal sites that are NOT under a Lambda directory and still build a
# region-free client. Each value is the premise; each premise is asserted below.
REGIONLESS_ALLOWED = {
    "lib/idp_common_pkg/idp_common/bedrock/model_utils.py": (
        "_load_model_limits_from_dynamodb is several frames below any caller with a "
        "region in scope. Covered two ways instead: the call is wrapped so an "
        "out-of-region failure degrades to the on-disk limits rather than raising, "
        "and ConfigOperation._configure_config_env bridges the resolved region into "
        "AWS_DEFAULT_REGION for exactly this case."
    ),
    "lib/idp_common_pkg/idp_common/agents/quick_start/tools/bootstrap_tools.py": (
        "Strands agent tools, invoked only by the Quick Start agent running in "
        "Lambda. Premise asserted below: no idp_sdk or idp_cli module imports this, "
        "so it is not reachable from a CLI command that took --region."
    ),
}


def _tracked_python() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [
        line
        for line in out.splitlines()
        if line.endswith(".py")
        # Tests legitimately construct these however they need to.
        and "/tests/" not in line
        and not line.startswith("tests/")
        and not Path(line).name.startswith("test_")
    ]


def _construction_sites() -> list[tuple[str, int, str, bool]]:
    """(path, lineno, ctor name, passes a region) for every tracked .py."""
    sites: list[tuple[str, int, str, bool]] = []
    for rel in _tracked_python():
        try:
            tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else (func.attr if isinstance(func, ast.Attribute) else None)
            )
            if name in CONFIG_CTORS:
                passes_region = any(kw.arg == "region" for kw in node.keywords)
                sites.append((rel, node.lineno, name, passes_region))
    return sites


def _is_lambda_path(rel: str) -> bool:
    return rel.startswith(LAMBDA_PREFIXES) or any(
        sub in rel for sub in LAMBDA_SUBSTRINGS
    )


@pytest.mark.unit
def test_site_discovery_is_not_vacuous():
    """If the AST walk stops finding sites, every assertion below passes for free."""
    sites = _construction_sites()
    assert len(sites) >= 30, f"only {len(sites)} construction sites found"
    assert any(
        rel.startswith("lib/idp_sdk/") for rel, _, _, _ in sites
    ), "the SDK's configuration operations are no longer discovered"
    assert any(
        _is_lambda_path(rel) for rel, _, _, _ in sites
    ), "no Lambda-deployed site found, so the structural exemption is untested"
    assert any(
        not _is_lambda_path(rel) for rel, _, _, _ in sites
    ), "no out-of-Lambda site found, so the requirement is untested"


@pytest.mark.unit
def test_ast_walk_sees_arbitrarily_nested_calls():
    """The reason this is an AST walk rather than a regular expression.

    The previous regex allowed one level of nested parentheses, so a site whose
    arguments nested two deep was invisible — and an invisible site is exactly the
    one that ships without a region.
    """
    src = (
        "ConfigurationManager(table_name=resolve(lookup(stack(name))))\n"
        "ConfigurationReader(table_name=f(g(x)), region=r)\n"
    )
    tree = ast.parse(src)
    found = [
        (n.func.id, any(kw.arg == "region" for kw in n.keywords))
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) in CONFIG_CTORS
    ]
    assert found == [
        ("ConfigurationManager", False),
        ("ConfigurationReader", True),
    ], found


@pytest.mark.unit
def test_every_out_of_lambda_construction_passes_a_region():
    offenders = [
        f"{rel}:{lineno} {name}(...)"
        for rel, lineno, name, passes in _construction_sites()
        if not passes and not _is_lambda_path(rel) and rel not in REGIONLESS_ALLOWED
    ]
    assert not offenders, (
        "these build idp_common's configuration layer with no region while running "
        "outside Lambda, so they resolve the table name in the requested region and "
        "then read or write it in whatever region the ambient credentials pick — a "
        "silent write to another stack's configuration table on a multi-region "
        f"account: {offenders}"
    )


@pytest.mark.unit
def test_lambda_sites_are_genuinely_under_a_lambda_directory():
    """The structural exemption's premise: every site it excuses really is deployed
    as a Lambda, rather than merely living in a conveniently named folder."""
    for rel, lineno, name, passes in _construction_sites():
        if passes or not _is_lambda_path(rel):
            continue
        handler_dir = (REPO_ROOT / rel).parent
        siblings = {p.name for p in handler_dir.iterdir()} if handler_dir.is_dir() else set()
        assert "index.py" in siblings or rel.endswith("_handler.py"), (
            f"{rel}:{lineno} is excused from passing a region because it is under a "
            f"Lambda directory, but its folder has no Lambda handler entry point "
            f"({sorted(siblings)[:8]}) — so the premise that AWS_REGION is always "
            "set for it is unproven"
        )


@pytest.mark.unit
def test_regionless_allowances_are_still_needed():
    """A stale allowance names a file the check would now pass anyway, which teaches
    a reader that a carve-out is load-bearing when it is not."""
    regionless_files = {
        rel
        for rel, _, _, passes in _construction_sites()
        if not passes and not _is_lambda_path(rel)
    }
    stale = sorted(set(REGIONLESS_ALLOWED) - regionless_files)
    assert not stale, (
        f"these files no longer build a region-free configuration client, so their "
        f"REGIONLESS_ALLOWED entries are stale — remove them: {stale}"
    )


@pytest.mark.unit
def test_model_utils_allowance_premise_holds():
    """``model_utils``'s premise, both halves.

    It is excused only because the call degrades rather than raising, AND because
    the SDK bridges the region into the environment for exactly this case. If either
    stops being true the allowance is unsound.
    """
    rel = "lib/idp_common_pkg/idp_common/bedrock/model_utils.py"
    tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))

    # Every `ConfigurationManager(...)` in this file must sit inside a `try:` whose
    # handler catches broadly, so an out-of-region read degrades to the on-disk
    # limits instead of raising. Established by AST containment rather than a text
    # window, which cannot tell a nearby `try:` from an enclosing one.
    guarded: list[bool] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        catches_broadly = any(
            h.type is None or getattr(h.type, "id", None) == "Exception"
            for h in node.handlers
        )
        if not catches_broadly:
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and getattr(inner.func, "id", None) == "ConfigurationManager"
            ):
                guarded.append(True)

    total = sum(
        1
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", None) == "ConfigurationManager"
    )
    assert total > 0, f"{rel} no longer constructs a ConfigurationManager at all"
    assert len(guarded) == total, (
        f"{rel} has {total - len(guarded)} of {total} ConfigurationManager call(s) "
        "outside a broadly-catching try:, so an out-of-region read now raises "
        "instead of degrading to the on-disk limits — the premise behind its "
        "REGIONLESS_ALLOWED entry"
    )
    bridge = (
        REPO_ROOT / "lib/idp_sdk/idp_sdk/operations/config.py"
    ).read_text(encoding="utf-8")
    assert 'os.environ["AWS_DEFAULT_REGION"] = region' in bridge, (
        "the SDK no longer bridges the resolved region into the environment, which "
        f"is the other half of why {rel} may stay region-free"
    )


@pytest.mark.unit
def test_bootstrap_tools_allowance_premise_holds():
    """``bootstrap_tools``'s premise: not reachable from a CLI command.

    It is excused as agent-only code running in Lambda. That is only true while no
    ``idp_sdk`` or ``idp_cli`` module imports it — the moment one does, a command
    that accepted ``--region`` can reach a region-free configuration write.
    """
    importers = []
    for rel in _tracked_python():
        if not rel.startswith(("lib/idp_sdk/", "lib/idp_cli_pkg/")):
            continue
        text = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        if "bootstrap_tools" in text or "quick_start" in text:
            importers.append(rel)
    assert not importers, (
        "these CLI/SDK modules reference the Quick Start agent tools, so those tools "
        "are now reachable from a command that took --region and must thread it: "
        f"{importers}"
    )
