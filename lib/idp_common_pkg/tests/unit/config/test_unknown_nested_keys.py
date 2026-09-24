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


def _target(annotation) -> tuple[str | None, type[BaseModel] | None]:
    """Where a field's value carries a nested model — read here, independently.

    ⚠️ **A second implementation on purpose, and the reason is measured.** Deriving
    the universe below from the production ``_nested_model_target`` made these tests
    move *with* a mutation of it: changing that function to treat every
    ``Dict[str, Any]`` as a model subtree emptied the free-form parametrisation
    instead of failing it, and the run stayed green. A test whose expectations are
    computed by the code under test cannot see a change in that code.

    ``test_the_production_traversal_agrees_with_an_independent_walk`` compares the two
    field by field, so the duplication is pinned rather than left to drift.
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


def _reachable_models() -> list[Reached]:
    """Every model the config tree reaches, with one route to each."""
    found: list[Reached] = []
    seen: set[type[BaseModel]] = set()

    def visit(model: type[BaseModel], path: str, steps: tuple[tuple[str, str], ...]):
        if model in seen:
            return
        seen.add(model)
        found.append(Reached(model, path, steps))
        for name, field in model.model_fields.items():
            shape, nested = _target(field.annotation)
            if nested is None:
                continue
            visit(nested, f"{path}.{name}" if path else name, steps + ((name, shape),))

    visit(IDPConfig, "", ())
    return found


REACHED = _reachable_models()


def _keeps_extras(model: type[BaseModel]) -> bool:
    """Whether this model retains an undeclared key instead of dropping it.

    There is deliberately **no list** of the models that do. A model here is either
    asserted to report a dropped key or asserted to keep it — the two tests below
    cover the same universe with no member in neither, so a model cannot be excluded
    from the report's guarantee by being left off a list.
    """
    return model.model_config.get("extra") == "allow"


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
    assert any(_keeps_extras(r.model) for r in REACHED), (
        "no extra='allow' model was reached, so the other half of the universe below "
        "is untested"
    )


class _ProbeA(BaseModel):
    knob: str = ""


class _ProbeB(BaseModel):
    other: str = ""


#: The resolver's contract, on annotations built here rather than on the ones the
#: tree happens to contain. Three of the shapes below — a mapping of models, a
#: ``tuple``/``set`` of models, and a union of two models — match no field today, so
#: the tree cannot exercise them and a change to any of those branches is otherwise
#: invisible. The union entry is the one that matters: answering it would mean
#: picking a member arbitrarily, and the walk's ``(None, None)`` there is the gap
#: `test_no_field_in_the_tree_holds_a_model_the_walk_declines_to_enter` guards.
RESOLVER_CONTRACT = [
    (_ProbeA, ("model", _ProbeA)),
    (typing.Optional[_ProbeA], ("model", _ProbeA)),
    (typing.List[_ProbeA], ("list", _ProbeA)),
    (typing.Optional[typing.List[_ProbeA]], ("list", _ProbeA)),
    (typing.Tuple[_ProbeA], ("list", _ProbeA)),
    (typing.Set[_ProbeA], ("list", _ProbeA)),
    (typing.Dict[str, _ProbeA], ("map", _ProbeA)),
    (typing.Annotated[_ProbeA, "meta"], ("model", _ProbeA)),
    (typing.Union[_ProbeA, _ProbeB], (None, None)),
    (typing.Dict[str, typing.Any], (None, None)),
    (typing.List[typing.Dict[str, typing.Any]], (None, None)),
    (str, (None, None)),
    (typing.Optional[int], (None, None)),
]


@pytest.mark.parametrize("annotation,expected", RESOLVER_CONTRACT, ids=lambda v: str(v))
def test_the_resolver_answers_each_annotation_shape_as_documented(annotation, expected):
    assert models_module._nested_model_target(annotation) == expected
    assert _target(annotation) == expected


class _MapLeaf(BaseModel):
    knob: str = ""


class _MapMid(BaseModel):
    leaf: _MapLeaf = _MapLeaf()


class _MapRoot(BaseModel):
    """A root with a ``Dict[str, Model]`` field, which the real tree does not have.

    The walk supports that shape and the suggestion index deliberately does not, and
    no configuration in this repository can tell the two decisions apart — removing
    the index's exclusion left every test green. So the discriminating input is built
    here instead: the walk takes any root model, so a synthetic one is a real
    measurement rather than a stand-in.
    """

    buckets: typing.Dict[str, _MapMid] = {}


def test_a_mapping_subtree_is_walked_but_never_suggested_into():
    findings = collect_ignored_config_keys(
        {"buckets": {"b1": {"knob": "x"}}}, _MapRoot, include_top_level=True
    )
    # Walked: the finding names the key the author actually used, so the path is one
    # they can go and edit.
    assert [f.path for f in findings] == ["buckets.b1.knob"]
    # Not suggested into: `knob` is a real field one level deeper, at
    # `buckets.<name>.leaf.knob`, and there is no spelling of that a reader could
    # paste — every candidate either invents a bucket name or breaks the dotted
    # round-trip. Absent beats invented.
    assert findings[0].suggestion is None, findings[0].suggestion
    assert all(
        "*" not in segment
        for path in models_module._field_path_index(_MapRoot).values()
        for segments in path
        for segment in segments
    )


def test_the_production_traversal_agrees_with_an_independent_walk():
    """The duplicated resolver above is pinned against the one the report uses.

    Every field in the tree, both answers, compared — so the second implementation
    cannot drift into describing a different tree than the walk descends, and a change
    to either that moves where it descends fails here by name instead of quietly
    resizing the parametrisations below.
    """
    disagreements = []
    for reached in REACHED:
        for name, field in reached.model.model_fields.items():
            mine = _target(field.annotation)
            theirs = models_module._nested_model_target(field.annotation)
            if mine != theirs:
                disagreements.append(
                    f"{reached.model.__name__}.{name}: this file says {mine}, "
                    f"models._nested_model_target says {theirs}"
                )
    assert not disagreements, "\n".join(disagreements)


# ---------------------------------------------------------------------------
# Every model, not the ones that happened to get a test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reached", REACHED, ids=lambda r: r.path or "IDPConfig")
def test_every_model_in_the_tree_answers_for_a_key_it_is_handed(reached: Reached):
    """The class, not the instance: one bogus key inside each model, in turn.

    Both halves of the universe in one test, so there is no list of models the
    report is not expected to cover. A model that drops the key must name it; a
    model that keeps it must be *measured* keeping it, because "deliberately open"
    is only true while ``extra="allow"`` is still there.
    """
    leaf: dict = {BOGUS: "value"}
    if _keeps_extras(reached.model):
        leaf.update(_required_placeholders(reached.model))
    data = _nest(reached.steps, leaf)
    findings = collect_ignored_config_keys(data, IDPConfig, include_top_level=True)
    paths = [f.path for f in findings]

    if not _keeps_extras(reached.model):
        assert _expected_path(reached.steps, BOGUS) in paths, (
            f"{reached.model.__name__} at '{reached.path}' drops '{BOGUS}' and the "
            f"walk did not report it; got {paths}"
        )
        return

    assert paths == [], (
        f"{reached.model.__name__} keeps an undeclared key, so reporting one would be "
        f"a false positive; got {paths}"
    )
    kept = _descend(IDPConfig(**data), reached.steps)
    assert BOGUS in kept.model_dump(), (
        f"{reached.model.__name__} is not reported on the premise that extra='allow' "
        "keeps the key, and it did not keep it"
    )


@pytest.mark.parametrize(
    "reached",
    [r for r in REACHED if not _keeps_extras(r.model)],
    ids=lambda r: r.path or "IDPConfig",
)
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
            cases.append((reached.path, reached.steps, name, owned[0]))
    return cases


MISNESTINGS = _misnesting_cases()


def _generalise_written(steps: tuple[tuple[str, str], ...]) -> list[str]:
    """The written prefix in the index's spelling: a list step is ``[]``, a map ``.*``."""
    out = []
    for name, shape in steps:
        out.append(
            f"{name}[]" if shape == "list" else f"{name}.*" if shape == "map" else name
        )
    return out


def _every_path_declaring(key: str) -> list[tuple[str, ...]]:
    """All paths in the tree that declare a field of this name — every route to it.

    Read here rather than from ``models._field_path_index`` for the same reason
    ``_target`` is: a count taken from the code under test cannot contradict it. A
    model reached several ways contributes one path per route, which is exactly the
    fact ``REACHED`` (one route per model) throws away.
    """
    found: list[tuple[str, ...]] = []

    def visit(model: type[BaseModel], prefix: tuple[str, ...], chain: tuple):
        for name, field in model.model_fields.items():
            if name == key:
                found.append(prefix + (name,))
            shape, nested = _target(field.annotation)
            if nested is None or nested in chain:
                continue
            step = (
                f"{name}[]"
                if shape == "list"
                else f"{name}.*"
                if shape == "map"
                else name
            )
            visit(nested, prefix + (step,), chain + (nested,))

    visit(IDPConfig, (), (IDPConfig,))
    return found


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

    # The suggestion is owed exactly when the answer is unambiguous, and that
    # condition is computed here rather than taken from the report: a key declared at
    # several places under what was written cannot be resolved to one of them, and a
    # guess there is worse than silence because it sends the author to edit something
    # correct. Counted independently of the production index.
    here = tuple(_generalise_written(steps))
    places = [
        path
        for path in _every_path_declaring(key)
        if path[: len(here)] == here and path != here + (key,)
    ]
    nearest = min((len(p) for p in places), default=0)
    closest = [p for p in places if len(p) == nearest]
    if len(closest) == 1:
        expected = ".".join(closest[0])
        assert finding.suggestion == expected, (
            f"'{key}' is declared at exactly one nearest place under "
            f"'{parent_path}' ({expected}), so that is the answer owed; got "
            f"{finding.suggestion}"
        )
        assert finding.suggestion != written
    else:
        assert finding.suggestion is None, (
            f"'{key}' is declared at {len(closest)} places equally near under "
            f"'{parent_path}' ({closest}), so any single suggestion is a guess: "
            f"{finding.suggestion}"
        )


def test_the_issues_own_witness_names_the_exact_path_it_belongs_at():
    """``ocr.dpi`` is the measurement that #1134's second witness was taken from.

    ``dpi`` is a real field of ``ImageConfig``, reached as ``ocr.image.dpi``.
    ``ImageConfig`` is reachable four ways, so the answer has to come from the one
    route under what was written rather than from the four in the tree.
    """
    findings = collect_ignored_config_keys({"ocr": {"dpi": "abc"}}, IDPConfig)
    assert [(f.path, f.kind, f.suggestion) for f in findings] == [
        ("ocr.dpi", "unknown", "ocr.image.dpi")
    ]
    # And the value really is routed around the validator that would reject it.
    assert IDPConfig(**{"ocr": {"dpi": "abc"}}).ocr.image.dpi is None
    with pytest.raises(ValidationError):
        ImageConfig(dpi="abc")


@pytest.mark.parametrize(
    "where",
    ["ocr", "classification", "extraction", "extraction.confidence"],
)
def test_the_same_misnested_key_is_answered_for_each_place_it_can_be_written(where):
    """``dpi`` written at four different levels must get four different answers.

    ``ImageConfig`` hangs off four models, so one witness cannot show that the answer
    depends on where the key was written: ``ocr.image.dpi`` is also the first
    candidate in tree order, so an implementation that ignored the written prefix
    entirely would answer that witness correctly and the other three wrongly.
    """
    data: dict = {"dpi": 300}
    for segment in reversed(where.split(".")):
        data = {segment: data}
    findings = collect_ignored_config_keys(data, IDPConfig)
    assert [(f.path, f.suggestion) for f in findings] == [
        (f"{where}.dpi", f"{where}.image.dpi")
    ]


@pytest.mark.parametrize(
    "written,expected",
    [
        ("ocr.image.backend", "ocr.backend"),
        ("ocr.image.model_id", "ocr.model_id"),
        ("extraction.agentic.model", "extraction.model"),
        (
            "extraction.confidence.image.escalation_enabled",
            "extraction.confidence.escalation_enabled",
        ),
        (
            "rule_validation.fact_extraction.token_size",
            "rule_validation.token_size",
        ),
    ],
)
def test_a_key_written_one_level_too_deep_is_answered_too(written, expected):
    """The mirror image of the mis-nesting case, and just as plausible.

    ``docs/configuration.md`` tells the reader that ``ocr.backend`` and
    ``ocr.model_id`` really do sit one level up, which is exactly the sentence an
    author over-generalises into ``ocr.image.backend``. Restricting candidates to
    *under* the written prefix — the fix for a cross-section guess — silently
    withheld the answer for 272 such pairs, so the search now widens outwards from
    where the key was written and stops at the first level that has any candidate.
    """
    data: dict = {written.split(".")[-1]: "probe"}
    for segment in reversed(written.split(".")[:-1]):
        data = {segment: data}
    findings = collect_ignored_config_keys(data, IDPConfig)
    assert [(f.path, f.suggestion) for f in findings] == [(written, expected)]


def test_an_ambiguous_key_is_reported_with_no_suggestion_at_all():
    """No path beats a wrong path, and this is the case that produces a wrong one.

    ``enabled`` is declared at nine places under ``extraction``. Any one of them is a
    guess, and a guess sends the author to edit something that was already correct —
    which is how a warning teaches people to stop reading it.
    """
    findings = collect_ignored_config_keys({"extraction": {"enabled": True}}, IDPConfig)
    assert [(f.path, f.suggestion) for f in findings] == [("extraction.enabled", None)]
    assert (
        len([p for p in _every_path_declaring("enabled") if p[0] == "extraction"]) > 1
    )


def test_a_key_that_belongs_to_another_section_is_not_suggested_across_sections():
    """``hitl`` has no ``model``; the answer is not ``classification.model``.

    A candidate must sit under what was written. Ranking the whole tree by nearness
    instead produced exactly this cross-section answer — a suggestion about a section
    the author said nothing about.
    """
    findings = collect_ignored_config_keys({"hitl": {"model": "x"}}, IDPConfig)
    assert [(f.path, f.suggestion) for f in findings] == [("hitl.model", None)]
    assert len(_every_path_declaring("model")) > 1


def test_a_suggested_path_inside_a_list_is_written_as_one():
    """``ocr.postHook[].arn`` is writable; ``ocr.postHook.arn`` is not a path."""
    findings = collect_ignored_config_keys({"ocr": {"arn": "x"}}, IDPConfig)
    assert [(f.path, f.suggestion) for f in findings] == [
        ("ocr.arn", "ocr.postHook[].arn")
    ]


def test_every_suggestion_the_tree_can_produce_names_a_real_field():
    """No suggestion anywhere in the tree can name a path that does not exist.

    Driven over every field name declared anywhere, written at every model's own
    level: the whole space of suggestions this report can emit, checked against the
    paths the tree actually declares. A suggestion is the part a reader acts on
    without checking, so a wrong one is worse than none.
    """
    every_path = {
        path for r in REACHED for path in _every_path_declaring_under(r.model)
    }
    names = sorted({name for path in every_path for name in [path[-1]]})
    offered = 0
    for reached in REACHED:
        if _keeps_extras(reached.model):
            continue
        for name in names:
            if name in reached.model.model_fields:
                continue
            data = _nest(reached.steps, {name: "probe"})
            for finding in collect_ignored_config_keys(
                data, IDPConfig, include_top_level=True
            ):
                if finding.suggestion is None:
                    continue
                offered += 1
                assert tuple(finding.suggestion.split(".")) in every_path, (
                    f"{finding.path} was answered with '{finding.suggestion}', not a path"
                )
    assert offered > 50, f"only {offered} suggestions were produced; the sweep is thin"


def _every_path_declaring_under(model: type[BaseModel]) -> list[tuple[str, ...]]:
    """Every path the tree declares, as segment tuples, from ``IDPConfig``."""
    out: list[tuple[str, ...]] = []

    def visit(m: type[BaseModel], prefix: tuple[str, ...], chain: tuple):
        for name, field in m.model_fields.items():
            out.append(prefix + (name,))
            shape, nested = _target(field.annotation)
            if nested is None or nested in chain:
                continue
            step = (
                f"{name}[]"
                if shape == "list"
                else f"{name}.*"
                if shape == "map"
                else name
            )
            visit(nested, prefix + (step,), chain + (nested,))

    visit(IDPConfig, (), (IDPConfig,))
    return out


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
            kind = _free_form_kind(field.annotation)
            if kind is None:
                continue
            # The probe value is itself a mapping on purpose: a free-form document
            # usually is, and a scalar leaf cannot tell a walk that correctly stops
            # at this field from one that descends into it and finds nothing to
            # report. That difference was measured — see the mutation note on
            # `_target`.
            leaf: object = {BOGUS: {"nested": "kept"}}
            out.append(
                (reached.path, reached.steps, name, [leaf] if kind == "list" else leaf)
            )
    return out


def _free_form_kind(annotation) -> str | None:
    """``"map"``, ``"list"`` or ``None``: does this annotation hold a free-form document?

    Read structurally rather than from ``str(annotation)``, which spells the same
    type three ways depending on how it was written and is why a substring match on
    it silently found nothing.
    """
    while hasattr(annotation, "__metadata__"):
        annotation = typing.get_args(annotation)[0]
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
        _shape2, nested = _target(model.model_fields[name].annotation)
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
        annotation = field.annotation
        while hasattr(annotation, "__metadata__"):
            annotation = typing.get_args(annotation)[0]
        out[name] = samples.get(annotation, "placeholder")
    return out


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

    The check is on the **path**, resolved through the merged defaults, not on the
    key name appearing somewhere in some file: grepping for ``output_format:`` at any
    depth in any system-defaults file passes on a coincidence, and a suppression
    resting on a coincidence is the shape of the defect ``gate_exemptions.json``
    exists to catch.
    """
    from idp_common.config.merge_utils import merge_config_with_defaults

    merged = merge_config_with_defaults({}, "pattern-2", validate=False)
    node: object = merged
    for segment in dotted.split("."):
        assert isinstance(node, dict) and segment in node, (
            f"'{dotted}' is suppressed because this repository ships it in its own "
            f"defaults, and the merged pattern-2 default configuration has no "
            f"'{segment}' there — delete the entry"
        )
        node = node[segment]


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


@pytest.mark.parametrize("key", sorted(IDP_CONFIG_DEPRECATED_FIELDS))
def test_a_key_the_loader_relocates_is_never_reported_as_no_longer_used(key):
    """Derived over the whole deprecated set, because one member was not deprecated.

    ``rule_classes`` is listed as deprecated and is **renamed** to
    ``policy_classes`` by ``IDPConfig``'s own validator — it carries policy rules and
    they are honoured. Telling the author it "is no longer used and will be ignored"
    is an instruction to delete working rules, which is a worse failure than the
    silence this change removes.

    The rename lives in ``models.py`` rather than in ``migrations/``, so a caller
    that migrates first — as every caller of the walk must — still has not seen it.
    That is the trap, so the property is asserted over every member of the set rather
    than for the one member that had it: whether the value **survives** decides
    whether the report may call the key unused.
    """
    sentinel = "zz-relocation-sentinel-zz"
    probe: object = [{"name": sentinel}]
    try:
        dumped = repr(IDPConfig(**{key: probe}).model_dump())
    except ValidationError:
        pytest.skip(f"'{key}' is a declared field and cannot carry a probe value")

    relocated = sentinel in dumped
    findings = collect_ignored_config_keys(
        {key: probe}, IDPConfig, include_top_level=True
    )
    if relocated:
        assert findings == [], (
            f"'{key}' is relocated on load and its value survives, so reporting it "
            f"at all says something false: {[f.describe() for f in findings]}"
        )
    else:
        assert [f.kind for f in findings] == ["deprecated"], (
            f"'{key}' is dropped on load, so it should be reported as deprecated; "
            f"got {[(f.path, f.kind) for f in findings]}"
        )


def test_every_relocated_key_really_lands_on_its_new_name():
    """The rename map is the shared authority, so each entry has to work.

    An entry that names a destination the loader does not write would make the walk
    silent about a key that really is dropped — the exact silence this change is
    about, introduced by the mechanism meant to prevent a false alarm.
    """
    assert models_module.LEGACY_TOP_LEVEL_RENAMES, "the map is empty; delete the code"
    for old_name, rename in models_module.LEGACY_TOP_LEVEL_RENAMES.items():
        assert old_name not in IDPConfig.model_fields, (
            f"'{old_name}' is a declared field, so nothing renames it"
        )
        assert rename.to in IDPConfig.model_fields, (
            f"'{old_name}' is said to become '{rename.to}', which is not a field"
        )
        assert rename.since, f"'{old_name}' names no release; the warning quotes it"
        sentinel = [{"name": "zz-rename-zz"}]
        landed = getattr(IDPConfig(**{old_name: sentinel}), rename.to)
        assert landed == sentinel, (
            f"'{old_name}' did not arrive at '{rename.to}': {landed}"
        )


def test_the_discard_warning_still_names_the_release_that_renamed_the_key():
    """The loud both-keys-present warning is an operator's only notice of data loss.

    Rewriting the hardcoded rename as a loop over the map dropped "in v0.5.9" from
    that message — a small thing an operator uses to work out which guidance they
    followed — so the release now travels in the map with the destination.
    """
    with_both = {
        "policy_classes": [{"x-aws-idp-policy-type": "kept", "rule_properties": {}}],
        "rule_classes": [{"x-aws-idp-policy-type": "lost", "rule_properties": {}}],
    }
    logger = logging.getLogger("idp_common.config.models")
    records: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())  # type: ignore[method-assign]
    logger.addHandler(handler)
    try:
        IDPConfig(**with_both)
    finally:
        logger.removeHandler(handler)

    discards = [line for line in records if "DISCARDING" in line]
    assert discards, records
    assert "renamed to 'policy_classes' in v0.5.9" in discards[0], discards[0]
    assert "(1 entry)" in discards[0], discards[0]


def test_no_field_in_the_tree_holds_a_model_the_walk_declines_to_enter():
    """Closure over the one gap: a field naming several models is never entered.

    ``_nested_model_target`` answers ``(None, None)`` for an annotation with more
    than one model in it, because nothing in the annotation says which member a value
    is. Keys under such a field are dropped and would go unreported — unlike the two
    documented exclusions, where nothing is dropped at all.

    No field is shaped that way today. A discriminated union is an ordinary way to
    evolve a config schema, so this fails when one appears rather than letting the
    guarantee narrow in silence: the models the walk reaches would simply stop
    including that subtree, and every parametrisation here would shrink with it.
    """
    offenders = []
    for reached in REACHED:
        for name, field in reached.model.model_fields.items():
            if (
                _models_anywhere_in(field.annotation)
                and _target(field.annotation)[1] is None
            ):
                offenders.append(f"{reached.model.__name__}.{name}: {field.annotation}")
    assert not offenders, (
        "these fields carry a config model the unknown-key walk will not enter, so "
        "keys inside them are dropped with no diagnostic. Teach "
        "`_nested_model_target` the shape (a discriminated union can be resolved "
        "through its discriminator) rather than leaving the gap:\n  "
        + "\n  ".join(offenders)
    )


def _models_anywhere_in(annotation) -> bool:
    """Whether any Pydantic model appears anywhere in an annotation."""
    while hasattr(annotation, "__metadata__"):
        annotation = typing.get_args(annotation)[0]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return True
    return any(_models_anywhere_in(arg) for arg in typing.get_args(annotation))


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


def test_the_bound_on_the_log_line_is_a_bound():
    """A limit large enough never to apply is the way this stops working.

    The truncation branch below is exercised by building twice the limit's worth of
    keys, so it stays green for any value; the magnitude is the part a change could
    quietly make meaningless.
    """
    assert 5 <= MAX_REPORTED_IGNORED_KEYS <= 50, MAX_REPORTED_IGNORED_KEYS


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


@pytest.mark.parametrize(
    "submitted,named",
    [
        ({"notes_typo": "x"}, True),
        ({"criteria_bucket": "b"}, True),
        ({"description": "a profile description"}, False),
        ({"rule_classes": [{"name": "policyA"}]}, False),
    ],
    ids=["a-plain-typo", "deprecated", "read-by-another-consumer", "renamed-on-load"],
)
def test_validate_config_is_the_only_reporter_and_knows_which_keys_are_read(
    submitted, named
):
    """One reporter for every depth, and it knows the two exceptions at depth 0.

    ``idp_cli``'s ``config-validate`` and ``idp_sdk``'s ``ConfigOperation.validate``
    used to compute ``set(config) - set(IDPConfig.model_fields)`` each, which put two
    differently-worded warnings about one key in front of the same reader — and, being
    a raw set difference, named two keys the loader honours: ``update_configuration``
    pops and stores ``description``, and ``rule_classes`` is renamed to
    ``policy_classes``. Both now consume these findings, so a plain typo is reported
    once and those two are reported nowhere.
    """
    from idp_common.config.merge_utils import validate_config

    result = validate_config(
        {"classes": [{"name": "invoice"}], **submitted}, "pattern-2"
    )
    key = next(iter(submitted))
    warned = [w for w in result["warnings"] if "configuration key" in w and key in w]
    listed = [f for f in result["ignored_keys"] if f["path"] == key]
    if named:
        assert len(warned) == 1, warned
        assert len(listed) == 1, result["ignored_keys"]
    else:
        assert warned == [], warned
        assert listed == [], result["ignored_keys"]


def test_validate_config_returns_the_findings_structurally():
    """``result["ignored_keys"]`` is what a caller acts on rather than parsing prose.

    ``--strict`` keys on the depth of a path, and the SDK builds its
    ``deprecated_fields`` / ``unknown_fields`` from the kind, so the shape is part of
    the contract rather than a convenience.
    """
    from idp_common.config.merge_utils import validate_config

    result = validate_config(
        {
            "classes": [{"name": "invoice"}],
            "notes_typo": "x",
            "extraction": {"max_tokens": 1, "validation": {"enabld": False}},
        },
        "pattern-2",
    )
    assert result["ignored_keys"] == [
        {
            "path": "extraction.max_tokens",
            "kind": "deprecated",
            "suggestion": None,
        },
        {
            "path": "extraction.validation.enabld",
            "kind": "unknown",
            "suggestion": "extraction.validation.enabled",
        },
        {"path": "notes_typo", "kind": "unknown", "suggestion": None},
    ]


def test_the_findings_survive_a_configuration_that_does_not_validate():
    """An **invalid** configuration is the one whose unread keys matter most.

    A key at the wrong depth is accepted in silence while its correctly-nested
    sibling raises — ``ocr.dpi: "abc"`` validates and ``ocr.image.dpi: "abc"`` does
    not — so "the models will not read ``ocr.dpi``, did you mean ``ocr.image.dpi``?"
    is frequently *the explanation* for the error reported beside it rather than a
    separate observation. This is also the only reporter either front end has:
    ``idp-cli config-validate`` and ``idp_sdk``'s ``validate`` both consume
    ``ignored_keys``, so a ``[]`` here means neither of them says anything at all
    about an unread key for a configuration that failed.

    The question needs neither the merge nor ``model_validate``: it is asked of the
    submitted document. So this asserts against a config carrying **both** an unread
    key and a genuine validation error, which is what pins the call above the two
    failure returns rather than below them.
    """
    from idp_common.config.merge_utils import validate_config

    result = validate_config(
        {
            "classes": [{"name": "invoice"}],
            "extracton": {"model": "x"},
            "extraction": {"validation": {"enabld": False}},
            "ocr": {"image": {"dpi": "not-a-number"}},
        },
        "pattern-2",
    )

    assert result["valid"] is False
    assert any("Pydantic validation failed" in e for e in result["errors"]), (
        "the config has to actually fail for this to be the failing path; without "
        "an error it would pass on the success path too and discriminate nothing"
    )
    # Sorted by dotted path, so the nested finding precedes the top-level one here:
    # '.' sorts below 'o', which puts 'extraction.' ahead of 'extracton'.
    assert result["ignored_keys"] == [
        {
            "path": "extraction.validation.enabld",
            "kind": "unknown",
            "suggestion": "extraction.validation.enabled",
        },
        {"path": "extracton", "kind": "unknown", "suggestion": "extraction"},
    ]
    assert [w for w in result["warnings"] if "extracton" in w], (
        "the prose warning is what the CLI prints on its failing branch"
    )


def test_a_bad_pattern_name_is_answered_before_the_document_is_read():
    """The pattern check stays above the findings, and that is a decision.

    An unrecognised pattern is a fault in the *call*; there is no question about the
    document worth answering until it names a real one, and answering both at once
    puts a list of key paths in front of somebody whose mistake was the pattern
    argument. So this one return keeps no findings, unlike the two below it.
    """
    from idp_common.config.merge_utils import validate_config

    result = validate_config({"notes_typo": "x"}, "pattern-does-not-exist")

    assert result["valid"] is False
    assert result["ignored_keys"] == []
    assert any("Invalid pattern" in e for e in result["errors"])


@pytest.mark.parametrize("dotted", sorted(models_module.PATHS_READ_ELSEWHERE))
def test_a_path_read_elsewhere_still_has_a_reader_doing_the_reading(dotted):
    """The premise, computed per entry, on the **read** rather than on the key name.

    Same shape and same reason as ``TOP_LEVEL_KEY_EXEMPT`` in
    ``scripts/tests/test_preset_keys_are_read.py``: ``description`` occurs ten times
    in its reader, mostly as an unrelated parameter, so matching the bare word would
    hold after the line that actually reads the config key was deleted.
    """
    reader, marker = models_module.PATHS_READ_ELSEWHERE[dotted]
    source = REPO_ROOT / reader
    assert source.is_file(), f"'{dotted}' names {reader}, which does not exist"
    assert marker in source.read_text(encoding="utf-8"), (
        f"'{dotted}' is excluded because {reader} reads it, and that file no longer "
        f"contains {marker!r} — the exclusion has outlived its reason"
    )
    assert dotted not in IDPConfig.model_fields, (
        f"'{dotted}' is a declared field, so this entry is inert"
    )


def test_validate_config_does_not_report_a_legacy_key_the_migration_relocates():
    """The CLI sees pre-migration shapes, so it has to migrate before it asks.

    ``extraction.agentic.validation`` became ``extraction.validation`` in v0.7 and is
    moved on load rather than dropped. Reporting it would name a working key as a
    typo, which is a worse failure than the silence this change removes: it sends the
    author to edit something that is correct.
    """
    from idp_common.config.merge_utils import validate_config

    result = validate_config(
        {
            "classes": [{"name": "invoice"}],
            "extraction": {"agentic": {"validation": {"enabled": True}}},
        },
        "pattern-2",
    )
    offending = [w for w in result["warnings"] if "configuration key" in w]
    assert offending == [], offending


def test_validate_config_does_not_mutate_the_config_it_was_handed():
    """The migration runs on a copy: a caller's dict is an input, not scratch space."""
    from idp_common.config.merge_utils import validate_config

    config = {
        "classes": [{"name": "invoice"}],
        "extraction": {"agentic": {"validation": {"enabled": True}}},
    }
    before = copy.deepcopy(config)
    validate_config(config, "pattern-2")
    assert config == before
