# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Lambdas that log their invocation event redact it with the CANONICAL denylist.

``lib/idp_common_pkg/idp_common/utils/log_sanitizer.py`` is the one redactor. Ten
resolvers under ``nested/api-resolvers/src/lambda/`` used to hand-copy a shortened
version of its key denylist into their own ``index.py``, and those copies drifted
eight keys behind the canonical list — nothing compared them, so nothing noticed.
A denylist that is duplicated by hand is a denylist that is eventually wrong, and
the failure is silent: the redactor still runs, still looks correct in review, and
just passes the newer key names straight through.

So the split is now mechanical, and derived here from the templates rather than
from a list of names kept in a comment:

* A function that carries an ``IDPCommon*Layer`` imports
  ``idp_common.utils.log_sanitizer`` directly — there is nothing to copy.
* A function that carries no layer cannot import the library at runtime at all
  (SAM packages each function from its own ``CodeUri``, so it cannot reach a
  sibling directory either). Those get a **byte-identical** committed copy of the
  module as ``log_sanitizer.py``, kept in step by
  ``scripts/sync_resolver_log_sanitizer.sh``. The module is stdlib-only, so the
  copy costs a few KB where attaching the base layer — Pillow, pypdfium2,
  requests — would cost tens of MB on functions that need none of it.

This is the same guarded-vendoring shape as
``src/lambda/chat_stream_processor/vendored/`` and its
``test_vendored_in_sync.py``.

Scope: three Lambda trees
-------------------------

This file is named for the api-resolver tree because that is the tree it was
written against, and the name is left alone so that the thirty committed copies of
the canonical module — whose docstring cites it — do not all have to be rewritten
to rename a test. It now covers ``src/lambda/`` and the
feature-platform control plane under ``feature-platform/main-stack-extensions/``
as well, and that is the point of the widening rather than an incidental extra:
when the scan below was rooted at the resolver tree alone it reported a clean tree
while **forty** log calls in **thirty-eight** files under ``src/lambda/`` wrote
their whole invocation event to CloudWatch, including the agent chat processor's — prompt, caller ``sub`` and
caller group list on every turn. The sites it could not see outnumbered the ones it
could by roughly four to one. A guard whose reach is narrower than the defect class
it describes reads, from a green test run, exactly like a guard that works.

The roots are listed once in ``LAMBDA_ROOTS`` and asserted against the sync
script's own root list, so a tree can not be added to one and forgotten in the
other. ``LAMBDA_ROOTS`` is not the whole repository, and the trees it leaves out are
themselves asserted rather than left implicit — see
``test_every_lambda_tree_is_either_covered_or_explicitly_out_of_scope``, which is
the lesson of this widening applied to itself: the reason nobody noticed the
resolver-only root for months is that a green run looks identical whether the guard
covers one tree or fifteen.

What each test below forbids:

* **logging the invocation event without sanitizing it** — see
  ``test_no_lambda_logs_the_raw_invocation_event``. This is the one that closes the
  actual defect class of #921 and #977; every other test here proves the *copies*
  are consistent, which is a different and weaker property. A function can pass all
  of them while writing ``identity.claims`` to CloudWatch in full, because it simply
  never mentions the sanitizer at all. Two resolvers did exactly that
  (``finetuning_jobs_resolver``, ``list_documents_range_resolver``) and the first
  round of these guards was green with both in the tree.
* reintroducing a hand-rolled key list anywhere under either tree, under any
  variable name (the original copies were all called ``_LOG_SENSITIVE_KEYS``, but
  renaming one must not buy an exemption);
* a vendored copy drifting from the canonical module by even one byte;
* a function importing ``idp_common`` for the sanitizer while carrying no layer to
  provide it — that is an ImportError at cold start, not a lint nit;
* the sync script scanning a different set of trees than this guard does.

The canonical module is loaded **by path**, not via ``import idp_common``: an
editable install in the environment may resolve to a different checkout entirely,
which would make this test assert against someone else's file.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
from functools import lru_cache
from pathlib import Path

import gate_premises
import pytest
import repo_files

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL = REPO_ROOT / "lib/idp_common_pkg/idp_common/utils/log_sanitizer.py"

# The trees of Lambda handler directories. Every immediate subdirectory of each is
# treated as one function's package, which is how SAM packages them.
RESOLVER_ROOT = REPO_ROOT / "nested/api-resolvers/src/lambda"
SRC_LAMBDA_ROOT = REPO_ROOT / "src/lambda"
FEATURE_PLATFORM_ROOT = REPO_ROOT / "feature-platform/main-stack-extensions/lambdas"
LAMBDA_ROOTS = (RESOLVER_ROOT, SRC_LAMBDA_ROOT, FEATURE_PLATFORM_ROOT)

# Templates are discovered by CONTENT, via the same script `make cfn-lint` and
# `make check-arn-partitions` use, rather than from a list of paths kept here.
# The list-of-paths version of this had already been wrong once: it named the
# nested api-resolvers template only, and so read FinetuningJobsResolverFunction —
# declared in the PARENT template — as carrying no layer, which is the direction
# that produces wrong advice. Widening to src/lambda would have reintroduced the
# same bug twice over, because those functions are declared across four templates
# (the parent, patterns/unified, nested/api-resolvers and nested/multi-doc-
# discovery) and nothing says a fifth will not appear.
DISCOVER_TEMPLATES = REPO_ROOT / "scripts/discover_templates.sh"
SYNC_SCRIPT = REPO_ROOT / "scripts/sync_resolver_log_sanitizer.sh"

# Lambda source trees this guard does NOT cover, each with the reason and the
# number of raw-event log sites the exemption covered when it was written. Keyed by
# repo-relative path, and compared at test time against the set of directories that
# actually hold ``AWS::Serverless::Function`` CodeUri packages, so a tree cannot be
# added to the repository — or renamed — without a decision being made about it.
#
# Read the value as ``(reason, raw-event log sites audited)``. The count is the
# ratchet: a NEW site added inside an exempt tree fails
# ``test_uncovered_trees_hide_no_more_sites_than_were_audited``, so the exemption
# covers the sites someone looked at, not the directory name forever. Writing the
# number down is the review moment — it is the step at which "this tree is out of
# scope" stops being a sentence and becomes a measurement.
#
# There is deliberately NO blanket premise here. The previous one said every entry
# was "built and deployed independently of the two main ones, or is not deployed by
# this solution at all", and that was false for four of them:
# ``feature-platform/main-stack-extensions/lambdas`` (now covered, see
# ``FEATURE_PLATFORM_ROOT``), ``patterns/unified/src``, ``nested/bedrockkb/src`` and
# ``nested/multi-doc-discovery`` are all nested stacks of ``template.yaml`` and are
# all built by the same publish run. An aggregate reading of that sentence passes;
# per member it fails four times. ``test_no_uncovered_tree_claims_to_be_built_apart``
# asserts the structural facts directly instead, so a reason may no longer borrow
# them.
UNCOVERED_LAMBDA_TREES: dict[str, tuple[str, int]] = {
    "patterns/unified/src": (
        "the document-processing pipeline steps. A nested stack of template.yaml, "
        "built in the same publish run; the reason is NOT independence but scope: "
        "their events are Step Functions payloads rather than caller-supplied API "
        "events, and these are the logs operators read most often when diagnosing a "
        "document, so what to keep in them is a judgement worth its own review "
        "rather than a mechanical sweep. The largest remaining instance of this "
        "defect class",
        15,
    ),
    "nested/bedrockkb/src": (
        "the Bedrock Knowledge Base custom resources. A nested stack of "
        "template.yaml, built in the same publish run; the reason is that these run "
        "only during a stack operation and their events are stack metadata",
        3,
    ),
    "nested/multi-doc-discovery": (
        "a container-image build helper (docker_build_lambda), not a handler that "
        "receives an API or pipeline event",
        0,
    ),
    "feature-platform/confbench-testset": ("an optional feature extension package", 0),
    "feature-platform/feature-template": (
        "the scaffold a new feature is copied from",
        0,
    ),
    "feature-platform/idp-data-generator": (
        "an optional feature extension package",
        1,
    ),
    "feature-platform/pii-anonymizer": (
        "an optional feature extension package, and its hook vendors third-party "
        "code kept byte-for-byte — see its PROVENANCE.md",
        0,
    ),
    "feature-platform/sample-feature": (
        "a sample feature, shipped as documentation",
        0,
    ),
    "feature-platform/sample-health-insurance-review": (
        "a sample feature, shipped as documentation",
        0,
    ),
    "feature-platform/seller-entitlement-service/lambdas": (
        "the marketplace seller-side service, deployed standalone to a seller "
        "account and not by this solution's own stack",
        0,
    ),
    "samples/lambda-hook-inference": (
        "sample hook implementations, deployed by the reader rather than by this "
        "solution",
        0,
    ),
    "notebooks/examples": ("a notebook example, not part of any deployed stack", 1),
}

# Per member, the two structural facts an "out of scope" reason is most tempted to
# borrow, written down so they can be CHECKED rather than asserted in prose.
#
#   notDeployedByParent    template.yaml declares no nested stack for it
#   notBuiltWithMainStack  no publish-run build source covers it
#
# Both are measured against the authorities by
# ``test_the_recorded_structural_facts_match_the_tree``. Note how many are False: four
# of these trees ARE part of the main deployment and are still legitimately uncovered,
# for scope and event shape. Recording the facts rather than forbidding a phrase is what
# lets both things be true at once -- the previous version of this check forbade three
# literal strings in the reason, which a rewording walked straight past.
#
# The five bundled catalog features are False on the build side for a reason worth
# knowing: they are absent from the publisher's component-dependency map, and a publish
# run builds them anyway through extensions-oss.yaml. A predicate reading only that map
# called all five independent.
TREE_INDEPENDENCE: dict[str, dict[str, bool]] = {
    "patterns/unified/src": {
        "notDeployedByParent": False,
        "notBuiltWithMainStack": False,
    },
    "nested/bedrockkb/src": {
        "notDeployedByParent": False,
        "notBuiltWithMainStack": False,
    },
    "nested/multi-doc-discovery": {
        "notDeployedByParent": False,
        "notBuiltWithMainStack": False,
    },
    "feature-platform/confbench-testset": {
        "notDeployedByParent": True,
        "notBuiltWithMainStack": False,
    },
    "feature-platform/feature-template": {
        "notDeployedByParent": True,
        "notBuiltWithMainStack": True,
    },
    "feature-platform/idp-data-generator": {
        "notDeployedByParent": True,
        "notBuiltWithMainStack": False,
    },
    "feature-platform/pii-anonymizer": {
        "notDeployedByParent": True,
        "notBuiltWithMainStack": False,
    },
    "feature-platform/sample-feature": {
        "notDeployedByParent": True,
        "notBuiltWithMainStack": False,
    },
    "feature-platform/sample-health-insurance-review": {
        "notDeployedByParent": True,
        "notBuiltWithMainStack": False,
    },
    "feature-platform/seller-entitlement-service/lambdas": {
        "notDeployedByParent": True,
        "notBuiltWithMainStack": True,
    },
    "samples/lambda-hook-inference": {
        "notDeployedByParent": True,
        "notBuiltWithMainStack": True,
    },
    "notebooks/examples": {
        "notDeployedByParent": True,
        "notBuiltWithMainStack": True,
    },
}

VENDORED_NAME = "log_sanitizer.py"
VENDORED_MODULE = "log_sanitizer"
CANONICAL_IMPORT = "idp_common.utils.log_sanitizer"

# The keys the hand-copies were missing. Asserted by name so the specific
# regression that motivated this guard cannot come back unnoticed, even if the
# canonical list is reorganized around it.
PREVIOUSLY_MISSING_KEYS = frozenset(
    {
        "access_key",
        "accesskey",
        "passwd",
        "private_key",
        "privatekey",
        "secret_key",
        "secretkey",
        "x-api-key",
    }
)

# A collection literal of strings this many canonical keys deep is a denylist copy,
# not a coincidence. Three is low enough to catch a partial copy and high enough
# that an unrelated tuple (e.g. ("token", "cursor")) does not trip it.
_DENYLIST_MATCH_THRESHOLD = 3


def _load_module_by_path(path: Path, name: str):
    """Import ``path`` as a standalone module, ignoring any installed copy."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical_deny_keys() -> frozenset[str]:
    module = _load_module_by_path(CANONICAL, "_canonical_log_sanitizer")
    return frozenset(module._DEFAULT_DENY_KEY_SUBSTRINGS)


def _lambda_dirs() -> list[Path]:
    """Every handler directory under either root, as a resolved path.

    Returned as paths rather than bare names because the two trees are indexed
    together from here on, and a name is not unique across them.
    """
    # Discovered through git, not `iterdir()`. A bare filesystem listing counts anything
    # that happens to be sitting there, and `__pycache__` appears the moment any `.py`
    # exists at that level -- so adding a `conftest.py` beside these handlers turned a
    # build artifact into a "resolver directory with no CodeUri" and failed this suite.
    # A directory is a handler directory only if git tracks Python inside it, which is
    # the same rule `_tracked_python` below already uses.
    tracked = _tracked_python()
    dirs: list[Path] = []
    for root in LAMBDA_ROOTS:
        for candidate in root.iterdir():
            if not candidate.is_dir():
                continue
            here = candidate.resolve()
            if any(f.parent == here for f in tracked):
                dirs.append(here)
    return sorted(dirs)


@lru_cache(maxsize=1)
def _tracked_python() -> frozenset[Path]:
    """Every Python file **git tracks**, as resolved absolute paths.

    The authoritative set, read through git rather than walked, because an
    extension that has been built locally leaves whole vendored copies of
    ``idp_common`` under ``.aws-sam/build/``, ``build/lib/`` and its own
    ``idp_common_pkg/``. Those carry the same log lines as the sources they were
    copied from, so a filesystem walk finds them and a fresh CI checkout does not.
    A gate that only fails on a machine where someone has run a build is the
    defect it is trying to catch.
    """
    return frozenset(repo_files.tracked_paths(REPO_ROOT, "*.py"))


def _python_files(directory: Path) -> list[Path]:
    """Every Python file git tracks in a handler package, including subpackages.

    Not just ``index.py``: ``src/lambda/chat_stream_processor`` puts its handler in
    ``app.py`` and carries two vendored processor modules under ``vendored/``, and
    both of those logged their raw event. An index-only scan is blind to a whole
    function.

    Restricted to tracked files — see ``_tracked_python``. Build output is a copy
    of a source this gate already reads, so counting it both double-counts and
    makes the count depend on what the machine happens to have built.
    """
    found = sorted(directory.rglob("*.py"))
    try:
        directory.resolve().relative_to(REPO_ROOT)
    except ValueError:
        # A synthetic tree under tmp_path: the self-checks below build one to prove
        # this scan reads whole directories, and nothing there is tracked.
        return found
    tracked = _tracked_python()
    return [p for p in found if p.resolve() in tracked]


@lru_cache(maxsize=1)
def _templates() -> tuple[Path, ...]:
    """Every CloudFormation template in the repo, found by content.

    Delegates to ``scripts/discover_templates.sh``, which is itself covered by
    ``scripts/tests/test_discover_templates.py`` and is what the cfn-lint and
    ARN-partition gates use. Sharing it means this guard cannot come to disagree
    with those about what a template is.
    """
    result = subprocess.run(
        ["bash", str(DISCOVER_TEMPLATES), "cfn"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    paths = tuple(
        REPO_ROOT / line for line in result.stdout.splitlines() if line.strip()
    )
    assert paths, (
        f"{DISCOVER_TEMPLATES.relative_to(REPO_ROOT)} found no templates; the layer "
        "scan below would then read every function as carrying no layer"
    )
    return paths


@lru_cache(maxsize=1)
def _serverless_functions() -> tuple[tuple[Path, tuple[str, ...]], ...]:
    """Every ``AWS::Serverless::Function`` CodeUri directory and its layer refs.

    Parsed with regex rather than a YAML loader: the templates are full of short-form
    intrinsics (``!Ref``, ``!Sub``, ``!If``) that a plain ``yaml.safe_load`` refuses,
    and all this needs is which resource block names which layer. Resource blocks
    start at exactly two spaces of indentation.

    Two things this deliberately does NOT assume, each of which used to drop a
    function silently — and a function missing from this list reads as "carries no
    layer", the direction that produces wrong advice:

    * a leading ``./`` on the CodeUri (``ListDocumentsGSIResolverFunction`` omits it);
    * that the layer parameter name ends in ``Arn`` — a nested stack receives
      ``IDPCommonBaseLayerArn`` as a parameter, while the parent declares the layer
      resource itself and refers to it as ``IDPCommonBaseLayer``.

    Nor does it assume which template a function is declared in; see ``_templates``.
    """
    functions: list[tuple[Path, tuple[str, ...]]] = []
    for template in _templates():
        lines = template.read_text(encoding="utf-8").splitlines()
        starts = [
            i for i, line in enumerate(lines) if re.match(r"^  [A-Za-z0-9]+:\s*$", line)
        ]
        starts.append(len(lines))
        for start, end in zip(starts, starts[1:]):
            block = "\n".join(lines[start:end])
            match = re.search(r"CodeUri:\s*(\S+)", block)
            if not match or "AWS::Serverless::Function" not in block:
                continue
            code_dir = (template.parent / match.group(1)).resolve()
            layers = tuple(re.findall(r"!Ref (IDPCommon\w*Layer(?:Arn)?)\b", block))
            functions.append((code_dir, layers))
    return tuple(functions)


@lru_cache(maxsize=1)
def _layers_by_code_dir() -> dict[Path, list[str]]:
    """Map handler directory under ``LAMBDA_ROOTS`` to its IDPCommon layer refs.

    Keyed by the resolved CodeUri directory and kept only when that directory sits
    directly inside one of ``LAMBDA_ROOTS``, so that two functions with the same
    directory name in different trees cannot collide, and so a CodeUri pointing
    somewhere else entirely is ignored.

    Two resources can share one CodeUri (``backfill_gsi_attributes`` backs both a
    trigger and a worker; ``start_codebuild`` backs a UI build and an ECR cleanup),
    so the layer lists are unioned rather than overwritten: the directory's code has
    to work under whichever resource loads it, and "some resource attaches the
    layer" is not enough to justify a canonical import. That distinction is not
    exercised by today's tree — no shared CodeUri has a layer on one resource and
    not the other — so it is a deliberate choice about the safe direction rather
    than a fix for an observed bug.
    """
    layers: dict[Path, list[str]] = {}
    roots = {root.resolve() for root in LAMBDA_ROOTS}
    for code_dir, found in _serverless_functions():
        if code_dir.parent not in roots:
            continue
        existing = layers.setdefault(code_dir, [])
        existing.extend(layer for layer in found if layer not in existing)
    return layers


def _lambda_trees() -> dict[str, int]:
    """Repo-relative parent directory of every Python Lambda package -> count.

    A "tree" is a directory whose children are handler packages; ``LAMBDA_ROOTS``
    holds the two this guard scans. Derived from the templates so that the guard's
    reach can be compared against the repository as it actually is, rather than
    against an assumption about it.
    """
    trees: dict[str, int] = {}
    seen: set[Path] = set()
    for code_dir, _layers in _serverless_functions():
        if code_dir in seen or not code_dir.is_dir():
            continue
        if not any(code_dir.glob("*.py")):
            continue  # a container-image or non-Python function
        seen.add(code_dir)
        key = code_dir.parent.relative_to(REPO_ROOT).as_posix()
        trees[key] = trees.get(key, 0) + 1
    return trees


def test_every_lambda_tree_is_either_covered_or_explicitly_out_of_scope():
    """The guard's reach is asserted, not assumed.

    ``LAMBDA_ROOTS`` covers two of the trees in this repository that hold Python
    Lambda packages. That is a deliberate boundary rather than a complete one, and
    the whole reason the previous resolver-only boundary went unnoticed for months
    is that nothing anywhere compared it against the repository: a green run reads
    the same whether the scan covers one tree or all of them.

    So compare it here. Every tree that holds an ``AWS::Serverless::Function`` with
    Python sources must be either scanned by this guard or named in
    ``UNCOVERED_LAMBDA_TREES`` with a reason. A new tree — a new feature-platform
    package, a new nested stack — then arrives as a failure asking for a decision,
    which is the only mechanism that reliably produces one.
    """
    trees = _lambda_trees()
    covered = {root.relative_to(REPO_ROOT).as_posix() for root in LAMBDA_ROOTS}
    assert covered <= set(trees), (
        "LAMBDA_ROOTS names a tree that holds no Python Lambda package at all, so "
        "the scan there covers nothing: "
        f"{sorted(covered - set(trees))}"
    )
    unaccounted = sorted(set(trees) - covered - set(UNCOVERED_LAMBDA_TREES))
    assert not unaccounted, (
        "These directories hold Python Lambda packages that this guard does not "
        "scan, and are not listed in UNCOVERED_LAMBDA_TREES. Either add the tree to "
        "LAMBDA_ROOTS (and to the roots list in "
        "scripts/sync_resolver_log_sanitizer.sh) and fix whatever the scan reports, "
        "or record why it is out of scope:\n  "
        + "\n  ".join(
            f"{tree} ({trees[tree]} function directories)" for tree in unaccounted
        )
    )
    stale = sorted(set(UNCOVERED_LAMBDA_TREES) - set(trees) - covered)
    assert not stale, (
        "These UNCOVERED_LAMBDA_TREES entries no longer name a tree holding Python "
        "Lambda packages — renamed, removed, or now covered. A stale exemption is "
        f"how a real gap gets waved through later: {stale}"
    )


@pytest.mark.parametrize("tree", sorted(UNCOVERED_LAMBDA_TREES))
def test_uncovered_trees_hide_no_more_sites_than_were_audited(tree: str):
    """An exempt tree is exempt for the sites audited, not for its name forever.

    Without this, "out of scope" is an open-ended licence: a handler added to an
    exempt tree tomorrow inherits an exemption nobody granted it, and the gate stays
    green. Pinning the count makes the grant finite, and forces the number to be
    written down — which is the step at which someone has to look.

    The count is what this file's own collector sees, so it is a measurement of the
    guard's view rather than an independent census. A pin of 0 therefore asserts
    "this collector finds nothing here", which is the property that matters for a
    ratchet: if it starts finding something, this fails.
    """
    reason, pinned = UNCOVERED_LAMBDA_TREES[tree]
    root = REPO_ROOT / tree
    if not root.is_dir():
        pytest.skip(f"{tree} does not exist; the accounting test above owns that")
    found = _raw_event_log_offenders(roots=(root,))
    assert len(found) == pinned, (
        f"{tree} is out of scope for {pinned} raw-event log site(s) ({reason}), but "
        f"{len(found)} were found. A new site inside an exempt tree is not covered "
        "by the exemption: sanitize it, or review the tree and update the pinned "
        f"count deliberately. Sites: {found}"
    )


@pytest.mark.parametrize("tree", sorted(UNCOVERED_LAMBDA_TREES))
def test_the_recorded_structural_facts_match_the_tree(tree: str):
    """Each uncovered tree's independence is recorded as DATA and checked, not as prose.

    The first version of this test forbade three literal phrases in the ``reason``
    string, and that was a substring denylist wearing a predicate's clothes: rewording
    "built and versioned separately" to "has its own build and release train, ships on
    an independent cadence, and the main stack does not deploy it" restored the false
    exemption with the suite green. The predicates were computed and then discarded
    unless the prose happened to contain one of three literals.

    So prose is no longer the carrier. :data:`TREE_INDEPENDENCE` records, per member,
    whether the parent deploys it and whether one publish run builds it, and those two
    booleans are asserted against the authorities — the nested-stack graph in
    ``template.yaml`` and the publisher's own build sources. A reason may now say
    anything; what it cannot do is disagree with a fact written down beside it, because
    the fact is checked rather than read.

    Four of the twelve are nested stacks built in the same run and are still
    legitimately uncovered, for scope and event shape. That is the case the prose
    version could not express without also permitting the false claim.
    """
    assert tree in TREE_INDEPENDENCE, (
        f"{tree} is in UNCOVERED_LAMBDA_TREES but its structural facts are not recorded "
        "in TREE_INDEPENDENCE. Record them -- an uncovered tree whose relationship to "
        "the main deployment is unstated is how a nested stack came to be exempted as "
        "independently deployed."
    )
    recorded = TREE_INDEPENDENCE[tree]
    nested, nested_why = gate_premises.not_a_nested_stack_of_parent(tree)
    built_apart, built_why = gate_premises.built_separately_from_main_stack(tree)

    assert recorded["notDeployedByParent"] == nested, (
        f"{tree}: TREE_INDEPENDENCE records notDeployedByParent="
        f"{recorded['notDeployedByParent']}, measured {nested}. {nested_why}"
    )
    assert recorded["notBuiltWithMainStack"] == built_apart, (
        f"{tree}: TREE_INDEPENDENCE records notBuiltWithMainStack="
        f"{recorded['notBuiltWithMainStack']}, measured {built_apart}. {built_why}"
    )


def test_every_recorded_tree_is_still_uncovered():
    """No stale entry in the fact table, and no member of it that is now covered."""
    stale = sorted(set(TREE_INDEPENDENCE) - set(UNCOVERED_LAMBDA_TREES))
    assert not stale, (
        f"TREE_INDEPENDENCE records facts about trees that are no longer uncovered: "
        f"{stale}. Delete them, so the table cannot drift into describing trees this "
        "guard now scans."
    )


def _imported_modules(path: Path) -> dict[str, set[str]]:
    """Map module -> names imported from it, for REAL imports in one file.

    AST-parsed, not substring-matched. A comment or docstring that quotes an import
    line is not an import, and these trees are full of exactly such prose: every
    vendored ``log_sanitizer.py`` carries the canonical module's ``Usage::`` block,
    whose body line reads ``from idp_common.utils.log_sanitizer import ...``. The
    substring form of this check passed only because an earlier change happened to
    delete the old comment blocks that quoted the same path; re-adding one such
    comment to a layer-free function would have failed the suite for no real reason.

    On a file Python cannot parse, fall back to a line-anchored text match so an
    unreadable file cannot silently read as importing nothing.
    """
    source = path.read_text(encoding="utf-8") if path.exists() else ""
    if not source:
        return {}
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        found: dict[str, set[str]] = {}
        for module in (CANONICAL_IMPORT, VENDORED_MODULE):
            pattern = (
                rf"^\s*(?:from {re.escape(module)} import|import {re.escape(module)}\b)"
            )
            if re.search(pattern, source, re.M):
                found[module] = {"sanitize_event_for_logging"}
        return found
    imports: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                continue  # explicit relative import; neither of the two routes
            imports.setdefault(node.module or "", set()).update(
                alias.name for alias in node.names
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.setdefault(alias.name, set())
    return imports


def _directory_imports(directory: Path) -> dict[str, set[str]]:
    """Every module imported by any Python file in a handler package."""
    merged: dict[str, set[str]] = {}
    for path in _python_files(directory):
        for module, names in _imported_modules(path).items():
            merged.setdefault(module, set()).update(names)
    return merged


def _imports_canonical_sanitizer(directory: Path) -> bool:
    """The function imports the sanitizer from ``idp_common`` — so it needs a layer."""
    for module, names in _directory_imports(directory).items():
        if module == CANONICAL_IMPORT or module.startswith(f"{CANONICAL_IMPORT}."):
            return True
        if module == "idp_common.utils" and VENDORED_MODULE in names:
            return True
    return False


def _imports_vendored_sanitizer(directory: Path) -> bool:
    """The function imports its own committed copy as a top-level sibling module."""
    return VENDORED_MODULE in _directory_imports(directory)


def _imports_the_sanitizer(directory: Path) -> bool:
    return _imports_canonical_sanitizer(directory) or _imports_vendored_sanitizer(
        directory
    )


# Collection constructors that wrap a literal: `frozenset({...})`, `set([...])`,
# `tuple([...])`, `list((...))`. A denylist written this way is still a denylist,
# and the previous scan — which required the assigned value to be a bare literal —
# could not see one. `PREVIOUSLY_MISSING_KEYS` above is itself a `frozenset({...})`,
# so the scan was blind to precisely the shape its own module uses.
_LITERAL_WRAPPERS = frozenset({"frozenset", "set", "tuple", "list"})
_MAX_WRAPPER_DEPTH = 3


def _string_literals(node: ast.AST, depth: int = 0) -> list[str] | None:
    """Return the string members of a collection literal, else None.

    Recognises a tuple/list/set literal, the keys of a dict literal, and any of
    those wrapped in a single-argument collection constructor.
    """
    if isinstance(node, ast.Call) and depth < _MAX_WRAPPER_DEPTH:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name in _LITERAL_WRAPPERS and len(node.args) == 1 and not node.keywords:
            return _string_literals(node.args[0], depth + 1)
        return None
    if isinstance(node, ast.Dict):
        elements: list[ast.expr] = [k for k in node.keys if k is not None]
    elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        elements = list(node.elts)
    else:
        return None
    values = []
    for element in elements:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            return None
        values.append(element.value)
    return values


# --- does the Lambda actually sanitize what it logs? -----------------------------
#
# Everything above proves the copies are consistent with each other. None of it
# proves any function *uses* one. This does.
#
# The check is on the DATAFLOW, not on the spelling. It would have been much
# shorter to forbid the literal text `json.dumps(event)`, and that version is worse
# than useless: it pins the exact shape of the line that happened to be wrong this
# time, so the identical leak walks straight past as `f"...{event}..."`, as
# `logger.info("%s", event)`, or as `str(event)`. A scanner that only recognises the
# shape you already found is a scanner that can only catch the bug you already
# fixed.
#
# So: find every log call, then ask of each argument "can the whole invocation event
# object reach here?" — descending through f-strings, `%` formatting, nested calls,
# and containers alike, and stopping at exactly two things: a narrowing operation
# (`event["x"]`, `event.get("x")`, `event.foo`, which yield a leaf, not the event),
# and `sanitize_event_for_logging(...)`, which is the point of the exercise.

_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)
SANITIZER_FUNC = "sanitize_event_for_logging"
EVENT_PARAM = "event"


def _callee_name(func: ast.expr) -> str | None:
    """The bare name being called: ``f`` for ``f()``, ``g`` for ``mod.g()``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_log_call(node: ast.expr) -> bool:
    """A ``logger.info(...)``-shaped call, or a bare ``print(...)``.

    ``print`` counts: in Lambda it lands in the same CloudWatch stream as the
    logger, so it leaks identically. Two functions under ``src/lambda`` did in fact
    use ``print`` for this (``calculate_capacity``, ``update_settings``), so this is
    not a hypothetical.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id == "print":
        return True
    if not isinstance(func, ast.Attribute) or func.attr not in _LOG_METHODS:
        return False
    # Receiver must look like a logger, so `session.info(...)` or
    # `response.get(...)`-adjacent calls are not dragged in.
    receiver = func.value
    if isinstance(receiver, ast.Name):
        return "log" in receiver.id.lower()
    if isinstance(receiver, ast.Attribute):
        return "log" in receiver.attr.lower()
    return False


def _whole_event_reaches(node: ast.AST) -> bool:
    """Can the *whole* object bound to the name ``event`` flow into ``node``?

    True for the bare name and for anything that carries it along unchanged:
    ``json.dumps(event)``, ``f"{event}"``, ``"%s" % event``, ``str(event)``,
    ``[event]``, ``json.dumps(event, default=str)``.

    False in exactly two situations, which is where all the judgement lives:

    * **Narrowed.** ``event["arguments"]``, ``event.get("fieldName")``,
      ``event.identity`` each evaluate to a piece of the event, not the event.
      Handlers log field names and operation names constantly and that is fine —
      flagging it would make this test unusable and get it deleted.
    * **Sanitized.** The name appears only inside a ``sanitize_event_for_logging``
      call, which is the required form.
    """
    if isinstance(node, ast.Name):
        return node.id == EVENT_PARAM
    if isinstance(node, ast.Call):
        if _callee_name(node.func) == SANITIZER_FUNC:
            return False  # the whole point: redacted before it reaches the log
        # Recurse into the arguments only. The callee expression itself
        # (`json.dumps`) cannot be the event.
        children: list[ast.AST] = list(node.args)
        children += [kw.value for kw in node.keywords]
        # `event.get(...)` — receiver is narrowed away, but a nested
        # `foo(event).bar()` still has to be followed.
        if isinstance(node.func, ast.Attribute) and not (
            isinstance(node.func.value, ast.Name) and node.func.value.id == EVENT_PARAM
        ):
            children.append(node.func.value)
        return any(_whole_event_reaches(child) for child in children)
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name) and node.value.id == EVENT_PARAM:
            return False  # `event.identity` — a piece of it
        return _whole_event_reaches(node.value)
    if isinstance(node, ast.Subscript):
        if isinstance(node.value, ast.Name) and node.value.id == EVENT_PARAM:
            return False  # `event["arguments"]` — a piece of it
        return any(_whole_event_reaches(child) for child in (node.value, node.slice))
    return any(_whole_event_reaches(child) for child in ast.iter_child_nodes(node))


def _rebinds_event(node: ast.AST) -> bool:
    """Does this statement bind the name ``event`` to something else?

    Needed because ``get_stepfunction_execution_resolver`` does
    ``for event in all_events:`` *inside* ``lambda_handler`` — from there on the
    name refers to a Step Functions execution-history record, not the invocation
    event, and treating those as leaks produced four false positives that would
    have had to be suppressed one by one.
    """
    targets: list[ast.expr] = []
    if isinstance(node, (ast.For, ast.AsyncFor)):
        targets = [node.target]
    elif isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        targets = [i.optional_vars for i in node.items if i.optional_vars is not None]
    elif isinstance(node, ast.ExceptHandler):
        return node.name == EVENT_PARAM
    for target in targets:
        for sub in ast.walk(target):
            if isinstance(sub, ast.Name) and sub.id == EVENT_PARAM:
                return True
    return False


def _event_log_violations_in_body(body: list[ast.stmt]) -> list[ast.expr]:
    """Log calls in ``body`` that leak the event, honouring rebinding.

    Walks statement lists in order so that a rebinding of ``event`` suppresses the
    check for everything after it — Python leaks a ``for`` target into the enclosing
    scope, so that is the real semantics, not a convenience.
    """
    found: list[ast.expr] = []
    for statement in body:
        if _rebinds_event(statement):
            # From here on `event` is something else. Stop, rather than continue
            # with a name that no longer means what this test is about.
            return found
        for node in ast.walk(statement):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue  # handled as its own scope by the caller
            if _is_log_call(node):
                assert isinstance(node, ast.Call)
                args: list[ast.AST] = list(node.args)
                args += [kw.value for kw in node.keywords]
                if any(_whole_event_reaches(arg) for arg in args):
                    found.append(node)
    return found


def _event_handling_functions(tree: ast.AST) -> list[ast.FunctionDef]:
    """Functions whose FIRST positional parameter is named ``event``.

    That is the Lambda handler convention, and it is what separates the real
    handlers (and the helpers they hand the event to, e.g. ``_get_caller_info``)
    from unrelated locals that happen to be called ``event``. Without it,
    ``parse_execution_history(events)`` — which loops ``for event in events`` over
    Step Functions history and logs freely — reads as three leaks.
    """
    handlers = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        positional = node.args.posonlyargs + node.args.args
        if positional and positional[0].arg == EVENT_PARAM:
            handlers.append(node)
    return handlers


def _raw_event_log_hits(path: Path) -> list[str]:
    """``file:line`` for every log call in ``path`` that can reach the raw event."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = []
    for handler in _event_handling_functions(tree):
        for call in _event_log_violations_in_body(handler.body):
            hits.append(f"{path.name}:{call.lineno} in {handler.name}()")
    return hits


def _label(path: Path) -> str:
    """Repo-relative where possible; absolute for the synthetic self-test trees."""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _raw_event_log_offenders(roots=LAMBDA_ROOTS) -> list[str]:
    """Every raw-event log call under ``roots``, as ``path:line in func()``.

    Takes the roots as an argument so that the self-test below can run the real
    collector over a synthetic tree, rather than asserting that the collector works
    by inspecting the code that implements it.
    """
    offenders = []
    for root in roots:
        for directory in sorted(p for p in root.iterdir() if p.is_dir()):
            for path in _python_files(directory):
                for hit in _raw_event_log_hits(path):
                    offenders.append(f"{_label(path.parent)}/{hit}")
    return offenders


def test_no_lambda_logs_the_raw_invocation_event():
    """A Lambda that logs its event sanitizes it. This is the #921/#977 defect itself.

    An api-resolver's invocation event carries ``identity.claims`` — Cognito
    ``sub``, ``email`` and group membership — on every authenticated call, and the
    chat and agent processors' events carry the user's prompt, which is the content
    of a private conversation. Both are precisely what the canonical denylist exists
    to redact. Writing them to CloudWatch in full is the leak; having a tidy,
    consistent, byte-identical copy of the redactor sitting unused in the same
    directory does not help.

    No allowlist, deliberately. Forty sites across both trees were fixed rather than
    exempted, including the 13 CloudFormation custom-resource handlers among them,
    whose events are stack metadata today: an exemption granted on the strength of what a given event
    happens to contain has to be re-audited every time that event's producer
    changes, and nothing would prompt that re-audit. If a function genuinely must
    log a raw event, that is a decision worth making in a review, not a name added
    to a list here.
    """
    offenders = _raw_event_log_offenders()
    assert not offenders, (
        "These log calls can write the unredacted invocation event — including "
        "identity.claims (Cognito sub, email, groups) and any user-supplied prompt "
        f"— to CloudWatch. Wrap the logged value in {SANITIZER_FUNC}(...), imported "
        f"from {CANONICAL_IMPORT} if the function carries an idp-common layer or "
        "from the vendored `log_sanitizer` module if it does not:\n  "
        + "\n  ".join(offenders)
    )


def test_the_raw_event_scan_reaches_handlers_in_both_lambda_trees():
    """The scan finds real event handlers under each root, not just under one.

    ``test_no_lambda_logs_the_raw_invocation_event`` passes both when a tree is
    clean and when it is not being read, and those two are indistinguishable from
    its output. This separates them: each root must contain at least one function
    whose first parameter is named ``event``, which is the shape the scan keys on.
    A root pointing at a directory that holds handlers but no such function — the
    tree renamed and a stub left behind, say — is reported here rather than
    silently reducing the scan to a no-op. (A root that does not exist at all fails
    earlier and louder: ``Path.iterdir`` raises.)

    Counting per root rather than asserting a total keeps this honest without
    pinning a number that changes whenever a Lambda is added.
    """
    for root in LAMBDA_ROOTS:
        handlers = 0
        for directory in (p for p in root.iterdir() if p.is_dir()):
            for path in _python_files(directory):
                handlers += len(
                    _event_handling_functions(
                        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                    )
                )
        assert handlers, (
            f"the raw-event scan found no function taking `{EVENT_PARAM}` as its "
            f"first parameter anywhere under {_label(root)}, so it is scanning "
            "nothing there and would pass however that tree is written"
        )


comprehension_types = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _denylist_positions(tree: ast.AST) -> dict[int, str]:
    """Describe where each expression node sits, for the failure message.

    Only cosmetic — the scan itself considers every position — but "default of
    _my_sanitize()" is a far more useful report than a bare line number.
    """
    where: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            where[id(node.value)] = f"{names[0] if names else '<unnamed>'} ="
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target = node.target
            name = target.id if isinstance(target, ast.Name) else "<unnamed>"
            where[id(node.value)] = f"{name} ="
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            label = f"default argument of {getattr(node, 'name', '<lambda>')}()"
            for default in node.args.defaults:
                where[id(default)] = label
            for default in node.args.kw_defaults:
                if default is not None:
                    where[id(default)] = label
        elif isinstance(node, ast.Call):
            label = f"argument to {_callee_name(node.func) or '<call>'}()"
            for arg in node.args:
                where[id(arg)] = label
            for kw in node.keywords:
                where[id(kw.value)] = label
        elif isinstance(node, ast.Compare):
            for comparator in node.comparators:
                where[id(comparator)] = "comparison operand (`k in (...)`)"
        elif isinstance(node, ast.Return) and node.value is not None:
            where[id(node.value)] = "returned literal"
        elif isinstance(node, comprehension_types):
            for generator in node.generators:
                where[id(generator.iter)] = "iterated in a comprehension"
    return where


def test_no_lambda_defines_its_own_denylist():
    """No hand-rolled sensitive-key list anywhere under either Lambda tree.

    Detected structurally — any collection literal of strings that overlaps the
    canonical denylist — so renaming ``_LOG_SENSITIVE_KEYS`` to something else does
    not slip past. Byte-identical vendored copies are the one allowed home for the
    list; ``test_vendored_copies_match_canonical`` proves they are identical.

    Scanned in **every expression position**, not just on the right-hand side of an
    assignment. The assignment-only version of this scan missed the two shapes a
    person is most likely to actually write::

        def _my_sanitize(obj, keys=("password", "secret", "token", "cookie")): ...
        if any(k in ("password", "secret", "token") for k in obj): ...

    Neither one ever binds the list to a name, and both were invisible: dropping
    either into a handler left this suite green.
    """
    canonical_keys = {k.lower() for k in _canonical_deny_keys()}
    offenders = []
    for root in LAMBDA_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if path.name == VENDORED_NAME:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            positions = _denylist_positions(tree)
            # `ast.walk` is breadth-first, so a wrapper such as `frozenset({...})` is
            # visited before the literal it wraps; keeping the first match per
            # (line, key set) reports the outermost node once rather than twice.
            seen: set[tuple[int, tuple[str, ...]]] = set()
            for node in ast.walk(tree):
                literals = _string_literals(node)
                if literals is None:
                    continue
                hits = {s.lower() for s in literals} & canonical_keys
                if len(hits) < _DENYLIST_MATCH_THRESHOLD:
                    continue
                key = (getattr(node, "lineno", -1), tuple(sorted(hits)))
                if key in seen:
                    continue
                seen.add(key)
                offenders.append(
                    f"{_label(path)}:{key[0]} "
                    f"{positions.get(id(node), '<expression>')} {sorted(hits)}"
                )
    assert not offenders, (
        "These files define their own log-redaction denylist instead of using the "
        "canonical one. Import sanitize_event_for_logging — from "
        f"{CANONICAL_IMPORT} if the function carries an idp-common layer, or from "
        "the vendored `log_sanitizer` module (add it with "
        "scripts/sync_resolver_log_sanitizer.sh) if it does not:\n  "
        + "\n  ".join(offenders)
    )


def _vendored_copies() -> list[Path]:
    copies: list[Path] = []
    for root in LAMBDA_ROOTS:
        copies.extend(root.rglob(VENDORED_NAME))
    return sorted(copies)


def test_vendored_copies_match_canonical():
    """Every vendored copy is byte-identical to the canonical module."""
    copies = _vendored_copies()
    assert copies, "expected at least one vendored log_sanitizer.py"
    canonical_text = CANONICAL.read_text(encoding="utf-8")
    for copy in copies:
        assert copy.read_text(encoding="utf-8") == canonical_text, (
            f"{_label(copy)} has drifted from {_label(CANONICAL)}. Edit the "
            "canonical file only, then run "
            "scripts/sync_resolver_log_sanitizer.sh."
        )


def test_vendored_denylist_is_exactly_canonical():
    """The denylist these functions actually apply is the canonical one.

    Byte-identity already implies this, but assert it on the loaded module too: this
    is the property that matters at runtime, and it is what fails if a future key is
    added to the canonical list without the copies being re-synced.
    """
    canonical_keys = _canonical_deny_keys()
    assert PREVIOUSLY_MISSING_KEYS <= canonical_keys, (
        "The canonical denylist no longer covers keys it is required to cover: "
        f"{sorted(PREVIOUSLY_MISSING_KEYS - canonical_keys)}"
    )
    for index, copy in enumerate(_vendored_copies()):
        module = _load_module_by_path(copy, f"_vendored_log_sanitizer_{index}")
        assert frozenset(module._DEFAULT_DENY_KEY_SUBSTRINGS) == canonical_keys, (
            f"{_label(copy)} applies a different denylist than the canonical "
            "module. Run scripts/sync_resolver_log_sanitizer.sh."
        )


def test_vendored_importers_have_a_copy_and_vice_versa():
    """A function importing the vendored module has one, and no copy is orphaned.

    An orphaned copy is dead weight in the package; a missing one is an ImportError
    at cold start. This is also what keeps the sync script honest now that it
    derives its own targets from these same imports: if the script's detection ever
    stopped matching an import it would leave that directory without a copy, and
    this is the test that says so.
    """
    importers, holders = set(), set()
    for directory in _lambda_dirs():
        if _imports_vendored_sanitizer(directory):
            importers.add(_label(directory))
        if (directory / VENDORED_NAME).exists():
            holders.add(_label(directory))
    assert importers == holders, (
        "Vendored log_sanitizer.py copies and the functions importing them are out "
        f"of step. Importing without a copy: {sorted(importers - holders)}; "
        f"carrying an unused copy: {sorted(holders - importers)}. Re-run "
        "scripts/sync_resolver_log_sanitizer.sh."
    )


def test_canonical_importers_carry_an_idp_common_layer():
    """Importing idp_common for the sanitizer requires a layer that provides it.

    Without one the import raises at cold start and every invocation fails, so this
    is the pairing that makes the two-route split safe: drop a function's layer and
    this test tells you to vendor the module instead.

    This is also the assertion that makes it safe for the template scan to miss a
    directory. Five directories under ``src/lambda`` are not placed by the CodeUri
    scan: three (``create_chat_session_resolver``, ``finetuning_data_generator``,
    ``ipset_updater``) are referenced by no template in the repository at all, and
    two are not Lambda packages — ``finetuning_state_machine`` holds a Step
    Functions definition, and ``external_idp_group_mapping`` holds the standalone,
    unit-tested twin of a handler the parent template deploys as ``InlineCode``.
    All five read as carrying no layer, which is the conservative answer, and this
    test is what would fail if one of them nonetheless imported the library.
    """
    layers = _layers_by_code_dir()
    broken = []
    for directory in _lambda_dirs():
        if not _imports_canonical_sanitizer(directory):
            continue
        if not layers.get(directory):
            broken.append(_label(directory))
    assert not broken, (
        f"These functions import {CANONICAL_IMPORT} but declare no IDPCommon layer "
        "in any CloudFormation template in this repo, so the import fails at cold "
        "start. Either attach the layer or vendor the module with "
        f"scripts/sync_resolver_log_sanitizer.sh: {sorted(broken)}"
    )


def test_every_resolver_directory_is_found_in_a_template():
    """Every api-resolver directory maps to a declared function.

    This is the guard on the guard. A function the template scan cannot find reads
    as "declares no IDPCommon layer", which is the unsafe direction: the layer test
    above would reject a correct canonical import and steer the author into
    vendoring a module the function could have imported from the layer it already
    carries. That is exactly what happened to finetuning_jobs_resolver (declared in
    the parent template) and list_documents_gsi_resolver (CodeUri without a
    leading `./`).

    Asserted for the resolver tree only, because the same invariant is simply not
    true of ``src/lambda``: five directories there are not Lambda packages with a
    CodeUri at all, for the reasons set out on
    ``test_canonical_importers_carry_an_idp_common_layer`` above. Making this test
    tree-wide would therefore mean either failing on those or carrying an exemption
    list for them, and neither buys anything — for a directory that does not use the
    redactor the layer answer is irrelevant, and for one that does, that test
    already fails in the safe direction.
    """
    found = _layers_by_code_dir()
    missing = [
        _label(d)
        for d in _lambda_dirs()
        if d.parent == RESOLVER_ROOT.resolve() and d not in found
    ]
    assert not missing, (
        "These directories under nested/api-resolvers/src/lambda have no matching "
        "CodeUri in any CloudFormation template in this repo, so the layer scan "
        "cannot tell whether they carry an IDPCommon layer and will assume they do "
        f"not: {sorted(missing)}"
    )


def test_sync_script_scans_the_same_roots_as_this_guard():
    """The sync script and this guard look at the same trees.

    The script used to carry a hand-written list of the nine resolver directories
    that needed a copy, and this test compared that list to the set derived here —
    two copies of one fact, with a test in the middle to stop them diverging. With
    thirty directories the list became the largest maintenance cost of the whole
    arrangement, so the script now derives its destinations from the imports in the
    handler sources, exactly as ``_imports_vendored_sanitizer`` does.

    What remains un-derivable is which trees to look in, so that stays an explicit
    list in both places and is compared here. A root present in one and absent from
    the other is the failure this replaces: the script would leave a whole tree
    unsynced, or the guard would stop checking one, and in both cases every test
    here would still pass.
    """
    script = SYNC_SCRIPT.read_text(encoding="utf-8")
    block = re.search(r"roots=\(\n(.*?)\n\)", script, re.S)
    assert block, f"could not find the roots=( ... ) list in {_label(SYNC_SCRIPT)}"
    listed = {
        line.strip().strip('"').strip("'")
        for line in block.group(1).splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    expected = {root.relative_to(REPO_ROOT).as_posix() for root in LAMBDA_ROOTS}
    assert listed == expected, (
        "scripts/sync_resolver_log_sanitizer.sh scans a different set of Lambda "
        f"trees than this guard. Only in the script: {sorted(listed - expected)}; "
        f"only in the guard: {sorted(expected - listed)}."
    )


# --- the scanners' own behaviour -------------------------------------------------
#
# All three scanners above are the kind of check that fails open: if
# `_string_literals` does not recognise a shape, a reintroduced denylist simply is
# not reported; if `_whole_event_reaches` does not follow a form of interpolation,
# a raw-event log is not reported; and if `_imported_modules` reads prose as an
# import, correct code is rejected. None of those failures is visible from the
# suite passing, so assert the behaviour directly.

# The whole value of the raw-event scan is that it is not a text match, so the
# forms below are the test. Each is the SAME defect as `json.dumps(event)` written
# differently, and a scanner that catches only the first is a scanner that catches
# only the bug we already fixed.
_RAW_EVENT_LOGS_THAT_MUST_BE_SEEN = {
    "json.dumps in an f-string": 'logger.info(f"e: {json.dumps(event)}")',
    "json.dumps as a lazy arg": 'logger.info("e: %s", json.dumps(event))',
    "bare name in an f-string": 'logger.info(f"e: {event}")',
    "bare name as a lazy arg": 'logger.info("e: %s", event)',
    "bare name, sole argument": "logger.info(event)",
    "str() coercion": "logger.info(str(event))",
    "repr() coercion": 'logger.debug(f"{event!r}")',
    "percent formatting": 'logger.info("e: %s" % event)',
    "str.format": 'logger.info("e: {}".format(event))',
    "concatenation": 'logger.info("e: " + str(event))',
    "inside a container": 'logger.info("e: %s", {"event": event})',
    "json.dumps with kwargs": "logger.info(json.dumps(event, default=str))",
    "json.dumps then sliced": 'print(f"{json.dumps(event, default=str)[:1000]}")',
    "pprint.pformat": "logger.info(pprint.pformat(event))",
    "debug level": 'logger.debug(f"{json.dumps(event)}")',
    "exception level": 'logger.exception(f"failed on {event}")',
    "print instead of logger": "print(json.dumps(event))",
    "print of the bare name": "print(event)",
    "logger.log with a level": 'logger.log(logging.INFO, f"{event}")',
    "module-level logging alias": 'LOG.info("%s", event)',
    "attribute logger": 'self.log.info("%s", event)',
    "keyword argument": 'logger.info("x", extra={"raw": event})',
}

# Narrowing an event down to a field is normal and must stay usable: handlers log
# the operation name and argument keys on nearly every call. Flagging these would
# make the test unusable, and an unusable test gets deleted rather than fixed.
_RAW_EVENT_LOGS_THAT_MUST_NOT_TRIP = {
    "sanitized, f-string": 'logger.info(f"e: {json.dumps(sanitize_event_for_logging(event))}")',
    "sanitized, lazy arg": 'logger.info("e: %s", sanitize_event_for_logging(event))',
    "sanitized with kwargs": "logger.info(json.dumps(sanitize_event_for_logging(event), default=str))",
    "sanitized then sliced": 'print(f"{json.dumps(sanitize_event_for_logging(event), default=str)[:1000]}")',
    "subscript": "logger.info(f\"{event['fieldName']}\")",
    "get() call": "logger.info(f\"{event.get('fieldName')}\")",
    "chained get()": "logger.info(f\"{event.get('arguments', {}).get('id')}\")",
    "attribute access": 'logger.info(f"{event.foo}")',
    "no event at all": 'logger.info("resolver invoked")',
    "a different name": 'logger.info(f"{json.dumps(other)}")',
    "len() of a field": 'logger.info("%s", len(event["arguments"]))',
}

_DENYLIST_SHAPES_THAT_MUST_BE_SEEN = {
    "set literal": '_K = {"password", "secret", "token"}\n',
    "tuple literal": '_K = ("password", "secret", "token")\n',
    "list literal": '_K = ["password", "secret", "token"]\n',
    "annotated assign": '_K: set = {"password", "secret", "token"}\n',
    "frozenset of a set": '_K = frozenset({"password", "secret", "token"})\n',
    "frozenset of a list": '_K = frozenset(["password", "secret", "token"])\n',
    "set of a list": '_K = set(["password", "secret", "token"])\n',
    "tuple of a list": '_K = tuple(["password", "secret", "token"])\n',
    "dict keys": '_K = {"password": "x", "secret": "x", "token": "x"}\n',
    "frozenset of dict keys": '_K = frozenset({"password": 1, "secret": 1, "token": 1})\n',
    # Never assigned to anything. These are the shapes an assignment-only scan
    # could not see, and they are not exotic — a default argument on a helper is
    # how a person actually writes this.
    "default argument": (
        'def _my_sanitize(obj, keys=("password", "secret", "token", "cookie")):\n'
        "    return obj\n"
    ),
    "keyword-only default": (
        'def _my_sanitize(obj, *, keys={"password", "secret", "token"}):\n'
        "    return obj\n"
    ),
    "lambda default": '_f = lambda o, k=("password", "secret", "token"): o\n',
    "inline membership test": (
        'if any(k in ("password", "secret", "token") for k in obj):\n    pass\n'
    ),
    "comparison operand": 'if key in ("password", "secret", "token"):\n    pass\n',
    "call argument": '_redact(obj, ["password", "secret", "token"])\n',
    "call keyword argument": '_redact(obj, keys=["password", "secret", "token"])\n',
    "bare expression statement": '("password", "secret", "token")\n',
    "returned literal": ('def _keys():\n    return ("password", "secret", "token")\n'),
}

_SHAPES_THAT_MUST_NOT_TRIP = {
    "two keys only": '_K = ("token", "cursor")\n',
    "unrelated strings": '_K = ("alpha", "beta", "gamma", "delta")\n',
    "non-literal": "_K = frozenset(some_other_module.KEYS)\n",
    "two keys in a default arg": 'def f(o, k=("token", "cursor")):\n    return o\n',
}

# Known residual gaps, not asserted either way: a denylist spelled as keyword
# arguments (`dict(password="x", ...)`), built by a comprehension, or assembled with
# `|=`/`.add()` across statements still escapes this scan. Each is a stranger way to
# write a constant than the shapes above, and closing them means evaluating
# arbitrary expressions.
#
# Which test is load-bearing for which failure, because it is easy to get this
# backwards (an earlier version of this comment did):
#
# * A vendored copy silently drifting from the canonical module —
#   `test_vendored_copies_match_canonical` is the guarantee.
# * A function hand-rolling a NEW local denylist in its own `index.py` — byte
#   identity of the copies says nothing whatsoever about that.
#   `test_no_lambda_defines_its_own_denylist` is the only guard, so its gaps are
#   real gaps and not merely defence in depth.
# * A function logging the raw event while using no denylist at all —
#   `test_no_lambda_logs_the_raw_invocation_event`. This is the one that maps to the
#   original defect; the other two are about consistency.
# * That scan looking at only one of the two Lambda trees, or at only `index.py`
#   within a directory — `test_the_raw_event_scan_reaches_handlers_in_both_lambda_trees`
#   and `test_the_raw_event_scan_covers_whole_directories_under_every_root`.


def _scan_snippet(log_call: str, *, param: str = EVENT_PARAM) -> list[str]:
    """Run the raw-event scan over ``log_call`` placed in a handler body."""
    source = f"def handler({param}, context):\n    {log_call}\n"
    tree = ast.parse(source)
    hits = []
    for handler in _event_handling_functions(tree):
        for call in _event_log_violations_in_body(handler.body):
            hits.append(f"{handler.name}:{call.lineno}")
    return hits


@pytest.mark.parametrize("label", sorted(_RAW_EVENT_LOGS_THAT_MUST_BE_SEEN))
def test_the_raw_event_scan_sees_every_way_of_logging_the_event(label):
    """The same leak, spelled twenty-two ways, is still the same leak.

    This is the test that makes the guard worth having. A check that forbade the
    literal string ``json.dumps(event)`` would pass every other entry here while the
    event still lands in CloudWatch verbatim — the scanner would be pinned to the
    shape of the line that happened to be wrong in #921. The spellings are not
    hypothetical: of the forty sites fixed under ``src/lambda``, three used ``print``
    rather than a logger, six passed the event as a lazy ``%s`` argument, three
    interpolated the bare name into an f-string, and two sliced
    ``json.dumps(event, default=str)`` to bound its length — which caps log volume
    and redacts nothing.
    """
    hits = _scan_snippet(_RAW_EVENT_LOGS_THAT_MUST_BE_SEEN[label])
    assert hits, (
        f"the raw-event scan cannot see `{label}`: "
        f"`{_RAW_EVENT_LOGS_THAT_MUST_BE_SEEN[label]}` would leak the unredacted "
        "invocation event and this suite would stay green"
    )


@pytest.mark.parametrize("label", sorted(_RAW_EVENT_LOGS_THAT_MUST_NOT_TRIP))
def test_the_raw_event_scan_allows_sanitized_and_narrowed_logging(label):
    """Sanitized events, and single fields pulled out of an event, are fine.

    False positives here are not harmless: handlers log the operation name on
    nearly every call, so a scan that flagged ``event.get("fieldName")`` would need
    dozens of suppressions and would be deleted instead of fixed.
    """
    snippet = _RAW_EVENT_LOGS_THAT_MUST_NOT_TRIP[label]
    assert not _scan_snippet(snippet), (
        f"the raw-event scan wrongly flagged `{label}`: `{snippet}` does not log "
        "the whole unredacted event"
    )


def test_the_raw_event_scan_covers_whole_directories_under_every_root(tmp_path):
    """Both roots are walked, and every Python file in a directory, not just index.py.

    Run against a synthetic pair of trees rather than against the repo, because the
    repo is clean: with no offending file anywhere, "the collector walks both roots"
    and "the collector walks nothing" are indistinguishable from a green run. This
    is the mutation probe for the widening itself, kept as a permanent test so that
    narrowing a root or going back to an ``index.py``-only scan fails here instead
    of quietly halving the coverage.
    """
    leak = "def handler(event, context):\n    logger.info(json.dumps(event))\n"
    clean = "def handler(event, context):\n    logger.info('called')\n"

    roots = []
    for root_name, offender in (
        ("tree_one", "index.py"),
        # A handler that is not in `index.py`, and is not even at the top level of
        # its directory: `src/lambda/chat_stream_processor` keeps its two processor
        # modules under `vendored/`, and both logged their raw event.
        ("tree_two", "vendored/processor.py"),
    ):
        root = tmp_path / root_name
        package = root / "some_function"
        (package / "vendored").mkdir(parents=True)
        (package / "unrelated.py").write_text(clean, encoding="utf-8")
        (package / offender).write_text(leak, encoding="utf-8")
        roots.append(root)

    offenders = _raw_event_log_offenders(roots)
    assert len(offenders) == 2, (
        "the collector did not report one leak per synthetic root; it is not "
        f"walking both roots, or not walking whole directories: {offenders}"
    )
    assert any("index.py" in o for o in offenders)
    assert any("processor.py" in o for o in offenders), (
        "a handler outside index.py was not scanned"
    )

    # And the clean sibling file is not reported, so the two assertions above are
    # not passing because everything is flagged.
    assert not any("unrelated.py" in o for o in offenders)


def test_the_raw_event_scan_ignores_an_unrelated_local_named_event():
    """``for event in all_events`` rebinds the name; those are not the invocation event.

    ``get_stepfunction_execution_resolver`` loops over Step Functions
    execution-history records under the name ``event`` — once inside
    ``lambda_handler`` itself, and again in two helpers. A scan that matched on the
    name alone reported four leaks there, all false. Suppressing four lines by hand
    is how a guard stops being trusted.
    """
    # Rebound by a for-loop inside the handler: not a hit.
    rebound = (
        "def lambda_handler(event, context):\n"
        "    for event in event['history']:\n"
        "        logger.warning(f'bad: {json.dumps(event)}')\n"
    )
    tree = ast.parse(rebound)
    hits = [
        call
        for handler in _event_handling_functions(tree)
        for call in _event_log_violations_in_body(handler.body)
    ]
    assert not hits, "a for-loop rebinding of `event` was treated as the event"

    # A helper whose first parameter is NOT `event` is not an event handler.
    helper = (
        "def parse_history(events):\n"
        "    for event in events:\n"
        "        logger.warning(f'{json.dumps(event)}')\n"
    )
    assert not _event_handling_functions(ast.parse(helper))

    # But a helper that IS handed the event is checked like the handler, because
    # `_get_caller_info(event)` in the stepfunction resolver is exactly that.
    assert _scan_snippet("logger.info(json.dumps(event))", param="event")
    assert not _scan_snippet("logger.info(json.dumps(event))", param="record")


def _denylist_key_hits(source: str) -> set[str]:
    """Every canonical key the real scan would match in ``source``.

    Uses exactly the same traversal as ``test_no_lambda_defines_its_own_denylist``
    — every expression position, not just assignments — so these self-tests cannot
    pass while the real scan is narrower.
    """
    canonical_keys = {k.lower() for k in _canonical_deny_keys()}
    hits: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        literals = _string_literals(node)
        if literals:
            hits |= {s.lower() for s in literals} & canonical_keys
    return hits


@pytest.mark.parametrize("label", sorted(_DENYLIST_SHAPES_THAT_MUST_BE_SEEN))
def test_the_denylist_scan_sees_every_collection_shape(label):
    """A denylist is a denylist in whatever container it is written in.

    `frozenset({...})` and a dict literal both escaped the original scan, which
    required the assigned value to be a bare tuple/list/set literal. The irony was
    that PREVIOUSLY_MISSING_KEYS in this very file is a `frozenset({...})`.

    The later entries here are never assigned at all — a default argument, a
    membership test, a call argument. Those escaped the scan even after the
    container shapes were fixed, because it still only inspected ``ast.Assign`` and
    ``ast.AnnAssign``.
    """
    hits = _denylist_key_hits(_DENYLIST_SHAPES_THAT_MUST_BE_SEEN[label])
    assert len(hits) >= _DENYLIST_MATCH_THRESHOLD, (
        f"the denylist scan cannot see a {label}; a hand-copied denylist written "
        f"that way would be reintroduced silently (matched only {sorted(hits)})"
    )


@pytest.mark.parametrize("label", sorted(_SHAPES_THAT_MUST_NOT_TRIP))
def test_the_denylist_scan_does_not_trip_on_ordinary_collections(label):
    hits = _denylist_key_hits(_SHAPES_THAT_MUST_NOT_TRIP[label])
    assert len(hits) < _DENYLIST_MATCH_THRESHOLD, (
        f"the denylist scan flagged an ordinary collection ({label}): {sorted(hits)}"
    )


def test_the_import_scan_ignores_prose_that_quotes_an_import(tmp_path):
    """A docstring or comment naming the canonical module is not an import of it.

    Every vendored copy carries the canonical ``Usage::`` docstring, one line of
    which is a real-looking ``from idp_common.utils.log_sanitizer import ...``.
    """
    handler = tmp_path / "prose_handler"
    handler.mkdir()
    (handler / "index.py").write_text(
        '"""Handler.\n'
        "\n"
        "Usage::\n"
        "\n"
        f"    from {CANONICAL_IMPORT} import sanitize_event_for_logging\n"
        '"""\n'
        f"# byte-identical vendored copy of {CANONICAL_IMPORT}\n"
        f"from {VENDORED_MODULE} import sanitize_event_for_logging\n",
        encoding="utf-8",
    )
    assert not _imports_canonical_sanitizer(handler)
    assert _imports_vendored_sanitizer(handler)
    assert _imports_the_sanitizer(handler)


def test_the_import_scan_sees_a_real_import_wherever_it_sits(tmp_path):
    """Including indented inside a function, a try block, or a non-index module."""
    handler = tmp_path / "indented_handler"
    (handler / "vendored").mkdir(parents=True)
    (handler / "index.py").write_text(
        "def handler(event, context):\n"
        f"    from {CANONICAL_IMPORT} import sanitize_event_for_logging\n"
        "    return sanitize_event_for_logging(event)\n",
        encoding="utf-8",
    )
    assert _imports_canonical_sanitizer(handler)
    assert not _imports_vendored_sanitizer(handler)

    # A handler package whose only importer is a module other than index.py still
    # counts: chat_stream_processor imports the sanitizer from its vendored copies
    # of the two chat processors, not from app.py.
    nested = tmp_path / "nested_handler"
    (nested / "vendored").mkdir(parents=True)
    (nested / "index.py").write_text("PLACEHOLDER = 1\n", encoding="utf-8")
    (nested / "vendored" / "processor.py").write_text(
        f"from {CANONICAL_IMPORT} import sanitize_event_for_logging\n",
        encoding="utf-8",
    )
    assert _imports_canonical_sanitizer(nested)
