# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A key the configuration models drop is reported, at every depth and in every model.

Every model in this tree takes Pydantic's default ``extra="ignore"`` (three
deliberately take ``extra="allow"``; none takes ``extra="forbid"``), so a key no
field matches is discarded during validation. ``IDPConfig`` warned about that at the
top level only, which is the level where a typo is least likely: below it, a
misspelled key left the shipped default in force with no diagnostic anywhere, and a
default is indistinguishable from a working setting
([#1134](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1134)).

**The universe is derived from the models, not listed here.** Every assertion below
walks the annotations out from ``IDPConfig`` and asserts its property over every
model it reaches — 40 today, counted rather than quoted — so a model added later is
covered by these tests without being added to them. A hand-written list of three
models is how the instance gets fixed and the class does not.

**The mis-nested case is covered separately from the misspelled one**, because they
fail differently. A misspelled key names nothing; a mis-nested key names a *real*
field at the wrong depth, so it is routed around the validator that would have
rejected its value — ``ocr.dpi: "abc"`` is accepted in silence while
``ocr.image.dpi: "abc"`` raises. It is also how the defect corrupts measurement: a
probe that writes a key one level up gets "accepted" and reads it as "valid".
"""

from __future__ import annotations

import copy
import logging
import typing
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from idp_common.config import models as models_module
from idp_common.config.migrations import migrate_config
from idp_common.config.models import (
    DEPRECATED_CONFIG_FIELDS_BY_MODEL,
    IDP_CONFIG_DEPRECATED_FIELDS,
    MAX_REPORTED_IGNORED_KEYS,
    SUPPRESSED_IGNORED_KEY_PATHS,
    IDPConfig,
    ImageConfig,
    OCRConfig,
    collect_ignored_config_keys,
)

pytestmark = pytest.mark.unit

BOGUS = "zzz_definitely_not_a_field"

#: This checkout, from this file's own location — not from an installed copy of the
#: library, which may point at a different tree entirely.
REPO_ROOT = Path(__file__).resolve().parents[5]


# ---------------------------------------------------------------------------
# The universe, derived from the model annotations
# ---------------------------------------------------------------------------


class Reached(typing.NamedTuple):
    """One model the config tree reaches, and how to put a key inside it."""

    model: type[BaseModel]
    path: str
    # Steps from the root: (field name, shape) where shape is model/list/map.
    steps: tuple[tuple[str, str], ...]


def _reachable_models() -> list[Reached]:
    """Walk ``IDPConfig``'s annotations and record every model with one route to it.

    Deliberately the same traversal the production walker uses, via the same
    ``_nested_model_target`` — a second implementation here would be a second thing
    to keep in step, and the property under test is about the tree the walker sees.
    """
    found: list[Reached] = []
    seen: set[type[BaseModel]] = set()

    def visit(model: type[BaseModel], path: str, steps: tuple[tuple[str, str], ...]):
        if model in seen:
            return
        seen.add(model)
        found.append(Reached(model, path, steps))
        for name, field in model.model_fields.items():
            shape, nested = models_module._nested_model_target(field.annotation)
            if nested is None:
                continue
            visit(nested, f"{path}.{name}" if path else name, steps + ((name, shape),))

    visit(IDPConfig, "", ())
    return found


REACHED = _reachable_models()
#: Models that keep an undeclared key instead of dropping it, so there is nothing to
#: report about one. Derived from ``model_config``, and the premise is measured in
#: ``test_an_extra_allow_model_really_keeps_the_key_it_is_not_warned_about``.
OPEN_MODELS = [r for r in REACHED if r.model.model_config.get("extra") == "allow"]
CLOSED_MODELS = [r for r in REACHED if r.model.model_config.get("extra") != "allow"]


def _nest(steps: tuple[tuple[str, str], ...], leaf: dict) -> dict:
    """Build the smallest config dict that puts ``leaf`` at ``steps``."""
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


def test_the_walk_reaches_the_whole_config_tree():
    """Non-vacuity for every parametrised test below.

    No figure is asserted — one goes stale and says nothing about coverage — but an
    empty or truncated walk would make every assertion here pass trivially, so the
    shape of the tree is checked instead: it is deep, it is wide, and it contains the
    models the issue's own witnesses live in.
    """
    assert len(REACHED) > 30, [r.path for r in REACHED]
    assert max(len(r.steps) for r in REACHED) >= 3, "no three-deep path was reached"
    reached = {r.model for r in REACHED}
    assert {IDPConfig, OCRConfig, ImageConfig} <= reached
    assert models_module.ValidationConfig in reached
    assert models_module.ErrorAnalyzerParameters in reached
    assert OPEN_MODELS, "no extra='allow' model was reached; that premise is untested"


# ---------------------------------------------------------------------------
# Every model, not the ones that happened to get a test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reached", CLOSED_MODELS, ids=lambda r: r.path or "IDPConfig")
def test_every_model_in_the_tree_reports_a_key_it_will_drop(reached: Reached):
    """The class, not the instance: one bogus key inside each model, in turn."""
    data = _nest(reached.steps, {BOGUS: "value"})
    findings = collect_ignored_config_keys(data, IDPConfig, include_top_level=True)
    paths = [f.path for f in findings]
    assert _expected_path(reached.steps, BOGUS) in paths, (
        f"{reached.model.__name__} at '{reached.path}' drops '{BOGUS}' and the walk "
        f"did not report it; got {paths}"
    )


@pytest.mark.parametrize("reached", CLOSED_MODELS, ids=lambda r: r.path or "IDPConfig")
def test_every_model_in_the_tree_reports_through_idpconfig_construction(
    reached: Reached, caplog
):
    """The same property end to end, through the log line an operator actually sees.

    Construction may still raise — a probe dict omits required fields — and that is
    fine: the report is a ``mode="before"`` validator, so it has already run. What
    would not be fine is the warning going missing.
    """
    data = _nest(reached.steps, {BOGUS: "value"})
    expected = _expected_path(reached.steps, BOGUS)
    with caplog.at_level(logging.WARNING, logger="idp_common.config.models"):
        try:
            IDPConfig(**data)
        except ValidationError:
            pass
    if not reached.steps:  # top level keeps its own long-standing message
        assert BOGUS in caplog.text
        return
    assert "unknown nested fields" in caplog.text, caplog.text
    assert expected in caplog.text, caplog.text


# ---------------------------------------------------------------------------
# The mis-nested case, which is not the misspelled case
# ---------------------------------------------------------------------------


def _misnesting_cases() -> list[tuple[str, tuple[tuple[str, str], ...], str, str]]:
    """Every (parent, child) pair in the tree, with one field name the child owns.

    Written at the parent's level, such a key is a real field at the wrong depth —
    the sharper form of the defect, because the value bypasses the child's
    validators rather than merely being unused.
    """
    cases = []
    for reached in CLOSED_MODELS:
        for name, field in reached.model.model_fields.items():
            shape, child = models_module._nested_model_target(field.annotation)
            if child is None or child.model_config.get("extra") == "allow":
                continue
            owned = sorted(set(child.model_fields) - set(reached.model.model_fields))
            if not owned:
                continue
            cases.append((reached.path, reached.steps, name, owned[0]))
    return cases


MISNESTINGS = _misnesting_cases()


def test_there_are_misnesting_cases_to_check():
    assert len(MISNESTINGS) > 20, MISNESTINGS


@pytest.mark.parametrize(
    "parent_path,steps,child_field,key",
    MISNESTINGS,
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_a_real_field_written_one_level_up_is_reported_with_the_path_it_belongs_at(
    parent_path: str, steps: tuple[tuple[str, str], ...], child_field: str, key: str
):
    data = _nest(steps, {key: "probe-value"})
    findings = collect_ignored_config_keys(data, IDPConfig, include_top_level=True)
    written = _expected_path(steps, key)
    matching = [f for f in findings if f.path == written]
    assert matching, (
        f"'{key}' is declared on the model behind '{parent_path}.{child_field}' and "
        f"not on '{parent_path}' itself, so writing it at '{written}' drops it — and "
        f"the walk reported {[f.path for f in findings]}"
    )
    finding = matching[0]
    assert finding.suggestion, (
        f"'{written}' names a real field elsewhere in the tree, so a message naming "
        "only the key would read as a false positive; no suggestion was offered"
    )
    assert finding.suggestion.split(".")[-1] == key
    assert finding.suggestion != written


def test_the_issues_own_witness_names_the_exact_path_it_belongs_at():
    """``ocr.dpi`` is the measurement that #1134's second witness was taken from.

    ``dpi`` is a real field of ``ImageConfig``, reached as ``ocr.image.dpi``, and
    ``ImageConfig`` is reachable four ways — so the suggestion has to pick the one
    nearest what was written rather than an arbitrary one.
    """
    findings = collect_ignored_config_keys({"ocr": {"dpi": "abc"}}, IDPConfig)
    assert [(f.path, f.kind, f.suggestion) for f in findings] == [
        ("ocr.dpi", "unknown", "ocr.image.dpi")
    ]
    # And the value really is routed around the validator that would reject it.
    assert IDPConfig(**{"ocr": {"dpi": "abc"}}).ocr.image.dpi is None
    with pytest.raises(ValidationError):
        ImageConfig(dpi="abc")


def test_a_misspelled_leaf_is_reported_with_the_field_it_resembles():
    findings = collect_ignored_config_keys(
        {"extraction": {"validation": {"enabld": False}}}, IDPConfig
    )
    assert [(f.path, f.suggestion) for f in findings] == [
        ("extraction.validation.enabld", "extraction.validation.enabled")
    ]
    # The guard the author was trying to turn off is still on, which is the reason
    # the silence mattered.
    assert (
        IDPConfig(
            **{"extraction": {"validation": {"enabld": False}}}
        ).extraction.validation.enabled
        is True
    )


def test_a_misspelled_block_name_is_reported():
    findings = collect_ignored_config_keys(
        {"extraction": {"validaton": {"fail_action": "escalate"}}}, IDPConfig
    )
    assert [(f.path, f.suggestion) for f in findings] == [
        ("extraction.validaton", "extraction.validation")
    ]


def test_a_parameter_read_by_name_reports_a_typo_in_its_name():
    """#1134's third witness: ``get_ea_param(name, default)`` cannot fail.

    An undeclared name resolves to the literal default, so the cap looks
    configurable and every value written for it is discarded.
    """
    findings = collect_ignored_config_keys(
        {
            "agents": {
                "error_analyzer": {"parameters": {"max_stepfunction_history_pagez": 3}}
            }
        },
        IDPConfig,
    )
    assert [f.path for f in findings] == [
        "agents.error_analyzer.parameters.max_stepfunction_history_pagez"
    ]
    assert findings[0].suggestion == (
        "agents.error_analyzer.parameters.max_stepfunction_history_pages"
    )


def test_a_key_inside_a_list_element_is_reported_with_its_index():
    findings = collect_ignored_config_keys(
        {"ocr": {"features": [{"name": "TABLES"}, {"nam": "FORMS"}]}}, IDPConfig
    )
    assert [f.path for f in findings] == ["ocr.features[1].nam"]


# ---------------------------------------------------------------------------
# What is NOT unknown: open subtrees, per subtree
# ---------------------------------------------------------------------------


def _free_form_fields() -> list[tuple[str, tuple[tuple[str, str], ...], str, object]]:
    """Fields that hold a document rather than a set of declared settings.

    Derived: a field whose annotation names no model is one the walk cannot descend
    into, so its keys are the author's to choose. Enumerated here to assert the
    premise per field — that the value is *kept* — rather than to list exclusions.
    """
    out = []
    for reached in REACHED:
        for name, field in reached.model.model_fields.items():
            _shape, nested = models_module._nested_model_target(field.annotation)
            if nested is not None:
                continue
            kind = _free_form_kind(field.annotation)
            if kind is None:
                continue
            out.append(
                (
                    reached.path,
                    reached.steps,
                    name,
                    [{BOGUS: "kept"}] if kind == "list" else {BOGUS: "kept"},
                )
            )
    return out


def _free_form_kind(annotation) -> str | None:
    """``"map"``, ``"list"`` or ``None``: does this annotation hold a free-form document?

    Read structurally rather than from ``str(annotation)``, which spells the same
    type three ways depending on how it was written and is why a substring match on
    it silently found nothing.
    """
    annotation = models_module._strip_annotated(annotation)
    origin = typing.get_origin(annotation)
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    if origin is typing.Union:
        kinds = {_free_form_kind(a) for a in args}
        kinds.discard(None)
        return kinds.pop() if len(kinds) == 1 else None
    if origin is dict:
        return "map" if len(args) == 2 and args[1] is typing.Any else None
    if origin is list:
        return "list" if len(args) == 1 and _free_form_kind(args[0]) == "map" else None
    return None


FREE_FORM = _free_form_fields()


def _owner_of(steps: tuple[tuple[str, str], ...]) -> dict:
    """Required-field placeholders for the model ``steps`` lands in."""
    model: type[BaseModel] = IDPConfig
    for name, _shape in steps:
        _shape2, nested = models_module._nested_model_target(
            model.model_fields[name].annotation
        )
        assert nested is not None
        model = nested
    return _required_placeholders(model)


def _descend(instance, steps: tuple[tuple[str, str], ...]):
    """Follow ``steps`` into a validated model, through lists and maps."""
    for name, shape in steps:
        instance = getattr(instance, name)
        if shape == "list":
            instance = instance[0]
        elif shape == "map":
            instance = instance["probe"]
    return instance


def test_there_are_free_form_subtrees_to_check():
    assert len(FREE_FORM) >= 5, FREE_FORM


@pytest.mark.parametrize(
    "owner_path,steps,field_name,leaf",
    FREE_FORM,
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_a_free_form_subtree_is_neither_reported_nor_dropped(
    owner_path: str, steps, field_name: str, leaf
):
    """One reason per subtree, computed: the key survives, so it is data, not a setting.

    This is the half that cannot be argued in a comment. "Deliberately open" is only
    true of a field that actually keeps what is written there; if one of these
    silently dropped the key, excluding it from the report would be hiding exactly
    the defect this change exists to surface.
    """
    owner = {name: value for name, value in _owner_of(steps).items()}
    owner[field_name] = leaf
    data = _nest(steps, owner)
    assert collect_ignored_config_keys(data, IDPConfig, include_top_level=True) == [], (
        f"'{owner_path}.{field_name}' holds free-form content and must not be walked"
    )

    kept = getattr(_descend(IDPConfig(**data), steps), field_name)
    flattened = kept[0] if isinstance(kept, list) else kept
    assert BOGUS in flattened, (
        f"'{owner_path}.{field_name}' is excluded from the report on the premise that "
        "it keeps what is written there, and it did not keep it"
    )


def _required_placeholders(model: type[BaseModel]) -> dict:
    """Values for a model's required fields, so it can be constructed at all.

    ``PipelineHook.arn`` is required, and a probe that omits it measures nothing
    about extras because validation stops first.
    """
    samples = {str: "placeholder", int: 1, float: 1.0, bool: True}
    out = {}
    for name, field in model.model_fields.items():
        if not field.is_required():
            continue
        annotation = models_module._strip_annotated(field.annotation)
        out[name] = samples.get(annotation, "placeholder")
    return out


@pytest.mark.parametrize("reached", OPEN_MODELS, ids=lambda r: r.path)
def test_an_extra_allow_model_really_keeps_the_key_it_is_not_warned_about(
    reached: Reached,
):
    """``extra="allow"`` is the other reason a key is not a finding, also computed.

    Nothing is dropped, so there is nothing to warn about — but that is a property of
    ``model_config``, and a model that stopped allowing extras while staying on this
    list would be silently dropping keys again.
    """
    leaf = {BOGUS: "kept"}
    leaf.update(_required_placeholders(reached.model))
    data = _nest(reached.steps, leaf)
    assert collect_ignored_config_keys(data, IDPConfig, include_top_level=True) == []

    stored = _descend(IDPConfig(**data), reached.steps)
    assert BOGUS in stored.model_dump(), (
        f"{reached.model.__name__} is exempt because extra='allow' keeps the key, and "
        "it did not"
    )


# ---------------------------------------------------------------------------
# The suppression, and its ratchets
# ---------------------------------------------------------------------------


def test_the_suppression_map_has_not_grown():
    """Count pin. One entry is a decision; a second arrives as a failure asking why.

    The suppression exists for a key this repository ships in its own defaults, so
    the operator did not write it and cannot act on the warning. That reason does not
    generalise, and a suppression list is the cheapest possible way to make this
    whole report say nothing.
    """
    assert set(SUPPRESSED_IGNORED_KEY_PATHS) == {"discovery.output_format"}


@pytest.mark.parametrize("dotted", sorted(SUPPRESSED_IGNORED_KEY_PATHS))
def test_a_suppressed_path_is_one_the_models_really_drop(dotted, monkeypatch):
    """Non-vacuity, per entry. A suppression that shields nothing is worse than none:
    it pre-suppresses whatever next occupies that path."""
    steps = tuple((part, "model") for part in dotted.split(".")[:-1])
    leaf = dotted.split(".")[-1]
    data = _nest(steps, {leaf: "probe"})

    monkeypatch.setattr(models_module, "SUPPRESSED_IGNORED_KEY_PATHS", {})
    assert [f.path for f in collect_ignored_config_keys(data, IDPConfig)] == [dotted], (
        f"'{dotted}' is suppressed but the models no longer drop it — delist it"
    )

    monkeypatch.undo()
    assert collect_ignored_config_keys(data, IDPConfig) == []


@pytest.mark.parametrize("dotted", sorted(SUPPRESSED_IGNORED_KEY_PATHS))
def test_a_suppressed_path_is_one_this_repository_ships(dotted):
    """Closure, per entry: the justification *is* that we ship it.

    A path nobody ships is an operator's own typo, and staying quiet about that is
    the defect. When the shipped copy goes, so does the entry.
    """
    leaf = dotted.split(".")[-1]
    shipped = [
        path
        for path in (REPO_ROOT / "lib/idp_common_pkg/idp_common/config").glob(
            "system_defaults/*.yaml"
        )
        if f"{leaf}:" in path.read_text(encoding="utf-8")
    ]
    assert shipped, (
        f"'{dotted}' is suppressed because this repository ships it in its own system "
        "defaults, and no system-defaults file mentions it any more"
    )


# ---------------------------------------------------------------------------
# Deprecated, which is a wording decision rather than a suppression
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    sorted(
        (m for m in DEPRECATED_CONFIG_FIELDS_BY_MODEL if m is not IDPConfig),
        key=lambda m: m.__name__,
    ),
)
def test_a_deprecated_entry_names_a_model_in_the_tree_and_keys_it_does_not_declare(
    model,
):
    """Staleness, per entry: the model is reachable and the key really is gone.

    ``IDPConfig`` itself is checked in ``test_the_top_level_deprecated_set...``
    below, because its set predates this walk and the measurement there is
    different.
    """
    assert model in {r.model for r in REACHED}, (
        f"{model.__name__} is registered as having deprecated keys but is not "
        "reachable from IDPConfig"
    )
    for key in DEPRECATED_CONFIG_FIELDS_BY_MODEL[model]:
        assert key not in model.model_fields, (
            f"{model.__name__}.{key} is a declared field, so it is read, not "
            "deprecated — delete the registry entry"
        )


def test_the_top_level_deprecated_set_is_registered_once():
    """One authority for "deprecated, not unknown" at every depth."""
    assert DEPRECATED_CONFIG_FIELDS_BY_MODEL[IDPConfig] == frozenset(
        IDP_CONFIG_DEPRECATED_FIELDS
    )


def test_exactly_one_top_level_deprecated_name_is_also_a_declared_field():
    """A pinned measurement of a set this change did not author.

    ``summary`` is both declared on ``IDPConfig`` (rule validation reads it) and
    listed as deprecated, so its listing can never fire — a declared name is never
    an extra key. That is harmless and long-standing; what is worth a ratchet is a
    *second* one appearing, which would mean a live field had been written off as
    deprecated.
    """
    declared = {k for k in IDP_CONFIG_DEPRECATED_FIELDS if k in IDPConfig.model_fields}
    assert declared == {"summary"}, declared


def test_a_deprecated_nested_key_is_reported_as_deprecated_not_unknown():
    findings = collect_ignored_config_keys(
        {"extraction": {"max_tokens": 4096}}, IDPConfig
    )
    assert [(f.path, f.kind, f.suggestion) for f in findings] == [
        ("extraction.max_tokens", "deprecated", None)
    ]


# ---------------------------------------------------------------------------
# Noise: this repository's own configuration must be clean
# ---------------------------------------------------------------------------


def test_the_default_configuration_of_a_stock_deployment_is_clean():
    """The report has to be silent on a stock deployment or it trains people to ignore it.

    A warning about a key the operator did not write is one they cannot act on, and
    the whole value of the line is that it appears on the day they mistype something.

    Asked through the real loader rather than by globbing YAML: the merge is what a
    deployment performs, it resolves the ``_inherits`` chain, and it needs no second
    copy of the "which files here are configuration documents" question that
    ``scripts/tests/test_preset_keys_are_read.py`` already answers for the presets.
    Every system-defaults file except ``pattern-1.yaml`` is reached this way —
    ``pattern-1.yaml`` inherits a file that no longer exists, which is
    [#1203](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1203)
    and not this walk's finding to make.
    """
    from idp_common.config.merge_utils import merge_config_with_defaults

    merged = merge_config_with_defaults({}, "pattern-2", validate=False)
    findings = collect_ignored_config_keys(merged, IDPConfig, include_top_level=True)
    assert findings == [], [f.describe() for f in findings]


# ---------------------------------------------------------------------------
# Bounds and shape of the report itself
# ---------------------------------------------------------------------------


def test_the_log_line_is_bounded():
    """A config written against a different product should not produce a page of log."""
    data = {
        "extraction": {f"bogus_{i}": i for i in range(MAX_REPORTED_IGNORED_KEYS * 2)}
    }
    findings = collect_ignored_config_keys(data, IDPConfig)
    rendered = models_module.format_ignored_config_keys(findings, "unknown")
    assert rendered.count(",") == MAX_REPORTED_IGNORED_KEYS
    assert rendered.endswith(f"and {MAX_REPORTED_IGNORED_KEYS} more")


def test_top_level_keys_are_left_to_the_existing_message_by_default():
    """Two warnings about one key is worse than one, so depth 0 is opt-in."""
    data = {"totally_bogus": {"x": 1}}
    assert collect_ignored_config_keys(data, IDPConfig) == []
    assert [
        f.path
        for f in collect_ignored_config_keys(data, IDPConfig, include_top_level=True)
    ] == ["totally_bogus"]


def test_a_legacy_shaped_key_is_not_reported_after_migration():
    """A migrated key is relocated, not dropped, so reporting it would be a false alarm."""
    legacy = {"extraction": {"agentic": {"validation": {"enabled": True}}}}
    assert (
        collect_ignored_config_keys(migrate_config(copy.deepcopy(legacy)), IDPConfig)
        == []
    )


def test_validate_config_reports_a_nested_typo_to_the_author():
    """The CLI path, where the author is still present and the fix is cheap."""
    from idp_common.config.merge_utils import validate_config

    result = validate_config(
        {
            "classes": [{"name": "invoice"}],
            "extraction": {"validation": {"enabld": False}},
            "ocr": {"dpi": 300},
        },
        "pattern-2",
    )
    assert result["valid"] is True, result["errors"]
    joined = "\n".join(result["warnings"])
    assert "extraction.validation.enabld" in joined
    assert "Did you mean 'extraction.validation.enabled'?" in joined
    assert "ocr.dpi" in joined
    assert "Did you mean 'ocr.image.dpi'?" in joined


def test_validate_config_stays_quiet_about_a_correct_configuration():
    from idp_common.config.merge_utils import validate_config

    result = validate_config(
        {
            "classes": [{"name": "invoice"}],
            "extraction": {"validation": {"enabled": False}},
            "ocr": {"image": {"dpi": 300}},
        },
        "pattern-2",
    )
    assert [w for w in result["warnings"] if "configuration key" in w] == []
