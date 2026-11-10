# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for Quick Start Agent tool logic (the *_impl functions)."""

import json
from unittest.mock import Mock, patch

import pytest

pytestmark = pytest.mark.unit


SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "Paystub",
    "x-aws-idp-document-type": "Paystub",
    "type": "object",
    "description": "pay",
    "properties": {
        "NetPay": {
            "type": "number",
            "description": "net",
            "x-aws-idp-evaluation-method": "NUMERIC_EXACT",
        }
    },
}


def _mock_bedrock(text):
    mc = Mock()
    mc.invoke_model.return_value = {}
    mc.extract_text_from_response.return_value = text
    return mc


class TestSearchCatalog:
    def test_empty_when_no_config_classes_or_schemas(self):
        from idp_common.agents.quick_start.tools import bootstrap_tools as bt

        with patch.object(bt, "_all_config_classes", return_value=[]):
            out = json.loads(bt.search_catalog_impl("employee paystub"))
        assert out["matched"] is False

    def test_indexes_config_classes(self):
        from idp_common.agents.quick_start.tools import bootstrap_tools as bt
        from idp_common.synthesis import catalog as catalog_mod

        captured = {}

        def _fake_match(prompt, entries, **_kw):
            captured["entries"] = entries
            return entries[0] if entries else None

        with (
            patch.object(bt, "_all_config_classes", return_value=[SCHEMA]),
            patch.object(catalog_mod, "match_catalog", side_effect=_fake_match),
        ):
            out = json.loads(bt.search_catalog_impl("paystub"))

        # The user's existing Paystub class was indexed into the catalog.
        assert [e.name for e in captured["entries"]] == ["Paystub"]
        assert out["matched"] is True
        assert out["name"] == "Paystub"

    def test_config_load_failure_degrades(self):
        from idp_common.agents.quick_start.tools import bootstrap_tools as bt

        # _all_config_classes swallows errors and returns [] -> empty catalog.
        with patch.object(bt, "_all_config_classes", return_value=[]):
            out = json.loads(bt.search_catalog_impl("anything"))
        assert out["matched"] is False


class TestEstimateAndAvailability:
    def test_estimate_returns_json(self):
        from idp_common.agents.quick_start.tools import bootstrap_tools as bt

        out = json.loads(bt.estimate_generation_cost_impl(5, 7))
        assert out["documents"] == 5
        assert "estimated_usd_low" in out

    def test_availability_reports_unavailable(self):
        from idp_common.agents.quick_start.tools import bootstrap_tools as bt

        with patch.object(bt, "_generation_queue_url", return_value=None):
            assert "NOT available" in bt.check_generator_availability_impl()

    def test_availability_reports_available_when_extension_installed(self):
        from idp_common.agents.quick_start.tools import bootstrap_tools as bt

        with patch.object(bt, "_generation_queue_url", return_value="https://sqs/q"):
            assert "is available" in bt.check_generator_availability_impl()


class TestAuthorTool:
    def test_authors_schema(self):
        import idp_common.bedrock as bedrock_mod
        from idp_common.agents.quick_start.tools import bootstrap_tools as bt

        with patch.object(
            bedrock_mod, "BedrockClient", return_value=_mock_bedrock(json.dumps(SCHEMA))
        ):
            out = json.loads(bt.author_schema_from_prompt_impl("a paystub"))
            assert out["$id"] == "Paystub"


class TestRequestGenerationGuards:
    def test_blocks_when_extension_not_installed(self):
        from idp_common.agents.quick_start.tools import bootstrap_tools as bt

        with patch.object(bt, "_generation_queue_url", return_value=None):
            out = json.loads(
                bt.request_document_generation_impl(json.dumps(SCHEMA), "v1")
            )
            assert out["enqueued"] is False
            assert "not installed" in out["reason"]

    def test_enqueues_to_discovered_extension_queue(self):
        from idp_common.agents.quick_start.tools import bootstrap_tools as bt

        sent = {}

        def _capture(queue_url, message):
            sent["url"] = queue_url
            sent["body"] = message

        with (
            patch.object(bt, "_generation_queue_url", return_value="https://sqs/q"),
            patch.object(bt, "_enqueue_generation", side_effect=_capture),
        ):
            out = json.loads(
                bt.request_document_generation_impl(
                    json.dumps(SCHEMA), "v1", doc_count=4
                )
            )
        assert out["enqueued"] is True
        assert sent["url"] == "https://sqs/q"
        assert sent["body"]["targetVersion"] == "v1"
        assert sent["body"]["docCount"] == 4
        assert "NetPay" in sent["body"]["allowedFieldNames"]

    def test_queue_url_from_arn(self):
        from idp_common.agents.quick_start.tools import bootstrap_tools as bt

        url = bt._queue_url_from_arn(
            "arn:aws:sqs:us-east-1:123456789012:idp-feature-BootstrapQueue"
        )
        assert url == (
            "https://sqs.us-east-1.amazonaws.com/123456789012/"
            "idp-feature-BootstrapQueue"
        )


class TestGenerateFromExistingConfig:
    def test_list_config_versions(self):
        from idp_common.agents.quick_start.tools import bootstrap_tools as bt

        mgr = Mock()
        mgr.list_config_versions.return_value = [
            {"versionName": "v1", "isActive": True, "description": "d"}
        ]
        mgr.get_raw_configuration.return_value = {"classes": [SCHEMA]}
        with patch(
            "idp_common.config.configuration_manager.ConfigurationManager",
            return_value=mgr,
        ):
            out = json.loads(bt.list_config_versions_impl())
        assert out["versions"][0]["versionName"] == "v1"
        assert out["versions"][0]["classes"] == ["Paystub"]

    def test_generate_from_existing_enqueues(self):
        from idp_common.agents.quick_start.tools import bootstrap_tools as bt

        mgr = Mock()
        mgr.get_raw_configuration.return_value = {"classes": [SCHEMA]}
        sent = {}

        def _capture(queue_url, message):
            sent["body"] = message

        with (
            patch(
                "idp_common.config.configuration_manager.ConfigurationManager",
                return_value=mgr,
            ),
            patch.object(bt, "_generation_queue_url", return_value="https://sqs/q"),
            patch.object(bt, "_enqueue_generation", side_effect=_capture),
        ):
            out = json.loads(
                bt.generate_from_existing_config_impl("v1", "Paystub", doc_count=2)
            )
        assert out["enqueued"] is True
        assert sent["body"]["targetVersion"] == "v1"
        assert sent["body"]["docCount"] == 2
        assert "NetPay" in sent["body"]["allowedFieldNames"]
        assert sent["body"]["preauthoredSchema"]["title"] == "Paystub"

    def test_generate_from_existing_missing_class(self):
        from idp_common.agents.quick_start.tools import bootstrap_tools as bt

        mgr = Mock()
        mgr.get_raw_configuration.return_value = {"classes": [SCHEMA]}
        with (
            patch(
                "idp_common.config.configuration_manager.ConfigurationManager",
                return_value=mgr,
            ),
            patch.object(bt, "_generation_queue_url", return_value="https://sqs/q"),
        ):
            out = json.loads(
                bt.generate_from_existing_config_impl("v1", "DoesNotExist")
            )
        assert out["enqueued"] is False
        assert "Paystub" in out["availableClasses"]


class TestListAvailableExtensions:
    def test_unavailable_when_table_env_missing(self, monkeypatch):
        from idp_common.agents.quick_start.tools import bootstrap_tools as bt

        monkeypatch.delenv("INSTALLED_FEATURES_TABLE", raising=False)
        out = json.loads(bt.list_available_extensions_impl())
        assert out["available"] is False
        assert out["extensions"] == []

    def test_lists_installed_extensions_sorted(self):
        from idp_common.agents.quick_start.tools import bootstrap_tools as bt

        rows = [
            {
                "featureId": "idp-data-generator",
                "displayName": "Test Set Generator",
                "installedVersion": "0.1.0",
                "featureApiEndpoint": "https://gen.example.com",
            },
            {
                "featureId": "idp-auto-optimizer",
                "displayName": "Auto Optimizer",
                "installedVersion": "0.1.3",
                "featureApiEndpoint": "https://tune.example.com",
            },
        ]
        with patch.object(bt, "_installed_features", return_value=rows):
            out = json.loads(bt.list_available_extensions_impl())

        assert out["available"] is True
        # sorted by displayName: "Auto Optimizer" < "Test Set Generator"
        assert [e["featureId"] for e in out["extensions"]] == [
            "idp-auto-optimizer",
            "idp-data-generator",
        ]

    def test_platform_disabled_degrades(self):
        from idp_common.agents.quick_start.tools import bootstrap_tools as bt

        # _installed_features returns None when the registry is unavailable
        # (env unset or the table read failed).
        with patch.object(bt, "_installed_features", return_value=None):
            out = json.loads(bt.list_available_extensions_impl())

        assert out["available"] is False
        assert out["extensions"] == []


class TestListSampleDocuments:
    def test_unavailable_when_bucket_env_missing(self, monkeypatch):
        from idp_common.agents.quick_start.tools import bootstrap_tools as bt

        monkeypatch.delenv("CONFIGURATION_BUCKET", raising=False)
        out = json.loads(bt.list_sample_documents_impl())
        assert out["available"] is False
        assert out["samples"] == []

    def test_reads_manifest_from_configuration_bucket(self, monkeypatch):
        from idp_common.agents.quick_start.tools import bootstrap_tools as bt

        monkeypatch.setenv("CONFIGURATION_BUCKET", "cfg-bucket")
        manifest = {
            "schemaVersion": "1.0",
            "samples": [
                {
                    "id": "lending_package",
                    "name": "Lending Package",
                    "kind": "document",
                },
                {"id": "w2", "name": "W-2 Forms", "kind": "batch", "fileCount": 20},
            ],
        }

        class _Body:
            def read(self):
                return json.dumps(manifest).encode("utf-8")

        class _S3:
            def get_object(self, Bucket, Key):
                assert Bucket == "cfg-bucket"
                assert Key == "config_library/samples-manifest.json"
                return {"Body": _Body()}

        with patch("boto3.client", return_value=_S3()):
            out = json.loads(bt.list_sample_documents_impl())

        assert out["available"] is True
        assert [s["id"] for s in out["samples"]] == ["lending_package", "w2"]

    def test_missing_manifest_degrades_gracefully(self, monkeypatch):
        from idp_common.agents.quick_start.tools import bootstrap_tools as bt

        monkeypatch.setenv("CONFIGURATION_BUCKET", "cfg-bucket")

        class _S3:
            def get_object(self, Bucket, Key):
                raise Exception("NoSuchKey")

        with patch("boto3.client", return_value=_S3()):
            out = json.loads(bt.list_sample_documents_impl())

        assert out["available"] is False
        assert out["samples"] == []
