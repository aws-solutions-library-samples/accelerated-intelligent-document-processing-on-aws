# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Address a Terraform deployment through the CloudFormation APIs.

A Terraform deployment of the accelerator creates a CloudFormation *label
stack* named like the deployment. It manages no resources, its description ends
in ``(Terraform)``, it carries the deployment's outputs, and its
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
import threading
from typing import Any

import botocore.session

logger = logging.getLogger(__name__)

TERRAFORM_SUFFIX = "(Terraform)"
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
# Set while this module creates its own clients, which must not be hooked.
_tls = threading.local()


class _Http:
    status_code = 200
    headers: dict[str, str] = {}


def _raw_clients(region: str) -> tuple[Any, Any]:
    _tls.raw = True
    try:
        session = botocore.session.Session()
        cfn = session.create_client("cloudformation", region_name=region)
        s3 = session.create_client("s3", region_name=region)
    finally:
        _tls.raw = False
    return cfn, s3


def is_terraform_stack(description: str | None) -> bool:
    return bool(description) and str(description).rstrip().endswith(TERRAFORM_SUFFIX)


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


def _load_root(region: str, stack_name: str) -> dict[str, Any] | None:
    key = (region, stack_name)
    with _lock:
        if key in _roots:
            return _roots[key]
    cfn, s3 = _raw_clients(region)
    try:
        stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    except Exception:  # noqa: BLE001 - not a stack we can read: not ours to serve
        directory = None
    else:
        outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
        url = outputs.get(DIRECTORY_OUTPUT)
        if not (is_terraform_stack(stack.get("Description")) and url):
            directory = None
        else:
            bucket, _, obj = url[len("s3://") :].partition("/")
            body = s3.get_object(Bucket=bucket, Key=obj)["Body"].read()
            root = json.loads(body)["root"]
            directory = dict(root, _stack_id=stack["StackId"], _stack_name=stack_name)
            logger.debug("Serving %s from its Terraform resource directory", stack_name)
    with _lock:
        _roots[key] = directory
        if directory is not None:
            _register_nested(region, directory)
    return directory


def _node_for(region: str, stack: str) -> tuple[dict[str, Any] | None, bool]:
    """(directory node, is_root) for a stack name or id, or (None, False)."""
    if (region, stack) in _nested:
        return _nested[(region, stack)], False
    if stack.startswith("arn:") and stack.endswith("/terraform"):
        # A synthetic nested stack id: <root>-<LogicalId>. Load the root it
        # names so its nested stacks are registered.
        name = stack.split("/")[1]
        parts = name.split("-")
        for i in range(len(parts) - 1, 0, -1):
            _load_root(region, "-".join(parts[:i]))
            if (region, stack) in _nested:
                return _nested[(region, stack)], False
        return None, False
    root = _load_root(region, stack)
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


def _serve(op: str, params: dict[str, Any], region: str) -> dict[str, Any] | None:
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
    node, is_root = _node_for(region, stack)
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


def _before_call(model: Any, context: dict[str, Any], **_):
    if model.name not in SERVED:
        return None
    params = context.get("idp_tf_params") or {}
    region = context.get("client_region") or "us-east-1"
    try:
        parsed = _serve(model.name, params, region)
    except Exception:  # noqa: BLE001 - never break a real CloudFormation call
        logger.debug("Terraform directory lookup failed", exc_info=True)
        return None
    if parsed is None:
        return None
    parsed["ResponseMetadata"] = {"HTTPStatusCode": 200}
    return _Http(), parsed


def attach(client: Any) -> None:
    events = client.meta.events
    events.register("before-parameter-build.cloudformation", _before_parameter_build)
    events.register("before-call.cloudformation", _before_call)


def install() -> None:
    """Attach the hook to every CloudFormation client created from now on."""
    global _installed
    if _installed:
        return
    original = botocore.session.Session.create_client

    def create_client(self, service_name, *args, **kwargs):
        client = original(self, service_name, *args, **kwargs)
        if service_name == "cloudformation" and not getattr(_tls, "raw", False):
            attach(client)
        return client

    botocore.session.Session.create_client = create_client  # type: ignore[method-assign]
    _installed = True


def reset_cache() -> None:
    with _lock:
        _roots.clear()
        _nested.clear()
