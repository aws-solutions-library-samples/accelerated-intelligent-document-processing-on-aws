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
    template_sections,
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


def _copied(sections, defaults):
    """The requested sections the generator copies: those the defaults have and
    do not leave an empty list (left out, issue #1720)."""
    return [s for s in sections if s in defaults and defaults[s] != []]


@pytest.mark.unit
@pytest.mark.parametrize("pattern,features", MATRIX)
def test_include_prompts_still_writes_every_prompt(pattern, features):
    template = yaml.safe_load(
        generate_config_template(
            features=features, pattern=pattern, include_prompts=True
        )
    )
    defaults = load_system_defaults(pattern)
    included = _copied(FEATURE_SETS[features], defaults)

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
    included = _copied(FEATURE_SETS[features], defaults)

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
# (issue #1691), each with its own reason. ``all`` copies each section's default
# values, and an uploaded file is merged over the stack's preset or the profile's
# current configuration rather than over the system defaults, so a section added
# to ``all`` reverts whatever that configuration changed in it. A section placed in
# neither ``all`` nor here is unaccounted for and fails the closure test below.
# Delete a name here when it is added to ``all``.
PENDING_DECISION = {
    "rule_validation": (
        "on in the system defaults and off in most presets, so adding it would "
        "start running rule validation, an extra model call per document, on those"
    ),
    "chat": (
        "on in the system defaults and off in several presets, so adding it would "
        "switch the chat companion back on for them"
    ),
    "hitl": (
        "off in the system defaults, so adding it would switch human review off "
        "on any profile where an administrator had turned it on"
    ),
}


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
    pending = set(PENDING_DECISION)
    assert pending <= ALL_SECTIONS
    assert not pending & set(FEATURE_SETS["all"])
    assert set(FEATURE_SETS["all"]) | pending == ALL_SECTIONS


@pytest.mark.unit
@pytest.mark.parametrize("pattern", VALID_PATTERNS)
def test_the_all_template_carries_every_placed_section_of_its_pattern(pattern):
    template = yaml.safe_load(generate_config_template(features="all", pattern=pattern))

    defaults = load_system_defaults(pattern)
    placed = SECTIONS_BY_PATTERN[pattern] - set(PENDING_DECISION)

    assert _sections(template) == set(_copied(placed, defaults))


@pytest.mark.unit
@pytest.mark.parametrize("pattern", VALID_PATTERNS)
@pytest.mark.parametrize("features", ["core", "all"])
def test_the_template_changes_nothing_it_is_merged_over(pattern, features):
    """``core`` and ``all`` copy each section's default values, so merged over the
    system defaults they are the system defaults. (Over a stack's preset they are
    not: a section they carry reverts any preset change to its defaults.)"""
    template = yaml.safe_load(
        generate_config_template(features=features, pattern=pattern)
    )

    merged = merge_config_with_defaults(template, pattern)
    baseline = merge_config_with_defaults({"notes": template["notes"]}, pattern)

    assert merged == baseline


def _differences(a, b, path=""):
    """Dotted paths at which two config trees differ."""
    if isinstance(a, dict) and isinstance(b, dict):
        found = set()
        for key in a.keys() | b.keys():
            here = f"{path}.{key}" if path else str(key)
            found |= _differences(a.get(key), b.get(key), here)
        return found
    return set() if a == b else {path}


@pytest.mark.unit
@pytest.mark.parametrize("pattern", VALID_PATTERNS)
def test_the_min_template_turns_off_what_it_leaves_out_and_nothing_else(pattern):
    """``min`` is the one set that disables sections rather than only copying them,
    and those flags are the whole of what it changes over the defaults."""
    defaults = load_system_defaults(pattern)
    template = yaml.safe_load(generate_config_template(features="min", pattern=pattern))

    merged = merge_config_with_defaults(template, pattern)
    baseline = merge_config_with_defaults({"notes": template["notes"]}, pattern)

    # Non-vacuity: the defaults leave both on, so the flags have work to do.
    assert defaults["summarization"]["enabled"] and defaults["evaluation"]["enabled"]
    assert merged["summarization"]["enabled"] is False
    assert merged["evaluation"]["enabled"] is False
    assert _differences(merged, baseline) == {
        "summarization.enabled",
        "evaluation.enabled",
    }


@pytest.mark.unit
@pytest.mark.parametrize("pattern", VALID_PATTERNS)
def test_core_is_min_plus_ocr(pattern):
    """What the two smaller sets copy from the defaults, for the help text's
    description. ``min`` also writes one-key ``enabled: false`` stubs; those are
    covered by the test above and are not copied sections."""

    def emitted(features):
        return _sections(
            yaml.safe_load(generate_config_template(features=features, pattern=pattern))
        )

    copied = {"classification", "extraction"} & SECTIONS_BY_PATTERN[pattern]
    stubs = {"summarization", "evaluation"}
    assert emitted("min") - stubs == copied
    assert emitted("core") == copied | ({"ocr"} & SECTIONS_BY_PATTERN[pattern])


# --- Issue #1709: no dead keys, strict section lists, an accurate header -------


@pytest.mark.unit
@pytest.mark.parametrize("pattern,features", MATRIX)
def test_every_top_level_key_is_a_section_the_pattern_has(pattern, features):
    """A key no defaults file carries is one nothing reads. ``min`` used to write
    ``assessment: {enabled: false}``, a retired section, which looked like it
    switched confidence scoring off and did nothing."""
    template = yaml.safe_load(
        generate_config_template(features=features, pattern=pattern)
    )

    assert set(template) - {"notes"} <= SECTIONS_BY_PATTERN[pattern]
    assert "assessment" not in template


@pytest.mark.unit
@pytest.mark.parametrize("pattern", VALID_PATTERNS)
def test_min_leaves_confidence_scoring_on(pattern):
    """What the docs and the header now say: ``min`` does not disable confidence."""
    template = yaml.safe_load(generate_config_template(features="min", pattern=pattern))
    merged = merge_config_with_defaults(template, pattern)

    assert merged["extraction"]["confidence"]["mode"] not in (None, "off")


@pytest.mark.unit
@pytest.mark.parametrize("pattern", VALID_PATTERNS)
def test_template_sections_is_the_derived_section_set(pattern):
    assert set(template_sections(pattern)) == SECTIONS_BY_PATTERN[pattern]


@pytest.mark.unit
@pytest.mark.parametrize(
    "features",
    [
        ["classification", "extration"],
        ["not-a-section"],
        ["min", "summarization"],
        ["notes"],
        ["use_bda"],
    ],
)
def test_an_unknown_name_in_a_list_is_refused(features):
    with pytest.raises(ValueError, match="Unknown configuration section"):
        generate_config_template(features=features, pattern="pattern-2")


@pytest.mark.unit
def test_a_section_the_pattern_lacks_is_refused_in_a_list():
    """pattern-1 has no ``ocr``. A preset skips it; a list naming it is refused."""
    assert "ocr" not in SECTIONS_BY_PATTERN["pattern-1"]
    with pytest.raises(ValueError, match="'ocr'"):
        generate_config_template(features=["ocr", "extraction"], pattern="pattern-1")
    generate_config_template(features="core", pattern="pattern-1")


@pytest.mark.unit
def test_a_preset_inside_a_list_says_presets_stand_alone():
    with pytest.raises(ValueError, match="cannot be combined"):
        generate_config_template(features=["min", "ocr"], pattern="pattern-2")


@pytest.mark.unit
def test_an_empty_list_is_refused():
    with pytest.raises(ValueError, match="No sections requested"):
        generate_config_template(features=[], pattern="pattern-2")


@pytest.mark.unit
@pytest.mark.parametrize("pattern", VALID_PATTERNS)
def test_every_section_of_a_pattern_can_be_requested_alone(pattern):
    defaults = load_system_defaults(pattern)
    for section in SECTIONS_BY_PATTERN[pattern]:
        # An empty default has nothing to copy, so a request for it alone is
        # refused (test_requesting_only_empty_sections_is_refused, issue #1720).
        if _is_empty(defaults[section]):
            continue
        template = yaml.safe_load(
            generate_config_template(features=[section], pattern=pattern)
        )
        assert set(template) == {"notes", section}


def _header_field(text, prefix):
    """The comma-separated names on the header line starting with ``prefix``."""
    for line in text.splitlines():
        if line.startswith(prefix):
            return set(line[len(prefix) :].rstrip(".").split(", "))
    return set()


HEADER_MATRIX = (
    MATRIX
    + [(p, ["classification", "summarization"]) for p in ["pattern-2"]]
    + [(p, ["extraction"]) for p in VALID_PATTERNS]
)


@pytest.mark.unit
@pytest.mark.parametrize("pattern,features", HEADER_MATRIX)
def test_the_header_describes_the_file_it_heads(pattern, features):
    """Every claim in the header is checked against the YAML below it, never
    against the generator's own bookkeeping. It used to call every template
    "a minimal configuration", including ``all``."""
    text = generate_config_template(features=features, pattern=pattern)
    body = yaml.safe_load(text)
    present = set(body) - {"notes"}
    stubs = {k for k, v in body.items() if v == {"enabled": False}}

    copied = _header_field(text, "# Sections copied from the system defaults: ")
    switched_off = _header_field(text, "# Sections switched off: ")
    omitted = _header_field(text, "# Sections not in this file: ")

    left_empty = _header_field(text, "# Empty in the system defaults, so left out: ")

    assert copied == present - stubs
    assert switched_off == stubs
    assert omitted == SECTIONS_BY_PATTERN[pattern] - present
    defaults = load_system_defaults(pattern)
    requested = FEATURE_SETS[features] if isinstance(features, str) else list(features)
    assert left_empty == {
        s for s in requested if s in defaults and _is_empty(defaults[s])
    }
    explains = "# Written empty, it would replace the profile's own on upload;" in text
    assert explains == bool(left_empty)
    assert "minimal configuration" not in text

    label = features if isinstance(features, str) else ", ".join(features)
    assert f"# Feature set: {label}\n" in text

    mode = ((body.get("extraction") or {}).get("confidence") or {}).get("mode")
    says_on = "# Confidence scoring stays on" in text
    assert says_on == (mode not in (None, "off"))
    if says_on:
        assert f"extraction.confidence.mode: {mode})" in text


@pytest.mark.unit
def test_the_header_matrix_exercises_every_header_line():
    """Non-vacuity: some template switches sections off, some leaves sections
    out, and some carries extraction, so no assertion above is always empty."""
    texts = [generate_config_template(features=f, pattern=p) for p, f in HEADER_MATRIX]
    for prefix in (
        "# Sections switched off: ",
        "# Sections not in this file: ",
        "# Confidence scoring stays on",
        "# Empty in the system defaults, so left out: ",
    ):
        assert any(prefix in t for t in texts), prefix
    assert any("# Sections switched off: " not in t for t in texts)


@pytest.mark.unit
def test_a_repeated_section_is_listed_once():
    text = generate_config_template(features=["ocr", "ocr"], pattern="pattern-2")
    assert "# Sections copied from the system defaults: ocr.\n" in text


@pytest.mark.unit
@pytest.mark.parametrize("pattern", VALID_PATTERNS)
def test_the_header_says_prompts_were_stripped_only_when_they_were(pattern):
    stripped = generate_config_template(features="all", pattern=pattern)
    full = generate_config_template(
        features="all", pattern=pattern, include_prompts=True
    )
    line = "# Long system_prompt, task_prompt and user_prompt values are left out;"

    assert line in stripped
    assert line not in full


# --- Issue #1720: nothing in a template empties what it is uploaded over -----


def _is_empty(value):
    return value == []


def _empty_lists(data, path=""):
    """Dotted paths of every empty list in a config tree, at any depth."""
    if isinstance(data, list):
        if not data:
            return {path}
        found = set()
        for i, item in enumerate(data):
            found |= _empty_lists(item, f"{path}[{i}]")
        return found
    if isinstance(data, dict):
        found = set()
        for key, value in data.items():
            found |= _empty_lists(value, f"{path}.{key}" if path else str(key))
        return found
    return set()


EVERY_REQUEST = [
    (pattern, features, include_prompts)
    for pattern in VALID_PATTERNS
    for features in list(FEATURE_SETS)
    + [[s] for s in template_sections(pattern) if s != "classes"]
    + [["classes", "extraction"]]
    for include_prompts in (False, True)
]


@pytest.mark.unit
def test_the_system_defaults_do_hold_an_empty_list():
    """Non-vacuity for the test below: ``classes`` is empty in every pattern's
    defaults, and every feature set names it, so the generator has one to skip."""
    for pattern in VALID_PATTERNS:
        assert load_system_defaults(pattern)["classes"] == []
    assert all("classes" in names for names in FEATURE_SETS.values())


@pytest.mark.unit
@pytest.mark.parametrize("pattern,features,include_prompts", EVERY_REQUEST)
def test_no_template_carries_an_empty_list(pattern, features, include_prompts):
    """An uploaded file is merged over the profile's configuration, and a list
    there is replaced rather than merged, so an empty list in a template empties
    the profile's list. ``classes: []`` removed every document class of the
    profile it was uploaded over (issue #1720). A list the defaults leave empty
    has nothing to contribute; one nested inside a section would fail here too,
    and needs the same treatment rather than an exemption."""
    template = yaml.safe_load(
        generate_config_template(
            features=features, pattern=pattern, include_prompts=include_prompts
        )
    )

    assert _empty_lists(template) == set()


@pytest.mark.unit
@pytest.mark.parametrize("pattern", VALID_PATTERNS)
def test_requesting_only_empty_sections_is_refused(pattern):
    """``--features classes`` has nothing to copy. A file holding only ``notes``
    would report success and, uploaded, overwrite the profile's notes."""
    with pytest.raises(ValueError, match="Nothing to generate: classes is empty"):
        generate_config_template(features=["classes"], pattern=pattern)
    # With a section that has defaults alongside, it generates as usual.
    template = yaml.safe_load(
        generate_config_template(features=["classes", "extraction"], pattern=pattern)
    )
    assert set(template) == {"notes", "extraction"}
