# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Reading revisions back out through the SDK.

Why this exists
---------------
Revisions were recordable and pinnable, but *invisible* to a programmatic caller:
`upload()` did not say which revision it had just created, `list()` did not expose
the counters `list_config_versions()` already returned, and there was no way to
enumerate or fetch a revision at all. An automated tuning loop therefore could not
use revisions to track its iterations — the IDP Auto Optimizer extension mints a
new *named profile* per iteration for exactly this reason, polluting every profile
picker and RBAC scope list in the host stack with one profile per attempt.

The load-bearing assertion in here is that `upload()` reports the revision it
produced. Without it a caller must guess a number, and a wrong guess pins the run
to a *different* configuration while reporting the one it meant.
"""

from unittest.mock import patch

import pytest

from idp_sdk import IDPClient
from idp_sdk.exceptions import IDPResourceNotFoundError

STACK_RESOURCES = [
    {
        "StackResourceSummaries": [
            {
                "LogicalResourceId": "ConfigurationTable",
                "PhysicalResourceId": "test-table",
            }
        ]
    }
]


def _wire_stack(mock_boto3):
    mock_boto3.return_value.get_paginator.return_value.paginate.return_value = (
        STACK_RESOURCES
    )


@pytest.mark.unit
@pytest.mark.config
class TestUploadReportsItsRevision:
    @patch("boto3.client")
    @patch("idp_common.config.configuration_manager.ConfigurationManager")
    def test_upload_returns_the_revision_it_created(
        self, mock_manager_class, mock_boto3, tmp_path
    ):
        _wire_stack(mock_boto3)
        manager = mock_manager_class.return_value
        manager.get_configuration.return_value = {"version": "lending"}
        manager.handle_update_custom_configuration.return_value = True
        manager.resolve_published_revision.return_value = 7

        config_file = tmp_path / "c.yaml"
        config_file.write_text("classes: []\n")

        client = IDPClient(stack_name="test-stack")
        result = client.config.upload(
            config_file=str(config_file), config_profile="lending", validate=False
        )

        assert result.success is True
        assert result.revision == 7, (
            "upload() must report the revision it produced, or the caller cannot "
            "pin a run to what it just uploaded"
        )
        manager.resolve_published_revision.assert_called_once_with("lending")

    @patch("boto3.client")
    @patch("idp_common.config.configuration_manager.ConfigurationManager")
    def test_no_history_reports_none_rather_than_a_made_up_number(
        self, mock_manager_class, mock_boto3, tmp_path
    ):
        # An older deployment has no revision history. `revision=None` is the
        # honest answer; `0` or `1` would be pinnable and wrong.
        _wire_stack(mock_boto3)
        manager = mock_manager_class.return_value
        manager.get_configuration.return_value = {"version": "lending"}
        manager.handle_update_custom_configuration.return_value = True
        manager.resolve_published_revision.return_value = None

        config_file = tmp_path / "c.yaml"
        config_file.write_text("classes: []\n")

        client = IDPClient(stack_name="test-stack")
        result = client.config.upload(
            config_file=str(config_file), config_profile="lending", validate=False
        )

        assert result.success is True
        assert result.revision is None

    @patch("boto3.client")
    @patch("idp_common.config.configuration_manager.ConfigurationManager")
    def test_an_unreadable_counter_does_not_fail_the_upload(
        self, mock_manager_class, mock_boto3, tmp_path
    ):
        """The configuration was saved; not knowing its number is not a failure."""
        _wire_stack(mock_boto3)
        manager = mock_manager_class.return_value
        manager.get_configuration.return_value = {"version": "lending"}
        manager.handle_update_custom_configuration.return_value = True
        manager.resolve_published_revision.side_effect = RuntimeError("throttled")

        config_file = tmp_path / "c.yaml"
        config_file.write_text("classes: []\n")

        client = IDPClient(stack_name="test-stack")
        result = client.config.upload(
            config_file=str(config_file), config_profile="lending", validate=False
        )

        assert result.success is True
        assert result.revision is None


@pytest.mark.unit
@pytest.mark.config
class TestListExposesTheCounters:
    @patch("boto3.client")
    @patch("idp_common.config.configuration_manager.ConfigurationManager")
    def test_list_carries_latest_and_published_revision(
        self, mock_manager_class, mock_boto3
    ):
        _wire_stack(mock_boto3)
        mock_manager_class.return_value.list_config_versions.return_value = [
            {
                "versionName": "lending",
                "isActive": True,
                "latestRevision": 9,
                "publishedRevision": 7,
            },
            {"versionName": "untouched", "isActive": False},
        ]

        client = IDPClient(stack_name="test-stack")
        result = client.config.list()

        by_name = {v.version_name: v for v in result.versions}
        assert by_name["lending"].latest_revision == 9
        # published != latest is the mid-restore state, and it is the one a caller
        # should pin to — the profile head reflects r7, not r9.
        assert by_name["lending"].published_revision == 7
        assert by_name["untouched"].latest_revision is None
        assert by_name["untouched"].published_revision is None


@pytest.mark.unit
@pytest.mark.config
class TestTheEnvironmentIdpCommonNeeds:
    """
    Both defects in this class were found by running the CLI against a live stack;
    every mocked test passed with them present.
    """

    @patch("boto3.client")
    @patch("idp_common.config.configuration_manager.ConfigurationManager")
    def test_the_configuration_bucket_is_exported_not_just_the_table(
        self, mock_manager_class, mock_boto3, monkeypatch
    ):
        # Revision history lives in TWO places: counters and index in DynamoDB,
        # recorded configurations in S3. ConfigRevisionStore reads
        # CONFIGURATION_BUCKET and treats its absence as "history disabled", then
        # silently does nothing — so exporting only the table meant CLI/SDK saves
        # cut no revision, listings came back empty, and deleting a profile
        # orphaned its bodies in S3.
        monkeypatch.delenv("CONFIGURATION_BUCKET", raising=False)
        mock_boto3.return_value.get_paginator.return_value.paginate.return_value = [
            {
                "StackResourceSummaries": [
                    {
                        "LogicalResourceId": "ConfigurationTable",
                        "PhysicalResourceId": "test-table",
                    },
                    {
                        "LogicalResourceId": "ConfigurationBucket",
                        "PhysicalResourceId": "test-bucket",
                    },
                ]
            }
        ]
        mock_manager_class.return_value.list_config_versions.return_value = []

        client = IDPClient(stack_name="test-stack")
        client.config.list()

        import os

        assert os.environ.get("CONFIGURATION_TABLE_NAME") == "test-table"
        assert os.environ.get("CONFIGURATION_BUCKET") == "test-bucket"

    @patch("boto3.client")
    @patch("idp_common.config.configuration_manager.ConfigurationManager")
    def test_a_profile_record_without_is_active_does_not_break_the_listing(
        self, mock_manager_class, mock_boto3
    ):
        # DynamoDB omits an attribute that was never written, so the key is
        # PRESENT with value None and `.get(key, False)` never returns its
        # default. Pydantic then rejected None for a bool — taking down the whole
        # command for every profile because ONE record lacked the attribute.
        _wire_stack(mock_boto3)
        mock_manager_class.return_value.list_config_versions.return_value = [
            {"versionName": "legacy", "isActive": None},
            {"versionName": "active", "isActive": True},
        ]

        client = IDPClient(stack_name="test-stack")
        result = client.config.list()

        by_name = {v.version_name: v for v in result.versions}
        assert by_name["legacy"].is_active is False
        assert by_name["active"].is_active is True

    @patch("boto3.client")
    @patch("idp_common.config.configuration_manager.ConfigurationManager")
    def test_unavailable_history_is_not_reported_as_an_empty_history(
        self, mock_manager_class, mock_boto3
    ):
        """
        A disabled store answers every read with [], which reads as "this profile
        has no revisions" for a profile that has plenty. The two have opposite
        implications — one means nothing was recorded, the other means what was
        recorded cannot be reached — so they must not share a response.
        """
        from idp_sdk.exceptions import IDPProcessingError

        _wire_stack(mock_boto3)
        manager = mock_manager_class.return_value
        manager.revisions.enabled = False

        client = IDPClient(stack_name="test-stack")
        with pytest.raises(IDPProcessingError) as excinfo:
            client.config.revisions(config_profile="lending")
        assert "unavailable" in str(excinfo.value)
        manager.list_revisions.assert_not_called()


@pytest.mark.unit
@pytest.mark.config
class TestDownloadARevision:
    @patch("boto3.client")
    @patch("idp_common.config.configuration_manager.ConfigurationManager")
    def test_download_reads_the_requested_revision_body(
        self, mock_manager_class, mock_boto3
    ):
        _wire_stack(mock_boto3)
        manager = mock_manager_class.return_value
        manager.get_revision.return_value = {
            "classes": [{"name": "W2"}],
            # Storage bookkeeping that must not leak into a re-uploadable config.
            "_config_format": "full",
            "config_type": "Config",
        }

        client = IDPClient(stack_name="test-stack")
        result = client.config.download(config_profile="lending", config_revision=7)

        manager.get_revision.assert_called_once_with("lending", 7)
        assert result.config == {"classes": [{"name": "W2"}]}
        assert result.revision == 7

    @patch("boto3.client")
    @patch("idp_common.config.configuration_manager.ConfigurationManager")
    def test_a_pruned_revision_raises_instead_of_returning_the_head(
        self, mock_manager_class, mock_boto3
    ):
        """
        Silently substituting the profile's current configuration would hand back
        a *different* configuration under the name the caller asked for — and it
        would look like a success, so the substitution would go unnoticed until
        the numbers from that run were compared against something else.
        """
        _wire_stack(mock_boto3)
        mock_manager_class.return_value.get_revision.return_value = None

        client = IDPClient(stack_name="test-stack")
        with pytest.raises(IDPResourceNotFoundError) as excinfo:
            client.config.download(config_profile="lending", config_revision=99)
        assert "r99" in str(excinfo.value)

    @patch("boto3.client")
    @patch("idp_common.config.configuration_manager.ConfigurationManager")
    def test_the_written_file_records_which_revision_it_is(
        self, mock_manager_class, mock_boto3, tmp_path
    ):
        # A downloaded revision and a downloaded head are otherwise
        # indistinguishable on disk.
        _wire_stack(mock_boto3)
        mock_manager_class.return_value.get_revision.return_value = {"classes": []}
        output = tmp_path / "r7.yaml"

        client = IDPClient(stack_name="test-stack")
        client.config.download(
            config_profile="lending", config_revision=7, output=str(output)
        )

        assert "revision r7" in output.read_text()

    @patch("boto3.client")
    @patch("idp_common.config.configuration_manager.ConfigurationManager")
    def test_no_revision_requested_uses_the_profile_head(
        self, mock_manager_class, mock_boto3
    ):
        _wire_stack(mock_boto3)
        manager = mock_manager_class.return_value
        with patch("idp_common.config.ConfigurationReader") as mock_reader_class:
            mock_reader_class.return_value.get_configuration.return_value = {
                "classes": []
            }
            client = IDPClient(stack_name="test-stack")
            result = client.config.download(config_profile="lending")

        manager.get_revision.assert_not_called()
        assert result.revision is None


@pytest.mark.unit
@pytest.mark.config
class TestListRevisions:
    @patch("boto3.client")
    @patch("idp_common.config.configuration_manager.ConfigurationManager")
    def test_revisions_are_returned_typed_and_newest_first(
        self, mock_manager_class, mock_boto3
    ):
        _wire_stack(mock_boto3)
        mock_manager_class.return_value.list_revisions.return_value = [
            {
                "revision": 3,
                "createdAt": "2026-08-31T10:00:00Z",
                "createdBy": "a@example.com",
                "label": None,
                "notes": "raised topK",
                "sizeBytes": 4096,
                "classFingerprint": "abc123",
                "pinned": False,
                "published": True,
            },
            {
                "revision": 2,
                "createdAt": "2026-08-31T09:00:00Z",
                "createdBy": "system",
                "label": "baseline",
                "notes": None,
                "sizeBytes": 4000,
                "classFingerprint": "abc123",
                "pinned": True,
                "published": False,
            },
        ]

        client = IDPClient(stack_name="test-stack")
        result = client.config.revisions(config_profile="lending")

        assert result.profile == "lending"
        assert result.count == 2
        assert [r.revision for r in result.revisions] == [3, 2]
        assert result.revisions[0].published is True
        assert result.revisions[1].pinned is True
        assert result.revisions[1].label == "baseline"
        # Same fingerprint => the two revisions extract the same fields, so their
        # accuracy numbers are comparable. That is the whole point of exposing it.
        assert result.revisions[0].class_fingerprint == "abc123"

    @patch("boto3.client")
    @patch("idp_common.config.configuration_manager.ConfigurationManager")
    def test_a_profile_with_no_history_returns_an_empty_list(
        self, mock_manager_class, mock_boto3
    ):
        _wire_stack(mock_boto3)
        mock_manager_class.return_value.list_revisions.return_value = []

        client = IDPClient(stack_name="test-stack")
        result = client.config.revisions(config_profile="untouched")

        assert result.count == 0
        assert result.revisions == []

    @patch("boto3.client")
    @patch("idp_common.config.configuration_manager.ConfigurationManager")
    def test_the_former_keyword_still_selects_the_profile(
        self, mock_manager_class, mock_boto3
    ):
        _wire_stack(mock_boto3)
        mock_manager_class.return_value.list_revisions.return_value = []

        client = IDPClient(stack_name="test-stack")
        result = client.config.revisions(config_version="lending")

        assert result.profile == "lending"

    def test_a_profile_is_required(self):
        # "Revisions of whatever is active" changes meaning the moment someone
        # activates another profile, so there is no useful default.
        client = IDPClient(stack_name="test-stack")
        with pytest.raises(ValueError):
            client.config.revisions()


@pytest.mark.unit
@pytest.mark.config
class TestRevisionNotes:
    """
    `handle_update_custom_configuration` accepted no notes at all, so EVERY
    ordinary edit — Web UI saves included — recorded a revision with an author and
    a timestamp but no statement of what changed. Only the special operations
    ("Reset to default", "Profile created", "Restored from r3") had any.
    """

    @patch("boto3.client")
    @patch("idp_common.config.configuration_manager.ConfigurationManager")
    def test_notes_reach_the_configuration_manager(
        self, mock_manager_class, mock_boto3, tmp_path
    ):
        _wire_stack(mock_boto3)
        manager = mock_manager_class.return_value
        manager.get_configuration.return_value = {"version": "lending"}
        manager.handle_update_custom_configuration.return_value = True
        manager.resolve_published_revision.return_value = 4

        config_file = tmp_path / "c.yaml"
        config_file.write_text("classes: []\n")

        client = IDPClient(stack_name="test-stack")
        client.config.upload(
            config_file=str(config_file),
            config_profile="lending",
            validate=False,
            description="Production tuning",
            revision_notes="raised topK to 20",
        )

        kwargs = manager.handle_update_custom_configuration.call_args.kwargs
        assert kwargs["revision_notes"] == "raised topK to 20"
        # Distinct fields: description belongs to the PROFILE and is overwritten by
        # every save; notes belong to the revision and are immutable.
        assert kwargs["description"] == "Production tuning"

    @patch("boto3.client")
    @patch("idp_common.config.configuration_manager.ConfigurationManager")
    def test_notes_are_optional(self, mock_manager_class, mock_boto3, tmp_path):
        _wire_stack(mock_boto3)
        manager = mock_manager_class.return_value
        manager.get_configuration.return_value = {"version": "lending"}
        manager.handle_update_custom_configuration.return_value = True

        config_file = tmp_path / "c.yaml"
        config_file.write_text("classes: []\n")

        client = IDPClient(stack_name="test-stack")
        client.config.upload(
            config_file=str(config_file), config_profile="lending", validate=False
        )

        assert (
            manager.handle_update_custom_configuration.call_args.kwargs[
                "revision_notes"
            ]
            is None
        )
