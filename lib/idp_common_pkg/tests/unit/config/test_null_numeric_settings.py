# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A stored ``null`` for a numeric setting must not stop a configuration loading.

A YAML key with no value (``temperature:``) or a JSON ``null`` reaches a
``mode="before"`` field validator as ``None``. A validator that hands it to
``int()`` or ``float()`` raises ``TypeError``, which pydantic does not convert to
a ``ValidationError``, so ``IDPConfig.model_validate`` raised and the whole
configuration failed to load, not just the one setting.

The cases are derived from the models rather than listed. The rule is about
capability, not about how a field's type is spelled: for every field that carries
a field validator (a ``"*"`` validator covers every field), a null may produce a
``ValidationError`` and nothing else. A validator added later with the same
defect fails that test whatever its annotation. On top of that, every validated
field declared as a number (``int``/``float``, constrained or ``Annotated``,
``Optional`` or a union of them) must load as its declared default, or as
``None`` when it is Optional.

The second half of the file applies the same rule to malformed non-null values
(``[]``, ``{}``, infinities, NaN, ``10**400``), which made validators raise
``TypeError`` or ``OverflowError``. It also checks, field by field, that turning
those into a ``ValidationError`` changed no outcome any value had before.

What this does not reach: validators pydantic does not list in
``__pydantic_decorators__.field_validators`` (an ``Annotated[..., BeforeValidator]``
or a ``model_validator``), and models that are not module-level classes of
``idp_common.config.models``. Every model under ``IDPConfig`` is one today.
"""

import inspect
import random
import types
from typing import (
    Annotated,
    Any,
    Dict,
    List,
    Optional,
    Tuple,
    Union,
    get_args,
    get_origin,
)

import pytest
from pydantic import BaseModel, PositiveInt, ValidationError, confloat

import idp_common.config.models as models
from idp_common.config.models import IDPConfig

pytestmark = pytest.mark.unit


def _strip_annotated(annotation: Any) -> Any:
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def _is_number(annotation: Any) -> bool:
    annotation = _strip_annotated(annotation)
    return (
        inspect.isclass(annotation)
        and issubclass(annotation, (int, float))
        and not issubclass(annotation, bool)
    )


def _numeric_kind(annotation: Any) -> Optional[str]:
    """``"required"`` for a number, ``"optional"`` for a union admitting ``None``
    whose other members are all numbers, ``None`` for anything else."""
    annotation = _strip_annotated(annotation)
    if _is_number(annotation):
        return "required"
    if get_origin(annotation) in (Union, types.UnionType):
        args = get_args(annotation)
        members = [a for a in args if a is not type(None)]
        if members and all(_is_number(a) for a in members):
            return "optional" if type(None) in args else "required"
    return None


def _config_models() -> List[type]:
    return [
        cls
        for _, cls in sorted(vars(models).items())
        if inspect.isclass(cls)
        and issubclass(cls, BaseModel)
        and cls.__module__ == models.__name__
    ]


def _validated_fields(cls: type) -> List[str]:
    names = set()
    for decorator in cls.__pydantic_decorators__.field_validators.values():
        for field in decorator.info.fields:
            if field == "*":
                names.update(cls.model_fields)
            elif field in cls.model_fields:
                names.add(field)
    return sorted(names)


ALL_VALIDATED = [
    (cls, name) for cls in _config_models() for name in _validated_fields(cls)
]


def _validated_numeric_fields() -> List[Tuple[type, str, str]]:
    found = []
    for cls, name in ALL_VALIDATED:
        kind = _numeric_kind(cls.model_fields[name].annotation)
        if kind is not None:
            found.append((cls, name, kind))
    return found


@pytest.mark.parametrize(
    "cls,name", ALL_VALIDATED, ids=[f"{c.__name__}.{n}" for c, n in ALL_VALIDATED]
)
def test_a_null_reaching_any_validator_raises_at_most_a_validation_error(cls, name):
    """A ``ValidationError`` names the setting; any other exception escapes pydantic
    and takes the whole configuration with it. ``by_name=True`` makes pydantic read
    the field name whatever alias the field declares, so an aliased field cannot
    ignore the null and pass without its validator running."""
    try:
        cls.model_validate({name: None}, by_name=True)
    except ValidationError:
        pass


def test_the_annotation_rule_recognises_every_numeric_spelling():
    """The default-value cases below are selected by annotation, so the selector
    itself is pinned against the spellings a new numeric field could use."""
    assert _numeric_kind(float) == "required"
    assert _numeric_kind(Annotated[float, "x"]) == "required"
    assert _numeric_kind(confloat(ge=0)) == "required"
    assert _numeric_kind(PositiveInt) == "required"
    assert _numeric_kind(Union[int, float]) == "required"
    assert _numeric_kind(Optional[int]) == "optional"
    assert _numeric_kind(Optional[confloat(ge=0)]) == "optional"
    assert _numeric_kind(Optional[PositiveInt]) == "optional"
    assert _numeric_kind(float | None) == "optional"
    assert _numeric_kind(bool) is None
    assert _numeric_kind(Optional[str]) is None


CASES = _validated_numeric_fields()


def _expected(cls: type, name: str, kind: str) -> Any:
    if kind == "optional":
        return None
    return cls.model_fields[name].get_default(call_default_factory=True)


def test_the_derived_case_list_covers_the_known_affected_fields():
    """An empty derivation would collect as zero tests and pass. Pin a few members
    that are known to carry a numeric validator, from both shapes."""
    names = {(cls.__name__, name) for cls, name, _ in CASES}
    for member in [
        ("ExtractionConfig", "temperature"),
        ("ExtractionConfig", "top_k"),
        ("OCRConfig", "max_workers"),
        ("HITLConfig", "confidence_threshold"),
        ("TableParsingConfig", "min_parse_success_rate"),
        ("ChatConfig", "max_tokens"),
    ]:
        assert member in names, member
    kinds = {kind for _, _, kind in CASES}
    assert kinds == {"required", "optional"}


@pytest.mark.parametrize(
    "cls,name,kind", CASES, ids=[f"{c.__name__}.{n}" for c, n, _ in CASES]
)
def test_a_null_numeric_setting_loads_as_its_default(cls, name, kind):
    value = getattr(cls.model_validate({name: None}, by_name=True), name)
    assert value == _expected(cls, name, kind)


def _paths_from_root() -> List[Tuple[Tuple[str, ...], type, str, str]]:
    """Every place under ``IDPConfig`` an affected field is reached through plain
    nested-model fields, so the null is exercised through the whole-config load."""
    wanted: Dict[type, List[Tuple[str, str]]] = {}
    for cls, name, kind in CASES:
        wanted.setdefault(cls, []).append((name, kind))

    paths = []

    def visit(model: type, prefix: Tuple[str, ...], chain: Tuple[type, ...]) -> None:
        for name, kind in wanted.get(model, []):
            paths.append((prefix + (name,), model, name, kind))
        for field_name, field in model.model_fields.items():
            annotation = field.annotation
            if get_origin(annotation) in (Union, types.UnionType):
                members = [a for a in get_args(annotation) if a is not type(None)]
                annotation = members[0] if len(members) == 1 else None
            if (
                inspect.isclass(annotation)
                and issubclass(annotation, BaseModel)
                and annotation not in chain
            ):
                visit(annotation, prefix + (field_name,), chain + (annotation,))

    visit(IDPConfig, (), (IDPConfig,))
    return paths


ROOT_PATHS = _paths_from_root()


def test_the_root_paths_reach_affected_fields():
    assert any(path[-1] == "temperature" for path, *_ in ROOT_PATHS)
    assert ("ocr", "max_workers") in {path for path, *_ in ROOT_PATHS}


@pytest.mark.parametrize(
    "path,cls,name,kind", ROOT_PATHS, ids=[".".join(p[0]) for p in ROOT_PATHS]
)
def test_a_null_numeric_setting_does_not_stop_the_configuration_loading(
    path, cls, name, kind
):
    document: Dict[str, Any] = {}
    cursor = document
    for segment in path[:-1]:
        cursor = cursor.setdefault(segment, {})
    cursor[path[-1]] = None

    config: Any = IDPConfig.model_validate(document, by_name=True)
    for segment in path:
        config = getattr(config, segment)
    assert config == _expected(cls, name, kind)


@pytest.mark.parametrize(
    "stored,expected",
    [
        ("0.4", 0.4),
        (0.25, 0.25),
        ("0.123", 0.123),
        (0.123, 0.123),
        ("0.1234567", 0.1234567),
        (0.1234567, 0.1234567),
        (1, 1.0),
        ("", 0.0),
        (True, 1.0),
        (False, 0.0),
    ],
)
def test_a_non_null_float_still_parses_as_before(stored, expected):
    assert models.ExtractionConfig.model_validate({"top_p": stored}).top_p == expected


def test_a_non_null_max_workers_still_parses_as_before():
    assert models.OCRConfig.model_validate({"max_workers": "8"}).max_workers == 8
    assert models.OCRConfig.model_validate({"max_workers": 2.7}).max_workers == 2
    assert models.OCRConfig.model_validate({"max_workers": True}).max_workers == 1
    assert models.OCRConfig.model_validate({"max_workers": ""}).max_workers == 20


@pytest.mark.parametrize("stored", [0, -1, 0.0, "0", "-3", "8.5", "1e3"])
def test_an_out_of_range_or_fractional_max_workers_is_still_rejected(stored):
    with pytest.raises(ValidationError):
        models.OCRConfig.model_validate({"max_workers": stored})


def test_a_blank_float_string_keeps_its_previous_outcome():
    """Only ``None`` changed. A whitespace-only string is still rejected by the
    per-model validators and still read as ``0.0`` by the classification ones."""
    with pytest.raises(ValidationError):
        models.ExtractionConfig.model_validate({"temperature": "  "})
    settings = models.ClassificationModelSettings.model_validate({"top_k": "  "})
    assert settings.top_k == 0.0
    with pytest.raises(ValidationError):
        models.OCRConfig.model_validate({"max_workers": "  "})


@pytest.mark.parametrize(
    "stored",
    [
        "nan",
        "inf",
        float("nan"),
        float("inf"),
        float("-inf"),
        "abc",
        "0,5",
        -0.5,
        "-0.5",
        b"",
        bytearray(),
    ],
)
def test_a_non_numeric_float_string_is_still_rejected(stored):
    with pytest.raises(ValidationError):
        models.ExtractionConfig.model_validate({"temperature": stored})


def test_a_null_falls_back_to_the_validated_class_own_default():
    """No model in the tree overrides an inherited numeric default today, so the
    derived cases cannot tell the class being validated from the class declaring
    the validator. A subclass that does override one must get its own default."""

    class Override(models.ExtractionConfig):
        temperature: float = 0.6

    assert Override.model_validate({"temperature": None}).temperature == 0.6


def test_an_in_range_float_parses_exactly():
    """A fixed-seed sample across the whole of ``top_p``'s range, as numbers and as
    strings: any rounding, clamping or sign change in the shared parser fails here
    rather than only at the handful of values spelled out above."""
    rnd = random.Random(1585)
    for value in [rnd.random() for _ in range(200)]:
        for stored in (value, repr(value)):
            parsed = models.ExtractionConfig.model_validate({"top_p": stored}).top_p
            assert parsed == float(stored), stored


# -- Malformed non-null values (class of #1583 / #1585) -----------------------
#
# ``temperature: []`` or ``{}`` made ``float()`` raise ``TypeError``, and
# ``ocr.max_workers: .inf`` or ``top_k: 10**400`` made ``int()`` or ``float()``
# raise ``OverflowError``. Pydantic converts neither, so the whole configuration
# failed to load with an error naming no setting. ``models.field_validator``
# re-raises both as ``ValueError``. The rule is the same as for a null: a
# validator may answer with a ``ValidationError`` and nothing else, and nothing
# that parsed before may parse differently.

MALFORMED = {
    "empty list": [],
    "empty dict": {},
    "inf": float("inf"),
    "-inf": float("-inf"),
    "nan": float("nan"),
    "10**400": 10**400,
}

#: Values a stored configuration ordinarily carries. Driven through every
#: validator next to ``MALFORMED`` to show the guard changes no outcome they had.
ORDINARY = [0, 1, -1, 0.5, 8, "", "  ", "0.25", "8", "abc", True, False, 10**18]


def _outcome(cls: type, name: str, value: Any) -> Tuple[str, Any]:
    try:
        return "ok", repr(
            getattr(cls.model_validate({name: value}, by_name=True), name)
        )
    except ValidationError as exc:
        return "invalid", [(e["loc"], e["type"], e["msg"]) for e in exc.errors()]
    except Exception as exc:  # the defect under test; reported, not swallowed
        return "raw", type(exc)


def test_the_malformed_cases_reach_every_validated_numeric_field():
    numeric = {(cls, name) for cls, name, _ in CASES}
    assert numeric  # an empty derivation would collect as a skip, not a failure
    assert numeric <= set(ALL_VALIDATED)


@pytest.mark.parametrize("label", sorted(MALFORMED))
@pytest.mark.parametrize(
    "cls,name", ALL_VALIDATED, ids=[f"{c.__name__}.{n}" for c, n in ALL_VALIDATED]
)
def test_a_malformed_value_raises_at_most_a_validation_error(cls, name, label):
    try:
        cls.model_validate({name: MALFORMED[label]}, by_name=True)
    except ValidationError:
        pass


@pytest.mark.parametrize(
    "cls,name", ALL_VALIDATED, ids=[f"{c.__name__}.{n}" for c, n in ALL_VALIDATED]
)
def test_the_guard_changes_only_what_used_to_crash(cls, name, monkeypatch):
    """Each value's outcome with the guard disabled is the previous behaviour.

    A value that parsed must parse to the same thing, and one that was rejected
    must be rejected with the same errors. One that raised ``TypeError`` or
    ``OverflowError`` must now be a ``ValidationError`` naming this field, and
    must not have been turned into a parsed value or a default.
    """
    field = cls.model_fields[name]
    names = {name, field.alias, field.validation_alias} - {None}
    for value in [*MALFORMED.values(), *ORDINARY]:
        with monkeypatch.context() as patched:
            patched.setattr(models, "_UNCONVERTED_INPUT_ERRORS", ())
            before = _outcome(cls, name, value)
        after = _outcome(cls, name, value)
        if before[0] == "raw":
            assert before[1] in (TypeError, OverflowError), (value, before)
            assert after[0] == "invalid", (value, after)
            assert {loc[0] for loc, _, _ in after[1]} <= names, (value, after)
        else:
            assert after == before, value


def test_the_comparison_has_crashes_to_compare(monkeypatch):
    """Without the guard the malformed values do crash, so the comparison above
    exercises the conversion rather than only the unchanged branch."""
    monkeypatch.setattr(models, "_UNCONVERTED_INPUT_ERRORS", ())
    crashed = {
        type_
        for cls, name in ALL_VALIDATED
        for value in MALFORMED.values()
        for kind, type_ in [_outcome(cls, name, value)]
        if kind == "raw"
    }
    assert crashed == {TypeError, OverflowError}


def test_the_guard_converted_the_reported_crashes():
    """The four reported inputs, as a whole-configuration load."""
    for document, loc in [
        ({"ocr": {"max_workers": float("inf")}}, ("ocr", "max_workers")),
        ({"extraction": {"temperature": []}}, ("extraction", "temperature")),
        ({"extraction": {"temperature": {}}}, ("extraction", "temperature")),
        ({"extraction": {"top_k": 10**400}}, ("extraction", "top_k")),
    ]:
        with pytest.raises(ValidationError) as caught:
            IDPConfig.model_validate(document)
        assert [e["loc"] for e in caught.value.errors()] == [loc], document


@pytest.mark.parametrize(
    "path,cls,name,kind", ROOT_PATHS, ids=[".".join(p[0]) for p in ROOT_PATHS]
)
def test_a_malformed_numeric_setting_fails_the_load_only_as_a_validation_error(
    path, cls, name, kind
):
    for value in MALFORMED.values():
        document: Dict[str, Any] = {}
        cursor = document
        for segment in path[:-1]:
            cursor = cursor.setdefault(segment, {})
        cursor[path[-1]] = value
        try:
            IDPConfig.model_validate(document, by_name=True)
        except ValidationError:
            pass


def test_every_validator_in_the_module_is_guarded():
    """Capability, not spelling: a validator declared with pydantic's own
    ``field_validator`` lacks the guard, whatever field or type it covers."""
    validators = [
        (cls.__name__, decorator.func)
        for cls in _config_models()
        for decorator in cls.__pydantic_decorators__.field_validators.values()
    ]
    assert validators
    unguarded = [
        (owner, getattr(func, "__name__", func))
        for owner, func in validators
        if not getattr(func, "__idp_input_errors_converted__", False)
    ]
    assert unguarded == []


def _probe_models() -> List[type]:
    """Two models whose field accepts anything, so pydantic's own type check can
    never be what rejects the value: only the guard can turn the crash into a
    ``ValidationError``. One validator is a classmethod, as every validator in
    ``models`` is today, and one a plain function, which pydantic also accepts."""

    class ClassmethodProbe(BaseModel):
        loose: Any = None

        @models.field_validator("loose", mode="before")
        @classmethod
        def parse(cls, v: Any) -> Any:
            return int(v)

    class FunctionProbe(BaseModel):
        loose: Any = None

        @models.field_validator("loose", mode="before")
        def parse(cls, v: Any) -> Any:  # noqa: N805 - pydantic passes the class
            return int(v)

    return [ClassmethodProbe, FunctionProbe]


@pytest.mark.parametrize("probe", _probe_models(), ids=lambda c: c.__name__)
@pytest.mark.parametrize("label", ["empty list", "empty dict", "inf", "-inf"])
def test_the_guard_does_not_rely_on_the_field_type_to_reject(probe, label):
    with pytest.raises(ValidationError) as caught:
        probe.model_validate({"loose": MALFORMED[label]})
    assert [e["loc"] for e in caught.value.errors()] == [("loose",)]
    assert probe.model_validate({"loose": "7"}).loose == 7
