# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Config validation for ``x-aws-idp-instance-array`` (Designate mode).

The key names the top-level array whose length is the section's instance count.
A typo would otherwise fail *silently* at runtime — the count simply never
appears — which is precisely the silent-no-op failure mode this work exists to
remove. So it is validated at config time instead.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from idp_common.config.models import IDPConfig

KEY = "x-aws-idp-instance-array"


def _klass(**overrides):
    base = {
        "$id": "patient_packet",
        "type": "object",
        "properties": {
            "records": {"type": "array", "items": {"type": "object"}},
        },
    }
    base.update(overrides)
    return base


def test_valid_declaration_is_accepted():
    cfg = IDPConfig(classes=[_klass(**{KEY: "records"})])
    assert cfg.classes[0][KEY] == "records"


def test_absent_declaration_is_fine():
    """The overwhelmingly common case must stay untouched."""
    cfg = IDPConfig(classes=[_klass()])
    assert KEY not in cfg.classes[0]


def test_ref_items_are_allowed():
    """Items resolved via $ref cannot be type-checked here; allow them."""
    IDPConfig(
        classes=[
            _klass(
                **{
                    KEY: "records",
                    "properties": {
                        "records": {
                            "type": "array",
                            "items": {"$ref": "#/$defs/Record"},
                        }
                    },
                    "$defs": {"Record": {"type": "object"}},
                }
            )
        ]
    )


@pytest.mark.parametrize(
    "overrides,expected_fragment",
    [
        # A typo is the failure this validator exists to catch.
        ({KEY: "recrods"}, "not a top-level property"),
        (
            {KEY: "records", "properties": {"records": {"type": "string"}}},
            "must be an array",
        ),
        (
            {
                KEY: "records",
                "properties": {
                    "records": {"type": "array", "items": {"type": "string"}}
                },
            },
            "must be an object",
        ),
        ({KEY: ""}, "must be the name of a top-level array property"),
        ({KEY: ["records"]}, "must be the name of a top-level array property"),
        ({KEY: 7}, "must be the name of a top-level array property"),
    ],
)
def test_malformed_declarations_are_rejected(overrides, expected_fragment):
    with pytest.raises(ValidationError) as exc:
        IDPConfig(classes=[_klass(**overrides)])
    assert expected_fragment in str(exc.value)


def test_error_names_the_class_and_lists_available_properties():
    """The message has to be actionable — a typo needs the candidate names."""
    with pytest.raises(ValidationError) as exc:
        IDPConfig(classes=[_klass(**{KEY: "recrods"})])
    message = str(exc.value)
    assert "patient_packet" in message
    assert "records" in message


def test_multiple_classes_only_the_bad_one_fails():
    good = _klass(**{KEY: "records"})
    bad = dict(_klass(**{KEY: "nope"}), **{"$id": "other_packet"})
    with pytest.raises(ValidationError) as exc:
        IDPConfig(classes=[good, bad])
    assert "other_packet" in str(exc.value)


# --------------------------------------------------------------------------
# $ref-declared record lists
#
# Found in review of #694: the validator type-checked the raw property node, so a
# record list declared as {"$ref": "#/$defs/RecordList"} — the idiom the UI schema
# editor and several shipped presets use for a reusable record type — was
# rejected outright. The runtime resolver does not care about the schema shape at
# all (it reads the extracted list's length), so this was a false rejection, and
# a HARD config-load failure at that: worse than the silent no-op the validator
# exists to prevent.
# --------------------------------------------------------------------------

_RECORD = {"type": "object", "properties": {"patient_name": {"type": "string"}}}


def test_ref_declared_array_is_accepted():
    cfg = IDPConfig(
        classes=[
            {
                "$id": "patient_packet",
                "type": "object",
                KEY: "records",
                "$defs": {"RecordList": {"type": "array", "items": _RECORD}},
                "properties": {"records": {"$ref": "#/$defs/RecordList"}},
            }
        ]
    )
    assert cfg.classes[0][KEY] == "records"


def test_ref_chain_is_followed():
    cfg = IDPConfig(
        classes=[
            {
                "$id": "patient_packet",
                "type": "object",
                KEY: "records",
                "$defs": {
                    "Outer": {"$ref": "#/$defs/RecordList"},
                    "RecordList": {"type": "array", "items": _RECORD},
                },
                "properties": {"records": {"$ref": "#/$defs/Outer"}},
            }
        ]
    )
    assert cfg.classes[0][KEY] == "records"


def test_ref_to_a_non_array_is_still_rejected():
    """Dereferencing must not weaken the check into a rubber stamp."""
    with pytest.raises(ValidationError) as exc:
        IDPConfig(
            classes=[
                {
                    "$id": "patient_packet",
                    "type": "object",
                    KEY: "records",
                    "$defs": {"NotAList": {"type": "string"}},
                    "properties": {"records": {"$ref": "#/$defs/NotAList"}},
                }
            ]
        )
    assert "it must be an array" in str(exc.value)


def test_unresolvable_ref_falls_back_to_checking_the_node_itself():
    """A dangling $ref cannot be proven to be an array, so it is still rejected."""
    with pytest.raises(ValidationError) as exc:
        IDPConfig(
            classes=[
                {
                    "$id": "patient_packet",
                    "type": "object",
                    KEY: "records",
                    "properties": {"records": {"$ref": "#/$defs/Missing"}},
                }
            ]
        )
    assert "it must be an array" in str(exc.value)


# --------------------------------------------------------------------------
# Synthesize mode: ``x-aws-idp-multi-instance`` (#715)
#
# The transform replaces the class's EFFECTIVE schema with an instances[]
# wrapper. Three config-validate rules, and the third is deliberately only a
# warning — see the invoice/line_items case below.
# --------------------------------------------------------------------------

MULTI = "x-aws-idp-multi-instance"


def _record_class(**overrides):
    """A class describing ONE record — the shape Synthesize mode is for."""
    base = {
        "$id": "Pay-Statement",
        "type": "object",
        "properties": {
            "CheckNumber": {"type": "string"},
            "NetPay": {"type": "number"},
        },
    }
    base.update(overrides)
    return base


def test_multi_instance_alone_is_accepted():
    cfg = IDPConfig(classes=[_record_class(**{MULTI: True})])
    assert cfg.classes[0][MULTI] is True


def test_multi_instance_absent_is_the_untouched_default():
    cfg = IDPConfig(classes=[_record_class()])
    assert MULTI not in cfg.classes[0]


def test_both_modes_on_one_class_is_rejected():
    """They answer opposite questions — Designate names an array the class ALREADY
    has, Synthesize creates one — so setting both is a contradiction, not a
    stronger request."""
    with pytest.raises(ValidationError) as exc:
        IDPConfig(classes=[_klass(**{MULTI: True, KEY: "records"})])
    message = str(exc.value)
    assert "mutually exclusive" in message
    assert MULTI in message and KEY in message


def test_multi_instance_on_a_class_that_already_has_an_instances_property_is_rejected():
    """The wrapper's own key would shadow the user's field and make the original
    unreachable. Rejected rather than renamed: a silent rename would change the
    extraction contract under the user."""
    with pytest.raises(ValidationError) as exc:
        IDPConfig(
            classes=[
                _record_class(
                    **{
                        MULTI: True,
                        "properties": {
                            "instances": {"type": "string"},
                            "CheckNumber": {"type": "string"},
                        },
                    }
                )
            ]
        )
    assert "already declares a top-level property named 'instances'" in str(exc.value)


def test_a_class_with_an_internal_array_is_accepted_not_warned_about():
    """THE case the heuristic must not break. An invoice with line_items[] is a
    single-instance document with an internal list; multi-instance on it is
    perfectly correct and yields instances[i].line_items[j]. Having an internal
    array is NOT evidence of already being a list wrapper."""
    cfg = IDPConfig(
        classes=[
            {
                "$id": "Invoice",
                "type": "object",
                MULTI: True,
                "properties": {
                    "InvoiceNumber": {"type": "string"},
                    "line_items": {
                        "type": "array",
                        "items": {"type": "object", "properties": {}},
                    },
                },
            }
        ]
    )
    assert cfg.classes[0][MULTI] is True


def test_a_class_that_is_nothing_but_one_array_only_warns(caplog):
    """It probably wanted Designate mode, but erroring would be wrong: the author
    may genuinely want instances[i].records[j]. Warn and carry on."""
    import logging

    with caplog.at_level(logging.WARNING):
        cfg = IDPConfig(
            classes=[
                {
                    "$id": "PatientPacket",
                    "type": "object",
                    MULTI: True,
                    "properties": {
                        "records": {"type": "array", "items": {"type": "object"}}
                    },
                }
            ]
        )
    assert cfg.classes[0][MULTI] is True
    assert "already looks like a packet of records" in caplog.text
    assert KEY in caplog.text  # names the suggested alternative


def test_an_already_transformed_wrapper_round_trips_without_error():
    """The transform is applied at runtime and never persisted — but the wrapper
    legitimately carries BOTH keys, so rejecting a schema this code produced
    itself would be a nasty trap for anyone who round-trips one."""
    from idp_common.schema.multi_instance import wrap_class_schema

    wrapped = wrap_class_schema(_record_class(**{MULTI: True}))
    cfg = IDPConfig(classes=[wrapped])
    assert cfg.classes[0]["properties"]["instances"]["type"] == "array"


def test_string_true_from_a_config_round_trip_is_still_honoured():
    """YAML/DynamoDB round-trips can stringify booleans; the rules must fire on
    the stringified form too or they silently stop applying after a save."""
    with pytest.raises(ValidationError):
        IDPConfig(classes=[_klass(**{MULTI: "true", KEY: "records"})])


# ---------------------------------------------------------------------------
# The shipped ocr-benchmark preset declares BANK_CHECK's instance axis.
# ---------------------------------------------------------------------------


def test_ocr_benchmark_presets_declare_bank_check_instance_axis():
    """`BANK_CHECK` is a packet class: its ONLY property is a `checks` array whose
    elements are separate check documents. Without the declaration the section
    reports ``instance_count`` 1 for a sheet holding 8 checks — and running
    detection over this corpus flags 18 of the first 40 sheets for exactly that
    reason (a real finding: the count was never declared; no data was lost).

    Pinned because Designate mode is free — no schema transform, no output change,
    no baseline migration — so there is no reason for this to regress, and if it
    does the only symptom is a number quietly reading 1 again.

    Also asserts the two modes are not BOTH set: `BANK_CHECK`'s top level is
    nothing but one record array, which is precisely the shape
    `x-aws-idp-multi-instance` would double-wrap into ``instances[i].checks[j]``.
    """
    import pathlib

    import yaml

    root = pathlib.Path(__file__).resolve().parents[5]
    for rel in (
        "config_library/managed_config/ocr-benchmark/config.yaml",
        "config_library/unified/ocr-benchmark/config.yaml",
    ):
        path = root / rel
        assert path.exists(), f"missing preset: {rel}"
        classes = yaml.safe_load(path.read_text())["classes"]
        bank_check = next(c for c in classes if (c.get("$id") or "") == "BANK_CHECK")
        assert bank_check.get("x-aws-idp-instance-array") == "checks", rel
        assert "x-aws-idp-multi-instance" not in bank_check, (
            f"{rel}: BANK_CHECK's top level is one record array, so the synthesized "
            "wrapper would produce instances[i].checks[j] — one level too many"
        )
        assert "checks" in (bank_check.get("properties") or {}), rel
