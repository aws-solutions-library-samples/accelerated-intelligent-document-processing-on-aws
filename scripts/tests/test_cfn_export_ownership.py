# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Export ownership: one producing stack per export name, unconditionally.

CloudFormation export names are unique per account+region, and a stack update
**cannot hand one over**. A stack's exports are written at the very *end* of its
own update, while a nested stack creates itself — and claims its exports — during
the resource phase. So on an update that moves an export name from the parent to
a nested stack, the parent still holds the name when the nested stack asks for
it, and the nested stack fails with::

    Export with name <StackName>-TrackingTableName is already exported
    by stack <StackName>

That is what made ``EnableFeaturePlatform=false`` a one-way door: the parent
exported ``${AWS::StackName}-TrackingTableName`` under an
``EnableFeaturePlatform=false`` condition and the nested feature-platform stack
declared the identical name when enabled, so no stack created with the platform
off could ever adopt it
(`#845 <https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/845>`_).

**Complementary conditions are not a defence, and this module deliberately does
not accept them as one.** The two declarations in that bug *were* mutually
exclusive at any single point in time — the failure is in the transition, which no
per-condition analysis of the template can see. So export names are collected with
``Condition`` ignored, on both the ``Outputs`` entry and the nested-stack resource
that produces it: if two stacks in one deployment can name the same export at all,
that is the finding.

Three directions are checked:

``test_no_export_name_has_more_than_one_producer``
    the invariant above, over the whole nested-stack graph rooted at
    ``template.yaml``, plus every separately-deployed extension stack under
    ``feature-platform/`` (those share the host's ``<MainStackName>-`` export
    namespace, so one of them re-exporting a host value would collide too).
    Note the invariant is *one unconditional producer per name*, not *the stack
    that owns the resource must be the producer*: which of two stacks keeps the
    name is a migration-cost question, decided by what already imports it.

``test_every_export_shipped_extensions_import_has_a_producer``
    the reverse break. Withdrawing a producer is only safe if the name survives,
    so every ``Fn::ImportValue '<MainStackName>-X'`` in a shipped extension
    template must still resolve to exactly one producer in the host graph.
    Removing one declaration without leaving another would satisfy the first test
    and leave every extension unable to resolve its import.

``test_host_exports_keep_their_pinned_producer``
    a *move*, which the two checks above both accept: moving a name from one stack
    to the other leaves exactly one producer and leaves the name with a producer.
    So every host export name has its producing stack pinned in a committed
    constant — all of them, not only the ones an in-repo template imports, because
    the closed-source extensions delivered by AWS Marketplace subscription import
    names no in-repo template does and cannot be read from here. CloudFormation
    refuses to let a producer withdraw an export another stack imports, so a move
    fails the HOST stack's update for every customer who has such a stack
    installed.

Resolution is symbolic: ``${AWS::StackName}`` becomes a per-stack token, a
parameter becomes whatever the parent passes for it (so ``MainStackName:
!Ref AWS::StackName`` resolves the nested namespace onto the parent's), and
anything it cannot resolve becomes a token unique to that template and key.
Unique tokens can hide a finding but cannot invent one, so a failure here is
always real. Two guards close that gap rather than leaving it silent:
``test_every_export_name_resolves`` and
``test_every_import_in_a_shipped_extension_resolves``.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PARENT_TEMPLATE = "template.yaml"

# Directory holding the separately-deployed extension stacks. Each is launched
# by an admin into the same account+region as the host, and each is handed the
# host's stack name as MainStackName, so they all share one export namespace.
EXTENSION_ROOT = "feature-platform"
# Deployed as a nested stack of the parent, not standalone — reached by the graph
# walk instead.
NESTED_EXTENSION_DIRS = {"main-stack-extensions"}
# Build output and vendored third-party trees under an extension directory.
_SKIP_PARTS = {".aws-sam", "node_modules", "build", "dist", "vendor"}

NESTED_PLATFORM_TEMPLATE = "feature-platform/main-stack-extensions/template.yaml"

# Every stack the checks below must see, pinned by SOURCE path. A threshold is
# satisfied by the wrong set: with `len(stacks) >= 8`, four of the five nested
# stacks could drop out of discovery and the gate would still pass while looking
# at none of patterns/unified, nested/api-resolvers, nested/bedrockkb or
# nested/multi-doc-discovery. An exact set fails instead, in both directions, so a
# template that is added or removed forces a decision here.
EXPECTED_HOST_SOURCES = {
    PARENT_TEMPLATE,
    "patterns/unified/template.yaml",
    "nested/api-resolvers/template.yaml",
    "nested/bedrockkb/template.yaml",
    "nested/multi-doc-discovery/template.yaml",
    NESTED_PLATFORM_TEMPLATE,
}

# The standalone extension stacks, pinned for the same reason. Seven against a
# `>= 6` threshold left room for one to vanish from discovery unnoticed, taking
# all of its imports and exports with it.
EXPECTED_EXTENSION_SOURCES = {
    "feature-platform/confbench-testset/template.yaml",
    "feature-platform/feature-template/template.yaml",
    "feature-platform/idp-data-generator/template.yaml",
    "feature-platform/pii-anonymizer/template.yaml",
    "feature-platform/sample-feature/template.yaml",
    "feature-platform/sample-health-insurance-review/template.yaml",
    "feature-platform/seller-entitlement-service/template.yaml",
}

# ---------------------------------------------------------------------------
# The host export contract: every export name the host deployment produces, and
# which stack must produce it.
#
# This is the pin that catches a *move*. A move leaves exactly one producer, and
# leaves the name with a producer, so it satisfies every other check here — which
# is why the expected producer has to be committed rather than read back out of
# the templates. Changing an entry is the thing to think hard about: an installed
# stack's Fn::ImportValue holds the name, and CloudFormation does not allow the
# current producer to withdraw an export that is in use, so a producer change
# fails the HOST stack's update for every customer who has that stack installed.
#
# All 23 names are produced by the nested platform stack. The main template
# deliberately declares no export at all: it cannot claim a name the nested stack
# claims, and it is the parent whose declaration can safely be withdrawn, because
# an extension can only exist on a platform-ON host.
# ---------------------------------------------------------------------------
PINNED_PRODUCERS: dict[str, str] = dict.fromkeys(
    (
        "ApplyFeatureConfigPresetFunctionArn",
        "ConfigurationTableArn",
        "ConfigurationTableName",
        "CustomerManagedEncryptionKeyArn",
        "DiscoveryBucketName",
        "InputBucketName",
        "InstalledFeaturesTableArn",
        "InstalledFeaturesTableName",
        "OutputBucketName",
        "RegisterFeatureFunctionArn",
        "RegisterFeatureHooksFunctionArn",
        "ReportingBucketArn",
        "ReportingBucketName",
        "TestSetBucketName",
        "TrackingTableArn",
        "TrackingTableName",
        "UserPoolClientId",
        "UserPoolId",
        "UsersTableArn",
        "UsersTableName",
        "WebUIBucketArn",
        "WebUIBucketName",
        "WorkingBucketName",
    ),
    NESTED_PLATFORM_TEMPLATE,
)

# Sentinels for pseudo-parameters that are constant across a deployment.
PSEUDO = {
    "AWS::Region": "REGION",
    "AWS::AccountId": "ACCOUNTID",
    "AWS::Partition": "PARTITION",
    "AWS::URLSuffix": "URLSUFFIX",
}

UNRESOLVED = "<unresolved:"


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags."""


def _tag_to_python(loader, tag_suffix, node):
    # `!Ref x` is `{"Ref": x}`, NOT `{"Fn::Ref": x}` — every other short form
    # takes the `Fn::` prefix. The distinction is load-bearing: map `!Ref` to
    # `Fn::Ref` and every reference becomes an unresolvable node, so no export
    # name resolves and the whole module finds nothing while passing.
    key = "Ref" if tag_suffix == "Ref" else f"Fn::{tag_suffix}"
    if isinstance(node, yaml.ScalarNode):
        return {key: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {key: loader.construct_sequence(node, deep=True)}
    return {key: loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag_to_python)


def _load(rel_path: str) -> dict:
    return yaml.load((REPO_ROOT / rel_path).read_text(), Loader=_CfnLoader) or {}


def _source_template_for(template_url: Any) -> str | None:
    """Map a nested stack's TemplateURL to the SOURCE template it is built from.

    ``./feature-platform/main-stack-extensions/.aws-sam/packaged.yaml``
    -> ``feature-platform/main-stack-extensions/template.yaml``

    The packaged file is a build artifact absent from a clean checkout, so the
    source template is what gets read (same approach as
    ``test_nested_stack_parameters.py``).
    """
    if not isinstance(template_url, str):
        return None
    match = re.match(r"^\./(.+?)/\.aws-sam/packaged\.ya?ml$", template_url.strip())
    if not match:
        return None
    for candidate in (
        f"{match.group(1)}/template.yaml",
        f"{match.group(1)}/template.yml",
    ):
        if (REPO_ROOT / candidate).is_file():
            return candidate
    return None


class _Stack:
    """One CloudFormation stack in a deployment, with its substitution env."""

    def __init__(self, stack_id: str, source: str, env: dict[str, str]) -> None:
        self.stack_id = stack_id
        self.source = source
        # Parameter name / pseudo-parameter -> resolved string.
        self.env = env

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_Stack({self.stack_id!r}, {self.source!r})"


def _opaque(source: str, key: str) -> str:
    return f"{UNRESOLVED}{source}:{key}>"


def _resolve(value: Any, stack: _Stack, key: str) -> str:
    """Resolve a template value to a string, symbolically.

    Only the forms that appear in an export name or an ImportValue are handled:
    plain strings, ``Ref``, ``Fn::Sub`` (scalar and list form), ``Fn::Join``.
    Anything else yields an opaque token unique to (template, key).
    """
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return _opaque(stack.source, key)

    if "Ref" in value and len(value) == 1:
        name = value["Ref"]
        if isinstance(name, str) and name in stack.env:
            return stack.env[name]
        return _opaque(stack.source, f"{key}:Ref:{name}")

    if "Fn::Sub" in value and len(value) == 1:
        body = value["Fn::Sub"]
        local: dict[str, Any] = {}
        if isinstance(body, list):
            template_str = body[0]
            if len(body) > 1 and isinstance(body[1], dict):
                local = body[1]
        else:
            template_str = body
        if not isinstance(template_str, str):
            return _opaque(stack.source, key)

        def sub_one(match: re.Match[str]) -> str:
            token = match.group(1)
            if token in local:
                return _resolve(local[token], stack, f"{key}:{token}")
            if token in stack.env:
                return stack.env[token]
            return _opaque(stack.source, f"{key}:{token}")

        return re.sub(r"\$\{([^}]+)\}", sub_one, template_str)

    if "Fn::Join" in value and len(value) == 1:
        joiner = value["Fn::Join"]
        if (
            isinstance(joiner, list)
            and len(joiner) == 2
            and isinstance(joiner[1], list)
        ):
            sep = joiner[0] if isinstance(joiner[0], str) else ""
            return sep.join(
                _resolve(part, stack, f"{key}[{i}]") for i, part in enumerate(joiner[1])
            )

    return _opaque(stack.source, key)


def _base_env(stack_id: str) -> dict[str, str]:
    env = dict(PSEUDO)
    env["AWS::StackName"] = stack_id
    return env


def _walk_nested(
    stack: _Stack, out: list[_Stack], seen: set[str], depth: int = 0
) -> None:
    """Depth-first walk of ``AWS::CloudFormation::Stack`` resources.

    The nested-stack resource's ``Condition`` is ignored on purpose: a stack that
    exists under *some* parameter combination is a potential producer of its
    exports, and it is precisely the toggle between combinations that breaks.
    """
    if depth > 10:  # pragma: no cover - cycle guard
        raise AssertionError(f"nested stack depth exceeded at {stack.source}")
    resources = _load(stack.source).get("Resources", {}) or {}
    for logical_id, body in sorted(resources.items()):
        if not isinstance(body, dict) or body.get("Type") != (
            "AWS::CloudFormation::Stack"
        ):
            continue
        props = body.get("Properties") or {}
        source = _source_template_for(props.get("TemplateURL"))
        if not source:
            continue
        child_id = f"{stack.stack_id}/{logical_id}"
        if child_id in seen:
            continue
        seen.add(child_id)
        # A nested stack's own AWS::StackName is a CloudFormation-generated name,
        # distinct from the parent's — hence a distinct token.
        env = _base_env(f"{child_id}-GENERATED")
        for param, passed in (props.get("Parameters") or {}).items():
            env[param] = _resolve(passed, stack, f"Parameters.{param}")
        child = _Stack(child_id, source, env)
        out.append(child)
        _walk_nested(child, out, seen, depth + 1)


def _extension_templates() -> list[str]:
    """Every standalone extension template under ``EXTENSION_ROOT``.

    Discovery is by **content** and at **any depth**, matching the choice
    ``scripts/discover_templates.sh cfn`` makes for ``make cfn-lint`` and
    ``make check-arn-partitions``: anything declaring ``AWSTemplateFormatVersion``
    counts, whatever it is called and wherever it sits. A ``template.yaml``-only
    glob would not see a ``.yml``-named template, and a one-level glob would not
    see a second template in a subdirectory — both are stacks in the same region
    sharing the same export namespace, so both have to be visible here.
    """
    root = REPO_ROOT / EXTENSION_ROOT
    found = []
    for pattern in ("*.yaml", "*.yml"):
        for candidate in root.rglob(pattern):
            relative = candidate.relative_to(root)
            if set(relative.parts) & NESTED_EXTENSION_DIRS:
                continue
            if any(part in _SKIP_PARTS for part in relative.parts):
                continue
            try:
                text = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "AWSTemplateFormatVersion" not in text:
                continue
            found.append(str(candidate.relative_to(REPO_ROOT)))
    return sorted(found)


def _extension_stacks() -> list[_Stack]:
    """Each standalone extension template as a stack in the host's namespace.

    An extension is launched with ``MainStackName`` = the host's stack name;
    everything else it declares is its own.
    """
    stacks = []
    for source in _extension_templates():
        label = str(Path(source).parent.relative_to(EXTENSION_ROOT))
        env = _base_env(f"EXT-{label}")
        env["MainStackName"] = "HOSTSTACK"
        stacks.append(_Stack(f"EXT/{label}", source, env))
    return stacks


def _host_stacks() -> list[_Stack]:
    """The host deployment: the parent plus every nested stack, recursively."""
    root = _Stack("HOSTSTACK", PARENT_TEMPLATE, _base_env("HOSTSTACK"))
    stacks = [root]
    _walk_nested(root, stacks, set())
    return stacks


def _deployment_stacks() -> list[_Stack]:
    """Every stack sharing one export namespace: host graph + extension stacks."""
    return _host_stacks() + _extension_stacks()


def _exports_of(stack: _Stack) -> dict[str, str]:
    """Resolved export name -> Output logical id, ignoring ``Condition``."""
    outputs = _load(stack.source).get("Outputs", {}) or {}
    exports: dict[str, str] = {}
    for logical_id, body in outputs.items():
        if not isinstance(body, dict):
            continue
        export = body.get("Export")
        if not isinstance(export, dict) or "Name" not in export:
            continue
        name = _resolve(export["Name"], stack, f"Outputs.{logical_id}.Export.Name")
        exports[name] = logical_id
    return exports


def _producers() -> dict[str, list[tuple[str, str, str]]]:
    """export name -> [(stack id, source template, Output logical id), ...]."""
    producers: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for stack in _deployment_stacks():
        for name, logical_id in _exports_of(stack).items():
            producers[name].append((stack.stack_id, stack.source, logical_id))
    return producers


def _imports_of(stack: _Stack) -> dict[str, str]:
    """Resolved Fn::ImportValue name -> a path showing where it appears."""
    found: dict[str, str] = {}

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key == "Fn::ImportValue":
                    found.setdefault(_resolve(val, stack, path), path)
                walk(val, f"{path}/{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")

    walk(_load(stack.source), stack.source)
    return found


# --------------------------------------------------------------------------- #
# Discovery guards — a silent zero here would make every assertion vacuous.
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_deployment_graph_is_discovered() -> None:
    """Every host stack is reached, by name — not merely "enough of them"."""
    found = {s.source for s in _host_stacks()}
    assert found == EXPECTED_HOST_SOURCES, (
        f"host stacks reached from {PARENT_TEMPLATE} do not match the expected "
        f"set.\n  not reached: {sorted(EXPECTED_HOST_SOURCES - found)}\n"
        f"  unexpected: {sorted(found - EXPECTED_HOST_SOURCES)}\n"
        "If a TemplateURL no longer points at '<dir>/.aws-sam/packaged.yaml', "
        "update _source_template_for(); if a nested stack was genuinely added or "
        "removed, update EXPECTED_HOST_SOURCES in the same change."
    )


@pytest.mark.unit
def test_extension_stacks_are_discovered() -> None:
    """Every standalone extension template is found, by name.

    One dropping out of discovery takes all of its imports and exports with it,
    silently, which a count would not notice.
    """
    found = set(_extension_templates())
    assert found == EXPECTED_EXTENSION_SOURCES, (
        "standalone extension templates found do not match the expected set.\n"
        f"  not found: {sorted(EXPECTED_EXTENSION_SOURCES - found)}\n"
        f"  unexpected: {sorted(found - EXPECTED_EXTENSION_SOURCES)}\n"
        "A new extension must be added to EXPECTED_EXTENSION_SOURCES so its "
        "imports and exports are covered here."
    )
    assert all(part not in rel for rel in found for part in NESTED_EXTENSION_DIRS), (
        "the nested platform stack must be reached by the graph walk, not as a "
        f"standalone extension: {sorted(found)}"
    )


@pytest.mark.unit
def test_every_host_export_is_pinned() -> None:
    """The host export contract is complete, in both directions.

    Without this, a name absent from ``PINNED_PRODUCERS`` is a name whose producer
    nothing checks — so adding an export and moving it later would both pass.
    """
    host_sources = {s.source for s in _host_stacks()}
    producers = _producers()
    discovered = {
        name.removeprefix("HOSTSTACK-")
        for name, entries in producers.items()
        if any(source in host_sources for _, source, _ in entries)
    }
    pinned = set(PINNED_PRODUCERS)
    assert discovered == pinned, (
        "the host export contract in PINNED_PRODUCERS is out of step with the "
        "templates.\n"
        f"  produced but not pinned: {sorted(discovered - pinned)}\n"
        f"  pinned but not produced: {sorted(pinned - discovered)}\n"
        "Add a new export with the stack that must produce it. Removing one is an "
        "upgrade-visible change for anything that imports it — check "
        "`aws cloudformation list-imports --export-name <StackName>-<name>` on a "
        "live stack before assuming nothing does."
    )


@pytest.mark.unit
def test_every_export_name_resolves() -> None:
    """No export name may be left partly unresolved.

    An unresolved token is unique per template, so it cannot cause a false
    collision — but it could *mask* a real one. Fail instead, so the resolver
    gets extended when a new intrinsic shows up in an export name.
    """
    unresolved = sorted(
        f"{name}  (in {producers[0][1]} Outputs.{producers[0][2]})"
        for name, producers in _producers().items()
        if UNRESOLVED in name
    )
    assert not unresolved, (
        "export name(s) could not be fully resolved — extend _resolve() so the "
        "collision check can see them:\n  " + "\n  ".join(unresolved)
    )


# --------------------------------------------------------------------------- #
# The invariant.
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_no_export_name_has_more_than_one_producer() -> None:
    """One export name, one producing stack — regardless of any Condition.

    Two stacks declaring the same name cannot hand it over in a single update
    (see the module docstring), so the deployment has a parameter combination it
    can never reach. Give the value a single owner and export it unconditionally.
    """
    duplicates = {
        name: producers
        for name, producers in sorted(_producers().items())
        if len({(stack_id, source) for stack_id, source, _ in producers}) > 1
    }
    report = "\n".join(
        f"  {name}\n"
        + "\n".join(
            f"    - {stack_id} ({source}) Outputs.{logical_id}"
            for stack_id, source, logical_id in producers
        )
        for name, producers in duplicates.items()
    )
    assert not duplicates, (
        "export name(s) declared by more than one stack in the same "
        "deployment:\n"
        f"{report}\n\n"
        "CloudFormation cannot move an export name between stacks in one "
        "update: a stack's exports are written at the END of its update while a "
        "nested stack claims its own during the resource phase, so the incoming "
        "producer fails with 'Export with name X is already exported by stack "
        "Y'. Mutually exclusive Conditions do not help — the failure is in the "
        "transition. Leave exactly ONE declaration, unconditional. Which stack "
        "keeps it is a migration question, not an ownership one: prefer the stack "
        "whose declaration existing installed stacks already import, because a "
        "producer that has to withdraw an in-use export fails the update."
    )


@pytest.mark.unit
def test_every_import_in_a_shipped_extension_resolves() -> None:
    """An unresolvable import name would be skipped by the check below.

    Same reasoning as ``test_every_export_name_resolves``, in the other
    direction: the producer lookup can only be done on a resolved name, so
    silently skipping the rest turns a missing producer into a pass. The case to
    watch for is an import that names the host stack through some parameter other
    than ``MainStackName`` — say ``${HostStackName}-Whatever``.
    """
    unresolved: list[str] = []
    for stack in _extension_stacks():
        for name, path in sorted(_imports_of(stack).items()):
            if UNRESOLVED in name:
                unresolved.append(f"{stack.source} at {path}")
    assert not unresolved, (
        "Fn::ImportValue name(s) in a shipped extension template could not be "
        "resolved, so the producer check below cannot see them:\n  "
        + "\n  ".join(unresolved)
        + "\n\nIf the template names the host stack through a parameter other "
        "than MainStackName, teach _extension_stacks() about it; otherwise "
        "extend _resolve()."
    )


@pytest.mark.unit
def test_every_export_shipped_extensions_import_has_a_producer() -> None:
    """The reverse break: an import whose producer was removed, not moved.

    Extension stacks are deployed separately and import host exports by name.
    CloudFormation refuses both to create a stack importing a non-existent export
    and to withdraw an export that is in use, so a name a shipped extension
    imports must always have exactly one producer in the host graph.

    Unresolvable names are not skipped here — ``
    test_every_import_in_a_shipped_extension_resolves`` fails on those first.
    """
    producers = _producers()
    host_sources = {s.source for s in _host_stacks()}
    missing: list[str] = []
    for stack in _extension_stacks():
        for name, path in sorted(_imports_of(stack).items()):
            if UNRESOLVED in name:
                continue  # reported by the guard above
            host_producers = [
                p for p in producers.get(name, []) if p[1] in host_sources
            ]
            if not host_producers:
                missing.append(f"{name}  imported at {path}")
    assert not missing, (
        "shipped extension template(s) import export name(s) no stack in the "
        "host deployment produces:\n  " + "\n  ".join(missing) + "\n\n"
        "Installing the extension would fail because the import cannot be "
        "resolved. If an export moved between stacks, keep the NAME identical."
    )


# --------------------------------------------------------------------------- #
# Closed-source consumers. Everything above reads templates in THIS repository,
# so it covers the OSS catalog and nothing else — while extensions delivered by
# AWS Marketplace subscription import host exports too, and cannot be read from
# here. Their imports are recorded below so the producer pin above has a reason to
# cover names no in-repo template imports.
#
# ⚠️ Two limits on what this can prove, both of which matter when the gate fires:
#
#  1. The recorded import set is a LOWER BOUND, not a census. It cannot be
#     verified from this repository, and the featureId check below compares
#     catalogued ids against the recorded ids — not import sets — so a NEW import
#     taken by an already-listed extension is invisible here. Before moving or
#     removing a host export, read the closed-source templates.
#  2. Only CATALOGUED extensions appear. A closed-source stack that is not yet
#     listed in the marketplace manifest can already be installed in a customer
#     account and importing host exports. `aws cloudformation list-imports
#     --export-name <StackName>-<name>` against a live stack is the only
#     authoritative answer to "is anything importing this".
# --------------------------------------------------------------------------- #

MARKETPLACE_MANIFEST = "config_library/extensions-marketplace.yaml"

# The minimum every extension imports to install itself: its ui-deployer invokes
# the register hook and writes its bundle into the host bucket, and its API
# verifies host-issued JWTs. Source: feature-platform/feature-template, the
# authoring scaffold, which imports exactly these four.
_INSTALL_CONTRACT = frozenset(
    {
        "RegisterFeatureFunctionArn",
        "WebUIBucketName",
        "UserPoolId",
        "UserPoolClientId",
    }
)

# Host export SUFFIXES (the part after `<StackName>-`) each catalogued marketplace
# extension imports, beyond the install contract above. See the limits noted in
# the block comment: extend an entry whenever one of them takes a new host import.
MARKETPLACE_EXTENSION_HOST_IMPORTS: dict[str, frozenset[str]] = {
    "idp-monitor": _INSTALL_CONTRACT
    | {
        "TrackingTableName",
        "TrackingTableArn",
        "ConfigurationTableName",
        "ConfigurationTableArn",
        "ReportingBucketName",
        "ReportingBucketArn",
        "CustomerManagedEncryptionKeyArn",
    },
    "idp-auto-optimizer": _INSTALL_CONTRACT
    | {
        "TrackingTableName",
        "TrackingTableArn",
        "ConfigurationTableName",
        "CustomerManagedEncryptionKeyArn",
    },
}


@pytest.mark.unit
def test_every_catalogued_marketplace_extension_declares_its_host_imports() -> None:
    """A new paid extension must not slip in unaccounted for, in either direction.

    This compares featureIds only. It cannot see an already-listed extension
    taking a *new* host import — see the limits noted above this test.
    """
    manifest = _load(MARKETPLACE_MANIFEST)
    catalogued = {
        entry["featureId"]
        for entry in (manifest.get("features") or [])
        if isinstance(entry, dict) and entry.get("featureId")
    }
    assert catalogued, (
        f"no featureId found in {MARKETPLACE_MANIFEST} — discovery is broken, so "
        "the check below proves nothing"
    )
    declared = set(MARKETPLACE_EXTENSION_HOST_IMPORTS)
    assert catalogued == declared, (
        "MARKETPLACE_EXTENSION_HOST_IMPORTS is out of step with "
        f"{MARKETPLACE_MANIFEST}.\n"
        f"  catalogued but not declared: {sorted(catalogued - declared)}\n"
        f"  declared but not catalogued: {sorted(declared - catalogued)}\n"
        "Those templates are closed-source, so read the extension's own template "
        "to fill in the entry rather than guessing — what is recorded here is a "
        "lower bound on what it imports, and nothing in this repository can check "
        "it."
    )


@pytest.mark.unit
def test_marketplace_recorded_imports_are_pinned_host_exports() -> None:
    """Every recorded marketplace import must be a name the pin actually covers.

    Catches a typo or a stale suffix in the map, which would otherwise record a
    dependency that no check then protects.
    """
    recorded = set().union(*MARKETPLACE_EXTENSION_HOST_IMPORTS.values())
    unknown = sorted(recorded - set(PINNED_PRODUCERS))
    assert not unknown, (
        "MARKETPLACE_EXTENSION_HOST_IMPORTS names host export(s) absent from "
        f"PINNED_PRODUCERS: {unknown}. Either the suffix is wrong, or the host no "
        "longer produces it — in which case the extension importing it is already "
        "broken."
    )


def _known_consumers_of(suffix: str) -> list[str]:
    """Shipped stacks known to import ``<StackName>-<suffix>``.

    In-repo extension templates are read; the marketplace entries come from the
    recorded lower bound, so an empty result does not mean nobody imports it.
    """
    consumers = [
        extension
        for extension, imports in sorted(MARKETPLACE_EXTENSION_HOST_IMPORTS.items())
        if suffix in imports
    ]
    for stack in _extension_stacks():
        if f"HOSTSTACK-{suffix}" in _imports_of(stack):
            consumers.append(stack.stack_id.removeprefix("EXT/"))
    return sorted(set(consumers))


@pytest.mark.unit
def test_host_exports_keep_their_pinned_producer() -> None:
    """Moving an export between stacks breaks the host update where it is imported.

    This is the check that sees a *move*, which every other check here accepts: a
    move leaves exactly one producer and leaves the name with a producer. It covers
    every host export name, not only the ones an in-repo template imports, because
    the closed-source extensions import names no in-repo template does.
    """
    producers = _producers()
    host_sources = {s.source for s in _host_stacks()}
    problems: list[str] = []
    for suffix, expected_source in sorted(PINNED_PRODUCERS.items()):
        actual = sorted(
            {
                source
                for _, source, _ in producers.get(f"HOSTSTACK-{suffix}", [])
                if source in host_sources
            }
        )
        if actual != [expected_source]:
            consumers = _known_consumers_of(suffix)
            problems.append(
                f"<StackName>-{suffix}: expected {expected_source}, found "
                f"{actual or 'no producer'}; known importers: "
                f"{', '.join(consumers) or 'none in this repository'}"
            )
    assert not problems, (
        "host export(s) changed producing stack, or lost their producer:\n  "
        + "\n  ".join(problems)
        + "\n\nAn installed stack's Fn::ImportValue holds the name, and "
        "CloudFormation does not allow the current producer to withdraw an export "
        "that is in use, so this fails the HOST stack's update for every customer "
        "who has such a stack installed. Keep the producer, or stage the move over "
        "two releases: add a second name, migrate consumers, retire the first "
        "later.\n\n"
        "Before deciding nothing imports it, note the 'known importers' list is a "
        "LOWER BOUND — it reads the in-repo extension templates plus a "
        "hand-maintained record of the catalogued closed-source ones, so it cannot "
        "see a new import by a closed-source extension, an uncatalogued one, or a "
        "customer-authored stack. Read the closed-source templates, and check a "
        "live stack with `aws cloudformation list-imports --export-name "
        "<StackName>-<name>`. If the move is genuinely intended, update "
        "PINNED_PRODUCERS in the same change and say so in the CHANGELOG."
    )


# --------------------------------------------------------------------------- #
# Meta-tests. A structural gate that passes on the bug it was written for is
# worth nothing, so both failure shapes are pinned against synthetic templates.
# --------------------------------------------------------------------------- #


def _write_synthetic(
    root: Path,
    *,
    parent_exports_name: bool,
    parent_condition: str | None,
    nested_exports_name: bool,
) -> None:
    """A two-stack deployment plus one extension importing the export.

    The parent always declares the Output; ``parent_exports_name`` decides whether
    it also declares the ``Export``, which is the only part that can collide.
    """
    parent_output: dict[str, Any] = {
        "Description": "tracking table",
        "Value": {"Ref": "TrackingTable"},
    }
    if parent_exports_name:
        parent_output["Export"] = {
            "Name": {"Fn::Sub": "${AWS::StackName}-TrackingTableName"}
        }
    if parent_condition:
        parent_output["Condition"] = parent_condition
    parent = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Parameters": {"EnablePlatform": {"Type": "String", "Default": "true"}},
        "Conditions": {
            "PlatformOn": {"Fn::Equals": [{"Ref": "EnablePlatform"}, "true"]},
            "PlatformOff": {"Fn::Equals": [{"Ref": "EnablePlatform"}, "false"]},
        },
        "Resources": {
            "TrackingTable": {"Type": "AWS::DynamoDB::Table"},
            "PlatformStack": {
                "Type": "AWS::CloudFormation::Stack",
                "Condition": "PlatformOn",
                "Properties": {
                    "TemplateURL": "./platform/.aws-sam/packaged.yaml",
                    "Parameters": {
                        "MainStackName": {"Ref": "AWS::StackName"},
                        "TrackingTableName": {"Ref": "TrackingTable"},
                    },
                },
            },
        },
        "Outputs": {"TrackingTableName": parent_output},
    }

    nested_outputs: dict[str, Any] = {
        "PlatformFunctionArn": {
            "Value": "arn",
            "Export": {"Name": {"Fn::Sub": "${MainStackName}-PlatformFunctionArn"}},
        }
    }
    if nested_exports_name:
        nested_outputs["TrackingTableName"] = {
            "Value": {"Ref": "TrackingTableName"},
            "Export": {"Name": {"Fn::Sub": "${MainStackName}-TrackingTableName"}},
        }
    nested = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Parameters": {
            "MainStackName": {"Type": "String"},
            "TrackingTableName": {"Type": "String"},
        },
        "Outputs": nested_outputs,
    }

    extension = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Parameters": {"MainStackName": {"Type": "String"}},
        "Resources": {
            "Fn": {
                "Type": "AWS::Serverless::Function",
                "Properties": {
                    "Environment": {
                        "Variables": {
                            "T": {
                                "Fn::ImportValue": {
                                    "Fn::Sub": "${MainStackName}-TrackingTableName"
                                }
                            }
                        }
                    }
                },
            }
        },
    }

    (root / "platform").mkdir(parents=True, exist_ok=True)
    (root / "extensions" / "demo").mkdir(parents=True, exist_ok=True)
    (root / "template.yaml").write_text(yaml.safe_dump(parent))
    (root / "platform" / "template.yaml").write_text(yaml.safe_dump(nested))
    (root / "extensions" / "demo" / "template.yaml").write_text(
        yaml.safe_dump(extension)
    )


@pytest.fixture
def synthetic(tmp_path, monkeypatch):
    """Point the module's discovery at a synthetic tree under ``tmp_path``."""

    this_module = sys.modules[__name__]

    def build(**kwargs) -> None:
        _write_synthetic(tmp_path, **kwargs)
        monkeypatch.setattr(this_module, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(this_module, "EXTENSION_ROOT", "extensions")
        monkeypatch.setattr(
            this_module,
            "PINNED_PRODUCERS",
            {
                "TrackingTableName": "platform/template.yaml",
                "PlatformFunctionArn": "platform/template.yaml",
            },
        )

    return build


@pytest.mark.unit
def test_gate_catches_the_845_collision(synthetic) -> None:
    """Complementary conditions must NOT satisfy the gate — that was the bug."""
    synthetic(
        parent_exports_name=True,
        parent_condition="PlatformOff",
        nested_exports_name=True,
    )
    with pytest.raises(AssertionError, match="more than one stack"):
        test_no_export_name_has_more_than_one_producer()


@pytest.mark.unit
def test_gate_catches_a_withdrawn_export(synthetic) -> None:
    """Dropping the nested re-export without adding a parent one is also a break.

    It satisfies the duplicate check — nobody produces the name twice — while
    leaving every extension unable to resolve its import.
    """
    synthetic(
        parent_exports_name=False,
        parent_condition=None,
        nested_exports_name=False,
    )
    test_no_export_name_has_more_than_one_producer()
    with pytest.raises(AssertionError, match="no stack in the host deployment"):
        test_every_export_shipped_extensions_import_has_a_producer()


@pytest.mark.unit
def test_gate_accepts_single_unconditional_ownership(synthetic) -> None:
    """The shape this repo uses: the nested stack owns it, the parent does not.

    The parent keeps a plain Output with no Export — visible to
    ``describe-stacks``, incapable of colliding.
    """
    synthetic(
        parent_exports_name=False,
        parent_condition=None,
        nested_exports_name=True,
    )
    test_no_export_name_has_more_than_one_producer()
    test_every_import_in_a_shipped_extension_resolves()
    test_every_export_shipped_extensions_import_has_a_producer()
    test_every_export_name_resolves()
    test_every_host_export_is_pinned()
    test_host_exports_keep_their_pinned_producer()


@pytest.mark.unit
def test_gate_catches_a_producer_move(synthetic) -> None:
    """A clean move — one stack to the other, still exactly one producer.

    This is the shape every other check here accepts, and the one that breaks the
    host update wherever the name is already imported.
    """
    synthetic(
        parent_exports_name=True,
        parent_condition=None,
        nested_exports_name=False,
    )
    # Nothing is duplicated and the import still resolves, so those pass.
    test_no_export_name_has_more_than_one_producer()
    test_every_export_shipped_extensions_import_has_a_producer()
    # The pin is what notices.
    with pytest.raises(AssertionError, match="changed producing stack"):
        test_host_exports_keep_their_pinned_producer()


@pytest.mark.unit
def test_gate_catches_an_unpinned_export(synthetic, monkeypatch) -> None:
    """A host export with no entry in the pin is an export whose producer is unchecked."""
    synthetic(
        parent_exports_name=False,
        parent_condition=None,
        nested_exports_name=True,
    )
    monkeypatch.setattr(
        sys.modules[__name__],
        "PINNED_PRODUCERS",
        {"PlatformFunctionArn": "platform/template.yaml"},
    )
    with pytest.raises(AssertionError, match="produced but not pinned"):
        test_every_host_export_is_pinned()


@pytest.mark.unit
def test_gate_catches_an_unresolvable_import(synthetic, tmp_path) -> None:
    """An import naming the host through an unknown parameter must not be skipped."""
    synthetic(
        parent_exports_name=False,
        parent_condition=None,
        nested_exports_name=True,
    )
    extension = tmp_path / "extensions" / "demo" / "template.yaml"
    extension.write_text(
        yaml.safe_dump(
            {
                "AWSTemplateFormatVersion": "2010-09-09",
                "Parameters": {"HostStackName": {"Type": "String"}},
                "Resources": {
                    "Fn": {
                        "Type": "AWS::Serverless::Function",
                        "Properties": {
                            "Environment": {
                                "Variables": {
                                    "T": {
                                        "Fn::ImportValue": {
                                            "Fn::Sub": "${HostStackName}-NoSuchExportAtAll"
                                        }
                                    }
                                }
                            }
                        },
                    }
                },
            }
        )
    )
    with pytest.raises(AssertionError, match="could not be resolved"):
        test_every_import_in_a_shipped_extension_resolves()


@pytest.mark.unit
def test_gate_sees_a_duplicate_producer_in_an_extension_subdirectory(
    synthetic, tmp_path
) -> None:
    """Extension discovery is recursive — a nested template is still a stack."""
    synthetic(
        parent_exports_name=False,
        parent_condition=None,
        nested_exports_name=True,
    )
    sub = tmp_path / "extensions" / "demo" / "sub"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "template.yaml").write_text(
        yaml.safe_dump(
            {
                "AWSTemplateFormatVersion": "2010-09-09",
                "Parameters": {"MainStackName": {"Type": "String"}},
                "Outputs": {
                    "Dup": {
                        "Value": "x",
                        "Export": {
                            "Name": {"Fn::Sub": "${MainStackName}-TrackingTableName"}
                        },
                    }
                },
            }
        )
    )
    with pytest.raises(AssertionError, match="more than one stack"):
        test_no_export_name_has_more_than_one_producer()
