# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What ``client.batch.delete_documents()`` tells a caller when it deleted nothing.

`idp_common.delete_documents`' two selectors return `List[str]`, so `[]` is the only
thing a failure could have been reported as — and `[]` is also the ordinary answer for
"that batch holds no documents". They therefore raise instead (#1187), and the reason
raising is the right answer is a property of *this* layer: the SDK turns the raise into
an `IDPProcessingError` a caller can act on, while it turns `[]` into
`BatchDeletionResult(success=True, deleted_count=0)`.

That conversion is what `docs/idp-sdk.md` promises and it is the justification for the
library refusing rather than reporting, so it is asserted here rather than left to be
read off the source. A narrower `except` added to `batch.delete_documents()` later would
restore the false success without any test in `idp_common` going red.

No AWS call is made: `boto3.resource` and `boto3.client` are patched, and the DynamoDB
fault is the class botocore's own error factory produces for a code the bundled service
model declares for `Scan`.
"""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from idp_sdk import IDPClient
from idp_sdk.exceptions import IDPConfigurationError, IDPProcessingError

STACK_RESOURCES = {
    "InputBucket": "in-bucket",
    "OutputBucket": "out-bucket",
    "DocumentsTable": "docs-table",
}


def _scan_fault(code: str = "ThrottlingException") -> ClientError:
    """The exception boto3 raises for ``code`` on a ``Scan``, from the service model.

    The code is checked against what the model declares for the operation, so a
    hand-written string cannot drift into standing for a fault DynamoDB never answers
    with. No credential is used and no call is made: botocore builds the client and its
    exception factory from the bundled model alone.
    """
    import botocore.session

    client = botocore.session.get_session().create_client(
        "dynamodb",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    declared = {
        shape.name
        for shape in client.meta.service_model.operation_model("Scan").error_shapes
    }
    assert code in declared, f"{code} is not a modelled Scan fault: {sorted(declared)}"
    return client.exceptions.from_code(code)(
        {"Error": {"Code": code, "Message": f"{code} from the service"}}, "Scan"
    )


def _client_with(table: MagicMock):
    """An `IDPClient` whose DynamoDB table is ``table`` and whose S3 client is a mock."""
    client = IDPClient(stack_name="test-stack")
    resource = MagicMock()
    resource.Table.return_value = table
    s3 = MagicMock()
    return (
        client,
        s3,
        patch.object(IDPClient, "_get_stack_resources", return_value=STACK_RESOURCES),
        patch("boto3.resource", return_value=resource),
        patch("boto3.client", return_value=s3),
    )


@pytest.mark.unit
@pytest.mark.batch
class TestBatchDeleteReportsASelectorFailure:
    def test_a_throttled_scan_raises_instead_of_reporting_a_successful_no_op(self):
        """The case the old handler reported as `success=True, deleted_count=0`."""
        table = MagicMock()
        table.scan.side_effect = _scan_fault()
        client, s3, resources, resource, s3_patch = _client_with(table)

        with resources, resource, s3_patch:
            with pytest.raises(IDPProcessingError) as raised:
                client.batch.delete_documents(batch_id="batch-2")

        cause = raised.value.__cause__
        assert isinstance(cause, ClientError)
        assert cause.response["Error"]["Code"] == "ThrottlingException"
        assert s3.delete_object.call_count == 0

    def test_a_batch_that_holds_nothing_still_reports_success(self):
        """The other direction, which is why `[]` cannot also mean "it went wrong"."""
        table = MagicMock()
        table.scan.return_value = {"Items": [{"ObjectKey": "batch-1/a.pdf"}]}
        client, s3, resources, resource, s3_patch = _client_with(table)

        with resources, resource, s3_patch:
            result = client.batch.delete_documents(batch_id="batch-9")

        assert result.success is True
        assert (result.deleted_count, result.total_count) == (0, 0)
        assert s3.delete_object.call_count == 0

    def test_a_missing_selector_is_refused_by_this_layer_before_anything_is_read(self):
        """The library guard is the second line here, not the first.

        This layer refuses a falsy selector on its own, which is why the defect in
        #1187 was reachable only by a direct library caller — and why the refusal in
        `idp_common` changes nothing for anyone calling through the SDK.
        """
        table = MagicMock()
        client, _s3, resources, resource, s3_patch = _client_with(table)

        with resources, resource, s3_patch:
            with pytest.raises(IDPConfigurationError):
                client.batch.delete_documents(batch_id=None)

        assert table.scan.call_count == 0
