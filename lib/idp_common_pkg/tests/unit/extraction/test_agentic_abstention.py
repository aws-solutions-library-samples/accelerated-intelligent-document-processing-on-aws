# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Advanced extraction must be able to say "I could not read this" (#782).

A ``required`` property renders as a non-nullable Pydantic field, so on the agentic
path an unreadable value had no representable answer: of ``null``, ``""``, omitting
the key and ``0.0``, only ``0.0`` validated. The retry loop then told the agent its
answer failed validation and asked it to fix it — so the contract actively pushed a
*fabricated* zero, which is schema-valid, silent and indistinguishable from a real
zero. Simple mode was never affected: it emits plain JSON, so ``null`` survives and
validation reports it loudly.

The fix relaxes required-ness only for the TRANSPORT model handed to the agent, and
leaves enforcement to ``extraction.validation`` against the real schema — which
treats null as absent and therefore reports it as a ``required`` violation, feeds it
back for self-correction, and can escalate it. So required-ness is not weakened; the
check moves from a place where it forces a fabricated value to one where it produces
a visible one.
"""

from __future__ import annotations

import copy

import pytest

from idp_common.extraction.validation import validate_extraction
from idp_common.schema import (
    create_pydantic_model_from_json_schema,
    relax_required_for_transport,
)

ROW_PROPS = {
    "Date": {"type": "string"},
    "Description": {"type": "string"},
    "Amount": {"type": "number"},
}


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
                    "properties": dict(ROW_PROPS),
                    "required": ["Date", "Description", "Amount"],
                },
            },
        },
        "required": ["AccountNumber", "Transactions"],
    }


# ---------------------------------------------------------------------------
# The schema transform
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_required_is_removed_at_every_level():
    """List ROWS are the case that matters — that is where per-cell abstention is
    needed — so a top-level-only relaxation would miss the actual bug."""
    relaxed = relax_required_for_transport(_statement_schema())
    assert "required" not in relaxed
    assert "required" not in relaxed["properties"]["Transactions"]["items"]


@pytest.mark.unit
def test_other_constraints_are_preserved():
    """Only required-ness is relaxed. minItems in particular is what makes a
    truncated list detectable, so losing it would trade one silent failure for
    another."""
    relaxed = relax_required_for_transport(_statement_schema())
    assert relaxed["properties"]["Transactions"]["minItems"] == 1
    assert relaxed["properties"]["Transactions"]["items"]["properties"] == ROW_PROPS
    assert relaxed["$id"] == "BankStatement"
    assert relaxed["type"] == "object"


@pytest.mark.unit
def test_the_input_schema_is_not_mutated():
    """The caller keeps using the real schema for validation, so mutating it in
    place would disable the very check this fix relies on."""
    schema = _statement_schema()
    before = copy.deepcopy(schema)
    relax_required_for_transport(schema)
    assert schema == before


@pytest.mark.unit
def test_a_property_actually_named_required_is_kept():
    """``required`` is also a legal property NAME. In that position its value is a
    schema, not a list of names, and dropping it would silently delete a field."""
    schema = {
        "type": "object",
        "properties": {
            "required": {"type": "string"},
            "Other": {"type": "number"},
        },
        "required": ["required", "Other"],
    }
    relaxed = relax_required_for_transport(schema)
    assert "required" not in relaxed  # the constraint list is gone
    assert relaxed["properties"]["required"] == {"type": "string"}  # the field remains


@pytest.mark.unit
def test_defs_and_refs_are_relaxed_too():
    schema = {
        "type": "object",
        "$defs": {
            "Row": {
                "type": "object",
                "properties": dict(ROW_PROPS),
                "required": ["Amount"],
            }
        },
        "properties": {"Rows": {"type": "array", "items": {"$ref": "#/$defs/Row"}}},
        "required": ["Rows"],
    }
    relaxed = relax_required_for_transport(schema)
    assert "required" not in relaxed["$defs"]["Row"]
    assert relaxed["properties"]["Rows"]["items"] == {"$ref": "#/$defs/Row"}


# ---------------------------------------------------------------------------
# The behaviour change, stated as the issue states it
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_only_a_fabricated_zero_validates_without_the_relaxation():
    """Pins the bug itself, so a regression is visible rather than theoretical."""
    row_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "Txn",
        "type": "object",
        "properties": dict(ROW_PROPS),
        "required": ["Date", "Description", "Amount"],
    }
    strict = create_pydantic_model_from_json_schema(row_schema, "TxnStrict")
    base = {"Date": "2024-01-02", "Description": "SEQ00001 x"}

    accepted = set()
    for label, amount in [
        ("null", None),
        ("empty", ""),
        ("omitted", ...),
        ("zero", 0.0),
    ]:
        candidate = dict(base) if amount is ... else dict(base, Amount=amount)
        try:
            strict.model_validate(candidate)
            accepted.add(label)
        except Exception:  # noqa: BLE001 - the rejection is the assertion
            pass
    assert accepted == {"zero"}, (
        "the strict model must accept ONLY a fabricated value; "
        f"accepted {sorted(accepted)}"
    )


@pytest.mark.unit
def test_the_transport_model_accepts_abstention_but_not_a_wrong_type():
    """Null and an omitted key become expressible. An empty string stays rejected —
    it is not an abstention for a number, it is a type error."""
    row_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "Txn",
        "type": "object",
        "properties": dict(ROW_PROPS),
        "required": ["Date", "Description", "Amount"],
    }
    transport = create_pydantic_model_from_json_schema(
        relax_required_for_transport(row_schema), "TxnTransport"
    )
    base = {"Date": "2024-01-02", "Description": "SEQ00001 x"}

    transport.model_validate(dict(base, Amount=None))
    transport.model_validate(dict(base))
    transport.model_validate(dict(base, Amount=0.0))
    with pytest.raises(Exception):
        transport.model_validate(dict(base, Amount=""))


@pytest.mark.unit
def test_abstention_is_still_reported_by_validation():
    """The whole point: relaxing the transport model must not make the abstention
    invisible. It is reported exactly as simple mode reports it today, and is
    attributed to a top-level field so escalation can target it."""
    schema = _statement_schema()
    extracted = {
        "AccountNumber": "123",
        "Transactions": [
            {"Date": "2024-01-02", "Description": "SEQ00001 ACH", "Amount": None},
            {"Date": "2024-01-03", "Description": "SEQ00002 POS", "Amount": 12.5},
        ],
    }
    # Accepted in transport...
    transport = create_pydantic_model_from_json_schema(
        relax_required_for_transport(schema), "StatementTransport", clean_schema=False
    )
    transport.model_validate(extracted)

    # ...and still reported against the real schema.
    report = validate_extraction(extracted, schema)
    assert report.valid is False
    assert [e.validator for e in report.errors] == ["required"]
    assert "'Amount' is a required property" in str(report.errors[0])
    assert report.failed_top_level_fields == {"Transactions"}


@pytest.mark.unit
def test_a_fabricated_zero_is_indistinguishable_and_passes_validation():
    """Why the abstention path has to exist: the value the old contract forced is
    accepted silently, which is what made the failure invisible."""
    schema = _statement_schema()
    fabricated = {
        "AccountNumber": "123",
        "Transactions": [
            {"Date": "2024-01-02", "Description": "SEQ00001 ACH", "Amount": 0.0}
        ],
    }
    report = validate_extraction(fabricated, schema)
    assert report.valid is True, "a fabricated 0.0 raises no error — hence the fix"


# ---------------------------------------------------------------------------
# The retry feedback must not ask for a guess
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_required_feedback_sanctions_abstention():
    schema = _statement_schema()
    report = validate_extraction(
        {"AccountNumber": "123", "Transactions": [{"Date": "d", "Description": "x"}]},
        schema,
    )
    feedback = report.agent_feedback()
    assert "'Amount' is a required property" in feedback
    assert "do NOT guess" in feedback
    assert "leave it null" in feedback


@pytest.mark.unit
def test_the_abstention_note_is_not_added_for_other_violations():
    """Keep the feedback tight: a type or format violation IS the agent's to fix,
    and telling it "null is fine" there would invite dropped data."""
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"Amount": {"type": "number"}},
    }
    report = validate_extraction({"Amount": "not-a-number"}, schema)
    assert report.valid is False
    feedback = report.agent_feedback()
    assert "do NOT guess" not in feedback


@pytest.mark.unit
def test_valid_report_feedback_is_unchanged():
    report = validate_extraction(
        {
            "AccountNumber": "1",
            "Transactions": [{"Date": "d", "Description": "x", "Amount": 1.0}],
        },
        _statement_schema(),
    )
    assert report.valid is True
    assert (
        report.agent_feedback()
        == "All extracted fields satisfy the schema constraints."
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
