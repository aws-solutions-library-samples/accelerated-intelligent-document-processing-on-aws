# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The VPC-endpoint S3 client is for presigning only — enforced statically.

Two different reasons exist to point an S3 client at an interface VPC endpoint,
and the templates already keep them apart with two different conditions:

* ``UseS3VpcEndpointForPresigner`` — the URL is signed and handed to the
  **browser**, which is inside the network. Signing is offline, so the Lambda
  needs no connectivity to the endpoint and does not have to be VPC-attached.
  (In the ``api-resolvers`` nested stack this arrives as the local
  ``HasS3EndpointUrl`` condition on the passed-in ``S3EndpointUrl`` parameter.)
* ``HasS3VpcEndpointAndVpc`` — the **Lambda itself** calls S3 and is deployed in
  the VPC, so it can actually route to the endpoint.

Using a presigner-configured client for real S3 calls silently mixes the two. It
does not raise: a VPCE hostname resolves to the endpoint's private addresses, so
the request hangs, and because REST API Gateway abandons an integration at 29s
the user gets ``504`` with an empty body and the resolver log says nothing. That
is exactly how ``POST /api/op/getTestSets`` failed in a private deployment while
the SPA around it worked.

This test derives the affected Lambdas from the templates (so a newly added one
is covered without editing this file) and checks the rule with the AST: any
module-level S3 client constructed with ``endpoint_url`` may only ever be the
receiver of a ``generate_presigned_*`` call.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest
import yaml

pytestmark = pytest.mark.unit


class _CFNLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short intrinsic tags."""


def _cfn_multi_constructor(loader, tag_suffix, node):
    tag = "!" + tag_suffix
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    return {tag: value}


_CFNLoader.add_multi_constructor("!", _cfn_multi_constructor)

# Conditions that mean "this endpoint exists for the BROWSER's benefit". A client
# built from one of these must not be used for the function's own S3 calls.
PRESIGNER_ONLY_CONDITIONS = {"HasS3EndpointUrl", "UseS3VpcEndpointForPresigner"}

TEMPLATES = (
    Path("template.yaml"),
    Path("nested/api-resolvers/template.yaml"),
)


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "template.yaml").is_file() and (parent / "publish.py").is_file():
            return parent
    raise RuntimeError("Could not locate repo root containing template.yaml")


def _conditions_in(node: Any, found: Set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "!If" and isinstance(value, list) and value:
                if isinstance(value[0], str):
                    found.add(value[0])
                for item in value[1:]:
                    _conditions_in(item, found)
            else:
                _conditions_in(value, found)
    elif isinstance(node, list):
        for item in node:
            _conditions_in(item, found)


def _presigner_lambda_dirs() -> List[Path]:
    """Directories of Lambdas whose S3_ENDPOINT_URL is presigner-only."""
    root = _repo_root()
    dirs: List[Path] = []

    for template_path in TEMPLATES:
        full = root / template_path
        with open(full, "r", encoding="utf-8") as f:
            # nosec B506 - _CFNLoader subclasses yaml.SafeLoader; the only
            # customization is a no-op constructor for CFN intrinsic tags. Input
            # is this repo's own committed template.
            template = yaml.load(f, Loader=_CFNLoader)  # nosec B506

        for resource in (template.get("Resources") or {}).values():
            if not isinstance(resource, dict):
                continue
            if resource.get("Type") != "AWS::Serverless::Function":
                continue
            props = resource.get("Properties") or {}
            env_vars = ((props.get("Environment") or {}).get("Variables")) or {}
            if "S3_ENDPOINT_URL" not in env_vars:
                continue

            conditions: Set[str] = set()
            _conditions_in(env_vars["S3_ENDPOINT_URL"], conditions)
            if not conditions & PRESIGNER_ONLY_CONDITIONS:
                # Gated on HasS3VpcEndpointAndVpc (or similar): the function is
                # in the VPC and is meant to call S3 through the endpoint.
                continue

            code_uri = props.get("CodeUri")
            if not isinstance(code_uri, str):
                continue
            candidate = (full.parent / code_uri).resolve()
            index = candidate / "index.py"
            if index.is_file():
                dirs.append(index)

    return sorted(set(dirs))


def _plain_s3_clients(tree: ast.Module) -> Dict[str, ast.Assign]:
    """Module-level ``X = boto3.client("s3", ...)`` with NO endpoint bound.

    The inverse of :func:`_endpoint_bound_clients`: the data-plane clients. A
    presigned URL generated on one of these points at the public S3 hostname,
    which a browser inside an air-gapped VPC cannot reach — the mirror image of
    the bug this module guards, and just as invisible from the resolver's log.
    """
    clients: Dict[str, ast.Assign] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not (isinstance(func, ast.Attribute) and func.attr == "client"):
            continue
        if not (
            call.args
            and isinstance(call.args[0], ast.Constant)
            and call.args[0].value == "s3"
        ):
            continue
        endpoint_bound = False
        for kw in call.keywords:
            if kw.arg != "endpoint_url":
                continue
            # endpoint_url=None is not endpoint-bound; anything else is.
            if not (isinstance(kw.value, ast.Constant) and kw.value.value is None):
                endpoint_bound = True
        if endpoint_bound:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                clients[target.id] = node
    return clients


def _endpoint_bound_clients(tree: ast.Module) -> Dict[str, ast.Assign]:
    """Module-level ``X = boto3.client("s3", endpoint_url=...)`` assignments."""
    clients: Dict[str, ast.Assign] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not (isinstance(func, ast.Attribute) and func.attr == "client"):
            continue
        if not any(
            isinstance(kw.value, ast.Name) or isinstance(kw.value, ast.Constant)
            for kw in call.keywords
            if kw.arg == "endpoint_url"
        ):
            continue
        # endpoint_url=None literal is not endpoint-bound.
        for kw in call.keywords:
            if kw.arg != "endpoint_url":
                continue
            if isinstance(kw.value, ast.Constant) and kw.value.value is None:
                break
        else:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    clients[target.id] = node
    return clients


def _calls_on(tree: ast.Module, name: str) -> List[str]:
    methods: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == name
        ):
            methods.append(func.attr)
    return methods


def _calls_on_unshadowed(tree: ast.Module, name: str) -> List[str]:
    """``name.method()`` calls, skipping functions where ``name`` is a parameter.

    ``_calls_on`` cannot tell the module-level client from a same-named function
    parameter, and these Lambdas genuinely have both (``s3_client`` is the module
    client AND the parameter of helpers like ``_validate_test_set_files``). Bailing
    out on that ambiguity would make the check silently vacuous on exactly the
    files it exists for, so resolve it instead: descend everywhere except the body
    of a function that shadows the name.
    """
    methods: List[str] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = child.args
                shadowed = any(
                    a.arg == name
                    for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]
                )
                if shadowed:
                    continue
            if isinstance(child, ast.Call):
                func = child.func
                if (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == name
                ):
                    methods.append(func.attr)
            walk(child)

    walk(tree)
    return methods


def _parameter_names(tree: ast.Module) -> Set[str]:
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                names.add(arg.arg)
    return names


@pytest.fixture(scope="module")
def presigner_lambdas() -> List[Path]:
    return _presigner_lambda_dirs()


def test_discovery_found_the_known_resolvers(presigner_lambdas):
    """Guard the guard: an empty list would make the assertion below vacuous."""
    names = {p.parent.name for p in presigner_lambdas}
    for expected in ("test_set_resolver", "upload_resolver", "api_handler"):
        assert expected in names, (
            f"{expected} was not discovered as a presigner Lambda; the template "
            f"scan is broken (found: {sorted(names)})"
        )


def test_endpoint_client_is_only_used_for_presigning(presigner_lambdas):
    violations: List[str] = []

    for index_path in presigner_lambdas:
        tree = ast.parse(index_path.read_text(encoding="utf-8"))
        clients = _endpoint_bound_clients(tree)
        params = _parameter_names(tree)

        for client_name in clients:
            assert client_name not in params, (
                f"{index_path.parent.name}: '{client_name}' is both the "
                "endpoint-bound module client and a function parameter, so this "
                "check cannot tell the two apart — rename one"
            )
            for method in _calls_on(tree, client_name):
                if not method.startswith("generate_presigned"):
                    violations.append(
                        f"{index_path.parent.name}: {client_name}.{method}()"
                    )

    assert not violations, (
        "These calls use an S3 client bound to the interface VPC endpoint for a "
        "real S3 API call: "
        + ", ".join(sorted(violations))
        + ". The endpoint hostname exists for presigned URLs handed to the "
        "browser; from a non-VPC-attached Lambda it resolves to private "
        "addresses, so the call hangs and API Gateway turns that into a bodiless "
        "504 at 29s. Use a plain boto3 S3 client for data-plane calls and keep "
        "the endpoint-bound one for generate_presigned_* only."
    )


def test_presigning_never_uses_the_data_plane_client(presigner_lambdas):
    """The inverse direction: a presigned URL must carry the endpoint hostname.

    Splitting the clients can be got wrong both ways. The assertion above catches
    a data-plane call on the endpoint-bound client (the resolver hangs). This one
    catches ``generate_presigned_*`` on the plain client, which fails differently
    and more quietly: the call succeeds, the resolver logs nothing, and the
    BROWSER gets a URL on the public S3 hostname that it cannot reach from an
    air-gapped VPC.

    Both checks share the same known blind spot: only module-level clients and
    direct ``name.method()`` calls are visible, so a client constructed inside a
    function, or handed to a helper under a different name, escapes them.
    """
    violations: List[str] = []

    for index_path in presigner_lambdas:
        tree = ast.parse(index_path.read_text(encoding="utf-8"))

        for client_name in _plain_s3_clients(tree):
            for method in _calls_on_unshadowed(tree, client_name):
                if method.startswith("generate_presigned"):
                    violations.append(
                        f"{index_path.parent.name}: {client_name}.{method}()"
                    )

    assert not violations, (
        "These presigned URLs are generated on an S3 client with no endpoint "
        "bound: "
        + ", ".join(sorted(violations))
        + ". In a private deployment the URL then names the public S3 hostname, "
        "which the browser cannot resolve — the request fails in the user's "
        "browser with nothing in this function's log. Use the endpoint-bound "
        "presign client for generate_presigned_* calls."
    )
