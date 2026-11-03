# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every Lambda that applies a tenant-isolation rule is told which mode it is in.

``TenantIsolationMode=Strict`` reaches code only as the ``TENANT_ISOLATION_MODE``
environment variable, and ``idp_common.config_scope.is_strict_isolation`` reads
anything other than ``Strict`` as off. So a consumer the template forgets to wire
does not fail: it silently stays in Off mode — an unscoped user sees everything,
ingest accepts a cross-tenant document — on a stack whose operator selected Strict.
That is the failure this file exists to make loud.

The set of consumers is **derived**, not listed, from two independent signals:

* a function whose environment carries ``USERS_TABLE_NAME`` resolves a caller's
  configuration scope, and strict mode changes what that resolution returns;
* a function whose code directory references the scope or tenant modules
  (``config_scope``, ``resolve_allowed_config_versions``, ``config.tenants``) or
  the mode itself (``TENANT_ISOLATION_MODE``, ``is_strict_isolation``).

A function either signal names must have ``TENANT_ISOLATION_MODE`` wired from the
stack parameter (``!Ref TenantIsolationMode``, or the host stack's export for a
Feature Platform stack). Each template that references the parameter must
declare it, and the parent must pass it to every nested stack that declares it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from repo_files import tracked_paths

REPO_ROOT = Path(__file__).resolve().parents[2]
FUNCTION_TYPES = {"AWS::Serverless::Function", "AWS::Lambda::Function"}
ENV = "TENANT_ISOLATION_MODE"
PARAM = "TenantIsolationMode"
PARAMETER_ENV = "TENANT_ISOLATION_MODE_PARAMETER"
# The SSM parameter the Feature Platform host publishes the mode in.
HOST_PARAMETER_NAME = "/${MainStackName}/tenant-isolation-mode"
CODE_SIGNALS = re.compile(
    r"config_scope|resolve_allowed_config_versions|config\.tenants|"
    r"TENANT_ISOLATION_MODE|is_strict_isolation"
)


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags."""


def _tag_to_python(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{tag_suffix}": loader.construct_sequence(node, deep=True)}
    return {f"Fn::{tag_suffix}": loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag_to_python)


def _templates() -> list[Path]:
    found = []
    # Tracked files only, so build output (`.aws-sam/`) is never read.
    for path in tracked_paths(REPO_ROOT, "*.yaml", "*.yml"):
        text = path.read_text(errors="ignore")
        if "AWSTemplateFormatVersion" in text and any(
            t in text for t in FUNCTION_TYPES
        ):
            found.append(path)
    return found


def _load(path: Path) -> dict:
    return yaml.load(path.read_text(), Loader=_CfnLoader) or {}


def _env(body: dict) -> dict:
    variables = ((body.get("Properties") or {}).get("Environment") or {}).get(
        "Variables"
    )
    return variables if isinstance(variables, dict) else {}


def _code_dir(template: Path, body: dict) -> Path | None:
    code = (body.get("Properties") or {}).get("CodeUri")
    if not isinstance(code, str) or code.startswith("s3://"):
        return None
    path = (template.parent / code).resolve()
    return path if path.is_dir() else None


_TRACKED_PY = tracked_paths(REPO_ROOT, "*.py")


def _tracked_python(directory: Path) -> list[Path]:
    """The tracked ``.py`` files under ``directory``.

    Tracked only, not ``rglob``: a function's code directory can hold a staged,
    gitignored copy of ``idp_common`` from a local package build (the data
    generator's does), and reading it would make this gate's verdict depend on
    what was last built on the machine running it rather than on the tree.
    """
    root = directory.resolve()
    return [p for p in _TRACKED_PY if p == root or root in p.parents]


def _code_references_isolation(directory: Path) -> bool:
    for py in _tracked_python(directory):
        # A test file mentioning the module is not a consumer of it.
        if "tests" in py.relative_to(directory).parts or py.name.startswith(
            ("test_", "conftest")
        ):
            continue
        if CODE_SIGNALS.search(py.read_text(errors="ignore")):
            return True
    return False


def _is_wired(env: dict) -> bool:
    """Whether a function's environment tells it the stack's isolation mode.

    Two shapes count. A function in a stack that receives the parameter sets
    ``TENANT_ISOLATION_MODE: !Ref TenantIsolationMode``. A Feature Platform stack
    cannot: it would have to import the mode as a stack export, and CloudFormation
    refuses to change an export another stack imports, so the host's mode could
    never be switched. Such a function instead names the SSM parameter the host
    publishes, in ``TENANT_ISOLATION_MODE_PARAMETER``, and reads it at run time.
    """
    if env.get(ENV) == {"Fn::Ref": PARAM}:
        return True
    named = env.get(PARAMETER_ENV)
    sub = named.get("Fn::Sub") if isinstance(named, dict) else None
    return isinstance(sub, str) and sub == HOST_PARAMETER_NAME


def _consumers():
    """``(template, function, reason)`` for every function that must be wired."""
    out = []
    for template in _templates():
        resources = _load(template).get("Resources") or {}
        for name, body in resources.items():
            if not isinstance(body, dict) or body.get("Type") not in FUNCTION_TYPES:
                continue
            reasons = []
            if "USERS_TABLE_NAME" in _env(body):
                reasons.append("has USERS_TABLE_NAME")
            directory = _code_dir(template, body)
            if directory is not None and _code_references_isolation(directory):
                reasons.append(f"code in {directory.relative_to(REPO_ROOT)}")
            if reasons:
                out.append((template, name, body, "; ".join(reasons)))
    return out


CONSUMERS = _consumers()


def test_the_consumer_set_is_not_vacuous():
    """The derivation must find the functions this change is about; an empty or
    tiny set would mean the signals stopped matching, not that nothing needs it."""
    names = {name for _t, name, _b, _r in CONSUMERS}
    for expected in (
        "QueueSender",
        "UploadResolverFunction",
        "ChatWithDocumentProcessorFunction",
        "ChatStreamProcessorFunction",
        "ListDocumentsGSIResolverFunction",
        "AgentCoreMCPHandlerFunction",
    ):
        assert expected in names, f"{expected} is no longer detected as a consumer"


@pytest.mark.parametrize(
    "template,name,body,reason",
    CONSUMERS,
    ids=[f"{t.relative_to(REPO_ROOT)}:{n}" for t, n, _b, _r in CONSUMERS],
)
def test_every_consumer_is_wired(template, name, body, reason):
    assert _is_wired(_env(body)), (
        f"{template.relative_to(REPO_ROOT)}: {name} ({reason}) must set "
        f"{ENV}: !Ref {PARAM} (or, in a Feature Platform stack, {PARAMETER_ENV}: "
        f"!Sub '{HOST_PARAMETER_NAME}'). Without it the function silently runs in "
        f"Off mode "
        f"on a stack deployed with {PARAM}=Strict."
    )


@pytest.mark.parametrize(
    "template", _templates(), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_a_template_that_refs_the_parameter_declares_it(template):
    doc = _load(template)
    text = template.read_text()
    if f"!Ref {PARAM}" not in text:
        return
    param = (doc.get("Parameters") or {}).get(PARAM)
    assert param, (
        f"{template.relative_to(REPO_ROOT)} uses !Ref {PARAM} but does not declare it"
    )
    assert param.get("Default") == "Off"
    assert sorted(param.get("AllowedValues") or []) == ["Off", "Strict"]


def test_the_parent_passes_the_mode_to_every_nested_stack_that_declares_it():
    parent = REPO_ROOT / "template.yaml"
    resources = _load(parent).get("Resources") or {}
    checked = 0
    for name, body in resources.items():
        if (
            not isinstance(body, dict)
            or body.get("Type") != "AWS::CloudFormation::Stack"
        ):
            continue
        url = (body.get("Properties") or {}).get("TemplateURL")
        if not isinstance(url, str) or "/.aws-sam/" not in url:
            continue
        child = (parent.parent / url.split("/.aws-sam/")[0] / "template.yaml").resolve()
        if not child.is_file():
            continue
        if PARAM not in ((_load(child).get("Parameters")) or {}):
            continue
        checked += 1
        passed = ((body.get("Properties") or {}).get("Parameters") or {}).get(PARAM)
        assert passed == {"Fn::Ref": PARAM}, (
            f"template.yaml: nested stack {name} declares {PARAM} but is not passed "
            f"it, so every function in it runs in Off mode whatever the parent says"
        )
    assert checked >= 2, "expected the api-resolvers and Feature Platform stacks"


def test_the_host_publishes_the_mode_as_a_parameter_not_an_export():
    """The mode reaches feature stacks through an SSM parameter whose value follows
    the main stack. An export of it would be imported, and CloudFormation refuses
    to change the value of an imported export, so TenantIsolationMode could never
    be switched once a feature was installed."""
    host = _load(REPO_ROOT / "feature-platform/main-stack-extensions/template.yaml")
    resources = host.get("Resources") or {}
    params = [
        body
        for body in resources.values()
        if isinstance(body, dict)
        and body.get("Type") == "AWS::SSM::Parameter"
        and (body.get("Properties") or {}).get("Name")
        == {"Fn::Sub": HOST_PARAMETER_NAME}
    ]
    assert len(params) == 1, "the host must publish exactly one mode parameter"
    assert params[0]["Properties"]["Value"] == {"Fn::Ref": PARAM}
    assert "Condition" not in params[0], (
        "the parameter must exist on every host stack a feature may read it from"
    )
    for name, output in (host.get("Outputs") or {}).items():
        assert (output or {}).get("Value") != {"Fn::Ref": PARAM}, (
            f"output {name} exports the mode; an imported export can never change"
        )


def test_a_feature_reading_the_parameter_may_read_exactly_it():
    """A function wired through the parameter needs ``ssm:GetParameter`` on that one
    parameter, or it fails closed (reads Strict) on every request."""
    checked = 0
    for template, name, body, _reason in CONSUMERS:
        if PARAMETER_ENV not in _env(body):
            continue
        checked += 1
        text = yaml.safe_dump((body.get("Properties") or {}).get("Policies"))
        assert (
            "ssm:GetParameter" in text
            and ":parameter/${MainStackName}/tenant-isolation-mode" in text
        ), (
            f"{template.relative_to(REPO_ROOT)}: {name} reads the mode parameter "
            f"but is not granted ssm:GetParameter on it"
        )
    assert checked >= 1, "expected the PII anonymizer's feature API"
