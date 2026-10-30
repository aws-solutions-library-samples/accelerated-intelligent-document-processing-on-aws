# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`base-classification.yaml` and the classification models must agree on two things.

The project rule is that the system defaults are the canonical list of settings and
that the loader ignores any key the model does not define
(`.claude/rules/config-and-models.md`), which makes
`system_defaults/base-classification.yaml` the surface an operator, the editor and
an agent reading the bundled source all discover classification settings from. Two
parity gates already sit around that file — its *filename* against
`docs/configuration.md` (`test_system_defaults_doc_parity.py`) and the model's
*fields* against the UI schema (`scripts/sdlc/tests/test_config_ui_schema_parity.py`)
— and **neither compared the YAML's key set with the Pydantic field set in either
direction**. Two shipped defects sat in exactly that gap:

* `reasoning_effort` and `model_lambda_hook_arn` were live on all three
  classification calls — read by `_get_classification_config`, forwarded to Bedrock,
  offered in the editor — and absent from the YAML, so classification silently ran
  at the model's own default and no reader of the canonical list could tell the
  setting existed. `base-extraction.yaml` shipped `reasoning_effort`, so the
  omission was specific to classification.
* The section-classification call shipped `task_prompt_topk_packet` and
  `task_prompt_verbalized_packet` — about a kilobyte of prompt text in every profile
  — at a path the service can never read, where editing them has no effect.

So this module asserts both directions, plus the property that makes the file's
values and the code's values one answer rather than two:

1. every leaf key in the YAML is a field the models define (no exceptions);
2. every model field has an entry in the YAML, or an entry in one of the two
   exemption tables below with its own reason; and
3. every value the YAML ships equals the model's field default, so **clearing** a
   field in the editor lands on the value the profile was already running. The
   divergence that motivated it: `DEFAULT_CLASSIFICATION_MODEL` was `nova-pro` while
   the YAML shipped `nova-2-lite`, so clearing one call's model moved that call onto
   a different, more expensive model and the warning named a model the profile did
   not use. `top_p` was `0.1` against a shipped `0.0`, and a positive `top_p`
   overrides `temperature`.

The exemption tables are registered in `scripts/tests/gate_exemptions.json`. Every
member's premise is computed here, per member, rather than asserted in prose — see
the four `test_*` functions after the two directions.
"""

from __future__ import annotations

import pathlib
from typing import Any, Dict, Iterator, Tuple
from unittest.mock import patch

import pytest
import yaml
from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from idp_common.config import models as config_models
from idp_common.config.merge_utils import merge_config_with_defaults
from idp_common.config.migrations.v08_to_v09 import (
    SECTION_CLASSIFICATION_UNREAD_CONFIDENCE_KEYS,
)

pytestmark = pytest.mark.unit

_DEFAULTS_DIR = (
    pathlib.Path(config_models.__file__).resolve().parent / "system_defaults"
)
_CLASSIFICATION_DEFAULTS = _DEFAULTS_DIR / "base-classification.yaml"
_OCR_DEFAULTS = _DEFAULTS_DIR / "base-ocr.yaml"

#: Paths are spelled the way `test_config_ui_schema_parity.py` spells them, so that
#: an entry can be compared with its counterpart there by eye.
_ROOT = "classification"


# ---------------------------------------------------------------------------
# Exemptions — one member's worth of reason each, premise computed below.
# ---------------------------------------------------------------------------

#: Subtrees the defaults omit, where the reason is a property of the whole subtree
#: rather than of any field in it. Matches the path and everything under it.
DEFAULTS_OMISSION_EXEMPT_SUBTREES: Dict[str, str] = {
    "classification.postHook": (
        "Pipeline hooks are registered by the Feature Platform at install time, not "
        "configured; the field is an empty list and an empty list has no value to "
        "ship. Computed by test_an_omitted_collection_has_nothing_to_ship."
    ),
    "classification.split_then_classify.splitting.boundary_model": (
        "Declared for a boundary-detection model (#1329) and not implemented: "
        "splitting.method refuses 'boundary_model', so a default here would be a "
        "setting nothing can reach. Computed by "
        "test_the_method_reaching_an_omitted_block_is_refused."
    ),
    "classification.split_then_classify.section_classification.embeddings": (
        "Declared for embeddings-based section classification (#1329) and not "
        "implemented: section_classification.method refuses 'embeddings'. Computed "
        "by test_the_method_reaching_an_omitted_block_is_refused."
    ),
    "classification.split_then_classify.section_classification.decision_model": (
        "Declared for an external classifier endpoint (#1329) and not implemented: "
        "section_classification.method refuses 'decision_model'. Computed by "
        "test_the_method_reaching_an_omitted_block_is_refused."
    ),
    "classification.split_then_classify.section_classification.low_confidence": (
        "Applies only when section_classification.method is NOT 'llm' — it decides "
        "what happens when a non-LLM classifier is unsure — and no such method is "
        "accepted, so no document can reach it. Its witness is any non-llm value "
        "('embeddings'), which is why it shares the computation above rather than "
        "being its own kind of claim."
    ),
}

#: Individual fields the defaults omit. One exact path per entry: a field is the
#: smallest thing that will do here, so no entry covers a sibling it has not
#: answered for.
DEFAULTS_OMISSION_EXEMPT_FIELDS: Dict[str, str] = {}

for _call in (
    "classification.combined.llm",
    "classification.split_then_classify.splitting.llm",
    "classification.split_then_classify.section_classification.llm",
):
    DEFAULTS_OMISSION_EXEMPT_FIELDS[f"{_call}.image.dpi"] = (
        "ImageConfig is shared by every stage's image panel, but dpi governs the "
        "PDF-to-raster render, which happens once, during OCR. Its default belongs "
        "on the block that reads it and is shipped there, in base-ocr.yaml. Computed "
        "by test_an_omitted_ocr_only_image_field_has_its_default_where_it_is_read."
    )
    DEFAULTS_OMISSION_EXEMPT_FIELDS[f"{_call}.image.preprocessing"] = (
        "Binarization is applied during the same OCR rasterization, for the same "
        "reason as dpi above, and its default is shipped on ocr.image too. Computed "
        "by test_an_omitted_ocr_only_image_field_has_its_default_where_it_is_read."
    )
del _call

for _key in sorted(SECTION_CLASSIFICATION_UNREAD_CONFIDENCE_KEYS):
    DEFAULTS_OMISSION_EXEMPT_FIELDS[
        f"classification.split_then_classify.section_classification.llm.confidence.{_key}"
    ] = (
        "The per-segment confidence block, on a call that is one call per section "
        "and returns one class. The service takes this shape only for a call that "
        "returns a `segments` array, which section classification never does, so a "
        "default here is prompt text in every profile at a path nothing reads. "
        "Computed by test_an_omitted_packet_confidence_block_is_not_read_here; the "
        "same two keys are omitted from the UI schema with the same reason."
    )
del _key

#: The non-llm `method` value whose acceptance would make each omitted sub-block
#: reachable. The premise for those four entries, as a per-member table.
_METHOD_REACHING_OMITTED_BLOCK: Dict[str, Tuple[str, str]] = {
    "classification.split_then_classify.splitting.boundary_model": (
        "classification.split_then_classify.splitting.method",
        "boundary_model",
    ),
    "classification.split_then_classify.section_classification.embeddings": (
        "classification.split_then_classify.section_classification.method",
        "embeddings",
    ),
    "classification.split_then_classify.section_classification.decision_model": (
        "classification.split_then_classify.section_classification.method",
        "decision_model",
    ),
    "classification.split_then_classify.section_classification.low_confidence": (
        "classification.split_then_classify.section_classification.method",
        "embeddings",
    ),
}


# ---------------------------------------------------------------------------
# The two key sets.
# ---------------------------------------------------------------------------


def _shipped() -> Dict[str, Any]:
    body = yaml.safe_load(_CLASSIFICATION_DEFAULTS.read_text())
    assert isinstance(body, dict) and "classification" in body, (
        f"{_CLASSIFICATION_DEFAULTS.name} does not hold a `classification:` block, so "
        "this gate would compare nothing"
    )
    return body["classification"]


def _nested_model(field) -> type[BaseModel] | None:
    """The ``BaseModel`` a field holds, through ``Optional`` and ``List``."""
    annotation = field.annotation
    for candidate in [annotation, *getattr(annotation, "__args__", ())]:
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            return candidate
    return None


def _yaml_paths(node: Any, prefix: str = _ROOT) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield f"{prefix}.{key}"
            yield from _yaml_paths(value, f"{prefix}.{key}")


def _model_paths(model: type[BaseModel], prefix: str = _ROOT) -> Iterator[str]:
    for name, field in model.model_fields.items():
        path = f"{prefix}.{name}"
        yield path
        nested = _nested_model(field)
        if nested is not None:
            yield from _model_paths(nested, path)


def _is_exempt(path: str) -> bool:
    if path in DEFAULTS_OMISSION_EXEMPT_FIELDS:
        return True
    return any(
        path == subtree or path.startswith(f"{subtree}.")
        for subtree in DEFAULTS_OMISSION_EXEMPT_SUBTREES
    )


def test_every_defaults_key_is_a_field_the_models_define():
    """A key the models do not define is silently discarded by ``extra='ignore'``.

    No exemptions in this direction, deliberately: a key in the canonical defaults
    that nothing reads is the DEF-15 shape — an operator edits it and nothing
    happens — and there is no reason to ship one.
    """
    shipped = set(_yaml_paths(_shipped()))
    defined = set(_model_paths(config_models.ClassificationConfig))
    assert shipped, "no keys read from the defaults"
    undefined = sorted(shipped - defined)
    assert not undefined, (
        "these keys in base-classification.yaml are not classification model "
        f"fields, so the loader drops them and editing them does nothing: {undefined}"
    )


def test_every_model_field_has_an_entry_in_the_defaults():
    """The direction nothing checked, and the one both shipped defects were in.

    This is also the closure over the exemption tables: a field that is in neither
    the defaults nor one of them fails here by name, so neither table can quietly
    stop covering the universe it is a carve-out from.
    """
    shipped = set(_yaml_paths(_shipped()))
    missing = sorted(
        path
        for path in _model_paths(config_models.ClassificationConfig)
        if path not in shipped and not _is_exempt(path)
    )
    assert not missing, (
        "these classification settings have no entry in base-classification.yaml, "
        "so nothing reading the canonical list of settings can discover them: "
        f"{missing}. Add them with an inline comment, or register the omission in "
        "DEFAULTS_OMISSION_EXEMPT_FIELDS / DEFAULTS_OMISSION_EXEMPT_SUBTREES with a "
        "reason for that one member and a computed premise."
    )


# ---------------------------------------------------------------------------
# Direction 3: the file's value and the code's default are one answer.
# ---------------------------------------------------------------------------


def _field_default(model: type[BaseModel], name: str) -> Any:
    """The value ``model`` supplies for ``name`` when the configuration omits it.

    Read from a default instance rather than by calling ``default_factory``, because
    Pydantic allows a factory that takes the already-validated data and so the
    callable's signature is not known here.
    """
    field = model.model_fields[name]
    if field.default is not PydanticUndefined:
        return field.default
    return getattr(model(), name)


def _shipped_leaves(
    model: type[BaseModel], node: Any, prefix: str = _ROOT
) -> Iterator[Tuple[str, type[BaseModel], str, Any]]:
    """``(path, owning model, field name, shipped value)`` per scalar leaf shipped."""
    if not isinstance(node, dict):
        return
    for name, field in model.model_fields.items():
        if name not in node:
            continue
        path = f"{prefix}.{name}"
        nested = _nested_model(field)
        if nested is not None:
            yield from _shipped_leaves(nested, node[name], path)
        else:
            yield path, model, name, node[name]


def test_every_shipped_value_is_the_models_default_for_that_field():
    """⚠️ The field default is what a **cleared** field falls back to.

    The YAML wins on a normal load, so a divergence between the two is invisible
    until a field is cleared — and at that moment the call moves to the code's
    opinion instead of the one the rest of the profile is running, with a warning
    (where there is one) naming the code's value. That is how a cleared model field
    came to move a call from nova-2-lite to nova-pro.

    The one excused shape is computed, not listed: a field whose code default is the
    empty string has no default at all, which is deliberate for a prompt — the text
    is a kilobyte of prose and a second copy of it in Python would be the drift this
    gate exists to stop. ``test_only_prompt_fields_carry_no_code_default`` bounds
    which fields may take that excuse.
    """
    mismatched = []
    for path, model, name, shipped in _shipped_leaves(
        config_models.ClassificationConfig, _shipped()
    ):
        default = _field_default(model, name)
        if default == "":
            continue
        coerced = getattr(model(**{name: shipped}), name)
        if coerced != default:
            mismatched.append(f"{path}: ships {coerced!r}, field default {default!r}")
    assert not mismatched, (
        "these settings ship one value and default to another, so clearing the field "
        "in the editor changes behaviour rather than restoring it:\n  "
        + "\n  ".join(mismatched)
    )


def test_only_prompt_fields_carry_no_code_default():
    """Bounds the excuse the test above computes.

    An empty code default is right for prompt prose and wrong for anything else — a
    numeric or enum setting with an empty default would take the excuse and hide
    exactly the divergence this module is about.
    """
    unexpected = sorted(
        path
        for path, model, name, _ in _shipped_leaves(
            config_models.ClassificationConfig, _shipped()
        )
        if model.model_fields[name].default == "" and "prompt" not in name
    )
    assert not unexpected, (
        "these shipped settings have an empty code default but are not prompts, so "
        f"they are excused from the default-parity check for no reason: {unexpected}"
    )


# ---------------------------------------------------------------------------
# The exemptions: staleness, and one computed premise per member.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    sorted({*DEFAULTS_OMISSION_EXEMPT_FIELDS, *DEFAULTS_OMISSION_EXEMPT_SUBTREES}),
)
def test_an_exempt_path_is_still_a_model_field(path: str):
    """A renamed or deleted field leaves an entry pre-exempting the next thing there."""
    assert path in set(_model_paths(config_models.ClassificationConfig)), (
        f"{path} is registered as a deliberate omission from the defaults but is no "
        "longer a classification model field; delete the entry."
    )


@pytest.mark.parametrize(
    "path",
    sorted({*DEFAULTS_OMISSION_EXEMPT_FIELDS, *DEFAULTS_OMISSION_EXEMPT_SUBTREES}),
)
def test_an_exempt_path_still_has_no_default_to_find(path: str):
    """Non-vacuity: an entry that shields nothing is dead and must go.

    Without this, giving one of these settings a default in the YAML would leave an
    entry here claiming it is deliberately absent when it is present — the exact
    "one justification, and it is false for this member" shape the registry exists
    to catch.
    """
    assert path not in set(_yaml_paths(_shipped())), (
        f"{path} now has an entry in base-classification.yaml, so its exemption "
        "shields nothing; delete it from DEFAULTS_OMISSION_EXEMPT_FIELDS / "
        "DEFAULTS_OMISSION_EXEMPT_SUBTREES."
    )


@pytest.mark.parametrize(
    ("block", "method_path", "method_value"),
    [(k, *v) for k, v in sorted(_METHOD_REACHING_OMITTED_BLOCK.items())],
)
def test_the_method_reaching_an_omitted_block_is_refused(
    block: str, method_path: str, method_value: str
):
    """The premise for the four declared-but-unimplemented blocks, per block.

    "Absent on purpose because nothing can reach it" is only true while the `method`
    value that would reach it is refused. The day one of them is implemented and
    accepted, this fails and that block has to be given defaults like any other
    setting — which is the whole point of computing the premise instead of writing
    it down.
    """
    path = method_path.split(".")[1:]  # drop the leading `classification`
    payload: Dict[str, Any] = {"strategy": "split_then_classify"}
    node = payload
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = method_value

    with pytest.raises(Exception, match=path[-1]):
        config_models.ClassificationConfig(**payload)
    assert block.startswith(method_path.rsplit(".", 1)[0] + "."), (
        f"{block} is not under the block {method_path} governs, so that method value "
        "says nothing about whether this subtree is reachable"
    )


@pytest.mark.parametrize(
    "path",
    sorted(p for p in DEFAULTS_OMISSION_EXEMPT_FIELDS if ".image." in p),
)
def test_an_omitted_ocr_only_image_field_has_its_default_where_it_is_read(path: str):
    """The premise for the shared-ImageConfig entries, per field.

    Their reason is not "no default is needed" but "the default belongs on the block
    that reads it". That is checkable: the same field must be shipped under
    `ocr.image` in `base-ocr.yaml`. If OCR ever stops shipping it, the setting has no
    default anywhere and the reason recorded here is false.
    """
    field = path.rsplit(".", 1)[-1]
    ocr = yaml.safe_load(_OCR_DEFAULTS.read_text())
    image = ((ocr or {}).get("ocr") or {}).get("image")
    assert isinstance(image, dict) and field in image, (
        f"{path} is excused because its default lives on ocr.image, but "
        f"{_OCR_DEFAULTS.name} does not ship ocr.image.{field}, so the setting has "
        "no default anywhere"
    )


def _section_classification_prompt_with(key: str, marker: str) -> str:
    """The section classification task prompt composed with ``marker`` in ``key``."""
    from idp_common.classification.service import ClassificationService

    config = merge_config_with_defaults(
        {
            "classification": {
                "strategy": "split_then_classify",
                "split_then_classify": {
                    "splitting": {"llm": {"granularity": "packet"}},
                    "section_classification": {
                        "llm": {"confidence": {"mode": "topk", key: marker}}
                    },
                },
            }
        },
        validate=False,
    )
    with patch("boto3.Session"):
        service = ClassificationService(
            region="us-west-2", config=config, backend="bedrock"
        )
    return service._get_classification_config(service.STAGE_SECTION_CLASSIFICATION)[
        "task_prompt"
    ]


@pytest.mark.parametrize("key", sorted(SECTION_CLASSIFICATION_UNREAD_CONFIDENCE_KEYS))
def test_an_omitted_packet_confidence_block_is_not_read_here(key: str):
    """The premise for the two per-segment confidence blocks, per block.

    Computed by composing the real section classification prompt with a marker in
    the key and checking the marker does not appear. ``test_split_then_classify.py``'s
    ``test_section_classification_uses_the_per_page_shaped_confidence_block``
    asserts the same behaviour from the service's side; this evaluates it as *this*
    exemption's premise, which is what keeps the omission from outliving the reason
    for it.
    """
    marker = f"MARKER-{key}"
    assert marker not in _section_classification_prompt_with(key, marker), (
        f"section classification DID read {key}, so shipping no default for it is a "
        "real omission rather than a deliberate one — give it a default and delete "
        "the exemption"
    )


def test_the_packet_confidence_premise_is_not_passing_by_reading_nothing():
    """Guard the guard: the same probe with the PAGE-shaped key must find the marker.

    Without this control, the test above would pass just as well if prompt
    composition were skipped entirely, or if the marker never reached the config —
    "the key is not read" and "nothing was read" look identical from one assertion.
    """
    marker = "MARKER-page-shape"
    assert marker in _section_classification_prompt_with("task_prompt_topk", marker), (
        "the page-shaped confidence block did not reach the section classification "
        "prompt either, so the premise test above is measuring nothing"
    )


@pytest.mark.parametrize(
    "path", sorted(p for p in DEFAULTS_OMISSION_EXEMPT_SUBTREES if p.endswith("Hook"))
)
def test_an_omitted_collection_has_nothing_to_ship(path: str):
    """The premise for ``postHook``: an empty collection has no value to put in a file.

    If it ever gains a non-empty default, that default is a setting and belongs in
    the canonical list like any other.
    """
    name = path.rsplit(".", 1)[-1]
    default = _field_default(config_models.ClassificationConfig, name)
    assert isinstance(default, (list, dict, set)) and not default, (
        f"{path} no longer defaults to an empty collection ({default!r}), so its "
        "default is a value the defaults file should carry"
    )
