# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A blueprint the BDA sync could not delete has to reach the SDK's caller.

`BdaBlueprintService._synchronize_deletes` takes a blueprint out of the BDA project
before deleting it — BDA refuses to delete one a project still associates — so a delete
that fails leaves a blueprint no project-scoped read can see, still counted against the
account's blueprint limit, removable only by the account-wide cleanup. Both SDK entry
points that run a sync used to discard that list, so the only trace was a CloudWatch log
line from inside the library.

It cannot be folded into the per-class status list, because both of these operations
*count* that list into `classes_synced` / `classes_failed`: an entry for something that
is not a document class would report a class as unsynced when every class synced. So the
count assertions below are as load-bearing as the ARN assertions — they are what says
the new report did not corrupt the old one.
"""

from unittest.mock import MagicMock, patch

import pytest

from idp_sdk.operations.config import ConfigOperation

PROJECT_ARN = "arn:aws:bedrock:us-west-2:123456789012:data-automation-project/p1"
ORPHAN = "arn:aws:bedrock:us-west-2:123456789012:blueprint/idp-Receipt-aaaa"


@pytest.fixture
def ops():
    """A `ConfigOperation` whose stack lookup and environment bridging are stubbed.

    `_configure_config_env` reads CloudFormation; nothing below needs it, and leaving
    it live would make these tests require credentials.
    """
    client = MagicMock()
    client._region = "us-west-2"
    client._require_stack.return_value = "test-stack"
    operation = ConfigOperation(client)
    with patch.object(ConfigOperation, "_configure_config_env", return_value="tbl"):
        yield operation


def _doubles(*, statuses, orphans, use_bda=True):
    """Patch the two collaborators `sync_bda` and `activate` import at call time."""
    service = MagicMock()
    service.create_blueprints_from_custom_configuration.return_value = statuses
    service.orphaned_blueprint_arns = orphans

    manager = MagicMock()
    manager.get_bda_project_arn.return_value = PROJECT_ARN
    config = MagicMock()
    config.use_bda = use_bda
    manager.get_configuration.return_value = config

    return (
        service,
        manager,
        patch.multiple(
            "idp_common.bda.bda_blueprint_service",
            BdaBlueprintService=MagicMock(return_value=service),
        ),
        patch(
            "idp_common.config.configuration_manager.ConfigurationManager",
            return_value=manager,
        ),
    )


@pytest.mark.unit
class TestSyncBdaReportsOrphans:
    def test_an_undeletable_blueprint_is_reported_without_failing_a_class(self, ops):
        _service, _manager, p_service, p_manager = _doubles(
            statuses=[{"class": "Invoice", "status": "success"}], orphans=[ORPHAN]
        )
        with p_service, p_manager:
            result = ops.sync_bda(config_version="v1", mode="replace")

        assert result.orphaned_blueprint_arns == [ORPHAN]
        # The class synced, so the counts must not move and `success` must stay True.
        assert (result.classes_synced, result.classes_failed) == (1, 0)
        assert result.success is True
        assert result.error is None

    def test_a_clean_sync_reports_an_empty_list(self, ops):
        """Non-vacuity for the assertion above."""
        _service, _manager, p_service, p_manager = _doubles(
            statuses=[{"class": "Invoice", "status": "success"}], orphans=[]
        )
        with p_service, p_manager:
            result = ops.sync_bda(config_version="v1", mode="replace")

        assert result.orphaned_blueprint_arns == []

    def test_a_sync_that_raised_still_reports_what_it_orphaned(self, ops):
        """The deletes run before the last two steps of a sync, and both of those can
        raise — a project rewrite and a DynamoDB write. So "the sync threw" does not
        mean "nothing was removed from the project", and reading the orphans off the
        service is what makes the difference: on this path the local the normal return
        uses was never assigned."""
        service, _manager, p_service, p_manager = _doubles(
            statuses=[], orphans=[ORPHAN]
        )
        service.create_blueprints_from_custom_configuration.side_effect = RuntimeError(
            "boom"
        )
        with p_service, p_manager:
            result = ops.sync_bda(config_version="v1", mode="replace")

        assert result.success is False
        assert result.error == "boom"
        assert result.orphaned_blueprint_arns == [ORPHAN]

    def test_orphans_are_reported_alongside_a_class_failure(self, ops):
        _service, _manager, p_service, p_manager = _doubles(
            statuses=[
                {"class": "Invoice", "status": "success"},
                {"class": "Receipt", "status": "failed", "error": "nope"},
            ],
            orphans=[ORPHAN],
        )
        with p_service, p_manager:
            result = ops.sync_bda(config_version="v1", mode="replace")

        assert result.orphaned_blueprint_arns == [ORPHAN]
        assert (result.classes_synced, result.classes_failed) == (1, 1)


@pytest.mark.unit
class TestActivateReportsOrphans:
    """`config activate` runs the same sync for a BDA-mode profile, and reported
    `success=True` with no sign that a blueprint had been left behind."""

    def test_an_undeletable_blueprint_is_reported_on_a_successful_activation(self, ops):
        _service, manager, p_service, p_manager = _doubles(
            statuses=[{"class": "Invoice", "status": "success"}], orphans=[ORPHAN]
        )
        with p_service, p_manager:
            result = ops.activate(config_version="v1")

        assert result.success is True
        assert result.bda_synced is True
        assert result.bda_orphaned_blueprint_arns == [ORPHAN]
        assert (result.bda_classes_synced, result.bda_classes_failed) == (1, 0)
        manager.activate_version.assert_called_once_with("v1")

    def test_an_orphan_is_reported_when_every_class_failed(self, ops):
        """The path that aborts the activation. It is the outcome most likely to have
        left a blueprint behind — the deletes run whatever happened to the classes —
        and the one where nothing else in the result mentions one, so an orphan list
        computed and then not passed to the result is exactly as invisible as the
        CloudWatch line this change exists to replace."""
        _service, manager, p_service, p_manager = _doubles(
            statuses=[{"class": "Invoice", "status": "failed", "error": "nope"}],
            orphans=[ORPHAN],
        )
        with p_service, p_manager:
            result = ops.activate(config_version="v1")

        assert result.success is False
        assert result.bda_classes_failed == 1
        assert result.bda_orphaned_blueprint_arns == [ORPHAN]
        manager.activate_version.assert_not_called()

    def test_an_orphan_is_reported_when_the_sync_raised(self, ops):
        """Same reasoning as the sync_bda case: the orphans have to be read off the
        service, because the local the success path uses is assigned after the sync
        call returns and this path never got there."""
        service, _manager, p_service, p_manager = _doubles(
            statuses=[], orphans=[ORPHAN]
        )
        service.create_blueprints_from_custom_configuration.side_effect = RuntimeError(
            "boom"
        )
        with p_service, p_manager:
            result = ops.activate(config_version="v1")

        assert result.success is False
        assert "boom" in (result.error or "")
        assert result.bda_orphaned_blueprint_arns == [ORPHAN]

    def test_an_orphan_is_reported_when_the_activation_write_itself_failed(self, ops):
        """The path the BDA-scoped handler does not cover.

        `manager.activate_version()` runs *after* the sync and *outside* the BDA
        try/except, so a throttle or a denial on that write lands in the function's
        outermost handler — with a completed sync behind it that may have left a
        blueprint orphaned. That handler reported neither the orphans nor the class
        counts, which is the same CloudWatch-only outcome as before the change.
        """
        _service, manager, p_service, p_manager = _doubles(
            statuses=[{"class": "Invoice", "status": "success"}], orphans=[ORPHAN]
        )
        manager.activate_version.side_effect = RuntimeError("throttled")
        with p_service, p_manager:
            result = ops.activate(config_version="v1")

        assert result.success is False
        assert result.error == "throttled"
        assert result.bda_orphaned_blueprint_arns == [ORPHAN]
        # And the counts survive too: 0 synced after a sync that synced one is a third
        # wrong answer, not a harmless omission.
        assert (result.bda_classes_synced, result.bda_classes_failed) == (1, 0)

    def test_a_failure_before_the_sync_reports_no_orphans_and_does_not_raise(self, ops):
        """The other side of binding those locals before the `try`.

        A profile lookup that throws happens before any of the BDA locals would have
        been assigned, so the outermost handler naming them has to be safe — a
        `NameError` from inside an exception handler replaces the error the caller
        needs with one about the handler.
        """
        _service, manager, p_service, p_manager = _doubles(statuses=[], orphans=[])
        manager.get_configuration.side_effect = RuntimeError("no such profile")
        with p_service, p_manager:
            result = ops.activate(config_version="v1")

        assert result.success is False
        assert result.error == "no such profile"
        assert result.bda_orphaned_blueprint_arns == []
        assert (result.bda_classes_synced, result.bda_classes_failed) == (0, 0)

    def test_a_clean_activation_reports_an_empty_list(self, ops):
        _service, _manager, p_service, p_manager = _doubles(
            statuses=[{"class": "Invoice", "status": "success"}], orphans=[]
        )
        with p_service, p_manager:
            result = ops.activate(config_version="v1")

        assert result.bda_orphaned_blueprint_arns == []

    def test_a_non_bda_profile_reports_no_orphans_and_runs_no_sync(self, ops):
        service, _manager, p_service, p_manager = _doubles(
            statuses=[], orphans=[ORPHAN], use_bda=False
        )
        with p_service, p_manager:
            result = ops.activate(config_version="v1")

        assert result.bda_synced is False
        assert result.bda_orphaned_blueprint_arns == []
        service.create_blueprints_from_custom_configuration.assert_not_called()
