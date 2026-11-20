# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The fit check's AWS-facing steps, each against a fake of what it reads.

``test_fit_check_operation.py`` covers the order of the steps; this covers what
each step does with what it is given: the profile's settings, a document's record,
the manifest, the wait, the Discovery fallbacks and the guard's snapshot.
"""

import csv
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from idp_sdk import IDPClient
from idp_sdk._core.fit_check import collect, inputs
from idp_sdk.exceptions import IDPProcessingError, IDPValidationError
from idp_sdk.models.base import DocumentBucket
from idp_sdk.operations.fit_check import FitCheckOperation

# --------------------------------------------------------------- collect


@pytest.mark.unit
def test_profile_settings_reads_mode_ocr_classes_and_granularity():
    config = {
        "use_bda": False,
        "ocr": {"backend": "textract", "features": [{"name": "TABLES"}, "LAYOUT", {}]},
        "extraction": {"mode": "advanced"},
        "classification": {
            "strategy": "combined",
            "combined": {"granularity": "packet"},
        },
        "classes": [
            {"$id": "Invoice", "properties": {"total": {}, "date": {}}},
            {"x-aws-idp-document-type": "W2", "properties": {}},
            {"description": "no id"},
        ],
    }
    settings = collect.profile_settings(config)
    assert settings["ocr_features"] == ["TABLES", "LAYOUT"]
    assert settings["extraction_mode"] == "advanced"
    assert settings["classification_granularity"] == "packet"
    assert settings["classes"] == {"Invoice": ["total", "date"], "W2": []}
    split = {
        "classification": {
            "strategy": "split_then_classify",
            "split_then_classify": {"splitting": {"llm": {"granularity": "page"}}},
        }
    }
    assert collect.profile_settings(split)["classification_granularity"] == "page"


@pytest.mark.unit
def test_canonical_hash_ignores_key_order_only():
    assert collect.canonical_sha256({"a": 1, "b": 2}) == collect.canonical_sha256(
        {"b": 2, "a": 1}
    )
    assert collect.canonical_sha256({"a": 1}) != collect.canonical_sha256({"a": 2})


class _S3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objects[Key])}


def _document():
    from idp_common.models import Document, ProcessingIssue, Section

    doc = Document(id="b/x.pdf", input_key="b/x.pdf", num_pages=2)
    doc.metering = {"OCR/textract/a": {"pages": 2}}
    doc.errors = ["one"]
    doc.processing_issues = [
        ProcessingIssue(stage="ocr", severity="warning", code="c", message="m")
    ]
    doc.sections = [
        Section(section_id="1", classification="Invoice", page_ids=["1", "2"])
    ]
    return doc


@pytest.mark.unit
def test_collect_document_reads_tracking_sections_pages_and_evaluation():
    s3 = _S3(
        {
            "b/x.pdf/sections/1/result.json": json.dumps(
                {
                    "inference_result": {"total": 1},
                    "explainability_info": [{"total": {"confidence": 0.9}}],
                }
            ).encode(),
            "b/x.pdf/pages/1/result.json": json.dumps({"text": "page one"}).encode(),
            "b/x.pdf/pages/2/result.json": b"not json",
            "b/x.pdf/evaluation/results.json": json.dumps(
                {"overall_metrics": {"precision": 1.0}}
            ).encode(),
        }
    )
    tracking = SimpleNamespace(get_document=lambda key: _document())
    record = collect.collect_document(
        sample={"file": "x.pdf", "format": "pdf", "pages": 2},
        object_key="b/x.pdf",
        status={"status": "COMPLETED", "duration_seconds": 3.0, "notes": ["n"]},
        tracking=tracking,
        s3=s3,
        output_bucket="out",
        with_evaluation=True,
    )
    assert record["errors"] == ["one"] and record["metering"] == {
        "OCR/textract/a": {"pages": 2}
    }
    assert record["issues"][0]["code"] == "c"
    assert record["sections"][0]["explainability_info"] == {
        "total": {"confidence": 0.9}
    }
    assert record["page_text"] == {"1": "page one"}  # an unreadable page is skipped
    assert record["evaluation"] == {"precision": 1.0}
    assert record["notes"] == ["n"]


@pytest.mark.unit
def test_a_document_without_a_tracking_record_says_so():
    record = collect.collect_document(
        sample={"file": "x.pdf"},
        object_key="b/x.pdf",
        status={"status": "NOT_FOUND"},
        tracking=SimpleNamespace(get_document=lambda key: None),
        s3=_S3({}),
        output_bucket="out",
        with_evaluation=False,
    )
    assert record["notes"] == ["No tracking record was found for this document"]
    assert record["sections"] == []


# ---------------------------------------------------------------- inputs


def _pdf(path: Path, text: str) -> Path:
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    pdf = pdfium.PdfDocument.new()
    page = pdf.new_page(200, 200)
    font = raw.FPDFText_LoadStandardFont(pdf.raw, b"Helvetica")
    obj = raw.FPDFPageObj_NewTextObj(pdf.raw, b"Helvetica", 12.0)
    buf = (text + "\x00").encode("utf-16-le")
    raw.FPDFText_SetText(obj, (raw.FPDF_WCHAR * (len(buf) // 2)).from_buffer_copy(buf))
    raw.FPDFPage_InsertObject(page.raw, obj)
    raw.FPDFPage_GenerateContent(page.raw)
    del font
    pdf.save(str(path))
    return path


@pytest.mark.unit
def test_a_pdf_has_its_pages_and_text_layer_scripts_counted(tmp_path):
    sample = inputs.inspect_file(_pdf(tmp_path / "a.pdf", "Invoice total"))
    assert sample.refused is None and sample.pages == 1
    assert sample.text_layer_scripts.get("Latin") == len("Invoicetotal")


@pytest.mark.unit
def test_an_unreadable_pdf_is_accepted_with_unknown_pages(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.7 truncated")
    sample = inputs.inspect_file(path)
    assert (
        sample.refused is None
        and sample.pages is None
        and sample.text_layer_scripts == {}
    )


@pytest.mark.unit
def test_a_file_with_no_extension_is_refused_and_a_missing_folder_raises(tmp_path):
    path = tmp_path / "README"
    path.write_text("x")
    sample = inputs.inspect_file(path)
    assert sample.refused.startswith("no extension is not a file type")
    with pytest.raises(NotADirectoryError):
        inputs.scan_samples(str(tmp_path / "missing"))


# ------------------------------------------------------------- operation


def _op():
    client = MagicMock(spec=IDPClient)
    client._region = "us-west-2"
    for name in ("config", "discovery", "batch"):
        setattr(client, name, MagicMock())
    return FitCheckOperation(client), client


@pytest.mark.unit
def test_the_stack_version_comes_from_its_description(monkeypatch):
    op, _ = _op()
    cfn = MagicMock()
    cfn.describe_stacks.return_value = {
        "Stacks": [{"Description": "Accelerator (x) (v0.6.16.dev1)"}]
    }
    monkeypatch.setattr("boto3.client", lambda *a, **k: cfn)
    assert op._stack_version("S") == "0.6.16.dev1"
    cfn.describe_stacks.return_value = {"Stacks": [{}]}
    assert op._stack_version("S") is None


@pytest.mark.unit
def test_the_snapshot_hashes_the_active_profile_and_default():
    op, client = _op()
    client.config.list.return_value.versions = [
        SimpleNamespace(version_name="default", is_active=False),
        SimpleNamespace(version_name="p1", is_active=True),
    ]
    configs = {"p1": {"a": 1}, "default": {"b": 2}}
    client.config.download.side_effect = lambda stack_name, config_profile: (
        SimpleNamespace(config=configs[config_profile])
    )
    snap = op._snapshot("S")
    assert snap == {
        "active_profile": "p1",
        "active_sha256": collect.canonical_sha256({"a": 1}),
        "default_sha256": collect.canonical_sha256({"b": 2}),
    }


@pytest.mark.unit
def test_pricing_is_the_stacks_merged_table(monkeypatch):
    op, client = _op()
    client.config._configure_config_env.return_value = "T"
    merged = MagicMock()
    merged.model_dump.return_value = {"pricing": [{"name": "x", "units": []}]}
    with patch("idp_common.config.configuration_manager.ConfigurationManager") as mgr:
        mgr.return_value.get_merged_pricing.return_value = merged
        assert op._pricing("S") == [{"name": "x", "units": []}]
        mgr.assert_called_once_with(region="us-west-2")
        mgr.return_value.get_merged_pricing.return_value = None
        assert op._pricing("S") == []


def _sample(tmp_path, name, fmt):
    p = tmp_path / name
    p.write_bytes(b"x")
    return inputs.SampleFile(path=str(p), file=name, format=fmt, size_bytes=1)


def _with_classes(op, client, after_calls):
    """The profile gains a class once ``after_calls`` Discovery calls have run."""
    state = {"calls": 0}

    def names(_):
        return (
            {"default": True, "p": False}
            if state["calls"] >= after_calls
            else {"default": True}
        )

    def bump(*a, **k):
        state["calls"] += 1
        return MagicMock(success=True)

    op._profile_names = names
    client.config.download.return_value.config = {"classes": [{"$id": "C"}]}
    return bump


@pytest.mark.unit
def test_several_samples_go_through_multi_document_discovery(tmp_path):
    op, client = _op()
    client.discovery.run_multi_doc.side_effect = _with_classes(op, client, 1)
    accepted = [_sample(tmp_path, "a.pdf", "pdf"), _sample(tmp_path, "b.png", "png")]
    assert op._generate_profile_quietly("S", "p", accepted, None, print) == "discovery"
    client.discovery.run.assert_not_called()


@pytest.mark.unit
def test_no_multi_document_class_falls_back_to_one_file_at_a_time(tmp_path):
    op, client = _op()
    client.discovery.run_multi_doc.side_effect = RuntimeError("extra not installed")
    client.discovery.run.side_effect = _with_classes(op, client, 1)
    accepted = [_sample(tmp_path, "a.pdf", "pdf"), _sample(tmp_path, "b.webp", "webp")]
    assert op._generate_profile_quietly("S", "p", accepted, None, print) == "discovery"
    # WebP is read by multi-document Discovery only.
    assert [Path(c.args[0]).name for c in client.discovery.run.call_args_list] == [
        "a.pdf"
    ]


@pytest.mark.unit
def test_quick_start_runs_when_discovery_finds_nothing(tmp_path):
    op, client = _op()
    client.discovery.run.side_effect = RuntimeError("model error")
    client.config.bootstrap.side_effect = _with_classes(op, client, 1)
    accepted = [_sample(tmp_path, "a.pdf", "pdf"), _sample(tmp_path, "b.docx", "docx")]
    op._profile_names = lambda _: {"default": True}
    client.discovery.run.side_effect = RuntimeError("model error")

    def bootstrap(*a, **k):
        op._profile_names = lambda _: {"default": True, "p": False}
        k["status_callback"](50.0, "authoring")
        return MagicMock(success=True)

    client.config.bootstrap.side_effect = bootstrap
    assert (
        op._generate_profile_quietly("S", "p", accepted, "invoices", print)
        == "quick-start"
    )
    kwargs = client.config.bootstrap.call_args.kwargs
    assert kwargs["target_profile"] == "p" and kwargs["generate_test_set"] is False


@pytest.mark.unit
def test_no_class_from_any_route_is_an_error(tmp_path):
    op, client = _op()
    op._profile_names = lambda _: {"default": True}
    accepted = [_sample(tmp_path, "a.docx", "docx")]
    with pytest.raises(IDPProcessingError, match="Pass --description"):
        op._generate_profile_quietly("S", "p", accepted, None, print)
    client.config.bootstrap.return_value = MagicMock(success=False, error="refused")
    with pytest.raises(
        IDPProcessingError, match="Quick Start did not create a class: refused"
    ):
        op._generate_profile_quietly("S", "p", accepted, "invoices", print)


@pytest.mark.unit
def test_the_manifest_names_each_sample_and_its_baseline(tmp_path):
    op, client = _op()
    seen = {}

    def process(**kwargs):
        with open(kwargs["manifest"], newline="", encoding="utf-8") as handle:
            seen["rows"] = list(csv.DictReader(handle))
        seen["kwargs"] = kwargs
        return MagicMock(document_ids=["b/a.pdf"])

    client.batch.process.side_effect = process
    gt = tmp_path / "gt"
    (gt / "a.pdf").mkdir(parents=True)
    accepted = [_sample(tmp_path, "a.pdf", "pdf"), _sample(tmp_path, "b.png", "png")]
    assert op._submit("S", "b", "p", accepted, str(gt)) == ["b/a.pdf"]
    assert [r["baseline_source"] for r in seen["rows"]] == [
        str((gt / "a.pdf").resolve()),
        "",
    ]
    assert seen["kwargs"]["batch_id"] == "b" and seen["kwargs"]["config_profile"] == "p"


def _status(document_id, bucket, status="X"):
    return SimpleNamespace(
        document_id=document_id,
        bucket=bucket,
        status=status,
        num_pages=1,
        duration_seconds=2.0,
        error=None,
    )


@pytest.mark.unit
def test_the_wait_maps_each_bucket_and_reports_unfinished_documents(monkeypatch):
    op, client = _op()
    monkeypatch.setattr("time.sleep", lambda s: None)
    running = SimpleNamespace(
        completed=0, failed=0, in_progress=1, queued=0, all_complete=False, documents=[]
    )
    done = SimpleNamespace(
        completed=1,
        failed=1,
        in_progress=1,
        queued=0,
        all_complete=False,
        documents=[
            _status("b/a", DocumentBucket.COMPLETED),
            _status("b/r", DocumentBucket.AWAITING_REVIEW),
            _status("b/f", DocumentBucket.FAILED),
            _status("b/q", DocumentBucket.RUNNING, "RUNNING"),
        ],
    )
    client.batch.get_status.side_effect = [running, done]
    ticks = iter([0.0, 0.0, 999.0])
    monkeypatch.setattr("time.monotonic", lambda: next(ticks))
    out = op._wait("S", "b", 10, 1, print)
    assert out["b/a"]["status"] == "COMPLETED"
    assert out["b/r"]["status"] == "AWAITING_REVIEW" and out["b/r"]["notes"]
    assert out["b/f"]["status"] == "FAILED"
    assert (
        out["b/q"]["status"] == "NOT_FINISHED" and "within 10 s" in out["b/q"]["error"]
    )


@pytest.mark.unit
def test_collect_reads_each_accepted_sample_by_its_key(tmp_path, monkeypatch):
    op, _ = _op()
    monkeypatch.setattr("boto3.client", lambda *a, **k: _S3({}))
    accepted = [_sample(tmp_path, "a.pdf", "pdf")]
    with (
        patch("idp_common.dynamodb.service.DocumentDynamoDBService") as svc,
        patch("idp_common.dynamodb.client.DynamoDBClient") as ddb,
    ):
        svc.return_value.get_document.return_value = None
        docs = op._collect(
            "S", {"DocumentsTable": "t", "OutputBucket": "o"}, accepted, "b", {}, False
        )
    ddb.assert_called_once_with(table_name="t", region="us-west-2")
    svc.return_value.get_document.assert_called_once_with("b/a.pdf")
    assert docs[0]["status"] == "NOT_FOUND"


@pytest.mark.unit
def test_a_run_needs_samples_and_at_least_one_accepted_file(tmp_path):
    op, client = _op()
    with pytest.raises(IDPValidationError, match="samples_dir is required"):
        op.run(None, "S")
    client._require_stack.return_value = "S"
    (tmp_path / "deck.pptx").write_bytes(b"PK")
    with pytest.raises(IDPValidationError, match="No file"):
        op.run(str(tmp_path), "S")
