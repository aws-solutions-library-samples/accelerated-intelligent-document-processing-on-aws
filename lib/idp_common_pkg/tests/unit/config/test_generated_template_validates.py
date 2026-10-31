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
