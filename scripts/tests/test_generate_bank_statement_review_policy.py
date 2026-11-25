# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The bank statement review policy prints the policy rules the presets hold.

``scripts/generate_bank_statement_review_policy.py`` writes
``samples/rule-validation/bank-statement-review-policy.pdf``: the policy document
of the rule validation sample, the one Policy Discovery extracts policy rules
from. The ``rule-validation`` preset holds the same policy as ``policy_classes``,
and the ``review-routing-showcase`` preset copies those. Three copies of one
policy drift apart one edit at a time, and nothing else compares them: a policy
rule reworded in the preset alone would be validated against a policy document
that says something else.

So the policy is read back from the rendered pages, for a fresh run and for the
committed file, and each policy type, policy rule identifier and policy rule text
is compared with the preset's.

``reportlab`` renders and ``pypdfium2`` reads back. Both are declared in
``lib/idp_common_pkg``'s ``test`` extra and imported plainly, so a missing one is
an error and not a skip.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pypdfium2
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
GENERATOR = REPO / "scripts" / "generate_bank_statement_review_policy.py"
COMMITTED = REPO / "samples" / "rule-validation"
POLICY_NAME = "bank-statement-review-policy.pdf"
PRESET = REPO / "config_library" / "unified" / "rule-validation" / "config.yaml"
#: The document the policy is written for, and the names the preset must take
#: for a bank statement.
STATEMENT = REPO / "samples" / "bank-statement-multipage.pdf"
NAME_REGEX = "x-aws-idp-document-name-regex"


@pytest.fixture(scope="module")
def generator():
    spec = importlib.util.spec_from_file_location(GENERATOR.stem, GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generated(generator, tmp_path_factory) -> Path:
    out_dir = tmp_path_factory.mktemp("rule-validation")
    generator.build(out_dir)
    return out_dir


@pytest.fixture(params=["generated", "committed"])
def folder(request, generated) -> Path:
    """A fresh run's output, then the file the repository ships."""
    return generated if request.param == "generated" else COMMITTED


@pytest.fixture(scope="module")
def policy_classes() -> list[dict]:
    return yaml.safe_load(PRESET.read_text(encoding="utf-8"))["policy_classes"]


def _flat(text: str) -> str:
    """One line with single spaces: how a wrapped paragraph compares."""
    return re.sub(r"\s+", " ", text).strip()


def _printed(path: Path) -> str:
    doc = pypdfium2.PdfDocument(str(path))
    try:
        pages = [doc[i].get_textpage().get_text_range() for i in range(len(doc))]
    finally:
        doc.close()
    return _flat(" ".join(pages))


def test_two_runs_write_the_same_file(generator, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    for out_dir in (first, second):
        out_dir.mkdir()
        generator.build(out_dir)

    assert (first / POLICY_NAME).read_bytes() == (second / POLICY_NAME).read_bytes()


def test_a_run_writes_the_policy_and_nothing_else(generated):
    assert [p.name for p in generated.iterdir()] == [POLICY_NAME]


def test_the_repository_ships_the_policy_and_no_other_document():
    """The folder holds the policy document of the sample and nothing else: the
    document that is validated against it is the bank statement."""
    assert [p.name for p in COMMITTED.iterdir()] == [POLICY_NAME]
    assert STATEMENT.is_file()


def test_the_policy_prints_every_policy_rule_under_its_policy_type(generator, folder):
    printed = _printed(folder / POLICY_NAME)

    position = 0
    for s, section in enumerate(generator.SECTIONS, 1):
        heading = f"{s}. {section.heading} ({section.policy_type})"
        assert heading in printed, heading
        # In order: a policy rule is printed after the heading of its own section.
        position = printed.index(heading, position)
        for r, rule in enumerate(section.rules, 1):
            title = f"{generator.rule_number(s, r)} {rule.heading} ({rule.id})"
            assert title in printed, title
            position = printed.index(title, position)
            assert printed.index(_flat(rule.text), position) > position, rule.id


def test_the_preset_holds_the_policy_word_for_word(generator, policy_classes):
    """Policy type by policy type and policy rule by policy rule, in the order the
    policy prints them."""
    assert [p["x-aws-idp-policy-type"] for p in policy_classes] == [
        section.policy_type for section in generator.SECTIONS
    ]
    for policy, section in zip(policy_classes, generator.SECTIONS):
        assert policy["$id"] == section.policy_type
        assert {
            rule_id: _flat(rule["description"])
            for rule_id, rule in policy["rule_properties"].items()
        } == {rule.id: _flat(rule.text) for rule in section.rules}
        assert list(policy["rule_properties"]) == [rule.id for rule in section.rules]


def test_the_shipped_policy_prints_what_the_preset_holds(policy_classes):
    """The committed PDF against the committed preset, with no generator between
    them: an edit to the generator without a new run leaves the two apart."""
    printed = _printed(COMMITTED / POLICY_NAME)

    for policy in policy_classes:
        assert f"({policy['x-aws-idp-policy-type']})" in printed
        for rule_id, rule in policy["rule_properties"].items():
            assert f"({rule_id})" in printed, rule_id
            assert _flat(rule["description"]) in printed, rule_id


def test_every_policy_class_applies_to_the_bank_statement_by_its_name(policy_classes):
    """With several policy classes, only those whose pattern matches are
    evaluated: a class without one would never run on the sample."""
    for policy in policy_classes:
        pattern = policy[NAME_REGEX]
        assert re.search(pattern, STATEMENT.name), policy["x-aws-idp-policy-type"]
        assert not re.search(pattern, "lending_package.pdf")


def test_an_identifier_used_twice_is_refused(generator, tmp_path, monkeypatch):
    first = generator.SECTIONS[0]
    twice = first._replace(rules=(first.rules[0], first.rules[0]))
    monkeypatch.setattr(generator, "SECTIONS", (twice, *generator.SECTIONS[1:]))

    with pytest.raises(ValueError, match="uses a rule id twice"):
        generator.build(tmp_path)

    monkeypatch.setattr(generator, "SECTIONS", (first, first))
    with pytest.raises(ValueError, match="a policy type is used twice"):
        generator.build(tmp_path)


def test_the_command_writes_the_policy_into_the_directory_it_is_given(
    generator, tmp_path, monkeypatch, capsys
):
    out_dir = tmp_path / "out" / "policy"
    monkeypatch.setattr(
        "sys.argv",
        ["generate_bank_statement_review_policy.py", "--out-dir", str(out_dir)],
    )

    generator.main()

    assert (out_dir / POLICY_NAME).is_file()
    rules = sum(len(section.rules) for section in generator.SECTIONS)
    assert capsys.readouterr().out.strip() == (
        f"Wrote {out_dir / POLICY_NAME}: {len(generator.SECTIONS)} policy types, "
        f"{rules} policy rules"
    )
