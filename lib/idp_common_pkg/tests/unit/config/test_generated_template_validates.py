# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
A template from ``generate_config_template`` must pass ``validate_config``.

The generator is what ``idp-cli config-create`` and ``IDPClient().config.create``
emit, and ``config-validate`` / ``config.upload`` are the next commands a user runs
on its output. By default the generator leaves long prompts out of the file for
readability; it used to write a placeholder string in their place, which the
validator and the runtime read as the prompt itself (issue #1673).

The matrix is derived from the generator's own registries rather than listed, so a
new pattern or feature set is covered without editing this file.
"""

import pytest
import yaml

from idp_common.config.merge_utils import (
    FEATURE_SETS,
    VALID_PATTERNS,
    generate_config_template,
    load_system_defaults,
    merge_config_with_defaults,
    validate_config,
)

PROMPT_KEYS = ("system_prompt", "task_prompt", "user_prompt")

MATRIX = [(p, f) for p in VALID_PATTERNS for f in FEATURE_SETS]


def _prompts(data, path=""):
    """Every prompt field in a config tree, as {dotted path: value}."""
    found = {}
    if isinstance(data, dict):
        for key, value in data.items():
            here = f"{path}.{key}" if path else str(key)
            if key in PROMPT_KEYS and isinstance(value, str):
                found[here] = value
            else:
                found.update(_prompts(value, here))
    elif isinstance(data, list):
        for i, item in enumerate(data):
            found.update(_prompts(item, f"{path}[{i}]"))
    return found


@pytest.mark.unit
def test_the_matrix_is_not_empty():
    # An empty derived parametrisation collects as a skip, not a failure.
    assert VALID_PATTERNS and FEATURE_SETS
    assert len(MATRIX) == len(VALID_PATTERNS) * len(FEATURE_SETS)


@pytest.mark.unit
@pytest.mark.parametrize("pattern,features", MATRIX)
def test_generated_template_passes_validation(pattern, features):
    template = yaml.safe_load(
        generate_config_template(features=features, pattern=pattern)
    )

    result = validate_config(template, pattern=pattern)

    assert result["valid"], result["errors"]
    assert result["errors"] == []


@pytest.mark.unit
@pytest.mark.parametrize("pattern,features", MATRIX)
def test_a_stripped_prompt_resolves_to_the_system_default(pattern, features):
    """Every prompt in the merged result is the default one, character for character.

    Stronger than validity: a placeholder in a field no validator checks (a
    ``system_prompt``) would pass the test above and still replace the real prompt
    at runtime.
    """
    template = yaml.safe_load(
        generate_config_template(features=features, pattern=pattern)
    )
    defaults = load_system_defaults(pattern)
    merged = merge_config_with_defaults(template, pattern)

    included = [s for s in FEATURE_SETS[features] if s in defaults]
    expected = {
        path: value
        for path, value in _prompts(defaults).items()
        if path.split(".")[0].split("[")[0] in included
    }
    actual = {path: _prompts(merged).get(path) for path in expected}

    # Non-vacuity: the sections in every feature set carry prompts.
    assert expected
    assert actual == expected


@pytest.mark.unit
@pytest.mark.parametrize("pattern,features", MATRIX)
def test_include_prompts_still_writes_every_prompt(pattern, features):
    template = yaml.safe_load(
        generate_config_template(
            features=features, pattern=pattern, include_prompts=True
        )
    )
    defaults = load_system_defaults(pattern)
    included = [s for s in FEATURE_SETS[features] if s in defaults]

    for section in included:
        assert _prompts(template[section]) == _prompts(defaults[section])


@pytest.mark.unit
@pytest.mark.parametrize("pattern,features", MATRIX)
def test_the_default_template_leaves_long_prompts_out(pattern, features):
    """The other half of the contract: stripping is what ``include_prompts`` toggles.

    Without this the tests above pass for a generator that writes every prompt in
    full, which validates and merges correctly but is not what the default promises.
    """
    template = yaml.safe_load(
        generate_config_template(features=features, pattern=pattern)
    )
    defaults = load_system_defaults(pattern)
    included = [s for s in FEATURE_SETS[features] if s in defaults]

    # Non-vacuity: the defaults do carry prompts long enough to strip.
    assert any(len(v) > 50 for s in included for v in _prompts(defaults[s]).values())
    for section in included:
        assert all(len(v) <= 50 for v in _prompts(template[section]).values())
        # Stripping removes long prompts and nothing else.
        assert template[section] == _without_long_prompts(defaults[section])


def _without_long_prompts(data):
    if isinstance(data, dict):
        return {
            k: _without_long_prompts(v)
            for k, v in data.items()
            if not (k in PROMPT_KEYS and isinstance(v, str) and len(v) > 50)
        }
    if isinstance(data, list):
        return [_without_long_prompts(item) for item in data]
    return data


# --- Which sections each feature set names (issue #1691) ---------------------


def _sections(defaults):
    """The top-level sections of a defaults tree: every key holding a mapping or a
    list. ``notes`` and ``use_bda`` are scalars and are not sections."""
    return {k for k, v in defaults.items() if isinstance(v, (dict, list))}


SECTIONS_BY_PATTERN = {p: _sections(load_system_defaults(p)) for p in VALID_PATTERNS}
ALL_SECTIONS = set().union(*SECTIONS_BY_PATTERN.values())


@pytest.mark.unit
def test_the_derived_section_universe_is_not_empty():
    # An empty universe would make the two tests below compare empty sets.
    assert all(SECTIONS_BY_PATTERN.values())
    assert {"classes", "extraction"} <= ALL_SECTIONS


# Sections the defaults carry that ``all`` leaves out, pending a product decision
# (issue #1691). ``all`` copies each section's default values, and an uploaded file
# is merged over the stack's preset rather than over the system defaults, so adding
# one of these would switch it back to its system default on every preset that
# changes it: ``rule_validation`` and ``chat`` are on in the system defaults and off
# in most presets. Delete a name here when it is added to ``all``.
AWAITING_DECISION = {"chat", "hitl", "rule_validation"}


@pytest.mark.unit
@pytest.mark.parametrize("features", list(FEATURE_SETS))
def test_every_name_in_a_feature_set_is_a_real_section(features):
    """A name no defaults file carries is dropped without a word, so a set naming
    one silently emits less than it says (``criteria_validation``, ``assessment``)."""
    assert set(FEATURE_SETS[features]) - ALL_SECTIONS == set()
    assert len(FEATURE_SETS[features]) == len(set(FEATURE_SETS[features]))


@pytest.mark.unit
def test_all_accounts_for_every_section_the_defaults_carry():
    """``all`` plus the sections awaiting a decision is exactly the derived set, so
    a section added to the defaults fails here until someone places it, and a
    pending name that ``all`` gains, or the defaults lose, fails too."""
    assert AWAITING_DECISION <= ALL_SECTIONS
    assert not AWAITING_DECISION & set(FEATURE_SETS["all"])
    assert set(FEATURE_SETS["all"]) | AWAITING_DECISION == ALL_SECTIONS


@pytest.mark.unit
@pytest.mark.parametrize("pattern", VALID_PATTERNS)
def test_the_all_template_carries_every_placed_section_of_its_pattern(pattern):
    template = yaml.safe_load(generate_config_template(features="all", pattern=pattern))

    assert _sections(template) == SECTIONS_BY_PATTERN[pattern] - AWAITING_DECISION


@pytest.mark.unit
@pytest.mark.parametrize("pattern", VALID_PATTERNS)
def test_the_all_template_changes_nothing_it_is_merged_over(pattern):
    """``all`` copies each section's default values, so merged over the system
    defaults it is the system defaults. (Over a stack's preset it is not: a section
    it carries reverts any preset change to that section's defaults.)"""
    template = yaml.safe_load(generate_config_template(features="all", pattern=pattern))

    merged = merge_config_with_defaults(template, pattern)
    baseline = merge_config_with_defaults({"notes": template["notes"]}, pattern)

    assert merged == baseline


@pytest.mark.unit
@pytest.mark.parametrize("pattern", VALID_PATTERNS)
def test_the_min_template_turns_off_what_it_leaves_out(pattern):
    """``min`` is the one set that disables sections rather than only copying them,
    and those flags are the whole of what it changes over the defaults."""
    defaults = load_system_defaults(pattern)
    template = yaml.safe_load(generate_config_template(features="min", pattern=pattern))

    merged = merge_config_with_defaults(template, pattern)

    # Non-vacuity: the defaults leave both on, so the flags have work to do.
    assert defaults["summarization"]["enabled"] and defaults["evaluation"]["enabled"]
    assert merged["summarization"]["enabled"] is False
    assert merged["evaluation"]["enabled"] is False
