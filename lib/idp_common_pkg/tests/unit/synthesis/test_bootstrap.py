# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for synthesis.bootstrap orchestration (no AWS, generator stubbed)."""

import json
from unittest.mock import Mock

import pytest

from idp_common.synthesis import bootstrap

pytestmark = pytest.mark.unit


VALID_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "Paystub",
    "x-aws-idp-document-type": "Paystub",
    "type": "object",
    "description": "Employee pay statement",
    "properties": {
        "EmployeeName": {
            "type": "string",
            "description": "Name",
            "x-aws-idp-evaluation-method": "FUZZY",
        },
        "NetPay": {
            "type": "number",
            "description": "Net pay",
            "x-aws-idp-evaluation-method": "NUMERIC_EXACT",
        },
    },
}


def _bedrock_returning_schema():
    client = Mock()
    client.invoke_model.return_value = {}
    client.extract_text_from_response.return_value = json.dumps(VALID_SCHEMA)
    return client


class _FakeConfigManager:
    def __init__(self):
        self.saved = {}

    def get_raw_configuration(self, config_type, version):
        return self.saved.get(version)

    def list_config_versions(self, raise_errors=False):
        return [{"versionName": name, "managed": False} for name in self.saved]

    def save_raw_configuration(
        self, config_type, config_dict, version, description=None
    ):
        self.saved[version] = config_dict


class TestResolveSchema:
    def test_pure_llm_when_no_catalog(self):
        req = bootstrap.BootstrapRequest(prompt="a paystub")
        schema, tier, matched = bootstrap.resolve_schema(
            req, config_classes=None, bedrock_client=_bedrock_returning_schema()
        )
        assert tier == "pure_llm"
        assert matched is None
        assert schema["$id"] == "Paystub"

    def test_user_docs_pending_when_examples_present(self):
        req = bootstrap.BootstrapRequest(prompt="x", example_doc_keys=["doc.pdf"])
        schema, tier, _ = bootstrap.resolve_schema(req, bedrock_client=Mock())
        assert tier == "user_docs_pending"
        assert schema is None

    def test_catalog_adapt_when_match(self):
        req = bootstrap.BootstrapRequest(prompt="a pay statement")
        config_classes = [
            {
                "x-aws-idp-document-type": "Paystub",
                "description": "pay",
                "type": "object",
                "properties": {"NetPay": {"type": "number"}},
            }
        ]
        client = Mock()
        client.invoke_model.return_value = {}
        client.extract_text_from_response.side_effect = [
            json.dumps({"index": 0, "confidence": 0.9, "reason": "match"}),
            json.dumps(VALID_SCHEMA),
        ]
        schema, tier, matched = bootstrap.resolve_schema(
            req, config_classes=config_classes, bedrock_client=client
        )
        assert tier == "catalog_adapt"
        assert matched == "Paystub"


class TestMergeClassIntoVersion:
    def test_creates_and_dedups(self):
        cm = _FakeConfigManager()
        bootstrap.merge_class_into_version(VALID_SCHEMA, "v1", config_manager=cm)
        assert len(cm.saved["v1"]["classes"]) == 1
        bootstrap.merge_class_into_version(VALID_SCHEMA, "v1", config_manager=cm)
        assert len(cm.saved["v1"]["classes"]) == 1


class TestProtectedProfiles:
    @staticmethod
    def _manager(versions):
        cm = Mock()
        cm.list_config_versions.return_value = versions
        cm.get_raw_configuration.return_value = {"classes": []}
        return cm

    def test_default_is_refused(self):
        cm = self._manager([{"versionName": "default", "managed": False}])
        with pytest.raises(bootstrap.ProtectedProfileError, match="'default'"):
            bootstrap.merge_class_into_version(
                VALID_SCHEMA, "default", config_manager=cm
            )
        cm.save_raw_configuration.assert_not_called()

    def test_a_stack_managed_profile_is_refused(self):
        cm = self._manager([{"versionName": "docsplit", "managed": True}])
        with pytest.raises(bootstrap.ProtectedProfileError, match="stack-managed"):
            bootstrap.merge_class_into_version(
                VALID_SCHEMA, "docsplit", config_manager=cm
            )
        cm.save_raw_configuration.assert_not_called()

    def test_an_unreadable_profile_listing_writes_nothing(self):
        cm = self._manager([])
        cm.list_config_versions.side_effect = RuntimeError("scan throttled")
        with pytest.raises(RuntimeError, match="scan throttled"):
            bootstrap.merge_class_into_version(
                VALID_SCHEMA, "docsplit", config_manager=cm
            )
        cm.list_config_versions.assert_called_once_with(raise_errors=True)
        cm.save_raw_configuration.assert_not_called()

    def test_a_protected_target_is_refused_before_any_model_call(self):
        cm = self._manager([])
        bedrock = Mock()
        req = bootstrap.BootstrapRequest(prompt="a paystub", target_version="default")
        result = bootstrap.run_bootstrap(req, config_manager=cm, bedrock_client=bedrock)
        assert not result.success
        assert result.error and "'default'" in result.error
        bedrock.invoke_model.assert_not_called()
        cm.save_raw_configuration.assert_not_called()

    @pytest.mark.parametrize(
        "versions",
        [[], [{"versionName": "team-invoices", "managed": False}]],
        ids=["new profile", "existing user profile"],
    )
    def test_a_user_profile_is_written(self, versions):
        cm = self._manager(versions)
        bootstrap.merge_class_into_version(
            VALID_SCHEMA, "team-invoices", config_manager=cm
        )
        cm.save_raw_configuration.assert_called_once()


class TestSetProcessingOptions:
    @staticmethod
    def _profile(**config):
        cm = _FakeConfigManager()
        cm.saved["invoices"] = {"classes": [VALID_SCHEMA], **config}
        return cm

    @pytest.mark.parametrize(
        ("documents_per_file", "splitting"),
        [("one", "disabled"), ("several", "llm_determined"), ("one_per_page", "page")],
    )
    def test_documents_per_file_sets_section_splitting(
        self, documents_per_file, splitting
    ):
        cm = self._profile()
        applied = bootstrap.set_processing_options(
            "invoices", config_manager=cm, documents_per_file=documents_per_file
        )
        saved = cm.saved["invoices"]
        assert (
            saved["classification"]["document_shape"]["sectionSplitting"] == splitting
        )
        assert applied == {"classification.document_shape.sectionSplitting": splitting}
        assert "summarization" not in saved
        assert saved["classes"] == [VALID_SCHEMA]

    def test_summaries_set_only_summarization(self):
        cm = self._profile()
        bootstrap.set_processing_options("invoices", config_manager=cm, summaries=False)
        saved = cm.saved["invoices"]
        assert saved["summarization"] == {"enabled": False}
        assert "classification" not in saved

    @pytest.mark.parametrize(
        ("long_tables", "mode"), [(True, "advanced"), (False, "simple")]
    )
    def test_long_tables_sets_the_extraction_mode(self, long_tables, mode):
        cm = self._profile(extraction={"model": "m"})
        applied = bootstrap.set_processing_options(
            "invoices", config_manager=cm, long_tables=long_tables
        )
        assert cm.saved["invoices"]["extraction"] == {"model": "m", "mode": mode}
        assert applied == {"extraction.mode": mode}

    def test_other_classification_settings_are_kept(self):
        cm = self._profile(
            classification={"model": "m", "document_shape": {"pageOrder": "scan"}}
        )
        bootstrap.set_processing_options(
            "invoices", config_manager=cm, documents_per_file="one"
        )
        classification = cm.saved["invoices"]["classification"]
        assert classification["model"] == "m"
        assert classification["document_shape"] == {
            "pageOrder": "scan",
            "sectionSplitting": "disabled",
        }

    def test_a_flat_section_splitting_key_is_given_the_same_value(self):
        """The flat key wins when the profile is read, so leaving it would undo this."""
        cm = self._profile(classification={"sectionSplitting": "llm_determined"})
        bootstrap.set_processing_options(
            "invoices", config_manager=cm, documents_per_file="one"
        )
        classification = cm.saved["invoices"]["classification"]
        assert classification["sectionSplitting"] == "disabled"
        assert classification["document_shape"]["sectionSplitting"] == "disabled"

    @pytest.mark.parametrize(
        ("version", "listed", "match"),
        [
            ("default", [], "'default'"),
            (
                "docsplit",
                [{"versionName": "docsplit", "managed": True}],
                "stack-managed",
            ),
        ],
    )
    def test_a_protected_profile_is_refused(self, version, listed, match):
        cm = Mock()
        cm.list_config_versions.return_value = listed
        with pytest.raises(bootstrap.ProtectedProfileError, match=match):
            bootstrap.set_processing_options(
                version, config_manager=cm, documents_per_file="one"
            )
        cm.save_raw_configuration.assert_not_called()

    def test_a_profile_that_does_not_exist_is_not_created(self):
        cm = _FakeConfigManager()
        with pytest.raises(ValueError, match="does not exist"):
            bootstrap.set_processing_options(
                "missing", config_manager=cm, documents_per_file="one"
            )
        assert cm.saved == {}

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [({"documents_per_file": "two"}, "must be one of"), ({}, "Nothing to set")],
    )
    def test_an_invalid_request_writes_nothing(self, kwargs, match):
        cm = self._profile()
        before = json.dumps(cm.saved, sort_keys=True)
        with pytest.raises(ValueError, match=match):
            bootstrap.set_processing_options("invoices", config_manager=cm, **kwargs)
        assert json.dumps(cm.saved, sort_keys=True) == before


class TestRunBootstrap:
    def test_config_only_when_generator_unavailable(self, monkeypatch):
        from idp_common.synthesis import engine

        monkeypatch.setattr(engine, "generator_available", lambda: (False, "no module"))
        cm = _FakeConfigManager()
        req = bootstrap.BootstrapRequest(prompt="a paystub", doc_count=2)
        result = bootstrap.run_bootstrap(
            req,
            config_manager=cm,
            test_set_bucket="bucket",
            bedrock_client=_bedrock_returning_schema(),
        )
        assert result.success
        assert result.generator_available is False
        assert result.config_version == "bootstrap-paystub"
        assert result.test_set_id is None
        assert "bootstrap-paystub" in cm.saved

    def test_full_loop_with_stubbed_generator(self, monkeypatch, tmp_path):
        from idp_common.synthesis import engine

        packet_dir = tmp_path / "packet"
        input_dir = packet_dir / "input"
        input_dir.mkdir(parents=True)
        (input_dir / "doc_001.pdf").write_bytes(b"%PDF fake")
        sect = packet_dir / "baseline" / "doc_001.pdf" / "sections" / "1"
        sect.mkdir(parents=True)
        (sect / "result.json").write_text(
            json.dumps(
                {
                    "document_class": {"type": "Paystub"},
                    "split_document": {"page_indices": [0]},
                    "inference_result": {"EmployeeName": "Jane", "NetPay": 100},
                }
            )
        )

        monkeypatch.setattr(engine, "generator_available", lambda: (True, ""))
        monkeypatch.setattr(
            engine,
            "synthesize",
            lambda job, status_cb=None: engine.SynthesisResult(
                success=True,
                packet_dir=str(packet_dir),
                docs_completed=1,
                docs_requested=1,
            ),
        )
        s3 = Mock()
        cm = _FakeConfigManager()
        req = bootstrap.BootstrapRequest(prompt="a paystub", doc_count=1)
        result = bootstrap.run_bootstrap(
            req,
            config_manager=cm,
            test_set_bucket="bucket",
            bedrock_client=_bedrock_returning_schema(),
            s3_client=s3,
        )
        assert result.success
        assert result.field_validation_ok is True
        assert result.test_set_id == "bootstrap-paystub"
        assert result.docs_generated == 1
        s3.upload_file.assert_called_once()

    def test_field_drift_is_pruned_not_fatal(self, monkeypatch, tmp_path):
        from idp_common.synthesis import engine

        packet_dir = tmp_path / "packet"
        input_dir = packet_dir / "input"
        input_dir.mkdir(parents=True)
        (input_dir / "doc_001.pdf").write_bytes(b"%PDF fake")
        sect = packet_dir / "baseline" / "doc_001.pdf" / "sections" / "1"
        sect.mkdir(parents=True)
        (sect / "result.json").write_text(
            json.dumps(
                {
                    "document_class": {"type": "Paystub"},
                    "split_document": {"page_indices": [0]},
                    "inference_result": {"EmployeeName": "Jane", "WrongFieldName": "x"},
                }
            )
        )
        monkeypatch.setattr(engine, "generator_available", lambda: (True, ""))
        monkeypatch.setattr(
            engine,
            "synthesize",
            lambda job, status_cb=None: engine.SynthesisResult(
                success=True, packet_dir=str(packet_dir), docs_completed=1
            ),
        )
        s3 = Mock()
        cm = _FakeConfigManager()
        req = bootstrap.BootstrapRequest(prompt="a paystub", doc_count=1)
        result = bootstrap.run_bootstrap(
            req,
            config_manager=cm,
            test_set_bucket="bucket",
            bedrock_client=_bedrock_returning_schema(),
            s3_client=s3,
        )
        assert result.success
        assert result.field_validation_ok is True
        assert "WrongFieldName" in result.field_validation_extra
        assert result.test_set_id == "bootstrap-paystub"
        assert result.docs_generated == 1
        s3.upload_file.assert_called_once()
