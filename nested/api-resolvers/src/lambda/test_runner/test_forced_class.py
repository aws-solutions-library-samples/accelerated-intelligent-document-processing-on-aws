# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
The test runner validates a forced ``documentClass`` before forwarding it (#1728).

A whole-document re-extract (``reextractTestSetDocument`` with a ``documentClass``)
delegates to this runner, which hands the class to the copier to stamp as S3 object
user metadata. The resolver validates it, but ``startTestRun`` is reachable on its
own (by an Admin or Author, and by a direct invoke), and it forwarded whatever it
was given: a value S3 metadata cannot carry failed later in the copier, and a class
forced onto a scoring run, or onto many documents at once, replaced classification
for all of them. So the runner:

- forwards a well-formed class on a draft-labeling run of exactly one named document;
- refuses a malformed one, a scoring run, a run of zero or several documents, and a
  class sent together with a forced section grouping, before anything is written or
  queued.

The run input mirrors what ``test_set_resolver`` builds for the delegated invoke.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from idp_common.forced_sections import encode_forced_sections  # type: ignore

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TRACKING_TABLE", "tracking")
os.environ.setdefault("CONFIG_TABLE", "config")
os.environ.setdefault("FILE_COPY_QUEUE_URL", "https://sqs.example/queue")

OBJECT_KEY = "lending_package.pdf"


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def index():
    """The runner with AWS clients and lookups replaced."""
    module = _load("test_runner_index_forced_class")
    module.sqs = MagicMock()
    module.dynamodb = MagicMock()
    module._get_test_set = MagicMock(
        return_value={"name": "lending", "fileCount": 3, "latestVersion": 0}
    )
    module._active_config_version = MagicMock(return_value="default")
    module._capture_config = MagicMock(return_value={"Config": {"notes": "x"}})
    module._published_revision = MagicMock(return_value=1)
    module._store_test_run_metadata = MagicMock()
    return module


def _event(document_class, *, draft_labeling=True, object_keys=(OBJECT_KEY,)):
    run_input = {"testSetId": "lending", "context": "Draft labeling run"}
    if draft_labeling:
        run_input["draftLabeling"] = True
    if object_keys:
        run_input["objectKeys"] = list(object_keys)
    if document_class is not None:
        run_input["documentClass"] = document_class
    return {"info": {"fieldName": "startTestRun"}, "arguments": {"input": run_input}}


def _assert_nothing_happened(index):
    index._store_test_run_metadata.assert_not_called()
    index.sqs.send_message.assert_not_called()


@pytest.mark.unit
class TestAccepted:
    @pytest.mark.parametrize("value", ["W2", "Bank Statement", "PA-Claims_Evidence"])
    def test_a_one_document_draft_labeling_run_forwards_the_class(self, index, value):
        index.handler(_event(value), None)

        body = json.loads(index.sqs.send_message.call_args.kwargs["MessageBody"])
        assert body["documentClass"] == value

    @pytest.mark.parametrize("value", [None, ""], ids=["absent", "empty"])
    def test_no_class_is_no_class_on_any_run(self, index, value):
        """Absence means "classify as usual", including on a scoring run."""
        index.handler(_event(value, draft_labeling=False, object_keys=()), None)

        body = json.loads(index.sqs.send_message.call_args.kwargs["MessageBody"])
        assert "documentClass" not in body


@pytest.mark.unit
class TestRefused:
    @pytest.mark.parametrize(
        "value",
        [
            "../../etc/passwd",
            "Bank Statement\n",
            "Bank\tStatement",
            "Relevé",
            "x" * 101,
            ["W2"],
            7,
        ],
        ids=["path", "trailing-newline", "tab", "non-ascii", "too-long", "list", "int"],
    )
    def test_a_malformed_class_is_a_bad_request(self, index, value):
        with pytest.raises(ValueError, match="Invalid documentClass"):
            index.handler(_event(value), None)
        _assert_nothing_happened(index)

    def test_a_scoring_run_is_refused(self, index):
        with pytest.raises(ValueError, match="only on a draft-labeling run"):
            index.handler(_event("W2", draft_labeling=False), None)
        _assert_nothing_happened(index)

    @pytest.mark.parametrize(
        "object_keys",
        [(), (OBJECT_KEY, "other.pdf")],
        ids=["no-named-document", "two-documents"],
    )
    def test_a_run_not_of_exactly_one_document_is_refused(self, index, object_keys):
        with pytest.raises(ValueError, match="exactly one document"):
            index.handler(_event("W2", object_keys=object_keys), None)
        _assert_nothing_happened(index)

    def test_a_class_with_a_forced_grouping_is_refused(self, index):
        """The grouping carries every section's class; a second, whole-document
        class beside it has no single meaning."""
        event = _event("W2")
        event["arguments"]["input"]["documentSections"] = encode_forced_sections(
            [{"section_id": "1", "classification": "Payslip", "page_indices": [0]}]
        )

        with pytest.raises(ValueError, match="not both"):
            index.handler(event, None)
        _assert_nothing_happened(index)
