# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`ConfigOperation.sync_bda(direction="cleanup_orphaned")` — the SDK's cleanup branch.

Before #1207 the method had no such branch: every direction fell through to
`BdaBlueprintService.create_blueprints_from_custom_configuration`, so asking for
`cleanup_orphaned` ran a *sync* under that name. The cleanup existed only inside the
`syncBdaIdp` resolver, which the SDK never invokes.

The assertion that carries the fix is therefore negative as well as positive: the
cleanup collaborator is called **and** the sync collaborator is not. A test that only
checked `cleanup_orphaned_blueprints` was called would pass over an implementation that
ran both, which would re-create the blueprints it had just deleted.

The counts are deliberately reported on `cleanup_deleted_count` /
`cleanup_failed_count` rather than on `classes_synced` / `classes_failed`: the cleanup
processes no classes, and a blueprint reported as a synced class is a wrong answer
rather than an imprecise one. Those defaults are asserted for the same reason the
orphan tests beside this file assert theirs.
"""

from unittest.mock import MagicMock, patch

import pytest

from idp_sdk.operations.config import ConfigOperation

PROJECT_ARN = "arn:aws:bedrock:us-west-2:123456789012:data-automation-project/p1"
ORPHAN = "arn:aws:bedrock:us-west-2:123456789012:blueprint/idp-Receipt-aaaa"


@pytest.fixture
def ops():
    """A `ConfigOperation` whose stack lookup and environment bridging are stubbed."""
    client = MagicMock()
    client._region = "us-west-2"
    client._require_stack.return_value = "test-stack"
    operation = ConfigOperation(client)
    with patch.object(ConfigOperation, "_configure_config_env", return_value="tbl"):
        yield operation


def _doubles(*, cleanup, orphans=()):
    """Patch the two collaborators `sync_bda` imports at call time."""
    service = MagicMock()
    service.cleanup_orphaned_blueprints.return_value = cleanup
    service.orphaned_blueprint_arns = list(orphans)

    manager = MagicMock()
    manager.get_bda_project_arn.return_value = PROJECT_ARN

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


def _clean(deleted=2, failed=0, message="Deleted 2 orphaned blueprints"):
    """A cleanup return in the shape `cleanup_orphaned_blueprints` documents."""
    return {
        "success": failed == 0,
        "message": message,
        "deleted_count": deleted,
        "failed_count": failed,
        "details": [],
    }


@pytest.mark.unit
class TestTheCleanupBranchExists:
    def test_the_cleanup_runs_and_the_sync_does_not(self, ops):
        """#1207: the direction must reach the cleanup instead of the sync.

        Running both would delete the orphans and then re-create blueprints from the
        configuration, so the negative half is not redundant.
        """
        service, _manager, p_service, p_manager = _doubles(cleanup=_clean())
        with p_service, p_manager:
            result = ops.sync_bda(direction="cleanup_orphaned", config_version="v1")

        service.cleanup_orphaned_blueprints.assert_called_once_with(version="v1")
        service.create_blueprints_from_custom_configuration.assert_not_called()
        assert result.success is True
        assert result.direction == "cleanup_orphaned"

    def test_a_sync_direction_still_reaches_the_sync(self, ops):
        """Non-vacuity for the test above: the branch is keyed on the direction."""
        service = MagicMock()
        service.create_blueprints_from_custom_configuration.return_value = [
            {"class": "Invoice", "status": "success"}
        ]
        service.orphaned_blueprint_arns = []
        manager = MagicMock()
        manager.get_bda_project_arn.return_value = PROJECT_ARN
        with (
            patch.multiple(
                "idp_common.bda.bda_blueprint_service",
                BdaBlueprintService=MagicMock(return_value=service),
            ),
            patch(
                "idp_common.config.configuration_manager.ConfigurationManager",
                return_value=manager,
            ),
        ):
            result = ops.sync_bda(direction="idp_to_bda", config_version="v1")

        service.create_blueprints_from_custom_configuration.assert_called_once()
        service.cleanup_orphaned_blueprints.assert_not_called()
        assert result.classes_synced == 1

    def test_an_unresolvable_profile_is_refused_before_anything_is_deleted(self, ops):
        """The safety property of the whole branch, and it is not obvious.

        The cleanup decides what is an orphan by building the expected
        blueprint-name prefixes from the named profile's classes.
        `ConfigurationManager.get_configuration("Config", version=None)` reads the
        *bare* `Config` key, which holds nothing on a normal stack, so an unresolved
        version yields an empty expected set — and then every blueprint carrying the
        stack's prefix matches nothing and is deleted. "No classes to keep" and
        "could not find out which classes to keep" are indistinguishable by the time
        the service sees them, and their safe actions are opposite.

        This state is ordinary rather than exotic: a stack nobody has activated a
        configuration on, with no `--config-profile` given.

        The assertion is that the collaborator was never called. A message saying it
        refused would pass over an implementation that deleted first.
        """
        service, manager, p_service, p_manager = _doubles(cleanup=_clean())
        manager.list_config_versions.return_value = [
            {"versionName": "lending", "isActive": False}
        ]
        with p_service, p_manager:
            result = ops.sync_bda(direction="cleanup_orphaned")

        service.cleanup_orphaned_blueprints.assert_not_called()
        assert result.success is False
        assert "needs a configuration profile" in (result.error or "")
        assert result.cleanup_deleted_count == 0

    def test_an_active_profile_is_resolved_and_the_cleanup_runs(self, ops):
        """Non-vacuity for the refusal above: it is the *absence* that refuses."""
        service, manager, p_service, p_manager = _doubles(cleanup=_clean())
        manager.list_config_versions.return_value = [
            {"versionName": "lending", "isActive": False},
            {"versionName": "claims", "isActive": True},
        ]
        with p_service, p_manager:
            result = ops.sync_bda(direction="cleanup_orphaned")

        service.cleanup_orphaned_blueprints.assert_called_once_with(version="claims")
        assert result.success is True

    def test_the_profile_the_caller_named_is_the_one_passed_through(self, ops):
        """The profile decides which blueprints survive, so it must not be re-resolved.

        `config_profile` is the current spelling of the same argument.
        """
        service, _manager, p_service, p_manager = _doubles(cleanup=_clean())
        with p_service, p_manager:
            ops.sync_bda(direction="cleanup_orphaned", config_profile="v7")

        service.cleanup_orphaned_blueprints.assert_called_once_with(version="v7")


@pytest.mark.unit
class TestTheCountsAreReportedAsBlueprints:
    def test_a_clean_cleanup_reports_its_deletions(self, ops, caplog):
        service, _manager, p_service, p_manager = _doubles(cleanup=_clean(deleted=5))
        with p_service, p_manager, caplog.at_level("ERROR"):
            result = ops.sync_bda(direction="cleanup_orphaned", config_version="v1")

        # Non-vacuity for the log assertion below: the guard really is a guard.
        assert "did not complete" not in caplog.text
        assert result.cleanup_deleted_count == 5
        assert result.cleanup_failed_count == 0
        # Nothing is reported as a class, in either direction.
        assert result.classes_synced == 0
        assert result.classes_failed == 0
        assert result.processed_classes == []
        assert result.error is None

    def test_a_cleanup_that_could_not_delete_everything_is_not_a_success(
        self, ops, caplog
    ):
        service, _manager, p_service, p_manager = _doubles(
            cleanup=_clean(deleted=1, failed=2, message="Deleted 1, 2 failed"),
            orphans=[ORPHAN],
        )
        with p_service, p_manager, caplog.at_level("ERROR"):
            result = ops.sync_bda(direction="cleanup_orphaned", config_version="v1")

        # The log line is asserted because an unbiased mutation of the `if not
        # succeeded:` guard around it survived every other assertion here. This is
        # an account-wide destructive operation, so a partial failure that leaves no
        # trace in the logs is how it goes unnoticed on a Lambda caller with no
        # console.
        assert "did not complete" in caplog.text
        assert "2 failed" in caplog.text
        assert result.success is False
        assert result.cleanup_deleted_count == 1
        assert result.cleanup_failed_count == 2
        assert result.error == "Deleted 1, 2 failed"
        # Still orphaned after the cleanup ran, so still reported.
        assert result.orphaned_blueprint_arns == [ORPHAN]

    def test_a_cleanup_the_service_reports_failed_is_not_a_success(self, ops):
        """`success: False` with a zero failure count must not read as clean.

        The service returns that shape when the listing or the configuration read
        raised before any delete was attempted.
        """
        service, _manager, p_service, p_manager = _doubles(
            cleanup={
                "success": False,
                "message": "Error during cleanup: AccessDenied",
                "deleted_count": 0,
                "failed_count": 0,
                "details": [],
            }
        )
        with p_service, p_manager:
            result = ops.sync_bda(direction="cleanup_orphaned", config_version="v1")

        assert result.success is False
        assert result.error == "Error during cleanup: AccessDenied"

    def test_a_sync_leaves_the_cleanup_counts_unset(self, ops):
        """`None`, not 0: a sync deleted no orphans *and* did not look for any.

        Reporting 0 would say the cleanup ran and found nothing.
        """
        service = MagicMock()
        service.create_blueprints_from_custom_configuration.return_value = [
            {"class": "Invoice", "status": "success"}
        ]
        service.orphaned_blueprint_arns = []
        manager = MagicMock()
        manager.get_bda_project_arn.return_value = PROJECT_ARN
        with (
            patch.multiple(
                "idp_common.bda.bda_blueprint_service",
                BdaBlueprintService=MagicMock(return_value=service),
            ),
            patch(
                "idp_common.config.configuration_manager.ConfigurationManager",
                return_value=manager,
            ),
        ):
            result = ops.sync_bda(direction="idp_to_bda", config_version="v1")

        assert result.cleanup_deleted_count is None
        assert result.cleanup_failed_count is None

    def test_a_cleanup_that_raises_is_reported_as_a_failure(self, ops):
        """The outer handler already covers this; asserted so the branch cannot
        acquire its own swallow."""
        service, _manager, p_service, p_manager = _doubles(cleanup=_clean())
        service.cleanup_orphaned_blueprints.side_effect = RuntimeError("boom")
        with p_service, p_manager:
            result = ops.sync_bda(direction="cleanup_orphaned", config_version="v1")

        assert result.success is False
        assert "boom" in (result.error or "")
