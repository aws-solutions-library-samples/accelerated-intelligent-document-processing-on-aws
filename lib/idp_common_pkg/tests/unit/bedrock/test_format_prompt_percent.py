# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A literal ``%`` in a prompt must survive ``format_prompt`` as a literal ``%``.

``BedrockClient.format_prompt`` substitutes with the ``%`` operator, so before
this suite existed every ``%`` an operator typed was read as a conversion
specifier. "only answer when you are over 90% confident" raised
``TypeError: not enough arguments for format string`` on every document, and a
prompt with no placeholders at all raised ``TypeError: %o format: an integer is
required, not dict``. ``format_prompt`` is shared by classification, extraction,
assessment, summarization, both rule-validation stages and the evaluation
LLM-judge comparator, so the blast radius was every stage that templates a
prompt, and the only way for an operator to find out was a failed run. (Chat is
not among them: ``src/lambda/chat_with_document_processor`` hands its configured
``system_prompt`` to Converse verbatim, templating nothing.)

The escaping has to happen before the ``{KEY} -> %(KEY)s`` rewrite rather than
after it, so these tests pin the two ends that ordering decides: a placeholder
still resolves to its value, and an operator-typed ``%(FOO)s`` does not.
"""

from __future__ import annotations

import os
import re

import pytest
import yaml

import idp_common.config.system_defaults as system_defaults
from idp_common.bedrock.client import BedrockClient

#: ``format_prompt`` reads no instance state, so an unconstructed client is
#: enough and avoids needing AWS credentials or a session.
CLIENT = BedrockClient.__new__(BedrockClient)

#: ``{KEY}`` as the prompts spell it: upper case, digits and underscores.
PLACEHOLDER = re.compile(r"\{([A-Z0-9_]+)\}")

DEFAULTS_DIR = os.path.dirname(os.path.abspath(system_defaults.__file__))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("template", "expected"),
    [
        # A bare % alongside a placeholder. Raised TypeError: not enough
        # arguments for format string.
        ("A={A} 50% off", "A=x 50% off"),
        # A bare % with no placeholder anywhere. Raised TypeError: %o format:
        # an integer is required, not dict -- the "% o" being read as a
        # specifier.
        ("no placeholders at all, 50% off", "no placeholders at all, 50% off"),
        # The workaround operators have had to know about until now. It must
        # keep rendering as one %, i.e. the escaping must not double-escape an
        # already-escaped sequence.
        ("A={A} 50%% off", "A=x 50% off"),
        # % on either side of a placeholder, where the adjacent character is
        # exactly what a conversion specifier would consume.
        ("{A}% and %{A}", "x% and %x"),
        ("95%{A}", "95%x"),
        # A %-conversion the operator typed in the prompt text. It is prompt
        # content, not a substitution the caller offered, so it survives
        # literally rather than raising KeyError for FOO.
        ("type %(FOO)s here", "type %(FOO)s here"),
        ("%(A)s is not the placeholder, {A} is", "%(A)s is not the placeholder, x is"),
        # Other conversion flags an operator might write in prose.
        ("up to 100%s of the page", "up to 100%s of the page"),
        ("5%d increase, {A}", "5%d increase, x"),
        ("% at the very start", "% at the very start"),
        ("trailing %", "trailing %"),
        # An unreferenced {KEY} is left literal, which callers rely on to pass
        # a prompt through more than one substitution pass.
        ("{UNREF} stays, {A} goes, 10% off", "{UNREF} stays, x goes, 10% off"),
    ],
)
def test_a_literal_percent_renders_as_a_literal_percent(template, expected):
    assert CLIENT.format_prompt(template, {"A": "x"}, required_placeholders=[]) == (
        expected
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("%%%", "%%"),
        ("%%%%", "%%"),
        ("50%%% off", "50%% off"),
    ],
)
def test_an_odd_run_of_percents_pairs_left_to_right(template, expected):
    """An odd-length run of ``%`` is paired from the left, and that is deliberate.

    The escaping consumes an existing ``%%`` ahead of a bare ``%``, so ``%%%``
    is read as one escaped pair followed by one bare ``%`` and renders ``%%``
    rather than ``%``. There is no reading of a run like that which is right for
    every author -- it is equally plausibly two literals or one literal and a
    typo -- so the rule is simply stated and pinned here. Before this change
    ``%%%`` raised ``TypeError``, and an output a reader can see beats a failed
    document, but do not "fix" this to collapse the run without deciding what
    the odd ``%`` was meant to be.
    """
    assert CLIENT.format_prompt(template, {}, required_placeholders=[]) == expected


@pytest.mark.unit
def test_a_percent_in_a_substituted_value_is_not_rescanned():
    """The escaping applies to the template, never to the values.

    A value is inserted by the ``%`` operator, which does not re-read what it
    wrote, so a ``%`` arriving from a class description or an OCR page lands
    verbatim. ``config_library/unified/rule-validation/config.yaml`` ships one
    ("Look for values like 'SpO2 88%'"), which reaches a prompt by this route.
    """
    rendered = CLIENT.format_prompt(
        "desc: {DESC}", {"DESC": "SpO2 88% and 90%% too"}, required_placeholders=[]
    )
    assert rendered == "desc: SpO2 88% and 90%% too"


@pytest.mark.unit
def test_required_placeholders_still_validates_the_template_not_the_dict():
    """The check reads the template the caller passed, before any rewriting.

    It asks whether the *template* offers a slot, which is a different question
    from whether the substitutions dict has a key, and the escaping must not
    move it.
    """
    with pytest.raises(ValueError, match=r"\{A\}"):
        CLIENT.format_prompt("no slot here, 50% off", {"A": "x"}, ["A"])

    assert (
        CLIENT.format_prompt("has {A}, 50% off", {"A": "x"}, ["A"]) == "has x, 50% off"
    )


def _shipped_prompts():
    """Every string value under a ``*prompt*`` key in the shipped defaults."""
    found = []
    for name in sorted(os.listdir(DEFAULTS_DIR)):
        if not (name.startswith("base-") and name.endswith(".yaml")):
            continue
        with open(os.path.join(DEFAULTS_DIR, name), encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)

        def walk(node, path):
            if isinstance(node, dict):
                for key, value in node.items():
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{path}[{index}]")
            elif isinstance(node, str) and "prompt" in path.lower():
                found.append((f"{name}:{path}", node))

        walk(loaded, name)
    return found


SHIPPED_PROMPTS = _shipped_prompts()


@pytest.mark.unit
def test_the_shipped_defaults_do_hold_prompts_to_check():
    """Guard the sweep below against finding nothing and passing vacuously."""
    assert len(SHIPPED_PROMPTS) > 10, (
        "no shipped prompts were discovered under the system defaults, so the "
        "regression sweep below would pass without reading anything"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("label", "prompt"),
    SHIPPED_PROMPTS,
    ids=[label for label, _ in SHIPPED_PROMPTS],
)
def test_every_shipped_prompt_renders_byte_identically(label, prompt):
    """No shipped prompt's rendering moves when ``%`` becomes literal.

    The oracle is a plain ``str.replace`` of each ``{KEY}``, which is what both
    the old and the new implementation produce for a prompt carrying no ``%`` --
    and none of them does today, the two ``%`` characters in these files being
    in YAML comments that never reach a template. So an equality failure here
    means the escaping reached something it should not have.
    """
    keys = sorted(set(PLACEHOLDER.findall(prompt)))
    substitutions = {key: f"<{key}>" for key in keys}

    expected = prompt
    for key, value in substitutions.items():
        expected = expected.replace(f"{{{key}}}", value)

    rendered = CLIENT.format_prompt(prompt, substitutions, required_placeholders=[])
    assert rendered == expected, f"{label} no longer renders byte-identically"
