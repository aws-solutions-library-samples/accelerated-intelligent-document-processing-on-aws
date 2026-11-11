# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Address a Terraform deployment through the CloudFormation APIs.

A Terraform deployment of the accelerator creates a CloudFormation *label
stack* named like the deployment. It manages no resources, its description
contains ``(Terraform)``, it carries the deployment's outputs, and its
``TerraformResourceDirectory`` output names an S3 object holding a
CloudFormation-shaped directory of every resource: logical id, type and
physical id, per module, with nested modules standing in for nested stacks.

This module attaches a hook to every CloudFormation client botocore creates.
For a label stack, and for the nested stacks its directory describes, the hook
answers ``ListStackResources``, ``DescribeStackResource``,
``DescribeStackResources`` and the nested stacks' ``DescribeStacks`` from the
directory. Every other call, and every call about a CloudFormation-deployed
stack, goes to CloudFormation unchanged. So code that resolves resources by
``--stack-name`` works against either kind of deployment without change.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

import botocore.session

logger = logging.getLogger(__name__)

TERRAFORM_SUFFIX = "(Terraform)"
# Set by the generated Terraform module on the functions that walk the stack
# tree at run time, which are the ones the translator lists in
# TERRAFORM_DIRECTORY_READERS (terraform/translator/idp_tf/translate.py; the
# data-mart rollup): the S3 URL of this deployment's resource directory. Its presence is what says the
# deployment is a Terraform one; a CloudFormation deployment never has it.
DIRECTORY_ENV = "IDP_TF_RESOURCE_DIRECTORY"
DIRECTORY_OUTPUT = "TerraformResourceDirectory"
SERVED = {
    "ListStackResources",
    "DescribeStackResource",
    "DescribeStackResources",
    "DescribeStacks",
}

_lock = threading.Lock()
# (region, root stack name) -> directory, or None when the stack is not a label
_roots: dict[tuple[str, str], dict[str, Any] | None] = {}
# (region, synthetic nested stack name) -> nested directory node
_nested: dict[tuple[str, str], dict[str, Any]] = {}
_installed = False
# botocore's own Session.create_client, kept so uninstall() can restore it.
_original_create_client: Any = None
# Set while this module creates its own clients, which must not be hooked.
_tls = threading.local()


class _Http:
    status_code = 200
    headers: dict[str, str] = {}


#: Explicit credentials a caller may give ``create_client``; the hook's own
#: lookups reuse them, so a client built for one principal reads the directory
#: as that principal rather than as the process's default credentials.
_CREDENTIAL_ARGS = ("aws_access_key_id", "aws_secret_access_key", "aws_session_token")

#: ``(service, region) -> client``, building unhooked clients.
ClientFactory = Callable[[str, str], Any]


def _factory_for(session: Any, kwargs: dict[str, Any]) -> ClientFactory:
    """Clients from the caller's own session and explicit credentials."""
    creds = {k: kwargs[k] for k in _CREDENTIAL_ARGS if kwargs.get(k)}

    def make(service: str, region: str) -> Any:
        _tls.raw = True
        try:
            return session.create_client(service, region_name=region, **creds)
        finally:
            _tls.raw = False

    return make


def _raw_clients(region: str, factory: ClientFactory | None = None) -> tuple[Any, Any]:
    if factory is None:
        factory = _factory_for(botocore.session.Session(), {})
    return factory("cloudformation", region), factory("s3", region)


def is_terraform_stack(description: str | None) -> bool:
    """The label stack's description carries the template's description, then
    ``(Terraform)``, then a read-only notice."""
    return bool(description) and TERRAFORM_SUFFIX in str(description)


def is_terraform_label(stack: dict[str, Any]) -> bool:
    """A ``DescribeStacks`` entry is a Terraform deployment's label stack.

    Both marks are required: the ``(Terraform)`` description, which a person
    could also write into an ordinary stack's description, and the resource
    directory output, which only the label stack carries.
    """
    outputs = {o.get("OutputKey") for o in stack.get("Outputs") or []}
    return is_terraform_stack(stack.get("Description")) and DIRECTORY_OUTPUT in outputs


def _register_nested(region: str, node: dict[str, Any]) -> None:
    for entry in node.get("resources", []):
        if entry.get("ResourceType") != "AWS::CloudFormation::Stack":
            continue
        child = (node.get("nested") or {}).get(entry["LogicalResourceId"])
        if not child:
            continue
        arn = entry["PhysicalResourceId"]
        name = arn.split("/")[1]
        child = dict(child, _stack_id=arn, _stack_name=name)
        _nested[(region, arn)] = child
        _nested[(region, name)] = child
        _register_nested(region, child)


# A warm Lambda container would otherwise serve the directory it first read
# for its whole life, across applies that change the deployment.
CACHE_SECONDS = 300
_loaded_at: dict[tuple[str, str], float] = {}


def _load_root(
    region: str, stack_name: str, factory: ClientFactory | None = None
) -> dict[str, Any] | None:
    key = (region, stack_name)
    with _lock:
        loaded = _loaded_at.get(key)
        fresh = loaded is None or time.monotonic() - loaded < CACHE_SECONDS
        if key in _roots and fresh:
            return _roots[key]
    cfn, s3 = _raw_clients(region, factory)
    env_url = os.environ.get(DIRECTORY_ENV)
    if env_url and stack_name == os.environ.get("STACK_NAME"):
        # Inside the deployment: the module told this function where its
        # directory is, so no DescribeStacks (and no permission for it) is needed.
        bucket, _, obj = env_url[len("s3://") :].partition("/")
        root = json.loads(s3.get_object(Bucket=bucket, Key=obj)["Body"].read())["root"]
        directory = dict(
            root, _stack_id=f"terraform:{stack_name}", _stack_name=stack_name
        )
        with _lock:
            _roots[key] = directory
            _loaded_at[key] = time.monotonic()
            _register_nested(region, directory)
        return directory
    try:
        stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    except Exception:  # noqa: BLE001 - not a stack we can read: not ours to serve
        directory = None
    else:
        outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
        url = outputs.get(DIRECTORY_OUTPUT)
        if not (is_terraform_label(stack) and url):
            directory = None
        else:
            bucket, _, obj = url[len("s3://") :].partition("/")
            body = s3.get_object(Bucket=bucket, Key=obj)["Body"].read()
            root = json.loads(body)["root"]
            directory = dict(root, _stack_id=stack["StackId"], _stack_name=stack_name)
            logger.debug("Serving %s from its Terraform resource directory", stack_name)
    with _lock:
        _roots[key] = directory
        _loaded_at[key] = time.monotonic()
        if directory is not None:
            _register_nested(region, directory)
    return directory


def _node_for(
    region: str, stack: str, factory: ClientFactory | None = None
) -> tuple[dict[str, Any] | None, bool]:
    """(directory node, is_root) for a stack name or id, or (None, False)."""
    if (region, stack) in _nested:
        return _nested[(region, stack)], False
    if stack.startswith("arn:") and stack.endswith("/terraform"):
        # A synthetic nested stack id: <root>-<LogicalId>. Load the root it
        # names so its nested stacks are registered.
        name = stack.split("/")[1]
        parts = name.split("-")
        for i in range(len(parts) - 1, 0, -1):
            _load_root(region, "-".join(parts[:i]), factory)
            if (region, stack) in _nested:
                return _nested[(region, stack)], False
        return None, False
    root = _load_root(region, stack, factory)
    return root, root is not None


def _summary(node: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "LogicalResourceId": entry["LogicalResourceId"],
        "PhysicalResourceId": entry["PhysicalResourceId"],
        "ResourceType": entry["ResourceType"],
        "ResourceStatus": "CREATE_COMPLETE",
        "LastUpdatedTimestamp": datetime.datetime.now(datetime.timezone.utc),
        "StackName": node["_stack_name"],
        "StackId": node["_stack_id"],
    }


def _serve(
    op: str,
    params: dict[str, Any],
    region: str,
    factory: ClientFactory | None = None,
) -> dict[str, Any] | None:
    stack = params.get("StackName")
    if not stack:
        return None
    stack = str(stack)
    if op == "DescribeStacks":
        # Only a synthetic nested stack is answered here; the label stack and
        # every CloudFormation stack are real, so do not look the root up.
        known = (region, stack) in _nested
        if not (known or (stack.startswith("arn:") and stack.endswith("/terraform"))):
            return None
    node, is_root = _node_for(region, stack, factory)
    if node is None:
        return None
    if op == "DescribeStacks":
        if is_root:
            return None  # the label stack itself is real
        return {
            "Stacks": [
                {
                    "StackName": node["_stack_name"],
                    "StackId": node["_stack_id"],
                    "StackStatus": "CREATE_COMPLETE",
                    "CreationTime": datetime.datetime.now(datetime.timezone.utc),
                    "Description": f"Terraform module {TERRAFORM_SUFFIX}",
                    "Outputs": [
                        {"OutputKey": k, "OutputValue": v}
                        for k, v in (node.get("outputs") or {}).items()
                        if v is not None
                    ],
                }
            ]
        }
    resources = node.get("resources", [])
    wanted = params.get("LogicalResourceId")
    if op == "ListStackResources":
        return {"StackResourceSummaries": [_summary(node, e) for e in resources]}
    if op == "DescribeStackResources":
        chosen = [
            e for e in resources if not wanted or e["LogicalResourceId"] == wanted
        ]
        return {"StackResources": [_summary(node, e) for e in chosen]}
    if op == "DescribeStackResource":
        for e in resources:
            if e["LogicalResourceId"] == wanted:
                return {"StackResourceDetail": _summary(node, e)}
        return None
    return None


def _before_parameter_build(params: dict[str, Any], context: dict[str, Any], **_):
    context["idp_tf_params"] = dict(params)


def _before_call(
    model: Any, context: dict[str, Any], factory: ClientFactory | None = None, **_
):
    if model.name not in SERVED:
        return None
    params = context.get("idp_tf_params") or {}
    region = context.get("client_region") or "us-east-1"
    try:
        parsed = _serve(model.name, params, region, factory)
    except Exception:  # noqa: BLE001 - never break a real CloudFormation call
        # Inside a Terraform deployment a failure here sends the call to
        # CloudFormation, which knows only the label stack and so answers with
        # an almost empty, confidently wrong result; say so loudly.
        log = logger.warning if os.environ.get(DIRECTORY_ENV) else logger.debug
        log(
            "Terraform directory lookup failed; falling back to CloudFormation",
            exc_info=True,
        )
        return None
    if parsed is None:
        return None
    parsed["ResponseMetadata"] = {"HTTPStatusCode": 200}
    return _Http(), parsed


def attach(client: Any, factory: ClientFactory | None = None) -> None:
    """Answer this client's label-stack calls from the resource directory.

    ``factory`` builds the clients the lookup itself uses; without one they
    come from a default session.
    """

    def before_call(model: Any, context: dict[str, Any], **kwargs: Any):
        return _before_call(model, context, factory=factory, **kwargs)

    events = client.meta.events
    events.register("before-parameter-build.cloudformation", _before_parameter_build)
    events.register("before-call.cloudformation", before_call)


def install() -> None:
    """Attach the hook to every CloudFormation client created from now on."""
    global _installed, _original_create_client
    if _installed:
        return
    original = botocore.session.Session.create_client
    _original_create_client = original

    def create_client(self, service_name, *args, **kwargs):
        client = original(self, service_name, *args, **kwargs)
        if service_name == "cloudformation" and not getattr(_tls, "raw", False):
            attach(client, _factory_for(self, kwargs))
        return client

    botocore.session.Session.create_client = create_client  # type: ignore[method-assign]
    _installed = True


def uninstall() -> None:
    """Restore botocore's ``create_client``. Clients already created keep the
    hook; only clients created afterwards are unhooked. For tests."""
    global _installed, _original_create_client
    if not _installed:
        return
    botocore.session.Session.create_client = _original_create_client  # type: ignore[method-assign]
    _original_create_client = None
    _installed = False


def install_if_terraform() -> None:
    """For run-time code inside a deployment: install the hook only when the
    deployment is a Terraform one (``IDP_TF_RESOURCE_DIRECTORY`` is set)."""
    if os.environ.get(DIRECTORY_ENV):
        install()


def reset_cache() -> None:
    with _lock:
        _roots.clear()
        _nested.clear()
        _loaded_at.clear()
