# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The two hosting modes ship the SAME Content-Security-Policy.

The SPA is served either by CloudFront (``WebUIHosting=CloudFront``) or straight
off the REST API as an S3 proxy (``WebUIHosting=APIGateway`` — mandatory for
GovCloud, usual for private-network deployments). Only the CloudFront path can
carry a ``ResponseHeadersPolicy``, so the API Gateway path sets the same header
per-method instead, which means the policy exists **twice** in two different
templates:

* ``template.yaml`` -> ``SecurityHeadersPolicy`` (CloudFront)
* ``nested/api-resolvers/template.yaml`` -> ``Mappings.WebUiSecurity`` (consumed by
  ``WebUIRootMethod`` and ``WebUIProxyMethod``)

Both copies carry a comment telling the next editor to update the other one, and a
comment is not a mechanism: the whole reason APIGateway mode ran with *no* CSP for
several releases (threat UI.T07) is that a security control attached to one hosting
mode is invisible from the other. Divergence here does not fail a build, does not
fail cfn-lint, and does not show up in any deploy test that only exercises the
default (CloudFront) mode — it shows up as a GovCloud deployment quietly missing a
directive the commercial one has.

``frame-ancestors`` is the one directive allowed to differ, and it is asserted
rather than skipped: CloudFront sends ``X-Frame-Options: SAMEORIGIN`` and the API
Gateway methods send ``DENY``, so the CSPs say ``'self'`` and ``'none'``
respectively. A CSP ``frame-ancestors`` *overrides* ``X-Frame-Options`` wherever
both are understood, so copying one mode's value into the other would silently
change that mode's framing rule.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT_TEMPLATE = REPO_ROOT / "template.yaml"
API_TEMPLATE = REPO_ROOT / "nested/api-resolvers/template.yaml"

#: Directive -> (CloudFront value, APIGateway value) for every directive the two
#: policies are allowed to disagree on. See the module docstring.
PERMITTED_DIFFERENCES = {"frame-ancestors": ("'self'", "'none'")}


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags."""


def _tag_to_python(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{tag_suffix}": loader.construct_sequence(node, deep=True)}
    return {f"Fn::{tag_suffix}": loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag_to_python)


def _load(path: Path) -> dict:
    return yaml.load(path.read_text(), Loader=_CfnLoader) or {}


def _cloudfront_policy() -> str:
    resources = _load(ROOT_TEMPLATE)["Resources"]
    config = resources["SecurityHeadersPolicy"]["Properties"][
        "ResponseHeadersPolicyConfig"
    ]
    return config["SecurityHeadersConfig"]["ContentSecurityPolicy"][
        "ContentSecurityPolicy"
    ]


def _api_gateway_policy() -> str:
    """The mapping value, with the API Gateway static-value quotes stripped.

    An API Gateway response-parameter static value is wrapped in a pair of single
    quotes, which the service strips before emitting the header. The CSP keywords
    inside it carry their own single quotes, so only the outer pair comes off.
    """
    raw = _load(API_TEMPLATE)["Mappings"]["WebUiSecurity"]["ContentSecurityPolicy"][
        "Value"
    ]
    assert raw.startswith("'") and raw.endswith("'"), (
        "the WebUiSecurity mapping value must keep the outer single quotes that make "
        f"it an API Gateway *static* value, not a mapping expression; got: {raw[:40]}"
    )
    return raw[1:-1]


def _directive_list(policy: str) -> list[tuple[str, str]]:
    parsed: list[tuple[str, str]] = []
    for chunk in policy.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, sources = chunk.partition(" ")
        parsed.append((name, re.sub(r"\s+", " ", sources).strip()))
    return parsed


def _directives(policy: str) -> dict[str, str]:
    """Directive -> sources, keeping the FIRST occurrence.

    Browsers enforce the first occurrence of a directive and ignore later ones, so
    a dict built by overwriting would validate a value the browser never applies —
    an append-style edit that left a blanket `script-src ... https:` earlier in the
    string would pass every assertion below.
    ``test_neither_policy_repeats_a_directive`` rejects that shape outright; this
    ordering makes the rest of the suite honest if it is ever relaxed.
    """
    out: dict[str, str] = {}
    for name, sources in _directive_list(policy):
        out.setdefault(name, sources)
    return out


def test_neither_policy_repeats_a_directive():
    """A repeated directive is dead text that reads as if it were enforced."""
    for mode, policy in (
        ("CloudFront", _cloudfront_policy()),
        ("APIGateway", _api_gateway_policy()),
    ):
        names = [name for name, _ in _directive_list(policy)]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        assert not duplicates, (
            f"{mode} CSP declares {duplicates} more than once; browsers enforce the "
            "first occurrence and ignore the rest, so the later copy is misleading"
        )


def test_both_hosting_modes_declare_the_same_directives():
    cloudfront = _directives(_cloudfront_policy())
    apigateway = _directives(_api_gateway_policy())
    assert sorted(cloudfront) == sorted(apigateway), (
        "the CloudFront and APIGateway CSPs no longer cover the same directives — "
        "a directive added to one mode is missing from the other:\n"
        f"  CloudFront only: {sorted(set(cloudfront) - set(apigateway))}\n"
        f"  APIGateway only: {sorted(set(apigateway) - set(cloudfront))}"
    )


def test_every_directive_matches_except_the_documented_differences():
    cloudfront = _directives(_cloudfront_policy())
    apigateway = _directives(_api_gateway_policy())
    drifted = {
        name: (value, apigateway.get(name))
        for name, value in cloudfront.items()
        if apigateway.get(name) != value and name not in PERMITTED_DIFFERENCES
    }
    assert not drifted, (
        "these CSP directives differ between hosting modes; update both copies "
        "(template.yaml SecurityHeadersPolicy and the api-resolvers WebUiSecurity "
        f"mapping) or record the difference in PERMITTED_DIFFERENCES: {drifted}"
    )


def test_the_documented_differences_are_still_what_they_claim_to_be():
    """A permitted difference is an assertion, not an exemption.

    If ``frame-ancestors`` stops being ``'self'``/``'none'``, either the framing
    posture changed on purpose (update this test and the X-Frame-Options headers
    together) or one copy was edited from the other without noticing that CSP wins
    over ``X-Frame-Options``.
    """
    cloudfront = _directives(_cloudfront_policy())
    apigateway = _directives(_api_gateway_policy())
    for name, (expected_cf, expected_api) in PERMITTED_DIFFERENCES.items():
        assert cloudfront[name] == expected_cf
        assert apigateway[name] == expected_api


#: A ``script-src`` source is acceptable only if it names a specific origin or is
#: one of the keywords already reviewed and accepted. Written as an allowlist
#: rather than a denylist of known-bad spellings: ``https:``, ``https://*``,
#: ``*``, ``data:`` and ``blob:`` all re-permit script from anywhere, and so does
#: the next spelling nobody thought to enumerate.
_SCRIPT_SRC_KEYWORDS = frozenset(
    {"'self'", "'none'", "'unsafe-inline'", "'unsafe-eval'", "'strict-dynamic'"}
)
#: ``https://host[:port][/path]``, optionally with a leftmost-label wildcard.
#:
#: Two or more labels are required after a ``*.``, so ``https://*.amazonaws.com``
#: passes but ``https://*.com`` — every host on a whole TLD — does not. That is a
#: heuristic, not a public-suffix check (``https://*.co.uk`` would still pass); it
#: catches the plausible mistake rather than pretending to be exhaustive.
#: A path only ever narrows a source, so it is allowed.
_HOST_SOURCE = re.compile(
    r"^https://(?:\*\.)?"
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+"
    r"(?::\d+)?(?:/\S*)?$"
)


def test_script_src_names_specific_origins_only():
    """The v0.6.x AppSec finding, pinned so it cannot regress in either mode.

    ``https:`` as a ``script-src`` source permits script from ANY HTTPS origin,
    which makes the directive close to decorative — that is the finding this branch
    fixed. Nonces and hashes are deliberately *not* in the keyword set: if the app
    moves to them, this test should be updated by whoever does that work rather
    than silently accepting them.
    """
    for mode, policy in (
        ("CloudFront", _cloudfront_policy()),
        ("APIGateway", _api_gateway_policy()),
    ):
        sources = _directives(policy)["script-src"].split()
        assert sources, f"{mode} has no script-src sources at all"
        bad = [
            s
            for s in sources
            if s not in _SCRIPT_SRC_KEYWORDS and not _HOST_SOURCE.match(s)
        ]
        assert not bad, (
            f"{mode} script-src carries {bad}, which does not name a specific "
            "origin (a bare scheme, a wildcard host, or data:/blob: lets script "
            "load from anywhere). List the exact hosts instead."
        )


#: The two methods that serve the SPA in APIGateway hosting mode. Both must send
#: the CSP: the document is what the policy protects, and an asset response that
#: omits it is a same-origin script/style delivered with no policy at all.
WEB_UI_METHODS = ("WebUIRootMethod", "WebUIProxyMethod")
CSP_HEADER = "method.response.header.Content-Security-Policy"


def _responses(logical_id: str) -> tuple[list[dict], list[dict]]:
    props = _load(API_TEMPLATE)["Resources"][logical_id]["Properties"]
    return props["Integration"]["IntegrationResponses"], props["MethodResponses"]


def test_every_web_ui_response_actually_sends_the_policy():
    """Otherwise the mapping is dead config and the tests above are decoration.

    Checking only the ``WebUiSecurity`` mapping would let someone delete the header
    from a method while every sync assertion stayed green — re-opening UI.T07
    exactly as it was, with a test suite reporting success.

    Every status code is covered, not just the 200: a 404 from a missing asset key
    is still a response from the SPA's own origin, and that is the response most
    easily reached by an attacker-chosen URL. Both halves are required by API
    Gateway — the integration response supplies the value, and the method response
    must *declare* the header or the service drops it silently rather than erroring.
    """
    expected = {"Fn::FindInMap": ["WebUiSecurity", "ContentSecurityPolicy", "Value"]}
    for logical_id in WEB_UI_METHODS:
        integration_responses, method_responses = _responses(logical_id)
        assert len(integration_responses) >= 3, (
            f"{logical_id} declares only {len(integration_responses)} integration "
            "responses; the 200/404/500 set is what this test scopes over"
        )
        for response in integration_responses:
            status = response["StatusCode"]
            value = response.get("ResponseParameters", {}).get(CSP_HEADER)
            assert value == expected, (
                f"{logical_id} {status} must map Content-Security-Policy from the "
                "WebUiSecurity mapping (an inline copy would drift from the "
                f"CloudFront policy); got {value!r}"
            )
        declared = {
            r["StatusCode"]
            for r in method_responses
            if r.get("ResponseParameters", {}).get(CSP_HEADER) is True
        }
        mapped = {r["StatusCode"] for r in integration_responses}
        assert mapped <= declared, (
            f"{logical_id} maps the CSP for {sorted(mapped - declared)} but those "
            "MethodResponses do not declare Content-Security-Policy, so API Gateway "
            "drops the header without failing the deployment"
        )


def test_the_guard_can_actually_see_both_policies():
    """A key or path mistake would make every test above vacuously pass."""
    for policy in (_cloudfront_policy(), _api_gateway_policy()):
        directives = _directives(policy)
        assert "script-src" in directives
        assert "connect-src" in directives
        assert len(directives) >= 8, directives
