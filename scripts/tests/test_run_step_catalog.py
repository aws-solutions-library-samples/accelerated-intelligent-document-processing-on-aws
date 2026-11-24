# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The pipeline steps are declared once, and every surface offers the same ones.

``idp_common.run_plan.steps`` is the declaration. Two surfaces cannot import it
(the web UI and the MCP gateway manager), so ``scripts/generate_run_step_catalog.py``
writes the list into each as JSON. This module holds both halves:

* the committed JSON copies are what the generator produces from the step list;
* the SDK's ``RerunStep`` values, the CLI's ``--step`` and ``--skip`` choices,
  the MCP ``reprocess`` tool's schema and handler, and the web UI's step picker
  each offer that list, no more and no less.

A surface that falls behind fails here with a message that names it.

The same generator writes the web UI's routing input catalogue
(``routing-inputs.json``) from ``ROUTING_INPUTS``, and the last section here
holds that file to the routing module.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import shutil
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GENERATOR = REPO / "scripts" / "generate_run_step_catalog.py"
SDK_MODELS = REPO / "lib" / "idp_sdk" / "idp_sdk" / "models" / "base.py"
GATEWAY_DIR = REPO / "src" / "lambda" / "agentcore_gateway_manager"
STEP_PICKER = (
    REPO / "src" / "ui" / "src" / "components" / "run-plan" / "StartStepPicker.tsx"
)
CLI_COMMANDS = ("reprocess", "rerun-inference")


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generator = _load("_generate_run_step_catalog", GENERATOR)
STEPS = generator.load_steps(REPO)
CATALOG = generator.catalog(STEPS)
START_VALUES = generator.start_values(CATALOG)
OPTIONAL_STEPS = generator.optional_step_ids(CATALOG)


# ----------------------------------------------------------------- the generator


@pytest.mark.parametrize("target", generator.TARGETS, ids=str)
def test_each_committed_copy_is_what_the_generator_writes(target):
    path = REPO / target
    assert path.exists(), (
        f"{target} is missing; run python3 scripts/generate_run_step_catalog.py"
    )
    assert path.read_text() == generator.render(CATALOG), (
        f"{target} is stale; run python3 scripts/generate_run_step_catalog.py"
    )


def test_the_copies_are_byte_identical():
    texts = {(REPO / target).read_text() for target in generator.TARGETS}
    assert len(texts) == 1


def test_the_catalogue_lists_the_steps_in_pipeline_order():
    assert [entry["id"] for entry in CATALOG["steps"]] == list(STEPS.step_ids())


def test_the_catalogue_says_which_steps_can_be_skipped():
    assert OPTIONAL_STEPS == list(STEPS.optional_step_ids())
    assert OPTIONAL_STEPS, "no optional step: the skip choices would be empty"


@pytest.mark.parametrize("mode", ("pipeline", "bda"))
def test_the_catalogue_says_which_steps_a_mode_has(mode):
    listed = [entry["id"] for entry in CATALOG["steps"] if mode in entry["modes"]]
    assert listed == [run_step.id for run_step in STEPS.steps_for_mode(mode)]
    for entry in CATALOG["steps"]:
        if mode in entry["modes"]:
            assert entry["states"][mode], (entry["id"], mode)


def test_every_entry_has_a_label_and_the_fields_the_surfaces_read():
    for entry in CATALOG["steps"]:
        assert set(entry) == {"id", "label", "required", "modes", "states"}
        assert entry["label"].strip()
        assert isinstance(entry["required"], bool)


def test_the_catalogue_lists_the_display_steps_of_the_step_module():
    assert CATALOG["display_steps"] == [
        {
            "id": display.id,
            "label": display.label,
            "before": display.before,
            "review_completed_only": display.review_completed_only,
        }
        for display in STEPS.DISPLAY_STEPS
    ]
    assert [entry["id"] for entry in CATALOG["display_steps"]] == [
        "apply_review_changes",
        "review_routing",
    ]


def test_a_display_step_is_not_a_step_a_run_starts_at_or_skips():
    """A surface that offers the steps reads ``steps``; the display steps are a
    list of their own, so none of them becomes a start or a skip choice."""
    display_ids = {entry["id"] for entry in CATALOG["display_steps"]}
    assert display_ids
    assert not display_ids & set(START_VALUES)
    assert not display_ids & set(OPTIONAL_STEPS)


@pytest.mark.parametrize("mode", ("pipeline", "bda"))
def test_a_display_step_comes_before_a_step_of_both_modes(mode):
    of_mode = {entry["id"] for entry in CATALOG["steps"] if mode in entry["modes"]}
    for entry in CATALOG["display_steps"]:
        assert set(entry) == {"id", "label", "before", "review_completed_only"}
        assert entry["label"].strip()
        assert isinstance(entry["review_completed_only"], bool)
        assert entry["before"] in of_mode, (entry["id"], mode)


def test_start_values_are_the_ids_the_aliases_and_resume():
    assert START_VALUES == list(STEPS.start_values())
    assert len(set(START_VALUES)) == len(START_VALUES)


def test_every_alias_names_a_step():
    ids = {entry["id"] for entry in CATALOG["steps"]}
    assert CATALOG["aliases"], "no alias: 'ocr' is expected to mean the beginning"
    for alias, target in CATALOG["aliases"].items():
        assert alias not in ids
        assert target in ids


def test_the_catalogue_is_plain_json_with_a_trailing_newline():
    text = generator.render(CATALOG)
    assert text.endswith("}\n")
    assert json.loads(text) == CATALOG


ROUTING_INPUTS = generator.routing_inputs(REPO)
ALL_TARGETS = (*generator.TARGETS, *generator.ROUTING_INPUT_TARGETS)


@pytest.fixture
def checkout(tmp_path):
    """A directory with this checkout's source modules and no generated copy."""
    for source in (generator.STEPS_MODULE, generator.ROUTING_MODULE):
        module = tmp_path / source
        module.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / source, module)
    return tmp_path


def _written(root: Path) -> dict[str, str]:
    return {
        str(target): (root / target).read_text()
        for target in ALL_TARGETS
        if (root / target).exists()
    }


def _expected_copies() -> dict[str, str]:
    expected = {str(target): generator.render(CATALOG) for target in generator.TARGETS}
    expected.update(
        {
            str(target): generator.render(ROUTING_INPUTS)
            for target in generator.ROUTING_INPUT_TARGETS
        }
    )
    return expected


def test_check_fails_and_writes_nothing_when_a_copy_is_missing(checkout, capsys):
    assert generator.main(["--check"], root=checkout) == 1
    assert _written(checkout) == {}
    err = capsys.readouterr().err
    for target in ALL_TARGETS:
        assert str(target) in err


def test_a_run_writes_every_copy_and_check_then_passes(checkout):
    assert generator.main([], root=checkout) == 0
    assert _written(checkout) == _expected_copies()
    assert generator.main(["--check"], root=checkout) == 0


@pytest.mark.parametrize("target", ALL_TARGETS, ids=str)
def test_check_names_the_one_copy_that_was_edited(checkout, capsys, target):
    generator.main([], root=checkout)
    edited = checkout / target
    edited.write_text(edited.read_text() + " ")
    before = _written(checkout)
    capsys.readouterr()

    assert generator.main(["--check"], root=checkout) == 1

    err = capsys.readouterr().err
    assert str(target) in err
    for other in ALL_TARGETS:
        if other != target:
            assert str(other) not in err
    assert _written(checkout) == before, "--check must not write"


def test_a_run_rewrites_only_the_copy_that_is_stale(checkout, capsys):
    generator.main([], root=checkout)
    stale = checkout / generator.ROUTING_INPUT_TARGETS[0]
    stale.write_text("{}")
    capsys.readouterr()

    assert generator.main([], root=checkout) == 0

    out = capsys.readouterr().out
    assert str(generator.ROUTING_INPUT_TARGETS[0]) in out
    for target in generator.TARGETS:
        assert str(target) not in out
    assert _written(checkout) == _expected_copies()


def test_the_catalogue_follows_the_step_module_of_the_checkout_it_is_given(checkout):
    module = checkout / generator.STEPS_MODULE
    module.write_text(module.read_text().replace('"Summarization"', '"Summary"'))

    labels = [
        entry["label"]
        for entry in generator.catalog(generator.load_steps(checkout))["steps"]
    ]

    assert "Summary" in labels and "Summarization" not in labels


def test_print_writes_the_catalogue_to_stdout_only(checkout, capsys):
    assert generator.main(["--print"], root=checkout) == 0
    assert capsys.readouterr().out == generator.render(CATALOG)
    assert _written(checkout) == {}


# ------------------------------------------------------------------ the surfaces


def _sdk_rerun_step_values() -> list[str]:
    tree = ast.parse(SDK_MODELS.read_text())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "RerunStep":
            return [
                statement.value.value
                for statement in node.body
                if isinstance(statement, ast.Assign)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            ]
    raise AssertionError(f"SDK: no RerunStep class in {SDK_MODELS}")


def test_the_sdk_step_values_are_the_catalogue():
    values = _sdk_rerun_step_values()
    assert sorted(values) == sorted(START_VALUES), (
        "SDK: idp_sdk.models.base.RerunStep does not offer the steps of "
        f"idp_common.run_plan.steps. RerunStep has {sorted(values)}; "
        f"the step list has {sorted(START_VALUES)}"
    )


def _cli_option(command: str, option: str):
    import idp_cli.cli as cli_module

    params = {param.name: param for param in cli_module.cli.commands[command].params}
    assert option in params, (
        f"CLI: 'idp-cli {command}' has no --{option} option; it has {sorted(params)}"
    )
    return params[option]


@pytest.mark.parametrize("command", CLI_COMMANDS)
def test_the_cli_step_choices_are_the_catalogue(command):
    choices = list(getattr(_cli_option(command, "step").type, "choices", ()))
    assert sorted(choices) == sorted(START_VALUES), (
        f"CLI: the --step choices of 'idp-cli {command}' are not the steps of "
        f"idp_common.run_plan.steps. The command offers {sorted(choices)}; "
        f"the step list has {sorted(START_VALUES)}"
    )


@pytest.mark.parametrize("command", CLI_COMMANDS)
def test_the_cli_skip_choices_are_the_optional_steps(command):
    option = _cli_option(command, "skip")
    choices = list(getattr(option.type, "choices", ()))
    assert choices == OPTIONAL_STEPS, (
        f"CLI: the --skip choices of 'idp-cli {command}' are not the optional "
        f"steps. The command offers {choices}; the step list has {OPTIONAL_STEPS}"
    )
    assert option.multiple, f"CLI: 'idp-cli {command} --skip' takes one value only"


def _gateway_manager(monkeypatch) -> types.ModuleType:
    """The gateway manager as imported.

    Its tool schema is built at import (the step values come from the JSON copy
    beside the module), so the module is loaded, with the deployment toolkit it
    imports replaced: that package is not a test dependency.
    """
    client_module = types.ModuleType(
        "bedrock_agentcore_starter_toolkit.operations.gateway.client"
    )
    client_module.GatewayClient = object
    for name, module in {
        "bedrock_agentcore_starter_toolkit": types.ModuleType("toolkit"),
        "bedrock_agentcore_starter_toolkit.operations": types.ModuleType("toolkit"),
        "bedrock_agentcore_starter_toolkit.operations.gateway": types.ModuleType(
            "toolkit"
        ),
        "bedrock_agentcore_starter_toolkit.operations.gateway.client": client_module,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    for sibling in ("cfnresponse", "log_sanitizer"):
        monkeypatch.delitem(sys.modules, sibling, raising=False)
    monkeypatch.syspath_prepend(str(GATEWAY_DIR))
    return _load("_gateway_manager_for_step_catalog", GATEWAY_DIR / "index.py")


def _gateway_reprocess_properties(module: types.ModuleType) -> dict:
    tools = {tool["name"]: tool for tool in module.IDP_TOOLS_SCHEMA}
    assert "reprocess" in tools, "MCP gateway: no 'reprocess' tool in IDP_TOOLS_SCHEMA"
    return tools["reprocess"]["inputSchema"]["properties"]


def _listed(description: str, after: str) -> list[str]:
    """The comma-separated values a description lists after a phrase, up to the
    full stop. The gateway's tool schema has no ``enum``, so the values a
    property accepts are written into its description."""
    assert after in description, (
        f"MCP gateway: the description does not list its values after '{after}': "
        f"{description!r}"
    )
    listed = description.split(after, 1)[1].split(".", 1)[0]
    return [value.strip() for value in listed.split(",")]


def test_the_mcp_reprocess_step_values_are_the_catalogue(monkeypatch):
    module = _gateway_manager(monkeypatch)
    offered = getattr(module, "REPROCESS_START_VALUES", None)
    assert offered == START_VALUES, (
        "MCP gateway: REPROCESS_START_VALUES in "
        "src/lambda/agentcore_gateway_manager/index.py is not the steps of "
        f"idp_common.run_plan.steps. The tool offers {offered}; "
        f"the step list has {START_VALUES}"
    )
    step = _gateway_reprocess_properties(module)["step"]
    described = _listed(step.get("description", ""), "One of:")
    assert described == START_VALUES, (
        "MCP gateway: the 'step' argument of the reprocess tool describes "
        f"{described}; the step list has {START_VALUES}"
    )


def test_the_mcp_reprocess_skip_values_are_the_optional_steps(monkeypatch):
    module = _gateway_manager(monkeypatch)
    offered = getattr(module, "REPROCESS_SKIP_VALUES", None)
    assert offered == OPTIONAL_STEPS, (
        "MCP gateway: REPROCESS_SKIP_VALUES in "
        "src/lambda/agentcore_gateway_manager/index.py is not the optional steps. "
        f"The tool offers {offered}; the step list has {OPTIONAL_STEPS}"
    )
    properties = _gateway_reprocess_properties(module)
    assert "skip" in properties, (
        "MCP gateway: the reprocess tool in "
        "src/lambda/agentcore_gateway_manager/index.py has no 'skip' argument"
    )
    skip = properties["skip"]
    described = _listed(skip.get("description", ""), "Any of:")
    assert skip.get("type") == "array" and described == OPTIONAL_STEPS, (
        "MCP gateway: the 'skip' argument of the reprocess tool is not an array "
        f"of the optional steps. It is {skip}; the step list has {OPTIONAL_STEPS}"
    )


def test_the_mcp_handler_checks_the_step_against_the_step_list():
    """The schema only describes the values; the handler is what refuses one that
    is not a step, and it reads the step list itself."""
    source = (
        REPO / "src/lambda/agentcore_mcp_handler/tools/batch_reprocess.py"
    ).read_text()
    assert "run_steps.start_values()" in source, (
        "MCP handler: tools/batch_reprocess.py does not check the step against "
        "idp_common.run_plan.steps.start_values()"
    )
    assert "run_steps.optional_step_ids()" in source, (
        "MCP handler: tools/batch_reprocess.py does not check the steps to skip "
        "against idp_common.run_plan.steps.optional_step_ids()"
    )


def _camel(value: str) -> str:
    """``policy_rule_validation`` or ``OUTPUT_MISSING`` as a message id segment.

    The UI's message ids are dotted lower camel case with no underscore
    (``formatjs/enforce-id`` in ``src/ui/eslint.config.js``), so a step id or a
    refusal code is declared under its camel-case form.
    """
    head, *rest = value.lower().split("_")
    return head + "".join(part.capitalize() for part in rest)


def _picker_message_ids(group: str) -> set[str]:
    assert STEP_PICKER.exists(), (
        f"Web UI: {STEP_PICKER.relative_to(REPO)} does not exist, so the web UI "
        "has no step picker for the steps of idp_common.run_plan.steps"
    )
    return set(
        re.findall(
            rf"""id:\s*['"]runPlan\.{group}\.([A-Za-z0-9]+)['"]""",
            STEP_PICKER.read_text(),
        )
    )


def test_the_ui_step_picker_has_a_message_for_every_step():
    """The picker's messages are the web UI's only step labels, so they cover the
    display steps as well as the steps a run can start at."""
    message_ids = _picker_message_ids("step")
    expected = (
        {_camel(entry["id"]) for entry in CATALOG["steps"]}
        | {_camel(entry["id"]) for entry in CATALOG["display_steps"]}
        | {_camel(CATALOG["resume"])}
    )
    assert message_ids == expected, (
        f"Web UI: {STEP_PICKER.relative_to(REPO)} declares a runPlan.step.<id> "
        f"message for {sorted(message_ids)}; the step list has {sorted(expected)}"
    )


def _picker_default_messages() -> dict[str, str]:
    """``runPlan.step.<id>`` to the English text the picker declares for it."""
    return dict(
        re.findall(
            r"""id:\s*['"]runPlan\.step\.([A-Za-z0-9]+)['"],\s*"""
            r"""defaultMessage:\s*['"]([^'"]+)['"]""",
            STEP_PICKER.read_text(),
        )
    )


def test_the_ui_step_messages_spell_each_label_as_the_step_list_does():
    """One label, one spelling: the English text of a step's message is the label
    the step list gives it."""
    declared = _picker_default_messages()
    entries = [*CATALOG["steps"], *CATALOG["display_steps"]]
    # Every step message was read, and resume is the one that is not a step.
    assert len(declared) == len(entries) + 1, sorted(declared)
    for entry in entries:
        assert declared[_camel(entry["id"])] == entry["label"], entry["id"]


def _refusal_codes() -> set[str]:
    """The codes a start step is refused with: the constants of
    ``run_plan/eligibility.py`` that are named after their own value."""
    tree = ast.parse(
        (REPO / "lib/idp_common_pkg/idp_common/run_plan/eligibility.py").read_text()
    )
    codes = set()
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == node.value.value:
                    codes.add(target.id)
    return codes


def test_the_ui_step_picker_explains_every_refusal_code():
    codes = _refusal_codes()
    assert len(codes) >= 5, f"too few refusal codes were read: {sorted(codes)}"
    explained = _picker_message_ids("reason")
    expected = {_camel(code) for code in codes}
    # `other` is the sentence for a code the picker does not know.
    assert explained - {"other"} == expected, (
        f"Web UI: {STEP_PICKER.relative_to(REPO)} declares a runPlan.reason.<code> "
        f"message for {sorted(explained)}; run_plan/eligibility.py refuses with "
        f"{sorted(codes)}"
    )
    assert "other" in explained


# ------------------------------------------------------------- routing inputs


@pytest.mark.parametrize("target", generator.ROUTING_INPUT_TARGETS, ids=str)
def test_the_routing_input_copy_is_what_the_generator_writes(target):
    path = REPO / target
    assert path.exists(), (
        f"{target} is missing; run python3 scripts/generate_run_step_catalog.py"
    )
    assert path.read_text() == generator.render(ROUTING_INPUTS), (
        f"{target} is stale; run python3 scripts/generate_run_step_catalog.py"
    )


def test_the_routing_inputs_read_from_source_are_the_modules_own():
    """The generator reads the routing module's source so that it needs nothing
    installed. What it reads has to be what the module holds when imported."""
    import dataclasses

    from idp_common.config import routing_validation as module

    # The file holds a list where the module holds a tuple.
    assert ROUTING_INPUTS["inputs"] == [
        {**dataclasses.asdict(routing_input), "choices": list(routing_input.choices)}
        for routing_input in module.ROUTING_INPUTS
    ]
    assert ROUTING_INPUTS["aggregates"] == list(module.AGGREGATES)
    assert ROUTING_INPUTS["id_pattern"] == module.ID_PATTERN
    assert ROUTING_INPUTS["limits"] == {
        "max_rules": module.MAX_RULES,
        "max_conditions": module.MAX_CONDITIONS,
        "max_label_bytes": module.MAX_LABEL_BYTES,
        "max_path_bytes": module.MAX_PATH_BYTES,
        "max_test_bytes": module.MAX_TEST_BYTES,
        "max_reason_bytes": module.MAX_REASON_BYTES,
        "max_reason_placeholders": module.MAX_REASON_PLACEHOLDERS,
    }


def test_the_routing_input_catalogue_has_every_kind_of_input():
    """A reader that silently dropped a field would still produce a file."""
    inputs = {entry["name"]: entry for entry in ROUTING_INPUTS["inputs"]}
    assert inputs["section_classes"]["is_list"] is True
    assert inputs["field_value"]["path_kind"] == "field"
    assert inputs["field_value"]["needs_path"] is True
    assert inputs["failed_policy_rule_count"]["path_optional"] is True
    assert {entry["type"] for entry in inputs.values()} == {
        "string",
        "number",
        "boolean",
        "field",
    }
    # The input whose path names a class, and every kind of path beside it.
    present = inputs["class_present"]
    assert (present["type"], present["needs_path"], present["is_list"]) == (
        "boolean",
        True,
        False,
    )
    assert {entry["path_kind"] for entry in inputs.values()} == {
        "",
        "field",
        "policy_rule",
        "policy_type",
        "class",
    }


def test_the_routing_input_catalogue_carries_the_choices_of_an_input():
    """A reader that dropped the last field would write every input as free."""
    inputs = {entry["name"]: entry for entry in ROUTING_INPUTS["inputs"]}
    assert inputs["policy_rule_verdict"]["choices"] == [
        "pass",
        "fail",
        "not_found",
        "unknown",
    ]
    assert inputs["processing_mode"]["choices"] == ["pipeline", "bda"]
    # Every input has the key, and an input with free values has no choices.
    assert all(isinstance(entry["choices"], list) for entry in inputs.values())
    assert inputs["page_count"]["choices"] == []


def test_the_routing_limits_include_the_reason_limits():
    assert ROUTING_INPUTS["limits"]["max_reason_bytes"] == 500
    assert ROUTING_INPUTS["limits"]["max_reason_placeholders"] == 10


def test_a_routing_input_the_reader_cannot_read_is_refused(checkout):
    module = checkout / generator.ROUTING_MODULE
    module.write_text(
        module.read_text().replace(
            'RoutingInput("page_count", "Page count", TYPE_NUMBER, False, False),',
            'RoutingInput("page_count", make_label(), TYPE_NUMBER, False, False),',
        )
    )

    with pytest.raises(ValueError):
        generator.routing_inputs(checkout)


def test_the_routing_inputs_follow_the_module_of_the_checkout_they_are_given(checkout):
    module = checkout / generator.ROUTING_MODULE
    module.write_text(
        module.read_text().replace("MAX_CONDITIONS = 8", "MAX_CONDITIONS = 3")
    )

    assert generator.routing_inputs(checkout)["limits"]["max_conditions"] == 3
