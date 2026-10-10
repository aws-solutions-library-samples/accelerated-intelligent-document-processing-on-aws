# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A blueprint the sync could not delete has to reach the user, not just CloudWatch.

A replace-mode sync rewrites the BDA project's blueprint list *before* deleting the
blueprints it removed, because BDA refuses to delete a blueprint a project still
associates. A delete that then fails leaves a blueprint that is already out of the
project — invisible to every project-scoped read — and still in the account, where it
counts against the blueprint limit and can still be matched by name prefix. Only the
account-wide cleanup will find it.

The sync's per-class status list cannot carry that: the orphan belongs to no class, and
every consumer of that list *counts* it into "classes synced" and "classes failed", so
an entry for a non-class would misreport a class as unsynced. It arrives on
`BdaBlueprintService.orphaned_blueprint_arns` instead, and this resolver's job is to put
it somewhere a user looks.

That place is `message`, in every outcome branch, and the literal word `WARNING` in it
is load-bearing rather than decorative: the UI leaves a sync message on screen instead
of auto-dismissing it after five seconds exactly when the message contains that word.
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

PROJECT_ARN = "arn:aws:bedrock:us-east-1:123456789012:data-automation-project/p1"
ORPHAN_ONE = "arn:aws:bedrock:us-east-1:123456789012:blueprint/idp-Receipt-aaaa"
ORPHAN_TWO = "arn:aws:bedrock:us-east-1:123456789012:blueprint/idp-Payslip-bbbb"


def _load_index():
    spec = importlib.util.spec_from_file_location(
        "sync_bda_idp_resolver_index", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_bda_idp_resolver_index"] = module
    spec.loader.exec_module(module)
    return module


index = _load_index()


def _event(direction="idp_to_bda"):
    """An Admin caller with an explicit project ARN.

    Admin skips the config-version scope lookup, and naming the ARN skips both the
    tracking-table read and the auto-create branch, so the only collaborator left to
    double is the blueprint service itself.
    """
    return {
        "info": {"fieldName": "syncBdaIdp"},
        "arguments": {
            "versionName": "default",
            "direction": direction,
            "syncMode": "replace",
            "bdaProjectArn": PROJECT_ARN,
            "saveArn": False,
        },
        "identity": {"claims": {"cognito:groups": ["Admin"], "email": "a@example.com"}},
    }


@pytest.fixture
def bda_service(monkeypatch):
    """Double the blueprint service; `orphaned_blueprint_arns` is what is varied.

    ``CONFIGURATION_TABLE_NAME`` is cleared so the resolver builds no
    ``ConfigurationManager`` and touches no DynamoDB.
    """
    monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)

    def _configure(*, statuses, orphans, sync_raises=None):
        service = MagicMock()
        if sync_raises is not None:
            service.create_blueprints_from_custom_configuration.side_effect = (
                sync_raises
            )
        else:
            service.create_blueprints_from_custom_configuration.return_value = statuses
        service.orphaned_blueprint_arns = orphans
        return patch.object(index, "BdaBlueprintService", return_value=service)

    return _configure


@pytest.mark.unit
def test_an_orphan_is_named_in_the_message_of_a_fully_successful_sync(bda_service):
    """The case that was silent: every class synced, so nothing else says anything."""
    with bda_service(
        statuses=[{"class": "Invoice", "status": "success"}], orphans=[ORPHAN_ONE]
    ):
        result = index.handler(_event(), None)

    assert result["success"] is True
    assert "WARNING" in result["message"]
    assert ORPHAN_ONE in result["message"]
    assert "cleanup" in result["message"].lower()
    # Not reported as a class outcome: the class did sync.
    assert result["processedClasses"] == ["Invoice"]
    assert result["bdaSyncStatus"] == "synced"


@pytest.mark.unit
def test_a_clean_sync_says_nothing_about_orphans(bda_service):
    """Non-vacuity for the assertions above: the same fixture with an empty list must
    produce a message with no warning in it at all."""
    with bda_service(statuses=[{"class": "Invoice", "status": "success"}], orphans=[]):
        result = index.handler(_event(), None)

    assert result["success"] is True
    assert "WARNING" not in result["message"]
    assert "orphan" not in result["message"].lower()


@pytest.mark.unit
def test_every_orphan_is_named_and_counted(bda_service):
    with bda_service(
        statuses=[{"class": "Invoice", "status": "success"}],
        orphans=[ORPHAN_ONE, ORPHAN_TWO],
    ):
        result = index.handler(_event(), None)

    assert "2 blueprint(s)" in result["message"]
    assert ORPHAN_ONE in result["message"] and ORPHAN_TWO in result["message"]


@pytest.mark.unit
def test_an_orphan_survives_a_partial_class_failure(bda_service):
    """A class failure already fills `message`, and appending is easy to get wrong in
    exactly the branch where two problems coincide."""
    with bda_service(
        statuses=[
            {"class": "Invoice", "status": "success"},
            {"class": "Receipt", "status": "failed", "error": "ValidationException"},
        ],
        orphans=[ORPHAN_ONE],
    ):
        result = index.handler(_event(), None)

    assert result["bdaSyncStatus"] == "partial"
    assert "Failed to sync 1 classes" in result["message"]
    assert ORPHAN_ONE in result["message"]


@pytest.mark.unit
def test_an_orphan_survives_a_total_class_failure(bda_service):
    """And it has to be on `error.message`, not only on `message`.

    The web UI's failure path reads `response.error.message || response.message` and
    renders the first of the two, and this branch always sets `error.message` — so text
    placed only on `message` here is present in the response and invisible to the user.
    The duplication across the two fields is deliberate for that reason: `message` stays
    the complete record.
    """
    with bda_service(
        statuses=[{"class": "Invoice", "status": "failed", "error": "nope"}],
        orphans=[ORPHAN_ONE],
    ):
        result = index.handler(_event(), None)

    assert result["success"] is False
    assert ORPHAN_ONE in result["error"]["message"]
    assert "WARNING" in result["error"]["message"]
    assert ORPHAN_ONE in result["message"]


@pytest.mark.unit
def test_a_total_class_failure_with_no_orphan_leaves_the_error_message_alone(
    bda_service,
):
    """Non-vacuity, and a guard on the one thing that branch's comment asks for: the
    failure *reasons* stay off `error.message` so a UI rendering both fields does not
    print them twice."""
    with bda_service(
        statuses=[{"class": "Invoice", "status": "failed", "error": "nope"}],
        orphans=[],
    ):
        result = index.handler(_event(), None)

    assert result["error"]["message"] == "Failed to sync classes: Invoice"


@pytest.mark.unit
def test_a_sync_that_raised_still_reports_what_it_orphaned(bda_service):
    """The deletes run before the last two steps of a sync, both of which can raise, so
    a sync that threw may still have taken a blueprint out of the project. The handler
    of last resort therefore asks the service rather than assuming there is nothing to
    report."""
    with bda_service(
        statuses=[], orphans=[ORPHAN_ONE], sync_raises=RuntimeError("boom")
    ):
        result = index.handler(_event(), None)

    assert result["success"] is False
    assert "boom" in result["error"]["message"]
    assert "cleanup_orphaned" in result["error"]["message"]
    assert "1 blueprint(s)" in result["error"]["message"]


@pytest.mark.unit
def test_a_sync_that_raised_with_nothing_orphaned_says_nothing_extra(bda_service):
    """Non-vacuity for the test above."""
    with bda_service(statuses=[], orphans=[], sync_raises=RuntimeError("boom")):
        result = index.handler(_event(), None)

    assert result["error"]["message"] == "Sync operation failed: boom."


@pytest.mark.unit
def test_a_long_orphan_list_is_counted_in_full_and_named_in_part(bda_service):
    """A sync that orphaned dozens produced one unreadable multi-kilobyte alert. The
    count and the remedy are what the reader acts on; the log line keeps every ARN."""
    many = [
        f"arn:aws:bedrock:us-east-1:123456789012:blueprint/idp-C{i}-a"
        for i in range(25)
    ]
    with bda_service(
        statuses=[{"class": "Invoice", "status": "success"}], orphans=many
    ):
        result = index.handler(_event(), None)

    message = result["message"]
    assert "25 blueprint(s)" in message
    assert f"and {25 - index.ORPHAN_ARNS_IN_MESSAGE} more" in message
    assert sum(arn in message for arn in many) == index.ORPHAN_ARNS_IN_MESSAGE


@pytest.mark.unit
def test_the_skipped_property_warning_and_the_orphan_warning_coexist(bda_service):
    """Both are appended to the same string; one used to be the only occupant."""
    with bda_service(
        statuses=[
            {
                "class": "Invoice",
                "status": "success",
                "warnings": [
                    {
                        "class": "Invoice",
                        "property": "party",
                        "type": "nested_object",
                        "message": "dropped",
                    }
                ],
            }
        ],
        orphans=[ORPHAN_ONE],
    ):
        result = index.handler(_event(), None)

    assert "Skipped: Invoice: party" in result["message"]
    assert ORPHAN_ONE in result["message"]
    assert result["warnings"][0]["property"] == "party"
