# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Code that was removed is gone, and stays gone.

**Two BDA branches no state can reach.** The BDA Invoke handler used to answer
its task token itself, without starting a BDA job, for a document that already
had extraction results; the BDA assembly handler had a branch for that answer.
The state machine sends a document that has its data past both (it goes to the
assembly of the existing document), so neither ran. The source checks keep them
from coming back; the behaviour case shows what the Invoke handler does with
such a document now: it starts the job.

**The SDK's client-side rerun.** ``idp_sdk._core.rerun_processor`` cleared a
document's outputs and queued it from the caller's machine, with its own copy of
the checks the server makes. The SDK asks the stack's reprocess resolver, so the
module is gone and nothing names its class.
"""

from __future__ import annotations

import ast
import importlib.util
from unittest.mock import MagicMock, patch

import pytest

from idp_common.models import Document, Section, Status
from tests.unit.lambdas._lambda_loader import REPO_ROOT, load_lambda

pytestmark = pytest.mark.unit

SRC = REPO_ROOT / "patterns" / "unified" / "src"

# Named by its path from the repository root: that spelling is what puts the
# module under the region-free import gate
# (patterns/unified/tests/test_handler_imports_are_region_free.py).
invoke_index = load_lambda(
    "bda_invoke_removed_index", "patterns/unified/src/bda_invoke_function"
)
INVOKE = SRC / "bda_invoke_function" / "index.py"
ASSEMBLY = SRC / "bda_processresults_function" / "index.py"


def _function(path, name) -> ast.FunctionDef:
    tree = ast.parse(path.read_text())
    found = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    assert len(found) == 1, f"{path.name} defines {name} {len(found)} times"
    return found[0]


def _strings(node) -> set:
    return {
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _called_attributes(node) -> set:
    return {
        n.func.attr
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }


class TestTheSource:
    def test_the_invoke_handler_never_answers_the_task_token_itself(self):
        tree = ast.parse(INVOKE.read_text())

        assert "send_task_success" not in _called_attributes(tree)
        assert "skipped" not in _strings(_function(INVOKE, "handler"))

    def test_the_invoke_handler_starts_a_job_on_every_path(self):
        """No return comes before the call that starts the BDA job."""
        handler = _function(INVOKE, "handler")
        statements = handler.body[0].body  # the body of its try
        position = {
            "job": next(
                i
                for i, node in enumerate(statements)
                if "invoke_data_automation"
                in {
                    n.func.id
                    for n in ast.walk(node)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                }
            )
        }
        early_returns = [
            node
            for statement in statements[: position["job"]]
            for node in ast.walk(statement)
            if isinstance(node, ast.Return)
        ]
        assert early_returns == []

    def test_the_assembly_handler_has_no_skipped_branch(self):
        handler = _function(ASSEMBLY, "handler")

        assert "skipped" not in _strings(handler)
        # What it still reads from a response's metadata is the blueprint-change
        # format's location fields.
        assert "processresults_skip" not in _strings(handler)


class TestTheInvokeHandler:
    def test_a_document_that_has_results_still_starts_a_bda_job(self, monkeypatch):
        document = Document(
            id="claims/packet.pdf",
            input_bucket="in",
            input_key="claims/packet.pdf",
            output_bucket="out",
            status=Status.RUNNING,
            num_pages=1,
            sections=[
                Section(
                    section_id="1",
                    classification="Invoice",
                    page_ids=["1"],
                    extraction_result_uri="s3://out/claims/packet.pdf/sections/1/result.json",
                )
            ],
        )
        started = []

        def invoke_data_automation(**kwargs):
            started.append(kwargs)
            return {"invocationArn": "arn:job"}

        table = MagicMock()
        monkeypatch.setattr(
            invoke_index, "invoke_data_automation", invoke_data_automation
        )
        monkeypatch.setattr(invoke_index, "get_tracking_table", lambda: table)
        with patch.object(invoke_index.boto3, "client") as new_client:
            response = invoke_index.handler(
                {
                    "working_bucket": "",
                    "document": document.to_dict(),
                    "BDAProjectArn": "arn:project",
                    "taskToken": "token-1",
                },
                None,
            )

        assert started == [
            {
                "data_project_arn": "arn:project",
                "input_s3_uri": "s3://in/claims/packet.pdf",
                "output_s3_uri": "s3:///claims/packet.pdf/bda_responses",
            }
        ]
        # The job answers the token when it ends; the handler recorded it.
        (recorded,) = table.put_item.call_args_list
        assert recorded.kwargs["Item"]["PK"] == "tasktoken#claims/packet.pdf"
        assert recorded.kwargs["Item"]["TaskToken"] == "token-1"
        assert response["bda_response"] == {"invocationArn": "arn:job"}
        assert "skipped" not in response
        # No client is built to answer the token from here.
        new_client.assert_not_called()

    def test_the_tracking_table_is_built_once_and_on_first_use(self, monkeypatch):
        """Nothing regional is built when the module loads, and a warm
        invocation reuses what the first one built."""
        resource = MagicMock()
        monkeypatch.setenv("TRACKING_TABLE", "tracking")
        monkeypatch.setattr(invoke_index, "_tracking_table", None)
        monkeypatch.setattr(invoke_index.boto3, "resource", resource)

        first = invoke_index.get_tracking_table()
        second = invoke_index.get_tracking_table()

        resource.assert_called_once_with("dynamodb")
        resource.return_value.Table.assert_called_once_with("tracking")
        assert first is second is resource.return_value.Table.return_value


#: Directories that hold build output or installed packages, not source.
_NOT_SOURCE = {
    "build",
    "dist",
    "node_modules",
    "test-reports",
    ".aws-sam",
    ".venv",
    "__pycache__",
}


def _python_sources(*roots: str):
    for root in roots:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            parts = path.relative_to(REPO_ROOT).parts
            if _NOT_SOURCE.intersection(parts) or any(
                part.endswith(".egg-info") for part in parts
            ):
                continue
            yield path


class TestTheClientSideRerun:
    def test_the_module_cannot_be_imported(self):
        assert importlib.util.find_spec("idp_sdk._core") is not None
        assert importlib.util.find_spec("idp_sdk._core.rerun_processor") is None

    def test_no_source_file_names_its_class(self):
        this_file = (
            REPO_ROOT
            / "lib/idp_common_pkg/tests/unit/lambdas"
            / ("test_removed_code_stays_removed.py")
        )
        read = 0
        naming = []
        for path in _python_sources("lib", "src"):
            read += 1
            if path != this_file and "RerunProcessor" in path.read_text(
                encoding="utf-8", errors="ignore"
            ):
                naming.append(str(path.relative_to(REPO_ROOT)))
        assert read > 500, "the walk found too few files to mean anything"
        assert naming == []
