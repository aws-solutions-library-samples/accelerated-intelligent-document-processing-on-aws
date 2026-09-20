# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Repo-wide gate on X-Ray tracing: the parameter controls it, and instrumented
Lambdas have a segment to write into.

Two defects motivated this gate (#983), and they are mirror images of each other:

* **A hardcoded control.** Both templates declared an ``EnableXRayTracing``
  parameter described as "Enable X-Ray tracing", and in both it reached exactly
  one resource: the state machine. Every Lambda that traced at all wrote a
  literal ``Tracing: Active``, so setting the parameter to ``false`` changed
  nothing about Lambda tracing and nothing about the X-Ray bill. Nineteen
  functions were in that state.
* **Instrumentation with nowhere to land.** Three functions called
  ``xray_recorder.put_annotation`` (or wrapped their handler in
  ``@xray_recorder.capture``) while declaring no ``Tracing`` at all, which
  defaults to ``PassThrough``: a segment exists only when an upstream caller
  already sampled the request, so the annotation is usually discarded. The two
  whose annotation *values* were correct at the Python level were the two least
  likely to produce a filterable trace.

Neither is visible in a passing build or a successful deployment, which is why
both persisted. The rules below are the durable half of the fix.

Rules enforced:

1. **No function hardcodes its tracing mode**, in any template that declares the
   parameter. A ``Tracing`` property on ``AWS::Serverless::Function`` (or
   ``TracingConfig.Mode`` on ``AWS::Lambda::Function``, or
   ``Globals.Function.Tracing``, which sets the mode for a whole template at
   once) must be an ``Fn::If`` on the tracing condition, not a literal. A literal
   ``Active`` is the #983 defect; a literal ``PassThrough`` is the same defect
   pointing the other way, since it also cannot be changed at deploy time.
   Templates that hardcode a mode and have **no** parameter to hang it on are the
   independently deployed ``feature-platform/`` extension stacks; they are listed
   exactly, in both directions, by
   ``test_templates_without_the_parameter_are_the_known_set``, so a seventh one
   fails rather than joining a silent exemption. "Independently deployed" is that
   exemption's whole justification, so it is itself checked, by
   ``test_exempt_templates_are_not_nested_stacks_of_the_parent``: the list held a
   template that ``template.yaml`` deploys as a nested stack and already passes 27
   parameters to, while the written reason said the parameter could not reach it.
2. **A template that declares tracing wires the parameter.** It defines the
   condition, the condition tests the parameter, and the parameter exists — and a
   nested template that declares the parameter is *passed* it by the parent, in
   the stack resource that instantiates **that** template. "Passed to some
   nested stack" is not the same claim and would let a new nested stack trace
   unconditionally while the gate stayed green.
3. **A Lambda whose source imports the X-Ray SDK declares a tracing mode.**
   Instrumentation with no ``Tracing`` property is instrumentation whose output
   depends on somebody else's sampling decision.
4. **A traced function's role can write a trace segment.** SAM attaches the X-Ray
   managed policy only to a role it *generates*, so a function with an explicit
   ``Role:`` can declare a mode, deploy cleanly, and write nothing —
   ``PipelineHooksDispatcherFunction`` was in that state. Its role now carries
   ``AWSXrayWriteOnlyAccess``.

Rule 1 is the one that stops #983 regressing: the next Lambda added is otherwise
as likely as not to copy whichever convention its neighbour uses.

Deliberately NOT asserted: that a particular function is traced, or what the
parameter's default is. Those are product decisions that belong in the template
and the changelog, not pinned here — a gate that froze the default would have to
be edited to change it, which is exactly the wrong way round.

Both rules 1 and 3 are checked against a **mutation** in
``TestGateCatchesReintroduction``, so "the gate passes" cannot come to mean "the
gate looks at nothing".
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DISCOVER = REPO_ROOT / "scripts" / "discover_templates.sh"

TRACING_PARAMETER = "EnableXRayTracing"
TRACING_CONDITION = "EnableXRayTracingCondition"

#: SAM's property, and the raw Lambda equivalent. Both are covered so the raw
#: resource type is not an unguarded route back to a hardcoded mode.
SERVERLESS_FUNCTION = "AWS::Serverless::Function"
LAMBDA_FUNCTION = "AWS::Lambda::Function"
FUNCTION_TYPES = {SERVERLESS_FUNCTION, LAMBDA_FUNCTION}

#: Importing this means the function creates or annotates X-Ray segments itself.
#: Broader than ``put_annotation`` on purpose: ``patch_all()`` and
#: ``@xray_recorder.capture`` also need a segment that exists.
XRAY_SDK_MARKER = "aws_xray_sdk"

#: Test and fixture modules sitting beside a handler import the SDK only to stub
#: it out. They are not deployed and say nothing about the function's tracing.
NON_RUNTIME_FILE = re.compile(r"(^test_|^conftest\.py$|_test\.py$)")

#: Floors, not exact counts, so adding a function does not edit this file. A drop
#: below either means discovery broke — which would make every rule below pass
#: vacuously — rather than that the repo genuinely shrank. Measured: 31 functions
#: declare a conditional tracing mode (7 in ``template.yaml``, 15 in
#: ``patterns/unified/template.yaml``, 9 in
#: ``feature-platform/main-stack-extensions/template.yaml``) and 9 source
#: directories import the X-Ray SDK — the 8 with ``put_annotation`` plus
#: ``rule-validation-orchestration-function``, which only decorates its handler.
MIN_TRACED_FUNCTIONS = 20
MIN_XRAY_SOURCE_DIRS = 6


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags.

    Subclasses ``SafeLoader``, so the ``python/object`` constructors that make
    ``yaml.load`` dangerous are never registered, and the multi-constructor
    below only ever returns plain scalars, lists and dicts.
    """


def _tag_to_python(loader: yaml.Loader, tag_suffix: str, node: yaml.Node) -> dict:
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{tag_suffix}": loader.construct_sequence(node, deep=True)}
    return {f"Fn::{tag_suffix}": loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag_to_python)


def _load_text(text: str) -> dict:
    loader = _CfnLoader(text)
    try:
        return loader.get_single_data() or {}
    finally:
        loader.dispose()


def _load(path: Path) -> dict:
    """Parse one template. Deliberately does not catch ``YAMLError``."""
    return _load_text(path.read_text(encoding="utf-8"))


def _repo_templates() -> list[Path]:
    """Every CloudFormation template in the repo, found by content.

    Delegates to the same script ``make cfn-lint`` and
    ``make check-arn-partitions`` use, so the gates cannot drift apart over which
    files count as templates, and a template added under a directory nobody
    thought to list is covered from the moment it exists.
    """
    out = subprocess.run(
        [str(DISCOVER), "cfn"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [REPO_ROOT / line for line in out.splitlines() if line]


def _functions(template: dict) -> dict[str, dict]:
    resources = template.get("Resources") or {}
    if not isinstance(resources, dict):
        return {}
    return {
        name: body
        for name, body in resources.items()
        if isinstance(body, dict) and body.get("Type") in FUNCTION_TYPES
    }


def _tracing_of(body: dict) -> Any:
    """The declared tracing mode, or ``None`` when the function declares none.

    ``AWS::Serverless::Function`` spells it ``Tracing: <mode>``;
    ``AWS::Lambda::Function`` spells it ``TracingConfig: {Mode: <mode>}``.
    """
    props = body.get("Properties") or {}
    if not isinstance(props, dict):
        return None
    if body.get("Type") == LAMBDA_FUNCTION:
        config = props.get("TracingConfig")
        if config is None:
            return None
        if isinstance(config, dict) and "Mode" in config:
            return config["Mode"]
        # An intrinsic standing in for the whole ``TracingConfig`` block, or a
        # malformed one: returned as-is so rule 1 JUDGES it. Reading ``Mode``
        # out of it would yield ``None``, and rule 1 reads ``None`` as "not
        # traced", which is the one answer that must not be inferred from a
        # shape this function does not understand.
        return config
    return props.get("Tracing")


def _is_conditional_on_tracing(node: Any) -> bool:
    """True when ``node`` is ``Fn::If`` on the tracing condition, ``Active`` first.

    The TRUE branch is pinned to ``Active`` because an inverted
    ``!If [EnableXRayTracingCondition, PassThrough, Active]`` is a copy-paste
    slip with no other signal: the template still reads as wired to the
    parameter, cfn-lint accepts it, and the only symptom is tracing being on
    exactly when the operator asked for it to be off.

    The FALSE branch is deliberately left free. Whether "off" is spelled
    ``PassThrough`` or the property is omitted on that leg resolves the same
    way, so pinning it would fail a template over a cosmetic difference.
    """
    if not isinstance(node, dict):
        return False
    value = node.get("Fn::If")
    return (
        isinstance(value, list)
        and len(value) == 3
        and value[0] == TRACING_CONDITION
        and value[1] == "Active"
    )


def _condition_tests_the_parameter(node: Any) -> bool:
    """True when a ``Conditions`` entry is an ``Fn::Equals`` on the parameter."""
    if not isinstance(node, dict):
        return False
    operands = node.get("Fn::Equals")
    if not isinstance(operands, list):
        return False
    # Both spellings: the long form is ``Ref``, and the loader above renders the
    # short form ``!Ref`` as ``Fn::Ref``.
    return any(
        isinstance(operand, dict)
        and any(operand.get(key) == TRACING_PARAMETER for key in ("Ref", "Fn::Ref"))
        for operand in operands
    )


def _globals_tracing(template: dict) -> Any:
    """``Globals.Function.Tracing``, which applies to every function at once.

    Covered because it is the cheapest possible bypass of a per-resource rule:
    one line at the top of a template sets the mode for the whole stack, and the
    six independently deployed ``feature-platform/`` extension templates really do
    declare it there.
    """
    function_globals = (template.get("Globals") or {}).get("Function")
    if not isinstance(function_globals, dict):
        return None
    return function_globals.get("Tracing")


def _hardcoded_tracing(template: dict) -> dict[str, Any]:
    """Rule 1: tracing modes that are not conditional on the parameter.

    Keyed by logical id, plus the pseudo-id ``Globals.Function`` for a
    template-wide default.
    """
    findings: dict[str, Any] = {}
    globals_tracing = _globals_tracing(template)
    if globals_tracing is not None and not _is_conditional_on_tracing(globals_tracing):
        findings["Globals.Function"] = globals_tracing
    for name, body in _functions(template).items():
        tracing = _tracing_of(body)
        if tracing is None:
            continue  # absent is allowed: it means "not traced by this stack"
        if not _is_conditional_on_tracing(tracing):
            findings[name] = tracing
    return findings


def _declares_the_parameter(template: dict) -> bool:
    return TRACING_PARAMETER in (template.get("Parameters") or {})


# --------------------------------------------------------------------------
# Rule 4: a traced function can actually write a segment.
# --------------------------------------------------------------------------

#: The X-Ray write actions a traced function needs. ``*`` and ``xray:*`` count
#: too, and are matched as prefixes below.
XRAY_WRITE_ACTIONS = {"xray:puttracesegments", "xray:puttelemetryrecords"}

#: Managed policies that carry those actions. Matched case-insensitively on the
#: ARN's policy name, so the ``${AWS::Partition}`` prefix does not matter.
XRAY_MANAGED_POLICIES = {
    "awsxraywriteonlyaccess",
    "awsxraydaemonwriteaccess",
    "awsxrayfullaccess",
}


def _explicit_role_logical_id(body: dict) -> str | None:
    """The logical id in ``Role: !GetAtt SomeRole.Arn``, or ``None``.

    Returns ``None`` both when there is no ``Role`` (SAM generates one) and when
    the value is a shape this cannot resolve; the caller distinguishes the two.
    """
    role = (body.get("Properties") or {}).get("Role")
    if role is None:
        return None
    if isinstance(role, dict):
        target = role.get("Fn::GetAtt")
        if isinstance(target, str):
            return target.split(".")[0]
        if isinstance(target, list) and target:
            return str(target[0])
    return None


def _grants_xray_write(role_body: dict) -> bool:
    """True when an ``AWS::IAM::Role`` can write X-Ray segments."""
    props = role_body.get("Properties") or {}

    for arn in props.get("ManagedPolicyArns") or []:
        for text in _flatten_strings(arn):
            if text.rsplit("/", 1)[-1].lower() in XRAY_MANAGED_POLICIES:
                return True

    for policy in props.get("Policies") or []:
        document = (policy or {}).get("PolicyDocument") or {}
        for statement in document.get("Statement") or []:
            if not isinstance(statement, dict) or statement.get("Effect") != "Allow":
                continue
            actions = statement.get("Action")
            actions = [actions] if isinstance(actions, str) else (actions or [])
            for action in actions:
                lowered = str(action).lower()
                if lowered in XRAY_WRITE_ACTIONS or lowered in {"*", "xray:*"}:
                    return True
    return False


def _traced_without_a_grant(template: dict) -> dict[str, str]:
    """Rule 4: traced functions whose role cannot write a trace segment.

    SAM attaches the X-Ray managed policy only to a role it **generates**. A
    function with an explicit ``Role:`` gets nothing, so it can declare a tracing
    mode, be deployed, and silently write no segment — the #983 defect one layer
    down, and the state ``PipelineHooksDispatcherFunction`` was in.

    An unresolvable ``Role`` (an imported ARN, a ``!Ref`` to a parameter, a role
    defined in another template) is reported rather than skipped: this gate
    cannot see whether that role grants anything, and reading "cannot tell" as
    "fine" is how the original defect survived.
    """
    resources = template.get("Resources") or {}
    findings: dict[str, str] = {}
    for name, body in _functions(template).items():
        if _tracing_of(body) is None:
            continue
        if (body.get("Properties") or {}).get("Role") is None:
            continue  # SAM generates the role and attaches the policy itself
        role_id = _explicit_role_logical_id(body)
        if role_id is None:
            findings[name] = "Role is a shape this gate cannot resolve"
            continue
        role_body = resources.get(role_id)
        if not isinstance(role_body, dict) or role_body.get("Type") != "AWS::IAM::Role":
            findings[name] = f"Role {role_id} is not an AWS::IAM::Role in this template"
            continue
        if not _grants_xray_write(role_body):
            findings[name] = f"{role_id} grants no xray:PutTraceSegments"
    return findings


# --------------------------------------------------------------------------
# Mapping a function resource to the source tree that runs inside it.
# --------------------------------------------------------------------------

#: ``patterns/unified/buildspec.yml`` tags each image
#: ``<basename of the source dir, underscores turned into hyphens>-$IMAGE_VERSION``.
#: The reverse mapping is derived from the directory listing rather than
#: transcribed, so a renamed directory cannot leave a stale table behind.
_IMAGE_TAG = re.compile(r":(?P<tag>[A-Za-z0-9._-]+?)-\$\{ImageVersion\}")


def _source_dirs_by_image_tag(template_dir: Path) -> dict[str, Path]:
    src = template_dir / "src"
    if not src.is_dir():
        return {}
    return {
        child.name.replace("_", "-"): child for child in src.iterdir() if child.is_dir()
    }


def _flatten_strings(node: Any) -> list[str]:
    """Every string anywhere in ``node`` — enough to find an ``Fn::Sub`` body."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for value in node.values() for s in _flatten_strings(value)]
    if isinstance(node, list):
        return [s for item in node for s in _flatten_strings(item)]
    return []


def _source_for(body: dict, template_path: Path) -> Path | None:
    """The directory holding the function's code, or ``None`` for inline code.

    Handles all three shapes this repo uses: a ``CodeUri`` path relative to the
    template, an ``ImageUri`` whose tag names a directory under the template's
    ``src/``, and inline code (which has no directory).
    """
    props = body.get("Properties") or {}
    template_dir = template_path.parent

    code_uri = props.get("CodeUri")
    if isinstance(code_uri, str) and code_uri.strip():
        return (template_dir / code_uri).resolve()

    for text in _flatten_strings(props.get("ImageUri")):
        match = _IMAGE_TAG.search(text)
        if match:
            by_tag = _source_dirs_by_image_tag(template_dir)
            resolved = by_tag.get(match.group("tag"))
            if resolved is not None:
                return resolved.resolve()
    return None


def _runtime_sources(directory: Path) -> list[Path]:
    return [
        path
        for path in sorted(directory.rglob("*.py"))
        if not NON_RUNTIME_FILE.search(path.name) and "__pycache__" not in path.parts
    ]


def _uses_xray(body: dict, template_path: Path) -> bool:
    """True when the code that runs in this function imports the X-Ray SDK.

    Inline code is read from the template itself; there is no directory to walk.
    """
    directory = _source_for(body, template_path)
    if directory is None:
        props = body.get("Properties") or {}
        inline = props.get("InlineCode")
        return isinstance(inline, str) and XRAY_SDK_MARKER in inline
    if not directory.is_dir():
        return False
    return any(
        XRAY_SDK_MARKER in path.read_text(encoding="utf-8", errors="replace")
        for path in _runtime_sources(directory)
    )


def _instrumented_without_tracing(template: dict, path: Path) -> list[str]:
    """Rule 3: functions that instrument X-Ray but declare no tracing mode."""
    return sorted(
        name
        for name, body in _functions(template).items()
        if _tracing_of(body) is None and _uses_xray(body, path)
    )


# --------------------------------------------------------------------------
# Rules, over every template in the repo.
# --------------------------------------------------------------------------

ALL_TEMPLATES = _repo_templates()
TEMPLATE_IDS = [str(p.relative_to(REPO_ROOT)) for p in ALL_TEMPLATES]


@pytest.mark.unit
def test_discovery_is_not_empty() -> None:
    """Every rule below iterates this list; an empty one passes them all."""
    assert len(ALL_TEMPLATES) >= 20, (
        f"scripts/discover_templates.sh cfn returned {len(ALL_TEMPLATES)} "
        f"templates, which is too few to be right — every assertion in this "
        f"file would pass vacuously"
    )


@pytest.mark.unit
@pytest.mark.parametrize("path", ALL_TEMPLATES, ids=TEMPLATE_IDS)
def test_no_function_hardcodes_its_tracing_mode(path: Path) -> None:
    template = _load(path)
    if not _declares_the_parameter(template):
        pytest.skip(
            f"{path.relative_to(REPO_ROOT)} has no {TRACING_PARAMETER} parameter; "
            f"covered by test_templates_without_the_parameter_are_the_known_set"
        )
    findings = _hardcoded_tracing(template)
    assert not findings, (
        f"{path.relative_to(REPO_ROOT)}: these set a literal tracing mode, so "
        f"{TRACING_PARAMETER} cannot turn tracing on or off for them "
        f"(issue #983): {findings}. Write "
        f"`Tracing: !If [{TRACING_CONDITION}, Active, PassThrough]` instead, and "
        f"define the condition if the template does not have it yet."
    )


#: Templates that hardcode a tracing mode and have no ``EnableXRayTracing``
#: parameter to hang it on. Each declares ``Globals.Function.Tracing: Active``,
#: which means tracing is unconditional there for the same reason it used to be
#: unconditional in the main templates.
#:
#: The reason each entry is here is **structural, and it is the only reason that
#: counts**: the stack is installed from the Extensions catalog as its own
#: CloudFormation stack, launched by the operator with its own parameters, and
#: nothing in ``template.yaml`` instantiates it. The main stack's parameter has no
#: route to it. Concretely, the catalog's install URL
#: (``get_feature_launch_url``) pre-fills exactly two host-derived values,
#: ``MainStackName`` and ``FeatureBucket``, plus whatever the extension's own
#: ``feature.yaml`` advertises in ``defaultParameters`` — an extension-authored
#: default, not a host value. So a per-extension ``EnableXRayTracing`` parameter
#: would be a knob the operator has to find and set on each installed stack
#: separately; it would not make the main stack's setting reach them.
#:
#: ``feature-platform/main-stack-extensions/template.yaml`` was in this list and is
#: not any more, because for that one the reason was **false**: it is a nested
#: stack of ``template.yaml`` (``FeaturePlatformStack``), already receives 27
#: parameters from the parent including ``LogLevel`` and ``LogRetentionDays``, and
#: now receives ``EnableXRayTracing`` the same way. Its nine Lambdas were the whole
#: of #983 surviving inside the main deployment.
#:
#: What remains here is a real, documented limitation rather than an oversight:
#: ``EnableXRayTracing=false`` on the main stack does not turn tracing off in an
#: installed extension stack. ``docs/monitoring.md`` states it, and says which
#: functions it costs money for — of the 21 functions in these six templates, the
#: 13 with a SAM-generated role get ``AWSXrayWriteOnlyAccess`` attached
#: automatically (SAM does that whenever ``Tracing`` is declared) and do emit
#: traces; the 8 with an explicit role carry no X-Ray grant, so they cannot write
#: a segment and emit nothing.
#:
#: The list is asserted EXACTLY, in both directions, so a seventh such template
#: fails this test instead of joining a silent exemption, and removing a hardcode
#: from one of these forces the entry to be deleted rather than left behind.
HARDCODED_WITHOUT_PARAMETER = {
    "feature-platform/confbench-testset/template.yaml",
    "feature-platform/feature-template/template.yaml",
    "feature-platform/idp-data-generator/template.yaml",
    "feature-platform/pii-anonymizer/template.yaml",
    "feature-platform/sample-feature/template.yaml",
    "feature-platform/sample-health-insurance-review/template.yaml",
}


@pytest.mark.unit
def test_templates_without_the_parameter_are_the_known_set() -> None:
    found = {
        str(path.relative_to(REPO_ROOT))
        for path in ALL_TEMPLATES
        if not _declares_the_parameter(_load(path)) and _hardcoded_tracing(_load(path))
    }
    unexpected = found - HARDCODED_WITHOUT_PARAMETER
    assert not unexpected, (
        f"these templates hardcode a tracing mode and declare no "
        f"{TRACING_PARAMETER} parameter to control it: {sorted(unexpected)}. "
        f"Either add the parameter and the condition and make the mode "
        f"conditional, or — if the stack is deployed independently and genuinely "
        f"cannot take the main stack's parameter — add it to "
        f"HARDCODED_WITHOUT_PARAMETER with the reason."
    )
    stale = HARDCODED_WITHOUT_PARAMETER - found
    assert not stale, (
        f"these entries no longer hardcode a tracing mode (or no longer exist), "
        f"so the exemption is stale and should be deleted: {sorted(stale)}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("path", ALL_TEMPLATES, ids=TEMPLATE_IDS)
def test_a_template_that_uses_the_condition_wires_the_parameter(path: Path) -> None:
    template = _load(path)
    uses_it = any(
        _is_conditional_on_tracing(_tracing_of(body))
        for body in _functions(template).values()
    )
    conditions = template.get("Conditions") or {}
    if TRACING_CONDITION not in conditions and not uses_it:
        return

    rel = path.relative_to(REPO_ROOT)
    assert TRACING_CONDITION in conditions, (
        f"{rel} makes tracing conditional on {TRACING_CONDITION} but never "
        f"defines that condition, so the stack fails to deploy"
    )
    assert _condition_tests_the_parameter(conditions[TRACING_CONDITION]), (
        f"{rel}: {TRACING_CONDITION} does not test !Ref {TRACING_PARAMETER}, so "
        f"the parameter named in its description is not what decides tracing"
    )
    assert TRACING_PARAMETER in (template.get("Parameters") or {}), (
        f"{rel}: {TRACING_CONDITION} references {TRACING_PARAMETER}, which the "
        f"template does not declare"
    )


@pytest.mark.unit
@pytest.mark.parametrize("path", ALL_TEMPLATES, ids=TEMPLATE_IDS)
def test_instrumented_functions_declare_a_tracing_mode(path: Path) -> None:
    offenders = _instrumented_without_tracing(_load(path), path)
    assert not offenders, (
        f"{path.relative_to(REPO_ROOT)}: these functions import "
        f"{XRAY_SDK_MARKER} but declare no tracing mode, so they run in "
        f"PassThrough and their annotations and subsegments land only when an "
        f"upstream caller already sampled the request (issue #983): {offenders}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("path", ALL_TEMPLATES, ids=TEMPLATE_IDS)
def test_every_traced_function_can_write_a_segment(path: Path) -> None:
    findings = _traced_without_a_grant(_load(path))
    assert not findings, (
        f"{path.relative_to(REPO_ROOT)}: these functions declare a tracing mode "
        f"but their role cannot write a trace segment, so tracing is declared and "
        f"does nothing — the #983 defect one layer down: {findings}. SAM attaches "
        f"AWSXrayWriteOnlyAccess only to a role it GENERATES; a function with an "
        f"explicit `Role:` has to carry the grant itself."
    )


# --------------------------------------------------------------------------
# Rule 2's second half: the parent must pass the parameter to THIS nested stack.
# --------------------------------------------------------------------------

#: ``TemplateURL: ./<dir>/.aws-sam/packaged.yaml`` -> ``<dir>/template.yaml``.
#: Same mapping ``scripts/tests/test_nested_stack_parameters.py`` uses, for the
#: same reason: the URL points at a build artifact, so the only way to reason
#: about the nested stack's parameters offline is to resolve it back to source.
_PACKAGED_URL = re.compile(r"^\./(.+?)/\.aws-sam/packaged\.ya?ml$")


def _source_template_for(template_url: Any) -> str | None:
    if not isinstance(template_url, str):
        return None
    match = _PACKAGED_URL.match(template_url.strip())
    if not match:
        return None
    for candidate in (
        f"{match.group(1)}/template.yaml",
        f"{match.group(1)}/template.yml",
    ):
        if (REPO_ROOT / candidate).is_file():
            return candidate
    return None


def _nested_stacks() -> list[tuple[str, str]]:
    """(logical id, source template path) for each nested stack of the parent."""
    resources = _load(REPO_ROOT / "template.yaml").get("Resources") or {}
    found = []
    for name, body in resources.items():
        if not isinstance(body, dict):
            continue
        if body.get("Type") != "AWS::CloudFormation::Stack":
            continue
        source = _source_template_for((body.get("Properties") or {}).get("TemplateURL"))
        if source:
            found.append((name, source))
    return sorted(found)


NESTED_STACKS = _nested_stacks()


@pytest.mark.unit
def test_nested_stacks_are_discovered() -> None:
    """A silent zero here would make the per-stack rule below vacuous."""
    assert len(NESTED_STACKS) >= 5, (
        f"expected at least 5 nested stacks in template.yaml, found "
        f"{len(NESTED_STACKS)}: {NESTED_STACKS}. If TemplateURL no longer points "
        f"at '<dir>/.aws-sam/packaged.yaml', update _source_template_for()."
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "logical_id,source", NESTED_STACKS, ids=[f"{n}->{s}" for n, s in NESTED_STACKS]
)
def test_a_nested_template_declaring_the_parameter_is_passed_it(
    logical_id: str, source: str
) -> None:
    """Checked per stack, not "to at least one stack".

    A nested template can declare the parameter, define the condition and write
    the ``!If`` correctly and STILL trace unconditionally, if the parent forgets
    ``EnableXRayTracing: !Ref EnableXRayTracing`` in that stack's ``Parameters``:
    the nested parameter then sits at its own default of ``true`` and a deploy
    with ``false`` leaves those functions tracing. That is #983 reintroduced
    through the one route this rule exists to close, so the check has to name the
    stack rather than be satisfied by a sibling.
    """
    nested = _load(REPO_ROOT / source)
    if not _declares_the_parameter(nested):
        pytest.skip(f"{source} does not declare {TRACING_PARAMETER}")

    parent = _load(REPO_ROOT / "template.yaml")
    body = (parent.get("Resources") or {})[logical_id]
    passed = (body.get("Properties") or {}).get("Parameters") or {}
    assert TRACING_PARAMETER in passed, (
        f"{source} declares {TRACING_PARAMETER} but template.yaml does not pass "
        f"it to {logical_id}, so the nested parameter sits at its own default "
        f"and the parent's setting never reaches those functions"
    )


@pytest.mark.unit
def test_exempt_templates_are_not_nested_stacks_of_the_parent() -> None:
    """The exemption's stated reason, checked instead of believed.

    ``HARDCODED_WITHOUT_PARAMETER`` excuses a template from rule 1 on exactly one
    ground: the stack is installed from the Extensions catalog on its own, so the
    main stack's parameter has no route to it. That is a **structural** claim about
    ``template.yaml``, and until this test existed it was only a comment — so the
    list carried ``feature-platform/main-stack-extensions/template.yaml``, which
    ``template.yaml`` deploys as ``FeaturePlatformStack`` and already hands 27
    parameters to, including ``LogLevel`` and ``LogRetentionDays``. Nine Lambdas
    inside the main deployment therefore kept tracing with the parameter set to
    ``false``, and every rule in this file skipped them, because the one list that
    mentioned the template said not to look.

    A wrong entry here is worse than a missing one: a missing entry fails
    ``test_templates_without_the_parameter_are_the_known_set`` loudly, while a wrong
    one turns the whole gate off for that template and reads as a decision.
    """
    nested_sources = {source for _, source in NESTED_STACKS}
    reachable = sorted(HARDCODED_WITHOUT_PARAMETER & nested_sources)
    assert not reachable, (
        f"these templates are exempt from rule 1 on the ground that they are "
        f"deployed independently and cannot receive {TRACING_PARAMETER}, but "
        f"template.yaml deploys them as nested stacks and so can pass it: "
        f"{reachable}. Give each one the parameter, the condition and a conditional "
        f"tracing mode, pass `{TRACING_PARAMETER}: !Ref {TRACING_PARAMETER}` from "
        f"the parent, and delete the entry. A nested stack has no route to a "
        f"per-extension parameter an operator could set, so the exemption's reason "
        f"cannot be true for it."
    )


@pytest.mark.unit
def test_at_least_one_nested_template_declares_the_parameter() -> None:
    """Otherwise every case of the rule above skips and it proves nothing."""
    declaring = [
        source
        for _, source in NESTED_STACKS
        if _declares_the_parameter(_load(REPO_ROOT / source))
    ]
    assert declaring, (
        f"no nested template declares {TRACING_PARAMETER} any more, so "
        f"test_a_nested_template_declaring_the_parameter_is_passed_it skips every "
        f"case — delete it or point it at the template that does"
    )


@pytest.mark.unit
def test_the_rules_are_not_vacuous() -> None:
    """Floors on what the rules above actually examined."""
    traced = 0
    xray_dirs: set[Path] = set()
    for path in ALL_TEMPLATES:
        template = _load(path)
        for body in _functions(template).values():
            if _is_conditional_on_tracing(_tracing_of(body)):
                traced += 1
            if _uses_xray(body, path):
                directory = _source_for(body, path)
                if directory is not None:
                    xray_dirs.add(directory)

    assert traced >= MIN_TRACED_FUNCTIONS, (
        f"only {traced} functions declare a conditional tracing mode, below the "
        f"floor of {MIN_TRACED_FUNCTIONS}. Either tracing was removed from "
        f"several functions, or the shape this gate recognises changed and rule "
        f"1 is now looking at nothing."
    )
    assert len(xray_dirs) >= MIN_XRAY_SOURCE_DIRS, (
        f"only {len(xray_dirs)} source directories import {XRAY_SDK_MARKER}, "
        f"below the floor of {MIN_XRAY_SOURCE_DIRS}. The likely cause is that "
        f"CodeUri/ImageUri resolution broke, which would silently make rule 3 "
        f"pass for every function."
    )


class TestGateCatchesReintroduction:
    """Introduce each defect and confirm the corresponding rule fails.

    A gate whose rules have never been observed failing is indistinguishable
    from a gate that examines nothing.
    """

    BASE = """
AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31
Parameters:
  EnableXRayTracing:
    Type: String
    Default: 'true'
    AllowedValues: ['true', 'false']
Conditions:
  EnableXRayTracingCondition: !Equals [!Ref EnableXRayTracing, 'true']
Resources:
  Good:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: nowhere/
      Tracing: !If [EnableXRayTracingCondition, Active, PassThrough]
"""

    def test_the_clean_fixture_passes_rule_1(self) -> None:
        assert _hardcoded_tracing(_load_text(self.BASE)) == {}

    @pytest.mark.parametrize(
        "mode", ["Active", "PassThrough", "Disabled"], ids=lambda m: f"literal-{m}"
    )
    def test_a_literal_mode_is_caught(self, mode: str) -> None:
        text = self.BASE + (
            f"  Regressed:\n"
            f"    Type: AWS::Serverless::Function\n"
            f"    Properties:\n"
            f"      CodeUri: nowhere/\n"
            f"      Tracing: {mode}\n"
        )
        assert "Regressed" in _hardcoded_tracing(_load_text(text))

    @pytest.mark.parametrize(
        "branches",
        ["PassThrough, Active", "Disabled, Disabled", "PassThrough, PassThrough"],
        ids=["inverted", "both-disabled", "both-off"],
    )
    def test_the_branch_values_are_checked_not_just_the_condition(
        self, branches: str
    ) -> None:
        """Wiring to the right condition is not the same as wiring it the right
        way round: an inverted ``!If`` traces exactly when the operator asked for
        tracing off, and nothing else in the template would show it."""
        text = self.BASE + (
            f"  Regressed:\n"
            f"    Type: AWS::Serverless::Function\n"
            f"    Properties:\n"
            f"      CodeUri: nowhere/\n"
            f"      Tracing: !If [EnableXRayTracingCondition, {branches}]\n"
        )
        assert "Regressed" in _hardcoded_tracing(_load_text(text))

    def test_globals_is_not_a_loophole(self) -> None:
        """One line under ``Globals`` sets the mode for every function at once."""
        text = self.BASE.replace(
            "Resources:",
            "Globals:\n  Function:\n    Tracing: Active\nResources:",
        )
        assert "Globals.Function" in _hardcoded_tracing(_load_text(text))

    def test_a_conditional_globals_default_is_accepted(self) -> None:
        text = self.BASE.replace(
            "Resources:",
            "Globals:\n  Function:\n"
            "    Tracing: !If [EnableXRayTracingCondition, Active, PassThrough]\n"
            "Resources:",
        )
        assert _hardcoded_tracing(_load_text(text)) == {}

    def test_the_raw_lambda_type_is_not_a_loophole(self) -> None:
        text = self.BASE + (
            "  Regressed:\n"
            "    Type: AWS::Lambda::Function\n"
            "    Properties:\n"
            "      TracingConfig:\n"
            "        Mode: Active\n"
        )
        assert "Regressed" in _hardcoded_tracing(_load_text(text))

    def test_an_intrinsic_whole_tracing_config_is_not_a_loophole(self) -> None:
        """``TracingConfig`` itself replaced by an intrinsic, so there is no
        ``Mode`` key to read. Reading that as "no tracing declared" would let the
        raw resource type back in through the one shape rule 1 does not parse."""
        text = self.BASE + (
            "  Regressed:\n"
            "    Type: AWS::Lambda::Function\n"
            "    Properties:\n"
            "      TracingConfig: !If [SomeOtherCondition, {Mode: Active}, "
            "!Ref 'AWS::NoValue']\n"
        )
        assert "Regressed" in _hardcoded_tracing(_load_text(text))

    def test_a_condition_other_than_the_tracing_one_is_caught(self) -> None:
        """``Fn::If`` alone is not enough — it has to be keyed on this parameter."""
        text = self.BASE + (
            "  Regressed:\n"
            "    Type: AWS::Serverless::Function\n"
            "    Properties:\n"
            "      CodeUri: nowhere/\n"
            "      Tracing: !If [SomeOtherCondition, Active, PassThrough]\n"
        )
        assert "Regressed" in _hardcoded_tracing(_load_text(text))

    def test_instrumentation_without_tracing_is_caught(self, tmp_path: Path) -> None:
        source = tmp_path / "handler_dir"
        source.mkdir()
        (source / "index.py").write_text(
            "from aws_xray_sdk.core import xray_recorder\n", encoding="utf-8"
        )
        template_path = tmp_path / "template.yaml"
        text = (
            self.BASE
            + "  Regressed:\n"
            + "    Type: AWS::Serverless::Function\n"
            + "    Properties:\n"
            + "      CodeUri: handler_dir/\n"
        )
        template_path.write_text(text, encoding="utf-8")
        offenders = _instrumented_without_tracing(_load_text(text), template_path)
        assert offenders == ["Regressed"]

    def test_a_test_file_alone_does_not_trip_rule_3(self, tmp_path: Path) -> None:
        """Only deployed code counts; a stub in a sibling test file does not."""
        source = tmp_path / "handler_dir"
        source.mkdir()
        (source / "index.py").write_text("print('no tracing here')\n", encoding="utf-8")
        (source / "test_index.py").write_text(
            "import aws_xray_sdk  # stubbed out in tests\n", encoding="utf-8"
        )
        template_path = tmp_path / "template.yaml"
        text = (
            self.BASE
            + "  Untraced:\n"
            + "    Type: AWS::Serverless::Function\n"
            + "    Properties:\n"
            + "      CodeUri: handler_dir/\n"
        )
        template_path.write_text(text, encoding="utf-8")
        assert _instrumented_without_tracing(_load_text(text), template_path) == []

    ROLE_FIXTURE = """
  Role:
    Type: AWS::IAM::Role
    Properties:
      ManagedPolicyArns:
        - arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  Traced:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: nowhere/
      Tracing: !If [EnableXRayTracingCondition, Active, PassThrough]
      Role: !GetAtt Role.Arn
"""

    def test_an_explicit_role_without_the_grant_is_caught(self) -> None:
        """The state PipelineHooksDispatcherFunction was in: a declared mode and
        a role SAM never touches, so no segment is ever written."""
        findings = _traced_without_a_grant(_load_text(self.BASE + self.ROLE_FIXTURE))
        assert "Traced" in findings

    @pytest.mark.parametrize(
        "grant",
        [
            "        - arn:aws:iam::aws:policy/AWSXrayWriteOnlyAccess\n",
            "        - !Sub 'arn:${AWS::Partition}:iam::aws:policy/AWSXrayWriteOnlyAccess'\n",
        ],
        ids=["literal-arn", "partition-substituted-arn"],
    )
    def test_the_managed_policy_satisfies_rule_4(self, grant: str) -> None:
        text = (self.BASE + self.ROLE_FIXTURE).replace(
            "        - arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole\n",
            "        - arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole\n"
            + grant,
        )
        assert _traced_without_a_grant(_load_text(text)) == {}

    def test_an_inline_statement_satisfies_rule_4(self) -> None:
        text = (self.BASE + self.ROLE_FIXTURE).replace(
            "  Traced:\n",
            "      Policies:\n"
            "        - PolicyName: XRay\n"
            "          PolicyDocument:\n"
            "            Statement:\n"
            "              - Effect: Allow\n"
            "                Action:\n"
            "                  - xray:PutTraceSegments\n"
            "                  - xray:PutTelemetryRecords\n"
            "                Resource: '*'\n"
            "  Traced:\n",
        )
        assert _traced_without_a_grant(_load_text(text)) == {}

    def test_a_deny_statement_does_not_satisfy_rule_4(self) -> None:
        text = (self.BASE + self.ROLE_FIXTURE).replace(
            "  Traced:\n",
            "      Policies:\n"
            "        - PolicyName: XRay\n"
            "          PolicyDocument:\n"
            "            Statement:\n"
            "              - Effect: Deny\n"
            "                Action: xray:PutTraceSegments\n"
            "                Resource: '*'\n"
            "  Traced:\n",
        )
        assert "Traced" in _traced_without_a_grant(_load_text(text))

    def test_a_generated_role_is_not_reported(self) -> None:
        """No ``Role:`` means SAM generates one and attaches the policy."""
        assert _traced_without_a_grant(_load_text(self.BASE)) == {}

    def test_an_unresolvable_role_is_reported_rather_than_skipped(self) -> None:
        text = self.BASE + (
            "  Traced:\n"
            "    Type: AWS::Serverless::Function\n"
            "    Properties:\n"
            "      CodeUri: nowhere/\n"
            "      Tracing: !If [EnableXRayTracingCondition, Active, PassThrough]\n"
            "      Role: arn:aws:iam::123456789012:role/SomeImportedRole\n"
        )
        assert "Traced" in _traced_without_a_grant(_load_text(text))

    def test_an_untraced_function_with_a_bare_role_is_not_reported(self) -> None:
        """Rule 4 is about tracing, not about roles."""
        text = self.BASE + (
            "  Untraced:\n"
            "    Type: AWS::Serverless::Function\n"
            "    Properties:\n"
            "      CodeUri: nowhere/\n"
            "      Role: arn:aws:iam::123456789012:role/SomeImportedRole\n"
        )
        assert _traced_without_a_grant(_load_text(text)) == {}
