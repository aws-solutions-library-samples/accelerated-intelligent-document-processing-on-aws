# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""processChanges refuses a section or page change to a BDA-mode document (#1554).

A BDA run keeps the sections a document already has, and processChanges clears each
changed section's extraction result before requeueing, so the change would lose
those values. The resolver therefore refuses any section or page change when the
requeued run would go down the BDA branch, for every caller, and leaves a request
with no changes (field values already saved, reprocessed for evaluation and
summary) alone.

Drives the real resolver ``handler`` with the real ``ConfigurationManager`` against
moto — the configuration profiles are written by ``save_configuration`` and
``set_bda_project_arn``, the writers the product uses, so the routing read is the
one a deployed resolver makes. The tracking, users, S3 and SQS doubles are the
caller-checks suite's. Every refusal asserts nothing was written; every allowed case
asserts the document was requeued.

Engine: moto, not DynamoDB.
"""

import json
import os
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError

from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.constants import CONFIG_TYPE_CONFIG, DEFAULT_VERSION
from idp_common.config.models import IDPConfig
from idp_common.models import Document

from .test_process_changes_caller_checks import (
    CONFIG,
    WORKING_BUCKET,
    _assert_processed,
    _assert_untouched,
    _call,
    _owned,
    _seed,
    process_changes_env,
)

PROJECT = "arn:aws:bedrock:us-east-1:111122223333:data-automation-project/p"

_SECTION_CHANGES = [
    pytest.param(
        {
            "modifiedSections": [
                {"sectionId": "1", "classification": "receipt", "pageIds": ["1"]}
            ],
            "modifiedPages": [],
        },
        id="reclassify",
    ),
    pytest.param(
        {
            "modifiedSections": [
                {
                    "sectionId": "2",
                    "classification": "receipt",
                    "pageIds": ["1"],
                    "isNew": True,
                }
            ],
            "modifiedPages": [],
        },
        id="add-section",
    ),
    pytest.param(
        {
            "modifiedSections": [
                {
                    "sectionId": "1",
                    "classification": "invoice",
                    "pageIds": ["1"],
                    "isDeleted": True,
                }
            ],
            "modifiedPages": [],
        },
        id="delete-section",
    ),
    pytest.param(
        {
            "modifiedSections": [],
            "modifiedPages": [{"pageId": 1, "classReset": True}],
        },
        id="page-class-reset",
    ),
    pytest.param(
        {
            "modifiedSections": [],
            "modifiedPages": [
                {
                    "pageId": 1,
                    "textModified": True,
                    "newTextUri": "s3://pc-output-bucket/packet.pdf/pages/1/text.json",
                }
            ],
        },
        id="page-text-edit",
    ),
    pytest.param(
        # An entry naming no change at all is still refused: the rule is that this
        # operation changes no section and no page in BDA mode, not a list of kinds.
        {"modifiedSections": [], "modifiedPages": [{"pageId": 1}]},
        id="page-entry-without-a-flag",
    ),
]

_NO_CHANGES = {"modifiedSections": [], "modifiedPages": []}


@pytest.fixture
def env():
    with process_changes_env() as built:
        yield built


def _profile(version, use_bda, project=None):
    manager = ConfigurationManager(table_name=CONFIG)
    manager.save_configuration(
        CONFIG_TYPE_CONFIG, IDPConfig(use_bda=use_bda), version=version
    )
    if project:
        manager.set_bda_project_arn(version, project)


def _bda_document(env, version="tenant-a", **attributes):
    _profile(DEFAULT_VERSION, False)
    _profile(version, True, PROJECT)
    _seed(env, **_owned(), ConfigVersion=version, **attributes)


@pytest.mark.unit
class TestRefusedInBdaMode:
    @pytest.mark.parametrize("changes", _SECTION_CHANGES)
    def test_a_section_or_page_change_is_refused(self, env, changes):
        _bda_document(env)
        with pytest.raises(ValueError, match="^BDA_MODE:"):
            _call(env, changes=changes)
        _assert_untouched(env, "InProgress")

    @pytest.mark.parametrize("changes", _SECTION_CHANGES[:2])
    def test_an_admin_is_refused_too(self, env, changes):
        _bda_document(env)
        with pytest.raises(ValueError, match="^BDA_MODE:"):
            _call(env, groups=("Admin",), username="admin", changes=changes)
        _assert_untouched(env, "InProgress")

    def test_a_document_naming_no_profile_routes_by_the_active_profile(self, env):
        # The queue processor pins an unpinned document to the ACTIVE profile before
        # it routes, so that is the profile whose mode decides; `default` here is a
        # pipeline profile that must not be read in its place.
        _profile(DEFAULT_VERSION, False)
        _profile("bda-profile", True, PROJECT)
        ConfigurationManager(table_name=CONFIG).activate_version("bda-profile")
        _seed(env, **_owned(), ConfigVersion=None)
        with pytest.raises(ValueError, match="^BDA_MODE:"):
            _call(env, groups=("Admin",), username="admin")
        _assert_untouched(env, "InProgress")


@pytest.mark.unit
class TestUnknownModeRefuses:
    def test_no_configuration_table_refuses_a_change(self, env):
        os.environ["CONFIGURATION_TABLE_NAME"] = ""
        _seed(env, **_owned())
        with pytest.raises(ValueError, match="processing mode could not be determined"):
            _call(env)
        _assert_untouched(env, "InProgress")

    def test_an_unreadable_configuration_refuses_a_change(self, env):
        boto3.resource("dynamodb").Table(CONFIG).delete()
        _seed(env, **_owned())
        with pytest.raises(ValueError, match="processing mode could not be determined"):
            _call(env)
        _assert_untouched(env, "InProgress")

    def test_a_failed_bda_project_read_refuses_a_change(self, env):
        # The project read is the one a lenient reader answers with None, which
        # would read as "no project" and so as pipeline mode.
        _bda_document(env)
        real = ConfigurationManager.get_bda_project_arn

        def failing(self, version, raise_errors=False):
            class _Throttled:
                def get_item(self, **_):
                    raise ClientError(
                        {"Error": {"Code": "ProvisionedThroughputExceededException"}},
                        "GetItem",
                    )

            table, self.table = self.table, _Throttled()
            try:
                return real(self, version, raise_errors=raise_errors)
            finally:
                self.table = table

        with patch.object(ConfigurationManager, "get_bda_project_arn", failing):
            with pytest.raises(
                ValueError, match="processing mode could not be determined"
            ):
                _call(env, groups=("Admin",), username="admin")
        _assert_untouched(env, "InProgress")

    def test_no_configuration_table_leaves_a_request_with_no_changes_alone(self, env):
        os.environ["CONFIGURATION_TABLE_NAME"] = ""
        _seed(env, **_owned())
        result = _call(env, changes=_NO_CHANGES)
        assert result["success"] is True, result
        assert env.queued() == 1


@pytest.mark.unit
class TestStillAllowed:
    def test_data_only_reprocessing_of_a_bda_document_keeps_its_values(self, env):
        _bda_document(env)
        result = _call(env, changes=_NO_CHANGES)
        assert result["success"] is True, result
        row = env.row()
        assert row["ObjectStatus"] == "QUEUED"
        assert row["HITLStatus"] == "Review Completed"
        # Nothing was cleared: the BDA run keeps the section and its values.
        assert env.result_exists()
        assert env.queued() == 1

    def test_an_unpinned_document_under_an_active_pipeline_profile_is_allowed(
        self, env
    ):
        # The reverse of the active-profile refusal: `default` is BDA, but the
        # profile an unpinned document is processed under is the active one.
        _profile(DEFAULT_VERSION, True, PROJECT)
        _profile("pipeline-profile", False)
        ConfigurationManager(table_name=CONFIG).activate_version("pipeline-profile")
        _seed(env, **_owned(), ConfigVersion=None)
        _assert_processed(env, _call(env, groups=("Admin",), username="admin"))

    @pytest.mark.parametrize("changes", _SECTION_CHANGES[:1] + _SECTION_CHANGES[4:5])
    def test_a_profile_asking_for_bda_with_no_project_is_pipeline_mode(
        self, env, changes
    ):
        # The queue processor falls back to the pipeline when no BDA project is
        # linked, so the edit is carried through and is allowed.
        _profile(DEFAULT_VERSION, False)
        _profile("tenant-a", True)
        _seed(env, **_owned())
        _assert_processed(env, _call(env, changes=changes))

    @pytest.mark.parametrize("changes", _SECTION_CHANGES[:1] + _SECTION_CHANGES[3:5])
    def test_a_pipeline_document_takes_section_and_page_changes(self, env, changes):
        _profile(DEFAULT_VERSION, False)
        _profile("tenant-a", False, PROJECT)
        _seed(env, **_owned())
        result = _call(env, changes=changes)
        assert result["success"] is True, result
        assert env.queued() == 1
        assert not env.result_exists()


# ------------------------------------------------------------ pinned revision

REVISIONS_BUCKET = "pc-configuration-bucket"


@pytest.fixture
def pinned_env():
    """``env`` with the Configuration bucket the template now gives the resolver.

    With ``CONFIGURATION_BUCKET`` set, every profile save cuts a revision, so a
    profile saved twice has a pinned revision that disagrees with its head.
    """
    with process_changes_env() as built:
        os.environ["CONFIGURATION_BUCKET"] = REVISIONS_BUCKET
        boto3.client("s3").create_bucket(Bucket=REVISIONS_BUCKET)
        try:
            yield built
        finally:
            os.environ.pop("CONFIGURATION_BUCKET", None)


def _profile_with_history(pinned_use_bda, head_use_bda, version="tenant-a"):
    """Save ``version`` twice, linked to a BDA project; return the first revision.

    The second save changes something beyond ``use_bda``, so a second revision is
    cut even when the two values are equal.
    """
    _profile(DEFAULT_VERSION, False)
    manager = ConfigurationManager(table_name=CONFIG)
    manager.save_configuration(
        CONFIG_TYPE_CONFIG, IDPConfig(use_bda=pinned_use_bda), version=version
    )
    pinned = manager.resolve_published_revision(version)
    second = IDPConfig(use_bda=head_use_bda)
    second.extraction.temperature = 0.37
    manager.save_configuration(CONFIG_TYPE_CONFIG, second, version=version)
    head = manager.resolve_published_revision(version)
    assert pinned is not None and head is not None and head != pinned, (pinned, head)
    # Proves the fixture: the head and the pinned revision really disagree.
    assert bool(manager.get_merged_configuration(version).use_bda) is head_use_bda
    assert (
        bool(manager.get_merged_configuration(version, revision=pinned).use_bda)
        is pinned_use_bda
    )
    manager.set_bda_project_arn(version, PROJECT)
    return int(pinned)


def _queued_document(env):
    (message,) = boto3.client("sqs").receive_message(
        QueueUrl=env.queue_url, MaxNumberOfMessages=10
    )["Messages"]
    return Document.load_document(json.loads(message["Body"]), WORKING_BUCKET)


_RECLASSIFY_CHANGE = _SECTION_CHANGES[0].values[0]


@pytest.mark.unit
class TestPinnedRevisionDecides:
    """The mode is read from the revision the requeued run will route by."""

    def test_a_pinned_bda_revision_under_a_pipeline_head_is_refused(self, pinned_env):
        pinned = _profile_with_history(pinned_use_bda=True, head_use_bda=False)
        _seed(pinned_env, **_owned(), ConfigRevision=pinned)
        with pytest.raises(ValueError, match="^BDA_MODE:"):
            _call(pinned_env, changes=_RECLASSIFY_CHANGE)
        _assert_untouched(pinned_env, "InProgress")

    def test_a_pinned_pipeline_revision_under_a_bda_head_is_allowed(self, pinned_env):
        pinned = _profile_with_history(pinned_use_bda=False, head_use_bda=True)
        _seed(pinned_env, **_owned(), ConfigRevision=pinned)
        _assert_processed(pinned_env, _call(pinned_env, changes=_RECLASSIFY_CHANGE))
        # The requeued document carries the pin the decision was made on, so the
        # queue processor routes it by the same revision.
        queued = _queued_document(pinned_env)
        assert (queued.config_version, queued.config_revision) == ("tenant-a", pinned)

    def test_a_pinned_revision_with_no_profile_name_reads_the_active_profile(
        self, pinned_env
    ):
        # The queue processor resolves the active profile and keeps the revision.
        pinned = _profile_with_history(pinned_use_bda=True, head_use_bda=False)
        ConfigurationManager(table_name=CONFIG).activate_version("tenant-a")
        _seed(pinned_env, **_owned(), ConfigVersion=None, ConfigRevision=pinned)
        with pytest.raises(ValueError, match="^BDA_MODE:"):
            _call(
                pinned_env,
                groups=("Admin",),
                username="admin",
                changes=_RECLASSIFY_CHANGE,
            )
        _assert_untouched(pinned_env, "InProgress")

    @pytest.mark.parametrize(
        "head_use_bda",
        [pytest.param(True, id="head-bda"), pytest.param(False, id="head-pipeline")],
    )
    def test_a_document_with_no_pinned_revision_is_decided_by_the_head(
        self, pinned_env, head_use_bda
    ):
        _profile_with_history(
            pinned_use_bda=not head_use_bda, head_use_bda=head_use_bda
        )
        _seed(pinned_env, **_owned())
        if head_use_bda:
            with pytest.raises(ValueError, match="^BDA_MODE:"):
                _call(pinned_env, changes=_RECLASSIFY_CHANGE)
            _assert_untouched(pinned_env, "InProgress")
        else:
            _assert_processed(pinned_env, _call(pinned_env, changes=_RECLASSIFY_CHANGE))


@pytest.mark.unit
class TestUnreadablePinnedRevisionRefuses:
    """Never read as pipeline mode, and never answered from the head instead."""

    def test_a_revision_that_is_not_retained_refuses(self, pinned_env):
        # The head is a pipeline profile, so a head fallback would allow this.
        _profile_with_history(pinned_use_bda=True, head_use_bda=False)
        _seed(pinned_env, **_owned(), ConfigRevision=99)
        with pytest.raises(ValueError, match="processing mode could not be determined"):
            _call(pinned_env, changes=_RECLASSIFY_CHANGE)
        _assert_untouched(pinned_env, "InProgress")

    def test_a_denied_revision_body_refuses_as_a_400_not_a_403(self, pinned_env):
        # The revision store raises PermissionError on S3 AccessDenied, which the
        # handler would otherwise pass through as an authorization denial.
        pinned = _profile_with_history(pinned_use_bda=True, head_use_bda=False)
        _seed(pinned_env, **_owned(), ConfigRevision=pinned)
        from idp_common.config.revisions import ConfigRevisionStore

        reads = []

        def denied(self, profile, revision):
            reads.append((profile, revision))
            raise PermissionError("AccessDenied on the revision body")

        with patch.object(ConfigRevisionStore, "get_body", denied):
            with pytest.raises(
                ValueError, match="processing mode could not be determined"
            ):
                _call(pinned_env, changes=_RECLASSIFY_CHANGE)
        assert reads == [("tenant-a", pinned)], reads
        _assert_untouched(pinned_env, "InProgress")

    def test_no_configuration_bucket_refuses_a_pinned_document(self, pinned_env):
        pinned = _profile_with_history(pinned_use_bda=True, head_use_bda=False)
        os.environ.pop("CONFIGURATION_BUCKET")
        _seed(pinned_env, **_owned(), ConfigRevision=pinned)
        with pytest.raises(ValueError, match="processing mode could not be determined"):
            _call(pinned_env, changes=_RECLASSIFY_CHANGE)
        _assert_untouched(pinned_env, "InProgress")
