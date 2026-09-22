# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for `BDABlueprintCreator`, the client wrapper that creates and maintains
the Bedrock Data Automation blueprints a BDA-mode deployment extracts against.

A blueprint *is* the extraction contract in BDA mode: the project's
`customOutputConfiguration.blueprints` list decides which document types are
recognised and what fields come back. So the failures that matter here are not crashes
but **quiet wrong writes to that list** — a blueprint dropped from it, a stage or
version silently changed, or an update applied to a different region's project. None of
those raise, and the next document simply extracts differently.

Three things shape these tests.

**Every method's error contract is asserted, because they disagree with each other.**
`update_data_automation_project` and `update_project_with_custom_configurations` swallow
a `ClientError` and return `None`; `create_blueprint`, `update_blueprint`,
`get_blueprint`, `bulk_update_data_automation_project` and both
`create_blueprint_version*` re-raise it; `delete_blueprint` returns a bool; and
`list_all_blueprints_with_prefix` returns whatever it had collected. A caller that
assumes one shape gets a silent `None` from another, so the shape is pinned per method
rather than assumed uniform.

**The read-modify-write on the blueprint list is asserted on the payload sent**, not
just on the call happening. That list is read from the live project, edited in memory
and written back whole, so a bug there does not fail — it ships a project missing a
blueprint. The tests therefore inspect `update_data_automation_project`'s
`customOutputConfiguration` kwarg.

**`region=None` is a deliberate value, not a default to ignore.** It defers to boto3's
own resolution, which is right inside Lambda and wrong for `idp-cli config-sync-bda
--region …`, where blueprints would be created against the caller's default region
instead of the stack's. Both are pinned.

No AWS call is made: `boto3.client` is patched at the module boundary in every case.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from idp_common.bda.bda_blueprint_creator import BDABlueprintCreator

MODULE = "idp_common.bda.bda_blueprint_creator"

SCHEMA = {"class": "Invoice", "properties": {"total": {"type": "string"}}}


def _client_error(code: str = "ValidationException", op: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"{code}!"}}, op)


def _creator(region: str | None = "us-west-2") -> BDABlueprintCreator:
    """A creator whose bedrock client is a MagicMock, with no AWS call made."""
    with patch(f"{MODULE}.boto3.client"):
        creator = BDABlueprintCreator(region=region)
    creator.bedrock_client = MagicMock()
    return creator


def _project(**overrides) -> dict:
    project = {
        "projectDescription": "desc",
        "projectStage": "LIVE",
        "standardOutputConfiguration": {"document": {}},
        "overrideConfiguration": {"document": {"splitter": {"state": "ENABLED"}}},
        "customOutputConfiguration": {"blueprints": []},
    }
    project.update(overrides)
    return {"project": project}


def _sent_blueprints(creator: BDABlueprintCreator) -> list:
    """The blueprint list actually written back to the project."""
    kwargs = creator.bedrock_client.update_data_automation_project.call_args.kwargs
    return kwargs["customOutputConfiguration"]["blueprints"]


@pytest.mark.unit
class TestConstruction:
    """__init__: the region argument reaches boto3."""

    def test_an_explicit_region_is_passed_to_the_client(self):
        # `idp-cli config-sync-bda --region eu-west-1` must not create blueprints in
        # whatever region boto3 would otherwise resolve; they would be invisible to the
        # stack being configured.
        with patch(f"{MODULE}.boto3.client") as client:
            BDABlueprintCreator(region="eu-west-1")
        assert client.call_args.kwargs["region_name"] == "eu-west-1"
        assert client.call_args.kwargs["service_name"] == "bedrock-data-automation"

    def test_no_region_defers_to_boto3(self):
        # None is passed through rather than replaced with a hardcoded default, which
        # is what makes the Lambda case correct.
        with patch(f"{MODULE}.boto3.client") as client:
            creator = BDABlueprintCreator()
        assert client.call_args.kwargs["region_name"] is None
        assert creator.region is None


@pytest.mark.unit
class TestUpdateDataAutomationProject:
    """update_data_automation_project: the read-modify-write of the blueprint list."""

    def test_a_new_blueprint_is_appended_to_the_existing_list(self):
        creator = _creator()
        creator.bedrock_client.get_data_automation_project.return_value = _project(
            customOutputConfiguration={"blueprints": [{"blueprintArn": "arn:keep"}]}
        )
        result = creator.update_data_automation_project(
            "arn:project", {"blueprintArn": "arn:new"}
        )
        assert result == {"blueprintArn": "arn:new"}
        # The pre-existing entry must survive: dropping it would silently stop the
        # project recognising that document type.
        assert [bp["blueprintArn"] for bp in _sent_blueprints(creator)] == [
            "arn:keep",
            "arn:new",
        ]

    def test_an_existing_arn_is_replaced_rather_than_duplicated(self):
        creator = _creator()
        creator.bedrock_client.get_data_automation_project.return_value = _project(
            customOutputConfiguration={
                "blueprints": [{"blueprintArn": "arn:x", "blueprintVersion": "1"}]
            }
        )
        creator.update_data_automation_project(
            "arn:project", {"blueprintArn": "arn:x", "blueprintVersion": "2"}
        )
        sent = _sent_blueprints(creator)
        assert len(sent) == 1, f"the ARN was duplicated rather than replaced: {sent}"
        assert sent[0]["blueprintVersion"] == "2"

    @pytest.mark.parametrize(
        "config",
        [None, {}],
        ids=["customOutputConfiguration-absent", "customOutputConfiguration-empty"],
    )
    def test_a_project_with_no_blueprint_list_yet_is_initialised(self, config):
        # A brand-new project has no blueprints key. Both shapes the API can return are
        # covered, because the code tests for None on the outer key and on the inner
        # list separately.
        creator = _creator()
        creator.bedrock_client.get_data_automation_project.return_value = _project(
            customOutputConfiguration=config
        )
        creator.update_data_automation_project(
            "arn:project", {"blueprintArn": "arn:first"}
        )
        assert [bp["blueprintArn"] for bp in _sent_blueprints(creator)] == ["arn:first"]

    def test_stage_and_version_are_only_sent_when_present(self):
        # A falsy stage or version must be omitted rather than sent as None: the API
        # rejects an explicit null, and sending one would fail the whole project update
        # for an unrelated blueprint.
        creator = _creator()
        creator.bedrock_client.get_data_automation_project.return_value = _project()
        creator.update_data_automation_project(
            "arn:project",
            {"blueprintArn": "arn:x", "blueprintStage": None, "blueprintVersion": ""},
        )
        assert _sent_blueprints(creator) == [{"blueprintArn": "arn:x"}]

    def test_stage_and_version_are_forwarded_when_given(self):
        creator = _creator()
        creator.bedrock_client.get_data_automation_project.return_value = _project()
        creator.update_data_automation_project(
            "arn:project",
            {
                "blueprintArn": "arn:x",
                "blueprintStage": "LIVE",
                "blueprintVersion": "3",
            },
        )
        assert _sent_blueprints(creator) == [
            {"blueprintArn": "arn:x", "blueprintStage": "LIVE", "blueprintVersion": "3"}
        ]

    def test_the_rest_of_the_project_configuration_is_preserved(self):
        # Every one of these is re-sent on update, so a dropped key silently resets
        # project-wide behaviour -- the splitter especially, which decides whether a
        # multi-document packet is split at all.
        creator = _creator()
        creator.bedrock_client.get_data_automation_project.return_value = _project()
        creator.update_data_automation_project("arn:project", {"blueprintArn": "a"})
        kwargs = creator.bedrock_client.update_data_automation_project.call_args.kwargs
        assert kwargs["projectDescription"] == "desc"
        assert kwargs["projectStage"] == "LIVE"
        assert kwargs["standardOutputConfiguration"] == {"document": {}}
        assert kwargs["overrideConfiguration"] == {
            "document": {"splitter": {"state": "ENABLED"}}
        }

    def test_a_client_error_returns_none_rather_than_raising(self):
        # This method's contract, and it differs from most of the others in this class.
        creator = _creator()
        creator.bedrock_client.get_data_automation_project.side_effect = _client_error()
        assert (
            creator.update_data_automation_project("arn:p", {"blueprintArn": "a"})
            is None
        )

    def test_a_failure_on_the_write_also_returns_none(self):
        creator = _creator()
        creator.bedrock_client.get_data_automation_project.return_value = _project()
        creator.bedrock_client.update_data_automation_project.side_effect = (
            _client_error()
        )
        assert (
            creator.update_data_automation_project("arn:p", {"blueprintArn": "a"})
            is None
        )


@pytest.mark.unit
class TestUpdateProjectWithCustomConfigurations:
    """update_project_with_custom_configurations: wholesale replacement."""

    def test_the_supplied_configuration_replaces_the_projects_own(self):
        # Unlike the method above, this one does NOT merge -- it overwrites. Pinned
        # because the two have near-identical names and opposite semantics, so calling
        # the wrong one drops every blueprint the caller did not pass.
        creator = _creator()
        creator.bedrock_client.get_data_automation_project.return_value = _project(
            customOutputConfiguration={"blueprints": [{"blueprintArn": "arn:existing"}]}
        )
        replacement = {"blueprints": [{"blueprintArn": "arn:only"}]}
        creator.update_project_with_custom_configurations("arn:project", replacement)
        kwargs = creator.bedrock_client.update_data_automation_project.call_args.kwargs
        assert kwargs["customOutputConfiguration"] == replacement

    def test_it_returns_the_api_response(self):
        creator = _creator()
        creator.bedrock_client.get_data_automation_project.return_value = _project()
        creator.bedrock_client.update_data_automation_project.return_value = {"ok": 1}
        assert creator.update_project_with_custom_configurations("a", {}) == {"ok": 1}

    def test_a_client_error_returns_none(self):
        creator = _creator()
        creator.bedrock_client.get_data_automation_project.side_effect = _client_error()
        assert creator.update_project_with_custom_configurations("a", {}) is None


@pytest.mark.unit
class TestCreateDataAutomationProject:
    """create_data_automation_project: the shipped project defaults."""

    def test_the_blueprint_is_attached_at_the_live_stage(self):
        creator = _creator()
        creator.bedrock_client.create_data_automation_project.return_value = {"p": 1}
        assert creator.create_data_automation_project("n", "d", "arn:bp") == {"p": 1}
        kwargs = creator.bedrock_client.create_data_automation_project.call_args.kwargs
        assert kwargs["customOutputConfiguration"] == {
            "blueprints": [{"blueprintArn": "arn:bp", "blueprintStage": "LIVE"}]
        }
        assert kwargs["projectStage"] == "LIVE"

    def test_the_document_splitter_is_enabled(self):
        # With the splitter disabled a multi-document packet is treated as one
        # document, so every classification and extraction after the first is wrong.
        # It is a shipped default with nothing else asserting it.
        creator = _creator()
        creator.create_data_automation_project("n", "d", "arn:bp")
        kwargs = creator.bedrock_client.create_data_automation_project.call_args.kwargs
        assert kwargs["overrideConfiguration"] == {
            "document": {"splitter": {"state": "ENABLED"}}
        }

    def test_document_output_is_markdown_with_page_and_element_granularity(self):
        # The OCR text the pipeline consumes downstream is this format; changing it
        # would alter every extraction prompt without failing anything here.
        creator = _creator()
        creator.create_data_automation_project("n", "d", "arn:bp")
        kwargs = creator.bedrock_client.create_data_automation_project.call_args.kwargs
        document = kwargs["standardOutputConfiguration"]["document"]
        assert document["outputFormat"]["textFormat"]["types"] == ["MARKDOWN"]
        assert document["extraction"]["granularity"]["types"] == ["PAGE", "ELEMENT"]

    def test_a_client_error_returns_none(self):
        creator = _creator()
        creator.bedrock_client.create_data_automation_project.side_effect = (
            _client_error()
        )
        assert creator.create_data_automation_project("n", "d", "arn:bp") is None


@pytest.mark.unit
class TestCreateBlueprint:
    """create_blueprint: schema validation and the raise-rather-than-return contract."""

    def test_a_successful_create_returns_the_blueprint(self):
        creator = _creator()
        creator.bedrock_client.create_blueprint.return_value = {
            "blueprint": {"blueprintArn": "arn:new"}
        }
        result = creator.create_blueprint("DOCUMENT", "invoice-bp", schema=SCHEMA)
        assert result == {"status": "success", "blueprint": {"blueprintArn": "arn:new"}}
        kwargs = creator.bedrock_client.create_blueprint.call_args.kwargs
        assert kwargs == {
            "blueprintName": "invoice-bp",
            "type": "DOCUMENT",
            "blueprintStage": "LIVE",
            "schema": SCHEMA,
        }

    def test_a_missing_schema_raises_before_any_api_call(self):
        # `schema=None` is the default, so this is reachable by forgetting an argument.
        # Creating a blueprint with no schema would extract nothing from every matching
        # document, so refusing is right -- and it must refuse BEFORE the call, or a
        # half-made blueprint is left behind.
        creator = _creator()
        with pytest.raises(ValueError, match="Schema cannot be None"):
            creator.create_blueprint("DOCUMENT", "bp")
        creator.bedrock_client.create_blueprint.assert_not_called()

    def test_a_client_error_is_re_raised(self):
        creator = _creator()
        creator.bedrock_client.create_blueprint.side_effect = _client_error()
        with pytest.raises(ClientError):
            creator.create_blueprint("DOCUMENT", "bp", schema=SCHEMA)

    def test_a_malformed_response_raises(self):
        # No "blueprint" key at all: a KeyError becomes the generic re-raise. The
        # caller must not receive a success dict built from a response it never got.
        creator = _creator()
        creator.bedrock_client.create_blueprint.return_value = {}
        with pytest.raises(KeyError):
            creator.create_blueprint("DOCUMENT", "bp", schema=SCHEMA)


@pytest.mark.unit
class TestCreateBlueprintVersion:
    """The two version-creating methods, which differ only in the project update."""

    def test_creating_a_version_also_updates_the_project(self):
        creator = _creator()
        creator.bedrock_client.create_blueprint_version.return_value = {
            "blueprint": {"blueprintArn": "arn:v2", "blueprintVersion": "2"}
        }
        creator.bedrock_client.get_data_automation_project.return_value = _project()
        result = creator.create_blueprint_version("arn:bp", "arn:project")
        assert result["status"] == "success"
        # Without this the new version exists but nothing extracts against it.
        assert _sent_blueprints(creator) == [
            {"blueprintArn": "arn:v2", "blueprintVersion": "2"}
        ]

    def test_the_without_project_update_variant_makes_no_project_call(self):
        # It exists so parallel workers do not race on the project's blueprint list;
        # each read-modify-write would otherwise clobber the others. If this variant
        # started updating the project, that race would silently return.
        creator = _creator()
        creator.bedrock_client.create_blueprint_version.return_value = {
            "blueprint": {"blueprintArn": "arn:v2"}
        }
        result = creator.create_blueprint_version_without_project_update("arn:bp")
        assert result == {"status": "success", "blueprint": {"blueprintArn": "arn:v2"}}
        creator.bedrock_client.get_data_automation_project.assert_not_called()
        creator.bedrock_client.update_data_automation_project.assert_not_called()

    @pytest.mark.parametrize(
        "method",
        ["create_blueprint_version", "create_blueprint_version_without_project_update"],
    )
    def test_a_client_error_is_re_raised_by_both(self, method):
        creator = _creator()
        creator.bedrock_client.create_blueprint_version.side_effect = _client_error()
        args = ("arn:bp", "arn:project") if "without" not in method else ("arn:bp",)
        with pytest.raises(ClientError):
            getattr(creator, method)(*args)


@pytest.mark.unit
class TestBulkUpdateDataAutomationProject:
    """bulk_update_data_automation_project: one write for many blueprints."""

    def test_existing_and_new_blueprints_are_merged_by_arn(self):
        creator = _creator()
        creator.bedrock_client.get_data_automation_project.return_value = _project(
            customOutputConfiguration={
                "blueprints": [
                    {"blueprintArn": "arn:a", "blueprintVersion": "1"},
                    {"blueprintArn": "arn:keep"},
                ]
            }
        )
        result = creator.bulk_update_data_automation_project(
            "arn:project",
            [
                {"blueprintArn": "arn:a", "blueprintVersion": "2"},
                {"blueprintArn": "arn:b", "blueprintStage": "LIVE"},
            ],
        )
        by_arn = {bp["blueprintArn"]: bp for bp in _sent_blueprints(creator)}
        assert set(by_arn) == {"arn:a", "arn:keep", "arn:b"}
        assert by_arn["arn:a"]["blueprintVersion"] == "2", "the update did not apply"
        assert result == {"status": "success", "blueprints_count": 3}

    def test_an_untouched_existing_blueprint_keeps_its_full_entry(self):
        # The merge rebuilds each *supplied* entry from scratch but must pass existing
        # ones through unchanged; rebuilding those would drop their stage and version.
        creator = _creator()
        creator.bedrock_client.get_data_automation_project.return_value = _project(
            customOutputConfiguration={
                "blueprints": [
                    {
                        "blueprintArn": "arn:untouched",
                        "blueprintStage": "LIVE",
                        "blueprintVersion": "7",
                    }
                ]
            }
        )
        creator.bulk_update_data_automation_project(
            "arn:project", [{"blueprintArn": "arn:new"}]
        )
        by_arn = {bp["blueprintArn"]: bp for bp in _sent_blueprints(creator)}
        assert by_arn["arn:untouched"] == {
            "blueprintArn": "arn:untouched",
            "blueprintStage": "LIVE",
            "blueprintVersion": "7",
        }

    def test_an_empty_blueprint_list_still_writes_the_project(self):
        creator = _creator()
        creator.bedrock_client.get_data_automation_project.return_value = _project()
        result = creator.bulk_update_data_automation_project("arn:project", [])
        assert result == {"status": "success", "blueprints_count": 0}

    def test_a_project_with_no_configuration_yet_is_initialised(self):
        creator = _creator()
        creator.bedrock_client.get_data_automation_project.return_value = _project(
            customOutputConfiguration=None
        )
        creator.bulk_update_data_automation_project(
            "arn:project", [{"blueprintArn": "arn:a"}]
        )
        assert _sent_blueprints(creator) == [{"blueprintArn": "arn:a"}]

    def test_a_client_error_is_re_raised(self):
        # Note the contrast with the single-blueprint method above, which returns None
        # for the same failure. Both contracts are pinned so the difference is visible.
        creator = _creator()
        creator.bedrock_client.get_data_automation_project.side_effect = _client_error()
        with pytest.raises(ClientError):
            creator.bulk_update_data_automation_project("arn:p", [])


@pytest.mark.unit
class TestUpdateAndGetBlueprint:
    """update_blueprint and get_blueprint."""

    def test_update_forwards_the_stage_and_schema(self):
        creator = _creator()
        creator.bedrock_client.update_blueprint.return_value = {
            "blueprint": {"blueprintArn": "arn:x"}
        }
        result = creator.update_blueprint("arn:x", "DEVELOPMENT", SCHEMA)
        assert result == {"status": "success", "blueprint": {"blueprintArn": "arn:x"}}
        assert creator.bedrock_client.update_blueprint.call_args.kwargs == {
            "blueprintArn": "arn:x",
            "blueprintStage": "DEVELOPMENT",
            "schema": SCHEMA,
        }

    def test_get_forwards_the_stage(self):
        creator = _creator()
        creator.bedrock_client.get_blueprint.return_value = {
            "blueprint": {"blueprintArn": "arn:x"}
        }
        assert creator.get_blueprint("arn:x", "LIVE")["status"] == "success"
        assert creator.bedrock_client.get_blueprint.call_args.kwargs == {
            "blueprintArn": "arn:x",
            "blueprintStage": "LIVE",
        }

    @pytest.mark.parametrize("method", ["update_blueprint", "get_blueprint"])
    def test_a_client_error_is_re_raised(self, method):
        creator = _creator()
        getattr(creator.bedrock_client, method).side_effect = _client_error()
        args = (
            ("arn:x", "LIVE", SCHEMA)
            if method == "update_blueprint"
            else ("arn:x", "LIVE")
        )
        with pytest.raises(ClientError):
            getattr(creator, method)(*args)


@pytest.mark.unit
class TestListBlueprints:
    """list_blueprints: the project's own configuration."""

    def test_it_returns_the_projects_custom_output_configuration(self):
        creator = _creator()
        config = {"blueprints": [{"blueprintArn": "arn:a"}]}
        creator.bedrock_client.get_data_automation_project.return_value = _project(
            customOutputConfiguration=config
        )
        assert creator.list_blueprints("arn:project", "LIVE") == config

    def test_the_projectStage_argument_is_ignored(self):
        """`projectStage` is accepted and never used -- the call hardcodes LIVE.

        See #1126. Asserted in the direction that is true: a caller asking for
        DEVELOPMENT silently receives the LIVE project's configuration, which reads as
        an empty or stale blueprint list rather than as an error. Pinned here so the
        argument cannot be quietly removed (which would break callers) or start working
        (which would change what they receive) without a test saying so.
        """
        creator = _creator()
        creator.bedrock_client.get_data_automation_project.return_value = _project()
        creator.list_blueprints("arn:project", "DEVELOPMENT")
        assert (
            creator.bedrock_client.get_data_automation_project.call_args.kwargs[
                "projectStage"
            ]
            == "LIVE"
        )

    def test_an_error_is_re_raised(self):
        creator = _creator()
        creator.bedrock_client.get_data_automation_project.side_effect = RuntimeError(
            "boom"
        )
        with pytest.raises(RuntimeError):
            creator.list_blueprints("arn:project", "LIVE")


@pytest.mark.unit
class TestDeleteBlueprint:
    """delete_blueprint: versions first, then the base."""

    def test_every_version_is_deleted_before_the_base(self):
        # BDA refuses to delete a base blueprint while versions remain, so the order is
        # load-bearing rather than tidy.
        creator = _creator()
        assert creator.delete_blueprint("arn:bp", 3) is True
        calls = creator.bedrock_client.delete_blueprint.call_args_list
        versions = [c.kwargs.get("blueprintVersion") for c in calls]
        assert versions == ["1", "2", "3", None], (
            f"expected versions 1-3 then the base, got {versions}"
        )

    def test_the_current_version_is_included(self):
        # `range(1, n + 1)` rather than `range(1, n)`: an off-by-one here leaves the
        # newest version behind, and the base delete then fails for a reason the log
        # does not explain.
        creator = _creator()
        creator.delete_blueprint("arn:bp", 1)
        assert (
            creator.bedrock_client.delete_blueprint.call_args_list[0].kwargs[
                "blueprintVersion"
            ]
            == "1"
        )

    def test_a_version_given_as_a_string_is_accepted(self):
        # The API returns blueprintVersion as a string, so the caller usually has one.
        creator = _creator()
        assert creator.delete_blueprint("arn:bp", "2") is True
        assert len(creator.bedrock_client.delete_blueprint.call_args_list) == 3

    def test_a_failed_version_delete_does_not_stop_the_others(self, caplog):
        # Deliberate: one version failing should not abandon the rest. But the failure
        # is only a warning, so the base delete below is what actually reports trouble.
        creator = _creator()
        creator.bedrock_client.delete_blueprint.side_effect = [
            RuntimeError("no"),
            None,
            None,
        ]
        with caplog.at_level("WARNING"):
            assert creator.delete_blueprint("arn:bp", 2) is True
        assert any("Failed to delete version 1" in r.message for r in caplog.records)

    def test_a_failed_base_delete_returns_false(self):
        creator = _creator()
        creator.bedrock_client.delete_blueprint.side_effect = [None, RuntimeError("no")]
        assert creator.delete_blueprint("arn:bp", 1) is False

    def test_a_non_numeric_version_returns_false_without_deleting_anything(self):
        # `int(blueprint_version)` raises outside the per-version try, so the outer
        # handler returns False and the base blueprint is never touched -- which is the
        # safe direction: it does not delete a blueprint whose versions it could not
        # enumerate.
        creator = _creator()
        assert creator.delete_blueprint("arn:bp", "not-a-number") is False
        creator.bedrock_client.delete_blueprint.assert_not_called()


@pytest.mark.unit
class TestListAllBlueprintsWithPrefix:
    """list_all_blueprints_with_prefix: paginated, prefix-filtered, AWS-excluded."""

    @staticmethod
    def _paginate(creator, *pages):
        paginator = MagicMock()
        paginator.paginate.return_value = list(pages)
        creator.bedrock_client.get_paginator.return_value = paginator
        return paginator

    def test_results_are_collected_across_pages(self):
        # A single-page test would pass with the pagination dropped entirely, and a
        # partial list here means a stack teardown silently leaves blueprints behind.
        creator = _creator()
        self._paginate(
            creator,
            {"blueprints": [{"blueprintName": "idp-a", "blueprintArn": "arn:a"}]},
            {"blueprints": [{"blueprintName": "idp-b", "blueprintArn": "arn:b"}]},
        )
        found = creator.list_all_blueprints_with_prefix("idp-")
        assert [bp["blueprintName"] for bp in found] == ["idp-a", "idp-b"]

    def test_aws_standard_blueprints_are_skipped_even_when_the_name_matches(self):
        # The ARN check comes first for a reason: deleting an AWS-managed blueprint is
        # not the caller's to do, and a prefix collision is plausible.
        creator = _creator()
        self._paginate(
            creator,
            {
                "blueprints": [
                    {"blueprintName": "idp-x", "blueprintArn": "arn:aws:blueprint/x"},
                    {"blueprintName": "idp-y", "blueprintArn": "arn:custom:y"},
                ]
            },
        )
        found = creator.list_all_blueprints_with_prefix("idp-")
        assert [bp["blueprintName"] for bp in found] == ["idp-y"]

    def test_a_non_matching_prefix_is_excluded(self):
        creator = _creator()
        self._paginate(
            creator,
            {
                "blueprints": [
                    {"blueprintName": "other-a", "blueprintArn": "arn:a"},
                    {"blueprintName": "idp-b", "blueprintArn": "arn:b"},
                ]
            },
        )
        assert [
            bp["blueprintName"]
            for bp in creator.list_all_blueprints_with_prefix("idp-")
        ] == ["idp-b"]

    def test_the_live_stage_is_requested(self):
        creator = _creator()
        paginator = self._paginate(creator, {"blueprints": []})
        creator.list_all_blueprints_with_prefix("idp-")
        assert paginator.paginate.call_args.kwargs == {"blueprintStage": "LIVE"}

    def test_an_error_returns_what_was_collected_rather_than_raising(self):
        """The contract is a partial list, and the caller cannot tell it is partial.

        This method swallows the exception and returns `blueprints` as-is, so a
        throttle on page two yields a short list with no signal. Pinned because the
        caller is a teardown path: a short list means blueprints are left behind, and
        "found 1 blueprint" reads exactly like "there was 1 blueprint".
        """
        creator = _creator()
        paginator = MagicMock()
        paginator.paginate.side_effect = _client_error("ThrottlingException")
        creator.bedrock_client.get_paginator.return_value = paginator
        assert creator.list_all_blueprints_with_prefix("idp-") == []

    def test_a_page_with_no_blueprints_key_is_tolerated(self):
        creator = _creator()
        self._paginate(
            creator,
            {},
            {"blueprints": [{"blueprintName": "idp-a", "blueprintArn": "a"}]},
        )
        assert len(creator.list_all_blueprints_with_prefix("idp-")) == 1
