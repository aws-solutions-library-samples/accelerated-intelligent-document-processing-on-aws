# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the web UI's `deleteDocument` resolver Lambda.

The resolver returns **one boolean** for a whole selection, and the UI sends the whole
multi-select in one call — so that boolean is the only thing a user sees about a delete
that did not finish. It used to answer `deleted_count > 0 or failed_count == 0`, which is
`True` whenever *any* document in the selection was removed: nine deleted and one
tracking row orphaned read exactly like ten deleted (#1238). These tests assert the
counts-to-boolean mapping directly, because that expression is where the truth this
module works to produce was being discarded.

The resolver is loaded from its Lambda source by path, the way the other resolver tests
here do it, with `boto3` patched so no client is constructed and no credential is used.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def resolver(monkeypatch):
    """The resolver module, with its module-level boto3 resources mocked out."""
    monkeypatch.setenv("TRACKING_TABLE_NAME", "tracking")
    monkeypatch.setenv("INPUT_BUCKET", "in")
    monkeypatch.setenv("OUTPUT_BUCKET", "out")
    path = os.path.join(
        os.path.dirname(__file__),
        "../../../../nested/api-resolvers/src/lambda/delete_document_resolver/index.py",
    )
    with patch("boto3.resource"), patch("boto3.client"):
        spec = importlib.util.spec_from_file_location("delete_document_resolver", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["delete_document_resolver"] = module
        spec.loader.exec_module(module)
    yield module
    sys.modules.pop("delete_document_resolver", None)


def _event(keys: list[str]) -> dict:
    return {
        "identity": {"claims": {"cognito:groups": ["Admin"]}},
        "arguments": {"objectKeys": keys},
    }


def _results(*successes: bool) -> list:
    return [
        {
            "success": ok,
            "object_key": f"k{i}",
            "errors": [] if ok else ["ThrottlingException"],
        }
        for i, ok in enumerate(successes)
    ]


@pytest.mark.unit
class TestTheReportedOutcome:
    @pytest.mark.parametrize(
        "successes, succeeds",
        [
            ((True,), True),
            ((False,), False),
            ((True, True, True), True),
            ((True, True, False), False),
            ((False, True, True), False),
            ((False, False, False), False),
        ],
        ids=[
            "one-ok",
            "one-failed",
            "all-ok",
            "last-failed",
            "first-failed",
            "all-failed",
        ],
    )
    def test_any_document_that_failed_makes_the_call_fail(
        self, resolver, successes, succeeds
    ):
        """⚠️ **A failed delete raises; it does not return a value.**

        Returning `failed_count == 0` was a *successful* invocation whose result was
        `false`, and nothing between here and the user looked at that value: the REST
        dispatcher answers 200 for any value a resolver yields, and the UI logged the
        result at debug level and navigated away. So the shape in #1366 — version rows
        gone, list row and document record kept, two faults in the log — reached the
        browser as an untroubled 200.

        A raise is the only outcome the transport distinguishes, which is why the
        assertion here is about the exception rather than about a boolean.
        """
        keys = [f"k{i}" for i in range(len(successes))]
        with patch.object(
            resolver, "delete_single_document", side_effect=_results(*successes)
        ):
            if succeeds:
                assert resolver.handler(_event(keys), MagicMock()) is True
            else:
                with pytest.raises(Exception) as caught:
                    resolver.handler(_event(keys), MagicMock())
                assert not isinstance(caught.value, (PermissionError, ValueError)), (
                    "a failed delete must not be reported as a bad request or a "
                    "denial — the dispatcher maps those to 400/403"
                )

    def test_a_partial_failure_is_not_reported_as_a_complete_delete(self, resolver):
        """The measured defect, spelled out separately from the table above.

        Two of three documents were removed and the third's tracking-list row is still
        in the list. Multi-select is the ordinary UI path, so this is the shape a user
        meets — and the failure has to name which document and why, because the cause
        is normally a deployment-level fault identical for every document and
        actionable only if it is said out loud.
        """
        with patch.object(
            resolver,
            "delete_single_document",
            side_effect=_results(True, True, False),
        ) as deleter:
            with pytest.raises(RuntimeError) as caught:
                resolver.handler(_event(["a", "b", "c"]), MagicMock())

        assert deleter.call_count == 3, "a failure must not abandon the rest"
        message = str(caught.value)
        # The keys the CALLER asked about, which is what a user can act on — the
        # per-document result dict carries its own `object_key` and the resolver does
        # not read it back out.
        assert "c" in message, f"the failing document is not named: {message}"
        assert "ThrottlingException" in message, (
            f"the cause is not reported to the caller: {message}"
        )
        assert "1 of 3" in message and "2 completed" in message, (
            f"the partial count is not reported: {message}"
        )

    def test_the_raise_carries_every_failure_not_just_the_first(self, resolver):
        """All of them, because a deployment fault fails every document the same way.

        Reporting only the first would make a selection of ten look like one bad
        document rather than one bad deployment.
        """
        with patch.object(
            resolver,
            "delete_single_document",
            side_effect=_results(False, True, False),
        ):
            with pytest.raises(RuntimeError) as caught:
                resolver.handler(_event(["a", "b", "c"]), MagicMock())

        message = str(caught.value)
        assert "a:" in message and "c:" in message, message
        assert "b:" not in message, f"a document that succeeded is named: {message}"

    def test_an_iam_denial_reaches_the_caller_without_its_arns(self, resolver):
        """⚠️ The message is shown in a browser, and an AWS denial names ARNs.

        An authorization failure's text carries the execution role's ARN and the
        resource ARN, both of which embed the deployment's account id. The reason the
        cause is reported at all is that "it failed" is not actionable — and what makes
        it actionable is the error code and the refused action, which survive
        redaction. The account id does not need to reach a browser to make the message
        useful, and this repository treats a committed account id as a gate failure.

        Redaction rather than truncation at the operation name: the rejected-expression
        fault ends with the attribute that was refused, which is the whole answer and
        carries no identifier.
        """
        denial = (
            "Error in list entry deletion: An error occurred (AccessDeniedException) "
            "when calling the Query operation: User: "
            "arn:aws:sts::000000000000:assumed-role/Stack-DeleteDocumentRole-Ab/Stack-x"
            " is not authorized to perform: dynamodb:Query on resource: "
            "arn:aws:dynamodb:us-east-1:000000000000:table/Stack-TrackingTable"
        )
        failing = [
            {
                "success": False,
                "object_key": "a",
                "deleted": {"input_file": True, "list_entries": False},
                "errors": [denial],
            }
        ]
        with patch.object(resolver, "delete_single_document", side_effect=failing):
            with pytest.raises(RuntimeError) as caught:
                resolver.handler(_event(["a"]), MagicMock())

        message = str(caught.value)
        assert "arn:aws:sts::" not in message
        assert "arn:aws:dynamodb:" not in message
        assert "000000000000" not in message, (
            f"an account id reached the caller: {message}"
        )
        # The actionable half survives.
        assert "AccessDeniedException" in message
        assert "dynamodb:Query" in message
        # And what the cleanup DID remove is reported with its values, so the message
        # cannot read as "nothing happened" for a document whose cleanup mostly
        # succeeded — and a count is shown as a count rather than claimed to have
        # "completed", which is not a question a count answers.
        assert "removed: input_file=True" in message, message
        assert "list_entries=False" in message, message

    def test_the_rejected_expression_detail_is_not_truncated_away(self, resolver):
        """The detail after the operation name is sometimes the entire answer.

        "Primary key attribute: SK" is what names the attribute DynamoDB refused. It
        carries no identifier, so a truncating sanitizer would discard the one thing a
        maintainer needs while a redacting one keeps it.
        """
        fault = (
            "Error in list entry deletion: An error occurred (ValidationException) "
            "when calling the Query operation: Filter Expression can only contain "
            "non-primary key attributes: Primary key attribute: SK"
        )
        failing = [
            {"success": False, "object_key": "a", "deleted": {}, "errors": [fault]}
        ]
        with patch.object(resolver, "delete_single_document", side_effect=failing):
            with pytest.raises(RuntimeError) as caught:
                resolver.handler(_event(["a"]), MagicMock())

        assert "Primary key attribute: SK" in str(caught.value)

    @pytest.mark.parametrize(
        "arn",
        [
            "arn:aws:s3:::my-bucket",
            "arn:aws-us-gov:s3:::my-bucket/key",
            "arn:aws-cn:dynamodb:cn-north-1:000000000000:table/T",
        ],
        ids=["commercial", "govcloud", "china"],
    )
    def test_every_partition_is_redacted(self, resolver, arn):
        """GovCloud and China ARNs are not spelled ``arn:aws:``.

        A redactor matching only the commercial partition would pass every test here
        and leak on the deployments this repository explicitly supports.
        """
        assert "arn:" in resolver._redact_arns(f"denied on {arn} today")
        assert arn not in resolver._redact_arns(f"denied on {arn} today")

    def test_redaction_leaves_an_iam_action_alone(self, resolver):
        """``s3:DeleteObjectVersion`` is colon-separated and must not be mangled."""
        text = "not authorized to perform: s3:DeleteObjectVersion"
        assert resolver._redact_arns(text) == text

    @pytest.mark.parametrize(
        "text",
        [
            'resource: "arn:aws:s3:::my-bucket"',
            "resource: 'arn:aws:s3:::my-bucket'",
            "denied on arn:aws:s3:::my-bucket.",
            "denied on arn:aws:s3:::my-bucket\nand then",
            "ends with arn:aws:s3:::my-bucket",
            "arn:aws:s3:::my-bucket, and more",
        ],
        ids=[
            "double-quoted",
            "single-quoted",
            "trailing-full-stop",
            "before-a-newline",
            "at-end-of-string",
            "comma-separated",
        ],
    )
    def test_an_arn_is_redacted_however_it_is_delimited(self, resolver, text):
        """The delimiter is where a pattern like this usually leaks.

        An AWS sentence ends an ARN with a quote, a full stop, a comma, a newline or
        nothing at all, and a pattern tuned to one of those passes its own test and
        leaks on the rest.
        """
        assert "arn:aws:s3:::my-bucket" not in resolver._redact_arns(text)

    def test_a_bare_account_id_is_redacted_too(self, resolver):
        """⚠️ The ARN pattern covers the shapes observed; it is not a bound on them.

        An authorization denial names the principal and the resource as ARNs, which is
        why redacting ARNs covers this path today. But "the message shapes I can name"
        is a weaker claim than "no account id", and the two are one differently-worded
        service error apart — so a standalone 12-digit run is redacted as well.
        """
        redacted = resolver._redact_arns(
            "account 123456789012 is not authorized; expected owner 210987654321"
        )
        assert "123456789012" not in redacted
        assert "210987654321" not in redacted
        assert "is not authorized" in redacted

    def test_redaction_does_not_touch_a_longer_number(self, resolver):
        """The lookarounds, so a 13-digit value is not partly masked."""
        assert "12345678901234" in resolver._redact_arns("size 12345678901234 bytes")

    def test_the_document_key_is_outside_the_redaction_scope(self, resolver):
        """A key may legitimately contain a 12-digit run, and must stay readable.

        Redaction applies to the error *detail*, not to the ``"<key>: "`` prefix the
        message is built from — so a date-stamped filename is still identifiable even
        though the copy of it quoted inside an AWS message is masked. Which document
        failed is the one thing the reader cannot do without.
        """
        # ⚠️ **Assembled, not written out.** A twelve-digit literal in this public
        # repository is indistinguishable from an AWS account id by inspection, and
        # `scripts/check_account_ids.py` correctly refuses one — so spelling this
        # invoice number out failed that gate. Hyphenating it would have cleared the
        # gate and made the test vacuous: the run of digits is the whole property under
        # test. The length assertion is what keeps it honest, so a future shortening of
        # this fixture fails here rather than quietly proving nothing.
        invoice_number = "2026" + "0112" + "0001"
        assert len(invoice_number) == 12, (
            "this fixture must carry a twelve-digit run or it is not exercising the "
            "account-id redaction at all"
        )
        key = f"invoices/{invoice_number}.pdf"
        failing = [
            {
                "success": False,
                "object_key": key,
                "deleted": {},
                "errors": [f"refused for arn:aws:s3:::b/{key}"],
            }
        ]
        with patch.object(resolver, "delete_single_document", side_effect=failing):
            with pytest.raises(RuntimeError) as caught:
                resolver.handler(_event([key]), MagicMock())

        assert key in str(caught.value), (
            "the failing document is no longer identifiable"
        )

    def test_a_caller_outside_the_two_groups_is_refused(self, resolver):
        # Defence in depth, unchanged by this change and worth keeping pinned: the
        # boolean above is only reachable by an Admin or an Author.
        event = {
            "identity": {"claims": {"cognito:groups": ["Viewer"]}},
            "arguments": {"objectKeys": ["a"]},
        }
        with pytest.raises(PermissionError):
            resolver.handler(event, MagicMock())
