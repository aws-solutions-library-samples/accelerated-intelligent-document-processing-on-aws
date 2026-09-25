# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`cleanup_orphaned` deletes account-wide, and the profile name is the only brake.

`syncBdaIdp` with `direction: "cleanup_orphaned"` deletes every BDA blueprint carrying
the stack's name prefix that no class in the named profile accounts for — not the
project's blueprints, the **account's**. The set of survivors is built from the profile
named in `versionName`, and `cleanup_orphaned_blueprints` reduces a `get_configuration`
that answers `None` to an empty class list. So a `versionName` whose record cannot be
read means an empty expected-prefix set, every prefixed blueprint matches nothing, and
all of them are deleted — reported as `success: True` with a deletion count, which is
indistinguishable from having done the right thing.

These tests therefore assert on **how many blueprints were deleted**, not on what the
response says. The response was never the problem: it said success.

Two things make this route easier to hit than the SDK's. It is the route the web UI and
the API use, so it is the one most users reach. And it does not resolve the active
profile — `versionName` defaults to the literal `"default"` — so a stack whose profiles
are named anything else reaches the cleanup with a name matching no record.

The service is real here, with only its blueprint creator and configuration manager
doubled. A test that doubled `cleanup_orphaned_blueprints` could not see any of this:
the reduction of `None` to an empty class list happens inside it.
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

PROJECT_ARN = "arn:aws:bedrock:us-east-1:123456789012:data-automation-project/p1"
BP = "arn:aws:bedrock:us-east-1:123456789012:blueprint"

# Two of these belong to the live profile's classes; the third is a genuine orphan.
ACCOUNT_BLUEPRINTS = [
    {"blueprintArn": f"{BP}/idp-Lending", "blueprintName": "idp-stack-Lending"},
    {"blueprintArn": f"{BP}/idp-Payslip", "blueprintName": "idp-stack-Payslip"},
    {"blueprintArn": f"{BP}/idp-Retired", "blueprintName": "idp-stack-Retired"},
]
THE_ONLY_ORPHAN = f"{BP}/idp-Retired"

LIVE_PROFILE = "lending"


def _load_index():
    spec = importlib.util.spec_from_file_location(
        "sync_bda_idp_resolver_index", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_bda_idp_resolver_index"] = module
    spec.loader.exec_module(module)
    return module


index = _load_index()

_ABSENT = object()


def _event(*, version_name=_ABSENT, project_arn=PROJECT_ARN):
    """An Admin caller asking for a cleanup.

    Admin skips the config-version scope lookup, so the only collaborators left are the
    configuration manager and the blueprint service. `version_name=_ABSENT` omits the
    argument entirely, which is how the resolver's own `"default"` fallback is reached.
    """
    arguments = {
        "direction": "cleanup_orphaned",
        "syncMode": "replace",
        "bdaProjectArn": project_arn,
        "saveArn": False,
    }
    if version_name is not _ABSENT:
        arguments["versionName"] = version_name
    return {
        "info": {"fieldName": "syncBdaIdp"},
        "arguments": arguments,
        "identity": {"claims": {"cognito:groups": ["Admin"], "email": "a@example.com"}},
    }


class Harness:
    """What the cleanup actually did, as opposed to what it reported.

    `deleted` is every ARN `delete_blueprint` was called with. `project_creations`
    counts the two writes the ARN-resolution chain performs before the cleanup branch
    is reached — `get_or_create_project_for_version`, which creates a BDA project, and
    the `creating` sync-status write that precedes it. A refusal placed after that chain
    would leave both non-zero while still returning the right message, which is why they
    are counted separately from the deletions.
    """

    def __init__(self):
        self.deleted: list = []
        self.project_creations: list = []
        self.status_writes: list = []
        self.config_reads: list = []


def _run(
    monkeypatch,
    *,
    event,
    records,
    versions=(),
    table_name="cfg-table",
    get_configuration=None,
):
    """Drive the real cleanup and report what it deleted.

    `records` maps a `version` value to the configuration object `get_configuration`
    answers with; anything absent answers `None`, which is what a missing DynamoDB
    record produces. `table_name=None` unsets `CONFIGURATION_TABLE_NAME`, the one input
    that leaves the resolver with no configuration manager at all.
    """
    h = Harness()

    if table_name is None:
        monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
    else:
        monkeypatch.setenv("CONFIGURATION_TABLE_NAME", table_name)

    creator = MagicMock()
    creator.list_all_blueprints_with_prefix.return_value = [
        dict(b) for b in ACCOUNT_BLUEPRINTS
    ]
    creator.list_blueprints.return_value = {"blueprints": []}

    def _delete(arn, _version):
        h.deleted.append(arn)
        return True

    creator.delete_blueprint.side_effect = _delete

    manager = MagicMock()
    manager.get_bda_project_arn.return_value = PROJECT_ARN
    manager.list_config_versions.return_value = list(versions)
    manager.set_bda_sync_status.side_effect = lambda v, s: h.status_writes.append((v, s))

    # `*_args, **_kwargs` on purpose: the guard reads
    # `get_configuration("Config", version=...)` positionally and the service reads
    # `get_configuration(config_type="Config", version=...)` by keyword, and both go
    # through this one double. A signature fitting only one of them would make the other
    # raise, which the resolver converts into `success: False` — a refusal-shaped answer
    # that would let every assertion below pass for the wrong reason.
    # Matched by identical type and equality rather than by `dict.get`, because one of
    # the inputs below is an unhashable JSON value: a `records.get(["lending"])` would
    # raise `TypeError`, the resolver would convert that into `success: False`, and the
    # test would pass on an accident instead of on the guard.
    def _default_get_configuration(*_args, version=None, **_kwargs):
        h.config_reads.append(version)
        for key, value in records.items():
            if type(version) is type(key) and version == key:
                return value
        return None

    manager.get_configuration.side_effect = (
        get_configuration or _default_get_configuration
    )

    from idp_common.bda.bda_blueprint_service import BdaBlueprintService

    service = BdaBlueprintService.__new__(BdaBlueprintService)
    service.blueprint_creator = creator
    service.config_manager = manager
    service.blueprint_name_prefix = "idp-stack"
    # `_project_arn` is a read-only property narrowing this attribute.
    service.dataAutomationProjectArn = PROJECT_ARN
    service.orphaned_blueprint_arns = []

    def _create(version):
        h.project_creations.append(version)
        return PROJECT_ARN

    service.get_or_create_project_for_version = _create
    # Stubbed on the instance so the non-cleanup directions can be driven for
    # non-vacuity without running a real sync; the cleanup branch never reaches it.
    service.create_blueprints_from_custom_configuration = lambda **_kwargs: []

    with (
        patch.object(index, "BdaBlueprintService", return_value=service),
        patch.object(index, "ConfigurationManager", return_value=manager),
    ):
        result = index.handler(event, None)
    return result, h


def _profile_with(classes):
    """A configuration record the service can read classes off.

    `$id` is the key `cleanup_orphaned_blueprints` reads (`ID_FIELD`, falling back to
    `x-aws-idp-document-type`). Spelled out here rather than as `name`, which the service
    does not read: a class dict it cannot name contributes no expected prefix, so the
    control below would delete all three and every refusal test would pass for a reason
    that has nothing to do with this guard.
    """
    item = MagicMock()
    item.classes = classes
    return item


LIVE_RECORDS = {LIVE_PROFILE: _profile_with([{"$id": "Lending"}, {"$id": "Payslip"}])}


@pytest.mark.unit
class TestTheControl:
    """Without this the refusals below prove nothing at all."""

    def test_the_named_profile_deletes_only_the_genuine_orphan(self, monkeypatch):
        result, h = _run(
            monkeypatch, event=_event(version_name=LIVE_PROFILE), records=LIVE_RECORDS
        )

        assert h.deleted == [THE_ONLY_ORPHAN]
        assert result["success"] is True
        assert result["cleanupDetails"]["deletedCount"] == 1
        assert result["cleanupDetails"]["failedCount"] == 0

    def test_a_profile_that_exists_with_no_classes_does_delete_everything(
        self, monkeypatch
    ):
        """The distinction the refusal draws, as a test.

        "Keep nothing" is a real instruction and deleting every prefixed blueprint is the
        right response to it. The refusal is about "could not find out what to keep",
        which arrives at the service identically. Without this test the refusal could
        later be widened to cover an empty class list and nothing would notice — and that
        would break a legitimate operation.
        """
        result, h = _run(
            monkeypatch,
            event=_event(version_name="emptied"),
            records={"emptied": _profile_with([])},
        )

        assert len(h.deleted) == 3
        assert result["success"] is True
        assert result["cleanupDetails"]["deletedCount"] == 3

    def test_a_record_that_is_falsy_but_present_is_still_honoured(self, monkeypatch):
        """The check is `is None`, and the difference is reachable.

        A configuration object may be falsy — an empty container, or a model defining
        `__len__` — while being a perfectly real record. Rewriting the check as
        `if not manager.get_configuration(...)` refuses it, which would break the
        keep-nothing case above through a spelling that looks equivalent.
        """
        falsy = MagicMock()
        falsy.classes = []
        falsy.__bool__ = lambda _self: False

        result, h = _run(
            monkeypatch,
            event=_event(version_name="falsy"),
            records={"falsy": falsy},
        )

        assert len(h.deleted) == 3
        assert result["success"] is True


@pytest.mark.unit
class TestAProfileThatCannotBeReadDeletesNothing:
    """Every one of these deleted all three blueprints and reported success.

    The shapes differ in how they fail to name a record, and they are separate tests
    rather than one parametrised case because the *reason* differs: a typo and a
    case-only difference miss the lookup, a falsy name never reaches it, and an absent
    argument reaches it under a name the resolver substituted.
    """

    def _refused(self, monkeypatch, event, records=None, **kw):
        result, h = _run(
            monkeypatch, event=event, records=records or LIVE_RECORDS, **kw
        )
        assert h.deleted == [], "not one blueprint may be deleted"
        assert result["success"] is False
        assert result["cleanupDetails"]["deletedCount"] == 0
        assert result["direction"] == "cleanup_orphaned"
        # The message has to reach the UI on both fields it renders.
        assert result["message"]
        assert result["error"]["message"] == result["message"]
        return result, h

    def test_a_mistyped_profile(self, monkeypatch):
        """The defect. `versionName: "lendnig"` deleted `lending`'s live blueprints."""
        result, _ = self._refused(monkeypatch, _event(version_name="lendnig"))
        assert "lendnig" in result["message"]
        assert "does not exist" in result["message"]

    def test_a_case_only_difference(self, monkeypatch):
        """DynamoDB keys are case-sensitive; a reader of the UI field may not be."""
        result, _ = self._refused(monkeypatch, _event(version_name="Lending"))
        assert "does not exist" in result["message"]

    def test_a_leading_space(self, monkeypatch):
        result, _ = self._refused(monkeypatch, _event(version_name=" lending"))
        assert "does not exist" in result["message"]

    def test_a_trailing_space(self, monkeypatch):
        result, _ = self._refused(monkeypatch, _event(version_name="lending "))
        assert "does not exist" in result["message"]

    def test_a_whitespace_only_name(self, monkeypatch):
        """Truthy, so the emptiness check does not see it; `Config#   ` names nothing.

        There is deliberately no `.strip()` clause — no input distinguishes one from the
        existence check, and a guard no input distinguishes is one that can be removed
        with the suite still green.
        """
        result, _ = self._refused(monkeypatch, _event(version_name="   "))
        assert "does not exist" in result["message"]

    def test_no_version_name_at_all(self, monkeypatch):
        """The resolver substitutes the literal `"default"`, which is not a profile here.

        This resolver does not resolve the *active* profile, so on a stack whose profiles
        are named anything else, omitting the argument is a mass-deletion input rather
        than a convenience.
        """
        result, _ = self._refused(monkeypatch, _event())
        assert "default" in result["message"]
        assert "does not exist" in result["message"]

    def test_an_explicit_null(self, monkeypatch):
        """`versionName: null`, which is falsy and must not reach the lookup.

        `_read_record` builds the key as `Config#<version>` only when the version is
        truthy and reads the **bare** `Config` key otherwise — a key that can hold a
        record nothing else can see. Left to the lookup this would *succeed* on a record
        describing no profile, so it is checked before the lookup and the message says so.
        """
        result, _ = self._refused(monkeypatch, _event(version_name=None))
        assert "needs a configuration profile" in result["message"]

    def test_an_empty_string(self, monkeypatch):
        result, _ = self._refused(monkeypatch, _event(version_name=""))
        assert "needs a configuration profile" in result["message"]

    def test_a_bare_config_key_holding_a_record_does_not_satisfy_the_check(
        self, monkeypatch
    ):
        """The #1230 shape: `get_configuration("Config", version=None)` answers a record.

        With the falsy check first, that record is never read, and the assertion that
        says so is `config_reads == []` — the emptiness check, not the lookup, is what
        refuses. Reordering the two clauses passes this test's deletion count only until
        this assertion.
        """
        records = dict(LIVE_RECORDS)
        records[None] = _profile_with([{"$id": "Lending"}])
        result, h = self._refused(
            monkeypatch, _event(version_name=None), records=records
        )
        assert h.config_reads == [], "the lookup must not be reached for a falsy name"
        assert "needs a configuration profile" in result["message"]

    def test_a_name_in_the_version_list_with_no_backing_record(self, monkeypatch):
        """A listed profile is not a readable one, and the list is not consulted.

        `list_config_versions` reads a different set of items from the one
        `get_configuration` reads, so a name can appear in the list with no `Config#<name>`
        record behind it. The guard asks the reader that the cleanup itself asks.
        """
        result, _ = self._refused(
            monkeypatch,
            _event(version_name="listed-only"),
            versions=[{"versionName": "listed-only", "isActive": True}],
        )
        assert "does not exist" in result["message"]

    def test_no_configuration_table_at_all(self, monkeypatch):
        """`CONFIGURATION_TABLE_NAME` unset leaves the resolver no manager to ask.

        This is the clause that stops the guard being switched off by a missing
        environment variable, which is the difference between a guard and the appearance
        of one. Note the cleanup would otherwise have run: the service carries its own
        configuration manager, so the resolver's is not what it reads.
        """
        result, _ = self._refused(monkeypatch, _event(version_name=LIVE_PROFILE), table_name=None)
        assert "configuration table" in result["message"]

    def test_a_configuration_read_that_raises(self, monkeypatch):
        """Fail closed: an unreadable profile is an unanswered question, not a green light."""

        def _boom(*_args, **_kwargs):
            raise RuntimeError("DynamoDB is having a day")

        result, h = _run(
            monkeypatch,
            event=_event(version_name=LIVE_PROFILE),
            records=LIVE_RECORDS,
            get_configuration=_boom,
        )

        assert h.deleted == []
        assert result["success"] is False

    @pytest.mark.parametrize(
        "not_a_name",
        [123, True, ["lending"], {"versionName": "lending"}, ("lending",), 1.5],
        ids=["int", "bool", "list", "dict", "tuple", "float"],
    )
    def test_a_version_name_that_is_not_a_profile_name(self, monkeypatch, not_a_name):
        """The rule is about capability, not about matching a shape.

        The guard proceeds only on an affirmative record read for the exact value the
        cleanup will use, so anything that is not a key with a record behind it stops —
        whatever its type, and without the guard needing to enumerate types. These arrive
        from a client that sends a JSON value of the wrong kind; without the guard each
        one deletes every prefixed blueprint in the account.
        """
        result, h = _run(
            monkeypatch, event=_event(version_name=not_a_name), records=LIVE_RECORDS
        )
        assert h.deleted == []
        assert result["success"] is False


@pytest.mark.unit
class TestTheGuardRunsBeforeTheProjectResolution:
    """The ordering is the whole of the fix, and it is invisible to a refusal assertion.

    The ARN-resolution chain that precedes the cleanup branch is not side-effect-free: a
    `bdaProjectArn` of `CREATE_NEW` writes a `creating` sync status and calls
    `get_or_create_project_for_version`, which **creates a BDA project**. A guard placed
    after that chain returns the same message while having already created one — so these
    tests count the writes rather than reading the response.
    """

    def test_a_refused_cleanup_creates_no_bda_project(self, monkeypatch):
        result, h = _run(
            monkeypatch,
            event=_event(version_name="lendnig", project_arn="CREATE_NEW"),
            records=LIVE_RECORDS,
        )

        assert h.project_creations == [], "a refused cleanup must create no BDA project"
        assert h.status_writes == [], "and must write no sync status"
        assert h.deleted == []
        assert result["success"] is False
        assert "does not exist" in result["message"]

    def test_a_refused_cleanup_with_no_profile_creates_no_bda_project(self, monkeypatch):
        """The same, for the input that made the old SDK placement raise `TypeError`.

        `_sanitize_project_name(None)` raises, so a guard after the resolution never runs
        for a null profile and the caller gets a type error instead of an explanation.
        """
        result, h = _run(
            monkeypatch,
            event=_event(version_name=None, project_arn="CREATE_NEW"),
            records=LIVE_RECORDS,
        )

        assert h.project_creations == []
        assert h.status_writes == []
        assert h.deleted == []
        assert "needs a configuration profile" in result["message"]
        # And specifically not the type error the after-the-resolution placement gives.
        assert "NoneType" not in result["message"]

    def test_create_new_still_creates_a_project_for_a_sync(self, monkeypatch):
        """Non-vacuity for both tests above: the counter does move on the path that
        creates. Without this, a `project_creations` list that is empty because nothing
        ever appends to it would read as proof of the ordering.
        """
        event = _event(version_name=LIVE_PROFILE, project_arn="CREATE_NEW")
        event["arguments"]["direction"] = "idp_to_bda"

        result, h = _run(monkeypatch, event=event, records=LIVE_RECORDS)

        assert h.project_creations == [LIVE_PROFILE]
        assert h.status_writes == [(LIVE_PROFILE, "creating")]
        assert result["success"] is True


@pytest.mark.unit
class TestTheRefusalDoesNotDisplaceTheAuthorizationChecks:
    """A refusal that arrives before the RBAC check would answer a Viewer's question.

    The guard sits after both authorization checks, so an unauthorized caller still gets
    `Unauthorized` and learns nothing about which profiles exist — including through the
    cleanup direction, where the refusal message names the profile it looked for.
    """

    def test_a_viewer_is_unauthorized_rather_than_refused(self, monkeypatch):
        event = _event(version_name="lendnig")
        event["identity"]["claims"]["cognito:groups"] = ["Viewer"]

        result, h = _run(monkeypatch, event=event, records=LIVE_RECORDS)

        assert result["error"]["type"] == "Unauthorized"
        assert h.deleted == []
        assert h.config_reads == []
