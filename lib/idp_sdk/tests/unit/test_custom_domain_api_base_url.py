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
