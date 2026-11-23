# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A class whose ``$id`` contains a space is accepted by every metaschema check.

JSON Schema declares ``$id`` as ``format: uri-reference``. ``jsonschema`` enforces
that format only when ``rfc3986-validator`` (or ``rfc3987``) is importable, so the
same class (``"Bank Statement"`` in the ``bank-statement-sample`` preset) is
accepted in one environment and rejected in another. Evaluation hands each class
schema to Stickler, which runs ``Draft7Validator.check_schema``, and the
evaluation service turns the resulting error into a section with no scored
attributes.

The ``strict_uri_reference`` fixture makes the format check active in every
environment, so these tests do not depend on what happens to be installed.
"""

import copy
import re

import jsonschema
import pytest

from idp_common.config.class_names import (
    schema_id_as_uri_reference,
    with_uri_safe_schema_ids,
)

# Characters RFC 3986 permits anywhere in a URI-reference. Anything else — a
# space, a non-ASCII letter, a quote — makes the value invalid outright.
_URI_REFERENCE_CHARS = re.compile(r"^[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]*$")


def _is_uri_reference(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if not _URI_REFERENCE_CHARS.match(value):
        return False
    try:
        from rfc3986_validator import validate_rfc3986
    except ImportError:
        return True
    return bool(validate_rfc3986(value, rule="URI_reference"))


@pytest.fixture
def strict_uri_reference(monkeypatch):
    """Enforce ``format: uri-reference`` in ``check_schema`` for both drafts."""
    for validator_cls in (jsonschema.Draft7Validator, jsonschema.Draft202012Validator):
        checker = copy.deepcopy(validator_cls.FORMAT_CHECKER)
        checker.checks("uri-reference", raises=ValueError)(_is_uri_reference)
        monkeypatch.setattr(validator_cls, "FORMAT_CHECKER", checker)
    # The fixture must actually reject the authored name, or every test below
    # would pass for the wrong reason.
    with pytest.raises(jsonschema.SchemaError):
        jsonschema.Draft7Validator.check_schema({"$id": "Bank Statement"})


def _bank_statement_class() -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "Bank Statement",
        "x-aws-idp-document-type": "Bank Statement",
        "type": "object",
        "description": "A bank statement",
        "properties": {
            "Account Number": {
                "type": "string",
                "x-aws-idp-evaluation-method": "EXACT",
            },
            "Statement Period": {
                "type": "string",
                "x-aws-idp-evaluation-method": "EXACT",
            },
        },
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    ["Bank Statement", "Bank-Statement", "W2", "a:b", "x#y#z", "50% off", "請求書", ""],
)
def test_every_encoded_id_is_a_uri_reference(name):
    assert _is_uri_reference(schema_id_as_uri_reference(name))


@pytest.mark.unit
@pytest.mark.parametrize("name", ["Bank-Statement", "Bank_Statement", "W2", "a.b~c/d"])
def test_an_id_made_of_safe_characters_is_unchanged(name):
    assert schema_id_as_uri_reference(name) == name


@pytest.mark.unit
def test_every_nested_id_is_rewritten_and_instance_data_is_not():
    schema = {
        "$id": "Bank Statement",
        "$defs": {"Addr": {"$id": "Postal Address", "type": "object"}},
        "properties": {
            "$id": {"type": "string", "default": {"$id": "keep me"}},
            "kind": {"enum": [{"$id": "keep me too"}]},
            "either": {"anyOf": [{"$id": "Option A"}, {"type": "null"}]},
        },
    }
    authored = copy.deepcopy(schema)

    result = with_uri_safe_schema_ids(schema)

    assert schema == authored, "the authored schema must not be modified"
    assert result["$id"] == "Bank%20Statement"
    assert result["$defs"]["Addr"]["$id"] == "Postal%20Address"
    assert result["properties"]["$id"]["default"] == {"$id": "keep me"}
    assert result["properties"]["kind"]["enum"] == [{"$id": "keep me too"}]
    assert result["properties"]["either"]["anyOf"][0]["$id"] == "Option%20A"
    # A copy all the way down: editing the result cannot reach the authored schema.
    result["properties"]["either"]["anyOf"].append({})
    result["properties"]["kind"]["enum"][0]["$id"] = "changed"
    assert schema == authored


@pytest.mark.unit
def test_evaluation_scores_a_class_whose_id_contains_a_space(strict_uri_reference):
    from idp_common.evaluation.service import EvaluationService
    from idp_common.models import Section

    config = {"classes": [_bank_statement_class()]}
    service = EvaluationService(region="us-east-1", config=config, max_workers=1)
    expected = {"Account Number": "123", "Statement Period": "Jan"}
    actual = {"Account Number": "123", "Statement Period": "Feb"}

    result = service.evaluate_section(
        Section(section_id="1", classification="Bank Statement", page_ids=["1"]),
        expected,
        actual,
    )

    matched = {a.name: a.matched for a in result.attributes}
    assert matched == {"Account Number": True, "Statement Period": False}
    # The class keeps the name the author wrote wherever it is reported.
    assert result.document_class == "Bank Statement"


@pytest.mark.unit
def test_the_stickler_config_keeps_the_authored_class_name():
    from idp_common.evaluation.stickler_backend.mapper import SticklerConfigMapper

    authored = _bank_statement_class()
    config = SticklerConfigMapper.build_stickler_model_config(authored)

    assert authored["$id"] == "Bank Statement"
    assert config["schema"]["$id"] == "Bank%20Statement"
    assert config["schema"]["x-aws-idp-document-type"] == "Bank Statement"


@pytest.mark.unit
def test_discovery_accepts_a_class_whose_id_contains_a_space(strict_uri_reference):
    from idp_common.discovery.classes_discovery import ClassesDiscovery

    discovery = object.__new__(ClassesDiscovery)
    schema = _bank_statement_class()

    assert discovery._validate_json_schema(schema) == (True, "")
    assert schema["$id"] == "Bank Statement"


@pytest.mark.unit
def test_schema_authoring_accepts_a_class_whose_id_contains_a_space(
    strict_uri_reference,
):
    from idp_common.synthesis.schema_author import _validate_authored_schema

    schema = _bank_statement_class()

    assert _validate_authored_schema(schema) == (True, "")
    assert schema["$id"] == "Bank Statement"
