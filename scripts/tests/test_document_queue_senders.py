# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A document reaches the document queue through one function.

``idp_common.run_plan.sender.send_run`` is the only code that puts a document on
the document queue. It encodes the run plan the queue processor reads, so a
message sent any other way starts a run with no plan; and the checks that decide
whether a document may run again (``idp_common.run_plan.eligibility``, which holds
the malware gate's refusals) sit in front of it, so a second sender is a second
door that those checks do not guard.

Three things are held here, each read from the tree:

* **No Lambda that is handed the document queue's URL sends to it itself.** The
  functions are found in the templates (an environment variable whose value is
  the queue), and every SQS send in their code has to name a different queue.
* **The library has one document-queue sender.** Every SQS send under ``lib/`` is
  either that one or is recorded with the queue it writes to. The SDK and the CLI
  have none: they ask the stack's reprocess resolver.
* **The malware gate's refusals are declared once**, in
  ``idp_common/run_plan/eligibility.py``. A second list of them is how a second
  door comes to refuse a different set.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Iterator

import gate_premises
import pytest
from repo_files import tracked_paths

pytestmark = pytest.mark.unit

REPO = gate_premises.REPO_ROOT

PARENT_TEMPLATE = "template.yaml"
#: The templates whose functions are read: the main stack, the API resolvers and
#: the processing pattern.
TEMPLATES = (
    PARENT_TEMPLATE,
    "nested/api-resolvers/template.yaml",
    "patterns/unified/template.yaml",
)
#: The document queue's logical id in the main stack.
DOCUMENT_QUEUE = "DocumentQueue"

SENDER = "lib/idp_common_pkg/idp_common/run_plan/sender.py"
ELIGIBILITY = "lib/idp_common_pkg/idp_common/run_plan/eligibility.py"
REFUSALS = "MALWARE_REFUSED_STATUSES"

#: The library trees a document-queue sender could be written in.
LIBRARY_TREES = (
    "lib/idp_common_pkg/idp_common",
    "lib/idp_sdk/idp_sdk",
    "lib/idp_cli_pkg/idp_cli",
)

#: Every file under ``LIBRARY_TREES`` that sends an SQS message, and the queue it
#: writes to. One of them is the document queue's sender. A file that is not listed
#: fails the inventory test, so a new send has to be read and placed here; an entry
#: whose file no longer sends fails too.
LIBRARY_QUEUE_SENDS = {
    SENDER: "the document queue: the one sender",
    "lib/idp_common_pkg/idp_common/review_queues/testset_docs.py": (
        "the Test Studio summary queue: asks for a test set's summary to be rebuilt"
    ),
    "lib/idp_common_pkg/idp_common/agents/quick_start/tools/bootstrap_tools.py": (
        "the data generator feature's queue, whose ARN comes from that feature's "
        "registration: asks it to generate sample documents"
    ),
}

SEND_METHODS = ("send_message", "send_message_batch")


# ------------------------------------------------------------------- templates


def _names(value: Any, logical_id: str) -> bool:
    """Whether a template value is the URL of the queue ``logical_id`` names."""
    if not isinstance(value, dict):
        return False
    if value.get("Fn::Ref") == logical_id or value.get("Ref") == logical_id:
        return True
    get_att = value.get("Fn::GetAtt")
    if get_att in (f"{logical_id}.QueueUrl", [logical_id, "QueueUrl"]):
        return True
    sub = value.get("Fn::Sub")
    return isinstance(sub, str) and sub.strip() == "${" + logical_id + "}"


def _queue_names_by_template() -> dict[str, set[str]]:
    """Template path to the names the document queue's URL goes by in it.

    In the main stack it is the queue resource. In a nested stack it is each
    parameter the main stack passes the queue's URL to.
    """
    names: dict[str, set[str]] = {template: set() for template in TEMPLATES}
    names[PARENT_TEMPLATE].add(DOCUMENT_QUEUE)
    parent = gate_premises.load_template(PARENT_TEMPLATE)
    for resource in (parent.get("Resources") or {}).values():
        properties = resource.get("Properties") or {}
        source = gate_premises.source_template_for(
            properties.get("TemplateURL") or properties.get("Location")
        )
        if source not in names:
            continue
        for parameter, value in (properties.get("Parameters") or {}).items():
            if _names(value, DOCUMENT_QUEUE):
                names[source].add(parameter)
    return names


def _functions_with_the_queue() -> list[tuple[str, str, str, frozenset[str]]]:
    """(template, logical id, code directory, environment variables) of every
    function whose environment holds the document queue's URL."""
    found = []
    for template, queue_names in _queue_names_by_template().items():
        resources = gate_premises.load_template(template).get("Resources") or {}
        for logical_id, resource in resources.items():
            if resource.get("Type") not in (
                "AWS::Serverless::Function",
                "AWS::Lambda::Function",
            ):
                continue
            properties = resource.get("Properties") or {}
            variables = (properties.get("Environment") or {}).get("Variables") or {}
            holding = frozenset(
                name
                for name, value in variables.items()
                if any(_names(value, queue) for queue in queue_names)
            )
            if not holding:
                continue
            code_uri = properties.get("CodeUri")
            code_dir = (
                str((Path(template).parent / code_uri).as_posix())
                if isinstance(code_uri, str)
                else ""
            )
            found.append((template, logical_id, code_dir, holding))
    return found


FUNCTIONS = _functions_with_the_queue()


# ------------------------------------------------------------------- code


def _is_test_file(path: Path) -> bool:
    return (
        path.name.startswith("test_")
        or path.name == "conftest.py"
        or "tests" in path.parts
    )


def _sends(tree: ast.AST) -> list[ast.Call]:
    """Every SQS send: a call of ``send_message`` or ``send_message_batch`` that
    names a queue, or passes its arguments in a way that hides which one."""
    calls = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in SEND_METHODS
        ):
            continue
        keywords = {keyword.arg for keyword in node.keywords}
        if "QueueUrl" in keywords or None in keywords:
            calls.append(node)
    return calls


def _environment_names(node: ast.AST) -> set[str]:
    """The environment variables an expression reads."""
    names = set()
    for part in ast.walk(node):
        key = None
        if isinstance(part, ast.Subscript) and ast.unparse(part.value) == "os.environ":
            key = part.slice
        elif (
            isinstance(part, ast.Call)
            and ast.unparse(part.func) in ("os.environ.get", "os.getenv")
            and part.args
        ):
            key = part.args[0]
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            names.add(key.value)
    return names


def _queue_variables(call: ast.Call, tree: ast.AST) -> set[str]:
    """The environment variables a send's ``QueueUrl`` comes from.

    Read from the argument itself, or, when the argument is a name or an
    attribute, from what the module assigns to it. Empty when it cannot be told.
    """
    argument = next(
        (keyword.value for keyword in call.keywords if keyword.arg == "QueueUrl"),
        None,
    )
    if argument is None:
        return set()
    names = _environment_names(argument)
    if names or not isinstance(argument, (ast.Name, ast.Attribute)):
        return names
    target_text = ast.unparse(argument)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if any(ast.unparse(target) == target_text for target in targets):
            names |= _environment_names(value)
    return names


def _python_files(code_dir: str) -> Iterator[Path]:
    for path in sorted((REPO / code_dir).rglob("*.py")):
        if not _is_test_file(path.relative_to(REPO)):
            yield path


def findings_for(code_dir: str, holding: frozenset[str]) -> list[str]:
    """Each SQS send in a function's code that is, or may be, to the document queue."""
    findings = []
    for path in _python_files(code_dir):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _sends(tree):
            where = f"{path.relative_to(REPO)}:{call.lineno}"
            variables = _queue_variables(call, tree)
            if not variables:
                findings.append(
                    f"{where} sends an SQS message and the queue cannot be read "
                    "from the code"
                )
            elif variables & holding:
                findings.append(
                    f"{where} sends to the document queue "
                    f"({', '.join(sorted(variables & holding))})"
                )
    return findings


# ------------------------------------------------------------------- Lambdas


def test_the_functions_that_hold_the_queue_are_found():
    """A scan that finds no function passes everything."""
    code_dirs = {code_dir for _, _, code_dir, _ in FUNCTIONS}
    assert "src/lambda/queue_sender" in code_dirs, (
        "the ingest sender was not found among the functions that hold the "
        f"document queue's URL: {sorted(code_dirs)}"
    )
    assert any(template != PARENT_TEMPLATE for template, _, _, _ in FUNCTIONS), (
        "no function of a nested stack was found: the parameter the queue's URL is passed in was not followed"
    )


@pytest.mark.parametrize(
    "template,logical_id,code_dir,holding",
    FUNCTIONS,
    ids=[logical_id for _, logical_id, _, _ in FUNCTIONS],
)
def test_a_function_that_holds_the_queue_does_not_send_to_it(
    template, logical_id, code_dir, holding
):
    assert code_dir and (REPO / code_dir).is_dir(), (
        f"{logical_id} in {template} is handed the document queue's URL and its "
        "code is not in a directory this test can read"
    )
    findings = findings_for(code_dir, holding)
    assert not findings, (
        f"{logical_id} ({template}) is handed the document queue's URL in "
        f"{sorted(holding)}. A document is queued with "
        "idp_common.run_plan.sender.send_run, which sends the run plan with it:\n  "
        + "\n  ".join(findings)
    )


# ------------------------------------------------------------------- library


def _library_files_that_send() -> dict[str, int]:
    """Library file to the number of SQS sends in it."""
    found = {}
    for tree_root in LIBRARY_TREES:
        for path in tracked_paths(REPO / tree_root, "*.py"):
            relative = path.relative_to(REPO)
            if _is_test_file(relative):
                continue
            count = len(_sends(ast.parse(path.read_text(encoding="utf-8"))))
            if count:
                found[relative.as_posix()] = count
    return found


def test_every_library_send_is_recorded_with_its_queue():
    found = _library_files_that_send()
    unrecorded = sorted(set(found) - set(LIBRARY_QUEUE_SENDS))
    assert not unrecorded, (
        f"{unrecorded} send an SQS message and are not in LIBRARY_QUEUE_SENDS. If "
        "the queue is the document queue, call idp_common.run_plan.sender.send_run "
        "instead; otherwise record the file with the queue it writes to."
    )
    gone = sorted(set(LIBRARY_QUEUE_SENDS) - set(found))
    assert not gone, f"recorded as sending an SQS message, and no longer do: {gone}"


def test_the_library_has_one_document_queue_sender():
    found = _library_files_that_send()
    assert found.get(SENDER) == 1, (
        f"{SENDER} holds {found.get(SENDER, 0)} SQS sends; it is the one place a "
        "document is put on the document queue, with one send"
    )
    document_queue = [
        path
        for path, queue in LIBRARY_QUEUE_SENDS.items()
        if queue.startswith("the document queue")
    ]
    assert document_queue == [SENDER]


def test_the_sdk_and_the_cli_send_nothing():
    """They ask the stack's reprocess resolver; they hold no sender of their own."""
    client_side = [
        path
        for path in _library_files_that_send()
        if not path.startswith("lib/idp_common_pkg/")
    ]
    assert client_side == []


def test_every_recorded_send_says_which_queue():
    for path, queue in LIBRARY_QUEUE_SENDS.items():
        assert len(queue) > 30, f"{path} needs its queue written out"


# ------------------------------------------------------------------- malware gate


def _status_names(node: ast.AST) -> set[str]:
    """The ``Status`` members, or the strings, a collection is written with."""
    names = set()
    elements: list[ast.AST] = []
    if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
        elements = list(node.elts)
    elif isinstance(node, ast.Dict):
        elements = [key for key in node.keys if key is not None]
    for element in elements:
        if isinstance(element, ast.Constant) and isinstance(element.value, str):
            names.add(element.value)
            continue
        text = ast.unparse(element)
        if text.startswith("Status."):
            names.add(text.split(".")[1])
        else:
            return set()
    return names


def _collections(tree: ast.AST) -> Iterator[tuple[int, set[str]]]:
    for node in ast.walk(tree):
        names = _status_names(node)
        if names:
            yield getattr(node, "lineno", 0), names


def _declared_refusals() -> set[str]:
    tree = ast.parse((REPO / ELIGIBILITY).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == REFUSALS
            for target in node.targets
        ):
            for _, names in _collections(node.value):
                return names
    raise AssertionError(f"{REFUSALS} is not assigned a set in {ELIGIBILITY}")


def _is_a_malware_status(name: str) -> bool:
    return name.startswith("MALWARE_") or name.startswith("QUARANTIN")


def test_the_refusals_are_the_malware_statuses_of_the_status_enum():
    """The set is checked against the enum, so a new verdict has to be placed."""
    models = (REPO / "lib/idp_common_pkg/idp_common/models.py").read_text(
        encoding="utf-8"
    )
    status = next(
        node
        for node in ast.walk(ast.parse(models))
        if isinstance(node, ast.ClassDef) and node.name == "Status"
    )
    members = {
        target.id
        for statement in status.body
        if isinstance(statement, ast.Assign)
        for target in statement.targets
        if isinstance(target, ast.Name)
    }
    assert _declared_refusals() == {m for m in members if _is_a_malware_status(m)}


def malware_lists_outside_eligibility() -> list[str]:
    """Collections written with exactly the gate's refusals, anywhere else in
    product code: a second declaration of them.

    A collection with some of them is not one. The ingest gate itself names the
    two verdicts it writes (``QUARANTINED`` and ``MALWARE_SCAN_FAILED``) where it
    writes them, and a table over every status contains all three.
    """
    declared = _declared_refusals()
    found = []
    for path in tracked_paths(REPO, "*.py"):
        relative = path.relative_to(REPO)
        if (
            relative.as_posix() == ELIGIBILITY
            or _is_test_file(relative)
            or relative.parts[0] not in ("lib", "src", "nested", "patterns")
        ):
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        if "MALWARE_SCAN_FAILED" not in source and "QUARANTINED" not in source:
            continue
        for line, names in _collections(ast.parse(source)):
            if names == declared:
                found.append(f"{relative}:{line} lists {sorted(names)}")
    return found


def test_the_malware_refusals_are_declared_once():
    assert len(_declared_refusals()) >= 2
    assert malware_lists_outside_eligibility() == [], (
        f"the malware gate's refusals are declared in {ELIGIBILITY} "
        f"({REFUSALS}); import that set instead of writing the statuses again"
    )
