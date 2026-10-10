# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Stack attribution and confirmation logic of ``OrphanedResourceCleanup``.

``idp_sdk._core.cleanup_orphaned`` deletes real infrastructure — CloudFront
distributions, log groups, AppSync APIs, IAM policies, S3 buckets, DynamoDB
tables — for IDP stacks that no longer exist. Every deletion is gated on one
question: *which stack does this resource belong to?* That question is answered
entirely by the six ``extract_stack_name_from_*`` methods plus
``get_stack_state``, and a wrong answer at that point destroys a live
deployment's data. This file is therefore organised around attribution rather
than around code paths.

Three shapes of wrong answer matter, and each is tested for every extractor:

* **Over-matching** — a name that belongs to no IDP stack yields a stack name.
  If that name happens to collide with a deleted stack, an unrelated resource is
  deleted.
* **Mis-attribution** — a resource of stack ``A`` yields the name of stack ``B``.
  This is the only failure mode that can delete a *live* stack's data, so the
  tables below deliberately include the near-miss names that produce a truncated
  or globally-substituted result.
* **Under-matching** — an orphan yields ``""`` and is skipped. Safe, but it
  leaves paid-for resources behind, so the cases where it happens are pinned
  here so that a future change to the patterns is a visible diff rather than a
  silent behaviour change.

``discover_idp_stacks`` is exercised against ``moto``'s CloudFormation across two
regions, because the property that protects a live stack is cross-regional: a
stack name may exist as ``DELETE_COMPLETE`` in one region and
``CREATE_COMPLETE`` in another, and only the post-loop sweep in
``discover_idp_stacks`` keeps the live one out of the deletion set.

``_confirm_deletion`` and ``_delete_resources_concurrently`` are driven through
``click.prompt``, since that is the function the module calls; the concurrent
path is asserted on the arguments each deletion received and on the resulting
result lists, not merely on "something was called".
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Tuple

import boto3
import click
import pytest
from moto import mock_aws

from idp_sdk._core import cleanup_orphaned as module_under_test
from idp_sdk._core.cleanup_orphaned import IDP_REGIONS, OrphanedResourceCleanup

# A template whose Description carries the marker `discover_idp_stacks` looks
# for. Identification by Description is the primary route in the source; the
# `IDP-` name heuristic is a fallback and is tested separately.
IDP_TEMPLATE = json.dumps(
    {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "AWS GenAI IDP Accelerator - test fixture",
        "Resources": {"Queue": {"Type": "AWS::SQS::Queue", "Properties": {}}},
    }
)

PLAIN_TEMPLATE = json.dumps(
    {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Some unrelated application",
        "Resources": {"Queue": {"Type": "AWS::SQS::Queue", "Properties": {}}},
    }
)


def seed_stack(
    region: str, name: str, *, deleted: bool = False, template: str = IDP_TEMPLATE
) -> None:
    """Create (and optionally delete) a CloudFormation stack in ``moto``.

    A deleted stack still appears in ``list_stacks`` with status
    ``DELETE_COMPLETE``, which is exactly the state the cleanup tool keys on.
    """
    cfn = boto3.client("cloudformation", region_name=region)
    cfn.create_stack(StackName=name, TemplateBody=template)
    if deleted:
        cfn.delete_stack(StackName=name)


@pytest.fixture
def aws(aws_credentials):
    """A moto-backed AWS for the duration of one test."""
    with mock_aws():
        yield aws_credentials


@pytest.fixture
def cleanup(aws) -> OrphanedResourceCleanup:
    """A cleanup object whose seven boto3 clients point at ``moto``."""
    return OrphanedResourceCleanup(region="us-east-1")


# ---------------------------------------------------------------------------
# extract_stack_name_from_comment  (CloudFront distributions)
# ---------------------------------------------------------------------------

COMMENT_CASES: List[Tuple[str, str]] = [
    # The shape the IDP templates actually produce.
    ("Web app cloudfront distribution IDP-mystack", "IDP-mystack"),
    ("Web app cloudfront distribution idp-lower-case", "idp-lower-case"),
    # Prefix present but nothing after it: no stack name, so nothing is deleted.
    ("Web app cloudfront distribution ", ""),
    # Not the IDP comment at all.
    ("", ""),
    ("Web app distribution IDP-mystack", ""),
    ("CloudFront distribution for IDP-mystack", ""),
    # Case matters: the prefix test is `str.startswith`, so a differently-cased
    # comment is not an IDP distribution as far as this tool is concerned.
    ("web app cloudfront distribution IDP-mystack", ""),
    # Leading whitespace defeats the prefix test.
    (" Web app cloudfront distribution IDP-mystack", ""),
]


@pytest.mark.unit
@pytest.mark.parametrize("comment,expected", COMMENT_CASES)
def test_extract_stack_name_from_comment(cleanup, comment, expected):
    assert cleanup.extract_stack_name_from_comment(comment) == expected


@pytest.mark.unit
def test_a_comment_with_extra_words_yields_a_bogus_stack_name(cleanup):
    """The prefix is stripped literally, so extra words become part of the name.

    A failure here would mean the extractor started parsing the comment instead
    of stripping a fixed prefix. The value below is not a stack name, and the
    safety net is `get_stack_state`, which answers ``UNKNOWN`` for it and causes
    the distribution to be skipped — so this is pinned as the reason the tool is
    safe here, not as a correct result.
    """
    assert (
        cleanup.extract_stack_name_from_comment(
            "Web app cloudfront distribution for stack IDP-mystack"
        )
        == "for stack IDP-mystack"
    )


# ---------------------------------------------------------------------------
# extract_stack_name_from_log_group
# ---------------------------------------------------------------------------

LOG_GROUP_CASES: List[Tuple[str, str]] = [
    # The nested-pattern-stack shape: the segment holding PATTERN ends the name.
    ("/IDP-mystack-PATTERN2-ABC123/lambda/OCRFunction", "IDP-mystack"),
    ("/IDP-mystack-PATTERN1-XY/lambda/BdaInvoke", "IDP-mystack"),
    # A segment holding STACK terminates the name too.
    ("/IDP-mystack-APIRESOLVERSTACK-Q1/lambda/Resolver", "IDP-mystack"),
    # A multi-hyphen stack name survives intact.
    ("/IDP-team-demo-01-PATTERN2-ABC/lambda/Classify", "IDP-team-demo-01"),
    # Glue crawler role log group.
    (
        "/aws-glue/crawlers-role/IDP-mystack-DocumentSectionsCrawlerRole-1AB",
        "IDP-mystack",
    ),
    # Glue near-miss: right prefix, different role.
    ("/aws-glue/crawlers-role/IDP-mystack-SomeOtherRole-1AB", ""),
    # No PATTERN/STACK segment at all. This is the `/<StackName>/lambda/<Fn>`
    # shape the repository's own log groups mostly use, and it yields nothing.
    ("/IDP-mystack/lambda/OCRFunction", ""),
    ("/IDP-mystack-prod/lambda/OCRFunction", ""),
    # Lambda's own default group shape: the first path segment is `aws`, which
    # holds no hyphen, so nothing is extracted.
    ("/aws/lambda/IDP-mystack-PATTERN2-OCRFunction", ""),
    # AppSync API log group: no `/lambda/` and not the Glue shape.
    ("/aws/appsync/apis/abcd1234efgh", ""),
    # No leading slash: fails the first test outright.
    ("IDP-mystack-PATTERN2-ABC/lambda/OCRFunction", ""),
    # PATTERN in the very first segment leaves nothing to the left of it.
    ("/PATTERN2-ABC/lambda/OCRFunction", ""),
    ("", ""),
]


@pytest.mark.unit
@pytest.mark.parametrize("log_group,expected", LOG_GROUP_CASES)
def test_extract_stack_name_from_log_group(cleanup, log_group, expected):
    assert cleanup.extract_stack_name_from_log_group(log_group) == expected


@pytest.mark.unit
def test_the_first_pattern_segment_wins_and_truncates_the_stack_name(cleanup):
    """A stack whose own name contains ``PATTERN`` is attributed incorrectly.

    The scan is left to right and stops at the first segment containing
    ``PATTERN``, so a stack legitimately named ``IDP-PATTERNS-demo`` has its log
    groups attributed to a stack called ``IDP``. That is a mis-attribution, and
    the consequence is not hypothetical: were a deleted stack named ``IDP`` to
    exist in the same account, this live stack's log groups would be deleted.
    Pinned as current behaviour — a fix belongs in the extractor, not here.
    """
    assert (
        cleanup.extract_stack_name_from_log_group(
            "/IDP-PATTERNS-demo-PATTERN2-ABC/lambda/OCRFunction"
        )
        == "IDP"
    )


# ---------------------------------------------------------------------------
# extract_stack_name_from_api_name  (AppSync)
# ---------------------------------------------------------------------------

API_NAME_CASES: List[Tuple[str, str]] = [
    ("IDP-mystack-p1-api", "IDP-mystack"),
    ("IDP-mystack-p2-api", "IDP-mystack"),
    ("IDP-mystack-p3-api", "IDP-mystack"),
    # No pattern infix: only the `-api` suffix is removed.
    ("IDP-mystack-api", "IDP-mystack"),
    ("IDP-team-demo-01-p2-api", "IDP-team-demo-01"),
    # Must end in `-api`.
    ("IDP-mystack-p2-api-v2", ""),
    ("IDP-mystack-graphql", ""),
    ("IDP-mystack-apis", ""),
    ("", ""),
    # `-api` alone leaves an empty name, which `get_stack_state` reports UNKNOWN.
    ("-api", ""),
]


@pytest.mark.unit
@pytest.mark.parametrize("api_name,expected", API_NAME_CASES)
def test_extract_stack_name_from_api_name(cleanup, api_name, expected):
    assert cleanup.extract_stack_name_from_api_name(api_name) == expected


@pytest.mark.unit
def test_the_pattern_infix_is_removed_everywhere_it_appears(cleanup):
    """``str.replace`` is global, so a repeated infix is stripped twice.

    A stack named ``IDP-p2-api-demo`` produces the API name
    ``IDP-p2-api-demo-p2-api``, and both occurrences are removed, yielding a name
    that matches no stack. The observable consequence is under-matching (the API
    is skipped as ``UNKNOWN``) rather than a wrong deletion, but the derived name
    is wrong, so it is pinned here.
    """
    assert cleanup.extract_stack_name_from_api_name("IDP-p2-api-demo-p2-api") == (
        "IDP-demo"
    )


# ---------------------------------------------------------------------------
# extract_stack_name_from_policy_name  (IAM + CloudFront response headers)
# ---------------------------------------------------------------------------

POLICY_NAME_CASES: List[Tuple[str, str]] = [
    ("IDP-mystack-security-headers-policy", "IDP-mystack"),
    ("IDP-mystack-PermissionsBoundary", "IDP-mystack"),
    # PATTERN-bearing IAM policies: the name ends at the PATTERN segment.
    ("IDP-mystack-PATTERN2STACK-LambdaECRAccessPolicy-AB1", "IDP-mystack"),
    ("IDP-mystack-PATTERN1STACK-Policy-XY", "IDP-mystack"),
    ("IDP-team-demo-PATTERN2STACK-Policy", "IDP-team-demo"),
    # None of the three shapes.
    ("IDP-mystack-LambdaRole", ""),
    ("IDP-mystack-security-headers", ""),
    ("IDP-mystack-permissionsboundary", ""),
    ("", ""),
    # PATTERN with nothing to its left.
    ("PATTERN2STACK-Policy", ""),
]


@pytest.mark.unit
@pytest.mark.parametrize("policy_name,expected", POLICY_NAME_CASES)
def test_extract_stack_name_from_policy_name(cleanup, policy_name, expected):
    assert cleanup.extract_stack_name_from_policy_name(policy_name) == expected


@pytest.mark.unit
def test_the_policy_suffix_is_also_removed_everywhere_it_appears(cleanup):
    """The suffix strip is a global ``str.replace``, not a right-strip.

    A stack whose name itself ends in ``-PermissionsBoundary`` loses both
    occurrences. As with the AppSync case the result matches no stack, so the
    policy is skipped; the pin exists so a change to right-stripping is visible.
    """
    assert (
        cleanup.extract_stack_name_from_policy_name(
            "IDP-PermissionsBoundary-demo-PermissionsBoundary"
        )
        == "IDP-demo"
    )


# ---------------------------------------------------------------------------
# extract_stack_name_from_bucket_name  (S3 — the highest-consequence extractor)
# ---------------------------------------------------------------------------

BUCKET_CASES: List[Tuple[str, str]] = [
    ("idp-mystack-inputbucket-1a2b3c", "idp-mystack"),
    ("idp-mystack-outputbucket-1a2b3c", "idp-mystack"),
    ("idp-mystack-workingbucket-1a2b3c", "idp-mystack"),
    ("idp-mystack-loggingbucket-1a2b3c", "idp-mystack"),
    ("idp-mystack-configurationbucket-1a2b3c", "idp-mystack"),
    ("idp-mystack-configbucket-1a2b3c", "idp-mystack"),
    ("idp-mystack-testsetbucket-1a2b3c", "idp-mystack"),
    ("idp-mystack-discoverybucket-1a2b3c", "idp-mystack"),
    ("idp-mystack-evaluationbaselinebucket-1a2b3c", "idp-mystack"),
    ("idp-mystack-reportingbucket-1a2b3c", "idp-mystack"),
    ("idp-team-demo-01-inputbucket-1a2b3c", "idp-team-demo-01"),
    # The suffix must be bracketed by hyphens on both sides: a bucket whose name
    # merely ends in the role word is not matched.
    ("idp-mystack-inputbucket", ""),
    ("idp-mystack-inputbuckets-1a2b3c", ""),
    # Nothing to the left of the suffix (`idx > 0` is required).
    ("-inputbucket-1a2b3c", ""),
    # Unrelated buckets must yield nothing at all.
    ("", ""),
    ("my-application-logs", ""),
    ("cloudtrail-bucket-123456789012", ""),
    ("idp-artifacts-us-east-1", ""),
]


@pytest.mark.unit
@pytest.mark.parametrize("bucket_name,expected", BUCKET_CASES)
def test_extract_stack_name_from_bucket_name(cleanup, bucket_name, expected):
    assert cleanup.extract_stack_name_from_bucket_name(bucket_name) == expected


@pytest.mark.unit
def test_bucket_matching_is_case_insensitive_but_returns_the_original_casing(cleanup):
    """The suffix search lowercases; the returned slice comes from the original.

    S3 bucket names are lowercase in practice, so this only matters for a name
    supplied from elsewhere. It is asserted because the returned string is fed
    straight to a case-insensitive stack lookup, and returning the *lowered*
    name instead would still work — meaning nothing else in the tool would
    notice if this changed.
    """
    assert (
        cleanup.extract_stack_name_from_bucket_name("IDP-MyStack-INPUTBUCKET-1A2B")
        == "IDP-MyStack"
    )


@pytest.mark.unit
def test_the_configuration_bucket_suffix_is_not_confused_with_the_config_one(cleanup):
    """``-configurationbucket-`` and ``-configbucket-`` are distinct suffixes.

    They are checked in that order, and neither is a substring of the other, so
    a configuration bucket must not be truncated at ``-config``. A failure here
    would attribute ``idp-mystack-configurationbucket-x`` to a stack named
    ``idp-mystack`` — which is right — or, if the suffix list were reordered
    carelessly and a `-config-` style entry added, to something shorter.
    """
    assert (
        cleanup.extract_stack_name_from_bucket_name(
            "idp-mystack-configurationbucket-1a2b"
        )
        == "idp-mystack"
    )
    assert (
        cleanup.extract_stack_name_from_bucket_name("idp-mystack-configbucket-1a2b")
        == "idp-mystack"
    )


@pytest.mark.unit
def test_the_earliest_suffix_in_the_declared_order_wins(cleanup):
    """Suffix precedence follows the declared list order, not position in the name.

    ``-inputbucket-`` is declared before ``-outputbucket-``, so for a name
    holding both it is the one that decides the split — even though it appears
    *later* in the string. The stack name therefore comes out as everything up to
    the input marker, which includes the output marker. The name below is
    contrived, but the property is not: the extractor does not take the leftmost
    marker in the name, which is what a "stack name then role" reading of the
    pattern would predict.
    """
    assert (
        cleanup.extract_stack_name_from_bucket_name(
            "idp-stack-outputbucket-x-inputbucket-y"
        )
        == "idp-stack-outputbucket-x"
    )


# ---------------------------------------------------------------------------
# extract_stack_name_from_table_name  (DynamoDB)
# ---------------------------------------------------------------------------

TABLE_CASES: List[Tuple[str, str]] = [
    ("IDP-mystack-TrackingTable-1AB2C", "IDP-mystack"),
    ("IDP-mystack-ConfigTable-1AB2C", "IDP-mystack"),
    ("IDP-mystack-AgentTable-1AB2C", "IDP-mystack"),
    ("IDP-mystack-MeteringTable-1AB2C", "IDP-mystack"),
    ("IDP-team-demo-01-TrackingTable-1AB", "IDP-team-demo-01"),
    # Suffix must be hyphen-bracketed on both sides.
    ("IDP-mystack-TrackingTable", ""),
    ("IDP-mystack-TrackingTables-1AB", ""),
    # Nothing to the left.
    ("-TrackingTable-1AB", ""),
    # Unrelated tables.
    ("", ""),
    ("my-application-sessions", ""),
    ("IDP-mystack-SomeOtherTable-1AB", ""),
]


@pytest.mark.unit
@pytest.mark.parametrize("table_name,expected", TABLE_CASES)
def test_extract_stack_name_from_table_name(cleanup, table_name, expected):
    assert cleanup.extract_stack_name_from_table_name(table_name) == expected


@pytest.mark.unit
def test_table_matching_is_case_sensitive_unlike_bucket_matching(cleanup):
    """The DynamoDB extractor does not lowercase, so casing must match exactly.

    This asymmetry with the S3 extractor is deliberate to pin, not to endorse: a
    table named with lowercase role words is never attributed to any stack and is
    therefore never cleaned up. The failure direction is under-matching (an
    orphaned table survives), which is the safe direction, but the two
    extractors disagreeing is the kind of difference that is invisible in review.
    """
    assert cleanup.extract_stack_name_from_table_name(
        "IDP-mystack-TrackingTable-1ab"
    ) == ("IDP-mystack")
    assert (
        cleanup.extract_stack_name_from_table_name("idp-mystack-trackingtable-1ab")
        == ""
    )
    # The S3 extractor, given the same casing, does match.
    assert (
        cleanup.extract_stack_name_from_bucket_name("idp-mystack-INPUTBUCKET-1ab")
        == "idp-mystack"
    )


# ---------------------------------------------------------------------------
# _get_cfn_client
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_cfn_client_is_built_for_the_requested_region(cleanup):
    """Stack discovery is per-region, so the client must honour its argument.

    A client built in the wrong region reports no stacks there, which turns a
    live stack into an unknown one — and unknown resources are skipped, so the
    error would be invisible until an orphan was missed.
    """
    assert cleanup._get_cfn_client("eu-west-1").meta.region_name == "eu-west-1"
    assert cleanup._get_cfn_client("ap-northeast-1").meta.region_name == (
        "ap-northeast-1"
    )


# ---------------------------------------------------------------------------
# discover_idp_stacks
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_discovery_separates_active_from_deleted_stacks(cleanup):
    seed_stack("us-east-1", "IDP-live")
    seed_stack("us-east-1", "IDP-dead", deleted=True)

    result = cleanup.discover_idp_stacks(regions=["us-east-1"])

    assert set(result["active_stacks"]) == {"IDP-live"}
    assert set(result["deleted_stacks"]) == {"IDP-dead"}
    assert result["deleted_stacks"]["IDP-dead"]["region"] == "us-east-1"
    assert result["deleted_stacks"]["IDP-dead"]["status"] == "DELETE_COMPLETE"
    assert result["deleted_stacks"]["IDP-dead"]["id"].startswith(
        "arn:aws:cloudformation"
    )


@pytest.mark.unit
def test_a_stack_alive_in_another_region_is_never_treated_as_deleted(cleanup):
    """The cross-region protection, in the order that used to break it.

    ``us-east-1`` is scanned before ``us-west-2``, so the ``DELETE_COMPLETE``
    instance is seen *first* and the in-loop guard (which only looks at stacks
    found so far) cannot exclude it. Only the sweep after the region loop
    removes it. If that sweep were dropped, the live stack's buckets and tables
    would be attributed to a deleted stack and deleted — the single most
    destructive failure this module can have.
    """
    seed_stack("us-east-1", "IDP-shared", deleted=True)
    seed_stack("us-west-2", "IDP-shared")

    result = cleanup.discover_idp_stacks(regions=["us-east-1", "us-west-2"])

    assert "IDP-shared" not in result["deleted_stacks"]
    assert result["active_stacks"]["IDP-shared"]["region"] == "us-west-2"
    assert cleanup.get_stack_state("IDP-shared")[0] == "ACTIVE"


@pytest.mark.unit
def test_the_live_instance_protects_a_differently_cased_deleted_name(cleanup):
    """The protection sweep compares upper-cased names.

    CloudFormation stack names are case-sensitive, but the resources this tool
    matches are not consistently cased (buckets are lowercase), so the sweep
    folds case. A stack deleted as ``IDP-Shared`` and live as ``idp-shared``
    must leave nothing in the deletion set.
    """
    seed_stack("us-east-1", "IDP-Shared", deleted=True)
    seed_stack("us-west-2", "idp-shared")

    result = cleanup.discover_idp_stacks(regions=["us-east-1", "us-west-2"])

    assert result["deleted_stacks"] == {}
    assert set(result["active_stacks"]) == {"idp-shared"}


@pytest.mark.unit
def test_a_non_idp_stack_is_in_neither_set(cleanup):
    """Anything not identified as IDP must be invisible to the tool.

    ``get_stack_state`` answers ``UNKNOWN`` for such a stack, and every
    ``cleanup_*`` method skips ``UNKNOWN``. So a stack outside the solution is
    protected by being unrecognised, and this is the test that keeps it that way.
    """
    seed_stack("us-east-1", "some-other-app", template=PLAIN_TEMPLATE)
    seed_stack(
        "us-east-1", "some-other-dead-app", deleted=True, template=PLAIN_TEMPLATE
    )

    result = cleanup.discover_idp_stacks(regions=["us-east-1"])

    assert result["active_stacks"] == {}
    assert result["deleted_stacks"] == {}
    assert cleanup.get_stack_state("some-other-dead-app") == ("UNKNOWN", None)


@pytest.mark.unit
def test_the_name_heuristic_recognises_a_short_idp_name_without_the_marker(cleanup):
    """A stack named ``IDP-*`` with few hyphens counts even with no marker.

    Deleted stacks can lose their template metadata, so the name is a fallback.
    ``IDP-dead`` has one hyphen and qualifies; the deeply-hyphenated name does
    not, and must stay invisible.
    """
    seed_stack("us-east-1", "IDP-dead", deleted=True, template=PLAIN_TEMPLATE)
    seed_stack("us-east-1", "IDP-a-b-c-d-e", deleted=True, template=PLAIN_TEMPLATE)

    result = cleanup.discover_idp_stacks(regions=["us-east-1"])

    assert set(result["deleted_stacks"]) == {"IDP-dead"}


@pytest.mark.unit
def test_the_name_heuristic_also_accepts_a_nested_pattern_stack_name(cleanup):
    """A long name qualifies if it carries a nested-pattern marker.

    ``IDP-demo-PATTERN2-NestedStackThing`` has four hyphens, so the
    ``count("-") <= 2`` arm rejects it; the pattern-marker arm accepts it.
    """
    seed_stack(
        "us-east-1",
        "IDP-demo-PATTERN2-abc-def",
        deleted=True,
        template=PLAIN_TEMPLATE,
    )

    result = cleanup.discover_idp_stacks(regions=["us-east-1"])

    assert set(result["deleted_stacks"]) == {"IDP-demo-PATTERN2-abc-def"}


@pytest.mark.unit
def test_discovery_defaults_to_the_declared_region_list(cleanup):
    """Called with no argument, discovery covers ``IDP_REGIONS`` and only those.

    The stack below sits in a region outside that list, so a tool that
    discovered "all regions" would find it. It must not: the region set is what
    bounds how long a cleanup run takes and which accounts' partitions are
    touched.
    """
    assert "us-east-2" not in IDP_REGIONS
    seed_stack("us-east-2", "IDP-elsewhere", deleted=True)
    seed_stack("eu-west-1", "IDP-listed", deleted=True)

    result = cleanup.discover_idp_stacks()

    assert set(result["deleted_stacks"]) == {"IDP-listed"}


@pytest.mark.unit
def test_an_explicit_region_list_narrows_discovery(cleanup):
    seed_stack("us-east-1", "IDP-here", deleted=True)
    seed_stack("us-west-2", "IDP-there", deleted=True)

    result = cleanup.discover_idp_stacks(regions=["us-east-1"])

    assert set(result["deleted_stacks"]) == {"IDP-here"}


@pytest.mark.unit
def test_a_region_that_cannot_be_listed_does_not_abort_the_other_regions(
    cleanup, monkeypatch
):
    """One unusable region must not hide the stacks in the rest.

    An opted-out region, or a credential without access to it, raises on
    ``list_stacks``. The failure is logged and swallowed. The risk this test
    guards is the *other* direction of that design: a region that fails leaves
    its live stacks undiscovered, so anything attributed to them becomes
    ``UNKNOWN`` — skipped, not deleted. The assertion is that discovery still
    completes and still reports the reachable region.
    """
    real_get_client = cleanup._get_cfn_client

    def flaky_client(region: str):
        client = real_get_client(region)
        if region == "us-west-2":

            def explode(*_args, **_kwargs):
                raise RuntimeError("region opted out")

            monkeypatch.setattr(client, "get_paginator", explode, raising=False)
        return client

    monkeypatch.setattr(cleanup, "_get_cfn_client", flaky_client)

    seed_stack("us-east-1", "IDP-here", deleted=True)
    seed_stack("us-west-2", "IDP-hidden", deleted=True)

    result = cleanup.discover_idp_stacks(regions=["us-east-1", "us-west-2"])

    assert set(result["deleted_stacks"]) == {"IDP-here"}
    assert cleanup._discovery_complete is True


# ---------------------------------------------------------------------------
# get_stack_state
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_stack_state_runs_discovery_on_first_use(cleanup):
    """The lookup is lazy: it discovers if nobody has.

    Every ``cleanup_*`` method relies on this, so a stack state queried before
    ``run_cleanup`` has discovered anything must still be correct rather than
    ``UNKNOWN``.
    """
    seed_stack("us-east-1", "IDP-dead", deleted=True)
    assert cleanup._discovery_complete is False

    state, info = cleanup.get_stack_state("IDP-dead")

    assert cleanup._discovery_complete is True
    assert state == "DELETED"
    assert info is not None and info["region"] == "us-east-1"


@pytest.mark.unit
@pytest.mark.parametrize(
    "queried",
    ["IDP-MixedCase", "idp-mixedcase", "IDP-MIXEDCASE", "iDp-MiXeDcAsE"],
)
def test_get_stack_state_matches_case_insensitively(cleanup, queried):
    """A bucket name is lowercase while its stack name may not be.

    If this lookup were case-sensitive, a live stack's lowercase bucket would
    resolve to ``UNKNOWN`` rather than ``ACTIVE``. That is safe today (unknown
    resources are skipped) but the same fold is what makes a *deleted* stack's
    bucket findable at all, so the property is load-bearing in both directions.
    """
    seed_stack("us-east-1", "IDP-MixedCase")
    cleanup.discover_idp_stacks(regions=["us-east-1"])

    state, info = cleanup.get_stack_state(queried)

    assert state == "ACTIVE"
    assert info is not None and info["status"] == "CREATE_COMPLETE"


@pytest.mark.unit
def test_get_stack_state_reports_unknown_with_no_info(cleanup):
    cleanup.discover_idp_stacks(regions=["us-east-1"])
    assert cleanup.get_stack_state("IDP-never-existed") == ("UNKNOWN", None)


# ---------------------------------------------------------------------------
# _confirm_deletion
# ---------------------------------------------------------------------------

STACK_INFO: Dict[str, str] = {
    "id": "arn:aws:cloudformation:us-east-1:123456789012:stack/IDP-dead/abc",
    "region": "us-east-1",
    "status": "DELETE_COMPLETE",
}


class PromptScript:
    """A scripted stand-in for ``click.prompt`` that records every call."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.calls: List[str] = []

    def __call__(self, text: str, **_kwargs) -> str:
        self.calls.append(text)
        if not self.answers:
            raise AssertionError(f"unexpected extra prompt: {text!r}")
        return self.answers.pop(0)


@pytest.fixture
def prompt(monkeypatch):
    """Install a scripted prompt; the factory returns the recorder."""

    def install(*answers: str) -> PromptScript:
        script = PromptScript(*answers)
        monkeypatch.setattr(click, "prompt", script)
        return script

    return install


@pytest.mark.unit
def test_auto_approve_does_not_prompt(cleanup, prompt):
    script = prompt()  # any call raises

    assert (
        cleanup._confirm_deletion("S3 Bucket", "b", "IDP-dead", STACK_INFO, True)
        is True
    )
    assert script.calls == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "answer,expected", [("y", True), ("yes", True), ("Y", True), (" y ", True)]
)
def test_an_affirmative_answer_confirms(cleanup, prompt, answer, expected):
    prompt(answer)
    assert (
        cleanup._confirm_deletion("S3 Bucket", "b", "IDP-dead", STACK_INFO, False)
        is expected
    )


@pytest.mark.unit
@pytest.mark.parametrize("answer", ["n", "no", "N", " n "])
def test_a_negative_answer_declines(cleanup, prompt, answer):
    prompt(answer)
    assert (
        cleanup._confirm_deletion("S3 Bucket", "b", "IDP-dead", STACK_INFO, False)
        is False
    )


@pytest.mark.unit
@pytest.mark.parametrize("answer", ["a", "all", "yes to all"])
def test_yes_to_all_is_recorded_per_resource_type(cleanup, prompt, answer):
    """ "Yes to all" must be scoped to the resource type it was answered for.

    A global flag would carry an approval for log groups over to S3 buckets,
    which is the difference between deleting logs and deleting documents.
    """
    prompt(answer)

    assert (
        cleanup._confirm_deletion(
            "S3 Bucket", "b", "IDP-dead", STACK_INFO, False, remaining_count=4
        )
        is True
    )
    assert cleanup._yes_to_all == {"S3 Bucket": True}
    assert cleanup._no_to_all == {}


@pytest.mark.unit
@pytest.mark.parametrize("answer", ["s", "skip", "skip all", "no to all"])
def test_skip_all_is_recorded_per_resource_type(cleanup, prompt, answer):
    prompt(answer)

    assert (
        cleanup._confirm_deletion("DynamoDB Table", "t", "IDP-dead", STACK_INFO, False)
        is False
    )
    assert cleanup._no_to_all == {"DynamoDB Table": True}
    assert cleanup._yes_to_all == {}


@pytest.mark.unit
def test_a_standing_yes_to_all_skips_the_prompt(cleanup, prompt):
    script = prompt()
    cleanup._yes_to_all["S3 Bucket"] = True

    assert (
        cleanup._confirm_deletion("S3 Bucket", "b", "IDP-dead", STACK_INFO, False)
        is True
    )
    assert script.calls == []


@pytest.mark.unit
def test_a_standing_skip_all_skips_the_prompt(cleanup, prompt):
    script = prompt()
    cleanup._no_to_all["S3 Bucket"] = True

    assert (
        cleanup._confirm_deletion("S3 Bucket", "b", "IDP-dead", STACK_INFO, False)
        is False
    )
    assert script.calls == []


@pytest.mark.unit
def test_an_unrecognised_answer_re_prompts_rather_than_deleting(cleanup, prompt):
    """An answer the parser does not understand must never mean yes.

    The loop has no default branch that falls through, and that is the property
    worth pinning: a typo at a destructive prompt re-asks. It also must not mean
    *no* silently, which would make a mistyped approval look like a decline.
    """
    script = prompt("maybe", "", "?", "y")

    assert (
        cleanup._confirm_deletion("S3 Bucket", "b", "IDP-dead", STACK_INFO, False)
        is True
    )
    assert len(script.calls) == 4


# ---------------------------------------------------------------------------
# _delete_resources_concurrently
# ---------------------------------------------------------------------------


class DeleteRecorder:
    """A deletion function that records its arguments and can be made to fail."""

    def __init__(self, fail_for: Tuple[str, ...] = ()):
        self.calls: List[Tuple[str, str]] = []
        self.fail_for = set(fail_for)

    def __call__(self, resource_id: str, stack_name: str) -> Tuple[bool, str]:
        self.calls.append((resource_id, stack_name))
        if resource_id in self.fail_for:
            return (False, f"Failed to delete {resource_id}: boom")
        return (True, f"{resource_id} (stack: {stack_name})")


def resources(*ids: str) -> List[Tuple[str, str, Dict[str, str]]]:
    return [(rid, "IDP-dead", STACK_INFO) for rid in ids]


@pytest.mark.unit
def test_no_resources_means_no_work_and_no_prompts(cleanup, prompt):
    prompt()
    recorder = DeleteRecorder()

    assert cleanup._delete_resources_concurrently("S3 Bucket", [], recorder) == {
        "deleted": [],
        "skipped": [],
        "errors": [],
    }
    assert recorder.calls == []


@pytest.mark.unit
def test_dry_run_reports_without_deleting_or_prompting(cleanup, prompt):
    """A dry run must not reach the deletion function at all.

    This is the flag a user sets to find out what *would* happen, so a single
    real call here is a data-loss bug. Asserting the recorder saw nothing is the
    whole point of the test; the report strings are secondary.
    """
    prompt()
    recorder = DeleteRecorder()

    results = cleanup._delete_resources_concurrently(
        "S3 Bucket", resources("b1", "b2"), recorder, dry_run=True
    )

    assert recorder.calls == []
    assert results["deleted"] == [
        "b1 (stack: IDP-dead) [DRY RUN]",
        "b2 (stack: IDP-dead) [DRY RUN]",
    ]
    assert results["skipped"] == [] and results["errors"] == []


@pytest.mark.unit
def test_dry_run_wins_over_auto_approve(cleanup, prompt):
    """``dry_run`` is tested first, so ``--dry-run --yes`` still deletes nothing.

    Both flags are plausible on one command line and the combination is the one
    where an ordering mistake is unrecoverable.
    """
    prompt()
    recorder = DeleteRecorder()

    results = cleanup._delete_resources_concurrently(
        "S3 Bucket", resources("b1"), recorder, dry_run=True, auto_approve=True
    )

    assert recorder.calls == []
    assert results["deleted"] == ["b1 (stack: IDP-dead) [DRY RUN]"]


@pytest.mark.unit
def test_auto_approve_deletes_every_resource_exactly_once(cleanup, prompt):
    prompt()
    recorder = DeleteRecorder()

    results = cleanup._delete_resources_concurrently(
        "S3 Bucket", resources("b1", "b2", "b3"), recorder, auto_approve=True
    )

    assert sorted(recorder.calls) == [
        ("b1", "IDP-dead"),
        ("b2", "IDP-dead"),
        ("b3", "IDP-dead"),
    ]
    assert sorted(results["deleted"]) == [
        "b1 (stack: IDP-dead)",
        "b2 (stack: IDP-dead)",
        "b3 (stack: IDP-dead)",
    ]
    assert results["errors"] == []


@pytest.mark.unit
def test_a_failing_deletion_is_reported_as_an_error_not_a_deletion(cleanup, prompt):
    """A deletion that returns ``False`` must not be counted as done.

    The caller prints the ``deleted`` list as an accomplished fact, so a failure
    landing there tells the operator a resource is gone when it is still billing.
    """
    prompt()
    recorder = DeleteRecorder(fail_for=("b2",))

    results = cleanup._delete_resources_concurrently(
        "S3 Bucket", resources("b1", "b2", "b3"), recorder, auto_approve=True
    )

    assert sorted(results["deleted"]) == [
        "b1 (stack: IDP-dead)",
        "b3 (stack: IDP-dead)",
    ]
    assert results["errors"] == ["Failed to delete b2: boom"]


@pytest.mark.unit
def test_worker_count_never_exceeds_the_number_of_resources(
    cleanup, prompt, monkeypatch
):
    """The pool is sized ``min(self._max_workers, count)``.

    Fifty threads for two buckets is harmless but the clamp is the only thing
    keeping a large account's run from opening ``_max_workers`` connections for
    a single deletion, so the argument is asserted directly.
    """
    prompt()
    seen: List[int] = []

    class RecordingPool(ThreadPoolExecutor):
        def __init__(self, max_workers=None, **kwargs):
            seen.append(max_workers)
            super().__init__(max_workers=max_workers, **kwargs)

    monkeypatch.setattr(module_under_test, "ThreadPoolExecutor", RecordingPool)

    cleanup._max_workers = 50
    cleanup._delete_resources_concurrently(
        "S3 Bucket", resources("b1", "b2"), DeleteRecorder(), auto_approve=True
    )
    assert seen == [2]

    cleanup._max_workers = 1
    cleanup._delete_resources_concurrently(
        "DynamoDB Table",
        resources("t1", "t2", "t3"),
        DeleteRecorder(),
        auto_approve=True,
    )
    assert seen == [2, 1]


@pytest.mark.unit
def test_interactive_approval_deletes_one_at_a_time_in_order(cleanup, prompt):
    """Answering ``y`` per resource takes the sequential path, not the pool.

    Order is asserted because the sequential branch is the one a user watches:
    the prompt names a resource and the deletion that follows must be that
    resource.
    """
    script = prompt("y", "y", "y")
    recorder = DeleteRecorder()

    results = cleanup._delete_resources_concurrently(
        "S3 Bucket", resources("b1", "b2", "b3"), recorder
    )

    assert recorder.calls == [
        ("b1", "IDP-dead"),
        ("b2", "IDP-dead"),
        ("b3", "IDP-dead"),
    ]
    assert len(script.calls) == 3
    assert results["deleted"] == [
        "b1 (stack: IDP-dead)",
        "b2 (stack: IDP-dead)",
        "b3 (stack: IDP-dead)",
    ]


@pytest.mark.unit
def test_declining_one_resource_still_offers_the_next(cleanup, prompt):
    prompt("n", "y")
    recorder = DeleteRecorder()

    results = cleanup._delete_resources_concurrently(
        "S3 Bucket", resources("b1", "b2"), recorder
    )

    assert recorder.calls == [("b2", "IDP-dead")]
    assert results["skipped"] == ["b1 (stack: IDP-dead - user declined)"]
    assert results["deleted"] == ["b2 (stack: IDP-dead)"]


@pytest.mark.unit
def test_yes_to_all_queues_the_current_and_every_later_resource(cleanup, prompt):
    """One ``a`` must cover the resource being asked about *and* the rest.

    Dropping the current resource from the queue is the easy off-by-one here,
    and it would leave exactly one orphan behind per run with no error reported.
    """
    script = prompt("a")
    recorder = DeleteRecorder()

    results = cleanup._delete_resources_concurrently(
        "S3 Bucket", resources("b1", "b2", "b3"), recorder
    )

    assert len(script.calls) == 1
    assert sorted(recorder.calls) == [
        ("b1", "IDP-dead"),
        ("b2", "IDP-dead"),
        ("b3", "IDP-dead"),
    ]
    assert len(results["deleted"]) == 3
    assert cleanup._yes_to_all["S3 Bucket"] is True


@pytest.mark.unit
def test_skip_all_skips_the_current_resource_too(cleanup, prompt):
    """One ``s`` must skip the resource being asked about as well as the rest.

    The symmetric off-by-one: including the current resource in the skip list is
    what makes ``s`` mean "not this one either". If it deleted the current one
    first, ``s`` would destroy the resource the user was looking at.
    """
    script = prompt("s")
    recorder = DeleteRecorder()

    results = cleanup._delete_resources_concurrently(
        "S3 Bucket", resources("b1", "b2", "b3"), recorder
    )

    assert len(script.calls) == 1
    assert recorder.calls == []
    assert results["skipped"] == [
        "b1 (stack: IDP-dead - user declined)",
        "b2 (stack: IDP-dead - user declined)",
        "b3 (stack: IDP-dead - user declined)",
    ]
    assert results["deleted"] == []


@pytest.mark.unit
def test_a_failing_deletion_on_the_interactive_path_is_also_an_error(cleanup, prompt):
    """The sequential branch has its own success/failure split from the pool's.

    Approving resources one at a time calls ``delete_fn`` inline rather than
    through the executor, so the branch that files the message under ``errors``
    is a different line from the concurrent one. A failure landing under
    ``deleted`` here would mislead the operator who is watching each prompt.
    """
    prompt("y", "y")
    recorder = DeleteRecorder(fail_for=("b1",))

    results = cleanup._delete_resources_concurrently(
        "S3 Bucket", resources("b1", "b2"), recorder
    )

    assert results["errors"] == ["Failed to delete b1: boom"]
    assert results["deleted"] == ["b2 (stack: IDP-dead)"]


@pytest.mark.unit
@pytest.mark.parametrize("answer,expected", [("a", True), ("s", False)])
def test_a_blanket_answer_works_with_no_remaining_count_supplied(
    cleanup, prompt, answer, expected
):
    """``remaining_count`` defaults to 0, and the wording branches on it.

    ``cleanup_cloudfront_distributions`` and the other non-batched sweeps call
    ``_confirm_deletion`` without a count, so the zero path is the one those take.
    The decision must be identical either way; only the message differs.
    """
    prompt(answer)

    assert (
        cleanup._confirm_deletion(
            "CloudFront distribution", "E123", "IDP-dead", STACK_INFO, False
        )
        is expected
    )
    assert (
        cleanup._yes_to_all.get("CloudFront distribution") is True
        if expected
        else cleanup._no_to_all.get("CloudFront distribution") is True
    )


@pytest.mark.unit
def test_a_standing_yes_to_all_bypasses_the_prompt_entirely(cleanup, prompt):
    """A ``yes to all`` recorded earlier in the run is honoured with no prompt.

    ``cleanup_s3_buckets`` and ``cleanup_dynamodb_tables`` both route through
    this helper, and the flag is keyed by resource type, so an approval given
    for buckets must not be reused for tables. Both halves are asserted.
    """
    script = prompt()
    cleanup._yes_to_all["S3 Bucket"] = True
    recorder = DeleteRecorder()

    results = cleanup._delete_resources_concurrently(
        "S3 Bucket", resources("b1", "b2"), recorder
    )

    assert script.calls == []
    assert len(results["deleted"]) == 2

    other = DeleteRecorder()
    script = prompt("n")
    cleanup._delete_resources_concurrently("DynamoDB Table", resources("t1"), other)
    assert other.calls == []
    assert len(script.calls) == 1
