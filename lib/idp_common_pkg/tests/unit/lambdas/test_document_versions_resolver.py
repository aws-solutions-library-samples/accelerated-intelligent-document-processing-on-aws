# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the document_versions_resolver Lambda."""

import importlib
import json
import os
import sys
from unittest.mock import MagicMock

import pytest

LAMBDA_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "../../../../../nested/api-resolvers/src/lambda/document_versions_resolver",
    )
)


@pytest.fixture()
def mod(monkeypatch):
    monkeypatch.setenv("OUTPUT_BUCKET", "out-bucket")
    monkeypatch.setenv("TRACKING_TABLE", "tracking")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    sys.path.insert(0, LAMBDA_DIR)
    if "index" in sys.modules:
        del sys.modules["index"]
    module = importlib.import_module("index")
    yield module
    sys.path.remove(LAMBDA_DIR)
    sys.modules.pop("index", None)


def _event(field, **args):
    return {
        "info": {"fieldName": field},
        "arguments": args,
        "identity": {"claims": {"cognito:groups": ["Admin"]}},
    }


@pytest.mark.unit
class TestListAndGet:
    def test_list_document_versions(self, mod):
        mod.document_service = MagicMock()
        mod.document_service.list_document_runs.return_value = [
            {
                "RunId": "20250707T141530Z-a",
                "SK": "run#20250707T141530Z-a",
                "ObjectKey": "k",
                "FileCount": 5,
                "Sections": [{"Id": "1", "Class": "W2", "OutputJSONUri": "s3://o/k"}],
            }
        ]
        out = mod.handler(_event("listDocumentVersions", objectKey="k"), None)
        assert len(out) == 1
        assert out[0]["RunId"] == "20250707T141530Z-a"
        assert out[0]["Sections"][0]["Class"] == "W2"

    def test_sections_carry_confidence_alerts_and_issues(self, mod):
        """The UI's "Low Confidence Fields" count / section Status read these
        off the snapshot — dropping them made every historical section look
        clean (count 0, no issues)."""
        mod.document_service = MagicMock()
        mod.document_service.list_document_runs.return_value = [
            {
                "RunId": "r1",
                "SK": "run#r1",
                "ObjectKey": "k",
                "Sections": [
                    {
                        "Id": "1",
                        "Class": "W2",
                        "OutputJSONUri": "s3://o/k",
                        "ConfidenceThresholdAlerts": [
                            {
                                "attributeName": "WagesTips",
                                "confidence": 0.42,
                                "confidenceThreshold": 0.8,
                            }
                        ],
                        "ProcessingIssues": [
                            {
                                "stage": "assessment",
                                "severity": "warning",
                                "code": "assessment_incomplete",
                                "message": "some rows unscored",
                                "rootCause": "truncation",
                                # Stored-only blob: must not be returned (not
                                # part of the GraphQL ProcessingIssue type).
                                "details": '{"big": "blob"}',
                            }
                        ],
                    }
                ],
            }
        ]
        out = mod.handler(_event("listDocumentVersions", objectKey="k"), None)
        section = out[0]["Sections"][0]
        assert section["ConfidenceThresholdAlerts"][0]["attributeName"] == "WagesTips"
        assert section["ConfidenceThresholdAlerts"][0]["confidence"] == 0.42
        issue = section["ProcessingIssues"][0]
        assert issue["code"] == "assessment_incomplete"
        assert issue["rootCause"] == "truncation"
        assert "details" not in issue

    def test_sections_from_pre_snapshot_runs_default_to_empty_lists(self, mod):
        """Runs recorded before the quality data was snapshotted have no such
        keys; emit [] (not null) so the UI's Array.isArray() checks match the
        live-document path."""
        mod.document_service = MagicMock()
        mod.document_service.list_document_runs.return_value = [
            {
                "RunId": "r1",
                "SK": "run#r1",
                "ObjectKey": "k",
                "Sections": [{"Id": "1", "Class": "W2", "OutputJSONUri": "s3://o/k"}],
            }
        ]
        out = mod.handler(_event("listDocumentVersions", objectKey="k"), None)
        section = out[0]["Sections"][0]
        assert section["ConfidenceThresholdAlerts"] == []
        assert section["ProcessingIssues"] == []

    def test_sections_carry_exclusion_flags(self, mod):
        """The "Skipped" badge is the only explanation a user gets for why an
        excluded section's panels are empty. This shaping is an explicit
        allow-list, so an omitted key is silently dropped from every historical
        view (GitHub #704)."""
        mod.document_service = MagicMock()
        mod.document_service.list_document_runs.return_value = [
            {
                "RunId": "r1",
                "SK": "run#r1",
                "ObjectKey": "k",
                "Sections": [
                    {
                        "Id": "1",
                        "Class": "Instructions",
                        "OutputJSONUri": "s3://o/k",
                        "Excluded": True,
                        "ExclusionReason": "instructions",
                    },
                    {"Id": "2", "Class": "W2", "OutputJSONUri": "s3://o/k2"},
                ],
            }
        ]
        out = mod.handler(_event("listDocumentVersions", objectKey="k"), None)
        skipped, normal = out[0]["Sections"]
        assert skipped["Excluded"] is True
        assert skipped["ExclusionReason"] == "instructions"
        # Runs recorded before the flags were snapshotted have no such keys.
        assert normal["Excluded"] is False
        assert normal["ExclusionReason"] is None

    def test_classification_confidence_is_shaped_as_a_number(self, mod):
        """Class confidence and reason survive into a historical view.

        DynamoDB returns numbers as Decimal and this resolver serializes with
        json.dumps(default=str), which would emit a Float field as the STRING
        "0.83" — hence the explicit float coercion. Runs recorded before #673
        have no such keys and must read as not scored (None), not 0.0.
        """
        from decimal import Decimal

        mod.document_service = MagicMock()
        mod.document_service.list_document_runs.return_value = [
            {
                "RunId": "r1",
                "SK": "run#r1",
                "ObjectKey": "k",
                "Sections": [
                    {
                        "Id": "1",
                        "Class": "W2",
                        "OutputJSONUri": "s3://o/k",
                        "Confidence": Decimal("0.83"),
                    },
                    {"Id": "2", "Class": "W2", "OutputJSONUri": "s3://o/k2"},
                ],
                "Pages": [
                    {
                        "Id": 1,
                        "Class": "W2",
                        "ClassConfidence": Decimal("0.83"),
                        "ClassReason": "Box 1 wage labels",
                    },
                    {"Id": 2, "Class": "W2"},
                ],
            }
        ]
        out = mod.handler(_event("listDocumentVersions", objectKey="k"), None)
        scored_section, unscored_section = out[0]["Sections"]
        scored_page, unscored_page = out[0]["Pages"]

        assert scored_section["Confidence"] == 0.83
        assert isinstance(scored_section["Confidence"], float)
        assert unscored_section["Confidence"] is None
        assert scored_page["ClassConfidence"] == 0.83
        assert isinstance(scored_page["ClassConfidence"], float)
        assert scored_page["ClassReason"] == "Box 1 wage labels"
        assert unscored_page["ClassConfidence"] is None
        assert unscored_page["ClassReason"] is None

    def test_get_document_version_includes_files(self, mod, monkeypatch):
        mod.document_service = MagicMock()
        mod.document_service.get_document_run.return_value = {
            "RunId": "r1",
            "SK": "run#r1",
            "ObjectKey": "k",
            "Sections": [],
        }
        monkeypatch.setattr(
            mod,
            "load_run_manifest",
            lambda *a, **k: {
                "files": [
                    {"key": "k/sections/1/result.json", "version_id": "v1", "size": 10}
                ]
            },
        )
        out = mod.handler(_event("getDocumentVersion", objectKey="k", runId="r1"), None)
        assert out["Files"][0]["VersionId"] == "v1"

    def test_get_missing_version_returns_none(self, mod):
        mod.document_service = MagicMock()
        mod.document_service.get_document_run.return_value = None
        out = mod.handler(
            _event("getDocumentVersion", objectKey="k", runId="nope"), None
        )
        assert out is None


@pytest.mark.unit
class TestDelete:
    def test_delete_requires_admin(self, mod, monkeypatch):
        mod.document_service = MagicMock()
        mod.document_service.get_document_run.return_value = {"SK": "run#r1"}
        monkeypatch.setattr(mod, "delete_run_artifacts", lambda *a, **k: 3)
        event = _event("deleteDocumentVersion", objectKey="k", runId="r1")
        event["identity"]["claims"]["cognito:groups"] = ["Author"]
        with pytest.raises(PermissionError):
            mod.handler(event, None)

    def test_delete_admin_ok(self, mod, monkeypatch):
        mod.document_service = MagicMock()
        mod.document_service.get_document_run.return_value = {
            "RunId": "r1",
            "SK": "run#r1",
        }
        # r1 is NOT the newest (r2 is), so deletion is allowed.
        mod.document_service.list_document_runs.return_value = [
            {"RunId": "r2", "SK": "run#r2"},
            {"RunId": "r1", "SK": "run#r1"},
        ]
        monkeypatch.setattr(mod, "delete_run_artifacts", lambda *a, **k: 3)
        out = mod.handler(
            _event("deleteDocumentVersion", objectKey="k", runId="r1"), None
        )
        assert out is True
        mod.document_service.delete_document_run.assert_called_once_with("k", "r1")

    def test_cannot_delete_current_version(self, mod, monkeypatch):
        """Deleting the newest run would destroy the live document's outputs."""
        mod.document_service = MagicMock()
        mod.document_service.get_document_run.return_value = {
            "RunId": "r2",
            "SK": "run#r2",
        }
        mod.document_service.list_document_runs.return_value = [
            {"RunId": "r2", "SK": "run#r2"},
            {"RunId": "r1", "SK": "run#r1"},
        ]
        deleted = {"called": False}
        monkeypatch.setattr(
            mod, "delete_run_artifacts", lambda *a, **k: deleted.update(called=True)
        )
        with pytest.raises(ValueError, match="current"):
            mod.handler(
                _event("deleteDocumentVersion", objectKey="k", runId="r2"), None
            )
        # Must not have touched S3 or the record.
        assert deleted["called"] is False
        mod.document_service.delete_document_run.assert_not_called()


@pytest.mark.unit
class TestCompare:
    def test_diff_detects_changed_value(self, mod, monkeypatch):
        mod.document_service = MagicMock()
        runs = {
            "rA": {
                "SK": "run#rA",
                "ConfigVersion": "v1",
                "Sections": [
                    {
                        "Id": "1",
                        "Class": "Invoice",
                        "OutputJSONUri": "s3://out-bucket/k/sections/1/result.json",
                    }
                ],
            },
            "rB": {
                "SK": "run#rB",
                "ConfigVersion": "v2",
                "Sections": [
                    {
                        "Id": "1",
                        "Class": "Invoice",
                        "OutputJSONUri": "s3://out-bucket/k/sections/1/result.json",
                    }
                ],
            },
        }
        mod.document_service.get_document_run.side_effect = lambda ok, rid: runs[rid]
        monkeypatch.setattr(mod, "load_run_manifest", lambda *a, **k: {"files": []})

        results = {
            "rA": {"inference_result": {"total": "100.00", "vendor": "Acme"}},
            "rB": {"inference_result": {"total": "125.00", "vendor": "Acme"}},
        }

        # Patch _load_section_results directly for determinism
        monkeypatch.setattr(
            mod,
            "_load_section_results",
            lambda ok, run, rid: {
                "Invoice": {
                    "class": "Invoice",
                    "result": results[rid]["inference_result"],
                }
            },
        )

        out = mod.handler(
            _event("compareDocumentVersions", objectKey="k", runIdA="rA", runIdB="rB"),
            None,
        )
        parsed = json.loads(out)
        assert parsed["identical"] is False
        invoice = [s for s in parsed["sections"] if s["section"] == "Invoice"][0]
        assert invoice["status"] == "changed"
        change = [c for c in invoice["changes"] if c["path"] == "total"][0]
        assert change["a"] == "100.00"
        assert change["b"] == "125.00"

    def test_identical_runs(self, mod, monkeypatch):
        mod.document_service = MagicMock()
        run = {"SK": "run#r", "Sections": [{"Id": "1", "Class": "Invoice"}]}
        mod.document_service.get_document_run.return_value = run
        monkeypatch.setattr(
            mod,
            "_load_section_results",
            lambda ok, r, rid: {"Invoice": {"class": "Invoice", "result": {"a": 1}}},
        )
        out = mod.handler(
            _event("compareDocumentVersions", objectKey="k", runIdA="r1", runIdB="r2"),
            None,
        )
        assert json.loads(out)["identical"] is True
