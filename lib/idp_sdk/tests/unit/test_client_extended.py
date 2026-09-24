# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`IDPClient._get_stack_resources` and the `FailureAnalysis` cascade count.

`_get_stack_resources` is the one piece of `client.py` with behaviour rather than
assignment: it resolves a CloudFormation stack's resources through `StackInfo`,
refuses a stack that is not in an operable state, and memoises the answer — but
**only** when the caller did not name a stack explicitly. That conditional cache
is the interesting part. Every stack-bearing operation in the SDK goes through
this method, and the per-call `stack_name` override exists so that one client can
address several stacks; a cache that ignored the override would hand the second
stack's operations the first stack's buckets, queue and tracking table. Nothing
would raise. Documents would simply be written to another deployment.

The tests build real CloudFormation stacks in `moto`, so the resources come back
as physical ids from a real (fake) stack rather than from a patched dictionary,
and the cache is demonstrated the only way that actually proves a cache exists:
by **deleting the stack** between two calls and showing the second one still
answers. A `MagicMock` call count would prove the same thing only about the mock.

`models/stack.py`'s `FailureAnalysis.cascade_count` is here because it is the one
computed property in that module, and what it counts is easy to get backwards.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from idp_sdk import IDPClient
from idp_sdk.exceptions import IDPConfigurationError, IDPStackError
from idp_sdk.models.stack import FailureAnalysis, FailureCause


def _template(input_bucket: str) -> dict:
    """A stack shaped like the parts `StackInfo` reads.

    `DocumentQueue` is not optional: `StackInfo.get_resources` resolves the queue
    URL from the stack's resources and raises without it, so a template missing it
    would make every test here fail for the wrong reason.
    """
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Resources": {
            "DocumentQueue": {"Type": "AWS::SQS::Queue"},
            "TrackingTable": {
                "Type": "AWS::DynamoDB::Table",
                "Properties": {
                    "KeySchema": [{"AttributeName": "PK", "KeyType": "HASH"}],
                    "AttributeDefinitions": [
                        {"AttributeName": "PK", "AttributeType": "S"}
                    ],
                    "BillingMode": "PAY_PER_REQUEST",
                },
            },
        },
        "Outputs": {
            "S3InputBucketName": {"Value": input_bucket},
            "S3OutputBucketName": {"Value": f"{input_bucket}-output"},
        },
    }


def _create_stack(name: str, input_bucket: str, region: str) -> None:
    boto3.client("cloudformation", region_name=region).create_stack(
        StackName=name, TemplateBody=json.dumps(_template(input_bucket))
    )


@pytest.mark.unit
class TestGetStackResources:
    @mock_aws
    def test_resources_are_resolved_from_the_live_stack(self, aws_credentials):
        """Outputs become friendly names; resources become physical ids.

        The queue URL and the table name are read from the stack's *resources*
        rather than its outputs, so both halves of `StackInfo`'s mapping are
        covered by this one assertion.
        """
        _create_stack("idp-a", "bucket-a", aws_credentials)

        resources = IDPClient(
            stack_name="idp-a", region=aws_credentials
        )._get_stack_resources()

        assert resources["InputBucket"] == "bucket-a"
        assert resources["OutputBucket"] == "bucket-a-output"
        assert resources["DocumentQueueUrl"].startswith("https://sqs.")
        assert resources["DocumentsTable"].startswith("idp-a-TrackingTable")
        assert resources["SettingsParameter"] == "idp-a-Settings"

    @mock_aws
    def test_an_output_the_stack_does_not_publish_comes_back_empty(
        self, aws_credentials
    ):
        """Absent outputs are `""`, not missing keys.

        Callers index this dictionary directly (`resources.get("TestSetBucket")`
        and friends), and a `KeyError` from a pattern that simply does not create
        a given bucket would be a crash rather than a check.
        """
        _create_stack("idp-a", "bucket-a", aws_credentials)

        resources = IDPClient(
            stack_name="idp-a", region=aws_credentials
        )._get_stack_resources()

        assert resources["TestSetBucket"] == ""
        assert resources["ConfigurationBucket"] == ""
        assert resources["StateMachineArn"] == ""

    @mock_aws
    def test_the_default_stack_is_resolved_once_and_then_cached(self, aws_credentials):
        """Proven by deleting the stack between the two calls.

        After the delete, `describe_stacks` raises — so a second resolution would
        fail. It does not, which is the only direct evidence that the second call
        never went to CloudFormation. The identity check on top of that shows the
        same dictionary object is handed back rather than an equal copy.
        """
        _create_stack("idp-a", "bucket-a", aws_credentials)
        client = IDPClient(stack_name="idp-a", region=aws_credentials)

        first = client._get_stack_resources()
        boto3.client("cloudformation", region_name=aws_credentials).delete_stack(
            StackName="idp-a"
        )
        second = client._get_stack_resources()

        assert second is first
        assert second["InputBucket"] == "bucket-a"

    @mock_aws
    def test_an_explicit_stack_name_is_not_served_from_the_cache(self, aws_credentials):
        """The override is why one client can address several deployments.

        This is the failure the whole cache condition exists to prevent: with the
        default stack already cached, an operation called with
        `stack_name="idp-b"` must see B's resources. If the cache answered, the
        caller's documents would go to A's input bucket with nothing raised.
        """
        _create_stack("idp-a", "bucket-a", aws_credentials)
        _create_stack("idp-b", "bucket-b", aws_credentials)
        client = IDPClient(stack_name="idp-a", region=aws_credentials)

        assert client._get_stack_resources()["InputBucket"] == "bucket-a"
        assert client._get_stack_resources("idp-b")["InputBucket"] == "bucket-b"
        assert client._get_stack_resources()["InputBucket"] == "bucket-a"

    @mock_aws
    def test_an_explicit_stack_name_does_not_populate_the_cache(self, aws_credentials):
        """A one-off override must not become the client's remembered answer.

        Caching it would mean the *next* default-stack call returned the overridden
        stack's resources — the same cross-deployment mix-up, arriving one call
        later and harder to attribute.
        """
        _create_stack("idp-a", "bucket-a", aws_credentials)
        _create_stack("idp-b", "bucket-b", aws_credentials)
        client = IDPClient(stack_name="idp-a", region=aws_credentials)

        client._get_stack_resources("idp-b")

        assert client._resources_cache is None
        assert client._get_stack_resources()["InputBucket"] == "bucket-a"

    @mock_aws
    def test_reassigning_the_stack_name_makes_the_next_call_re_resolve(
        self, aws_credentials
    ):
        """The setter clears the cache, and the effect is a different answer.

        Asserting only that `_resources_cache` became `None` would not show that
        the subsequent read reaches the new stack, which is the behaviour a caller
        relies on after switching stacks.
        """
        _create_stack("idp-a", "bucket-a", aws_credentials)
        _create_stack("idp-b", "bucket-b", aws_credentials)
        client = IDPClient(stack_name="idp-a", region=aws_credentials)

        assert client._get_stack_resources()["InputBucket"] == "bucket-a"
        client.stack_name = "idp-b"

        assert client._get_stack_resources()["InputBucket"] == "bucket-b"

    @mock_aws
    def test_a_stack_that_does_not_exist_is_refused_by_name(self, aws_credentials):
        """`StackInfo.validate_stack` swallows the CloudFormation error.

        It returns `False` rather than raising, so the only thing a caller would
        otherwise see is a falsy return; naming the stack in `IDPStackError` is
        what makes a typo diagnosable.
        """
        with pytest.raises(IDPStackError, match="'idp-absent'"):
            IDPClient(
                stack_name="idp-absent", region=aws_credentials
            )._get_stack_resources()

    @mock_aws
    def test_a_deleted_stack_is_refused_and_not_cached(self, aws_credentials):
        """A stack deleted since the client was built.

        The refusal has to leave the cache empty; caching a failure would make the
        client permanently unusable against a stack that is being redeployed.
        """
        _create_stack("idp-a", "bucket-a", aws_credentials)
        boto3.client("cloudformation", region_name=aws_credentials).delete_stack(
            StackName="idp-a"
        )
        client = IDPClient(stack_name="idp-a", region=aws_credentials)

        with pytest.raises(IDPStackError):
            client._get_stack_resources()

        assert client._resources_cache is None

    @mock_aws
    def test_a_stack_in_a_transitional_state_is_refused(self, aws_credentials):
        """Only CREATE/UPDATE/UPDATE_ROLLBACK complete are operable.

        `StackInfo.validate_stack` is patched to return `False` because `moto`
        will not produce a `ROLLBACK_COMPLETE` or `UPDATE_IN_PROGRESS` stack — the
        real states this guard exists for. What is being checked here is the
        client's half of the contract: a `False` verdict becomes `IDPStackError`
        naming the stack, rather than resources being read from a stack whose
        buckets may not exist yet.
        """
        from idp_sdk._core.stack_info import StackInfo

        _create_stack("idp-a", "bucket-a", aws_credentials)
        client = IDPClient(stack_name="idp-a", region=aws_credentials)

        with patch.object(StackInfo, "validate_stack", return_value=False):
            with pytest.raises(IDPStackError, match="not in a valid state"):
                client._get_stack_resources()

    def test_resolving_resources_with_no_stack_at_all_is_a_configuration_error(self):
        """The stack requirement is checked before any AWS call is attempted.

        So a caller who forgot `stack_name` gets a message telling them what to
        pass, rather than a `NoRegionError` or a CloudFormation `ValidationError`
        about a stack named `None`.
        """
        with pytest.raises(IDPConfigurationError, match="stack_name is required"):
            IDPClient()._get_stack_resources()


@pytest.mark.unit
class TestClientSurface:
    def test_every_documented_namespace_is_present(self):
        """Including the two the older test's list omits.

        `publish` and `chat` are constructed in `__init__` alongside the rest, and
        an import error in either would break every `IDPClient()` — so they are
        worth naming here rather than being covered only by whichever test happens
        to use them.
        """
        client = IDPClient()

        for name in (
            "stack",
            "batch",
            "document",
            "config",
            "discovery",
            "manifest",
            "testing",
            "search",
            "evaluation",
            "assessment",
            "publish",
            "chat",
        ):
            assert getattr(client, name) is not None, name

    def test_each_namespace_holds_a_back_reference_to_the_client(self):
        """Operations read `self._client._region` and `self._client._stack_name`.

        A namespace constructed with anything other than the client itself would
        resolve stacks and regions from the wrong place, which is the shape of a
        whole class of cross-account and cross-region mistakes.
        """
        client = IDPClient(stack_name="s", region="eu-west-1")

        assert client.config._client is client
        assert client.discovery._client is client
        assert client.manifest._client is client


@pytest.mark.unit
class TestFailureAnalysisCascadeCount:
    """`cascade_count` counts *all* failures flagged as cascades."""

    def test_cascades_are_counted_across_every_failure_not_just_the_root_causes(self):
        """The distinction is what makes a deployment failure readable.

        One resource fails and every enclosing nested stack reports a failure of
        its own, so a real `UPDATE_ROLLBACK` carries dozens of events and one
        actionable cause. `root_causes` is the short list to read; `cascade_count`
        is how many of the rest were consequences. Counting cascades within
        `root_causes` instead would report 0 for every deployment, since a root
        cause is by definition not a cascade.
        """
        root = FailureCause(
            resource="OCRFunction",
            reason="Resource handler returned message: invalid image",
            status="CREATE_FAILED",
            stack="idp-pattern",
        )
        cascades = [
            FailureCause(
                resource="PATTERNSTACK",
                reason="Embedded stack failed",
                status="CREATE_FAILED",
                stack="idp",
                is_cascade=True,
            ),
            FailureCause(
                resource="idp",
                reason="The following resource(s) failed",
                status="ROLLBACK_IN_PROGRESS",
                stack="idp",
                is_cascade=True,
            ),
        ]

        analysis = FailureAnalysis(
            stack_name="idp",
            root_causes=[root],
            all_failures=[root, *cascades],
        )

        assert analysis.cascade_count == 2
        assert len(analysis.root_causes) == 1

    def test_a_single_isolated_failure_has_no_cascades(self):
        analysis = FailureAnalysis(
            stack_name="idp",
            root_causes=[],
            all_failures=[
                FailureCause(
                    resource="Bucket",
                    reason="already exists",
                    status="CREATE_FAILED",
                    stack="idp",
                )
            ],
        )

        assert analysis.cascade_count == 0

    def test_an_analysis_with_no_failures_counts_zero(self):
        """The default-constructed case: a deployment that did not fail."""
        assert FailureAnalysis(stack_name="idp").cascade_count == 0
