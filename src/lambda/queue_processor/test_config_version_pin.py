# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""start_workflow() pins the configuration version before starting an execution.

`queue_processor` is the single chokepoint every document execution passes
through, so it is where the pin is made non-optional. Historically the pin was
set only when the uploader supplied `config-version` S3 metadata (or when
`queue_sender` managed to resolve it), so a document could reach the workflow
unpinned — and then every downstream consumer re-resolved the active version for
itself, each with its own filtered-scan bug. That is how issue #599 presented:
the queue sender failed to stamp a version, so the pipeline-hooks dispatcher fell
back to its own broken scan, read `Config#default`, and silently disabled every
registered hook.

Pinning here collapses N independent resolutions into one recorded value.
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_INDEX_PATH = os.path.join(os.path.dirname(__file__), "index.py")
_MODULE_NAME = "queue_processor_index_pin_test"


@pytest.fixture
def index_module(monkeypatch):
    """Import index with idp_common + boto3 mocked out."""
    env_vars = {
        "CONCURRENCY_TABLE": "test-concurrency",
        "STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:t",
        "MAX_CONCURRENT": "5",
        "CONFIG_TABLE": "test-config-table",
        "WORKING_BUCKET": "test-working-bucket",
    }

    fake_models = MagicMock()
    fake_docs_service = MagicMock()
    fake_docs_service.create_document_service = MagicMock(return_value=MagicMock())
    fake_config = MagicMock()

    fake_xray_core = MagicMock()
    fake_xray_core.xray_recorder = MagicMock()
    fake_xray_core.patch_all = MagicMock()

    module_patches = {
        "idp_common": MagicMock(),
        "idp_common.models": fake_models,
        "idp_common.docs_service": fake_docs_service,
        "idp_common.config": fake_config,
        "aws_xray_sdk": MagicMock(),
        "aws_xray_sdk.core": fake_xray_core,
    }
    for name, mod in module_patches.items():
        monkeypatch.setitem(sys.modules, name, mod)

    with (
        patch.dict(os.environ, env_vars, clear=False),
        patch("boto3.resource") as mock_resource,
        patch("boto3.client") as mock_client,
    ):
        mock_resource.return_value.Table.return_value = MagicMock()
        mock_client.return_value = MagicMock()

        spec = importlib.util.spec_from_file_location(_MODULE_NAME, _INDEX_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE_NAME] = module
        spec.loader.exec_module(module)

        module.sfn = MagicMock()
        module.sfn.start_execution.return_value = {
            "executionArn": "arn:aws:states:us-east-1:1:execution:t:e"
        }
        yield module
        sys.modules.pop(_MODULE_NAME, None)


class _Doc:
    """Minimal document stand-in. serialize_document mirrors the real wrapper,
    which carries config_version so consumers need not decompress."""

    def __init__(self, config_version=None, config_revision=None):
        self.id = "w2.pdf"
        self.config_version = config_version
        self.config_revision = config_revision
        self.status = None
        self.start_time = None
        self.workflow_execution_arn = None

    def serialize_document(self, bucket, prefix, logger):
        return {
            "document_id": self.id,
            "s3_uri": f"s3://{bucket}/compressed_documents/{self.id}/1.json",
            "config_version": self.config_version,
            "config_revision": self.config_revision,
            "compressed": True,
        }

    def to_dict(self):
        return {"id": self.id, "config_version": self.config_version}


def _mock_manager(
    index_module, active_version="claims-pack-v0.4.0", use_bda=False, published_revision=None
):
    """Patch ConfigurationManager so resolve_active_version returns a known value."""
    manager = MagicMock()
    manager.resolve_active_version.return_value = active_version
    manager.resolve_published_revision.return_value = published_revision
    manager.get_merged_configuration.return_value = MagicMock(use_bda=use_bda)
    index_module.ConfigurationManager = MagicMock(return_value=manager)
    return manager


def _sfn_input(index_module):
    """The document payload actually handed to Step Functions."""
    kwargs = index_module.sfn.start_execution.call_args.kwargs
    return json.loads(kwargs["input"])["document"]


@pytest.mark.unit
class TestConfigVersionPin:
    def test_unpinned_document_is_stamped_with_the_active_version(self, index_module):
        """The core change: a document that arrives with no config_version leaves
        with one, so no downstream consumer has to resolve it."""
        doc = _Doc(config_version=None)
        manager = _mock_manager(index_module, "claims-pack-v0.4.0")

        index_module.start_workflow(doc)

        assert doc.config_version == "claims-pack-v0.4.0"
        manager.resolve_active_version.assert_called_once()
        # And it reaches the workflow, which is what the dispatcher reads.
        assert _sfn_input(index_module)["config_version"] == "claims-pack-v0.4.0"

    def test_an_existing_pin_is_never_overwritten(self, index_module):
        """A version chosen at upload time (or carried through a HITL reprocess)
        must win — re-resolving would silently move the document to whatever is
        active now."""
        doc = _Doc(config_version="pinned-v1.0.0")
        manager = _mock_manager(index_module, "claims-pack-v0.4.0")

        index_module.start_workflow(doc)

        assert doc.config_version == "pinned-v1.0.0"
        manager.resolve_active_version.assert_not_called()
        assert _sfn_input(index_module)["config_version"] == "pinned-v1.0.0"

    def test_the_published_revision_is_pinned_alongside_the_version(self, index_module):
        """
        Without a revision pin, a save made while the document is in flight would
        change the configuration under it mid-pipeline — extraction on r7 and
        assessment on r8 — and the result would match no single configuration.
        """
        doc = _Doc(config_version=None)
        manager = _mock_manager(index_module, "claims-pack-v0.4.0", published_revision=7)

        index_module.start_workflow(doc)

        assert doc.config_revision == 7
        manager.resolve_published_revision.assert_called_once_with("claims-pack-v0.4.0")
        assert _sfn_input(index_module)["config_revision"] == 7

    def test_a_profile_with_no_history_pins_no_revision(self, index_module):
        """An older deployment, or a profile untouched since the upgrade: consumers
        fall back to the profile head exactly as before."""
        doc = _Doc(config_version=None)
        _mock_manager(index_module, "default", published_revision=None)

        index_module.start_workflow(doc)

        assert doc.config_revision is None
        assert _sfn_input(index_module)["config_revision"] is None

    def test_an_existing_revision_pin_is_never_overwritten(self, index_module):
        """A revision chosen at upload time, or carried through a reprocess, wins."""
        doc = _Doc(config_version="lending", config_revision=3)
        manager = _mock_manager(index_module, "lending", published_revision=9)

        index_module.start_workflow(doc)

        assert doc.config_revision == 3
        manager.resolve_published_revision.assert_not_called()

    def test_an_unusable_revision_degrades_to_no_pin(self, index_module):
        """The pin is serialized into the Step Functions input, so a non-numeric
        answer must not break the workflow start."""
        doc = _Doc(config_version=None)
        _mock_manager(index_module, "lending", published_revision="not-a-number")

        index_module.start_workflow(doc)

        assert doc.config_revision is None
        assert index_module.sfn.start_execution.called

    def test_no_active_version_pins_default_rather_than_failing(self, index_module):
        """A freshly deployed stack writes Config#default with no IsActive
        attribute, so "nothing active" is normal. resolve_active_version returns
        'default' for it and the document must still process."""
        doc = _Doc(config_version=None)
        _mock_manager(index_module, "default")

        index_module.start_workflow(doc)

        assert doc.config_version == "default"
        index_module.sfn.start_execution.assert_called_once()

    def test_resolution_failure_does_not_fail_the_document(self, index_module):
        """A throttled ConfigurationManager must not stop the execution — the
        pin is an improvement on the status quo, not a new failure mode."""
        doc = _Doc(config_version=None)
        manager = MagicMock()
        manager.resolve_active_version.side_effect = RuntimeError("throttled")
        manager.get_merged_configuration.return_value = MagicMock(use_bda=False)
        index_module.ConfigurationManager = MagicMock(return_value=manager)

        index_module.start_workflow(doc)

        assert doc.config_version is None  # degrades to the historical behavior
        index_module.sfn.start_execution.assert_called_once()

    def test_routing_flags_come_from_the_pinned_version(self, index_module):
        """The use_bda/bda_project_arn lookup must read the SAME version that was
        just pinned, or the document could route as BDA under one config while
        the rest of the pipeline uses another."""
        doc = _Doc(config_version=None)
        manager = _mock_manager(index_module, "bda-v2.0.0", use_bda=True)
        manager.get_bda_project_arn.return_value = "arn:aws:bedrock:::project/p"

        index_module.start_workflow(doc)

        manager.get_merged_configuration.assert_called_with("bda-v2.0.0")
        manager.get_bda_project_arn.assert_called_with("bda-v2.0.0")
        payload = _sfn_input(index_module)
        assert payload["use_bda"] is True
        assert payload["bda_project_arn"] == "arn:aws:bedrock:::project/p"

    def test_no_config_table_skips_pinning_without_error(self, index_module):
        """CONFIG_TABLE unset (a degraded but survivable deploy) must not raise."""
        doc = _Doc(config_version=None)
        _mock_manager(index_module)

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONFIG_TABLE", None)
            index_module.start_workflow(doc)

        assert doc.config_version is None
        index_module.sfn.start_execution.assert_called_once()
