# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``VITE_API_BASE_URL`` must follow the origin the browser is actually on.

The SPA's data transport is ``POST {VITE_API_BASE_URL}/op/<field>``, and that
value is frozen into the JS bundle by Vite at build time. Under APIGateway
hosting the SPA and the ``/op`` transport are the *same* REST API, so when a
custom domain fronts it (``CustomDomainUrl``) the bundle must call the API on
that domain.

Pinning it to the raw ``<api-id>.execute-api.<region>.amazonaws.com`` host is not
merely a cosmetic cross-origin wart. With ``ApiGatewayVisibility=PRIVATE`` the
private-DNS override for that hostname exists only inside the VPC that owns the
execute-api endpoint, so a browser reaching the app through the vanity domain
frequently cannot resolve it at all: the SPA shell loads and then every data
call fails. That is what produced
``POST https://<vanity-host>/api/op/getTestSets 504`` reports from a private
deployment.

CloudFront hosting is the deliberate exception — the distribution has a single S3
origin and no ``/api`` behaviour, so a custom domain in front of it cannot reach
the API and the bundle must keep the execute-api URL.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "template.yaml").is_file() and (parent / "publish.py").is_file():
            return parent
    raise RuntimeError("Could not locate repo root containing template.yaml")


@pytest.fixture(scope="module")
def template() -> dict:
    with open(_repo_root() / "template.yaml", "r", encoding="utf-8") as f:
        # nosec B506 - _CFNLoader subclasses yaml.SafeLoader; the only
        # customization is a no-op constructor for CFN intrinsic tags. Input is
        # this repo's own committed template.
        return yaml.load(f, Loader=_CFNLoader)  # nosec B506


def _ui_env_var(template: dict, name: str) -> Any:
    env_vars = template["Resources"]["UICodeBuildProject"]["Properties"]["Environment"][
        "EnvironmentVariables"
    ]
    for env_var in env_vars:
        if env_var["Name"] == name:
            return env_var["Value"]
    raise AssertionError(f"{name} is not set on UICodeBuildProject")


class TestApiBaseUrlFollowsTheBrowserOrigin:
    def test_it_is_conditional_on_the_custom_domain(self, template):
        value = _ui_env_var(template, "VITE_API_BASE_URL")
        assert "!If" in value, (
            "VITE_API_BASE_URL is unconditional, so a deployment with "
            "CustomDomainUrl set bakes the raw execute-api host into the bundle "
            "and every /op call from the vanity domain is cross-origin (and in "
            "PRIVATE mode usually unresolvable)"
        )
        condition, when_custom, otherwise = value["!If"]
        assert condition == "UseCustomDomainForApi"
        assert when_custom == {"!Sub": "${CustomDomainUrl}/api"}, (
            "the custom-domain branch must be the domain plus the /api base-path "
            f"mapping, got {when_custom!r}"
        )
        assert otherwise == {"!GetAtt": "APIRESOLVERSTACK.Outputs.HttpApiEndpoint"}, (
            f"the fallback must remain the REST API's own endpoint, got {otherwise!r}"
        )

    def test_the_condition_requires_both_custom_domain_and_apigw_hosting(
        self, template
    ):
        condition = template["Conditions"]["UseCustomDomainForApi"]
        assert "!And" in condition, f"expected an !And, got {condition!r}"
        operands = condition["!And"]
        assert {"!Condition": "HasCustomDomain"} in operands, (
            "without HasCustomDomain the branch would !Sub an empty "
            "CustomDomainUrl into '/api'"
        )
        assert {"!Condition": "UseApiGatewayHosting"} in operands, (
            "CloudFront hosting must NOT route the API through the custom "
            "domain: the distribution has one S3 origin and no /api behaviour, "
            "so every data call would 403/404"
        )

    def test_changing_the_custom_domain_rebuilds_the_bundle(self, template):
        """A baked value that does not trigger a rebuild is a value that lies.

        ``CodeBuildRun`` is the custom resource that calls ``StartBuild``;
        CloudFormation only re-invokes it when one of its properties changes. If
        ``CustomDomainUrl`` were absent from ``UIBuildInputs``, adding the domain
        to an existing stack would update the CodeBuild env var, run no build,
        and leave the previous bundle — still pointing at execute-api — in place.
        """
        inputs = template["Resources"]["CodeBuildRun"]["Properties"]["UIBuildInputs"]
        flattened = yaml.dump(inputs)
        assert "CustomDomainUrl" in flattened, (
            "CustomDomainUrl is baked into VITE_API_BASE_URL but is not a "
            "CodeBuildRun property, so setting it on an existing stack will not "
            "rebuild the Web UI and the fix will silently not take effect"
        )
        assert "WebUIHosting" in flattened, (
            "WebUIHosting selects the branch, so it must also force a rebuild"
        )


def _evaluate(template: dict, node: Any, params: dict) -> Any:
    """Resolve `!If` / `!Ref` / `!Sub` / `!GetAtt` for a given parameter set.

    Enough of an evaluator to answer "what does this env var actually become for
    these parameters" — an assertion about the *shape* of an `!If` cannot tell you
    whether existing deployments still get the value they got before, and that is
    the question a change to a baked-in URL has to answer.
    """
    if isinstance(node, dict) and len(node) == 1:
        ((tag, value),) = node.items()
        if tag == "!If":
            name, when_true, when_false = value
            branch = (
                when_true
                if _bool(template, {"!Condition": name}, params)
                else when_false
            )
            return _evaluate(template, branch, params)
        if tag == "!Ref":
            return params.get(value, f"<{value}>")
        if tag == "!GetAtt":
            return f"<GetAtt:{value}>"
        if tag == "!Sub":
            out = value
            for key, val in params.items():
                out = out.replace("${" + key + "}", str(val))
            return out
        raise AssertionError(f"unhandled intrinsic {tag}: {node!r}")
    return node


def _bool(template: dict, node: Any, params: dict) -> bool:
    """Resolve a CloudFormation condition expression, named or inline."""
    ((tag, value),) = node.items()
    if tag == "!Condition":
        return _bool(template, template["Conditions"][value], params)
    if tag == "!Equals":
        left, right = (_evaluate(template, v, params) for v in value)
        return str(left) == str(right)
    if tag == "!Not":
        return not _bool(template, value[0], params)
    if tag == "!And":
        return all(_bool(template, v, params) for v in value)
    if tag == "!Or":
        return any(_bool(template, v, params) for v in value)
    raise AssertionError(f"unhandled condition expression {tag}: {node!r}")


EXECUTE_API = "<GetAtt:APIRESOLVERSTACK.Outputs.HttpApiEndpoint>"


class TestExistingDeploymentsAreUnaffected:
    """The other three corners of the matrix must resolve exactly as before.

    Only one parameter combination may change behaviour. Anything else — every
    public CloudFront deployment, every stack with no custom domain — has to keep
    the execute-api URL it was already baking in, or this fix breaks far more
    than it repairs.
    """

    @pytest.mark.parametrize(
        "hosting,domain,expected,why",
        [
            (
                "CloudFront",
                "",
                EXECUTE_API,
                "the default public build: no custom domain, CloudFront hosting",
            ),
            (
                "APIGateway",
                "",
                EXECUTE_API,
                "private/GovCloud hosting with no custom domain",
            ),
            (
                "CloudFront",
                "https://idp.example.com",
                EXECUTE_API,
                "CloudFront has one S3 origin and no /api behaviour, so the API "
                "must NOT be routed through a domain in front of it",
            ),
            (
                "APIGateway",
                "https://idp.example.com",
                "https://idp.example.com/api",
                "the only corner that changes: the custom domain fronts this "
                "same REST API",
            ),
        ],
    )
    def test_api_base_url_matrix(self, template, hosting, domain, expected, why):
        value = _ui_env_var(template, "VITE_API_BASE_URL")
        resolved = _evaluate(
            template,
            value,
            {"WebUIHosting": hosting, "CustomDomainUrl": domain},
        )
        assert resolved == expected, (
            f"WebUIHosting={hosting!r} CustomDomainUrl={domain!r} resolved to "
            f"{resolved!r}, expected {expected!r} — {why}"
        )

    def test_the_evaluator_is_not_trivially_agreeing(self, template):
        """Guard the guard: it must distinguish the corners, not return one value."""
        value = _ui_env_var(template, "VITE_API_BASE_URL")
        results = {
            _evaluate(
                template,
                value,
                {"WebUIHosting": h, "CustomDomainUrl": d},
            )
            for h in ("CloudFront", "APIGateway")
            for d in ("", "https://idp.example.com")
        }
        assert len(results) == 2, (
            "the evaluator produced "
            f"{results} — it should yield exactly two distinct values across the "
            "matrix (execute-api for three corners, the domain for one)"
        )
