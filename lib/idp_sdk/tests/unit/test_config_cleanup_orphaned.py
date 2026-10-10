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

from idp_sdk.operations.config import ConfigOperation, _failed_cleanup_arns

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
    """Patch the two collaborators `sync_bda` imports at call time.

    `manager.get_configuration` returns a truthy object by default, because the
    cleanup branch now refuses a profile that does not exist and every test in the
    two classes below is about a profile that does. `TestTheProfileMustExist` drives
    the real collaborators instead and does not use this.
    """
    service = MagicMock()
    service.cleanup_orphaned_blueprints.return_value = cleanup
    # `[]` for every cleanup: this attribute is written only by
    # `_synchronize_deletes`, inside `create_blueprints_from_custom_configuration`,
    # which the cleanup path never calls. Pinned at the real value so that reading it
    # for the orphan list -- which reported "0 orphans" beside a non-zero failure
    # count -- cannot come back.
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


def _clean(
    deleted=2, failed=0, message="Deleted 2 orphaned blueprints", failed_arns=()
):
    """A cleanup return in the shape `cleanup_orphaned_blueprints` really builds.

    One `details` entry per blueprint attempted, each with `name`, `arn` and a
    `status` of `"deleted"` or `"failed"` -- copied from the service rather than
    invented, because the SDK now reads the failed ARNs out of this list.
    """
    details = [
        {
            "name": f"idp-Deleted-{index}",
            "arn": f"{ORPHAN}-ok-{index}",
            "status": "deleted",
        }
        for index in range(deleted)
    ]
    arns = list(failed_arns) or [f"{ORPHAN}-bad-{index}" for index in range(failed)]
    details += [
        {"name": f"idp-Failed-{index}", "arn": arn, "status": "failed"}
        for index, arn in enumerate(arns)
    ]
    return {
        "success": failed == 0,
        "message": message,
        "deleted_count": deleted,
        "failed_count": failed,
        "details": details,
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
        # And nothing is reported as still orphaned when every delete succeeded.
        assert result.orphaned_blueprint_arns == []
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
            cleanup=_clean(
                deleted=1,
                failed=2,
                message="Deleted 1, 2 failed",
                failed_arns=[ORPHAN, f"{ORPHAN}-two"],
            ),
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
        # Still orphaned after the cleanup ran, so still named -- the ARN is the only
        # way to find one, since no project-scoped read will ever show it again.
        # Read off the cleanup's `details`; the service attribute a sync uses is `[]`
        # on this path, and reading that reported no orphans beside "2 failed".
        assert result.orphaned_blueprint_arns == [ORPHAN, f"{ORPHAN}-two"]
        # And only the failures: the deleted one is gone, not orphaned.
        assert len(result.orphaned_blueprint_arns) == 2

    def test_a_nonzero_failure_count_is_not_a_success_whatever_success_says(self, ops):
        """`success=True` alongside `failed_count=2` must not read as clean.

        The service does not produce that shape today — its `success` is
        `failed_count == 0` — so this covers the belt rather than an observed bug.
        Kept because it is the direction that matters: this is a destructive
        account-wide operation, and a future service change that reported `success`
        per-phase rather than per-blueprint would otherwise turn two undeleted
        blueprints into exit 0. Dropping `and failed == 0` left the suite green.
        """
        service, _manager, p_service, p_manager = _doubles(
            cleanup={
                "success": True,
                "message": "Deleted 1, 2 failed",
                "deleted_count": 1,
                "failed_count": 2,
                "details": [
                    {"name": "a", "arn": ORPHAN, "status": "failed"},
                    {"name": "b", "arn": f"{ORPHAN}-two", "status": "failed"},
                ],
            }
        )
        with p_service, p_manager:
            result = ops.sync_bda(direction="cleanup_orphaned", config_version="v1")

        assert result.success is False
        assert result.cleanup_failed_count == 2

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


@pytest.mark.unit
class TestTheProfileMustExist:
    """The profile name is the only thing standing between this and a mass deletion.

    These tests drive the **real** `BdaBlueprintService.cleanup_orphaned_blueprints`
    through `sync_bda`, with the blueprint creator and the configuration manager
    doubled. The manager being a double is a real limit and is stated rather than
    glossed: the key construction the guard's own docstring reasons about —
    `_read_record` building `Config#<version>` only when the version is truthy, and
    reading the bare `Config` key otherwise — is *not* exercised here. What is
    exercised is the service's own reduction of a `None` configuration to an empty
    class list, which is the step that turns a bad profile name into a mass deletion.

    Everything above this class doubles the service too, which means it
    cannot see what the service does with a profile that does not exist — and what it
    does is reduce `get_configuration(...) is None` to `current_classes = []`, so the
    expected-prefix set comes out empty, every blueprint carrying the stack's prefix
    matches nothing, and all of them are deleted with `success=True` and a deletion
    count. That answer is indistinguishable from a correct one.

    The measurement that matters is therefore **how many blueprints were deleted**,
    not what the result object says. A typo in `--config-profile` is the ordinary way
    to reach it.
    """

    ACCOUNT_BLUEPRINTS = [
        # Two belong to the live profile's classes; one is a genuine orphan.
        {
            "blueprintArn": f"{ORPHAN}-lending",
            "blueprintName": "idp-stack-Lending",
            "blueprintVersion": "1",
        },
        {
            "blueprintArn": f"{ORPHAN}-payslip",
            "blueprintName": "idp-stack-Payslip",
            "blueprintVersion": "1",
        },
        {
            "blueprintArn": f"{ORPHAN}-gone",
            "blueprintName": "idp-stack-Retired",
            "blueprintVersion": "1",
        },
    ]

    def _run(self, ops, *, existing_profile, asked_for, active=True):
        """Drive `sync_bda` with a real service and report what was deleted.

        Returns ``(result, deleted_arns)``.
        """
        deleted: list = []

        creator = MagicMock()
        creator.list_all_blueprints_with_prefix.return_value = list(
            self.ACCOUNT_BLUEPRINTS
        )
        creator.list_blueprints.return_value = {"blueprints": []}

        def _delete(arn, _version):
            deleted.append(arn)
            return True

        creator.delete_blueprint.side_effect = _delete

        # The real configuration manager, with only its DynamoDB read replaced: a
        # profile that exists answers with its classes, anything else answers None,
        # which is exactly what `get_configuration` does for a missing record.
        # `$id` is the key `cleanup_orphaned_blueprints` reads (`ID_FIELD`, falling
        # back to `x-aws-idp-document-type`). Spelled out here rather than as `name`,
        # which the service does not read: a class dict it cannot name contributes no
        # expected prefix, so the control below would delete all three and the
        # refusal tests would pass for a reason that has nothing to do with the fix.
        config_item = MagicMock()
        config_item.classes = [{"$id": "Lending"}, {"$id": "Payslip"}]

        manager = MagicMock()
        manager.get_bda_project_arn.return_value = PROJECT_ARN
        manager.list_config_versions.return_value = [
            {"versionName": existing_profile, "isActive": active}
        ]

        # `*_args, **_kwargs` on purpose: the SDK guard calls
        # `get_configuration("Config", version=...)` positionally and the service
        # calls `get_configuration(config_type="Config", version=...)` by keyword, and
        # both reads go through this one double. A signature that fitted only one of
        # them would make the other raise, which the service converts into
        # `success: False` -- a *refusal-shaped* answer that would let the assertions
        # below pass for the wrong reason.
        def _get_configuration(*_args, version=None, **_kwargs):
            return config_item if version == existing_profile else None

        manager.get_configuration.side_effect = _get_configuration

        from idp_common.bda.bda_blueprint_service import BdaBlueprintService

        service = BdaBlueprintService.__new__(BdaBlueprintService)
        service.blueprint_creator = creator
        service.config_manager = manager
        service.blueprint_name_prefix = "idp-stack"
        # `_project_arn` is a read-only property narrowing this attribute.
        service.dataAutomationProjectArn = PROJECT_ARN
        service.orphaned_blueprint_arns = []

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
            result = ops.sync_bda(
                direction="cleanup_orphaned", config_version=asked_for
            )
        return result, deleted

    def test_a_profile_that_exists_deletes_only_the_genuine_orphan(self, ops):
        """The control. Without this the refusals below prove nothing.

        Two of the three account blueprints belong to the profile's classes, so
        exactly one is an orphan.
        """
        result, deleted = self._run(
            ops, existing_profile="lending", asked_for="lending"
        )

        assert deleted == [f"{ORPHAN}-gone"]
        assert result.success is True
        assert result.cleanup_deleted_count == 1

    def test_a_mistyped_profile_deletes_nothing_and_says_why(self, ops):
        """The defect. `--config-profile lendnig` deleted all three and exited 0.

        The assertion is on `deleted`, not on the result object: the result object
        said `success=True, cleanup_deleted_count=3, error=None`, which is why nothing
        noticed.
        """
        result, deleted = self._run(
            ops, existing_profile="lending", asked_for="lendnig"
        )

        assert deleted == [], "not one blueprint may be deleted"
        assert result.success is False
        assert result.cleanup_deleted_count == 0
        assert "lendnig" in (result.error or "")
        assert "does not exist" in (result.error or "")

    def test_a_whitespace_only_profile_deletes_nothing(self, ops):
        """The same input class through a different spelling.

        `"   "` is truthy, so the emptiness check does not see it; it is the
        *existence* check that catches it, because `Config#   ` names no record. There
        is deliberately no `.strip()` clause: one was written, no input distinguished
        it from this, and mutating it away left the suite green.
        """
        result, deleted = self._run(ops, existing_profile="lending", asked_for="   ")

        assert deleted == []
        # The existence message, not the no-profile-active one -- which is what says
        # which of the two checks did the work.
        assert "does not exist" in (result.error or "")

    def test_an_empty_profile_name_resolves_to_the_active_one(self, ops):
        """`""` never reaches either check, and the boundary is worth recording.

        The resolution loop treats a falsy `config_version` as "not specified" and
        substitutes the active profile, so `--config-profile ""` behaves as if the
        option were omitted rather than being refused. That is defensible -- it is the
        same reading `config-download` gives it -- and it means the emptiness check's
        only reachable input is a `None` that survived the resolution because no
        profile is active.

        Recorded as a test rather than left implicit because it is easy to read the
        emptiness check as covering `""` and to write a docstring saying so.
        """
        result, deleted = self._run(ops, existing_profile="lending", asked_for="")

        assert deleted == [f"{ORPHAN}-gone"]
        assert result.success is True

    def test_no_profile_named_and_none_active_deletes_nothing(self, ops):
        """The unresolvable case, measured on the path it actually takes.

        With no profile active the resolution loop leaves `config_version` as `None`.
        Asserted here rather than in a doubled test because the doubled fixture
        supplies a project ARN, which hides that the real project resolution raises
        `TypeError` out of `_sanitize_project_name(None)` — a refusal placed after it
        never runs, and the caller gets that type error instead of an explanation.
        """
        result, deleted = self._run(
            ops, existing_profile="lending", asked_for=None, active=False
        )

        assert deleted == []
        assert result.success is False
        assert "needs a configuration profile" in (result.error or "")
        # And specifically not the type error the old placement produced.
        assert "NoneType" not in (result.error or "")

    def test_the_active_profile_is_resolved_when_none_is_named(self, ops):
        """Non-vacuity for the test above: it is the absence that refuses."""
        result, deleted = self._run(
            ops, existing_profile="lending", asked_for=None, active=True
        )

        assert deleted == [f"{ORPHAN}-gone"]
        assert result.success is True

    def test_a_profile_that_exists_with_no_classes_does_delete_everything(self, ops):
        """The distinction the refusal draws, stated as a test.

        "Keep nothing" is a real instruction and deleting every prefixed blueprint is
        the right response to it. The refusal is about "could not find out what to
        keep", which arrives at the service identically. Without this test the
        refusal could be widened to cover an empty class list and nothing would
        notice — and that would break a legitimate operation.
        """
        empty = MagicMock()
        empty.classes = []  # a profile that exists and keeps nothing

        creator = MagicMock()
        creator.list_all_blueprints_with_prefix.return_value = list(
            self.ACCOUNT_BLUEPRINTS
        )
        creator.list_blueprints.return_value = {"blueprints": []}
        deleted: list = []
        creator.delete_blueprint.side_effect = lambda arn, _v: (
            deleted.append(arn) or True
        )

        manager = MagicMock()
        manager.get_bda_project_arn.return_value = PROJECT_ARN
        manager.get_configuration.return_value = empty

        from idp_common.bda.bda_blueprint_service import BdaBlueprintService

        service = BdaBlueprintService.__new__(BdaBlueprintService)
        service.blueprint_creator = creator
        service.config_manager = manager
        service.blueprint_name_prefix = "idp-stack"
        # `_project_arn` is a read-only property narrowing this attribute.
        service.dataAutomationProjectArn = PROJECT_ARN
        service.orphaned_blueprint_arns = []

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
            result = ops.sync_bda(
                direction="cleanup_orphaned", config_version="emptied"
            )

        assert len(deleted) == 3
        assert result.success is True


@pytest.mark.unit
class TestFailedCleanupArnsToleratesTheShapesItCanBeGiven:
    """`_failed_cleanup_arns` reads the error path of a destructive operation.

    A `KeyError` or a `TypeError` raised while reporting a partial failure replaces
    the report with a stack trace, and the report is the only place the still-orphaned
    ARNs appear. So the reader is defensive about the shape — and each of those
    defences gets a test here, because three of them survived every mutation when they
    were first written, which is the same standard by which a redundant `.strip()` was
    deleted from the guard in the same commit. A defence with no test is
    indistinguishable from a redundant one.
    """

    def test_a_details_list_that_is_none_reports_no_arns(self):
        """The outer handler in `cleanup_orphaned_blueprints` returns `details: []`,
        but a future `None` there must not raise inside the failure report."""
        assert _failed_cleanup_arns({"details": None, "failed_count": 1}) == []

    def test_a_details_entry_that_is_not_a_dict_is_skipped(self):
        assert _failed_cleanup_arns(
            {"details": ["just a message", {"arn": ORPHAN, "status": "failed"}]}
        ) == [ORPHAN]

    def test_a_failed_entry_with_no_arn_is_dropped_rather_than_reported_as_none(self):
        """A `None` in this list renders as the word `None` beside real ARNs, which
        reads as a blueprint the operator should go and find."""
        assert _failed_cleanup_arns(
            {
                "details": [
                    {"name": "nameless", "status": "failed"},
                    {"name": "real", "arn": ORPHAN, "status": "failed"},
                ]
            }
        ) == [ORPHAN]

    def test_only_the_failures_are_reported(self):
        """Non-vacuity for all three above: the filter is on `status`."""
        assert _failed_cleanup_arns(
            {
                "details": [
                    {"name": "a", "arn": f"{ORPHAN}-ok", "status": "deleted"},
                    {"name": "b", "arn": ORPHAN, "status": "failed"},
                ]
            }
        ) == [ORPHAN]

    def test_a_cleanup_with_no_details_key_at_all_reports_no_arns(self):
        assert _failed_cleanup_arns({"failed_count": 0}) == []
