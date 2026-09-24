# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every system-defaults file resolves, and ``pattern-1``'s set is derived, not observed.

``pattern-1.yaml`` kept ``base-assessment.yaml`` in its ``_inherits`` after the v0.6
confidence/geometry split (commit ``e3d23471``) deleted that module, so
``load_system_defaults("pattern-1")`` raised ``FileNotFoundError`` and with it every
caller that resolves defaults for the BDA pattern —
[#1203](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1203).
``base.yaml``, which ``pattern-2.yaml`` inherits, had the substitution applied;
``pattern-1.yaml``'s direct list did not, and nothing asked it.

The asymmetry is the defect, so the checks here are written to be asymmetry-proof, in
four layers from weakest to strongest:

1. **The class fix.** Every ``_inherits`` entry anywhere in the directory names a file
   that exists, and every entry in ``VALID_PATTERNS`` loads through the public
   function. The next module deletion fails here rather than at a call site.
2. **The key set is derived from the refactor.** ``pattern-1``'s resolved top-level
   keys must equal the set it resolved to *before* the split with the split's
   documented relocation applied. An inheritance list that merely loads does not pass
   this; neither does one that pulls in a module ``pattern-1`` should not have.
3. **Every pre-split key is accounted for.** The relocation table below is closed over
   the ``assessment`` block as it stood at ``e3d23471^``: each key either names a v0.6
   home that must be populated in ``pattern-1``'s resolved defaults, or is recorded as
   deliberately dropped with the reason.
4. **The relocated sections are value-identical to ``pattern-2``'s.** Both patterns
   draw the same unmodified module files and neither overrides them, so any future
   edit that changes what a BDA deployment gets — to either module, or to either
   pattern's ``_inherits`` — fails here.

The literals in layers 2 and 3 were measured once by resolving the pre-split tree
(``git archive e3d23471^`` of this directory, resolved with the same loader) and are
recorded as literals on purpose: asking the files as they stand today cannot tell a
correct inheritance list from a plausible one.
"""

from __future__ import annotations

import pathlib
from typing import Any, Dict, Optional

import pytest
import yaml

from idp_common.config.merge_utils import VALID_PATTERNS, load_system_defaults

pytestmark = pytest.mark.unit

_DEFAULTS = (
    pathlib.Path(__file__).resolve().parents[5]
    / "lib"
    / "idp_common_pkg"
    / "idp_common"
    / "config"
    / "system_defaults"
)

# ---------------------------------------------------------------------------
# What the v0.6 split did, recorded from the refactor rather than from the tree
# ---------------------------------------------------------------------------

#: ``pattern-1``'s resolved top-level keys immediately before the v0.6 split.
PRE_V06_PATTERN_1_TOP_LEVEL_KEYS = frozenset(
    {
        "use_bda",
        "notes",
        "classes",
        "assessment",
        "summarization",
        "evaluation",
        "agents",
        "discovery",
    }
)

#: The keys the pre-split ``assessment`` block carried. Identical for both patterns,
#: because both resolved it from the one ``base-assessment.yaml``.
PRE_V06_ASSESSMENT_KEYS = frozenset(
    {
        "default_confidence_threshold",
        "enabled",
        "granular",
        "ground_geometry_in_ocr",
        "hitl_enabled",
        "image",
        "max_tokens",
        "model",
        "system_prompt",
        "task_prompt",
        "temperature",
        "top_k",
        "top_p",
        "validation_enabled",
    }
)

#: Where each pre-split ``assessment`` key went, per the split's own migration
#: (``idp_common/config/migrations/v05_to_v06.py``). ``None`` means the refactor
#: dropped it rather than moving it — for every pattern, not just this one.
V06_RELOCATION = {
    "model": "extraction.confidence.model",
    "system_prompt": "extraction.confidence.system_prompt",
    "task_prompt": "extraction.confidence.task_prompt",
    "temperature": "extraction.confidence.temperature",
    "top_p": "extraction.confidence.top_p",
    "top_k": "extraction.confidence.top_k",
    "image": "extraction.confidence.image",
    "ground_geometry_in_ocr": "extraction.geometry.mode",
    "hitl_enabled": "hitl.enabled",
    "default_confidence_threshold": "hitl.confidence_threshold",
    # `enabled` is now derived rather than stored: `confidence.enabled` is
    # `mode != "off"`, so `mode` is its home and the derivation is asserted below.
    "enabled": "extraction.confidence.mode",
    # Dropped by the refactor, each for a stated reason:
    #   max_tokens        - the confidence pass always requests the model maximum.
    #   validation_enabled- vestigial; removed with the dead validation branch.
    #   granular          - sub-block no longer carried in the defaults (the runtime
    #                       reads model-side defaults); its one surfaced knob became
    #                       `extraction.confidence.list_batch_size`, asserted below.
    "max_tokens": None,
    "validation_enabled": None,
    "granular": None,
}

#: The two sections the split introduced, plus the surviving list-batch knob, all of
#: which a BDA deployment needs present in its defaults.
REQUIRED_PATTERN_1_PATHS = (
    "extraction.confidence.mode",
    "extraction.confidence.model",
    "extraction.confidence.system_prompt",
    "extraction.confidence.task_prompt",
    "extraction.confidence.list_batch_size",
    "extraction.geometry.mode",
    "hitl.enabled",
    "hitl.confidence_threshold",
)

#: What `extraction` may contain for `pattern-1`. BDA does its own extraction, so the
#: LLM extraction settings are deliberately absent; confidence and geometry are
#: present because v0.6 moved them under this key.
PATTERN_1_EXTRACTION_SUBSECTIONS = frozenset({"confidence", "geometry"})

#: Sections `pattern-1` must not acquire — BDA performs these stages internally.
PATTERN_1_FORBIDDEN_TOP_LEVEL_KEYS = frozenset({"ocr", "classification"})


def _lookup(config: Dict[str, Any], dotted: str) -> Optional[Any]:
    """Resolve a dotted path, returning ``None`` if any segment is absent."""
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _declared_inherits(path: pathlib.Path) -> list[str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    inherits = raw.get("_inherits")
    if inherits is None:
        return []
    return [inherits] if isinstance(inherits, str) else list(inherits)


# ---------------------------------------------------------------------------
# 1. The class fix
# ---------------------------------------------------------------------------


def test_every_inherited_file_named_in_the_directory_exists():
    """The check that was missing: a module deleted out from under an ``_inherits``.

    Read from the YAML rather than through the loader, so a file that is unreachable
    from any pattern is still covered — an inheritance chain nothing currently walks
    is exactly where this rots unnoticed.
    """
    files = sorted(_DEFAULTS.glob("*.yaml"))
    assert files, f"no defaults files found under {_DEFAULTS}"

    dangling = [
        f"{path.name} inherits {name}"
        for path in files
        for name in _declared_inherits(path)
        if not (_DEFAULTS / name).exists()
    ]
    assert not dangling, (
        "these _inherits entries name files that do not exist, so resolving the "
        f"pattern raises FileNotFoundError: {dangling}"
    )


@pytest.mark.parametrize("pattern", VALID_PATTERNS)
def test_every_valid_pattern_loads(pattern: str):
    """``VALID_PATTERNS`` is the accepted argument list, so all of it has to resolve.

    Testing ``pattern-2`` alone is what let the ``pattern-1`` break ship: it is an
    accepted value of every ``pattern=`` parameter in this package and in the SDK.
    """
    resolved = load_system_defaults(pattern)
    assert resolved, f"{pattern} resolved to an empty configuration"
    assert "_inherits" not in resolved, "the directive leaked into the resolved config"


# ---------------------------------------------------------------------------
# 2. The key set, derived from the refactor
# ---------------------------------------------------------------------------


def test_pattern_1_resolves_to_the_key_set_the_v06_split_implies():
    """The set is computed from the pre-split set, not read off today's files.

    Loading without raising is weak evidence — an inheritance list can resolve
    cleanly and still give a BDA deployment the wrong defaults (too few modules, or
    the LLM extraction settings BDA does not use). Pinning the derived set is what
    distinguishes the repair from a list that merely parses.
    """
    expected = (PRE_V06_PATTERN_1_TOP_LEVEL_KEYS - {"assessment"}) | {
        "extraction",
        "hitl",
    }
    assert set(load_system_defaults("pattern-1")) == expected


def test_pattern_1_takes_only_the_confidence_and_geometry_parts_of_extraction():
    """``extraction`` appears for `pattern-1` only as the new home of two sections.

    This is the assertion that would fail if the break were "fixed" by inheriting
    ``base-extraction.yaml``: that resolves, and it hands a BDA deployment an LLM
    extraction model and prompts it never runs.
    """
    resolved = load_system_defaults("pattern-1")
    assert set(resolved["extraction"]) == PATTERN_1_EXTRACTION_SUBSECTIONS
    assert not (set(resolved) & PATTERN_1_FORBIDDEN_TOP_LEVEL_KEYS), (
        "BDA performs OCR and classification internally; those sections must not be "
        "inherited"
    )


# ---------------------------------------------------------------------------
# 3. Every pre-split key is accounted for
# ---------------------------------------------------------------------------


def test_the_relocation_table_is_closed_over_the_pre_split_block():
    """A key with no entry either way is a key nobody checked the fate of."""
    assert set(V06_RELOCATION) == set(PRE_V06_ASSESSMENT_KEYS)


@pytest.mark.parametrize(
    "legacy_key,home",
    sorted((k, v) for k, v in V06_RELOCATION.items() if v is not None),
)
def test_each_relocated_key_has_a_populated_home_in_pattern_1(
    legacy_key: str, home: str
):
    """What ``pattern-1`` carried before the split, it carries after it."""
    value = _lookup(load_system_defaults("pattern-1"), home)
    assert value is not None, (
        f"{legacy_key} moved to {home} in v0.6, but pattern-1's defaults have no "
        f"value there"
    )


@pytest.mark.parametrize("path", REQUIRED_PATTERN_1_PATHS)
def test_the_keys_a_bda_deployment_needs_are_present(path: str):
    """The confidence pass and the review thresholds are what BDA mode still uses."""
    assert _lookup(load_system_defaults("pattern-1"), path) is not None


def test_the_confidence_pass_is_still_on_and_geometry_still_ocr_derived():
    """The semantics of the three dropped-or-derived keys, through the real models.

    Pre-split, ``pattern-1`` carried ``assessment.enabled: true``,
    ``ground_geometry_in_ocr: true`` and ``hitl_enabled: false``. Their v0.6
    equivalents are a derived ``enabled``, a geometry *mode*, and the top-level HITL
    toggle — so asserting the raw YAML would miss the derivation. This goes through
    ``IDPConfig``, which is what a deployment reads.
    """
    from idp_common.config.models import IDPConfig

    config = IDPConfig(**load_system_defaults("pattern-1"))

    assert config.extraction.confidence.mode != "off"
    assert config.extraction.confidence.enabled is True, (
        "pre-split assessment.enabled was true; v0.6 derives it from mode != off"
    )
    assert config.extraction.geometry.mode == "ocr_only", (
        "pre-split ground_geometry_in_ocr was true, whose v0.6 spelling is ocr_only"
    )
    assert config.hitl.enabled is False, "pre-split hitl_enabled was false"


# ---------------------------------------------------------------------------
# 4. Equivalence with pattern-2 on the relocated sections
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "section", ["extraction.confidence", "extraction.geometry", "hitl"]
)
def test_the_relocated_sections_match_pattern_2_exactly(section: str):
    """Both patterns inherit these modules unmodified, so the values must be equal.

    This is the check that survives edits neither pattern file makes: add a key to
    ``base-confidence.yaml`` and drop ``base-confidence.yaml`` from one pattern's
    ``_inherits``, and only this fails. Equality is the right relation because
    neither ``pattern-1.yaml`` nor ``pattern-2.yaml`` overrides anything in these
    three sections — if one ever needs to, this assertion is the place to record it.
    """
    one = _lookup(load_system_defaults("pattern-1"), section)
    two = _lookup(load_system_defaults("pattern-2"), section)
    assert one is not None and two is not None, f"{section} missing from a pattern"
    assert one == two, (
        f"{section} differs between the patterns, so a BDA deployment and a pipeline "
        f"deployment get different confidence/geometry/HITL defaults"
    )
