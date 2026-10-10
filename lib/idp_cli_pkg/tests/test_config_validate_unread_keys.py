# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``config-validate`` reports unread configuration keys, and reports them once.

This command used to compute its own top-level extras as
``set(config) - set(IDPConfig.model_fields)``. That set difference knows nothing about
the configuration tree, so it made two statements that were false: it said
``description`` would be ignored, when ``update_configuration`` pops and stores it,
and it said ``rule_classes`` would be ignored, when the loader renames it to
``policy_classes`` and honours it — telling an operator to delete live policy rules.
It also saw only the top level, so the misspelled or mis-nested key this whole change
is about
([#1134](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1134))
went unmentioned, and the keys it did report arrived twice once `validate_config`
began reporting them too.

The command now prints `validate_config`'s findings and nothing of its own.
"""

from __future__ import annotations

import logging

import pytest
import yaml
from click.testing import CliRunner

from idp_cli.cli import cli

pytestmark = pytest.mark.unit


def _validate(tmp_path, config: dict, *extra_args: str):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    logging.disable(logging.CRITICAL)
    try:
        return CliRunner().invoke(
            cli, ["config-validate", "--config-file", str(path), *extra_args]
        )
    finally:
        logging.disable(logging.NOTSET)


def test_a_nested_typo_is_reported_with_the_field_it_was_meant_to_be(tmp_path):
    result = _validate(
        tmp_path,
        {
            "classes": [{"name": "invoice"}],
            "extraction": {"validation": {"enabld": False}},
            "ocr": {"dpi": 300},
        },
    )
    assert result.exit_code == 0, result.output
    # Rich wraps the output, so compare on a single line.
    flat = " ".join(result.output.split())
    assert "extraction.validation.enabld" in flat
    assert "Did you mean 'extraction.validation.enabled'?" in flat
    assert "ocr.dpi" in flat
    assert "Did you mean 'ocr.image.dpi'?" in flat


@pytest.mark.parametrize(
    "key,value",
    [("description", "a profile description"), ("rule_classes", [{"name": "policyA"}])],
)
def test_a_key_another_consumer_reads_or_the_loader_renames_is_not_reported(
    tmp_path, key, value
):
    result = _validate(tmp_path, {"classes": [{"name": "invoice"}], key: value})
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    # Quoted, because the prose of an unrelated warning legitimately contains the
    # word "descriptions" — a bare substring check passes on that coincidence and
    # would keep passing if the key were named again.
    assert f"'{key}'" not in flat, flat
    assert "will be ignored" not in flat, flat


def test_a_top_level_typo_is_reported_exactly_once(tmp_path):
    result = _validate(tmp_path, {"classes": [{"name": "invoice"}], "notes_typo": "x"})
    flat = " ".join(result.output.split())
    assert flat.count("notes_typo") == 1, flat


@pytest.mark.parametrize(
    "config,expected_exit",
    [
        ({"notes_typo": "x"}, 1),
        ({"criteria_bucket": "b"}, 1),
        # Not extra at all: one is read by another consumer, one is renamed on load.
        ({"description": "d"}, 0),
        ({"rule_classes": [{"name": "policyA"}]}, 0),
        # Nested, and deliberately still passing --strict: see the docstring below.
        ({"extraction": {"validation": {"enabld": False}}}, 0),
    ],
    ids=["typo", "deprecated", "read-elsewhere", "renamed", "nested"],
)
def test_strict_keeps_its_contract_of_top_level_fields_only(
    tmp_path, config, expected_exit
):
    """``--strict`` fails on a top-level extra, and deliberately not on a nested one.

    Extending it downwards would fail configurations that pass today, in a flag whose
    whole purpose is to be used in a pipeline — a release decision rather than part of
    fixing the silence. The nested finding is still *reported* either way. What did
    change is that the two keys above which are not extra at all no longer fail it.
    """
    result = _validate(
        tmp_path, {"classes": [{"name": "invoice"}], **config}, "--strict"
    )
    assert result.exit_code == expected_exit, result.output
    if expected_exit == 1:
        assert "Strict mode" in result.output


def test_the_findings_are_printed_when_validation_fails(tmp_path):
    """The failing branch reports them too, and it is the branch that needs them most.

    A key at the wrong depth is accepted in silence while its correctly-nested sibling
    raises — ``ocr.dpi: "abc"`` validates and ``ocr.image.dpi: "abc"`` does not — so
    the finding is usually *the explanation* for the error printed beside it rather
    than a separate observation. Printing the findings only on the passing branch
    withheld them from exactly the reader who was already looking at an error and
    needed to know which key the models would not read.

    Two halves, and both are asserted: ``validate_config`` has to compute the findings
    before it gives up (they are a question about the submitted document, not about
    the merge or the Pydantic pass), and this command has to print them on the branch
    it takes when ``valid`` is false.
    """
    result = _validate(
        tmp_path,
        {
            "classes": [{"name": "invoice"}],
            "extracton": {"model": "x"},
            "extraction": {"validation": {"enabld": False}},
            "ocr": {"image": {"dpi": "not-a-number"}},
        },
    )
    assert result.exit_code == 1, result.output
    flat = " ".join(result.output.split())
    # The failure really is reported, so this is the failing branch and not a pass
    # that happens to print warnings.
    assert "Validation failed" in flat
    assert "ocr.image.dpi" in flat
    # ...and both findings arrive with it, at both depths.
    assert "extracton" in flat
    assert "Did you mean 'extraction'?" in flat
    assert "extraction.validation.enabld" in flat
