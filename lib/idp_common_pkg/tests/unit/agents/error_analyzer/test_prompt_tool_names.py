# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every shipped error-analyzer system prompt names only tools the agent has.

The prompt is the model's only description of how to choose between its tools, and
it used to direct the model to eight tools that were never registered
(``cloudwatch_document_logs``, ``dynamodb_status``, ``lambda_lookup`` and so on) while
omitting most of the nine it actually had (#1555).

The authority for "a tool the agent has" is the agent itself: the tests build a real
``strands.Agent`` through ``create_error_analyzer_agent`` and read the names and input
parameters out of its tool registry, which is exactly what Strands sends to the model.
Neither the tool list nor the prompt is restated here.

The rule is about identifiers, not about phrasing: every underscore-joined identifier
in a shipped prompt must be a registered tool, a parameter of one, or a member of
:data:`NON_TOOL_IDENTIFIERS` (data field names the prompt quotes, each with its own
reason). That fails closed: a new phantom tool name is an unknown identifier whatever
sentence it appears in. The converse is asserted too -- every registered tool is
named by every shipped prompt -- because a prompt that omits a tool under-describes
the tool set as surely as one that invents a tool over-describes it.

A parenthesised argument list must hold only parameters of the nearest tool named
before it on the same line, so an argument attached to the wrong tool fails, and
every registered tool must be named inside the ``TOOL USAGE`` section itself, not
merely mentioned elsewhere in the prompt.

What it cannot see, measured by mutation rather than assumed: a tool referred to
without an underscore (``LambdaLookup``, ``xray-trace``, ``xray.trace``, "the lambda
lookup tool"); a phantom spelled exactly like a registered parameter or a listed data
field; an argument named outside parentheses or inside double quotes; an argument
list holding a multi-word item, which reads as prose and is not judged; and a wrong
enumerated value such as an ``issue_type`` the tool does not accept.
Prose is not machine-checkable; the underscore form is the one tool names take, and
every phantom this test was written against had it. Tool *descriptions* (the
docstrings Strands sends) are not checked: they quote many result keys, and no
derivable set separates those from tool names.
"""

import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import strands
import yaml

from idp_common.config.models import (
    ChatCompanionConfig,
    ErrorAnalyzerConfig,
    IDPConfig,
)

REPO_ROOT = Path(__file__).resolve().parents[6]

pytestmark = pytest.mark.unit

#: An identifier of words joined by at least one underscore, in any case. Case is
#: matched loosely and compared exactly, so ``Fetch_Document_Record`` is an unknown
#: identifier rather than an invisible one.
IDENTIFIER = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b")

#: A double-quoted value, removed from an argument list before its words are read,
#: so ``(issue_type "throttling")`` contributes ``issue_type`` only.
QUOTED = re.compile(r'"[^"]*"')


class _TagTolerantLoader(yaml.SafeLoader):
    """A SafeLoader that reads any ``!Tag`` as a plain node.

    CloudFormation templates are tracked YAML that a plain ``safe_load`` rejects on
    ``!Ref`` and friends. Tolerating tags lets a genuine parse failure be an error
    instead of a silent skip.
    """


def _construct_tagged(loader, _suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_TagTolerantLoader.add_multi_constructor("!", _construct_tagged)

#: Identifiers a shipped prompt may quote that are data, not tools. One reason per
#: member. ``test_no_non_tool_identifier_is_stale`` fails on a member no
#: shipped prompt contains, so a dead entry cannot pre-approve whatever next takes
#: its name.
NON_TOOL_IDENTIFIERS = {
    "status_reason": (
        "A Document attribute the summarization Lambda sets when the stage fails; "
        "the prompt names it as where the generic wrapper message shows up, not as "
        "a tool."
    ),
    "assessment_incomplete": (
        "A ProcessingIssue code the assessment stage records; the prompt names it as "
        "an example of a non-fatal issue not to mistake for the failure."
    ),
    "log_group": (
        "A key in search_cloudwatch_logs results; the evidence template asks for its "
        "full value."
    ),
    "log_stream": (
        "A key in search_cloudwatch_logs results; the evidence template asks for its "
        "full value."
    ),
}


def _build_agent():
    """Build the real agent with no AWS access and return it."""
    from idp_common.agents.error_analyzer.agent import create_error_analyzer_agent

    with (
        patch(
            "idp_common.agents.error_analyzer.agent.get_config",
            return_value=IDPConfig(),
        ),
        patch(
            "idp_common.agents.error_analyzer.agent.create_strands_bedrock_model",
            return_value=MagicMock(),
        ),
        patch(
            "idp_common.agents.error_analyzer.agent.get_error_analyzer_model_id",
            return_value="test-model",
        ),
        patch("boto3.Session"),
    ):
        return create_error_analyzer_agent()


@pytest.fixture(scope="module")
def registered_tools() -> dict[str, set[str]]:
    """Tool name -> input parameter names, read from the agent's Strands registry."""
    if isinstance(strands, MagicMock) or not isinstance(strands.Agent, type):
        pytest.fail(
            "strands is stubbed in this environment; the registered tool set cannot "
            "be read, so this test would measure nothing. Install strands-agents."
        )
    agent = _build_agent()
    assert isinstance(agent, strands.Agent)
    specs = agent.tool_registry.get_all_tools_config()
    tools = {
        name: set(spec["inputSchema"]["json"].get("properties", {}))
        for name, spec in specs.items()
    }
    assert tools, "the agent registered no tools, so every check here is vacuous"
    assert set(tools) == set(agent.tool_names)
    return tools


#: The ``agents.<key>`` sections whose ``system_prompt`` is a copy of the
#: error-analyzer prompt. ``chat_companion`` carries one in its model default and has
#: carried one in YAML; no agent reads it today, but a copy that ships is checked.
PROMPT_CARRYING_AGENTS = ("error_analyzer", "chat_companion")


def tracked_yaml_prompts(root: Path) -> dict[str, str]:
    """Each ``agents.<key>.system_prompt`` copy in every YAML file git sees under root.

    Tracked and untracked-but-not-ignored files both count, so a new preset is
    checked before it is committed.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
        + ["--", "*.yaml", "*.yml"],
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.split("\0")
    prompts = {}
    for rel in filter(None, listed):
        path = root / rel
        if not path.is_file():  # tracked but deleted in the working tree
            continue
        text = path.read_text(encoding="utf-8")
        if not any(key in text for key in PROMPT_CARRYING_AGENTS):
            continue
        try:
            docs = list(yaml.load_all(text, Loader=_TagTolerantLoader))  # nosec B506 # noqa: S506 - SafeLoader subclass
        except yaml.YAMLError as exc:
            raise AssertionError(
                f"{rel} mentions {PROMPT_CARRYING_AGENTS} but does not parse, so a "
                f"prompt copy in it cannot be checked: {exc}"
            ) from exc
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            agents = doc.get("agents")
            if not isinstance(agents, dict):
                continue
            for agent_key in PROMPT_CARRYING_AGENTS:
                section = agents.get(agent_key)
                if isinstance(section, dict) and "system_prompt" in section:
                    prompts[f"{rel}::{agent_key}"] = str(section["system_prompt"])
    return prompts


def shipped_prompts() -> dict[str, str]:
    """Every copy of the error-analyzer system prompt the product ships."""
    prompts = {
        "ErrorAnalyzerConfig.system_prompt default": ErrorAnalyzerConfig.model_fields[
            "system_prompt"
        ].default,
        # Not read by any agent today, but it is the same prompt text and is merged
        # into every effective configuration, so it is held to the same rule.
        "ChatCompanionConfig.system_prompt default": ChatCompanionConfig.model_fields[
            "system_prompt"
        ].default,
    }
    prompts.update(tracked_yaml_prompts(REPO_ROOT))
    return prompts


SHIPPED = shipped_prompts()


def test_the_system_default_prompt_is_among_those_checked():
    """The YAML walk reaches the system default, so it is reading real files."""
    assert (
        "lib/idp_common_pkg/idp_common/config/system_defaults/base-agents.yaml"
        "::error_analyzer" in SHIPPED
    )


def test_the_yaml_walk_finds_a_preset_copy(tmp_path):
    """A preset carrying its own copy is collected, so a future copy is checked too."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    preset = tmp_path / "config_library" / "x" / "config.yaml"
    preset.parent.mkdir(parents=True)
    preset.write_text(
        "agents:\n  error_analyzer:\n    system_prompt: use lambda_lookup\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    assert tracked_yaml_prompts(tmp_path) == {
        "config_library/x/config.yaml::error_analyzer": "use lambda_lookup"
    }


@pytest.mark.parametrize("source", sorted(SHIPPED))
def test_prompt_names_no_unregistered_tool(source, registered_tools):
    known = set(registered_tools).union(*registered_tools.values())
    unknown = sorted(
        set(IDENTIFIER.findall(SHIPPED[source])) - known - set(NON_TOOL_IDENTIFIERS)
    )
    assert not unknown, (
        f"{source} names identifiers that are neither a registered error-analyzer "
        f"tool nor a parameter of one: {unknown}. Registered tools: "
        f"{sorted(registered_tools)}"
    )


@pytest.mark.parametrize("source", sorted(SHIPPED))
def test_prompt_names_every_registered_tool(source, registered_tools):
    named = set(IDENTIFIER.findall(SHIPPED[source]))
    missing = sorted(set(registered_tools) - named)
    assert not missing, f"{source} never mentions registered tools {missing}"


#: A section heading in the prompt: a line of capitals ending in a colon, such as
#: ``TOOL USAGE:`` or ``CRITICAL — DRILL INTO THE FAILING STAGE'S OWN LOGS:``.
HEADING = r"^[ \t]*[A-Z][^a-z\n]*:[ \t]*\r?$"


def _tool_usage_section(prompt: str) -> str | None:
    """The text from ``TOOL USAGE:`` to the next heading, or None if absent.

    It ends at a heading rather than a blank line, so blank lines between groups of
    tools inside the section do not cut it short.
    """
    match = re.search(
        r"^[ \t]*TOOL USAGE:[ \t]*\r?\n(.*?)(?=" + HEADING + r"|\Z)",
        prompt,
        re.M | re.S,
    )
    return match.group(1) if match else None


@pytest.mark.parametrize("source", sorted(SHIPPED))
def test_the_tool_usage_section_names_every_registered_tool(source, registered_tools):
    """A tool named only in passing elsewhere is not one the section describes."""
    section = _tool_usage_section(SHIPPED[source])
    if section is None:
        pytest.fail(f"{source} has no TOOL USAGE section to describe its tools")
    missing = sorted(set(registered_tools) - set(IDENTIFIER.findall(section)))
    assert not missing, f"{source}'s TOOL USAGE section omits {missing}"


def _argument_words(group: str) -> list[str] | None:
    """The words of ``group`` if it is an argument list, else None.

    An argument list is items separated by commas or the word ``or``, each a single
    word once double-quoted values are removed: ``document_id``, ``date, hours_back,
    limit``, ``issue_type "performance" or "capacity"``. A parenthetical of prose,
    such as ``the ConfigVersion field from fetch_document_record``, has an item of
    several words and is not one.
    """
    items = re.split(r",|\bor\b", QUOTED.sub("", group).replace("`", ""))
    words = [item.split() for item in items if item.strip()]
    if not words or any(len(item) != 1 for item in words):
        return None
    return [item[0] for item in words]


@pytest.mark.parametrize("source", sorted(SHIPPED))
def test_arguments_listed_after_a_tool_are_its_own(source, registered_tools):
    """An argument list belongs to the nearest tool named before it on its line.

    Every word in it must be a parameter of that tool, so
    ``fetch_document_record (document_id)`` fails, and so does a list that sits
    after other words (``search_cloudwatch_logs without document_id (a, b)``).
    """
    tool_name = re.compile(
        r"\b(" + "|".join(map(re.escape, sorted(registered_tools))) + r")\b"
    )
    wrong = []
    for line in SHIPPED[source].splitlines():
        for group in re.finditer(r"\(([^()]*)\)", line):
            owners = tool_name.findall(line[: group.start()])
            words = _argument_words(group.group(1))
            if not owners or words is None:
                continue
            owner = owners[-1]
            wrong.extend(
                f"{owner}: {w}" for w in words if w not in registered_tools[owner]
            )
    assert not wrong, f"{source} pairs tools with arguments they do not take: {wrong}"


def test_an_argument_list_is_told_apart_from_prose():
    assert _argument_words("document_id") == ["document_id"]
    assert _argument_words("date, hours_back, limit") == ["date", "hours_back", "limit"]
    assert _argument_words('issue_type "a", "b" or "c"') == ["issue_type"]
    assert _argument_words("object_key or document_id") == ["object_key", "document_id"]
    assert _argument_words("`object_key`") == ["object_key"]
    assert _argument_words("the ConfigVersion field from fetch_document_record") is None
    assert _argument_words("e.g. after a model error") is None


def test_no_non_tool_identifier_is_stale():
    used = set().union(*(IDENTIFIER.findall(p) for p in SHIPPED.values()))
    dead = sorted(set(NON_TOOL_IDENTIFIERS) - used)
    assert not dead, f"NON_TOOL_IDENTIFIERS entries no shipped prompt uses: {dead}"


def test_no_non_tool_identifier_is_a_tool(registered_tools):
    known = set(registered_tools).union(*registered_tools.values())
    assert not set(NON_TOOL_IDENTIFIERS) & known
