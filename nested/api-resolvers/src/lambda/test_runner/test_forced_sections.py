# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
The test runner accepts a forced section grouping only where it can mean one thing.

A section-scoped re-extract (``reextractTestSetDocument`` with a ``sectionId``)
delegates to this runner with ``documentSections``: the document's whole grouping,
encoded by ``idp_common.forced_sections``, with one section's class corrected. That
grouping describes exactly one document's ground truth, so the runner:

- forwards it verbatim to the copier on a draft-labeling run of exactly one document;
- refuses it on a scoring run, and on a draft-labeling run of zero or several
  documents, before anything is written or queued;
- refuses a value the pipeline's decoder would reject, as a bad request rather than a
  document failing minutes later in classification.

The run input mirrors what ``test_set_resolver`` builds for the delegated invoke.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from idp_common.forced_sections import (  # type: ignore
    ForcedSectionsError,
    decode_forced_sections,
    encode_forced_sections,
)

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TRACKING_TABLE", "tracking")
os.environ.setdefault("CONFIG_TABLE", "config")
os.environ.setdefault("FILE_COPY_QUEUE_URL", "https://sqs.example/queue")

OBJECT_KEY = "lending_package.pdf"

# Encoded by the same function the test-set resolver calls, so the fixture is the
# shape the runner actually receives rather than a hand-written approximation.
GROUPING = encode_forced_sections(
    [
        {"section_id": "1", "classification": "Payslip", "page_indices": [0, 1]},
        {"section_id": "2", "classification": "W2", "page_indices": [2]},
    ]
)


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
    """The resolver with AWS clients and lookups replaced."""
    module = _load("test_runner_index_forced_sections")
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


def _event(*, draft_labeling=True, object_keys=(OBJECT_KEY,), sections=GROUPING):
    run_input = {"testSetId": "lending", "context": "Draft labeling run"}
    if draft_labeling:
        run_input["draftLabeling"] = True
    if object_keys:
        run_input["objectKeys"] = list(object_keys)
    if sections is not None:
        run_input["documentSections"] = sections
    # No 'identity': the trusted service-to-service invoke the resolver makes.
    return {"info": {"fieldName": "startTestRun"}, "arguments": {"input": run_input}}


def _queued_body(index):
    return json.loads(index.sqs.send_message.call_args.kwargs["MessageBody"])


def _assert_nothing_happened(index):
    index._store_test_run_metadata.assert_not_called()
    index.sqs.send_message.assert_not_called()


@pytest.mark.unit
class TestAccepted:
    def test_a_one_document_draft_labeling_run_forwards_the_grouping(self, index):
        index.handler(_event(), None)

        body = _queued_body(index)
        # Verbatim: the copier stamps the string as object metadata and the
        # pipeline decodes it, so re-encoding here could only lose fidelity.
        assert body["documentSections"] == GROUPING
        assert body["purpose"] == "draft-labeling"
        assert body["objectKeys"] == [OBJECT_KEY]
        assert decode_forced_sections(body["documentSections"])[1] == {
            "section_id": "2",
            "classification": "W2",
            "page_indices": [2],
        }

    def test_a_run_without_a_grouping_queues_none(self, index):
        index.handler(_event(sections=None), None)

        assert "documentSections" not in _queued_body(index)

    def test_the_guard_leaves_an_ordinary_scoring_run_alone(self, index):
        """No grouping, no draft labeling, the whole set: the everyday run."""
        index.handler(_event(draft_labeling=False, object_keys=(), sections=None), None)

        body = _queued_body(index)
        assert "documentSections" not in body
        assert "purpose" not in body
        assert body["filesToProcess"] == 3


@pytest.mark.unit
class TestRefused:
    def test_a_scoring_run_is_refused(self, index):
        with pytest.raises(ValueError, match="only on a draft-labeling run"):
            index.handler(_event(draft_labeling=False), None)
        _assert_nothing_happened(index)

    @pytest.mark.parametrize(
        "object_keys",
        [(), (OBJECT_KEY, "other.pdf")],
        ids=["no-named-document", "two-documents"],
    )
    def test_a_draft_labeling_run_not_of_exactly_one_document_is_refused(
        self, index, object_keys
    ):
        with pytest.raises(ValueError, match="exactly one document"):
            index.handler(_event(object_keys=object_keys), None)
        _assert_nothing_happened(index)

    @pytest.mark.parametrize(
        "value",
        ["not json", '[["1","Payslip"]]', '[["1","Payslip","0-x"]]'],
        ids=["not-json", "entry-missing-pages", "malformed-pages"],
    )
    def test_a_grouping_the_pipeline_cannot_decode_is_a_bad_request(self, index, value):
        with pytest.raises(ValueError, match="Invalid documentSections") as raised:
            index.handler(_event(sections=value), None)
        # The decoder's own reason is kept, so the caller learns what was wrong.
        assert isinstance(raised.value.__cause__, ForcedSectionsError)
        _assert_nothing_happened(index)
