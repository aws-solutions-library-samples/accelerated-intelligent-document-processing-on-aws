import json
import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Annotated

import boto3
import pypdfium2 as pdfium
import pytest
import yaml
from moto import mock_aws
from PIL import Image
from pydantic import BaseModel, Field, field_validator

from idp_common.models import Document, Page, Section

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

    Mocks S3 only, allowing Bedrock URLs to pass through to real AWS.
    Uses URL-based passthrough to allow Bedrock API calls while mocking S3.
    """
    # Configure mock_aws to mock S3 but passthrough all bedrock URLs to real AWS
    with mock_aws(
        config={
            "core": {
                "mock_credentials": False,
                "passthrough": {"urls": [r".*bedrock.*\.amazonaws\.com.*"]},
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
    result, _ = structured_output(
        model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
        data_format=License,
        enable_image_tools=True,
        prompt=Image.open(Path(__file__).parent / "old_cal_license.png"),
    )

    print(result)

    assert result.issue_date == "08/04/1965", result.issue_date
    assert result.expiration_date == "08/20/1970", result.expiration_date


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
        # `pattern-2` was removed by the unification; the preset lives under
        # `unified` now. The old path had rotted unnoticed because this test
        # never ran.
        / "unified"
        / "lending-package-sample"
        / "config.yaml"
    )

    with open(config_path, "r") as f:
        config_data = yaml.safe_load(f)

    CONFIG = {
        "extraction": {
            "agentic": {"enabled": True},
            "model": "us.anthropic.claude-sonnet-4-20250514-v1:0",
            "temperature": 0.0,
            "top_k": 5.0,
            "top_p": 0.1,
            "max_tokens": 4096,
            "task_prompt": config_data.get("extraction", {}).get("task_prompt", ""),
        },
        "classes": config_data.get("classes", []),
    }

    os.environ.setdefault("AWS_REGION", "us-east-1")
    os.environ.setdefault("METRIC_NAMESPACE", "IDP-Test")

    s3_client = s3_bucket["client"]
    bucket_name = s3_bucket["bucket"]

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_pdf = Path(temp_dir) / "lending_package.pdf"
        shutil.copy(sample_pdf, temp_pdf)

        pdf_doc = pdfium.PdfDocument(sample_pdf)
        first_page = pdf_doc[0]
        pil_img = first_page.render().to_pil()
        ocr_text = ""

        ocr_text_path = Path(temp_dir) / "ocr_text.txt"
        with open(ocr_text_path, "w") as f:
            f.write(ocr_text)

        s3_client.upload_file(str(ocr_text_path), bucket_name, "ocr_text.txt")

        pil_img = first_page.render().to_pil()
        img_path = Path(temp_dir) / "page_1.png"
        pil_img.save(str(img_path))
        pdf_doc.close()

        s3_client.upload_file(str(temp_pdf), bucket_name, "lending_package.pdf")
        s3_client.upload_file(str(img_path), bucket_name, "page_1.png")

        document = Document(
            id="test_lending_package",
            input_bucket=bucket_name,
            input_key="lending_package.pdf",
            output_bucket=bucket_name,
        )

        page = Page(
            page_id="1",
            image_uri=f"s3://{bucket_name}/page_1.png",
            parsed_text_uri=f"s3://{bucket_name}/ocr_text.txt",
        )
        document.pages = {"1": page}

        section = Section(
            section_id="1",
            classification="Payslip",
            page_ids=["1"],
            confidence=1.0,
        )
        document.sections = [section]

        extraction_service = ExtractionService(config=CONFIG)

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
        assert 23526.8 == ytd_gross, (
            f"YTDGrossPay should be ~$23526.8, got ${ytd_gross}"
        )

        # Verify date fields are present and valid
        assert "PayDate" in inference_result, "Should extract PayDate"
        pay_date = inference_result.get("PayDate")
        assert pay_date is not None, "PayDate should not be null"
        assert "07/25/2008" in pay_date or "7/25/2008" in pay_date, (
            f"PayDate should be 07/25/2008, got {pay_date}"
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
        assert metadata["extraction_time_seconds"] > 0, (
            "Extraction time should be positive"
        )

        # Verify reasonable extraction time (should be under 2 minutes for a single page)
        assert metadata["extraction_time_seconds"] < 120, (
            f"Extraction took too long: {metadata['extraction_time_seconds']}s"
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
        print(f"  CurrentGrossPay: ${current_gross:.2f} (expected ~$492.43)")
        print(f"  CurrentNetPay: ${current_net:.2f} (expected ~$291.80)")
        print(f"  YTDGrossPay: ${ytd_gross:,.2f} (expected ~$25,508.90)")
        print(
            f"  EmployeeName: {employee_name.get('FirstName')} {employee_name.get('LastName')}"
        )
        print(f"  PayDate: {pay_date}")
        print("=" * 80)
