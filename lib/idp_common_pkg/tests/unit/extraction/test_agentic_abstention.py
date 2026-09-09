# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Advanced extraction must be able to say "I could not read this CELL" (#782).

A ``required`` property renders as a non-nullable Pydantic field, so on the agentic
path an unreadable value had no representable answer: of ``null``, ``""``, omitting
the key and ``0.0``, only ``0.0`` validated. The retry loop then told the agent its
answer failed validation and asked it to fix it — so the contract actively pushed a
*fabricated* zero. Simple mode was never affected.

``required`` conflates structural presence (the key exists, a list is a list) with
having a readable value. Only the second may relax. So the transport model keeps
``required`` and makes every SCALAR leaf nullable: a null cell is accepted; an
omitted key, a nulled list, an empty tool call or a misspelled key set still fail.
Enforcement of the value then moves to ``extraction.validation``, which reports a
null required scalar as ``'X' is a required property`` and feeds it back.
"""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from idp_common.extraction.validation import (
    required_null_paths,
    validate_extraction,
)
from idp_common.schema import (
    create_pydantic_model_from_json_schema,
    nullable_leaves_for_transport,
)

ROW_PROPS = {
    "Date": {"type": "string", "format": "date"},
    "Description": {"type": "string"},
    "Amount": {"type": "number"},
    "Kind": {"type": "string", "enum": ["debit", "credit"]},
}
ROW_REQUIRED = ["Date", "Description", "Amount", "Kind"]


def _statement_schema() -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "BankStatement",
        "type": "object",
        "properties": {
            "AccountNumber": {"type": "string"},
            "Transactions": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": copy.deepcopy(ROW_PROPS),
                    "required": list(ROW_REQUIRED),
                },
            },
        },
        "required": ["AccountNumber", "Transactions"],
    }


def _row(**over) -> dict:
    row = {
        "Date": "2024-01-02",
        "Description": "SEQ00001 x",
        "Amount": 12.5,
        "Kind": "debit",
    }
    row.update(over)
    return row


def _accepts(model, payload) -> bool:
    try:
        model.model_validate(payload)
        return True
    except Exception:  # noqa: BLE001 - the rejection is the observation
        return False


# ---------------------------------------------------------------------------
# The schema transform
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_required_is_kept_at_every_level():
    """The whole point of this design over dropping ``required``: structure is
    still enforced, only the value is relaxable."""
    t = nullable_leaves_for_transport(_statement_schema())
    assert t["required"] == ["AccountNumber", "Transactions"]
    assert t["properties"]["Transactions"]["items"]["required"] == ROW_REQUIRED


@pytest.mark.unit
def test_scalar_leaves_become_nullable_and_nothing_else_does():
    t = nullable_leaves_for_transport(_statement_schema())
    items = t["properties"]["Transactions"]["items"]
    assert t["properties"]["AccountNumber"]["type"] == ["string", "null"]
    assert items["properties"]["Amount"]["type"] == ["number", "null"]
    assert items["properties"]["Date"]["type"] == ["string", "null"]
    assert items["properties"]["Date"]["format"] == "date"  # other keywords kept
    # Arrays and objects are structural and stay exactly as declared.
    assert t["properties"]["Transactions"]["type"] == "array"
    assert t["properties"]["Transactions"]["minItems"] == 1
    assert items["type"] == "object"
    assert t["type"] == "object"


@pytest.mark.unit
def test_enum_gains_none_so_the_jsonschema_validator_agrees_with_the_type():
    t = nullable_leaves_for_transport(_statement_schema())
    assert t["properties"]["Transactions"]["items"]["properties"]["Kind"]["enum"] == [
        "debit",
        "credit",
        None,
    ]


@pytest.mark.unit
def test_type_arrays_and_already_nullable_leaves_are_handled():
    t = nullable_leaves_for_transport(
        {
            "type": "object",
            "properties": {
                "A": {"type": ["string", "integer"]},
                "B": {"type": ["string", "null"]},
            },
        }
    )
    assert t["properties"]["A"]["type"] == ["string", "integer", "null"]
    # Already nullable: 'null' is not a scalar type so the leaf is left alone.
    assert t["properties"]["B"]["type"] == ["string", "null"]


@pytest.mark.unit
def test_the_input_schema_is_not_mutated():
    schema = _statement_schema()
    before = copy.deepcopy(schema)
    nullable_leaves_for_transport(schema)
    assert schema == before


@pytest.mark.unit
def test_defs_and_combinator_branches_are_recursed_but_not_themselves_widened():
    schema = {
        "type": "object",
        "$defs": {
            "Row": {
                "type": "object",
                "properties": {"A": {"type": "number"}},
                "required": ["A"],
            }
        },
        "properties": {
            "Rows": {"type": "array", "items": {"$ref": "#/$defs/Row"}},
            "Either": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "object", "properties": {"z": {"type": "integer"}}},
                ]
            },
        },
        "required": ["Rows"],
    }
    t = nullable_leaves_for_transport(schema)
    assert t["$defs"]["Row"]["properties"]["A"]["type"] == ["number", "null"]
    assert t["$defs"]["Row"]["required"] == ["A"]
    assert t["properties"]["Rows"]["items"] == {"$ref": "#/$defs/Row"}
    assert t["properties"]["Either"]["anyOf"][0]["type"] == ["string", "null"]
    assert t["properties"]["Either"]["anyOf"][1]["properties"]["z"]["type"] == [
        "integer",
        "null",
    ]
    assert (
        "type" not in t["properties"]["Either"]
    )  # the combinator node itself untouched


# ---------------------------------------------------------------------------
# The generated model: the behaviour table from the design
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def models():
    strict = create_pydantic_model_from_json_schema(
        _statement_schema(), "StatementStrict", clean_schema=False
    )
    transport = create_pydantic_model_from_json_schema(
        nullable_leaves_for_transport(_statement_schema()),
        "StatementTransport",
        clean_schema=False,
    )
    return strict, transport


def _doc(rows, account="123") -> dict:
    return {"AccountNumber": account, "Transactions": rows}


@pytest.mark.unit
def test_only_a_fabricated_zero_validates_without_the_transform(models):
    """Pins the bug itself, so a regression is visible rather than theoretical."""
    strict, _ = models
    accepted = {
        label
        for label, row in [
            ("null", _row(Amount=None)),
            ("empty", _row(Amount="")),
            ("omitted", {k: v for k, v in _row().items() if k != "Amount"}),
            ("zero", _row(Amount=0.0)),
        ]
        if _accepts(strict, _doc([row]))
    }
    assert accepted == {"zero"}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("label", "payload", "expected"),
    [
        ("cell null", lambda: _doc([_row(Amount=None)]), True),
        ("enum cell null", lambda: _doc([_row(Kind=None)]), True),
        ("top-level scalar null", lambda: _doc([_row()], account=None), True),
        (
            "cell key omitted",
            lambda: _doc([{k: v for k, v in _row().items() if k != "Amount"}]),
            False,
        ),
        ("cell empty string", lambda: _doc([_row(Amount="")]), False),
        ("whole list null", lambda: _doc(None), False),
        ("empty tool call", lambda: {}, False),
        ("misspelled keys", lambda: {"account_number": "1", "transactions": []}, False),
        ("real zero", lambda: _doc([_row(Amount=0.0)]), True),
    ],
)
def test_transport_model_behaviour_table(models, label, payload, expected):
    """Cell-level abstention is expressible; every STRUCTURAL failure that dropping
    ``required`` would have let through is still rejected."""
    _, transport = models
    assert _accepts(transport, payload()) is expected, label


@pytest.mark.unit
def test_multi_instance_wrapper_structure_survives():
    """``schema.multi_instance`` synthesises ``required: ["instances"]``. That is
    machinery, not a readable value, and must never be relaxable."""
    wrapper = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "W",
        "type": "object",
        "properties": {
            "instances": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": copy.deepcopy(ROW_PROPS),
                    "required": ROW_REQUIRED,
                },
            }
        },
        "required": ["instances"],
    }
    m = create_pydantic_model_from_json_schema(
        nullable_leaves_for_transport(wrapper), "W", clean_schema=False
    )
    assert _accepts(m, {"instances": None}) is False
    assert _accepts(m, {}) is False
    assert _accepts(m, {"instances": []}) is False  # minItems still binds
    assert _accepts(m, {"instances": [_row(Amount=None)]}) is True


# ---------------------------------------------------------------------------
# Enforcement moves to validation, and stays visible
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_abstention_is_reported_by_validation_and_attributed(models):
    _, transport = models
    extracted = _doc([_row(Amount=None), _row(Description="SEQ00002", Amount=3.0)])
    transport.model_validate(extracted)  # accepted in transport...
    report = validate_extraction(extracted, _statement_schema())  # ...still reported
    assert report.valid is False
    assert [e.validator for e in report.errors] == ["required"]
    assert "'Amount' is a required property" in str(report.errors[0])
    assert report.failed_top_level_fields == {"Transactions"}
    assert report.errors[0].leaf is True


@pytest.mark.unit
def test_a_fabricated_zero_passes_validation_silently():
    """Why the abstention path must exist: the value the old contract forced raises
    nothing at all."""
    assert (
        validate_extraction(_doc([_row(Amount=0.0)]), _statement_schema()).valid is True
    )


# ---------------------------------------------------------------------------
# The retry feedback: sanction abstention for cells, never for lists
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_feedback_sanctions_abstention_for_a_null_cell():
    report = validate_extraction(_doc([_row(Amount=None)]), _statement_schema())
    fb = report.agent_feedback()
    assert "'Amount' is a required property" in fb
    assert "do NOT guess" in fb
    assert "never null a whole list" in fb


@pytest.mark.unit
def test_feedback_does_not_sanction_nulling_a_whole_list():
    """A required LIST is missing. The note that says "leave it null" must not
    appear — that is the #666 failure, and ``build_empty_list_feedback`` says the
    opposite."""
    report = validate_extraction(
        {"AccountNumber": "1", "Transactions": None}, _statement_schema()
    )
    assert report.valid is False
    assert report.errors[0].validator == "required"
    assert report.errors[0].leaf is False
    assert "do NOT guess" not in report.agent_feedback()


@pytest.mark.unit
def test_feedback_note_is_not_added_for_type_or_format_violations():
    report = validate_extraction(
        _doc([_row(Amount="not-a-number")]), _statement_schema()
    )
    assert report.valid is False
    assert "do NOT guess" not in report.agent_feedback()


@pytest.mark.unit
def test_feedback_note_always_shows_at_least_one_of_its_errors():
    """25 date-format errors sort before 5 required errors; truncation used to hide
    every required error while the note still appeared."""
    rows = [_row(Date="bad-date", Description=f"SEQ{i:05d}") for i in range(30)]
    rows += [_row(Amount=None, Description=f"SEQ{i:05d}") for i in range(30, 35)]
    report = validate_extraction(_doc(rows), _statement_schema())
    fb = report.agent_feedback()
    assert "do NOT guess" in fb
    assert "'Amount' is a required property" in fb
    assert "more violation(s)." in fb and "of the same kind" not in fb


# ---------------------------------------------------------------------------
# Ungated abstention accounting
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_required_null_paths_counts_and_locates_abstentions():
    data = _doc([_row(Amount=None), _row(), _row(Amount=None, Kind=None)], account=None)
    paths, total = required_null_paths(data, _statement_schema())
    assert total == 4
    assert paths == [
        "AccountNumber",
        "Transactions[0].Amount",
        "Transactions[2].Amount",
        "Transactions[2].Kind",
    ]


@pytest.mark.unit
def test_required_null_paths_respects_limit_but_reports_the_true_total():
    data = _doc([_row(Amount=None) for _ in range(50)])
    paths, total = required_null_paths(data, _statement_schema(), limit=10)
    assert total == 50 and len(paths) == 10


@pytest.mark.unit
def test_required_null_paths_is_empty_when_nothing_is_missing():
    assert required_null_paths(_doc([_row()]), _statement_schema()) == ([], 0)


@pytest.mark.unit
def test_required_null_paths_resolves_a_ref_one_level():
    schema = {
        "type": "object",
        "$defs": {
            "Row": {
                "type": "object",
                "properties": {"A": {"type": "number"}},
                "required": ["A"],
            }
        },
        "properties": {"Rows": {"type": "array", "items": {"$ref": "#/$defs/Row"}}},
        "required": ["Rows"],
    }
    assert required_null_paths({"Rows": [{"A": None}, {"A": 1}]}, schema) == (
        ["Rows[0].A"],
        1,
    )


# ---------------------------------------------------------------------------
# The shard path now carries a schema validator
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extract_one_shard_passes_the_schema_validator_to_the_runner():
    """The per-shard runtime had NO in-loop schema validation — the strict model
    was its only guard. With nullable leaves that guard no longer catches a null
    cell, so the validator must reach the shard agent."""
    from idp_common.extraction.runtime import extract_one_shard

    class M(BaseModel):
        account: str | None = None

    seen = {}

    async def runner(*, shard_index, total_shards, payload, **kwargs):
        seen.update(kwargs)
        return M(account="A"), {"metering": {}}

    def validator(_data):
        return True, ""

    asyncio.run(
        extract_one_shard(
            shard_index=0,
            total_shards=1,
            payload={"page_start": 0, "page_end": 1, "total_pages": 1, "content": []},
            model_id="m",
            data_format=M,
            config=None,  # type: ignore[arg-type]
            section_id="s",
            shard_runner=runner,
            schema_validator=validator,
        )
    )
    assert seen.get("schema_validator") is validator


# ---------------------------------------------------------------------------
# The fix is applied where it matters: at the service's transport-model sites
# ---------------------------------------------------------------------------


def _capturing_model_builder(captured: dict):
    from idp_common.schema import create_pydantic_model_from_json_schema as real

    def _build(*, schema, class_label, clean_schema=False, **kw):
        captured[class_label] = schema
        return real(schema=schema, class_label=class_label, clean_schema=clean_schema)

    return _build


@pytest.mark.unit
def test_the_shard_plan_builds_its_model_from_the_nullable_transport_schema():
    """Reverting the transform at ``_build_agentic_shard_plan`` must fail a test —
    previously nothing asserted the SERVICE used it."""
    from unittest.mock import patch

    from idp_common.config.models import IDPConfig
    from idp_common.extraction.service import ExtractionService

    svc = ExtractionService(
        config=IDPConfig(
            **{"extraction": {"mode": "advanced", "agentic": {"enabled": True}}}
        )
    )
    svc._reset_context()
    svc._class_schema = _statement_schema()
    svc._class_label = "BankStatement"
    svc._document_text = "no tables here"
    captured: dict = {}
    with (
        patch(
            "idp_common.extraction.service.create_pydantic_model_from_json_schema",
            side_effect=_capturing_model_builder(captured),
        ),
        patch.object(ExtractionService, "_build_shard_payloads", return_value=[]),
    ):
        svc._build_agentic_shard_plan(SimpleNamespace(class_label="BankStatement"))
    schema = captured["BankStatement"]
    assert schema["required"] == ["AccountNumber", "Transactions"]  # kept
    row = schema["properties"]["Transactions"]["items"]
    assert row["required"] == ROW_REQUIRED  # kept
    assert row["properties"]["Amount"]["type"] == ["number", "null"]  # widened


@pytest.mark.unit
def test_run_shard_agent_forwards_the_schema_validator():
    """The last hop of the threading — the one that makes the validator DO anything.
    The first hop (extract_one_shard -> runner) was already pinned; this one was not."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from idp_common.extraction import agentic_idp

    class M(BaseModel):
        a: str | None = None

    def validator(_d):
        return True, ""

    fake = AsyncMock(return_value=(M(), {}))
    with patch.object(agentic_idp, "structured_output_async", fake):
        asyncio.run(
            agentic_idp._run_shard_agent(
                shard_index=0,
                total_shards=2,
                page_start=0,
                page_end=1,
                total_pages=2,
                model_id="m",
                data_format=M,
                shard_prompt="p",
                config=None,  # type: ignore[arg-type]
                context="Extraction",
                max_retries=1,
                connect_timeout=1.0,
                read_timeout=1.0,
                max_tokens=None,
                checkpoint_callback=None,
                schema_validator=validator,
            )
        )
    assert fake.await_args.kwargs["schema_validator"] is validator


# ---------------------------------------------------------------------------
# Shard-scoped validation: presence is not a shard's job
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_shard_validation_schema_drops_required_and_minitems_everywhere():
    from idp_common.extraction.validation import shard_validation_schema

    s = shard_validation_schema(_statement_schema())
    assert "required" not in s
    assert "required" not in s["properties"]["Transactions"]["items"]
    assert "minItems" not in s["properties"]["Transactions"]
    # Everything a shard CAN fix survives.
    assert (
        s["properties"]["Transactions"]["items"]["properties"]["Date"]["format"]
        == "date"
    )
    assert s["properties"]["Transactions"]["items"]["properties"]["Kind"]["enum"] == [
        "debit",
        "credit",
    ]


@pytest.mark.unit
def test_shard_scoped_validator_accepts_a_correct_partial_shard():
    """Shard 3 of 5 carries rows but not the document-level scalars; a cover-page
    shard carries no rows at all. Both are CORRECT shards and must pass, or each
    costs up to three extra agent turns and the cover page is told to invent rows."""
    from idp_common.config.models import IDPConfig
    from idp_common.extraction.service import ExtractionService

    svc = ExtractionService(
        config=IDPConfig(
            **{"extraction": {"mode": "advanced", "agentic": {"enabled": True}}}
        )
    )
    svc._reset_context()
    svc._class_schema = _statement_schema()
    full = svc._build_schema_validator()
    shard = svc._build_schema_validator(shard_scoped=True)
    assert full is not None and shard is not None

    rows_only = {
        "AccountNumber": None,
        "Transactions": [_row(), _row(Description="SEQ00002")],
    }
    cover_page = {"AccountNumber": "123", "Transactions": []}
    assert full(rows_only)[0] is False  # the full validator demands AccountNumber
    assert shard(rows_only) == (
        True,
        "All extracted fields satisfy the schema constraints.",
    )
    assert shard(cover_page)[0] is True  # minItems not enforced per shard
    # A shard can still be corrected on what it CAN fix.
    bad_date = {"AccountNumber": None, "Transactions": [_row(Date="not-a-date")]}
    ok, feedback = shard(bad_date)
    assert ok is False and "not a 'date'" in feedback and "required" not in feedback


# ---------------------------------------------------------------------------
# Review follow-ups
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_abstention_accounting_counts_scalar_cells_only():
    """A nulled LIST is the #666 loss, not an abstention; it must not be filed
    under the benign label — that is the whole point of ``FieldError.leaf``."""
    assert required_null_paths(
        {"AccountNumber": "1", "Transactions": None}, _statement_schema()
    ) == ([], 0)
    assert required_null_paths(
        {"AccountNumber": None, "Transactions": [_row()]}, _statement_schema()
    ) == (
        ["AccountNumber"],
        1,
    )
    wrapper = {
        "type": "object",
        "properties": {"instances": {"type": "array", "items": {"type": "object"}}},
        "required": ["instances"],
    }
    assert required_null_paths({"instances": None}, wrapper) == ([], 0)


@pytest.mark.unit
def test_const_leaves_are_not_widened():
    t = nullable_leaves_for_transport(
        {
            "type": "object",
            "properties": {"Currency": {"type": "string", "const": "USD"}},
        }
    )
    assert t["properties"]["Currency"] == {"type": "string", "const": "USD"}


@pytest.mark.unit
def test_leaf_attribution_resolves_a_ref_to_a_scalar_definition():
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "$defs": {"Amt": {"type": "number"}},
        "properties": {"X": {"$ref": "#/$defs/Amt"}},
        "required": ["X"],
    }
    report = validate_extraction({}, schema)
    assert report.errors[0].leaf is True
    assert "do NOT guess" in report.agent_feedback()


@pytest.mark.unit
def test_feedback_appends_the_required_error_rather_than_displacing_one():
    """25 format errors, a unique minItems-style error at index 24, and the required
    error beyond the cut: the unique error must still be shown."""
    rows = [_row(Date="bad-date", Description=f"SEQ{i:05d}") for i in range(24)]
    rows.append(_row(Kind="neither", Description="SEQ00024"))  # unique enum error
    rows += [_row(Amount=None, Description=f"SEQ{i:05d}") for i in range(25, 30)]
    fb = validate_extraction(_doc(rows), _statement_schema()).agent_feedback()
    assert "'neither' is not one of" in fb
    assert "'Amount' is a required property" in fb
    assert "do NOT guess" in fb


@pytest.mark.unit
def test_every_transport_model_goes_through_the_one_helper():
    """The three transport-model sites and the two shard fan-out sites each call a
    helper; the helper is what a test can pin. Reverting the transform or the shard
    scoping AT the helper now fails — and the sites are one-line delegations."""
    from idp_common.config.models import IDPConfig
    from idp_common.extraction.service import ExtractionService

    svc = ExtractionService(
        config=IDPConfig(
            **{"extraction": {"mode": "advanced", "agentic": {"enabled": True}}}
        )
    )
    svc._reset_context()
    svc._class_schema = _statement_schema()
    model = svc._transport_model(_statement_schema(), "Stmt")
    assert _accepts(model, _doc([_row(Amount=None)])) is True  # abstention
    assert _accepts(model, {}) is False  # structure still enforced
    assert _accepts(model, _doc(None)) is False

    shard = svc._shard_schema_validator()
    assert shard is not None
    rows_only = {"AccountNumber": None, "Transactions": [_row()]}
    assert shard(rows_only)[0] is True
    assert shard({"AccountNumber": "1", "Transactions": []})[0] is True


@pytest.mark.unit
def test_service_source_has_no_transport_or_shard_site_outside_the_helpers():
    """Belt and braces for the delegation: a new call site that bypasses the helper
    is the one thing the helper test cannot see."""
    import inspect

    from idp_common.extraction import service as svc_mod

    src = inspect.getsource(svc_mod)
    # The transform is called exactly once — inside _transport_model.
    assert src.count("nullable_leaves_for_transport(") == 1
    # The shard-scoped validator is built exactly once — inside
    # _shard_schema_validator — and no fan-out site builds its own.
    assert src.count("return self._build_schema_validator(shard_scoped=True)") == 1
    # (The full-schema validator is still passed directly where a WHOLE section is
    # validated, e.g. the escalation re-extraction; that is the non-shard rule.)
    assert src.count("self._build_schema_validator(shard_scoped=True)") == 1
    assert src.count("schema_validator=self._shard_schema_validator()") == 2


@pytest.mark.unit
def test_shard_schema_drops_row_count_bounds_but_keeps_a_property_named_minitems():
    from idp_common.extraction.validation import shard_validation_schema

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "Rows": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "uniqueItems": True,
            },
            "minItems": {"type": "string"},  # an attribute that happens to be named so
        },
        "required": ["Rows", "minItems"],
    }
    s = shard_validation_schema(schema)
    assert "minItems" not in s["properties"]["Rows"]
    assert "maxItems" not in s["properties"]["Rows"]
    assert s["properties"]["Rows"]["uniqueItems"] is True
    assert "minItems" in s["properties"]  # the PROPERTY survives
    assert "required" not in s


@pytest.mark.unit
def test_const_leaves_are_not_abstainable_anywhere():
    """The transform, the note predicate and the accounting must agree on const."""
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "Currency": {"type": "string", "const": "USD"},
            "Amt": {"type": "number"},
        },
        "required": ["Currency", "Amt"],
    }
    report = validate_extraction({"Amt": 1.0}, schema)
    assert report.errors[0].leaf is False
    assert "do NOT guess" not in report.agent_feedback()
    assert required_null_paths({"Currency": None, "Amt": None}, schema) == (["Amt"], 1)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
