# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the summarization service module.
"""

# ruff: noqa: E402, I001
# The above line disables E402 (module level import not at top of file) and I001 (import block sorting) for this file

import pytest

# Mock dependencies before importing modules
import warnings
from unittest.mock import MagicMock, patch

# Import standard library modules
import json

# Import application modules
from idp_common.summarization.service import SummarizationService
from idp_common.summarization.models import DocumentSummary
from idp_common.models import Document, Page, Section, Status


@pytest.fixture(autouse=True)
def suppress_datetime_warning():
    """Fixture to suppress the datetime.utcnow() deprecation warning from botocore."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="datetime.datetime.utcnow\\(\\) is deprecated",
            category=DeprecationWarning,
        )
        yield


@pytest.mark.unit
class TestSummarizationService:
    """Tests for the SummarizationService class."""

    @pytest.fixture
    def mock_config(self):
        """Fixture providing a mock configuration."""
        return {
            "summarization": {
                "enabled": True,
                "model": "anthropic.claude-3-sonnet-20240229-v1:0",
                "temperature": 0.0,
                "top_k": 5,
                "system_prompt": "You are a helpful assistant that summarizes documents.",
                "task_prompt": "Please summarize the following document: {DOCUMENT_TEXT}",
            }
        }

    @pytest.fixture
    def service(self, mock_config):
        """Fixture providing a SummarizationService instance."""
        return SummarizationService(region="us-west-2", config=mock_config)

    @pytest.fixture
    def sample_document(self):
        """Fixture providing a sample document with pages and sections."""
        doc = Document(
            id="test-doc",
            input_key="test-document.pdf",
            input_bucket="input-bucket",
            output_bucket="output-bucket",
            status=Status.EXTRACTING,
        )

        # Add pages
        doc.pages = {
            "1": Page(
                page_id="1",
                parsed_text_uri="s3://input-bucket/test-document.pdf/pages/1/text.txt",
            ),
            "2": Page(
                page_id="2",
                parsed_text_uri="s3://input-bucket/test-document.pdf/pages/2/text.txt",
            ),
            "3": Page(
                page_id="3",
                parsed_text_uri="s3://input-bucket/test-document.pdf/pages/3/text.txt",
            ),
        }

        # Add sections
        doc.sections = [
            Section(
                section_id="1",
                classification="invoice",
                page_ids=["1", "2"],
                extraction_result_uri="s3://input-bucket/test-document.pdf/sections/1/result.json",
            ),
            Section(
                section_id="2",
                classification="receipt",
                page_ids=["3"],
                extraction_result_uri="s3://input-bucket/test-document.pdf/sections/2/result.json",
            ),
        ]

        return doc

    def test_init(self, mock_config):
        """Test initialization with configuration."""
        service = SummarizationService(
            region="us-west-2", config=mock_config, backend="bedrock"
        )

        assert service.region == "us-west-2"
        assert service.backend == "bedrock"
        assert service.bedrock_model == "anthropic.claude-3-sonnet-20240229-v1:0"

    def test_init_invalid_backend(self, mock_config):
        """Test initialization with invalid backend."""
        service = SummarizationService(
            region="us-west-2", config=mock_config, backend="invalid"
        )

        assert service.backend == "bedrock"  # Should fall back to bedrock

    def test_init_missing_model(self):
        """Test initialization with empty config uses default model."""
        service = SummarizationService(region="us-west-2", config={})
        # Should use default model from SummarizationConfig
        # Nova Pro: the SummarizationConfig default moved off Nova Premier when
        # Premier reached end of life (2026-09-14) and stopped being invocable.
        assert service.bedrock_model == "us.amazon.nova-pro-v1:0"

    def test_get_summarization_config(self, service):
        """Test getting and validating summarization configuration."""
        config = service._get_summarization_config()

        assert config["model_id"] == "anthropic.claude-3-sonnet-20240229-v1:0"
        assert config["temperature"] == 0.0
        assert config["top_k"] == 5
        assert "You are a helpful assistant" in config["system_prompt"]
        assert "Please summarize" in config["task_prompt"]

    def test_get_summarization_config_missing_prompts(self, mock_config):
        """Test getting configuration with missing prompts."""
        # Remove system_prompt
        config_no_system = mock_config.copy()
        config_no_system["summarization"] = mock_config["summarization"].copy()
        del config_no_system["summarization"]["system_prompt"]

        service = SummarizationService(region="us-west-2", config=config_no_system)
        with pytest.raises(ValueError, match="No system_prompt found"):
            service._get_summarization_config()

        # Remove task_prompt
        config_no_task = mock_config.copy()
        config_no_task["summarization"] = mock_config["summarization"].copy()
        del config_no_task["summarization"]["task_prompt"]

        service = SummarizationService(region="us-west-2", config=config_no_task)
        with pytest.raises(ValueError, match="No task_prompt found"):
            service._get_summarization_config()

    def test_extract_json_code_block(self):
        """Test extracting JSON from code block format."""
        from idp_common.utils import extract_json_from_text

        text = 'Here is the summary:\n```json\n{"summary": "This is a summary", "key_points": ["Point 1", "Point 2"]}\n```\nI hope this helps!'

        json_str = extract_json_from_text(text)
        parsed = json.loads(json_str)

        assert parsed["summary"] == "This is a summary"
        assert parsed["key_points"] == ["Point 1", "Point 2"]

    def test_extract_json_braces(self):
        """Test extracting JSON between braces."""
        from idp_common.utils import extract_json_from_text

        text = 'Here is the summary: {"summary": "This is a summary", "key_points": ["Point 1", "Point 2"]} I hope this helps!'

        json_str = extract_json_from_text(text)
        parsed = json.loads(json_str)

        assert parsed["summary"] == "This is a summary"
        assert parsed["key_points"] == ["Point 1", "Point 2"]

    def test_extract_json_normalized(self):
        """Test extracting JSON with normalization."""
        from idp_common.utils import extract_json_from_text

        text = 'Here is the summary: {\n"summary": "This is a summary",\n"key_points": ["Point 1", "Point 2"]\n} I hope this helps!'

        json_str = extract_json_from_text(text)
        parsed = json.loads(json_str)

        assert parsed["summary"] == "This is a summary"
        assert parsed["key_points"] == ["Point 1", "Point 2"]

    def test_extract_json_empty(self):
        """Test extracting JSON from empty text."""
        from idp_common.utils import extract_json_from_text

        json_str = extract_json_from_text("")
        assert json_str == ""

    def test_create_error_summary(self, service):
        """Test creating an error summary."""
        error_message = "Test error message"
        summary = service._create_error_summary(error_message)

        assert summary.content["error"] == "Error generating summary"
        assert summary.metadata["error"] == error_message

    @patch("idp_common.bedrock.invoke_model")
    def test_invoke_bedrock_model(self, mock_invoke_model, service):
        """Test invoking Bedrock model."""
        # Configure mock
        mock_invoke_model.return_value = {
            "response": {
                "output": {"message": {"content": [{"text": "Test response"}]}}
            },
            "metering": {"input_tokens": 100, "output_tokens": 50},
        }

        content = [{"text": "Test content"}]
        config = {
            "model_id": "test-model",
            "system_prompt": "Test system prompt",
            "temperature": 0.0,
            "top_k": 5,
            "top_p": 0.1,
            "max_tokens": 5000,
        }

        result = service._invoke_bedrock_model(content, config)

        # Verify invoke_model was called with correct parameters
        mock_invoke_model.assert_called_once_with(
            model_id=config["model_id"],
            system_prompt=config["system_prompt"],
            content=content,
            temperature=config["temperature"],
            top_k=config["top_k"],
            top_p=config["top_p"],
            max_tokens=config["max_tokens"],
            context="Summarization",
            model_lambda_hook_arn=None,
            reasoning_effort=None,
        )

        # Verify result
        assert (
            result["response"]["output"]["message"]["content"][0]["text"]
            == "Test response"
        )
        assert result["metering"]["input_tokens"] == 100
        assert result["metering"]["output_tokens"] == 50

    @patch("idp_common.bedrock.invoke_model")
    def test_process_text_success(self, mock_invoke_model, service):
        """Test processing text successfully."""
        # Configure mock
        mock_invoke_model.return_value = {
            "response": {
                "output": {
                    "message": {
                        "content": [
                            {
                                "text": '{"summary": "This is a summary", "key_points": ["Point 1", "Point 2"]}'
                            }
                        ]
                    }
                }
            },
            "metering": {"input_tokens": 100, "output_tokens": 50},
        }

        result = service.process_text("Test document text")

        # Verify result
        assert result.content["summary"] == "This is a summary"
        assert result.content["key_points"] == ["Point 1", "Point 2"]
        assert result.metadata["metering"]["input_tokens"] == 100
        assert result.metadata["metering"]["output_tokens"] == 50

    @patch("idp_common.bedrock.invoke_model")
    def test_process_text_empty(self, mock_invoke_model, service):
        """Test processing empty text."""
        result = service.process_text("")

        # Verify result
        assert result.content["error"] == "Error generating summary"
        assert result.metadata["error"] == "Empty text provided"

        # Verify invoke_model was not called
        mock_invoke_model.assert_not_called()

    @pytest.mark.skip(reason="Temporarily disabled due to exception handling issues")
    @patch("idp_common.bedrock.invoke_model")
    def test_process_text_error(self, mock_invoke_model, service):
        """Test processing text with error."""
        # Configure mock to raise exception
        mock_invoke_model.side_effect = Exception("Test error")

        # Expect exception to be raised
        with pytest.raises(Exception, match="Test error"):
            service.process_text("Test document text")

    @patch("idp_common.s3.get_text_content")
    @patch("idp_common.s3.write_content")
    @patch("idp_common.summarization.service.SummarizationService.process_text")
    @patch("idp_common.utils.merge_metering_data")
    def test_process_document_section(
        self,
        mock_merge_metering,
        mock_process_text,
        mock_write_content,
        mock_get_text_content,
        service,
        sample_document,
    ):
        """Test processing a document section."""
        # Configure mocks
        mock_get_text_content.side_effect = ["Page 1 text", "Page 2 text"]

        summary = DocumentSummary(
            content={
                "summary": "This is a summary",
                "key_points": ["Point 1", "Point 2"],
            },
            metadata={"metering": {"input_tokens": 100, "output_tokens": 50}},
        )
        mock_process_text.return_value = summary

        # Configure merge_metering_data mock to return expected structure
        mock_merge_metering.return_value = {"input_tokens": 100, "output_tokens": 50}

        # Process section
        result, section_metering = service.process_document_section(
            sample_document, "1"
        )

        # Verify get_text_content was called for each page
        assert mock_get_text_content.call_count == 2

        # Verify process_text was called with combined text
        mock_process_text.assert_called_once()
        call_args = mock_process_text.call_args[0][0]
        assert "<page-number>1</page-number>" in call_args
        assert "<page-number>2</page-number>" in call_args
        assert "Page 1 text" in call_args
        assert "Page 2 text" in call_args

        # Verify write_content was called twice (JSON and Markdown)
        assert mock_write_content.call_count == 2

        # Verify section attributes were updated
        section = next(s for s in result.sections if s.section_id == "1")
        assert "summary_uri" in section.attributes
        assert "summary_md_uri" in section.attributes

        # Verify section metering data was returned
        assert section_metering == {"input_tokens": 100, "output_tokens": 50}

    @patch("idp_common.s3.get_text_content")
    def test_process_document_section_no_pages(
        self, mock_get_text_content, service, sample_document
    ):
        """Test processing a document section with no pages."""
        # Modify section to have no pages
        for section in sample_document.sections:
            if section.section_id == "1":
                section.page_ids = []

        # Process section
        result, section_metering = service.process_document_section(
            sample_document, "1"
        )

        # Verify get_text_content was not called
        mock_get_text_content.assert_not_called()

        # Verify error was added
        assert "Section 1 has no page IDs" in result.errors

        # Verify empty metering data was returned for error case
        assert section_metering == {}

    @patch("idp_common.s3.get_text_content")
    def test_process_document_section_invalid_section(
        self, mock_get_text_content, service, sample_document
    ):
        """Test processing an invalid document section."""
        # Process non-existent section
        result, section_metering = service.process_document_section(
            sample_document, "999"
        )

        # Verify get_text_content was not called
        mock_get_text_content.assert_not_called()

        # Verify error was added
        assert "Section 999 not found in document" in result.errors

        # Verify empty metering data was returned for error case
        assert section_metering == {}

    @patch("concurrent.futures.ThreadPoolExecutor")
    @patch("idp_common.s3.get_json_content")
    @patch("idp_common.s3.write_content")
    def test_process_document(
        self,
        mock_write_content,
        mock_get_json_content,
        mock_executor,
        service,
        sample_document,
    ):
        """Test processing a complete document."""
        # Configure mocks
        mock_executor_instance = MagicMock()
        mock_executor.return_value.__enter__.return_value = mock_executor_instance

        # Mock the future results
        future1 = MagicMock()
        future2 = MagicMock()

        # Configure the futures to return (document, metering) tuples
        doc1 = sample_document
        doc1.sections[0].attributes = {
            "summary_uri": "s3://output-bucket/test-document.pdf/sections/1/summary.json"
        }
        section1_metering = {"input_tokens": 100, "output_tokens": 50}
        future1.result.return_value = (doc1, section1_metering)

        doc2 = sample_document
        doc2.sections[1].attributes = {
            "summary_uri": "s3://output-bucket/test-document.pdf/sections/2/summary.json"
        }
        section2_metering = {"input_tokens": 150, "output_tokens": 75}
        future2.result.return_value = (doc2, section2_metering)

        # Configure the executor to return the futures
        mock_executor_instance.submit.side_effect = [future1, future2]

        # Mock as_completed to return our futures in order
        with patch("concurrent.futures.as_completed", return_value=[future1, future2]):
            # Mock get_json_content to return summary content
            mock_get_json_content.side_effect = [
                {"summary": "Section 1 summary"},
                {"summary": "Section 2 summary"},
            ]

            # Process document
            result = service.process_document(sample_document)

            # Verify executor was used to process sections in parallel
            assert mock_executor_instance.submit.call_count == 2

            # Verify write_content was called for combined results (JSON, fulltext, and markdown)
            assert mock_write_content.call_count == 3

            # Verify document has summarization_result
            assert result.summarization_result is not None
            assert result.summary_report_uri is not None

    @patch("idp_common.s3.get_text_content")
    @patch("idp_common.s3.write_content")
    @patch("idp_common.summarization.service.SummarizationService.process_text")
    @patch("idp_common.utils.merge_metering_data")
    def test_process_document_as_whole(
        self,
        mock_merge_metering,
        mock_process_text,
        mock_write_content,
        mock_get_text_content,
        service,
        sample_document,
    ):
        """Test processing a document as a whole."""
        # Configure mocks
        mock_get_text_content.side_effect = [
            "Page 1 text",
            "Page 2 text",
            "Page 3 text",
        ]

        summary = DocumentSummary(
            content={
                "summary": "This is a summary",
                "key_points": ["Point 1", "Point 2"],
            },
            metadata={"metering": {"input_tokens": 100, "output_tokens": 50}},
        )
        mock_process_text.return_value = summary

        # Configure merge_metering_data mock to return expected structure
        mock_merge_metering.return_value = {"input_tokens": 100, "output_tokens": 50}

        # Remove sections to force processing as whole
        sample_document.sections = []

        # Process document
        result = service._process_document_as_whole(sample_document)

        # Verify get_text_content was called for each page
        assert mock_get_text_content.call_count == 3

        # Verify process_text was called with combined text
        mock_process_text.assert_called_once()
        call_args = mock_process_text.call_args[0][0]
        assert "<page-number>1</page-number>" in call_args
        assert "<page-number>2</page-number>" in call_args
        assert "<page-number>3</page-number>" in call_args

        # Verify write_content was called three times (JSON, fulltext, and Markdown)
        assert mock_write_content.call_count == 3

        # Verify document has summarization_result
        assert result.summarization_result is not None
        assert result.summary_report_uri is not None

        # Verify document metering was updated
        assert result.metering["input_tokens"] == 100
        assert result.metering["output_tokens"] == 50

    def test_update_document_status(self, service, sample_document):
        """Test updating document status."""
        # Test successful update
        result1 = service._update_document_status(sample_document)
        assert result1.status == Status.EXTRACTING  # Status unchanged

        # Test failed update
        result2 = service._update_document_status(
            sample_document, success=False, error_message="Test error"
        )
        assert result2.status == Status.FAILED
        assert "Test error" in result2.errors

    @patch("idp_common.bedrock.invoke_model")
    def test_process_document_enabled_true(self, mock_invoke_model, sample_document):
        """Test that summarization proceeds normally when enabled=true."""
        config = {
            "summarization": {
                "enabled": True,
                "model": "anthropic.claude-3-sonnet-20240229-v1:0",
                "temperature": 0.0,
                "top_k": 5,
                "system_prompt": "You are a helpful assistant.",
                "task_prompt": "Summarize: {DOCUMENT_TEXT}",
            }
        }

        service = SummarizationService(region="us-west-2", config=config)

        # Configure mock to return a valid response
        mock_invoke_model.return_value = {
            "response": {
                "output": {
                    "message": {"content": [{"text": '{"summary": "Test summary"}'}]}
                }
            },
            "metering": {"input_tokens": 100, "output_tokens": 50},
        }

        # Mock S3 operations
        with patch("idp_common.s3.get_text_content") as mock_get_text:
            mock_get_text.side_effect = ["Page 1", "Page 2", "Page 3"]
            with patch("idp_common.s3.write_content"):
                with patch("idp_common.utils.merge_metering_data") as mock_merge:
                    mock_merge.return_value = {"input_tokens": 100, "output_tokens": 50}

                    # Remove sections to use whole document processing
                    sample_document.sections = []

                    result = service.process_document(sample_document)

        # Verify that LLM was actually called (summarization proceeded)
        mock_invoke_model.assert_called_once()

        # Verify document has summarization results
        assert result.summarization_result is not None
        assert result.summary_report_uri is not None

    @patch("idp_common.bedrock.invoke_model")
    def test_process_document_enabled_false(self, mock_invoke_model, sample_document):
        """Test that summarization is skipped when enabled=false."""
        config = {
            "summarization": {
                "enabled": False,
                "model": "anthropic.claude-3-sonnet-20240229-v1:0",
                "temperature": 0.0,
                "top_k": 5,
                "system_prompt": "You are a helpful assistant.",
                "task_prompt": "Summarize: {DOCUMENT_TEXT}",
            }
        }

        service = SummarizationService(region="us-west-2", config=config)

        # Set original status to something other than FAILED
        original_status = Status.EXTRACTING
        sample_document.status = original_status

        result = service.process_document(sample_document)

        # Verify that LLM was NOT called (summarization was skipped)
        mock_invoke_model.assert_not_called()

        # Verify document status was updated to COMPLETED
        assert result.status == Status.COMPLETED

        # Verify no summarization results were created
        assert result.summarization_result is None
        assert result.summary_report_uri is None

    @patch("idp_common.bedrock.invoke_model")
    def test_process_document_enabled_missing_defaults_to_true(
        self, mock_invoke_model, sample_document
    ):
        """Test that summarization proceeds when enabled property is missing (defaults to true)."""
        config = {
            "summarization": {
                # No 'enabled' property - defaults to True
                "model": "anthropic.claude-3-sonnet-20240229-v1:0",
                "temperature": 0.0,
                "top_k": 5,
                "system_prompt": "You are a helpful assistant.",
                "task_prompt": "Summarize: {DOCUMENT_TEXT}",
            }
        }

        service = SummarizationService(region="us-west-2", config=config)

        # Configure mock to return a valid response
        mock_invoke_model.return_value = {
            "response": {
                "output": {
                    "message": {"content": [{"text": '{"summary": "Test summary"}'}]}
                }
            },
            "metering": {"input_tokens": 100, "output_tokens": 50},
        }

        # Mock S3 operations
        with patch("idp_common.s3.get_text_content") as mock_get_text:
            mock_get_text.side_effect = ["Page 1", "Page 2", "Page 3"]
            with patch("idp_common.s3.write_content"):
                with patch("idp_common.utils.merge_metering_data") as mock_merge:
                    mock_merge.return_value = {"input_tokens": 100, "output_tokens": 50}

                    # Remove sections to use whole document processing
                    sample_document.sections = []

                    result = service.process_document(sample_document)

        # Verify that LLM was called (summarization proceeded by default)
        mock_invoke_model.assert_called_once()

        # Verify document has summarization results
        assert result.summarization_result is not None
        assert result.summary_report_uri is not None

    @patch("idp_common.bedrock.invoke_model")
    def test_process_document_enabled_false_preserves_failed_status(
        self, mock_invoke_model, sample_document
    ):
        """Test that when enabled=false, FAILED status is preserved."""
        config = {
            "summarization": {
                "enabled": False,
                "model": "anthropic.claude-3-sonnet-20240229-v1:0",
                "temperature": 0.0,
                "top_k": 5,
                "system_prompt": "You are a helpful assistant.",
                "task_prompt": "Summarize: {DOCUMENT_TEXT}",
            }
        }

        service = SummarizationService(region="us-west-2", config=config)

        # Set document status to FAILED
        sample_document.status = Status.FAILED

        result = service.process_document(sample_document)

        # Verify that LLM was NOT called
        mock_invoke_model.assert_not_called()

        # Verify document status remained FAILED (not changed to COMPLETED)
        assert result.status == Status.FAILED

    @patch("idp_common.bedrock.invoke_model")
    @patch("idp_common.s3.get_text_content")
    def test_process_document_section_respects_enabled_config(
        self, mock_get_text, mock_invoke_model, sample_document
    ):
        """Test that process_document_section bypasses processing when enabled=false via process_document."""
        config = {
            "summarization": {
                "enabled": False,
                "model": "anthropic.claude-3-sonnet-20240229-v1:0",
                "temperature": 0.0,
                "top_k": 5,
                "system_prompt": "You are a helpful assistant.",
                "task_prompt": "Summarize: {DOCUMENT_TEXT}",
            }
        }

        service = SummarizationService(region="us-west-2", config=config)

        # Set original status
        sample_document.status = Status.EXTRACTING

        # Call process_document which should skip entirely
        result = service.process_document(sample_document)

        # Verify that neither bedrock nor S3 operations were called
        mock_invoke_model.assert_not_called()
        mock_get_text.assert_not_called()

        # Verify document status was updated to COMPLETED
        assert result.status == Status.COMPLETED

    def test_process_document_no_pages(self, service, sample_document):
        """Test processing document with no pages."""
        # Configure with enabled=true to ensure we test the no-pages logic
        config = {
            "summarization": {
                "enabled": True,
                "model": "anthropic.claude-3-sonnet-20240229-v1:0",
                "temperature": 0.0,
                "top_k": 5,
                "system_prompt": "You are a helpful assistant.",
                "task_prompt": "Summarize: {DOCUMENT_TEXT}",
            }
        }

        service = SummarizationService(region="us-west-2", config=config)

        # Remove pages
        sample_document.pages = {}

        result = service.process_document(sample_document)

        # Verify error was added and status set to FAILED
        assert "Document has no pages to summarize" in result.errors
        assert result.status == Status.FAILED


@pytest.mark.unit
class TestExtractionArrayElision:
    """Tests for Fix A: compact/elide extraction JSON before injection."""

    def test_elide_large_array_replaces_with_head_tail_and_marker(self):
        from idp_common.summarization.service import _elide_large_arrays

        data = {"transactions": [{"i": i} for i in range(6400)], "total": 6400}
        out = _elide_large_arrays(data, 50)

        # head (3) + marker (1) + tail (2) == 6 entries
        assert len(out["transactions"]) == 6
        assert out["transactions"][0] == {"i": 0}
        assert out["transactions"][-1] == {"i": 6399}
        assert "6400 items total" in out["transactions"][3]
        # Scalars/counts preserved
        assert out["total"] == 6400

    def test_small_array_not_elided(self):
        from idp_common.summarization.service import _elide_large_arrays

        data = {"items": [1, 2, 3]}
        assert _elide_large_arrays(data, 50) == {"items": [1, 2, 3]}

    def test_cap_zero_disables_elision(self):
        from idp_common.summarization.service import _elide_large_arrays

        data = {"items": list(range(1000))}
        assert _elide_large_arrays(data, 0) == data

    def test_nested_arrays_elided(self):
        from idp_common.summarization.service import _elide_large_arrays

        data = {"outer": {"inner": list(range(500))}}
        out = _elide_large_arrays(data, 10)
        assert len(out["outer"]["inner"]) == 6

    @patch("idp_common.bedrock.invoke_model")
    def test_process_text_injects_compact_elided_extraction(self, mock_invoke_model):
        """The prompt sent to Bedrock must NOT contain the full 6400-row list."""
        mock_invoke_model.return_value = {
            "response": {
                "output": {"message": {"content": [{"text": '{"summary": "ok"}'}]}}
            },
            "metering": {"input_tokens": 10, "output_tokens": 5},
        }
        config = {
            "summarization": {
                "enabled": True,
                "model": "us.amazon.nova-pro-v1:0",
                "system_prompt": "sys",
                "task_prompt": "Doc: {DOCUMENT_TEXT}\nAttrs: {EXTRACTION_RESULTS}",
                "max_extraction_array_items": 50,
            }
        }
        service = SummarizationService(region="us-west-2", config=config)
        extraction = {"transactions": [{"i": i} for i in range(6400)]}
        service.process_text("short document text", extraction)

        sent_prompt = mock_invoke_model.call_args.kwargs["content"][0]["text"]
        # Elided: marker present, not every row.
        assert "6400 items total" in sent_prompt
        # Compact (no indent) — pretty-printed 2-space indent should be absent.
        assert '\n  "transactions"' not in sent_prompt


@pytest.mark.unit
class TestFitOrSkipGuard:
    """Tests for Fix B: fit-or-truncate guard + graceful overflow degradation."""

    def _make_service(self, model="us.amazon.nova-pro-v1:0"):
        config = {
            "summarization": {
                "enabled": True,
                "model": model,
                "system_prompt": "sys",
                "task_prompt": "Doc: {DOCUMENT_TEXT}\nAttrs: {EXTRACTION_RESULTS}",
            }
        }
        return SummarizationService(region="us-west-2", config=config)

    def test_build_placeholders_truncates_oversized_text(self):
        service = self._make_service()
        config = service._get_summarization_config()
        # ~2M chars ≈ 500K tokens, far over Nova Pro's 300K window.
        huge_text = "word " * 400_000
        placeholders, truncated = service._build_placeholders(huge_text, {}, config)
        assert truncated is True
        assert len(placeholders["DOCUMENT_TEXT"]) < len(huge_text)
        assert "truncated" in placeholders["DOCUMENT_TEXT"].lower()

    def test_build_placeholders_no_truncation_when_small(self):
        service = self._make_service()
        config = service._get_summarization_config()
        placeholders, truncated = service._build_placeholders(
            "small text", {"a": 1}, config
        )
        assert truncated is False
        assert placeholders["DOCUMENT_TEXT"] == "small text"

    @patch("idp_common.bedrock.invoke_model")
    def test_process_text_overflow_returns_stub_not_raise(self, mock_invoke_model):
        """An input-token overflow from Bedrock must degrade to a stub, not raise."""
        mock_invoke_model.side_effect = Exception(
            "ValidationException: Input Tokens Exceeded"
        )
        service = self._make_service()
        result = service.process_text("some document text", {"a": 1})

        assert result.metadata.get("summarization_skipped") is True
        assert result.metadata.get("skip_reason") == "input_token_overflow"
        assert "Summary Unavailable" in result.content["summary"]

    @patch("idp_common.bedrock.invoke_model")
    def test_process_text_non_overflow_error_still_raises(self, mock_invoke_model):
        """Non-overflow errors must still propagate (not silently swallowed)."""
        mock_invoke_model.side_effect = Exception("AccessDeniedException")
        service = self._make_service()
        with pytest.raises(Exception, match="AccessDeniedException"):
            service.process_text("some document text", {"a": 1})
