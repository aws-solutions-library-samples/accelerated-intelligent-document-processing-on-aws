# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What `idp-cli delete-documents` tells a caller when it deleted nothing.

`idp_common.delete_documents`' two selectors return `List[str]`, so `[]` is the only
thing a failure could have been reported as — and `[]` is also the ordinary answer for
"that batch holds no documents". They therefore raise instead (#1187), and the reason
raising is the right refusal is a property of the *callers*: each turns the raise into
something a caller can act on, while each turns `[]` into a reported success.

This file pins the CLI half of that. The SDK half is pinned in
`lib/idp_sdk/tests/unit/test_batch_delete_selector_failure.py`, and the argument is the
same on both sides: the conversion is the justification for the library refusing rather
than reporting, so it is asserted where it happens rather than read off the source.

⚠️ **The change that would put the false success back is a swallow at the *call site*,
not a narrower outer `except`, and the exit code alone does not separate them.** Both
mutations were executed against a throttled scan:

| Mutation | Exit | Cause in output | "No documents found" |
|---|---|---|---|
| none (as shipped) | 1 | yes | no |
| swallow at the selector call site (`except Exception: doc_list = []`) | **0** | no | **yes** |
| narrow this command's outer `except Exception` to `ValueError` | 1 | **no** | no |

So the first is the one that restores the pre-change false success, and the second — the
more obvious-looking mutation — does not: the `ClientError` escapes uncaught, which is
still a non-zero exit. That is why each test here asserts the cause is *named* and not
only that the exit code is non-zero. The assertion on the output is what catches both
rows; the exit-code assertion catches only the first. Asserting the exit code alone would
pass a command that fails with a bare traceback and tells the operator nothing.

Three outcomes, which must stay distinguishable by exit code alone, since that is what a
shell script wrapping the command reads:

| Situation | Exit | Output |
|---|---|---|
| no selector given | 1 | refused by this layer, before any table is read |
| selector matched nothing | 0 | "No documents found for batch: …" |
| scan throttled or rejected | 1 | the cause, not a claim that there was nothing |

No AWS call is made and no credential is used: the SDK client, `boto3.resource` and
`boto3.client` are patched, and the DynamoDB fault is the class botocore's own error
factory produces for a code the bundled service model declares for `Scan`.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from click.testing import CliRunner

from idp_cli.cli import cli

RESOURCES = SimpleNamespace(
    input_bucket="in-bucket",
    output_bucket="out-bucket",
    documents_table="docs-table",
)


def _scan_fault(code: str = "ThrottlingException") -> ClientError:
    """The exception boto3 raises for ``code`` on a ``Scan``, from the service model.

    The code is checked against what the model declares for the operation, so a
    hand-written string cannot drift into standing for a fault DynamoDB never answers
    with. Built through `botocore.session` rather than `boto3.client`, because this
    module patches `boto3.client`.
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


def _invoke(table: MagicMock, *args: str):
    """Run `delete-documents` against ``table``, with nothing reaching AWS."""
    sdk_client = MagicMock()
    sdk_client.stack.get_resources.return_value = RESOURCES
    dynamodb = MagicMock()
    dynamodb.Table.return_value = table
    s3 = MagicMock()

    with (
        patch("idp_sdk.IDPClient", return_value=sdk_client),
        patch("boto3.resource", return_value=dynamodb),
        patch("boto3.client", return_value=s3),
    ):
        result = CliRunner().invoke(
            cli, ["delete-documents", "--stack-name", "s", "--force", *args]
        )
    return result, s3


@pytest.mark.unit
class TestDeleteDocumentsReportsASelectorFailure:
    def test_a_throttled_scan_exits_1_instead_of_reporting_nothing_to_delete(self):
        """The case the old handler reported as "No documents found" with exit 0."""
        table = MagicMock()
        table.scan.side_effect = _scan_fault()

        result, s3 = _invoke(table, "--batch-id", "batch-2")

        assert result.exit_code == 1
        assert "No documents found" not in result.output
        assert "ThrottlingException" in result.output
        assert s3.delete_object.call_count == 0

    def test_a_batch_that_holds_nothing_still_exits_0(self):
        """The other direction, which is why `[]` cannot also mean "it went wrong"."""
        table = MagicMock()
        table.scan.return_value = {"Items": [{"ObjectKey": "batch-1/a.pdf"}]}

        result, s3 = _invoke(table, "--batch-id", "batch-9")

        assert result.exit_code == 0
        assert "No documents found for batch: batch-9" in result.output
        assert s3.delete_object.call_count == 0

    @pytest.mark.parametrize("selector", ["--batch-id", "--pattern"])
    def test_an_empty_selector_is_refused_by_this_layer_before_anything_is_read(
        self, selector
    ):
        """The library guard is the second line here, not the first.

        This layer counts selectors with a truth test, so an empty one is refused
        before a table is opened — which is why the defect in #1187 was reachable only
        by a direct library caller, and why the refusal in `idp_common` changes nothing
        for anyone going through the CLI.
        """
        table = MagicMock()

        result, _s3 = _invoke(table, selector, "")

        assert result.exit_code == 1
        assert "Must specify one of" in result.output
        assert table.scan.call_count == 0
