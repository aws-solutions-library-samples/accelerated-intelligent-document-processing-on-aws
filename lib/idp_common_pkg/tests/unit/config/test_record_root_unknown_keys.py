# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A key a configuration *record* drops is reported, whichever record it is.

[#1134](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1134)
solved this for the ``IDPConfig`` document. The Configuration Table holds three
other record types, and ``ModelConfigLimitsConfig`` is not reachable from
``IDPConfig`` at all — there is no ``model_limits`` field on it — so the walk
written for #1134 never saw a per-model limit entry at any depth. The root takes
``extra="forbid"`` and ``ModelLimitEntry`` takes Pydantic's default
``extra="ignore"``, so ``model_limits[0].max_input_tokenz`` was accepted and
discarded while the UI reported the save as successful and the shipped context
window for that model family stayed in force
([#1211](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1211)).

**The universe is derived from production code, not listed here.** The record roots
come from the annotation on ``ConfigurationManager.save_configuration``, which is
where a record of each type is validated from a dict, and the models under each root
come from walking its annotations. A record type added to that signature is covered
by these tests without being added to them, and a hand-written list naming
``ModelLimitEntry`` is how this instance gets fixed and the class does not — which is
the exact way #1134 left #1211 behind.

**The report is attached to each root's own ``mode="before"`` validator, not to a save
path.** These roots are built from a dict in four modules: the configuration resolver
behind the Model Limits and Pricing UI panels, ``update_configuration`` at deploy
time, ``ConfigurationManager.save_configuration`` and the per-record ``save_*``
helpers. The resolver — the operator-facing one — does not go through
``save_configuration``, so a report wired in there would be absent from the path the
defect was reported against. ``test_the_operator_save_path_reports_too`` pins the save
path as well, but the guarantee is the validator.

**The mis-nested case is covered separately from the misspelled one**, because they
fail differently: a misspelled key names nothing, while a mis-nested key names a
*real* field at the wrong depth and so routes its value around the validator that
would have rejected it.
"""

from __future__ import annotations

import logging
import typing
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from idp_common.config import models as models_module
from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.models import (
    IDPConfig,
    ModelConfigLimitsConfig,
    ModelLimitEntry,
    PricingConfig,
    collect_ignored_config_keys,
)

pytestmark = pytest.mark.unit

BOGUS = "zzz_definitely_not_a_field"

LOGGER_NAME = "idp_common.config.models"

#: This checkout, from this file's own location — not from an installed copy of the
#: library, which may point at a different tree entirely.
REPO_ROOT = Path(__file__).resolve().parents[5]


# ---------------------------------------------------------------------------
# The universe: record roots from the save path, models from the annotations
# ---------------------------------------------------------------------------


def _record_roots() -> list[type[BaseModel]]:
    """Every record type ``save_configuration`` can validate from a dict.

    Read off that method's own annotation, because that is the enumeration
    production code keeps: adding a fifth record type means widening this union,
    and every assertion below then covers it. Deriving the set by scanning the
    module for ``BaseModel`` subclasses instead would sweep in the nested element
    models and the DynamoDB wrappers, and would not notice a root being dropped
    from the save path.
    """
    annotation = typing.get_type_hints(ConfigurationManager.save_configuration)[
        "config"
    ]
    return [
        arg
        for arg in typing.get_args(annotation)
        if isinstance(arg, type) and issubclass(arg, BaseModel)
    ]


ROOTS = _record_roots()


class Reached(typing.NamedTuple):
    """One model a record root reaches, and how to put a key inside it."""

    root: type[BaseModel]
    model: type[BaseModel]
    path: str
    # Steps from the root: (field name, shape) where shape is model/list/map.
    steps: tuple[tuple[str, str], ...]

    @property
    def id(self) -> str:
        return f"{self.root.__name__}:{self.path or '<root>'}"


def _target(annotation) -> tuple[str | None, type[BaseModel] | None]:
    """Where a field's value carries a nested model — read here, independently.

    A second implementation on purpose, for the reason
    ``test_unknown_nested_keys.py`` records and measured: a universe derived from
    the production ``_nested_model_target`` moves *with* a mutation of it, so the
    parametrisation shrinks instead of failing and the run stays green.
    """
    while hasattr(annotation, "__metadata__"):  # Annotated[T, ...]
        annotation = typing.get_args(annotation)[0]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "model", annotation
    origin = typing.get_origin(annotation)
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    if origin is typing.Union:
        models = [r for r in (_target(a) for a in args) if r[1] is not None]
        return models[0] if len(models) == 1 else (None, None)
    if origin in (list, set, frozenset, tuple) and len(args) == 1:
        shape, model = _target(args[0])
        return ("list", model) if shape == "model" else (None, None)
    if origin is dict and len(args) == 2:
        shape, model = _target(args[1])
        return ("map", model) if shape == "model" else (None, None)
    return None, None


def _reachable(root: type[BaseModel]) -> list[Reached]:
    """Every model this root reaches, with one route to each."""
    found: list[Reached] = []
    seen: set[type[BaseModel]] = set()

    def visit(model: type[BaseModel], path: str, steps: tuple[tuple[str, str], ...]):
        if model in seen:
            return
        seen.add(model)
        found.append(Reached(root, model, path, steps))
        for name, field in model.model_fields.items():
            shape, nested = _target(field.annotation)
            if nested is None:
                continue
            visit(nested, f"{path}.{name}" if path else name, steps + ((name, shape),))

    visit(root, "", ())
    return found


#: Every (root, model) pair, root itself included. ``IDPConfig`` is a record root too
#: and is deliberately not excluded: the property asserted here is a property of
#: every root, and excluding the one that already had it would leave no test that
#: says so.
REACHED = [reached for root in ROOTS for reached in _reachable(root)]

#: Only what is below a root. A key at depth 0 is a different question — the roots
#: that forbid extras raise for it rather than dropping it, which
#: ``test_a_nested_field_name_written_at_a_records_root_is_rejected_not_ignored``
#: covers over exactly those roots — so the nested report is asked for only where a
#: key is actually dropped.
NESTED = [reached for reached in REACHED if reached.steps]


def _keeps_extras(model: type[BaseModel]) -> bool:
    """Whether this model retains an undeclared key instead of dropping it.

    Deliberately no list: a model is either asserted to report a dropped key or
    asserted to keep it, so none can be excluded from the guarantee by omission.
    """
    return model.model_config.get("extra") == "allow"


def _forbids_extras(model: type[BaseModel]) -> bool:
    return model.model_config.get("extra") == "forbid"


def _nest(steps: tuple[tuple[str, str], ...], leaf: dict) -> dict:
    """Build the smallest record dict that puts ``leaf`` at ``steps``."""
    value: typing.Any = leaf
    for name, shape in reversed(steps):
        if shape == "list":
            value = {name: [value]}
        elif shape == "map":
            value = {name: {"probe": value}}
        else:
            value = {name: value}
    return value


def _expected_path(steps: tuple[tuple[str, str], ...], key: str) -> str:
    parts = []
    for name, shape in steps:
        if shape == "list":
            parts.append(f"{name}[0]")
        elif shape == "map":
            parts.append(f"{name}.probe")
        else:
            parts.append(name)
    return ".".join(parts + [key])


# ---------------------------------------------------------------------------
# Non-vacuity for everything below
# ---------------------------------------------------------------------------


def test_the_record_roots_are_the_ones_the_save_path_accepts():
    """Without this, an empty or truncated universe passes every test below.

    No count is asserted — a figure goes stale and says nothing about coverage —
    but the roots the defect was reported against must be present, and each must be
    a model this module defines rather than something the annotation picked up.
    """
    assert ModelConfigLimitsConfig in ROOTS
    assert PricingConfig in ROOTS
    assert IDPConfig in ROOTS
    for root in ROOTS:
        assert root.__module__ == models_module.__name__, root


def test_there_is_a_root_other_than_idpconfig_with_a_nested_model_that_drops_keys():
    """The property under test has to be non-trivial for a record root.

    If every non-``IDPConfig`` root's tree stopped at its own fields, the
    parametrised tests would reduce to the #1134 coverage and this file would pass
    while asserting nothing new.
    """
    others = [
        reached
        for reached in NESTED
        if reached.root is not IDPConfig and not _keeps_extras(reached.model)
    ]
    assert others, [reached.id for reached in REACHED]
    assert {reached.root for reached in others} >= {
        ModelConfigLimitsConfig,
        PricingConfig,
    }
    assert ModelLimitEntry in {reached.model for reached in others}


def test_the_element_models_take_pydantics_permissive_default():
    """The premise of this whole file, measured rather than assumed.

    The roots forbid extras and the models under them do not, which is the exact
    asymmetry that made the defect silent. If a later change gave an element model
    ``extra="forbid"`` the report would become unreachable for it, and the tests
    below would pass by never producing a finding — so the premise is checked here
    instead of inferred from a green run.
    """
    droppers = [
        reached
        for reached in NESTED
        if not _keeps_extras(reached.model) and not _forbids_extras(reached.model)
    ]
    assert droppers, [
        (reached.id, reached.model.model_config.get("extra")) for reached in NESTED
    ]


# ---------------------------------------------------------------------------
# Every model under every root, not the ones that happened to get a test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reached", NESTED, ids=lambda r: r.id)
def test_the_walk_reports_a_misspelled_key_in_every_model_under_every_root(
    reached: Reached,
):
    """The class, not the instance: one bogus key inside each model, in turn.

    Both halves of the universe in one test, so there is no list of models the
    report is not expected to cover. A model that drops the key must name it with
    its dotted path; a model that keeps it must be *measured* keeping it, because
    "deliberately open" is only true while ``extra="allow"`` is still there.
    """
    data = _nest(reached.steps, {BOGUS: "value"})
    findings = collect_ignored_config_keys(data, reached.root)
    paths = [finding.path for finding in findings]

    if _keeps_extras(reached.model):
        assert paths == [], (
            f"{reached.model.__name__} keeps an undeclared key, so reporting one "
            f"would be a false positive; got {paths}"
        )
        return

    assert _expected_path(reached.steps, BOGUS) in paths, (
        f"{reached.model.__name__} at '{reached.root.__name__}.{reached.path}' drops "
        f"'{BOGUS}' and the walk did not report it; got {paths}"
    )


@pytest.mark.parametrize(
    "reached",
    [reached for reached in NESTED if not _keeps_extras(reached.model)],
    ids=lambda r: r.id,
)
def test_every_root_reports_through_its_own_construction(reached: Reached, caplog):
    """The same property end to end, through the log line an operator actually sees.

    This is the assertion that fails when a record root has no reporting validator,
    so it is what covers a fifth record type added to the save path later.

    Construction may still raise — a probe dict omits required fields, and three of
    these roots forbid extras at depth 0 — and that is fine: the report is a
    ``mode="before"`` validator, so it has already run.

    ⚠️ The instrument is the ``idp_common.config.models`` logger.
    ``warnings.catch_warnings(record=True)`` records nothing from this path and
    returns a false clean.
    """
    data = _nest(reached.steps, {BOGUS: "value"})
    expected = _expected_path(reached.steps, BOGUS)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        try:
            reached.root(**data)
        except ValidationError:
            pass
    assert f"{reached.root.__name__}: Ignoring unknown nested fields" in caplog.text, (
        caplog.text
    )
    assert expected in caplog.text, caplog.text


@pytest.mark.parametrize(
    "root",
    [root for root in ROOTS if not _keeps_extras(root)],
    ids=lambda r: r.__name__,
)
def test_a_correctly_spelled_record_is_reported_clean(root: type[BaseModel]):
    """The other direction: no finding when every key is read.

    A reporter that names something for a well-formed record is worse than none,
    because it teaches an operator to ignore the line.
    """
    for reached in _reachable(root):
        if reached.steps and not _keeps_extras(reached.model):
            data = _nest(reached.steps, {})
            assert collect_ignored_config_keys(data, root) == [], reached.id


# ---------------------------------------------------------------------------
# The mis-nested case, which is not the misspelled case
# ---------------------------------------------------------------------------


def _misnesting_cases() -> list[tuple[Reached, str, str]]:
    """Every (parent, child) pair under a root, paired with a field the child owns.

    Written at the parent's level such a key is a real field at the wrong depth —
    the sharper form of the defect, because the value bypasses the child's
    validators rather than merely going unused.
    """
    cases: list[tuple[Reached, str, str]] = []
    for reached in REACHED:
        if _keeps_extras(reached.model):
            continue
        for name, field in reached.model.model_fields.items():
            _shape, child = _target(field.annotation)
            if child is None or _keeps_extras(child):
                continue
            owned = sorted(set(child.model_fields) - set(reached.model.model_fields))
            if not owned:
                continue
            cases.append((reached, name, owned[0]))
    return cases


MISNESTINGS = _misnesting_cases()

#: A pair whose parent is the root itself is a depth-0 key, and the three roots that
#: forbid extras *raise* for it rather than dropping it — the loud outcome, covered by
#: its own test below. Only the pairs strictly below a root are silent.
NESTED_MISNESTINGS = [case for case in MISNESTINGS if case[0].steps]


def test_there_are_misnesting_cases_below_a_record_root():
    """Non-vacuity, and it is narrow: only ``PricingConfig`` has such a pair today.

    ``ModelConfigLimitsConfig``'s tree is two levels deep, so its only mis-nesting
    is at depth 0 and raises. Asserting a number here would pin the shape of the
    pricing record rather than the property, so what is asserted is that the
    parametrisation is not empty and that it reaches a model under a root other than
    ``IDPConfig``.
    """
    assert NESTED_MISNESTINGS, MISNESTINGS
    assert any(case[0].root is not IDPConfig for case in NESTED_MISNESTINGS), [
        case[0].id for case in NESTED_MISNESTINGS
    ]


@pytest.mark.parametrize(
    "reached,child_field,key",
    NESTED_MISNESTINGS,
    ids=lambda v: v.id if isinstance(v, Reached) else str(v),
)
def test_a_real_field_written_one_level_up_is_reported_under_every_root(
    reached: Reached, child_field: str, key: str
):
    """A real field at the wrong depth is named, and named where it was written.

    The path in the finding has to be the path the author typed, not the one the
    field belongs at — that is the line that tells them which of the two to edit.
    """
    data = _nest(reached.steps, {key: "probe-value"})
    written = _expected_path(reached.steps, key)
    findings = collect_ignored_config_keys(data, reached.root)
    assert written in [finding.path for finding in findings], (
        f"'{key}' is declared on the model behind "
        f"'{reached.root.__name__}.{reached.path}.{child_field}' and not on "
        f"'{reached.path}' itself, so writing it at '{written}' drops it — and the "
        f"walk reported {[f.path for f in findings]}"
    )


@pytest.mark.parametrize(
    "reached,child_field,key",
    NESTED_MISNESTINGS,
    ids=lambda v: v.id if isinstance(v, Reached) else str(v),
)
def test_a_misnested_key_reaches_the_log_and_does_not_reach_the_model(
    reached: Reached, child_field: str, key: str, caplog
):
    """The measurement trap this defect sets, asserted in both halves.

    Construction *succeeds* for a mis-nested key, so "it validated" is not evidence
    the value arrived. The value must be absent from the model and the path must be
    present in the log.
    """
    data = _nest(reached.steps, {key: "probe-value"})
    written = _expected_path(reached.steps, key)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        try:
            record = reached.root(**data)
        except ValidationError:
            record = None
    assert written in caplog.text, caplog.text
    if record is not None:
        assert "probe-value" not in str(record.model_dump()), (
            f"'{written}' was reported as dropped and the value is still in the "
            "record, so the report is wrong"
        )


@pytest.mark.parametrize(
    "root", [root for root in ROOTS if _forbids_extras(root)], ids=lambda r: r.__name__
)
def test_a_nested_field_name_written_at_a_records_root_is_rejected_not_ignored(
    root: type[BaseModel],
):
    """Why the nested report deliberately leaves depth 0 alone on these roots.

    ``extra="forbid"`` at the root means a key there is an error, not a silent
    drop, so a warning saying it was ignored would be false. This is the one place
    the mis-nested case is already loud, and it is asserted rather than assumed
    because the reporting call would have to change if it stopped being true.
    """
    nested_names: set[str] = set()
    for reached in _reachable(root):
        if reached.steps:
            nested_names |= set(reached.model.model_fields) - set(root.model_fields)
    assert nested_names, root.__name__
    key = sorted(nested_names)[0]
    with pytest.raises(ValidationError) as caught:
        root(**{key: "probe-value"})
    assert key in str(caught.value)


# ---------------------------------------------------------------------------
# The witnesses from the issue, and the paths an operator's edit really takes
# ---------------------------------------------------------------------------


def test_the_issues_own_witness_names_the_key_and_the_field_it_resembles(caplog):
    """The reported reproduction, end to end, including what it does *not* do.

    ``max_input_tokens`` is the context window the Bedrock client resolves a model
    against, so a dropped one leaves the shipped default in force while the save
    looks successful. The record still validates — asserted here, because the fix
    reports and does not reject, and a change to rejection would be a
    behaviour change for stored records.
    """
    data = {
        "model_limits": [
            {"pattern": "x", "max_output_tokens": 100, "max_input_tokenz": 5}
        ]
    }
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        record = ModelConfigLimitsConfig(**data)
    assert record.model_limits[0].max_input_tokens is None
    assert (
        "model_limits[0].max_input_tokenz (did you mean "
        "model_limits[0].max_input_tokens?)" in caplog.text
    ), caplog.text


def test_a_price_written_on_the_entry_instead_of_the_unit_is_reported(caplog):
    """The pricing record's own mis-nesting, which is a plausible hand edit.

    ``price`` belongs to a unit; written on the entry it is dropped, so the entry
    carries the shipped price and the operator's number is nowhere.
    """
    data = {
        "pricing": [
            {
                "name": "bedrock/some.model",
                "price": "0.5",
                "units": [{"name": "inputTokens", "price": "1"}],
            }
        ]
    }
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        record = PricingConfig(**data)
    assert "pricing[0].price" in caplog.text, caplog.text
    assert "0.5" not in str(record.model_dump())


def test_the_operator_save_path_reports_too(caplog):
    """``save_configuration`` is one of four places a record is built from a dict.

    The guarantee is the validator on each root, which is why this needs no
    parametrisation over the other three call sites — but the save path is the one
    an operator's edit goes through, so it is measured rather than reasoned about.
    """
    mock_table = Mock()
    mock_table.get_item.return_value = {}
    data = {
        "model_limits": [
            {"pattern": "x", "max_output_tokens": 100, "max_input_tokenz": 5}
        ]
    }
    with patch("idp_common.config.configuration_manager.boto3.resource") as mock_boto3:
        mock_boto3.return_value.Table.return_value = mock_table
        manager = ConfigurationManager(table_name="test-table")
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            manager.save_configuration("CustomModelConfigLimits", data)
    mock_table.put_item.assert_called_once()
    assert "model_limits[0].max_input_tokenz" in caplog.text, caplog.text


@pytest.mark.parametrize(
    "relative,root",
    [
        ("config_library/model_config_limits.yaml", ModelConfigLimitsConfig),
        ("config_library/pricing.yaml", PricingConfig),
    ],
)
def test_the_records_this_repository_ships_are_clean(
    relative: str, root: type[BaseModel]
):
    """A seeded deployment must produce no warning at all.

    These two files are loaded into the Configuration Table by
    ``update_configuration`` at deploy time, through the same validator. A finding
    in one of them would put the line in front of every operator on every stack
    update, which is how a real warning stops being read.
    """
    path = REPO_ROOT / relative
    assert path.is_file(), path
    data = yaml.safe_load(path.read_text())
    findings = collect_ignored_config_keys(data, root, include_top_level=True)
    assert findings == [], [finding.describe() for finding in findings]
