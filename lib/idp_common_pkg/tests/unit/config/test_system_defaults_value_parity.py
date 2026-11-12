# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every value the system defaults ship equals the model's default for that field.

A deployment validates its profile after merging it onto the system defaults
(``merge_utils.load_system_defaults``), so the YAML value is what an unset setting
resolves to. The Pydantic field default is what a setting resolves to without that
merge: a configuration built directly (the SDK, a notebook, a unit test), a sub-block
written without the key, and a value cleared to ``null`` by the editor, which the
numeric validators turn back into the field default. When the two disagree, the same
setting has two answers and which one applies depends on how the configuration was
reached. Two had shipped: every discovery call fell back to ``temperature`` 1.0
against a shipped 0.0, and LLM-based evaluation fell back to a different model and
``top_p``.

``test_classification_defaults_parity.py`` holds this property for classification
alone. This module holds it for every section and both patterns, derived from the
two authorities directly: the merged system defaults, and each model's own default
read from a default instance (so a field validator that derives a value, such as
``extraction.mode``, is read the way a caller sees it). It also reads the same path
through a default ``IDPConfig()``, which is where a parent ``default_factory`` that
passes its own value would show.

Two shapes are excused by computation rather than by listing:

* a setting the two patterns ship with **different** values has no single default
  to agree with; the set is pinned to ``PATTERN_DEPENDENT_PATHS``;
* a **prompt** whose code default is empty or ``None`` carries no second copy of the
  prose on purpose; a copy in Python is the drift this module exists to stop. The
  prompts that do carry a copy are pinned in ``PROMPTS_WITH_A_CODE_COPY`` and never
  take this excuse, so emptying one fails rather than excusing it.

Everything else that differs is listed, one path per entry, in
``DEFAULT_PARITY_EXEMPT`` with its own reason.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Tuple

import pytest
from pydantic import BaseModel

from idp_common.config import models as config_models
from idp_common.config.merge_utils import VALID_PATTERNS, load_system_defaults

pytestmark = pytest.mark.unit


#: The only settings the two patterns may ship with different values. Pinned so the
#: pattern-dependent excuse cannot grow to cover a section setting without notice.
PATTERN_DEPENDENT_PATHS = frozenset({"notes", "use_bda"})

#: Prompts whose code default carries a copy of the shipped text. The prompt excuse
#: is computed from the current code default, so emptying one of these would
#: excuse it; they are pinned as never taking it, because code reading the model
#: (the evaluation comparator, the chat and error-analyzer agents) would then send
#: an empty prompt.
PROMPTS_WITH_A_CODE_COPY = frozenset(
    {
        "agents.error_analyzer.system_prompt",
        "chat.system_prompt",
        "evaluation.llm_method.system_prompt",
        "evaluation.llm_method.task_prompt",
    }
)


#: Paths whose shipped value deliberately differs from the field default. One exact
#: path per entry. Each entry must still differ (``test_an_exempt_path_still_differs``)
#: and still be a shipped leaf, so an entry cannot outlive its reason.
DEFAULT_PARITY_EXEMPT: Dict[str, str] = {
    "extraction.agentic.runtime": (
        "None defers to the EXTRACTION_RUNTIME environment variable and, without "
        "it, selects in-process sharding, the path a notebook or the standalone "
        "library runs, which has no Step Functions state machine to hand shards to. "
        "The deployed stack ships 'step_functions' because it does have one."
    ),
    "extraction.agentic.max_concurrent_batches": (
        "1 is sequential, which is what the in-process runtime above can sustain in "
        "one process without a Bedrock quota it was not configured for. The stack "
        "ships 10 alongside 'step_functions'. Whether the library default should "
        "follow is a product decision this test does not make."
    ),
    "extraction.agentic.table_parsing.enabled": (
        "The parse_table tool is off unless a configuration turns it on; the "
        "shipped profile turns it on. Whether the library default should follow is "
        "a product decision this test does not make."
    ),
    "extraction.validation.escalation_model": (
        "None means no dedicated escalation model: unless a class sets its own "
        "override, escalation becomes a plain retry with the extraction model. A "
        "concrete default would remove the way to ask for that."
    ),
    "extraction.confidence.escalation_model": (
        "None skips the model-escalation round of the self-healing ladder. A "
        "concrete default would remove the way to say that."
    ),
    "extraction.confidence.model": (
        "None leaves the confidence model unset for a configuration built without "
        "the system defaults. Whether a concrete library default is wanted is a "
        "product decision this test does not make."
    ),
    "ocr.model_id": (
        "Read only when ocr.backend is 'bedrock', and the field default backend is "
        "'textract', so the library default never reaches a Bedrock OCR call."
    ),
    "extraction.multi_instance_detection.question": (
        "Prose put to the model. An empty field means 'use the shipped wording', "
        "which the probe holds as DEFAULT_INSTANCE_QUESTION in instance_probe.py "
        "(test_instance_probe.py keeps it equal to the YAML), so the empty field "
        "default already resolves to the shipped text. It is not named *prompt*, "
        "so the computed prompt excuse does not reach it."
    ),
    "rule_validation.fact_extraction": (
        "The whole block is None without the system defaults, and the rule "
        "validation service refuses to start without it, naming the missing block. "
        "A default block would carry empty prompts, so the call would run instead "
        "of refusing. Whether that is wanted is a product decision this test does "
        "not make."
    ),
    "rule_validation.rule_validation_orchestrator": (
        "The whole block is None without the system defaults, and with it None the "
        "orchestrator skips the cross-file summary rather than running it with "
        "empty prompts. Whether a default block is wanted is a product decision "
        "this test does not make."
    ),
    "ocr.features": (
        "An empty list makes Textract OCR call DetectDocumentText; the shipped "
        "TABLES, LAYOUT and SIGNATURES switch it to AnalyzeDocument, a different "
        "and more expensive Textract API. Moving the library default would move "
        "every caller that skips the merge onto it, which is a cost decision this "
        "test does not make."
    ),
    "rule_validation.recommendation_options": (
        "Prose the rule-validation prompts interpolate, held in the YAML like a "
        "prompt and not copied into code; it is not named *prompt*, so the "
        "computed prompt excuse does not reach it."
    ),
}


def _nested_model(field) -> type[BaseModel] | None:
    """The ``BaseModel`` a field holds, through ``Optional`` and ``List``."""
    annotation = field.annotation
    for candidate in [annotation, *getattr(annotation, "__args__", ())]:
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            return candidate
    return None


Leaf = Tuple[str, type, str, Any, Any]


def _leaves(
    model: type[BaseModel], node: Any, root: Any, prefix: str = ""
) -> Iterator[Leaf]:
    """``(path, owning model, field, shipped value, value via IDPConfig())`` per leaf.

    Every shipped key the models declare is either descended into (a mapping under
    a model-typed field) or yielded, including a list of models such as
    ``ocr.features``, which is compared whole after coercion. A key the models do
    not declare is not this module's concern: it is dropped, and
    ``test_unknown_nested_keys.py`` reports it.
    """
    if not isinstance(node, dict):
        return
    for name, value in node.items():
        field = model.model_fields.get(name)
        if field is None:
            continue
        path = f"{prefix}{name}"
        via_root = getattr(root, name, None) if isinstance(root, BaseModel) else None
        nested = _nested_model(field)
        if nested is not None and isinstance(value, dict):
            yield from _leaves(nested, value, via_root, f"{path}.")
            continue
        yield path, model, name, value, via_root


def _absent_blocks(
    model: type[BaseModel], node: Any, root: Any, prefix: str = ""
) -> Iterator[str]:
    """Shipped blocks a default ``IDPConfig()`` holds as ``None``.

    Below such a block there is no instance to read a leaf from, so the leaves are
    compared with their own model's defaults only, and the block itself is reported.
    """
    if not isinstance(node, dict) or not isinstance(root, BaseModel):
        return
    for name, value in node.items():
        field = model.model_fields.get(name)
        nested = _nested_model(field) if field is not None else None
        if nested is None or not isinstance(value, dict):
            continue
        child = getattr(root, name, None)
        if child is None:
            yield f"{prefix}{name}"
        else:
            yield from _absent_blocks(nested, value, child, f"{prefix}{name}.")


def _shipped_leaves(pattern: str) -> List[Leaf]:
    leaves = list(
        _leaves(
            config_models.IDPConfig,
            load_system_defaults(pattern),
            config_models.IDPConfig(),
        )
    )
    # An empty derivation collects as a pass, which would take the whole guarantee
    # with it.
    assert len(leaves) > 50, f"only {len(leaves)} leaves read from {pattern}"
    return leaves


def _coerced(model: type[BaseModel], name: str, shipped: Any) -> Any:
    """The shipped value as the model holds it (``"0.0"`` -> ``0.0``)."""
    return getattr(model(**{name: shipped}), name)


def _field_default(model: type[BaseModel], name: str) -> Any:
    """What ``model`` supplies for ``name`` when the configuration omits it."""
    return getattr(model(), name)


def _pattern_dependent_paths() -> Dict[str, List[Any]]:
    """Paths the patterns ship with different values, and those values.

    A path one pattern ships and the other does not is not pattern-dependent: the
    pattern that ships it is compared on its own.
    """
    per_pattern = {
        pattern: {path: shipped for path, _, _, shipped, _ in _shipped_leaves(pattern)}
        for pattern in VALID_PATTERNS
    }
    shared = set.intersection(*(set(values) for values in per_pattern.values()))
    return {
        path: [per_pattern[p][path] for p in VALID_PATTERNS]
        for path in shared
        if len({repr(per_pattern[p][path]) for p in VALID_PATTERNS}) > 1
    }


def _is_prompt_without_code_copy(model: type[BaseModel], name: str, path: str) -> bool:
    """A prompt field holding prose, with no copy of it in code.

    Read off the annotation as well as the name: ``extraction.forced_tool``'s
    ``fallback_to_prompt`` is a switch, not prose, and a name match alone would let
    a ``None`` there take the excuse.
    """
    annotation = model.model_fields[name].annotation
    holds_text = annotation is str or str in getattr(annotation, "__args__", ())
    return (
        path not in PROMPTS_WITH_A_CODE_COPY
        and "prompt" in name
        and holds_text
        and _field_default(model, name) in ("", None)
    )


def _disagreements() -> Dict[str, str]:
    """Every shipped leaf whose default differs, before the listed exemptions."""
    pattern_dependent = _pattern_dependent_paths()
    found: Dict[str, str] = {}
    for pattern in VALID_PATTERNS:
        absent = list(
            _absent_blocks(
                config_models.IDPConfig,
                load_system_defaults(pattern),
                config_models.IDPConfig(),
            )
        )
        for block in absent:
            found[block] = f"{pattern} ships the block, IDPConfig() holds None"
        for path, model, name, shipped, via_root in _shipped_leaves(pattern):
            under_absent = any(path.startswith(f"{block}.") for block in absent)
            if path in pattern_dependent or _is_prompt_without_code_copy(
                model, name, path
            ):
                continue
            expected = _coerced(model, name, shipped)
            default = _field_default(model, name)
            if default != expected:
                found[path] = (
                    f"{pattern} ships {expected!r}, {model.__name__}.{name} "
                    f"defaults to {default!r}"
                )
            elif not under_absent and via_root != expected:
                found[path] = (
                    f"{pattern} ships {expected!r}, IDPConfig() holds {via_root!r} "
                    "there (a parent default_factory passes its own value)"
                )
    return found


def test_every_shipped_value_is_the_models_default_for_that_field():
    undeclared = {
        path: why
        for path, why in _disagreements().items()
        if path not in DEFAULT_PARITY_EXEMPT
    }
    assert not undeclared, (
        "these settings ship one value and default to another, so a configuration "
        "built without the system-defaults merge, or a cleared field, gets a "
        "different answer from a deployment:\n  "
        + "\n  ".join(f"{path}: {why}" for path, why in sorted(undeclared.items()))
        + "\nAlign the field default with the YAML, or, if the difference is "
        "deliberate, add the path to DEFAULT_PARITY_EXEMPT with its reason."
    )


@pytest.mark.parametrize("path", sorted(DEFAULT_PARITY_EXEMPT))
def test_an_exempt_path_still_differs(path: str):
    """Non-vacuity, per entry: an entry that shields nothing is deleted."""
    assert path in _disagreements(), (
        f"{path} is listed in DEFAULT_PARITY_EXEMPT but its default now equals the "
        "shipped value (or it is no longer shipped); delete the entry."
    )


def test_the_pattern_dependent_settings_are_pinned():
    """Bounds the pattern-dependent excuse.

    The patterns inherit the same section files, so a section setting that differs
    by pattern would be new, and it would take the excuse silently.
    """
    assert set(_pattern_dependent_paths()) == PATTERN_DEPENDENT_PATHS, (
        "the settings the patterns ship differently are no longer exactly "
        f"{sorted(PATTERN_DEPENDENT_PATHS)}; each is excused from the parity check "
        "for that reason alone, so a new one needs reviewing before it is added"
    )


@pytest.mark.parametrize("path", sorted(PROMPTS_WITH_A_CODE_COPY))
def test_a_prompt_with_a_code_copy_keeps_it(path: str):
    """The pinned prompts are shipped, and their code copy is not empty."""
    shipped = {
        leaf[0]: leaf for pattern in VALID_PATTERNS for leaf in _shipped_leaves(pattern)
    }
    assert path in shipped, f"{path} is no longer shipped; delete it from the set"
    _, model, name, _, _ = shipped[path]
    assert _field_default(model, name) not in ("", None), (
        f"{path} lost its code copy, so code reading the model sends an empty prompt"
    )


def test_only_prompt_fields_take_the_prompt_excuse():
    """Bounds the prompt excuse to fields whose name says they are a prompt."""
    excused = sorted(
        path
        for pattern in VALID_PATTERNS
        for path, model, name, _, _ in _shipped_leaves(pattern)
        if _is_prompt_without_code_copy(model, name, path)
    )
    assert excused, "no prompt takes the excuse, so it is dead"
    assert all("prompt" in path.rsplit(".", 1)[-1] for path in excused), excused
