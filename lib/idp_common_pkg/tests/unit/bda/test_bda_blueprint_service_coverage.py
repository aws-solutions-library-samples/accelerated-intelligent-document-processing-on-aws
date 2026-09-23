# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the parts of `BdaBlueprintService` that decide what a BDA-mode
deployment can extract: project lifecycle, blueprint retrieval, the IDP↔BDA schema
transforms, and the three write paths that add, replace or delete blueprints.

A blueprint *is* the extraction contract in BDA mode — the project's
`customOutputConfiguration.blueprints` list decides which document types are
recognised and which fields come back — and this service is what writes that list. So
almost nothing here fails loudly. A definition dropped from a schema, a `$ref` left
pointing at a name that was renamed beside it, an AWS standard blueprint left
associated, a live blueprint classified as orphaned: each of those returns "success"
and changes what the next document extracts. These tests therefore assert the
*payload* that reaches `create_blueprint`, `update_blueprint`,
`update_project_with_custom_configurations`, `delete_blueprint` and
`handle_update_custom_configuration`, rather than that the call happened.

Five things shaped the choice of cases.

**The transform is lossy on purpose, and the loss is the thing to pin.** BDA supports
neither objects inside objects nor arrays inside object definitions, so
`_process_object_properties` and `_extract_complex_objects` *drop* such properties,
recording each in `_skipped_properties` — which is what reaches the caller as a
per-class warning. A test that only checked "transform produced a blueprint" would pass
whether one field or half the schema went missing, so each drop is asserted on the
surviving property set **and** on the warning that explains it. The warning half is not
decoration: a drop that is only logged reaches the user as `status: success` with no
warnings for a class whose entire line-items section has left the contract.

**Project identity is per config version and must be stable.** A version whose
recorded project ARN is re-created instead of reused ends up with its blueprints split
across two projects, and BDA extracts against whichever one the ARN in config names.
The reuse path, the ResourceNotFound replacement path, the exact DynamoDB key, and the
region the tracking row is read in (`idp-cli config-sync-bda --region` used to write it
to the ambient region) are each pinned separately.

**The error contracts here still disagree with each other, so each is asserted rather
than assumed.** `_retrieve_all_blueprints` raises — for a read failure and for a
missing project ARN alike — because an empty list is a statement about the project and
in replace mode it clears every IDP class; `_synchronize_deletes` returns the ARNs it
could not delete, since `delete_blueprint` reports failure by returning `False` and
those orphans are disassociated by then; the project-association step downgrades the
affected classes to `failed`; `_remove_aws_standard_blueprints_from_project` still
swallows; `cleanup_orphaned_blueprints` converts a failure into a result dict; and
`_convert_aws_standard_blueprints_to_custom` re-raises wrapped. What separates the
ones that raise from the ones that swallow is whether the caller can tell the failure
apart from a legitimate answer.

**`sync_mode` and `sync_direction` select which side is destroyed.** `replace` means
the source of truth wins and the other side's extra entries are deleted; `merge` means
nothing is deleted. Those pairs are tested against each other — the same fixture run
both ways — because a mode that quietly behaves like the other one is exactly the
failure that costs a user their classes or their blueprints.

**Nothing here is left uncovered as dead.** Three statements that used to be
unreachable are gone rather than untested: the
`"instruction" not in result` fallback in `_process_array_property_simple` (which ran
after `_add_bda_fields_to_schema` had already defaulted it), the
`classess.extend(classess_added)` branch in `create_blueprints_from_custom_configuration`
(whose list was never appended to), and `_process_array_property`'s degrade-to-a-typed-
array branch for nesting items, which its only caller drops before delegating on the
same four conditions. That last one mattered more than dead code usually does: it was a
second, different intended behaviour for one input shape.

No AWS call is made: `boto3` is patched at the module boundary and both collaborators
(`BDABlueprintCreator`, `ConfigurationManager`) are mocks.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from idp_common.bda.bda_blueprint_service import BdaBlueprintService

MODULE = "idp_common.bda.bda_blueprint_service"

PROJECT_ARN = "arn:aws:bedrock:us-west-2:123456789012:data-automation-project/p1"
# Deliberately different from PROJECT_ARN: a reuse assertion that named the ARN the
# service was constructed with would also pass if the method simply returned that.
RECORDED_ARN = "arn:aws:bedrock:us-west-2:123456789012:data-automation-project/rec"


def _client_error(code: str = "ValidationException", op: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"{code}!"}}, op)


def _service(region: str | None = "us-west-2", prefix: str = "idp") -> Any:
    """A service whose two collaborators are mocks and which touches no AWS."""
    with (
        patch(f"{MODULE}.BDABlueprintCreator"),
        patch(f"{MODULE}.ConfigurationManager"),
        patch.dict(
            "os.environ",
            {"CONFIGURATION_TABLE_NAME": "config-table", "STACK_NAME": prefix},
        ),
    ):
        service = BdaBlueprintService(
            dataAutomationProjectArn=PROJECT_ARN, region=region
        )
    service.blueprint_creator = MagicMock()
    service.config_manager = MagicMock()
    service.config_manager.get_configuration.return_value = None
    return service


def _idp_class(
    class_id: str = "Invoice",
    properties: dict | None = None,
    description: str = "An invoice",
    **extra,
) -> dict:
    """An IDP document class schema as it is stored in the configuration table."""
    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": class_id,
        "x-aws-idp-document-type": class_id,
        "description": description,
        "type": "object",
        "properties": properties
        if properties is not None
        else {"total": {"type": "string", "description": "Invoice total"}},
    }
    schema.update(extra)
    return schema


def _class_with_nested_definition(class_id: str = "Invoice") -> dict:
    """A class whose `$defs` definition holds an object BDA cannot represent.

    Only the `$defs` route reaches `_process_object_properties`, which is the one
    place a dropped property is *recorded* in `_skipped_properties`; a nested object
    written inline at the top level is dropped by `_extract_complex_objects` with no
    warning at all.
    """
    return _idp_class(
        class_id=class_id,
        properties={"party": {"$ref": "#/$defs/Party"}},
        **{
            "$defs": {
                "Party": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Name"},
                        "address": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                    },
                }
            }
        },
    )


def _bda_blueprint(
    class_name: str = "Invoice",
    arn: str | None = None,
    name: str | None = None,
    version: str = "1",
    schema: dict | str | None = None,
) -> dict:
    """A blueprint as `list_blueprints` + `get_blueprint` together describe one."""
    return {
        "blueprintArn": arn or f"arn:aws:bedrock:::blueprint/idp-{class_name}-aaaa1111",
        "blueprintName": name or f"idp-{class_name}-aaaa1111",
        "blueprintVersion": version,
        "schema": schema
        if schema is not None
        else {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "class": class_name,
            "description": f"{class_name} document",
            "type": "object",
            "properties": {
                "total": {
                    "type": "string",
                    "inferenceType": "explicit",
                    "instruction": "The total",
                }
            },
        },
    }


def _wire_project(service: Any, blueprints: list[dict]) -> None:
    """Make `_retrieve_all_blueprints` see exactly `blueprints` in the project."""
    service.blueprint_creator.list_blueprints.return_value = {
        "blueprints": [
            {
                "blueprintArn": bp["blueprintArn"],
                "blueprintName": bp["blueprintName"],
                "blueprintVersion": bp["blueprintVersion"],
            }
            for bp in blueprints
        ]
    }
    by_arn = {bp["blueprintArn"]: bp for bp in blueprints}

    def _get(blueprint_arn: str, stage: str = "LIVE") -> dict:
        found = by_arn[blueprint_arn]
        return {
            "blueprint": {
                "blueprintArn": found["blueprintArn"],
                "blueprintName": found["blueprintName"],
                "schema": found["schema"],
            }
        }

    service.blueprint_creator.get_blueprint.side_effect = _get


def _created_schema(service: Any, call_index: int = 0) -> dict:
    """The blueprint schema handed to `create_blueprint` on a given call."""
    call = service.blueprint_creator.create_blueprint.call_args_list[call_index]
    return json.loads(call.kwargs["schema"])


def _refs(obj: Any) -> list[str]:
    """Every `$ref` string anywhere in a schema."""
    found = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "$ref" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_refs(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_refs(item))
    return found


# ---------------------------------------------------------------------------
# get_or_create_project_for_version
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetOrCreateProjectForVersion:
    """Each config version owns one BDA project, tracked in the ConfigurationTable.

    Losing that identity is not a crash: the version gets a second project, its
    blueprints are split across the two, and BDA extracts against whichever ARN the
    caller happens to hold.
    """

    @pytest.fixture
    def service(self) -> Any:
        return _service()

    @pytest.fixture
    def table(self) -> Any:
        return MagicMock()

    def _run(self, service: Any, table: Any, version: str = "v1") -> Any:
        with (
            patch("boto3.resource") as resource,
            patch.dict(
                "os.environ",
                {"CONFIGURATION_TABLE_NAME": "config-table", "STACK_NAME": "idp"},
            ),
        ):
            resource.return_value.Table.return_value = table
            result = service.get_or_create_project_for_version(version)
            self._resource = resource
        return result

    def _wire_creation(self, service: Any, project_arn: str = PROJECT_ARN) -> None:
        service.blueprint_creator.create_blueprint.return_value = {
            "status": "success",
            "blueprint": {"blueprintArn": "arn:aws:bedrock:::blueprint/bootstrap-1"},
        }
        service.blueprint_creator.create_data_automation_project.return_value = {
            "projectArn": project_arn
        }

    def test_a_recorded_project_that_still_exists_is_reused(self, service, table):
        """The whole point of the tracking row: do not build a second project."""
        table.get_item.return_value = {"Item": {"ProjectArn": RECORDED_ARN}}

        result = self._run(service, table, "v1")

        assert result == RECORDED_ARN
        service.blueprint_creator.bedrock_client.get_data_automation_project.assert_called_once_with(
            projectArn=RECORDED_ARN, projectStage="LIVE"
        )
        service.blueprint_creator.create_data_automation_project.assert_not_called()
        service.blueprint_creator.create_blueprint.assert_not_called()
        table.put_item.assert_not_called()

    def test_the_tracking_row_is_keyed_by_the_version_name(self, service, table):
        """A wrong key finds nothing, so every sync would create a new project."""
        table.get_item.return_value = {"Item": {"ProjectArn": RECORDED_ARN}}

        self._run(service, table, "production")

        assert table.get_item.call_args.kwargs["Key"] == {
            "Configuration": "BdaProject#production"
        }

    def test_the_tracking_table_is_resolved_in_the_services_region(self, table):
        """`idp-cli config-sync-bda --region eu-west-1` must not read us-east-1.

        The table name is not region-qualified, so a resource built without
        `region_name` silently answers about a different account's table — reporting
        "no project" for a version that has one, or handing back an ARN from the
        wrong region.
        """
        service = _service(region="eu-west-1")
        table.get_item.return_value = {"Item": {"ProjectArn": RECORDED_ARN}}

        self._run(service, table, "v1")

        assert self._resource.call_args.args[0] == "dynamodb"
        assert self._resource.call_args.kwargs["region_name"] == "eu-west-1"

    def test_a_recorded_project_that_no_longer_exists_is_replaced(self, service, table):
        """A stale ARN must not be handed back; every later call would fail on it."""
        stale = "arn:aws:bedrock:::data-automation-project/deleted"
        table.get_item.return_value = {"Item": {"ProjectArn": stale}}
        service.blueprint_creator.bedrock_client.get_data_automation_project.side_effect = _client_error(
            "ResourceNotFoundException", "GetDataAutomationProject"
        )
        self._wire_creation(service, project_arn="arn:aws:bedrock:::project/fresh")

        result = self._run(service, table, "v1")

        assert result == "arn:aws:bedrock:::project/fresh"
        assert table.put_item.call_args.kwargs["Item"]["ProjectArn"] == (
            "arn:aws:bedrock:::project/fresh"
        )

    @pytest.mark.parametrize(
        "code", ["ThrottlingException", "AccessDeniedException", "InternalServerError"]
    )
    def test_an_inconclusive_verification_does_not_create_a_second_project(
        self, service, table, code
    ):
        """Only "the project is gone" may lead to a replacement.

        A throttle, an AccessDenied or a server error on `GetDataAutomationProject`
        says nothing about whether the recorded project still exists. Creating one
        anyway overwrites the tracking row and orphans the first project's
        blueprints while the version's config names the new, empty one — and the
        sync reports success, because nothing raised. The assertion is on the
        second project *not* being created, which is the thing that was lost.
        """
        table.get_item.return_value = {"Item": {"ProjectArn": RECORDED_ARN}}
        service.blueprint_creator.bedrock_client.get_data_automation_project.side_effect = _client_error(
            code, "GetDataAutomationProject"
        )
        self._wire_creation(service, project_arn="arn:aws:bedrock:::project/second")

        with pytest.raises(ClientError):
            self._run(service, table, "v1")

        service.blueprint_creator.create_data_automation_project.assert_not_called()
        table.put_item.assert_not_called()

    def test_an_unreadable_tracking_row_falls_through_to_creation(self, service, table):
        table.get_item.side_effect = _client_error("ProvisionedThroughput", "GetItem")
        self._wire_creation(service, project_arn="arn:aws:bedrock:::project/new")

        assert self._run(service, table, "v1") == "arn:aws:bedrock:::project/new"

    def test_a_row_without_a_project_arn_is_treated_as_absent(self, service, table):
        """A tracking row can exist with no ARN; that must not be returned as one."""
        table.get_item.return_value = {"Item": {"Configuration": "BdaProject#v1"}}
        self._wire_creation(service, project_arn="arn:aws:bedrock:::project/new")

        assert self._run(service, table, "v1") == "arn:aws:bedrock:::project/new"
        service.blueprint_creator.bedrock_client.get_data_automation_project.assert_not_called()

    def test_the_project_name_maps_version_punctuation_to_hyphens(self, service, table):
        """Project names allow only `[a-zA-Z0-9-]`, so `_` becomes `-` here.

        Deliberately unlike `_sanitize_class_name`, which preserves `_` because BDA
        permits it in a *blueprint* name. Using the class rule here would produce a
        project name CreateDataAutomationProject rejects.
        """
        table.get_item.return_value = {}
        self._wire_creation(service)

        self._run(service, table, "prod/v1_beta test")

        kwargs = (
            service.blueprint_creator.create_data_automation_project.call_args.kwargs
        )
        assert kwargs["project_name"] == "idp-prod-v1-beta-test"
        assert "prod/v1_beta test" in kwargs["description"]

    def test_the_bootstrap_blueprint_is_valid_and_then_deleted(self, service, table):
        """BDA needs one blueprint to create a project; leaving it behind would add a
        `Bootstrap` document type to the extraction contract."""
        table.get_item.return_value = {}
        self._wire_creation(service)

        self._run(service, table, "v1")

        create = service.blueprint_creator.create_blueprint.call_args.kwargs
        schema = json.loads(create["schema"])
        assert schema["class"] == "Bootstrap"
        assert schema["properties"]["placeholder"]["inferenceType"] == "explicit"
        assert create["blueprint_name"].startswith("idp-v1-bootstrap-")
        service.blueprint_creator.bedrock_client.delete_blueprint.assert_called_once_with(
            blueprintArn="arn:aws:bedrock:::blueprint/bootstrap-1"
        )

    def test_the_project_is_created_from_the_bootstrap_blueprint(self, service, table):
        table.get_item.return_value = {}
        self._wire_creation(service)

        self._run(service, table, "v1")

        kwargs = (
            service.blueprint_creator.create_data_automation_project.call_args.kwargs
        )
        assert kwargs["blueprint_arn"] == "arn:aws:bedrock:::blueprint/bootstrap-1"

    def test_a_bootstrap_failure_raises_before_a_project_is_created(
        self, service, table
    ):
        table.get_item.return_value = {}
        service.blueprint_creator.create_blueprint.return_value = None

        with pytest.raises(RuntimeError, match="bootstrap blueprint"):
            self._run(service, table, "v1")

        service.blueprint_creator.create_data_automation_project.assert_not_called()

    def test_a_project_create_returning_nothing_raises(self, service, table):
        table.get_item.return_value = {}
        self._wire_creation(service)
        service.blueprint_creator.create_data_automation_project.return_value = None

        with pytest.raises(RuntimeError, match="Failed to create BDA project"):
            self._run(service, table, "v1")

        table.put_item.assert_not_called()

    def test_a_project_create_returning_no_arn_raises_rather_than_recording_none(
        self, service, table
    ):
        """Recording `ProjectArn: None` would make every later lookup hand back
        `None`, which fails several frames away in `_blueprint_lookup`."""
        table.get_item.return_value = {}
        self._wire_creation(service)
        service.blueprint_creator.create_data_automation_project.return_value = {
            "projectStage": "LIVE"
        }

        with pytest.raises(RuntimeError, match="No projectArn"):
            self._run(service, table, "v1")

        table.put_item.assert_not_called()

    def test_the_tracking_row_records_arn_name_and_version(self, service, table):
        table.get_item.return_value = {}
        self._wire_creation(service, project_arn="arn:aws:bedrock:::project/new")

        self._run(service, table, "v1")

        assert table.put_item.call_args.kwargs["Item"] == {
            "Configuration": "BdaProject#v1",
            "ProjectArn": "arn:aws:bedrock:::project/new",
            "ProjectName": "idp-v1",
            "VersionName": "v1",
        }

    def test_a_failure_to_record_still_returns_the_created_project(
        self, service, table
    ):
        """The project exists in AWS by then; raising would leak it untracked."""
        table.get_item.return_value = {}
        self._wire_creation(service, project_arn="arn:aws:bedrock:::project/new")
        table.put_item.side_effect = _client_error("AccessDenied", "PutItem")

        assert self._run(service, table, "v1") == "arn:aws:bedrock:::project/new"

    def test_a_bootstrap_cleanup_failure_does_not_fail_the_call(self, service, table):
        table.get_item.return_value = {}
        self._wire_creation(service, project_arn="arn:aws:bedrock:::project/new")
        service.blueprint_creator.bedrock_client.delete_blueprint.side_effect = (
            RuntimeError("still associated")
        )

        assert self._run(service, table, "v1") == "arn:aws:bedrock:::project/new"


# ---------------------------------------------------------------------------
# _retrieve_all_blueprints
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRetrieveAllBlueprints:
    """What this returns is the sync's entire view of BDA.

    An under-reported list makes `_synchronize_deletes` believe nothing exists (so it
    deletes nothing) and makes replace-mode BDA→IDP believe the project is empty (so it
    clears every IDP class).
    """

    @pytest.fixture
    def service(self) -> Any:
        return _service()

    def test_aws_standard_blueprints_are_excluded_by_default(self, service):
        """An AWS standard blueprint pulled into the IDP class list would appear as a
        document class nobody authored, and the next sync would try to own it."""
        custom = _bda_blueprint("Invoice")
        standard = _bda_blueprint(
            "Payslip",
            arn="arn:aws:bedrock:us-west-2:aws:blueprint/bedrock-data-insights-payslip",
            name="payslip",
        )
        _wire_project(service, [custom, standard])

        result = service._retrieve_all_blueprints(PROJECT_ARN)

        assert [bp["blueprintName"] for bp in result] == ["idp-Invoice-aaaa1111"]
        fetched = [
            call.kwargs["blueprint_arn"]
            for call in service.blueprint_creator.get_blueprint.call_args_list
        ]
        assert fetched == [custom["blueprintArn"]]

    def test_aws_standard_blueprints_are_included_when_asked_for(self, service):
        custom = _bda_blueprint("Invoice")
        standard = _bda_blueprint(
            "Payslip",
            arn="arn:aws:bedrock:us-west-2:aws:blueprint/bedrock-data-insights-payslip",
            name="payslip",
        )
        _wire_project(service, [custom, standard])

        result = service._retrieve_all_blueprints(
            PROJECT_ARN, include_aws_standard=True
        )

        assert sorted(bp["blueprintName"] for bp in result) == [
            "idp-Invoice-aaaa1111",
            "payslip",
        ]

    def test_the_listed_version_is_carried_onto_the_fetched_blueprint(self, service):
        """`GetBlueprint` does not report the version, but `delete_blueprint` and the
        project association both need it; defaulting it to 1 deletes the wrong one."""
        _wire_project(service, [_bda_blueprint("Invoice", version="7")])

        result = service._retrieve_all_blueprints(PROJECT_ARN)

        assert result[0]["blueprintVersion"] == "7"

    def test_a_blueprint_listed_without_a_version_defaults_to_one(self, service):
        service.blueprint_creator.list_blueprints.return_value = {
            "blueprints": [
                {"blueprintArn": "arn:aws:bedrock:::blueprint/idp-Invoice-1"}
            ]
        }
        service.blueprint_creator.get_blueprint.return_value = {
            "blueprint": {"blueprintName": "idp-Invoice-1", "schema": "{}"}
        }

        result = service._retrieve_all_blueprints(PROJECT_ARN)

        assert result[0]["blueprintVersion"] == "1"

    def test_a_read_failure_raises_rather_than_answering_an_empty_list(self, service):
        """An empty list is an answer about the project, not about the read.

        `[]` is how a caller learns a project associates no blueprints, and in
        replace mode that clears every IDP class; returning it for an AccessDenied
        therefore deleted the user's configuration on a transient error. In phase 2
        it also hid every existing blueprint from `_blueprint_lookup`, so the sync
        created a second blueprint for every class.
        """
        service.blueprint_creator.list_blueprints.side_effect = _client_error(
            "AccessDeniedException", "ListBlueprints"
        )

        with pytest.raises(ClientError):
            service._retrieve_all_blueprints(PROJECT_ARN)

    def test_no_project_arn_raises_rather_than_answering_at_all(self, service):
        """There is no honest answer for a missing ARN.

        This used to return `None` for a falsy ARN and `[]` for a read failure —
        two different shapes for two different non-answers, one of which failed two
        frames later in `_blueprint_lookup` on an unrelated `TypeError`.
        `get_or_create_project_for_version`'s docstring cited that `TypeError` as
        the reason it raises rather than returning a `None` ARN.
        """
        with pytest.raises(ValueError, match="project ARN is required"):
            service._retrieve_all_blueprints("")

    def test_a_project_with_no_custom_output_configuration_has_no_blueprints(
        self, service
    ):
        """`customOutputConfiguration` is optional on the API response, so
        `list_blueprints` answers `None` for a project configured for standard
        output only. That is a project with no blueprints; reading `.get` on it
        raised an `AttributeError` that the outer handler turned into `[]` — the
        same empty view, arrived at by a crash."""
        service.blueprint_creator.list_blueprints.return_value = None

        assert service._retrieve_all_blueprints(PROJECT_ARN) == []

    def test_one_blueprint_without_an_arn_does_not_hide_the_others(self, service):
        """A malformed entry costs that entry only.

        The AWS-standard filter tested `"aws:blueprint" in blueprint_arn` against a
        value that could be `None`; the `TypeError` was caught by the method's outer
        handler, which answered `[]`, so one bad entry made every *other* blueprint
        in the project invisible — and in replace mode an empty view clears all IDP
        classes. The assertion is on the good blueprint surviving.

        The service model declares `blueprintArn` required on this response, so this
        is defensive rather than an observed input; what it pins is that one
        unusable entry cannot take the project's other blueprints with it.
        """
        good = _bda_blueprint("Invoice")
        service.blueprint_creator.list_blueprints.return_value = {
            "blueprints": [
                {"blueprintArn": good["blueprintArn"], "blueprintVersion": "1"},
                {"blueprintVersion": "1"},
            ]
        }
        service.blueprint_creator.get_blueprint.return_value = {
            "blueprint": {"blueprintName": good["blueprintName"], "schema": "{}"}
        }

        result = service._retrieve_all_blueprints(PROJECT_ARN)

        assert [bp["blueprintName"] for bp in result] == [good["blueprintName"]]


# ---------------------------------------------------------------------------
# Schema hygiene: extension stripping and definition-name sanitization
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStripIdpExtensionFields:
    """`CreateBlueprint` rejects a schema carrying unknown fields outright, so a
    field left in place costs that class its blueprint entirely."""

    @pytest.fixture
    def service(self) -> Any:
        return _service()

    def test_extension_format_and_required_are_stripped_at_every_depth(self, service):
        schema = {
            "$id": "Invoice",
            "x-aws-idp-document-type": "Invoice",
            "required": ["total"],
            "properties": {
                "total": {
                    "type": "string",
                    "format": "currency",
                    "x-aws-idp-evaluation-method": "EXACT",
                },
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["amount"],
                        "properties": {
                            "amount": {"type": "string", "format": "number"}
                        },
                    },
                },
                "either": {
                    "anyOf": [
                        {"type": "string", "x-aws-idp-list-item-description": "x"},
                        {"type": "number"},
                    ]
                },
            },
        }

        result = service._strip_idp_extension_fields(schema)

        assert "x-aws-idp-document-type" not in result
        assert "required" not in result
        assert result["properties"]["total"] == {"type": "string"}
        items = result["properties"]["rows"]["items"]
        assert "required" not in items
        assert items["properties"]["amount"] == {"type": "string"}
        assert result["properties"]["either"]["anyOf"][0] == {"type": "string"}

    def test_the_description_survives_stripping(self, service):
        """`description` becomes BDA's `instruction`, which is the whole prompt for a
        field. Stripping it would leave BDA extracting against a bare name."""
        schema = {"properties": {"total": {"type": "string", "description": "The sum"}}}

        result = service._strip_idp_extension_fields(schema)

        assert result["properties"]["total"]["description"] == "The sum"

    def test_a_field_merely_containing_the_prefix_is_kept(self, service):
        """The rule is a prefix test; a substring test would eat a user's own key."""
        schema = {"properties": {"notes-x-aws-idp-style": {"type": "string"}}}

        result = service._strip_idp_extension_fields(schema)

        assert "notes-x-aws-idp-style" in result["properties"]

    def test_a_non_dict_is_returned_unchanged(self, service):
        assert service._strip_idp_extension_fields("leaf") == "leaf"


@pytest.mark.unit
class TestSanitizeDefNames:
    """A definition key is renamed to satisfy BDA; every `$ref` to it must move too.

    A `$ref` left pointing at the old key is either rejected by `CreateBlueprint` or,
    worse, accepted against an undefined target — and the section it names comes back
    empty for every document.
    """

    @pytest.fixture
    def service(self) -> Any:
        return _service()

    def test_a_defs_name_with_a_space_is_renamed_and_its_refs_follow(self, service):
        schema = {
            "$defs": {"Line Items": {"type": "object", "properties": {}}},
            "properties": {"lines": {"$ref": "#/$defs/Line Items"}},
        }

        result = service._sanitize_def_names(schema)

        assert list(result["$defs"]) == ["LineItems"]
        assert _refs(result) == ["#/$defs/LineItems"]

    def test_refs_nested_in_lists_and_items_are_updated_too(self, service):
        schema = {
            "$defs": {"Tax, Total": {"type": "object", "properties": {}}},
            "properties": {
                "rows": {"type": "array", "items": {"$ref": "#/$defs/Tax, Total"}},
                "either": {"anyOf": [{"$ref": "#/$defs/Tax, Total"}]},
            },
        }

        result = service._sanitize_def_names(schema)

        assert set(_refs(result)) == {"#/$defs/TaxTotal"}

    def test_draft07_definitions_use_the_matching_ref_prefix(self, service):
        """A draft-07 schema's refs read `#/definitions/…`; rewriting them with the
        `$defs` prefix would leave every one of them dangling."""
        schema = {
            "definitions": {"Line Items": {"type": "object", "properties": {}}},
            "properties": {"lines": {"$ref": "#/definitions/Line Items"}},
        }

        result = service._sanitize_def_names(schema)

        assert list(result["definitions"]) == ["LineItems"]
        assert _refs(result) == ["#/definitions/LineItems"]

    def test_a_ref_with_an_unrelated_prefix_is_left_alone(self, service):
        schema = {
            "$defs": {"Line Items": {"type": "object", "properties": {}}},
            "properties": {
                "lines": {"$ref": "#/$defs/Line Items"},
                "other": {"$ref": "https://example.com/Line Items"},
            },
        }

        result = service._sanitize_def_names(schema)

        assert result["properties"]["other"]["$ref"] == "https://example.com/Line Items"

    def test_valid_names_are_not_touched(self, service):
        schema = {
            "$defs": {"LineItems": {"type": "object", "properties": {}}},
            "properties": {"lines": {"$ref": "#/$defs/LineItems"}},
        }

        result = service._sanitize_def_names(schema)

        assert list(result["$defs"]) == ["LineItems"]
        assert _refs(result) == ["#/$defs/LineItems"]

    def test_a_schema_with_no_definitions_is_returned_unchanged(self, service):
        schema = {"properties": {"total": {"type": "string"}}}

        assert service._sanitize_def_names(schema) == schema


# ---------------------------------------------------------------------------
# The lossy parts of the IDP → BDA transform
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTransformDropsUnsupportedStructures:
    """BDA supports neither an object inside an object nor an array inside an object
    definition, so the transform drops those properties and records a warning.

    Nothing raises. The blueprint is created, the document is processed, and the
    dropped fields are simply absent from every result — which is why each drop is
    asserted on the surviving property set *and* on the warning that explains it.
    """

    @pytest.fixture
    def service(self) -> Any:
        return _service()

    def test_a_nested_object_inside_a_definition_is_dropped_and_reported(self, service):
        service._current_class = "Invoice"
        schema = _idp_class(
            properties={"party": {"$ref": "#/$defs/Party"}},
            **{
                "$defs": {
                    "Party": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Name"},
                            "address": {
                                "type": "object",
                                "properties": {"city": {"type": "string"}},
                            },
                        },
                    }
                }
            },
        )

        blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)

        assert list(blueprint["definitions"]["Party"]["properties"]) == ["name"]
        warnings = [
            w for w in service._skipped_properties if w["property"] == "address"
        ]
        assert warnings and warnings[0]["type"] == "nested_object"
        assert warnings[0]["class"] == "Invoice"
        assert "does not support nested objects" in warnings[0]["message"]

    def test_a_nested_array_inside_a_definition_is_dropped_and_reported(self, service):
        service._current_class = "Invoice"
        schema = _idp_class(
            properties={"party": {"$ref": "#/$defs/Party"}},
            **{
                "$defs": {
                    "Party": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "phones": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Phone numbers",
                            },
                        },
                    }
                }
            },
        )

        blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)

        assert list(blueprint["definitions"]["Party"]["properties"]) == ["name"]
        warnings = [w for w in service._skipped_properties if w["property"] == "phones"]
        assert warnings and warnings[0]["type"] == "nested_array"

    def test_a_ref_inside_a_definition_is_rewritten_to_the_draft07_path(self, service):
        """A `#/$defs/…` ref in a draft-07 blueprint resolves to nothing."""
        result = service._process_object_properties(
            {"child": {"$ref": "#/$defs/Child"}}
        )

        assert result == {"child": {"$ref": "#/definitions/Child", "instruction": "-"}}

    def test_a_leaf_inside_a_definition_takes_its_instruction_from_its_description(
        self, service
    ):
        result = service._process_object_properties(
            {"name": {"type": "string", "description": "Legal name"}}
        )

        assert result["name"] == {
            "type": "string",
            "inferenceType": "explicit",
            "instruction": "Legal name",
        }

    def test_a_leaf_with_no_description_gets_a_generic_instruction(self, service):
        result = service._process_object_properties({"name": {"type": "string"}})

        assert result["name"]["instruction"] == "Extract this field from the document"

    def test_a_top_level_object_whose_children_nest_is_dropped_with_a_warning(
        self, service
    ):
        """The section disappears from the contract — not just its nested child.

        The warning is the half that matters. `_process_single_class` reports only
        what is in `_skipped_properties`, so a drop that was merely logged reached
        the user as `status: success` with no warnings for a class whose entire
        section had left the extraction contract.
        """
        simple, defs = service._extract_complex_objects(
            {
                "party": {
                    "type": "object",
                    "properties": {
                        "address": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        }
                    },
                },
                "total": {"type": "string"},
            }
        )

        assert list(simple) == ["total"]
        assert defs == {}
        dropped = [w for w in service._skipped_properties if w["property"] == "party"]
        assert dropped, "the dropped section was not reported to the caller"
        assert dropped[0]["type"] == "nested_object"

    def test_a_flat_top_level_object_becomes_a_definition_and_a_ref(self, service):
        simple, defs = service._extract_complex_objects(
            {
                "party": {
                    "type": "object",
                    "description": "The payer",
                    "properties": {"name": {"type": "string"}},
                }
            }
        )

        assert simple["party"] == {"$ref": "#/definitions/party"}
        assert defs["party"]["description"] == "The payer"
        assert defs["party"]["properties"]["name"]["inferenceType"] == "explicit"

    def test_an_array_of_objects_that_nest_is_dropped_with_a_warning(self, service):
        """The rows are not degraded, they are gone: no `$ref`, no definition, and no
        property at all for the array — which for a line-items section is the whole
        table. Reported as a warning for the same reason as the object case above."""
        simple, defs = service._extract_complex_objects(
            {
                "rows": {
                    "type": "array",
                    "description": "Line items",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sub": {
                                "type": "object",
                                "properties": {"a": {"type": "string"}},
                            }
                        },
                    },
                }
            }
        )

        assert simple == {}
        assert defs == {}
        dropped = [w for w in service._skipped_properties if w["property"] == "rows"]
        assert dropped, "the dropped line-items section was not reported to the caller"
        assert dropped[0]["type"] == "nested_array"

    def test_a_non_dict_property_value_is_skipped_by_name_rather_than_failing(
        self, service
    ):
        """A property whose value is not a schema object cannot be described to BDA.

        Passing it through used to hand a `None` (or a bare string) to
        `_process_flat_schema`, which called `.get` on it: the class was reported
        failed with a raw `TypeError` / `AttributeError` naming neither the class nor
        the property. The rest of the schema is kept and the offending property is
        named.
        """
        blueprint = service._transform_json_schema_to_bedrock_blueprint(
            _idp_class(properties={"broken": None, "total": {"type": "string"}})
        )

        assert list(blueprint["properties"]) == ["total"]
        dropped = [w for w in service._skipped_properties if w["property"] == "broken"]
        assert dropped, "the unusable property was dropped without telling the caller"
        assert dropped[0]["type"] == "invalid_property_schema"
        assert "NoneType" in dropped[0]["message"]

    def test_a_string_property_value_is_skipped_too(self, service):
        """A string passes the `"$ref" in prop_value` substring test and then fails on
        `.get`, so it reached a different error from `None` on the way to the same
        opaque failure."""
        blueprint = service._transform_json_schema_to_bedrock_blueprint(
            _idp_class(properties={"broken": "a string", "total": {"type": "string"}})
        )

        assert list(blueprint["properties"]) == ["total"]
        dropped = [w for w in service._skipped_properties if w["property"] == "broken"]
        assert dropped and "str" in dropped[0]["message"]

    def test_an_array_of_flat_objects_is_extracted_to_an_item_definition(self, service):
        """The definition name and the `$ref` must agree, or the array resolves to
        nothing."""
        extracted: dict = {}

        result = service._process_array_property(
            "transactions",
            {
                "type": "array",
                "description": "All transactions",
                "items": {
                    "type": "object",
                    "description": "One transaction",
                    "properties": {"amount": {"type": "string"}},
                },
            },
            extracted,
        )

        assert result["items"] == {"$ref": "#/definitions/transactionsItem"}
        assert result["instruction"] == "All transactions"
        assert "transactionsItem" in extracted
        assert extracted["transactionsItem"]["description"] == "One transaction"
        assert (
            extracted["transactionsItem"]["properties"]["amount"]["inferenceType"]
            == "explicit"
        )

    def test_an_explicit_instruction_beats_the_description_on_an_extracted_array(
        self, service
    ):
        extracted: dict = {}

        result = service._process_array_property(
            "rows",
            {
                "type": "array",
                "description": "from description",
                "instruction": "from instruction",
                "items": {"type": "object", "properties": {"a": {"type": "string"}}},
            },
            extracted,
        )

        assert result["instruction"] == "from instruction"

    def test_an_array_of_objects_with_neither_text_gets_a_dash(self, service):
        extracted: dict = {}

        result = service._process_array_property(
            "rows",
            {"type": "array", "items": {"type": "object", "properties": {}}},
            extracted,
        )

        assert result["instruction"] == "-"

    def test_an_array_of_primitives_keeps_its_description_as_the_instruction(
        self, service
    ):
        extracted: dict = {}

        result = service._process_array_property(
            "tags",
            {"type": "array", "description": "Tags", "items": {"type": "string"}},
            extracted,
        )

        assert result["instruction"] == "Tags"
        assert extracted == {}

    def test_an_array_of_primitives_with_no_text_gets_a_dash(self, service):
        extracted: dict = {}

        result = service._process_array_property(
            "tags", {"type": "array", "items": {"type": "string"}}, extracted
        )

        assert result["instruction"] == "-"


@pytest.mark.unit
class TestArraySimpleAndBdaFieldHelpers:
    """BDA's rule for arrays is narrow: an `instruction` is required and an
    `inferenceType` is rejected. Both mistakes fail `CreateBlueprint` for the class."""

    @pytest.fixture
    def service(self) -> Any:
        return _service()

    def test_an_array_of_refs_keeps_the_ref_and_takes_the_description(self, service):
        result = service._process_array_property_simple(
            {
                "type": "array",
                "description": "All rows",
                "items": {"$ref": "#/definitions/Row"},
            }
        )

        assert result["items"] == {"$ref": "#/definitions/Row"}
        assert result["instruction"] == "All rows"
        assert "inferenceType" not in result

    def test_an_explicit_instruction_beats_the_description_on_a_ref_array(
        self, service
    ):
        result = service._process_array_property_simple(
            {
                "type": "array",
                "description": "from description",
                "instruction": "from instruction",
                "items": {"$ref": "#/definitions/Row"},
            }
        )

        assert result["instruction"] == "from instruction"

    def test_a_ref_array_with_no_text_gets_a_dash(self, service):
        result = service._process_array_property_simple(
            {"type": "array", "items": {"$ref": "#/definitions/Row"}}
        )

        assert result["instruction"] == "-"

    def test_a_primitive_array_is_reduced_to_the_item_type_with_no_inference_type(
        self, service
    ):
        result = service._process_array_property_simple(
            {
                "type": "array",
                "description": "Tags",
                "items": {"type": "string", "description": "A tag"},
            }
        )

        assert result["items"] == {"type": "string"}
        assert result["instruction"] == "Tags"
        assert "inferenceType" not in result

    def test_a_primitive_array_with_no_text_gets_a_dash(self, service):
        result = service._process_array_property_simple(
            {"type": "array", "items": {"type": "string"}}
        )

        assert result["instruction"] == "-"

    def test_a_missing_type_is_inferred_from_properties(self, service):
        result = service._add_bda_fields_to_schema(
            {"properties": {"a": {"type": "string"}}}
        )

        assert result["type"] == "object"

    def test_a_missing_type_is_inferred_from_items(self, service):
        """Falling through to `string` here would turn a table into one text field."""
        result = service._add_bda_fields_to_schema({"items": {"type": "string"}})

        assert result["type"] == "array"
        assert result["items"] == {"type": "string"}
        assert "inferenceType" not in result

    def test_a_bare_property_defaults_to_a_string_leaf(self, service):
        result = service._add_bda_fields_to_schema({"description": "Something"})

        assert result == {
            "type": "string",
            "inferenceType": "explicit",
            "instruction": "Something",
        }

    def test_array_items_that_are_objects_are_processed_recursively(self, service):
        result = service._add_bda_fields_to_schema(
            {
                "type": "array",
                "instruction": "Rows",
                "items": {
                    "type": "object",
                    "properties": {"a": {"type": "string", "description": "A"}},
                },
            }
        )

        assert result["items"]["properties"]["a"] == {
            "type": "string",
            "inferenceType": "explicit",
            "instruction": "A",
        }

    def test_array_ref_items_are_normalized_to_the_definitions_path(self, service):
        result = service._add_bda_fields_to_schema(
            {"type": "array", "items": {"$ref": "#/$defs/Row"}, "instruction": "Rows"}
        )

        assert result["items"] == {"$ref": "#/definitions/Row"}

    def test_an_arrays_description_becomes_its_instruction_and_loses_inference_type(
        self, service
    ):
        result = service._add_bda_fields_to_schema(
            {
                "type": "array",
                "description": "Rows of the table",
                "inferenceType": "explicit",
                "items": {"type": "string"},
            }
        )

        assert result["instruction"] == "Rows of the table"
        assert "inferenceType" not in result
        assert "description" not in result

    def test_a_non_dict_schema_is_returned_unchanged(self, service):
        assert service._add_bda_fields_to_schema("leaf") == "leaf"


@pytest.mark.unit
class TestBdaToIdpImportPath:
    """The reverse direction: an AWS-authored blueprint becoming an IDP class.

    AWS standard blueprints omit fields the IDP schema requires and use BDA's own
    vocabulary. Whatever is not repaired or translated here reaches the configuration
    table, where it is what the UI renders and what the next IDP→BDA sync pushes
    back — so a missed translation round-trips a schema BDA will reject.
    """

    @pytest.fixture
    def service(self) -> Any:
        return _service()

    def test_a_non_dict_property_value_is_left_alone_by_normalization(self, service):
        """AWS schemas are not validated before this runs; a stray scalar must not
        take down the import of the whole blueprint."""
        result = service._normalize_aws_blueprint_schema(
            {"properties": {"broken": "scalar", "total": {"type": "string"}}}
        )

        assert result["properties"]["broken"] == "scalar"
        assert result["type"] == "object"

    def test_an_array_without_an_instruction_is_repaired_for_bda(self, service):
        """BDA requires `instruction` and `inferenceType` on an array and rejects BDA
        fields on its `items`; an unrepaired array fails the round trip back."""
        result = service._normalize_aws_blueprint_schema(
            {
                "properties": {
                    "tags": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "inferenceType": "explicit",
                            "instruction": "one tag",
                        },
                    }
                }
            }
        )

        tags = result["properties"]["tags"]
        assert tags["items"] == {"type": "string"}
        assert tags["instruction"] == "-"
        assert tags["inferenceType"] == "explicit"

    def test_normalization_reaches_properties_nested_in_an_object(self, service):
        """A `$ref` missing its instruction is repaired at the top level; the same
        defect one level down would otherwise survive."""
        result = service._normalize_aws_blueprint_schema(
            {
                "properties": {
                    "section": {
                        "type": "object",
                        "properties": {
                            "child": {"$ref": "#/definitions/Child"},
                            "note": {"instruction": 'say \\"hi\\"'},
                        },
                    }
                }
            }
        )

        nested = result["properties"]["section"]["properties"]
        assert nested["child"]["instruction"] == "-"
        assert nested["note"]["instruction"] == 'say "hi"'

    def test_an_array_with_no_instruction_becomes_a_described_idp_array(self, service):
        """The transform defaults the missing `instruction` before translating it, so
        the IDP class gets `description: "-"` rather than losing the field."""
        idp = service.transform_bda_blueprint_to_idp_class_schema(
            {
                "class": "Invoice",
                "properties": {
                    "rows": {"type": "array", "items": {"type": "string"}},
                    "broken": "scalar",
                },
            }
        )

        assert idp["properties"]["rows"]["description"] == "-"
        assert idp["properties"]["broken"] == "scalar"

    def test_a_definition_without_a_type_is_given_one(self, service):
        """An IDP class definition with properties and no `type` is not a valid object
        schema, and the UI's schema builder will not render it."""
        idp = service.transform_bda_blueprint_to_idp_class_schema(
            {
                "class": "Invoice",
                "definitions": {
                    "Party": {"properties": {"name": {"type": "string"}}},
                    "Broken": "scalar",
                },
                "properties": {"party": {"$ref": "#/definitions/Party"}},
            }
        )

        assert idp["$defs"]["Party"]["type"] == "object"
        assert idp["$defs"]["Broken"] == "scalar"
        assert idp["properties"]["party"]["$ref"] == "#/$defs/Party"

    def test_property_names_inside_draft07_definitions_are_sanitized(self, service):
        """`_sanitize_property_names` is handed both schema dialects; skipping the
        `definitions` branch would push `Total & Tax` back to BDA unchanged."""
        schema = {
            "definitions": {
                "Party": {
                    "type": "object",
                    "properties": {"Total & Tax": {"type": "string"}},
                }
            }
        }

        sanitized, mapping = service._sanitize_property_names(schema)

        assert list(sanitized["definitions"]["Party"]["properties"]) == ["TotalTax"]
        assert mapping == {"Total & Tax": "TotalTax"}

    def test_an_array_inside_an_object_counts_as_nested_complexity(self, service):
        """It is the array case that motivated the check — BDA rejects an array inside
        an object definition — so it must answer True on its own."""
        assert (
            service._has_nested_complex_structures(
                {"rows": {"type": "array", "items": {"type": "string"}}}
            )
            is True
        )

    def test_a_non_dict_property_is_not_nested_complexity(self, service):
        assert (
            service._has_nested_complex_structures(
                {"broken": "scalar", "total": {"type": "string"}}
            )
            is False
        )


# ---------------------------------------------------------------------------
# _process_single_class and _process_classes_parallel
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProcessSingleClass:
    """Create-versus-update is the decision that keeps one blueprint per class.

    Getting it wrong does not fail: the project ends up with two blueprints for the
    same document type, or with a blueprint whose schema no longer matches the config.
    """

    @pytest.fixture
    def service(self) -> Any:
        service = _service()
        service.blueprint_creator.create_blueprint.return_value = {
            "status": "success",
            "blueprint": {
                "blueprintArn": "arn:aws:bedrock:::blueprint/new-1",
                "blueprintName": "idp-Invoice-new11111",
            },
        }
        service.blueprint_creator.create_blueprint_version_without_project_update.return_value = {
            "blueprint": {"blueprintVersion": "4"}
        }
        return service

    def _existing(
        self, class_name: str = "Invoice", schema: dict | None = None
    ) -> dict:
        return {
            "blueprintArn": f"arn:aws:bedrock:::blueprint/idp-{class_name}-old",
            "blueprintName": f"idp-{class_name}-old11111",
            "blueprintVersion": "2",
            "schema": json.dumps(
                schema
                if schema is not None
                else {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "class": class_name,
                    "description": "stale",
                    "type": "object",
                    "properties": {},
                }
            ),
        }

    def test_a_changed_existing_blueprint_is_updated_not_recreated(self, service):
        existing = self._existing()

        result = service._process_single_class(_idp_class(), [existing])

        assert result["status"] == "success"
        service.blueprint_creator.create_blueprint.assert_not_called()
        kwargs = service.blueprint_creator.update_blueprint.call_args.kwargs
        assert kwargs["blueprint_arn"] == existing["blueprintArn"]
        assert kwargs["stage"] == "LIVE"
        sent = json.loads(kwargs["schema"])
        assert sent["class"] == "Invoice"
        assert sent["properties"]["total"]["instruction"] == "Invoice total"
        assert result["_internal"]["blueprint_version"] == "4"

    def test_an_unchanged_blueprint_is_left_alone_but_still_claimed(self, service):
        """It must appear in `blueprints_updated`, or `_synchronize_deletes` treats
        the live blueprint for this class as an orphan and deletes it."""
        unchanged = self._existing(
            schema=service._transform_json_schema_to_bedrock_blueprint(_idp_class())
        )

        result = service._process_single_class(_idp_class(), [unchanged])

        service.blueprint_creator.update_blueprint.assert_not_called()
        service.blueprint_creator.create_blueprint.assert_not_called()
        assert result["_internal"]["blueprint_arn"] == unchanged["blueprintArn"]
        assert result["_internal"]["blueprint_version"] is None

    def test_a_new_class_is_created_and_versioned(self, service):
        result = service._process_single_class(_idp_class(), [])

        name = service.blueprint_creator.create_blueprint.call_args.kwargs[
            "blueprint_name"
        ]
        assert name.startswith("idp-Invoice-")
        assert (
            result["_internal"]["blueprint_arn"] == "arn:aws:bedrock:::blueprint/new-1"
        )
        assert result["_internal"]["blueprint_version"] == "4"

    def test_sanitizing_a_property_name_rewrites_the_class_in_place(self, service):
        """The config must be corrected too, or the two sides never converge: BDA has
        `TotalTax`, the config keeps `Total & Tax`, and every sync sees a diff."""
        custom_class = _idp_class(
            properties={"Total & Tax": {"type": "string", "description": "Sum"}}
        )

        result = service._process_single_class(custom_class, [])

        assert list(custom_class["properties"]) == ["TotalTax"]
        assert result["_internal"]["classes_modified"] is True
        assert "TotalTax" in _created_schema(service)["properties"]

    def test_a_clean_class_is_not_reported_as_modified(self, service):
        """Otherwise every sync rewrites the configuration table for nothing."""
        result = service._process_single_class(_idp_class(), [])

        assert result["_internal"]["classes_modified"] is False

    def test_sanitizing_on_the_update_path_also_rewrites_the_class(self, service):
        custom_class = _idp_class(
            properties={"Total & Tax": {"type": "string", "description": "Sum"}}
        )

        result = service._process_single_class(custom_class, [self._existing()])

        assert list(custom_class["properties"]) == ["TotalTax"]
        assert result["_internal"]["classes_modified"] is True

    def test_a_rejected_create_carries_the_api_result_into_the_error(self, service):
        """The per-class reason is the only diagnosis a caller gets; a bare count
        would send the user to CloudWatch."""
        service.blueprint_creator.create_blueprint.return_value = {
            "status": "failed",
            "message": "ValidationException: schema too deep",
        }

        result = service._process_single_class(_idp_class(), [])

        assert result == {
            "class": "Invoice",
            "status": "failed",
            "error": (
                "Failed to create blueprint: {'status': 'failed', "
                "'message': 'ValidationException: schema too deep'}"
            ),
        }
        service.blueprint_creator.create_blueprint_version_without_project_update.assert_not_called()

    def test_the_warnings_for_this_class_only_are_returned(self, service):
        """`_skipped_properties` accumulates across classes on the shared service, so
        an unfiltered read would blame one class for another's dropped fields."""
        service._skipped_properties = [
            {"class": "Receipt", "property": "other", "type": "nested_object"}
        ]

        result = service._process_single_class(_class_with_nested_definition(), [])

        assert [w["class"] for w in result["warnings"]] == ["Invoice"]
        assert result["warnings"][0]["property"] == "address"

    def test_a_class_id_is_read_from_the_document_type_when_id_is_absent(self, service):
        class_schema = _idp_class(class_id="Receipt")
        del class_schema["$id"]

        result = service._process_single_class(class_schema, [])

        assert result["class"] == "Receipt"
        assert service.blueprint_creator.create_blueprint.call_args.kwargs[
            "blueprint_name"
        ].startswith("idp-Receipt-")


@pytest.mark.unit
class TestProcessClassesParallel:
    """The fan-out collects three things the caller acts on: per-class status, the
    ARNs that must survive `_synchronize_deletes`, and the association payload."""

    @pytest.fixture
    def service(self) -> Any:
        service = _service()
        service.blueprint_creator.create_blueprint.side_effect = [
            {
                "status": "success",
                "blueprint": {
                    "blueprintArn": f"arn:aws:bedrock:::blueprint/bp-{i}",
                    "blueprintName": f"idp-C{i}-aaaa",
                },
            }
            for i in range(1, 6)
        ]
        service.blueprint_creator.create_blueprint_version_without_project_update.return_value = {
            "blueprint": {"blueprintVersion": "2"}
        }
        return service

    def test_every_successful_class_is_associated_with_the_project_once(self, service):
        """A blueprint missing from this payload exists in the account but not in the
        project, so BDA never classifies a document as that type."""
        classes = [_idp_class(class_id="Invoice"), _idp_class(class_id="Receipt")]

        status, updated, modified = service._process_classes_parallel(classes, [])

        assert sorted(entry["class"] for entry in status) == ["Invoice", "Receipt"]
        assert modified is False
        service.blueprint_creator.bulk_update_data_automation_project.assert_called_once()
        args = (
            service.blueprint_creator.bulk_update_data_automation_project.call_args.args
        )
        assert args[0] == PROJECT_ARN
        assert sorted(bp["blueprintArn"] for bp in args[1]) == sorted(updated)
        assert {bp["blueprintVersion"] for bp in args[1]} == {"2"}

    def test_a_failed_class_is_neither_associated_nor_claimed(self, service):
        """If a failed class's ARN leaked into `blueprints_updated`, a stale blueprint
        would be protected from cleanup."""
        # One worker, so the failing response is the one the first class receives.
        service.max_workers = 1
        service.blueprint_creator.create_blueprint.side_effect = [
            {"status": "failed", "message": "nope"},
            {
                "status": "success",
                "blueprint": {
                    "blueprintArn": "arn:aws:bedrock:::blueprint/ok",
                    "blueprintName": "idp-Receipt-aaaa",
                },
            },
        ]
        classes = [_idp_class(class_id="Invoice"), _idp_class(class_id="Receipt")]

        status, updated, _ = service._process_classes_parallel(classes, [])

        assert updated == ["arn:aws:bedrock:::blueprint/ok"]
        failed = [entry for entry in status if entry["status"] == "failed"]
        assert [entry["class"] for entry in failed] == ["Invoice"]
        assert "nope" in failed[0]["error"]
        args = (
            service.blueprint_creator.bulk_update_data_automation_project.call_args.args
        )
        assert [bp["blueprintArn"] for bp in args[1]] == [
            "arn:aws:bedrock:::blueprint/ok"
        ]

    def test_dropped_properties_are_reported_per_class_in_the_status(self, service):
        status, _, _ = service._process_classes_parallel(
            [_class_with_nested_definition()], []
        )

        assert status[0]["warnings"][0]["property"] == "address"

    def test_a_sanitized_class_sets_the_modified_flag_for_the_caller(self, service):
        """That flag is what makes the caller write the corrected classes back."""
        dirty = _idp_class(properties={"Total & Tax": {"type": "string"}})

        _, _, modified = service._process_classes_parallel([dirty], [])

        assert modified is True

    def test_a_thread_that_raises_does_not_abort_the_other_classes(self, service):
        real = service._process_single_class

        def _explode(custom_class, existing_blueprints):
            if custom_class.get("$id") == "Invoice":
                raise RuntimeError("worker died")
            return real(custom_class, existing_blueprints)

        classes = [_idp_class(class_id="Invoice"), _idp_class(class_id="Receipt")]
        with patch.object(service, "_process_single_class", side_effect=_explode):
            status, updated, _ = service._process_classes_parallel(classes, [])

        assert [entry["class"] for entry in status] == ["Receipt"]
        assert len(updated) == 1

    def test_a_failure_to_associate_fails_the_classes_it_affects(self, service):
        """A blueprint outside the project's `customOutputConfiguration` extracts
        nothing.

        The blueprint exists in the account, so every per-class step succeeded; what
        failed is the one write that makes BDA recognise the document type. Reporting
        `success` was the only outcome that hid it completely — a clean sync report
        and nothing extracting.
        """
        service.blueprint_creator.bulk_update_data_automation_project.side_effect = (
            RuntimeError("conflict")
        )

        status, updated, _ = service._process_classes_parallel(
            [_idp_class("Invoice"), _idp_class("Receipt")], []
        )

        assert [entry["status"] for entry in status] == ["failed", "failed"]
        assert all("not be recognised" in entry["error"] for entry in status)
        # The ARNs stay claimed: they name blueprints that do exist, and dropping
        # them here would offer them to `_synchronize_deletes` for deletion.
        assert len(updated) == 2

    def test_no_successful_class_means_no_project_write_at_all(self, service):
        """An empty association payload would clear the project's blueprint list."""
        service.blueprint_creator.create_blueprint.side_effect = [
            {"status": "failed", "message": "nope"}
        ]

        service._process_classes_parallel([_idp_class()], [])

        service.blueprint_creator.bulk_update_data_automation_project.assert_not_called()


# ---------------------------------------------------------------------------
# AWS standard blueprint conversion
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConvertAwsStandardBlueprints:
    """Converting an AWS standard blueprint hands its schema to IDP as a class and
    replaces it with a custom blueprint the stack owns."""

    @pytest.fixture
    def service(self) -> Any:
        service = _service()
        service.blueprint_creator.create_blueprint.return_value = {
            "status": "success",
            "blueprint": {
                "blueprintArn": "arn:aws:bedrock:::blueprint/custom-1",
                "blueprintName": "idp-Payslip-aaaa",
            },
        }
        service.blueprint_creator.create_blueprint_version_without_project_update.return_value = {
            "blueprint": {"blueprintVersion": "1"}
        }
        return service

    def test_a_class_that_already_exists_is_skipped_without_creating_anything(
        self, service
    ):
        """Converting it again would add a duplicate class to the configuration and a
        second blueprint for the same document type."""
        aws_bp = _bda_blueprint(
            "Payslip",
            arn="arn:aws:bedrock:us-west-2:aws:blueprint/payslip",
            name="payslip",
        )

        result = service._convert_single_aws_blueprint(
            aws_bp, [_idp_class(class_id="Payslip")]
        )

        assert result["status"] == "success"
        assert result["_internal"] == {"skipped": True}
        service.blueprint_creator.create_blueprint.assert_not_called()

    def test_a_string_schema_is_parsed_before_the_class_is_read(self, service):
        aws_bp = _bda_blueprint(
            "Payslip",
            arn="arn:aws:bedrock:us-west-2:aws:blueprint/payslip",
            name="payslip",
        )
        aws_bp["schema"] = json.dumps(aws_bp["schema"])

        result = service._convert_single_aws_blueprint(aws_bp, [])

        assert result["class"] == "Payslip"
        assert result["_internal"]["idp_class_schema"]["$id"] == "Payslip"
        assert result["_internal"]["aws_blueprint_arn"] == aws_bp["blueprintArn"]

    def test_the_replacement_blueprint_is_named_for_the_sanitized_class(self, service):
        aws_bp = _bda_blueprint("Pay slip", name="payslip", arn="arn:aws:x:::b/pay")

        service._convert_single_aws_blueprint(aws_bp, [])

        name = service.blueprint_creator.create_blueprint.call_args.kwargs[
            "blueprint_name"
        ]
        assert name.startswith("idp-Pay-slip-")

    def test_a_rejected_create_reports_the_blueprint_name_as_the_class(self, service):
        """The class is unknown at that point, so the name is the only handle the
        caller has on which blueprint failed."""
        service.blueprint_creator.create_blueprint.return_value = {"status": "failed"}
        aws_bp = _bda_blueprint("Payslip", name="aws-payslip", arn="arn:aws:x:::b/pay")

        result = service._convert_single_aws_blueprint(aws_bp, [])

        assert result == {"status": "failed", "class": "aws-payslip"}

    def test_an_unparseable_schema_is_reported_as_a_failed_conversion(self, service):
        aws_bp = _bda_blueprint("Payslip", name="aws-payslip", arn="arn:aws:x:::b/pay")
        aws_bp["schema"] = "{not json"

        result = service._convert_single_aws_blueprint(aws_bp, [])

        assert result == {"status": "failed", "class": "aws-payslip"}

    def test_conversions_are_collected_and_the_new_blueprints_associated(self, service):
        aws_bp = _bda_blueprint("Payslip", name="payslip", arn="arn:aws:x:::b/pay")

        result = service._convert_aws_standard_blueprints_parallel([aws_bp], [])

        assert result["new_custom_blueprint_arns"] == [
            "arn:aws:bedrock:::blueprint/custom-1"
        ]
        assert result["aws_blueprint_arns_to_remove"] == ["arn:aws:x:::b/pay"]
        assert result["converted_classes"][0]["$id"] == "Payslip"
        args = (
            service.blueprint_creator.bulk_update_data_automation_project.call_args.args
        )
        assert args[1] == [
            {
                "blueprintArn": "arn:aws:bedrock:::blueprint/custom-1",
                "blueprintVersion": "1",
            }
        ]

    def test_a_skipped_conversion_contributes_no_class_and_no_removal(self, service):
        """Its AWS blueprint must stay in the project: the existing IDP class already
        points at a custom blueprint, and removing this one is not this call's job."""
        aws_bp = _bda_blueprint("Payslip", name="payslip", arn="arn:aws:x:::b/pay")

        result = service._convert_aws_standard_blueprints_parallel(
            [aws_bp], [_idp_class(class_id="Payslip")]
        )

        assert result["converted_classes"] == []
        assert result["aws_blueprint_arns_to_remove"] == []
        assert result["conversion_status"] == [
            {"status": "success", "class": "Payslip"}
        ]
        service.blueprint_creator.bulk_update_data_automation_project.assert_not_called()

    def test_a_worker_exception_does_not_lose_the_other_conversions(self, service):
        real = service._convert_single_aws_blueprint

        def _explode(aws_blueprint, existing_classes):
            if aws_blueprint["blueprintName"] == "boom":
                raise RuntimeError("worker died")
            return real(aws_blueprint, existing_classes)

        good = _bda_blueprint("Payslip", name="payslip", arn="arn:aws:x:::b/pay")
        bad = _bda_blueprint("Other", name="boom", arn="arn:aws:x:::b/boom")
        with patch.object(
            service, "_convert_single_aws_blueprint", side_effect=_explode
        ):
            result = service._convert_aws_standard_blueprints_parallel([good, bad], [])

        assert [entry["class"] for entry in result["conversion_status"]] == ["Payslip"]

    def test_a_failure_to_associate_converted_blueprints_fails_the_conversion(
        self, service
    ):
        """Same shape as `_process_classes_parallel`: the new custom blueprint exists
        but is not in the project, so the document type it replaces is no longer
        recognised at all."""
        service.blueprint_creator.bulk_update_data_automation_project.side_effect = (
            RuntimeError("conflict")
        )
        aws_bp = _bda_blueprint("Payslip", name="payslip", arn="arn:aws:x:::b/pay")

        result = service._convert_aws_standard_blueprints_parallel([aws_bp], [])

        assert result["conversion_status"][0]["status"] == "failed"
        assert "not be recognised" in result["conversion_status"][0]["error"]

    def test_converting_without_a_version_refuses_rather_than_guessing(self, service):
        """The version names which configuration the derived classes are written to;
        defaulting it would write another version's classes."""
        with pytest.raises(ValueError, match="missing version"):
            service._convert_aws_standard_blueprints_to_custom("")

    def test_an_empty_project_converts_nothing(self, service):
        service.blueprint_creator.list_blueprints.return_value = {"blueprints": []}

        assert service._convert_aws_standard_blueprints_to_custom("v1") == []
        service.config_manager.handle_update_custom_configuration.assert_not_called()

    def test_converted_classes_are_appended_to_the_existing_configuration(
        self, service
    ):
        """Appended, not replaced: merge-mode BDA→IDP must not drop a class that has
        no BDA blueprint."""
        existing = _idp_class(class_id="Invoice")
        config = MagicMock()
        config.classes = [existing]
        service.config_manager.get_configuration.return_value = config
        _wire_project(
            service,
            [_bda_blueprint("Payslip", arn="arn:aws:x:aws:blueprint/pay", name="pay")],
        )

        result = service._convert_aws_standard_blueprints_to_custom("v1")

        assert result["converted_count"] == 1
        written = (
            service.config_manager.handle_update_custom_configuration.call_args.kwargs
        )
        assert written["version"] == "v1"
        assert [cls["$id"] for cls in written["custom_config"]["classes"]] == [
            "Invoice",
            "Payslip",
        ]

    def test_the_converted_aws_blueprints_are_removed_from_the_project(self, service):
        """Left associated, the converted blueprint competes with its replacement for
        the same document type; a blueprint that was *skipped* must stay."""
        config = MagicMock()
        config.classes = [_idp_class(class_id="Invoice")]
        service.config_manager.get_configuration.return_value = config
        aws_arn = "arn:aws:x:aws:blueprint/pay"
        _wire_project(
            service,
            [
                _bda_blueprint("Payslip", arn=aws_arn, name="pay"),
                _bda_blueprint(
                    "Invoice", arn="arn:aws:x:::b/keep", name="idp-Invoice-a"
                ),
            ],
        )

        service._convert_aws_standard_blueprints_to_custom("v1")

        payload = service.blueprint_creator.update_project_with_custom_configurations.call_args.kwargs[
            "customConfiguration"
        ]
        assert [bp["blueprintArn"] for bp in payload["blueprints"]] == [
            "arn:aws:x:::b/keep"
        ]

    def test_a_failure_removing_them_still_writes_the_converted_classes(self, service):
        config = MagicMock()
        config.classes = []
        service.config_manager.get_configuration.return_value = config
        aws_arn = "arn:aws:x:aws:blueprint/pay"
        _wire_project(service, [_bda_blueprint("Payslip", arn=aws_arn, name="pay")])
        service.blueprint_creator.update_project_with_custom_configurations.side_effect = RuntimeError(
            "conflict"
        )

        result = service._convert_aws_standard_blueprints_to_custom("v1")

        assert result["converted_count"] == 1
        service.config_manager.handle_update_custom_configuration.assert_called_once()

    def test_a_configuration_write_failure_is_reported_as_a_raised_error(self, service):
        """This one re-raises where its siblings swallow, because losing the converted
        classes silently would mean the new blueprints exist with no class behind
        them."""
        config = MagicMock()
        config.classes = []
        service.config_manager.get_configuration.return_value = config
        _wire_project(
            service,
            [_bda_blueprint("Payslip", arn="arn:aws:x:aws:blueprint/pay", name="pay")],
        )
        service.config_manager.handle_update_custom_configuration.side_effect = (
            RuntimeError("table write failed")
        )

        with pytest.raises(
            Exception, match="Failed to convert AWS standard blueprints"
        ):
            service._convert_aws_standard_blueprints_to_custom("v1")


# ---------------------------------------------------------------------------
# create_blueprints_from_custom_configuration: direction and mode
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSyncDirectionAndMode:
    """`sync_direction` picks which side is read and `sync_mode` picks whether the
    other side's extras are destroyed. A mode that behaves like its opposite either
    deletes a user's classes or leaves blueprints the config no longer describes."""

    @pytest.fixture
    def service(self) -> Any:
        service = _service()
        service.blueprint_creator.create_blueprint.return_value = {
            "status": "success",
            "blueprint": {
                "blueprintArn": "arn:aws:bedrock:::blueprint/new-1",
                "blueprintName": "idp-Invoice-new11111",
            },
        }
        service.blueprint_creator.create_blueprint_version_without_project_update.return_value = {
            "blueprint": {"blueprintVersion": "1"}
        }
        return service

    def _config(self, service: Any, classes: list[dict]) -> Any:
        config = MagicMock()
        config.classes = classes
        service.config_manager.get_configuration.return_value = config
        return config

    def test_an_invalid_sync_mode_is_refused(self, service):
        with pytest.raises(Exception, match="Invalid sync_mode"):
            service.create_blueprints_from_custom_configuration(
                version="v1", sync_mode="destroy"
            )

    def test_replace_mode_bda_to_idp_makes_bda_the_source_of_truth(self, service):
        """The classes written back must be exactly the BDA-derived ones: an IDP class
        with no blueprint behind it can never be extracted, so it is removed."""
        self._config(
            service, [_idp_class(class_id="Invoice"), _idp_class(class_id="Receipt")]
        )
        _wire_project(service, [_bda_blueprint("Payslip")])

        status = service.create_blueprints_from_custom_configuration(
            version="v1", sync_direction="bda_to_idp", sync_mode="replace"
        )

        written = (
            service.config_manager.handle_update_custom_configuration.call_args.kwargs
        )
        classes = written["custom_config"]["classes"]
        assert [cls["$id"] for cls in classes] == ["Payslip"]
        assert classes[0]["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert classes[0]["properties"]["total"]["description"] == "The total"
        assert "inferenceType" not in classes[0]["properties"]["total"]
        assert status == [{"status": "success", "class": "Payslip"}]
        service.blueprint_creator.create_blueprint.assert_not_called()

    def test_replace_mode_bda_to_idp_with_an_empty_project_clears_the_classes(
        self, service
    ):
        self._config(service, [_idp_class(class_id="Invoice")])
        service.blueprint_creator.list_blueprints.return_value = {"blueprints": []}

        service.create_blueprints_from_custom_configuration(
            version="v1", sync_direction="bda_to_idp", sync_mode="replace"
        )

        written = (
            service.config_manager.handle_update_custom_configuration.call_args.kwargs
        )
        assert written["custom_config"] == {"classes": []}

    def test_one_unconvertible_blueprint_does_not_cost_the_others_their_classes(
        self, service
    ):
        self._config(service, [])
        good = _bda_blueprint("Invoice")
        bad = _bda_blueprint("Broken", name="idp-Broken-bbbb", arn="arn:aws:x:::b/bad")
        bad["schema"] = "{not json"
        _wire_project(service, [good, bad])

        status = service.create_blueprints_from_custom_configuration(
            version="v1", sync_direction="bda_to_idp", sync_mode="replace"
        )

        written = (
            service.config_manager.handle_update_custom_configuration.call_args.kwargs
        )
        assert [cls["$id"] for cls in written["custom_config"]["classes"]] == [
            "Invoice"
        ]
        assert {"status": "failed", "class": "idp-Broken-bbbb"} in status

    def test_a_failure_writing_the_replaced_classes_fails_the_sync(self, service):
        """A replace that did not write is not a replace.

        Everything left in this block either reads the project or writes the aligned
        class list — per-blueprint conversion errors are caught one level in and
        reported per class. So a failure here means the alignment the user asked for
        did not happen, and reporting the per-class statuses as though it had left
        them with the pre-sync classes and no sign of it.
        """
        self._config(service, [])
        _wire_project(service, [_bda_blueprint("Invoice")])
        service.config_manager.handle_update_custom_configuration.side_effect = (
            RuntimeError("table write failed")
        )

        with pytest.raises(Exception, match="Failed to process blueprint creation"):
            service.create_blueprints_from_custom_configuration(
                version="v1", sync_direction="bda_to_idp", sync_mode="replace"
            )

    def test_a_project_that_cannot_be_read_does_not_clear_the_idp_classes(
        self, service
    ):
        """The costliest of these: replace mode reading `[]` as "BDA is empty".

        An AccessDenied or a throttle on the project read used to answer `[]`, which
        this branch treats as "no blueprints in BDA" and responds to by clearing
        every IDP class. The assertion is that the configuration is not written at
        all — the classes are what was lost.
        """
        self._config(service, [_idp_class(class_id="Invoice")])
        service.blueprint_creator.list_blueprints.side_effect = _client_error(
            "AccessDeniedException", "GetDataAutomationProject"
        )

        with pytest.raises(Exception, match="Failed to process blueprint creation"):
            service.create_blueprints_from_custom_configuration(
                version="v1", sync_direction="bda_to_idp", sync_mode="replace"
            )

        service.config_manager.handle_update_custom_configuration.assert_not_called()

    def test_a_project_that_cannot_be_read_does_not_duplicate_every_blueprint(
        self, service
    ):
        """The phase-2 half of the same defect.

        An empty view makes every existing blueprint invisible to
        `_blueprint_lookup`, so each class takes the create path and the project ends
        up with two blueprints per document type. The assertion is that no blueprint
        is created.
        """
        self._config(service, [_idp_class(class_id="Invoice")])
        service.blueprint_creator.list_blueprints.side_effect = _client_error(
            "ThrottlingException", "GetDataAutomationProject"
        )

        with pytest.raises(Exception, match="Failed to process blueprint creation"):
            service.create_blueprints_from_custom_configuration(
                version="v1", sync_direction="idp_to_bda", sync_mode="replace"
            )

        service.blueprint_creator.create_blueprint.assert_not_called()

    def test_bidirectional_uses_merge_for_phase_one_whatever_the_mode_says(
        self, service
    ):
        """Legacy contract: a bidirectional `replace` must not let phase 1 delete the
        IDP classes it is about to push to BDA in phase 2."""
        self._config(service, [_idp_class(class_id="Invoice")])
        _wire_project(service, [])

        with patch.object(
            service, "_convert_aws_standard_blueprints_to_custom", return_value=[]
        ) as merge_path:
            service.create_blueprints_from_custom_configuration(
                version="v1", sync_direction="bidirectional", sync_mode="replace"
            )

        merge_path.assert_called_once_with(version="v1")

    def test_merge_mode_appends_the_conversion_details_to_the_status(self, service):
        self._config(service, [])
        with patch.object(
            service,
            "_convert_aws_standard_blueprints_to_custom",
            return_value={
                "status": "success",
                "converted_count": 2,
                "conversion_details": [
                    {"status": "success", "class": "Payslip"},
                    {"status": "failed", "class": "aws-other"},
                ],
            },
        ):
            status = service.create_blueprints_from_custom_configuration(
                version="v1", sync_direction="bda_to_idp", sync_mode="merge"
            )

        assert status == [
            {"status": "success", "class": "Payslip"},
            {"status": "failed", "class": "aws-other"},
        ]

    def test_a_conversion_that_converted_nothing_adds_no_status(self, service):
        self._config(service, [])
        with patch.object(
            service,
            "_convert_aws_standard_blueprints_to_custom",
            return_value={"status": "success", "converted_count": 0},
        ):
            status = service.create_blueprints_from_custom_configuration(
                version="v1", sync_direction="bda_to_idp", sync_mode="merge"
            )

        assert status == []

    def test_a_conversion_failure_in_merge_mode_is_not_raised(self, service):
        self._config(service, [])
        with patch.object(
            service,
            "_convert_aws_standard_blueprints_to_custom",
            side_effect=RuntimeError("conversion blew up"),
        ):
            assert (
                service.create_blueprints_from_custom_configuration(
                    version="v1", sync_direction="bda_to_idp", sync_mode="merge"
                )
                == []
            )

    def test_a_version_with_no_configuration_row_writes_nothing_to_bda(self, service):
        """A version that does not exist must not be read as "no classes", which in
        replace mode would delete every blueprint in the project."""
        service.config_manager.get_configuration.return_value = None
        _wire_project(service, [_bda_blueprint("Invoice")])

        result = service.create_blueprints_from_custom_configuration(
            version="ghost", sync_direction="idp_to_bda"
        )

        assert result == []
        service.blueprint_creator.delete_blueprint.assert_not_called()
        service.blueprint_creator.create_blueprint.assert_not_called()

    def test_replace_mode_idp_to_bda_deletes_a_blueprint_with_no_class(self, service):
        self._config(service, [_idp_class(class_id="Invoice")])
        orphan = _bda_blueprint("Receipt", name="idp-Receipt-oldbbbb", version="3")
        _wire_project(service, [orphan])

        service.create_blueprints_from_custom_configuration(
            version="v1", sync_direction="idp_to_bda", sync_mode="replace"
        )

        service.blueprint_creator.delete_blueprint.assert_called_once_with(
            orphan["blueprintArn"], "3"
        )

    def test_merge_mode_idp_to_bda_keeps_a_blueprint_with_no_class(self, service):
        """Same fixture as the replace case; only the mode differs. Deleting here
        would remove a document type the user kept in BDA on purpose."""
        self._config(service, [_idp_class(class_id="Invoice")])
        orphan = _bda_blueprint("Receipt", name="idp-Receipt-oldbbbb", version="3")
        _wire_project(service, [orphan])

        service.create_blueprints_from_custom_configuration(
            version="v1", sync_direction="idp_to_bda", sync_mode="merge"
        )

        service.blueprint_creator.delete_blueprint.assert_not_called()

    def test_a_sanitized_class_is_written_back_to_the_configuration(self, service):
        classes = [
            _idp_class(
                properties={"Total & Tax": {"type": "string", "description": "S"}}
            )
        ]
        self._config(service, classes)
        _wire_project(service, [])

        service.create_blueprints_from_custom_configuration(
            version="v1", sync_direction="idp_to_bda"
        )

        written = (
            service.config_manager.handle_update_custom_configuration.call_args.kwargs
        )
        assert list(written["custom_config"]["classes"][0]["properties"]) == [
            "TotalTax"
        ]
        assert written["version"] == "v1"

    def test_an_unchanged_configuration_is_not_rewritten(self, service):
        self._config(service, [_idp_class()])
        _wire_project(service, [])

        service.create_blueprints_from_custom_configuration(
            version="v1", sync_direction="idp_to_bda"
        )

        service.config_manager.handle_update_custom_configuration.assert_not_called()

    def test_aws_standard_blueprints_are_disassociated_after_an_idp_to_bda_sync(
        self, service
    ):
        self._config(service, [_idp_class()])
        service.blueprint_creator.list_blueprints.return_value = {
            "blueprints": [
                {"blueprintArn": "arn:aws:x:aws:blueprint/payslip"},
                {"blueprintArn": "arn:aws:bedrock:::blueprint/new-1"},
            ]
        }
        service.blueprint_creator.get_blueprint.return_value = {
            "blueprint": {"blueprintName": "idp-Invoice-new11111", "schema": "{}"}
        }

        service.create_blueprints_from_custom_configuration(
            version="v1", sync_direction="idp_to_bda", sync_mode="merge"
        )

        payload = service.blueprint_creator.update_project_with_custom_configurations.call_args.kwargs[
            "customConfiguration"
        ]
        assert [bp["blueprintArn"] for bp in payload["blueprints"]] == [
            "arn:aws:bedrock:::blueprint/new-1"
        ]

    def test_an_unexpected_failure_is_wrapped_rather_than_reported_as_success(
        self, service
    ):
        self._config(service, [_idp_class()])
        with patch.object(
            service, "_retrieve_all_blueprints", side_effect=RuntimeError("boom")
        ):
            with pytest.raises(Exception, match="Failed to process blueprint creation"):
                service.create_blueprints_from_custom_configuration(
                    version="v1", sync_direction="idp_to_bda"
                )


# ---------------------------------------------------------------------------
# cleanup_orphaned_blueprints and the project/delete helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCleanupOrphanedBlueprints:
    """This deletes account-wide by name prefix, so the classification of "orphan" is
    the only thing standing between a stale blueprint and a live one."""

    @pytest.fixture
    def service(self) -> Any:
        service = _service()
        service.blueprint_creator.delete_blueprint.return_value = True
        return service

    def _config(self, service: Any, classes: list[dict]) -> None:
        config = MagicMock()
        config.classes = classes
        service.config_manager.get_configuration.return_value = config

    def test_an_orphan_is_disassociated_then_deleted_and_a_live_one_is_not(
        self, service
    ):
        self._config(service, [_idp_class(class_id="Invoice")])
        live = {
            "blueprintName": "idp-Invoice-aaaa1111",
            "blueprintArn": "arn:aws:x:::b/live",
            "blueprintVersion": "2",
        }
        orphan = {
            "blueprintName": "idp-Receipt-bbbb2222",
            "blueprintArn": "arn:aws:x:::b/orphan",
            "blueprintVersion": "5",
        }
        service.blueprint_creator.list_all_blueprints_with_prefix.return_value = [
            live,
            orphan,
        ]
        service.blueprint_creator.list_blueprints.return_value = {
            "blueprints": [
                {"blueprintArn": "arn:aws:x:::b/live"},
                {"blueprintArn": "arn:aws:x:::b/orphan"},
            ]
        }

        result = service.cleanup_orphaned_blueprints("v1")

        payload = service.blueprint_creator.update_project_with_custom_configurations.call_args.kwargs[
            "customConfiguration"
        ]
        assert [bp["blueprintArn"] for bp in payload["blueprints"]] == [
            "arn:aws:x:::b/live"
        ]
        service.blueprint_creator.delete_blueprint.assert_called_once_with(
            "arn:aws:x:::b/orphan", "5"
        )
        assert result["success"] is True
        assert result["deleted_count"] == 1
        assert result["details"] == [
            {
                "name": "idp-Receipt-bbbb2222",
                "arn": "arn:aws:x:::b/orphan",
                "status": "deleted",
            }
        ]

    def test_an_orphan_without_a_version_is_deleted_at_version_one(self, service):
        self._config(service, [])
        service.blueprint_creator.list_all_blueprints_with_prefix.return_value = [
            {"blueprintName": "idp-Receipt-bbbb", "blueprintArn": "arn:aws:x:::b/o"}
        ]
        service.blueprint_creator.list_blueprints.return_value = {"blueprints": []}

        service.cleanup_orphaned_blueprints("v1")

        service.blueprint_creator.delete_blueprint.assert_called_once_with(
            "arn:aws:x:::b/o", "1"
        )

    def test_nothing_is_written_to_the_project_when_no_orphan_is_associated(
        self, service
    ):
        """An unnecessary rewrite of the blueprint list is itself a risk."""
        self._config(service, [])
        service.blueprint_creator.list_all_blueprints_with_prefix.return_value = [
            {
                "blueprintName": "idp-Receipt-bbbb",
                "blueprintArn": "arn:aws:x:::b/o",
                "blueprintVersion": "1",
            }
        ]
        service.blueprint_creator.list_blueprints.return_value = {
            "blueprints": [{"blueprintArn": "arn:aws:x:::b/unrelated"}]
        }

        service.cleanup_orphaned_blueprints("v1")

        service.blueprint_creator.update_project_with_custom_configurations.assert_not_called()
        service.blueprint_creator.delete_blueprint.assert_called_once()

    def test_a_delete_reported_as_unsuccessful_is_counted_and_surfaced(self, service):
        """`delete_blueprint` answers a bool rather than raising, so a `False` that is
        read as success leaves the blueprint in the account and the caller told the
        cleanup worked."""
        self._config(service, [])
        service.blueprint_creator.list_all_blueprints_with_prefix.return_value = [
            {
                "blueprintName": "idp-Receipt-bbbb",
                "blueprintArn": "arn:aws:x:::b/o",
                "blueprintVersion": "1",
            }
        ]
        service.blueprint_creator.list_blueprints.return_value = {"blueprints": []}
        service.blueprint_creator.delete_blueprint.return_value = False

        result = service.cleanup_orphaned_blueprints("v1")

        assert result["success"] is False
        assert result["deleted_count"] == 0
        assert result["failed_count"] == 1
        assert result["details"][0]["status"] == "failed"
        assert result["message"] == "Deleted 0 orphaned blueprints, 1 failed"

    def test_a_delete_that_raises_records_the_reason_and_continues(self, service):
        self._config(service, [])
        blueprints = [
            {
                "blueprintName": f"idp-Receipt{i}-bbbb",
                "blueprintArn": f"arn:aws:x:::b/o{i}",
                "blueprintVersion": "1",
            }
            for i in (1, 2)
        ]
        service.blueprint_creator.list_all_blueprints_with_prefix.return_value = (
            blueprints
        )
        service.blueprint_creator.list_blueprints.return_value = {"blueprints": []}
        service.blueprint_creator.delete_blueprint.side_effect = [
            _client_error("ConflictException", "DeleteBlueprint"),
            True,
        ]

        result = service.cleanup_orphaned_blueprints("v1")

        assert result["deleted_count"] == 1
        assert result["failed_count"] == 1
        assert "ConflictException" in result["details"][0]["error"]

    def test_failing_to_disassociate_does_not_stop_the_deletion(self, service):
        self._config(service, [])
        service.blueprint_creator.list_all_blueprints_with_prefix.return_value = [
            {
                "blueprintName": "idp-Receipt-bbbb",
                "blueprintArn": "arn:aws:x:::b/o",
                "blueprintVersion": "1",
            }
        ]
        service.blueprint_creator.list_blueprints.side_effect = _client_error(
            "AccessDenied", "ListBlueprints"
        )

        result = service.cleanup_orphaned_blueprints("v1")

        assert result["deleted_count"] == 1

    def test_no_blueprints_with_the_prefix_is_reported_as_nothing_to_do(self, service):
        service.blueprint_creator.list_all_blueprints_with_prefix.return_value = []

        result = service.cleanup_orphaned_blueprints("v1")

        assert result == {
            "success": True,
            "message": "No orphaned blueprints found",
            "deleted_count": 0,
            "failed_count": 0,
            "details": [],
        }
        service.config_manager.get_configuration.assert_not_called()
        service.blueprint_creator.delete_blueprint.assert_not_called()

    def test_a_version_with_no_classes_makes_every_prefixed_blueprint_an_orphan(
        self, service
    ):
        service.config_manager.get_configuration.return_value = None
        service.blueprint_creator.list_all_blueprints_with_prefix.return_value = [
            {
                "blueprintName": "idp-Invoice-aaaa",
                "blueprintArn": "arn:aws:x:::b/a",
                "blueprintVersion": "1",
            }
        ]
        service.blueprint_creator.list_blueprints.return_value = {"blueprints": []}

        result = service.cleanup_orphaned_blueprints("v1")

        assert result["deleted_count"] == 1

    def test_a_class_with_no_id_contributes_no_expected_prefix(self, service):
        """An empty prefix would match every name and protect every blueprint,
        turning cleanup into a no-op."""
        self._config(service, [{"description": "class with no id"}])
        service.blueprint_creator.list_all_blueprints_with_prefix.return_value = [
            {
                "blueprintName": "idp-Invoice-aaaa",
                "blueprintArn": "arn:aws:x:::b/a",
                "blueprintVersion": "1",
            }
        ]
        service.blueprint_creator.list_blueprints.return_value = {"blueprints": []}

        result = service.cleanup_orphaned_blueprints("v1")

        assert result["deleted_count"] == 1

    def test_a_listing_failure_is_reported_as_a_failed_cleanup(self, service):
        service.blueprint_creator.list_all_blueprints_with_prefix.side_effect = (
            _client_error("AccessDeniedException", "ListBlueprints")
        )

        result = service.cleanup_orphaned_blueprints("v1")

        assert result["success"] is False
        assert result["message"].startswith("Cleanup failed:")
        assert result["deleted_count"] == 0
        service.blueprint_creator.delete_blueprint.assert_not_called()


@pytest.mark.unit
class TestRemoveAwsStandardBlueprintsFromProject:
    @pytest.fixture
    def service(self) -> Any:
        return _service()

    def test_only_the_custom_blueprints_are_kept(self, service):
        service.blueprint_creator.list_blueprints.return_value = {
            "blueprints": [
                {"blueprintArn": "arn:aws:bedrock:us-west-2:aws:blueprint/payslip"},
                {"blueprintArn": "arn:aws:bedrock:::blueprint/idp-Invoice-a"},
            ]
        }

        service._remove_aws_standard_blueprints_from_project()

        kwargs = service.blueprint_creator.update_project_with_custom_configurations.call_args.kwargs
        assert kwargs["customConfiguration"] == {
            "blueprints": [
                {"blueprintArn": "arn:aws:bedrock:::blueprint/idp-Invoice-a"}
            ]
        }

    def test_a_project_of_custom_blueprints_only_is_not_rewritten(self, service):
        service.blueprint_creator.list_blueprints.return_value = {
            "blueprints": [
                {"blueprintArn": "arn:aws:bedrock:::blueprint/idp-Invoice-a"}
            ]
        }

        service._remove_aws_standard_blueprints_from_project()

        service.blueprint_creator.update_project_with_custom_configurations.assert_not_called()

    def test_a_listing_failure_is_swallowed(self, service):
        service.blueprint_creator.list_blueprints.side_effect = _client_error(
            "AccessDenied", "ListBlueprints"
        )

        service._remove_aws_standard_blueprints_from_project()

        service.blueprint_creator.update_project_with_custom_configurations.assert_not_called()


@pytest.mark.unit
class TestSynchronizeDeletes:
    @pytest.fixture
    def service(self) -> Any:
        return _service()

    def test_a_blueprint_from_another_stack_is_never_deleted(self, service):
        """The prefix test is the only thing keeping this out of another deployment's
        blueprints, which share the account."""
        foreign = {
            "blueprintName": "other-stack-Invoice-aaaa",
            "blueprintArn": "arn:aws:x:::b/foreign",
            "blueprintVersion": "1",
        }

        service._synchronize_deletes([foreign], [])

        service.blueprint_creator.delete_blueprint.assert_not_called()
        service.blueprint_creator.update_project_with_custom_configurations.assert_not_called()

    def test_a_claimed_blueprint_is_kept_even_with_a_matching_prefix(self, service):
        claimed = {
            "blueprintName": "idp-Invoice-aaaa",
            "blueprintArn": "arn:aws:x:::b/claimed",
            "blueprintVersion": "1",
        }

        service._synchronize_deletes([claimed], ["arn:aws:x:::b/claimed"])

        service.blueprint_creator.delete_blueprint.assert_not_called()

    @pytest.fixture
    def orphans(self) -> list:
        return [
            {
                "blueprintName": f"idp-Old{i}-aaaa",
                "blueprintArn": f"arn:aws:x:::b/o{i}",
                "blueprintVersion": "1",
            }
            for i in (1, 2, 3)
        ]

    def test_one_delete_failure_does_not_abandon_the_rest(self, service, orphans):
        """The project's blueprint list is rewritten first — BDA refuses to delete an
        associated blueprint — so a `try` around the whole loop left every later
        orphan both undeleted and no longer visible to the project-scoped retrieval.
        The assertion is that all three deletes are attempted."""
        service.blueprint_creator.list_blueprints.return_value = {
            "blueprints": [{"blueprintArn": bp["blueprintArn"]} for bp in orphans]
        }
        service.blueprint_creator.delete_blueprint.side_effect = [
            _client_error("ConflictException", "DeleteBlueprint"),
            True,
            True,
        ]

        failed = service._synchronize_deletes(orphans, [])

        assert service.blueprint_creator.delete_blueprint.call_count == 3
        assert failed == ["arn:aws:x:::b/o1"]
        payload = service.blueprint_creator.update_project_with_custom_configurations.call_args.kwargs[
            "customConfiguration"
        ]
        assert payload == {"blueprints": []}

    def test_a_delete_that_reports_false_is_reported_as_orphaned(
        self, service, orphans
    ):
        """`BDABlueprintCreator.delete_blueprint` reports failure by returning `False`
        rather than raising, so the return value is the only signal there is — and it
        was discarded. An orphan left behind here is disassociated already, so the
        caller has to be told to run the account-wide cleanup."""
        service.blueprint_creator.list_blueprints.return_value = {
            "blueprints": [{"blueprintArn": bp["blueprintArn"]} for bp in orphans]
        }
        service.blueprint_creator.delete_blueprint.side_effect = [True, False, True]

        assert service._synchronize_deletes(orphans, []) == ["arn:aws:x:::b/o2"]

    def test_deleting_everything_reports_nothing_orphaned(self, service, orphans):
        service.blueprint_creator.list_blueprints.return_value = {
            "blueprints": [{"blueprintArn": bp["blueprintArn"]} for bp in orphans]
        }
        service.blueprint_creator.delete_blueprint.return_value = True

        assert service._synchronize_deletes(orphans, []) == []


@pytest.mark.unit
class TestDeleteProject:
    """Deleting the project for a config version; the DynamoDB row must go with it or
    the next lookup hands back an ARN that no longer resolves."""

    @pytest.fixture
    def service(self) -> Any:
        return _service(region="eu-west-1")

    def test_the_tracking_row_is_cleaned_up_in_the_services_region(self, service):
        table = MagicMock()
        table.scan.return_value = {
            "Items": [{"Configuration": "BdaProject#v1", "ProjectArn": PROJECT_ARN}]
        }

        with (
            patch("boto3.resource") as resource,
            patch.dict("os.environ", {"CONFIGURATION_TABLE_NAME": "config-table"}),
        ):
            resource.return_value.Table.return_value = table
            assert service.delete_project(PROJECT_ARN) is True

        assert resource.call_args.kwargs["region_name"] == "eu-west-1"
        table.delete_item.assert_called_once_with(
            Key={"Configuration": "BdaProject#v1"}
        )
        service.blueprint_creator.bedrock_client.delete_data_automation_project.assert_called_once_with(
            projectArn=PROJECT_ARN
        )

    def test_without_a_configured_table_the_project_is_still_deleted(self, service):
        with patch.dict("os.environ", {}, clear=True):
            assert service.delete_project(PROJECT_ARN) is True

        service.blueprint_creator.bedrock_client.delete_data_automation_project.assert_called_once()

    def test_a_failed_project_delete_answers_false(self, service):
        service.blueprint_creator.bedrock_client.delete_data_automation_project.side_effect = _client_error(
            "ConflictException", "DeleteDataAutomationProject"
        )

        with patch.dict("os.environ", {}, clear=True):
            assert service.delete_project(PROJECT_ARN) is False


# ---------------------------------------------------------------------------
# The project ARN is Optional on the attribute and required at every use
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProjectArnIsRequiredWhereItIsUsed:
    """`dataAutomationProjectArn` is legitimately `None` — the caller may be about to
    create the project, or may want only the schema transforms — and it is legitimately
    assigned after construction, which three production call sites do. What is not
    legitimate is a `None` reaching a BDA API: nine call sites used to pass the
    attribute straight into a parameter declared `str`, where it becomes a botocore
    `ParamValidationError` or a `TypeError` several frames away naming neither this
    class nor the missing ARN.

    Every one of those reads now goes through `_project_arn`, so the narrowing is done
    once and holds at runtime. `bda_blueprint_service.py` additionally carries
    `# pyright: reportArgumentType=error`, which fails the type gate if a tenth site
    reads the attribute directly; these two tests are the runtime half, which is what
    catches a read the type checker cannot see (a `getattr`, or a subclass).
    """

    def test_the_accessor_answers_the_arn_when_there_is_one(self):
        assert _service()._project_arn == PROJECT_ARN

    def test_the_accessor_refuses_rather_than_handing_back_none(self):
        service = _service()
        service.dataAutomationProjectArn = None

        with pytest.raises(RuntimeError, match="no BDA project ARN"):
            service._project_arn

    def test_an_arn_assigned_after_construction_is_used(self):
        """Three production call sites construct the service, create the project, then
        assign the ARN. Reading it through the accessor must not have frozen the
        constructor's value."""
        service = _service()
        service.dataAutomationProjectArn = None
        service.dataAutomationProjectArn = RECORDED_ARN

        assert service._project_arn == RECORDED_ARN

    @pytest.mark.parametrize(
        "call",
        [
            pytest.param(
                lambda s: s._synchronize_deletes(
                    [
                        {
                            "blueprintName": "idp-Old-aaaa",
                            "blueprintArn": "arn:aws:x:::b/o1",
                            "blueprintVersion": "1",
                        }
                    ],
                    [],
                ),
                id="synchronize-deletes",
            ),
            pytest.param(
                lambda s: s._remove_aws_standard_blueprints_from_project(),
                id="remove-aws-standard",
            ),
            pytest.param(
                lambda s: s._process_classes_parallel([_idp_class()], []),
                id="process-classes-parallel",
            ),
            pytest.param(
                lambda s: s.create_blueprints_from_custom_configuration(
                    version="v1", sync_direction="idp_to_bda", sync_mode="replace"
                ),
                id="create-blueprints-from-configuration",
            ),
        ],
    )
    def test_no_bda_call_is_made_with_a_missing_project_arn(self, call):
        """The assertion is on what must *not* happen: a call reaching BDA with no
        project to name. Two of these swallow the RuntimeError by design, so asserting
        on the exception alone would not cover them."""
        service = _service()
        service.dataAutomationProjectArn = None
        service.config_manager.get_configuration.return_value = None

        try:
            call(service)
        except Exception:
            pass

        service.blueprint_creator.update_project_with_custom_configurations.assert_not_called()
        service.blueprint_creator.bulk_update_data_automation_project.assert_not_called()
        service.blueprint_creator.list_blueprints.assert_not_called()
