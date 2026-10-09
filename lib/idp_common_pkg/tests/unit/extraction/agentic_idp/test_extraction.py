import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Annotated

import boto3
import pytest
import yaml
from moto import mock_aws
from PIL import Image
from pydantic import BaseModel, Field, field_validator

from idp_common import s3
from idp_common.config.merge_utils import merge_config_with_defaults
from idp_common.models import Document, Section
from idp_common.ocr.service import OcrService

# Check if strands is actually available (not mocked)
try:
    import strands  # noqa: F401

    from idp_common.extraction.agentic_idp import structured_output
    from idp_common.extraction.service import ExtractionService

    STRANDS_AVAILABLE = True
except ImportError:
    STRANDS_AVAILABLE = False

# Configure logging to show INFO level logs during tests
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)


@pytest.fixture
def s3_bucket():
    """Create a mocked S3 bucket for testing.

    Mocks S3 only, letting Bedrock *and Textract* through to real AWS. Textract
    is in the passthrough because `samples/lending_package.pdf` is a scan with a
    zero-character text layer, so without a real OCR call there is no document
    text at all and the agent is left reading the page image alone — which is a
    configuration the pipeline never runs, and which misread a cents value when
    this test was asserting against it.
    """
    with mock_aws(
        config={
            "core": {
                "mock_credentials": False,
                "passthrough": {
                    "urls": [
                        r".*bedrock.*\.amazonaws\.com.*",
                        r".*textract.*\.amazonaws\.com.*",
                    ]
                },
            }
        }
    ):
        s3_client = boto3.client("s3", region_name="us-east-1")
        bucket_name = "test-idp-bucket"
        s3_client.create_bucket(Bucket=bucket_name)
        yield {"client": s3_client, "bucket": bucket_name}


class Address(BaseModel):
    city: str
    state: str
    street: str
    zip_code: str


class License(BaseModel):
    sex: str
    class_: str
    height: str
    weight: str
    address: Address
    eye_color: str
    last_name: str
    first_name: str
    issue_date: Annotated[
        str,
        Field(description="the date the license was issued formatted as MM/DD/YYYY"),
    ]
    date_of_birth: Annotated[
        str, Field(json_schema_extra=dict(description="formatted as MM/DD/YYYY"))
    ]
    expiration_date: Annotated[
        str, Field(json_schema_extra=dict(description="formatted as MM/DD/YYYY"))
    ]
    driver_license_number: str

    @field_validator("issue_date", "date_of_birth", "expiration_date")
    def validate_date_format(cls, value) -> str:
        try:
            datetime.strptime(value, "%m/%d/%Y")
        except Exception:
            raise ValueError("Date format should be parsed into MM/DD/YYYY")

        return value


# LIVE Bedrock: the s3_bucket fixture passes bedrock URLs through to real AWS,
# and this one calls the model directly. Marked `integration` so the default
# gate (`-m "not integration"`) excludes it — it costs money and needs
# credentials. It previously carried only `agentic`, which nothing filters on,
# so it was one unconditional stub away from billing CI five times per run.
@pytest.mark.integration
@pytest.mark.agentic
@pytest.mark.parametrize("execution_number", range(5))
@pytest.mark.skipif(not STRANDS_AVAILABLE, reason="strands package not available")
def test_structured_output_call_license(execution_number):
    # ⚠️ The model comes from the shipped default rather than a pin here. The pin
    # was `claude-sonnet-4-20250514-v1:0`, which Bedrock now refuses with
    # `ResourceNotFoundException: ... marked by provider as Legacy and you have
    # not been actively using the model in the last 30 days` -- so this test is a
    # time bomb that re-arms after 30 days of disuse, five times over via the
    # parametrize. Same reasoning as the payslip test below; fixed here too
    # because the pin is the defect, not the one instance of it.
    model_id = (
        merge_config_with_defaults({}, "pattern-2").get("extraction", {}).get("model")
    )
    assert model_id, "no extraction model resolved from the system defaults"
    result, _ = structured_output(
        model_id=model_id,
        data_format=License,
        prompt=Image.open(Path(__file__).parent / "old_cal_license.png"),
    )

    print(result)

    # The DATES are the assertion, not their rendering. This test samples -- it
    # passes no `config`, and the resolved model strips temperature/top_p/top_k --
    # five times over through the parametrize, so pinning a format string here
    # would fail on a conformant value, as the payslip test's `PayDate` did.
    assert datetime.strptime(result.issue_date, "%m/%d/%Y").date() == date(
        1965, 8, 4
    ), result.issue_date
    assert datetime.strptime(result.expiration_date, "%m/%d/%Y").date() == date(
        1970, 8, 20
    ), result.expiration_date


@pytest.mark.integration  # LIVE Bedrock — see the note above.
@pytest.mark.agentic
@pytest.mark.parametrize("execution_number", range(1))
@pytest.mark.skipif(not STRANDS_AVAILABLE, reason="strands package not available")
def test_payslip(execution_number, s3_bucket):
    """
    Test agentic extraction using lending_package.pdf sample config.

    This test validates the agentic extraction flow:
    - Loads the unified lending-package-sample config
    - Processes first page of lending_package.pdf (Payslip)
    - Verifies extraction results structure
    """

    sample_pdf = (
        Path(__file__).parent.parent.parent.parent.parent.parent.parent
        / "samples"
        / "lending_package.pdf"
    )

    if not sample_pdf.exists():
        pytest.skip(f"Sample file not found: {sample_pdf}")

    config_path = (
        Path(__file__).parent.parent.parent.parent.parent.parent.parent
        / "config_library"
        # ⚠️ `pattern-2` means two different things within this function, so do
        # not "fix" the apparent inconsistency below. `config_library/pattern-2/`
        # was removed by the unification and the PRESET lives under `unified`
        # now -- that old path had rotted unnoticed because this test never ran.
        # But `idp_common/config/system_defaults/pattern-2.yaml` still exists and
        # is still the `use_bda: false` overlay, which is what this preset is, so
        # it is the right argument to `merge_config_with_defaults`.
        / "unified"
        / "lending-package-sample"
        / "config.yaml"
    )

    with open(config_path, "r") as f:
        config_data = yaml.safe_load(f)

    # ⚠️ The preset defines no `extraction.task_prompt`, so `.get(..., "")` sent
    # an EMPTY prompt. `ExtractionService` does not merge the system defaults --
    # a deployed stack does that before the service ever sees a config -- so
    # nothing filled the gap, and the agent was asked to extract from nothing.
    #
    # That is also why the page image never arrived: `{DOCUMENT_IMAGE}` is what
    # attaches it, and the placeholder lives in the prompt. `samples/
    # lending_package.pdf` is a scan with a zero-character text layer, so
    # without a real OCR call the image is the only content there is. With an
    # empty prompt the model answered, correctly, that the document text
    # appeared to be missing.
    #
    # The defaults are resolved through `merge_config_with_defaults`, the same
    # function `update_configuration` and the SDK's stack deployer call, rather
    # than by reading `base-extraction.yaml` directly. Reading the one file would
    # get the same answer today and would be its own rot vector: it bypasses the
    # inheritance resolution, so an override added to `pattern-2.yaml` or
    # `base.yaml` would change what a deployment resolves while this test kept
    # reading the base module and passing.
    merged = merge_config_with_defaults(config_data, "pattern-2")
    merged_extraction = merged.get("extraction") or {}

    task_prompt = merged_extraction.get("task_prompt") or ""
    assert task_prompt, (
        "no extraction task_prompt after merging the system defaults; the agent "
        "would be asked to extract from nothing"
    )
    assert "{DOCUMENT_IMAGE}" in task_prompt, (
        "the task prompt has no {DOCUMENT_IMAGE} placeholder, so the page image "
        "is not attached -- and this sample PDF has no text layer, so the image "
        "would be the only content the agent could read"
    )

    # ⚠️ The extraction block is the MERGED one with only the deliberate
    # overrides laid on top, rather than a hand-written dict. A hand-written
    # `{"agentic": {"enabled": True}}` replaces the whole `agentic` block, so
    # every sub-field falls back to a Pydantic default instead of the resolved
    # one -- and `table_parsing.enabled` defaults to False where the system
    # default is True. That silently switched off the deterministic table tools
    # (`agentic_idp.py` gates both the prompt guidance and the `parse_table` /
    # `map_table_to_schema` registration on that flag), in the one test that
    # reaches them. `max_tokens` is dropped for the same reason: `IDPConfig`
    # logs it as a removed field and ignores it, so leaving it in the dict read
    # as pinning an output budget that nothing pins.
    # ⚠️ `mode` is the switch. `ExtractionConfig.reconcile_mode_and_agentic`
    # overwrites `agentic.enabled` from `mode` whenever `mode` is set --
    # `agentic.enabled = (mode == "advanced")` -- so setting `agentic.enabled`
    # alone is silently discarded, and the merged defaults carry `mode: simple`.
    # Measured: `mode=simple` resolves to `agentic.enabled=False` whatever the
    # flag says, and the run then takes the traditional single-LLM-pass path with
    # every assertion still passing, in a test named for the agentic one.
    # `config/migrations/v05_to_v06.py` calls this exact pairing a footgun and
    # exists to prevent it in user configs.
    #
    # The `agentic` override below is therefore redundant *for `enabled`* and is
    # kept only to be explicit about intent. What is genuinely load-bearing is
    # `dict(merged_extraction)`: a hand-written `agentic` block would replace the
    # whole thing and reset `table_parsing.enabled` to its Pydantic default of
    # False, which is the defect this started as.
    #
    # The model is deliberately NOT overridden. A pin here used to name
    # `claude-sonnet-4-20250514-v1:0`, which Bedrock now refuses --
    # `ResourceNotFoundException: ... marked by provider as Legacy and you have
    # not been actively using the model in the last 30 days` -- so the test went
    # red for a reason that has nothing to do with extraction. Taking the
    # resolved default cannot age out, because the release that retires a model
    # moves that default too. ⚠️ The trade is reproducibility: Sonnet 5 rejects
    # `temperature`/`top_k`/`top_p` and the client strips them, so this test
    # samples where a pinned Sonnet 4 decoded greedily. A value assertion below
    # can therefore move without any code changing, which is why the failure
    # messages name the model.
    extraction_config = dict(merged_extraction)
    extraction_config["mode"] = "advanced"
    extraction_config["agentic"] = {
        **(merged_extraction.get("agentic") or {}),
        "enabled": True,
    }

    CONFIG = {
        "extraction": extraction_config,
        # From `merged`, not the raw preset: identical for this preset today, and
        # the paragraph above argues against exactly that shortcut.
        "classes": merged.get("classes") or [],
        "ocr": merged.get("ocr") or {},
    }

    # The configuration the service actually ends up with, asserted before any
    # live call. This is the only place the reconciliation above is observable
    # from, and nothing else in the tree notices if it regresses.
    extraction_service = ExtractionService(config=CONFIG)
    resolved = extraction_service.config.extraction
    assert resolved.agentic.enabled is True, (
        f"agentic extraction is off (mode={resolved.mode!r}); this test would run "
        "the traditional single-pass path and still pass"
    )
    # Guards the `dict(merged_extraction)` half rather than the `mode` half: this
    # one resolves True under `mode: simple` too, so it does not discriminate on
    # the path taken. `agentic.enabled` above and `extraction_method` below do.
    assert resolved.agentic.table_parsing.enabled is True, (
        "the deterministic table tools are not registered; `parse_table` and "
        "`map_table_to_schema` are gated on this flag"
    )
    extraction_model = resolved.model
    assert extraction_model, "no extraction model resolved"

    os.environ.setdefault("AWS_REGION", "us-east-1")
    os.environ.setdefault("METRIC_NAMESPACE", "IDP-Test")

    s3_client = s3_bucket["client"]
    bucket_name = s3_bucket["bucket"]

    s3_client.upload_file(str(sample_pdf), bucket_name, "lending_package.pdf")

    document = Document(
        id="test_lending_package",
        input_bucket=bucket_name,
        input_key="lending_package.pdf",
        output_bucket=bucket_name,
    )

    # Real OCR through the pipeline's own public entry point, because this
    # sample has no text layer to read instead: `pdfium`'s text page returns
    # zero characters for it. This used to write an empty `ocr_text.txt`, which
    # left the page image as the agent's only source and had it reading cents
    # off a scan -- it returned $291.6 for a $291.90 field, with no OCR text to
    # cross-check against.
    #
    # ⚠️ `process_document` rather than a hand-rolled render plus
    # `_analyze_document`, and the difference is faithfulness rather than taste.
    # Every input the agent then sees -- the render DPI, the resize ceiling, the
    # JPEG encoding, the Textract features and the textractor linearizer -- comes
    # from the resolved configuration by construction, so none of them can drift
    # from a deployment while this test keeps passing. Hand-rolling them got all
    # four wrong at once: 72 dpi against the pipeline's 300 (`ocr/service.py`
    # notes Textract "silently drops" faint glyphs below ~200 dpi, issue #729, and
    # this test asserts exact cents), a lossless PNG where production sends a
    # lossy JPEG, and `detect_document_text` plus a LINE join -- which is what
    # `_parse_textract_response` falls back to only when textractor FAILS, and
    # which on this two-column payslip interleaves the columns and emits no table
    # markup at all.
    #
    # It costs about $0.11 and six seconds more per run, because the sample is six
    # pages and this OCRs all of them. That is the right trade for a tier that
    # runs by hand and exists to exercise the real path.
    ocr_service = OcrService(region=os.environ["AWS_REGION"], config=CONFIG)
    document = ocr_service.process_document(document)
    assert "1" in document.pages, f"OCR produced no page 1: {document.errors}"
    # ⚠️ Scoped to page 1, because `process_document` OCRs all six pages of the
    # sample and appends a per-page failure to `document.errors` while carrying
    # on. A transient Textract throttle on page 4 would otherwise fail a payslip
    # test over a page it never reads.
    # ⚠️ Anchored with `\b`, not `"page 1" in ...`: a substring match also claims
    # "page 10" through "page 19", which would re-arm the very misattribution
    # this scoping removes the moment anyone swaps in a longer document -- as the
    # table-parsing note below suggests doing.
    page_one_errors = [
        e
        for e in document.errors
        if re.match(r"error processing page 1\b", str(e).lower())
    ]
    assert not page_one_errors, f"OCR failed for page 1: {page_one_errors}"
    assert document.pages["1"].parsed_text_uri, "page 1 has no parsed text"

    ocr_text = s3.get_text_content(document.pages["1"].parsed_text_uri)
    assert ocr_text.strip(), (
        "OCR produced no text for the payslip page; the agent would be left "
        "reading the image alone, which the pipeline never does"
    )
    # ⚠️ What this pins is that `ocr.features` resolved to include TABLES/LAYOUT
    # and that the textractor MARKDOWN linearizer ran -- not that the
    # deterministic table parser gets USED. It is registered and then declines:
    # `_preflight_table_parse` recommends the tool only at an estimated 50+ rows,
    # and this section measures 30, so the result records
    # `tool_usage_decision: {expected: false, actual: false, tool_enabled: true}`.
    # That is a near miss rather than a wide one -- 30 is one row short of the
    # separate `tool_usage_recommended` threshold -- so covering the deterministic
    # path needs a genuinely large table, such as the `bank-statement-sample`
    # preset, rather than a slightly busier payslip.
    #
    # (`tables_detected` is NOT the reason and cannot be: `_analyze_ocr_for_tables`
    # derives it by counting pipe-bearing lines, so it is >= 1 for any text that
    # satisfies the assertion below. It measures 3 here.)
    #
    # A failure here is more likely to be the linearizer than the configuration:
    # `_parse_textract_response` falls back to plain `parsed_response.text` when
    # `to_markdown()` raises, and its own comments name a signature block as the
    # anticipated cause -- so read the OCR log before suspecting `ocr.features`.
    assert "|" in ocr_text, (
        "the OCR text carries no table markup, so either the textractor markdown "
        "linearizer fell back to plain text (check the OCR log first) or "
        f"ocr.features no longer includes TABLES/LAYOUT (resolved to "
        f"{(CONFIG.get('ocr') or {}).get('features')!r})"
    )

    section = Section(
        section_id="1",
        classification="Payslip",
        page_ids=["1"],
        confidence=1.0,
    )
    document.sections = [section]

    # The instance whose resolved config was asserted above, so the run and the
    # assertion cannot diverge.
    result_document = extraction_service.process_document_section(
        document=document, section_id=section.section_id
    )

    result_section = result_document.sections[0]

    assert result_section.extraction_result_uri is not None

    if result_section.extraction_result_uri.startswith("s3://"):
        s3_path = result_section.extraction_result_uri.replace("s3://", "")
        bucket, key = s3_path.split("/", 1)
        result_obj = s3_client.get_object(Bucket=bucket, Key=key)
        result_data = json.loads(result_obj["Body"].read())
    else:
        pytest.skip("Expected S3 result URI")

    # Verify result structure
    assert "inference_result" in result_data, "Should have inference_result"
    assert "metadata" in result_data, "Should have metadata"

    inference_result = result_data["inference_result"]
    metadata = result_data["metadata"]

    # ⚠️ The path the run ACTUALLY took, read back from what it wrote. Every
    # assertion below holds on the traditional single-pass path too, so without
    # this the test can pass green while exercising the opposite of the thing it
    # is named for -- which is exactly what `mode: simple` made it do.
    assert metadata.get("extraction_method") == "agentic", (
        f"this test ran the {metadata.get('extraction_method')!r} path, not the "
        f"agentic one (model {extraction_model})"
    )

    # Verify key financial fields are extracted
    assert "CurrentGrossPay" in inference_result, "Should extract CurrentGrossPay"
    assert "CurrentNetPay" in inference_result, "Should extract CurrentNetPay"
    assert "YTDGrossPay" in inference_result, "Should extract YTDGrossPay"
    assert "YTDNetPay" in inference_result, "Should extract YTDNetPay"

    # Helper function to parse monetary values
    def parse_money(value):
        if value is None:
            return None
        # Remove $ and commas, convert to float
        if isinstance(value, str):
            return float(value.replace("$", "").replace(",", "").strip())
        return float(value)

    # Verify CurrentGrossPay value (known from payslip sample)
    current_gross = parse_money(inference_result.get("CurrentGrossPay"))
    assert current_gross is not None, "CurrentGrossPay should not be null"
    assert 452.43 == current_gross, "CurrentGrossPay missmatch"

    # Verify CurrentNetPay value (known from payslip sample)
    current_net = parse_money(inference_result.get("CurrentNetPay"))
    assert current_net is not None, "CurrentNetPay should not be null"
    assert current_net == 291.90, (
        f"CurrentNetPay should be ~$291.90, got ${current_net}"
    )

    # Verify YTDGrossPay value (known from payslip sample)
    ytd_gross = parse_money(inference_result.get("YTDGrossPay"))
    assert ytd_gross is not None, "YTDGrossPay should not be null"
    assert 23526.8 == ytd_gross, f"YTDGrossPay should be ~$23526.8, got ${ytd_gross}"

    # Verify date fields are present and valid
    assert "PayDate" in inference_result, "Should extract PayDate"
    pay_date = inference_result.get("PayDate")
    assert pay_date is not None, "PayDate should not be null"
    # ⚠️ The DAY is the assertion; the rendering is not. `PayDate` carries
    # `"format": "date"` in the shipped Payslip schema, and JSON Schema's
    # `date` format is RFC 3339 full-date -- so `2008-07-25` is the correct
    # answer and the US rendering is the tolerated one, not the other way
    # round. This used to demand `07/25/2008` and failed the run on a
    # schema-conformant value.
    normalized = pay_date.replace("/", "-").strip()
    assert normalized in ("2008-07-25", "07-25-2008", "7-25-2008"), (
        f"PayDate should be 25 July 2008, ideally as the schema's "
        f"RFC 3339 `2008-07-25`, got {pay_date}"
    )

    # Verify EmployeeName with exact values
    assert "EmployeeName" in inference_result, "Should extract EmployeeName"
    assert inference_result.get("EmployeeName") is not None, (
        "EmployeeName should not be null"
    )
    assert isinstance(inference_result["EmployeeName"], dict), (
        "EmployeeName should be a nested object"
    )

    employee_name = inference_result["EmployeeName"]
    assert employee_name.get("FirstName") == "JOHN", (
        f"FirstName should be JOHN, got {employee_name.get('FirstName')}"
    )
    assert employee_name.get("LastName") == "STILES", (
        f"LastName should be STILES, got {employee_name.get('LastName')}"
    )

    # Verify address fields if present
    if inference_result.get("EmployeeAddress"):
        assert isinstance(inference_result["EmployeeAddress"], dict), (
            "EmployeeAddress should be a nested object"
        )
        emp_addr = inference_result["EmployeeAddress"]
        # Check for known values if extracted
        if emp_addr.get("ZipCode"):
            assert "12345" in str(emp_addr["ZipCode"]), (
                f"Employee ZipCode should be 12345, got {emp_addr['ZipCode']}"
            )

    if inference_result.get("CompanyAddress"):
        assert isinstance(inference_result["CompanyAddress"], dict), (
            "CompanyAddress should be a nested object"
        )

    # Verify metadata contains timing information
    assert "extraction_time_seconds" in metadata, "Should have extraction time"
    assert isinstance(metadata["extraction_time_seconds"], (int, float)), (
        "Extraction time should be numeric"
    )
    assert metadata["extraction_time_seconds"] > 0, "Extraction time should be positive"

    # ⚠️ A performance check, NOT a hang bound -- it is evaluated after
    # `process_document_section` returns, so a genuine hang never reaches it. No
    # wall-clock limit exists: `pytest-timeout` is not installed and
    # `pytest.ini` sets no timeout, and the library's ceilings are per-request
    # (a 600s agent read timeout plus a 90s cumulative retry budget) with no
    # agent-loop iteration cap. Making this a real bound needs `pytest-timeout`.
    #
    # 180s against a measured ~17s on the agentic path. It was 120s when the
    # agent was bailing out in seconds on an empty prompt, so that figure
    # described nothing.
    assert metadata["extraction_time_seconds"] < 180, (
        f"Extraction took too long: {metadata['extraction_time_seconds']}s "
        f"(model {extraction_model})"
    )

    # Print complete results as formatted JSON
    print("\n" + "=" * 80)
    print("COMPLETE EXTRACTION RESULTS")
    print("=" * 80)
    print("\nFull Result Data:")
    print(json.dumps(result_data, indent=2))

    print("\n" + "=" * 80)
    print("EXTRACTION SUMMARY")
    print("=" * 80)
    print(
        f"Extracted {len([k for k, v in inference_result.items() if v is not None])} non-null fields"
    )
    print(f"Extraction time: {metadata['extraction_time_seconds']:.2f}s")

    print("\n" + "-" * 80)
    print("Verified Values:")
    print("-" * 80)
    # The expected figures here must match the assertions above. All three
    # disagreed with them ($492.43 / $291.80 / $25,508.90), and this block is
    # the first thing anyone debugging a failure reads.
    print(f"  CurrentGrossPay: ${current_gross:.2f} (expected $452.43)")
    print(f"  CurrentNetPay: ${current_net:.2f} (expected $291.90)")
    print(f"  YTDGrossPay: ${ytd_gross:,.2f} (expected $23,526.80)")
    print(
        f"  EmployeeName: {employee_name.get('FirstName')} {employee_name.get('LastName')}"
    )
    print(f"  PayDate: {pay_date}")
    print("=" * 80)
