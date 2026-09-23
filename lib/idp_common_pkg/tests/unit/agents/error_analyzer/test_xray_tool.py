# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the error analyzer's X-Ray tools.

Two public tools, `analyze_document_trace` and `analyze_system_performance`, over a
layer of segment parsing and threshold comparison. The thresholds are the part
worth testing hardest: `_analyze_trace_segments` and `_analyze_service_performance`
each read a configured value and decide whether a segment or a service is "slow"
or "high error", and those verdicts are the entire content of the recommendations
the agent then reports. A threshold compared in the wrong unit — X-Ray segment
times are **seconds**, the thresholds are **milliseconds** — turns every segment
slow or none of them, and either way the report reads plausibly.

The `xray_client` is a stub throughout and `get_ea_param` is patched where a
threshold matters, so the tests state the threshold they are testing against
instead of depending on whatever the bundled config happens to hold.

One asymmetry to know when reading these: trace lookup tries DynamoDB first and
falls back to an X-Ray annotation query, and the DynamoDB half swallows its own
exceptions while the annotation half does not. So a DynamoDB failure degrades to
the fallback and an X-Ray failure reaches the tool's outer handler. Both paths are
asserted.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from idp_common.agents.error_analyzer.tools.xray_tool import (
    _analyze_service_performance,
    _analyze_stack_traces,
    _analyze_trace_segments,
    _build_service_timeline,
    _build_trace_analysis_response,
    _create_trace_not_found_response,
    _extract_trace_summary,
    _find_trace_id_for_document,
    _generate_recommendations,
    _get_trace_id_from_dynamodb,
    _get_trace_id_from_xray_annotations,
    _get_trace_segments,
    _parse_segment_document,
    _parse_segment_for_lambda,
    analyze_document_trace,
    analyze_system_performance,
    extract_lambda_request_ids,
)

MODULE = "idp_common.agents.error_analyzer.tools.xray_tool"
TRACE_ID = "1-5f84c7a1-0123456789abcdef01234567"


def _segment(
    *,
    name: str = "OCRFunction",
    start: float = 100.0,
    end: float = 101.0,
    error: bool = False,
    fault: bool = False,
    as_string: bool = False,
    **extra: Any,
):
    """One X-Ray segment, wrapped the way batch_get_traces returns it."""
    doc: dict[str, Any] = {
        "id": "seg-1",
        "name": name,
        "start_time": start,
        "end_time": end,
    }
    if error:
        doc["error"] = True
    if fault:
        doc["fault"] = True
    doc.update(extra)
    return {"Document": json.dumps(doc) if as_string else doc}


def _fixed_thresholds(**overrides):
    """Patch get_ea_param so each test states the thresholds it relies on."""
    values = {
        "xray_slow_segment_threshold_ms": 5000,
        "xray_analysis_hours": 3,
        "xray_error_rate_threshold": 0.05,
        "xray_response_time_threshold_ms": 10000,
    }
    values.update(overrides)
    return patch(
        f"{MODULE}.get_ea_param",
        side_effect=lambda field, default: values.get(field, default),
    )


@pytest.mark.unit
class TestParseSegmentDocument:
    """_parse_segment_document: X-Ray returns the document as a JSON string."""

    def test_a_json_string_is_parsed(self):
        assert _parse_segment_document('{"name": "OCR"}') == {"name": "OCR"}

    def test_a_dict_is_returned_unchanged(self):
        doc = {"name": "OCR"}
        assert _parse_segment_document(doc) is doc

    @pytest.mark.parametrize("value", ["", "not json", "{unclosed"])
    def test_unparseable_text_becomes_an_empty_dict_rather_than_raising(self, value):
        # A single malformed segment must not abort analysis of the whole trace.
        assert _parse_segment_document(value) == {}

    @pytest.mark.parametrize("value", [None, {}])
    def test_absent_input_becomes_an_empty_dict(self, value):
        assert _parse_segment_document(value) == {}


@pytest.mark.unit
class TestAnalyzeTraceSegments:
    """_analyze_trace_segments: durations, error segments, slow segments."""

    def test_no_segments_reports_an_error_rather_than_a_clean_bill(self):
        # Zero segments means the trace could not be read, not that nothing was
        # slow. Reporting "no performance issues" would be a false all-clear.
        assert _analyze_trace_segments([]) == {"error": "No trace segments available"}

    def test_durations_are_summed_and_converted_to_milliseconds(self):
        # X-Ray reports epoch seconds; the response is in ms. Getting this backwards
        # makes every threshold comparison meaningless.
        with _fixed_thresholds():
            result = _analyze_trace_segments(
                [_segment(start=100.0, end=101.5), _segment(start=101.5, end=102.0)]
            )
        assert result["total_duration_ms"] == pytest.approx(2000.0)
        assert result["total_segments"] == 2

    def test_an_error_segment_is_recorded_with_its_cause(self):
        with _fixed_thresholds():
            result = _analyze_trace_segments(
                [
                    _segment(
                        error=True,
                        cause={"exceptions": [{"message": "AccessDenied"}]},
                    )
                ]
            )
        assert len(result["error_segments"]) == 1
        assert result["error_segments"][0]["cause"] == [{"message": "AccessDenied"}]
        assert result["has_performance_issues"] is True

    def test_a_fault_counts_as_an_error_segment(self):
        # error and fault are different X-Ray fields for client- and server-side
        # failures; both mean the segment failed.
        with _fixed_thresholds():
            result = _analyze_trace_segments([_segment(fault=True)])
        assert len(result["error_segments"]) == 1

    def test_a_missing_cause_yields_an_empty_exception_list_not_a_crash(self):
        with _fixed_thresholds():
            result = _analyze_trace_segments([_segment(error=True)])
        assert result["error_segments"][0]["cause"] == []

    def test_a_segment_over_the_threshold_is_slow(self):
        with _fixed_thresholds(xray_slow_segment_threshold_ms=5000):
            result = _analyze_trace_segments([_segment(start=0.0, end=6.0)])
        assert len(result["slow_segments"]) == 1
        assert result["slow_segments"][0]["duration_ms"] == pytest.approx(6000.0)
        assert result["has_performance_issues"] is True

    def test_a_segment_under_the_threshold_is_not_slow(self):
        with _fixed_thresholds(xray_slow_segment_threshold_ms=5000):
            result = _analyze_trace_segments([_segment(start=0.0, end=4.0)])
        assert result["slow_segments"] == []
        assert result["has_performance_issues"] is False

    def test_a_segment_exactly_at_the_threshold_is_not_slow(self):
        # Strictly greater than, so the boundary is not an issue. Pinned because
        # flipping it makes a correctly-configured threshold report every segment.
        with _fixed_thresholds(xray_slow_segment_threshold_ms=5000):
            result = _analyze_trace_segments([_segment(start=0.0, end=5.0)])
        assert result["slow_segments"] == []

    def test_the_configured_threshold_is_honoured_rather_than_a_hardcoded_one(self):
        with _fixed_thresholds(xray_slow_segment_threshold_ms=500):
            result = _analyze_trace_segments([_segment(start=0.0, end=1.0)])
        assert len(result["slow_segments"]) == 1

    def test_a_clean_trace_reports_no_performance_issues(self):
        with _fixed_thresholds():
            result = _analyze_trace_segments([_segment(start=0.0, end=0.5)])
        assert result["has_performance_issues"] is False
        assert result["error_segments"] == []

    def test_an_unparseable_segment_is_skipped_and_the_rest_analysed(self):
        with _fixed_thresholds():
            result = _analyze_trace_segments(
                [{"Document": "not json"}, _segment(start=0.0, end=1.0)]
            )
        # total_segments counts what was handed in; only parseable ones contribute.
        assert result["total_segments"] == 2
        assert result["total_duration_ms"] == pytest.approx(1000.0)

    def test_string_and_dict_documents_analyse_identically(self):
        with _fixed_thresholds():
            as_dict = _analyze_trace_segments([_segment(start=0.0, end=1.0)])
            as_str = _analyze_trace_segments(
                [_segment(start=0.0, end=1.0, as_string=True)]
            )
        assert as_dict == as_str


@pytest.mark.unit
class TestBuildServiceTimeline:
    """_build_service_timeline: chronological order is the whole point."""

    def test_segments_are_returned_in_start_time_order_regardless_of_input_order(self):
        timeline = _build_service_timeline(
            [
                _segment(name="Extract", start=200.0, end=201.0),
                _segment(name="OCR", start=100.0, end=101.0),
            ]
        )
        assert [entry["service_name"] for entry in timeline] == ["OCR", "Extract"]

    def test_each_entry_carries_its_duration_in_milliseconds(self):
        timeline = _build_service_timeline([_segment(start=100.0, end=101.5)])
        assert timeline[0]["duration_ms"] == pytest.approx(1500.0)

    def test_an_error_sets_has_error(self):
        assert _build_service_timeline([_segment(error=True)])[0]["has_error"] is True

    def test_a_fault_sets_has_error(self):
        assert _build_service_timeline([_segment(fault=True)])[0]["has_error"] is True

    def test_a_clean_segment_does_not_set_has_error(self):
        assert _build_service_timeline([_segment()])[0]["has_error"] is False

    def test_annotations_are_carried_through_for_the_agent_to_read(self):
        timeline = _build_service_timeline(
            [_segment(annotations={"document_id": "report.pdf"})]
        )
        assert timeline[0]["annotations"] == {"document_id": "report.pdf"}

    def test_an_unparseable_segment_is_dropped_rather_than_ordered_at_zero(self):
        # A segment that sorted to position zero with no name would look like the
        # first thing that ran.
        timeline = _build_service_timeline([{"Document": "not json"}, _segment()])
        assert len(timeline) == 1

    def test_no_segments_gives_an_empty_timeline(self):
        assert _build_service_timeline([]) == []


@pytest.mark.unit
class TestTraceIdLookup:
    """_find_trace_id_for_document and its two sources."""

    def test_dynamodb_returns_the_stored_trace_id(self, monkeypatch):
        monkeypatch.setenv("TRACKING_TABLE_NAME", "tracking")
        with patch(f"{MODULE}.boto3.resource") as resource:
            table = resource.return_value.Table.return_value
            table.get_item.return_value = {"Item": {"TraceId": TRACE_ID}}
            assert _get_trace_id_from_dynamodb("report.pdf") == TRACE_ID
        key = table.get_item.call_args.kwargs["Key"]
        assert key == {"PK": "doc#report.pdf", "SK": "none"}

    def test_no_tracking_table_configured_skips_dynamodb_entirely(self, monkeypatch):
        monkeypatch.delenv("TRACKING_TABLE_NAME", raising=False)
        with patch(f"{MODULE}.boto3.resource") as resource:
            assert _get_trace_id_from_dynamodb("report.pdf") is None
        resource.assert_not_called()

    def test_an_absent_item_yields_nothing(self, monkeypatch):
        monkeypatch.setenv("TRACKING_TABLE_NAME", "tracking")
        with patch(f"{MODULE}.boto3.resource") as resource:
            resource.return_value.Table.return_value.get_item.return_value = {}
            assert _get_trace_id_from_dynamodb("report.pdf") is None

    def test_a_dynamodb_failure_is_swallowed_so_the_fallback_can_run(self, monkeypatch):
        # This half deliberately does not propagate: a missing table or a denied
        # read should fall through to the annotation query rather than fail the tool.
        monkeypatch.setenv("TRACKING_TABLE_NAME", "tracking")
        with patch(
            f"{MODULE}.boto3.resource", side_effect=RuntimeError("AccessDenied")
        ):
            assert _get_trace_id_from_dynamodb("report.pdf") is None

    def test_the_annotation_query_filters_on_the_document_id(self):
        client = MagicMock()
        client.get_trace_summaries.return_value = {"TraceSummaries": [{"Id": TRACE_ID}]}
        assert _get_trace_id_from_xray_annotations("report.pdf", client) == TRACE_ID
        expression = client.get_trace_summaries.call_args.kwargs["FilterExpression"]
        assert expression == 'annotation.document_id = "report.pdf"'

    def test_the_annotation_query_looks_back_twenty_four_hours(self):
        client = MagicMock()
        client.get_trace_summaries.return_value = {"TraceSummaries": []}
        _get_trace_id_from_xray_annotations("report.pdf", client)
        kwargs = client.get_trace_summaries.call_args.kwargs
        assert kwargs["EndTime"] - kwargs["StartTime"] == timedelta(hours=24)

    def test_the_annotation_query_returns_the_first_of_several_traces(self):
        client = MagicMock()
        client.get_trace_summaries.return_value = {
            "TraceSummaries": [{"Id": TRACE_ID}, {"Id": "other"}]
        }
        assert _get_trace_id_from_xray_annotations("report.pdf", client) == TRACE_ID

    def test_no_matching_traces_yields_nothing(self):
        client = MagicMock()
        client.get_trace_summaries.return_value = {"TraceSummaries": []}
        assert _get_trace_id_from_xray_annotations("report.pdf", client) is None

    def test_dynamodb_is_preferred_and_the_annotation_query_is_not_run(self):
        client = MagicMock()
        with patch(f"{MODULE}._get_trace_id_from_dynamodb", return_value=TRACE_ID):
            assert _find_trace_id_for_document("report.pdf", client) == TRACE_ID
        client.get_trace_summaries.assert_not_called()

    def test_the_annotation_query_runs_when_dynamodb_has_nothing(self):
        client = MagicMock()
        client.get_trace_summaries.return_value = {"TraceSummaries": [{"Id": TRACE_ID}]}
        with patch(f"{MODULE}._get_trace_id_from_dynamodb", return_value=None):
            assert _find_trace_id_for_document("report.pdf", client) == TRACE_ID
        client.get_trace_summaries.assert_called_once()


@pytest.mark.unit
class TestGetTraceSegments:
    """_get_trace_segments: unwrapping batch_get_traces."""

    def test_segments_are_returned_from_the_first_trace(self):
        client = MagicMock()
        client.batch_get_traces.return_value = {
            "Traces": [{"Segments": [{"Document": "{}"}]}]
        }
        assert _get_trace_segments(client, TRACE_ID) == [{"Document": "{}"}]
        assert client.batch_get_traces.call_args.kwargs["TraceIds"] == [TRACE_ID]

    def test_no_traces_gives_an_empty_list(self):
        client = MagicMock()
        client.batch_get_traces.return_value = {"Traces": []}
        assert _get_trace_segments(client, TRACE_ID) == []

    def test_a_trace_with_no_segments_gives_an_empty_list(self):
        client = MagicMock()
        client.batch_get_traces.return_value = {"Traces": [{}]}
        assert _get_trace_segments(client, TRACE_ID) == []


@pytest.mark.unit
class TestResponseBuilders:
    """_build_trace_analysis_response and the not-found shortcut."""

    def test_the_minimal_response_carries_the_document_and_the_found_flag(self):
        response = _build_trace_analysis_response(document_id="a.pdf", trace_found=True)
        assert response["document_id"] == "a.pdf"
        assert response["trace_found"] is True

    def test_optional_sections_are_omitted_rather_than_set_to_null(self):
        # The agent branches on key presence, so a null section reads as "analysed
        # and found nothing" where absence reads as "not analysed".
        response = _build_trace_analysis_response(document_id="a.pdf", trace_found=True)
        for key in ("trace_id", "detailed_analysis", "service_timeline", "message"):
            assert key not in response

    def test_supplied_sections_are_included(self):
        response = _build_trace_analysis_response(
            document_id="a.pdf",
            trace_found=True,
            trace_id=TRACE_ID,
            detailed_analysis={"total_segments": 1},
            service_timeline=[{"service_name": "OCR"}],
            message="ok",
        )
        assert response["trace_id"] == TRACE_ID
        assert response["detailed_analysis"] == {"total_segments": 1}
        assert response["service_timeline"] == [{"service_name": "OCR"}]
        assert response["message"] == "ok"

    def test_a_not_found_response_reports_zero_traces(self):
        response = _build_trace_analysis_response(
            document_id="a.pdf", trace_found=False
        )
        assert response["traces_found"] == 0

    def test_a_found_response_does_not_report_a_trace_count(self):
        response = _build_trace_analysis_response(document_id="a.pdf", trace_found=True)
        assert "traces_found" not in response

    def test_default_recommendations_are_supplied_when_none_are_given(self):
        # The agent reads this list; an empty one leaves it with nothing to say.
        response = _build_trace_analysis_response(document_id="a.pdf", trace_found=True)
        assert response["recommendations"]

    def test_supplied_recommendations_replace_the_defaults(self):
        response = _build_trace_analysis_response(
            document_id="a.pdf", trace_found=True, recommendations=["do this"]
        )
        assert response["recommendations"] == ["do this"]

    def test_the_not_found_shortcut_explains_the_twenty_four_hour_window(self):
        # The most common cause is a document older than X-Ray's retention, and
        # saying so is the difference between a useful answer and a dead end.
        response = _create_trace_not_found_response("a.pdf")
        assert response["trace_found"] is False
        assert response["traces_found"] == 0
        assert any("24 hours" in r for r in response["recommendations"])


@pytest.mark.unit
class TestExtractTraceSummary:
    """_extract_trace_summary: flattening one trace summary."""

    def test_every_field_is_extracted(self):
        summary = _extract_trace_summary(
            {
                "Id": TRACE_ID,
                "Duration": 1.5,
                "ResponseTime": 1.2,
                "HasError": True,
                "HasFault": False,
                "HasThrottle": True,
                "ServiceIds": [{"Name": "OCR"}, {"Name": "Extract"}],
            }
        )
        assert summary["trace_id"] == TRACE_ID
        assert summary["duration"] == 1.5
        assert summary["response_time"] == 1.2
        assert summary["has_error"] is True
        assert summary["has_fault"] is False
        assert summary["has_throttle"] is True
        assert summary["service_ids"] == ["OCR", "Extract"]

    def test_an_empty_summary_defaults_rather_than_raising(self):
        summary = _extract_trace_summary({})
        assert summary["trace_id"] is None
        assert summary["duration"] == 0
        assert summary["has_error"] is False
        assert summary["service_ids"] == []


@pytest.mark.unit
class TestParseSegmentForLambda:
    """_parse_segment_for_lambda: recursive Lambda discovery."""

    def test_a_lambda_segment_yields_its_name_and_request_id(self):
        result = _parse_segment_for_lambda(
            {
                "origin": "AWS::Lambda",
                "name": "OCRFunction",
                "aws": {"request_id": "req-1"},
            }
        )
        assert result == [{"function_name": "OCRFunction", "request_id": "req-1"}]

    def test_a_resource_arn_overrides_the_segment_name(self):
        result = _parse_segment_for_lambda(
            {
                "origin": "AWS::Lambda",
                "name": "wrong",
                "resource_arn": "arn:aws:lambda:us-east-1:1:function:RealName",
                "aws": {"request_id": "req-1"},
            }
        )
        assert result[0]["function_name"] == "RealName"

    def test_a_non_lambda_segment_yields_nothing(self):
        assert _parse_segment_for_lambda({"origin": "AWS::S3", "name": "bucket"}) == []

    def test_a_lambda_with_no_request_id_is_still_reported(self):
        # The caller filters these out; discovering the function is useful on its own.
        result = _parse_segment_for_lambda({"origin": "AWS::Lambda", "name": "OCR"})
        assert result == [{"function_name": "OCR", "request_id": None}]

    def test_a_lambda_with_no_name_is_reported_as_unknown(self):
        result = _parse_segment_for_lambda(
            {"origin": "AWS::Lambda", "aws": {"request_id": "req-1"}}
        )
        assert result[0]["function_name"] == "Unknown"

    def test_nested_subsegments_are_searched(self):
        result = _parse_segment_for_lambda(
            {
                "origin": "AWS::StepFunctions",
                "subsegments": [
                    {
                        "origin": "AWS::Lambda",
                        "name": "OCR",
                        "aws": {"request_id": "req-1"},
                    }
                ],
            }
        )
        assert result == [{"function_name": "OCR", "request_id": "req-1"}]

    def test_recursion_reaches_arbitrary_depth(self):
        # Step Functions nests a Lambda invocation two or three levels down, so a
        # single-level search would find nothing on a real trace.
        result = _parse_segment_for_lambda(
            {
                "subsegments": [
                    {
                        "subsegments": [
                            {
                                "origin": "AWS::Lambda",
                                "name": "Deep",
                                "aws": {"request_id": "req-deep"},
                            }
                        ]
                    }
                ]
            }
        )
        assert result == [{"function_name": "Deep", "request_id": "req-deep"}]

    def test_several_lambdas_across_branches_are_all_found(self):
        result = _parse_segment_for_lambda(
            {
                "subsegments": [
                    {"origin": "AWS::Lambda", "name": "A", "aws": {"request_id": "1"}},
                    {"origin": "AWS::Lambda", "name": "B", "aws": {"request_id": "2"}},
                ]
            }
        )
        assert {entry["function_name"] for entry in result} == {"A", "B"}


@pytest.mark.unit
class TestExtractLambdaRequestIdsFromTrace:
    """extract_lambda_request_ids: trace id -> {function: request id}."""

    def _run(self, traces):
        with patch(f"{MODULE}.boto3.client") as factory:
            factory.return_value.batch_get_traces.return_value = {"Traces": traces}
            return extract_lambda_request_ids(TRACE_ID)

    def test_a_lambda_segment_is_mapped(self):
        assert self._run(
            [
                {
                    "Segments": [
                        _segment(
                            as_string=True,
                            origin="AWS::Lambda",
                            name="OCR",
                            aws={"request_id": "req-1"},
                        )
                    ]
                }
            ]
        ) == {"OCR": "req-1"}

    def test_no_traces_gives_an_empty_mapping(self):
        assert self._run([]) == {}

    def test_a_lambda_with_no_request_id_is_excluded_from_the_mapping(self):
        # A function name mapped to None would send the agent to CloudWatch with a
        # null filter, matching every concurrent invocation.
        assert (
            self._run(
                [
                    {
                        "Segments": [
                            _segment(as_string=True, origin="AWS::Lambda", name="OCR")
                        ]
                    }
                ]
            )
            == {}
        )

    def test_an_unparseable_segment_is_skipped_and_the_rest_mapped(self):
        assert self._run(
            [
                {
                    "Segments": [
                        {"Document": "not json"},
                        _segment(
                            as_string=True,
                            origin="AWS::Lambda",
                            name="OCR",
                            aws={"request_id": "req-1"},
                        ),
                    ]
                }
            ]
        ) == {"OCR": "req-1"}

    def test_an_api_failure_gives_an_empty_mapping_rather_than_raising(self):
        with patch(f"{MODULE}.boto3.client") as factory:
            factory.return_value.batch_get_traces.side_effect = RuntimeError(
                "Throttled"
            )
            assert extract_lambda_request_ids(TRACE_ID) == {}

    def test_a_client_construction_failure_propagates(self):
        # The client is built OUTSIDE the try block, so unlike an API failure this
        # one is not converted to an empty mapping. Pinned because the two
        # boto3-facing failures behave differently and the difference is invisible
        # at the call site: in a Lambda the region and credentials are always
        # present, so this path is effectively unreachable there, but a caller
        # running the tool locally sees the exception rather than {}.
        with patch(f"{MODULE}.boto3.client", side_effect=RuntimeError("no region")):
            with pytest.raises(RuntimeError, match="no region"):
                extract_lambda_request_ids(TRACE_ID)


@pytest.mark.unit
class TestAnalyzeStackTraces:
    """_analyze_stack_traces: trace summaries for one stack."""

    def _client(self, traces):
        client = MagicMock()
        client.get_trace_summaries.return_value = {"TraceSummaries": traces}
        return client

    def test_the_filter_expression_names_the_stack(self):
        client = self._client([])
        _analyze_stack_traces(
            client, "MyStack", datetime.now(timezone.utc), datetime.now(timezone.utc)
        )
        assert (
            client.get_trace_summaries.call_args.kwargs["FilterExpression"]
            == 'annotation.stack_name = "MyStack"'
        )

    def test_no_traces_reports_zero_with_a_message(self):
        result = _analyze_stack_traces(
            self._client([]),
            "MyStack",
            datetime.now(timezone.utc),
            datetime.now(timezone.utc),
        )
        assert result == {"traces_found": 0, "message": "No traces found for stack"}

    def test_errors_faults_and_throttles_are_counted_separately(self):
        # They mean different things — a fault is server-side, a throttle is
        # capacity — and the recommendations branch on throttles specifically.
        traces = [
            {"HasError": True},
            {"HasFault": True},
            {"HasThrottle": True},
            {"HasError": True, "HasThrottle": True},
        ]
        result = _analyze_stack_traces(
            self._client(traces),
            "MyStack",
            datetime.now(timezone.utc),
            datetime.now(timezone.utc),
        )
        assert result["traces_found"] == 4
        assert result["total_errors"] == 2
        assert result["total_faults"] == 1
        assert result["total_throttles"] == 2

    def test_the_error_rate_is_a_fraction_of_traces_found(self):
        result = _analyze_stack_traces(
            self._client([{"HasError": True}, {}, {}, {}]),
            "MyStack",
            datetime.now(timezone.utc),
            datetime.now(timezone.utc),
        )
        assert result["error_rate"] == 0.25

    def test_services_across_traces_are_deduplicated(self):
        result = _analyze_stack_traces(
            self._client(
                [
                    {"ServiceIds": [{"Name": "OCR"}, {"Name": "Extract"}]},
                    {"ServiceIds": [{"Name": "OCR"}]},
                ]
            ),
            "MyStack",
            datetime.now(timezone.utc),
            datetime.now(timezone.utc),
        )
        assert sorted(result["services_involved"]) == ["Extract", "OCR"]

    def test_an_xray_failure_reports_zero_traces_with_the_error(self):
        client = MagicMock()
        client.get_trace_summaries.side_effect = RuntimeError("Throttled")
        result = _analyze_stack_traces(
            client, "MyStack", datetime.now(timezone.utc), datetime.now(timezone.utc)
        )
        assert result["traces_found"] == 0
        assert "Throttled" in result["error"]


@pytest.mark.unit
class TestAnalyzeServicePerformance:
    """_analyze_service_performance: the service graph and its window clamp."""

    def _client(self, services):
        client = MagicMock()
        client.get_service_graph.return_value = {"Services": services}
        return client

    def _service(self, *, name="OCR", error_rate=0.0, total_time=0.0, requests=100):
        return {
            "Name": name,
            "Type": "AWS::Lambda",
            "SummaryStatistics": {
                "ErrorStatistics": {"ErrorRate": error_rate},
                "ResponseTimeHistogram": {"TotalTime": total_time},
                "RequestCount": requests,
            },
            "Edges": [{}, {}],
        }

    def test_no_services_reports_zero_with_a_message(self):
        with _fixed_thresholds():
            result = _analyze_service_performance(
                self._client([]),
                datetime.now(timezone.utc) - timedelta(hours=1),
                datetime.now(timezone.utc),
            )
        assert result == {
            "services_found": 0,
            "message": "No service map data available",
        }

    def test_each_service_is_summarised(self):
        with _fixed_thresholds():
            result = _analyze_service_performance(
                self._client([self._service(total_time=0.25)]),
                datetime.now(timezone.utc) - timedelta(hours=1),
                datetime.now(timezone.utc),
            )
        analysis = result["service_analysis"][0]
        assert analysis["name"] == "OCR"
        assert analysis["request_count"] == 100
        assert analysis["response_time_ms"] == pytest.approx(250.0)
        assert analysis["edges"] == 2

    def test_a_service_over_the_error_rate_threshold_is_flagged(self):
        # 0.02, not the code's own 0.05 default: a test that overrides a threshold to
        # the value already hardcoded cannot distinguish "reads the config" from
        # "ignores it".
        with _fixed_thresholds(xray_error_rate_threshold=0.02):
            result = _analyze_service_performance(
                self._client([self._service(error_rate=0.03)]),
                datetime.now(timezone.utc) - timedelta(hours=1),
                datetime.now(timezone.utc),
            )
        assert result["high_error_services"] == ["OCR"]

    def test_a_service_under_the_error_rate_threshold_is_not_flagged(self):
        with _fixed_thresholds(xray_error_rate_threshold=0.02):
            result = _analyze_service_performance(
                self._client([self._service(error_rate=0.01)]),
                datetime.now(timezone.utc) - timedelta(hours=1),
                datetime.now(timezone.utc),
            )
        assert result["high_error_services"] == []

    def test_a_slow_service_is_flagged_and_the_threshold_is_in_milliseconds(self):
        # TotalTime is seconds and the threshold is ms; a service at 12s must be
        # flagged against a 10,000 ms threshold, not compared as 12 vs 10000.
        with _fixed_thresholds(xray_response_time_threshold_ms=4000):
            result = _analyze_service_performance(
                self._client([self._service(total_time=5.0)]),
                datetime.now(timezone.utc) - timedelta(hours=1),
                datetime.now(timezone.utc),
            )
        assert result["slow_services"] == ["OCR"]

    def test_a_service_exactly_at_the_error_rate_threshold_is_not_flagged(self):
        # Strictly greater than. Pinned because flipping it to >= would flag every
        # service whose error rate happens to equal a round configured value.
        with _fixed_thresholds(xray_error_rate_threshold=0.02):
            result = _analyze_service_performance(
                self._client([self._service(error_rate=0.02)]),
                datetime.now(timezone.utc) - timedelta(hours=1),
                datetime.now(timezone.utc),
            )
        assert result["high_error_services"] == []

    def test_a_service_exactly_at_the_response_time_threshold_is_not_slow(self):
        with _fixed_thresholds(xray_response_time_threshold_ms=4000):
            result = _analyze_service_performance(
                self._client([self._service(total_time=4.0)]),
                datetime.now(timezone.utc) - timedelta(hours=1),
                datetime.now(timezone.utc),
            )
        assert result["slow_services"] == []

    def test_a_fast_service_is_not_flagged_as_slow(self):
        with _fixed_thresholds(xray_response_time_threshold_ms=4000):
            result = _analyze_service_performance(
                self._client([self._service(total_time=1.0)]),
                datetime.now(timezone.utc) - timedelta(hours=1),
                datetime.now(timezone.utc),
            )
        assert result["slow_services"] == []

    def test_a_window_longer_than_the_configured_hours_is_clamped(self):
        # X-Ray rejects a service-graph window past its own limit, so an over-long
        # request would fail rather than return less data.
        client = self._client([self._service()])
        end = datetime.now(timezone.utc)
        with _fixed_thresholds(xray_analysis_hours=3):
            _analyze_service_performance(client, end - timedelta(hours=24), end)
        kwargs = client.get_service_graph.call_args.kwargs
        assert kwargs["EndTime"] - kwargs["StartTime"] == timedelta(hours=3)

    def test_a_window_within_the_configured_hours_is_left_alone(self):
        client = self._client([self._service()])
        end = datetime.now(timezone.utc)
        with _fixed_thresholds(xray_analysis_hours=3):
            _analyze_service_performance(client, end - timedelta(hours=1), end)
        kwargs = client.get_service_graph.call_args.kwargs
        assert kwargs["EndTime"] - kwargs["StartTime"] == timedelta(hours=1)

    def test_the_configured_hours_are_capped_at_the_api_limit_of_six(self):
        # The cap is a hard X-Ray constraint, so a config asking for more must not
        # be honoured — the request would be rejected outright.
        client = self._client([self._service()])
        end = datetime.now(timezone.utc)
        with _fixed_thresholds(xray_analysis_hours=48):
            _analyze_service_performance(client, end - timedelta(hours=24), end)
        kwargs = client.get_service_graph.call_args.kwargs
        assert kwargs["EndTime"] - kwargs["StartTime"] == timedelta(hours=6)

    def test_an_xray_failure_reports_zero_services_with_the_error(self):
        client = MagicMock()
        client.get_service_graph.side_effect = RuntimeError("Throttled")
        with _fixed_thresholds():
            result = _analyze_service_performance(
                client,
                datetime.now(timezone.utc) - timedelta(hours=1),
                datetime.now(timezone.utc),
            )
        assert result["services_found"] == 0
        assert "Throttled" in result["error"]


@pytest.mark.unit
class TestGenerateRecommendations:
    """_generate_recommendations: what the agent is told to do next."""

    def test_stack_errors_produce_an_error_recommendation(self):
        recommendations = _generate_recommendations(
            {"traces_found": 5, "total_errors": 2}, None
        )
        assert any("error traces" in r for r in recommendations)

    def test_stack_throttles_produce_a_capacity_recommendation(self):
        recommendations = _generate_recommendations(
            {"traces_found": 5, "total_throttles": 3}, None
        )
        assert any("throttling" in r for r in recommendations)

    def test_a_clean_stack_analysis_produces_no_stack_recommendation(self):
        recommendations = _generate_recommendations(
            {"traces_found": 5, "total_errors": 0, "total_throttles": 0}, None
        )
        assert not any("stack" in r.lower() for r in recommendations)

    def test_a_stack_analysis_with_no_traces_is_ignored(self):
        recommendations = _generate_recommendations({"traces_found": 0}, None)
        assert any("X-Ray tracing is enabled" in r for r in recommendations)

    def test_high_error_services_are_named(self):
        recommendations = _generate_recommendations(
            None, {"services_found": 2, "high_error_services": ["OCR", "Extract"]}
        )
        assert any("OCR" in r and "Extract" in r for r in recommendations)

    def test_at_most_three_services_are_named_to_keep_the_text_usable(self):
        recommendations = _generate_recommendations(
            None,
            {"services_found": 5, "slow_services": ["A", "B", "C", "D", "E"]},
        )
        slow = next(r for r in recommendations if "Optimize slow" in r)
        assert "D" not in slow and "E" not in slow

    def test_nothing_found_falls_back_to_configuration_advice(self):
        # With no findings the useful answer is that tracing may not be on, rather
        # than an empty list the agent reports as "no issues".
        recommendations = _generate_recommendations(None, None)
        assert recommendations
        assert any("X-Ray tracing is enabled" in r for r in recommendations)

    def test_both_analyses_contribute_their_own_recommendations(self):
        recommendations = _generate_recommendations(
            {"traces_found": 1, "total_errors": 1},
            {"services_found": 1, "slow_services": ["OCR"]},
        )
        assert any("error traces" in r for r in recommendations)
        assert any("Optimize slow" in r for r in recommendations)


@pytest.mark.unit
class TestAnalyzeDocumentTrace:
    """analyze_document_trace: the tool as the agent calls it."""

    def test_an_empty_document_id_is_rejected_before_any_aws_call(self):
        with patch(f"{MODULE}.boto3.client") as factory:
            result = analyze_document_trace("")
        assert result["success"] is False
        factory.assert_not_called()

    def test_no_trace_found_reports_not_found_with_guidance(self):
        with (
            patch(f"{MODULE}.boto3.client"),
            patch(f"{MODULE}._find_trace_id_for_document", return_value=None),
        ):
            result = analyze_document_trace("report.pdf")
        assert result["trace_found"] is False
        assert result["traces_found"] == 0
        assert result["recommendations"]

    def test_a_trace_with_no_segments_is_an_error_not_an_empty_analysis(self):
        # Finding a trace id and then being unable to read it is a different
        # condition from there being no trace, and the agent should not report a
        # clean analysis of nothing.
        with (
            patch(f"{MODULE}.boto3.client"),
            patch(f"{MODULE}._find_trace_id_for_document", return_value=TRACE_ID),
            patch(f"{MODULE}._get_trace_segments", return_value=[]),
        ):
            result = analyze_document_trace("report.pdf")
        assert result["success"] is False
        assert TRACE_ID in result["error"]

    def test_a_full_analysis_carries_the_trace_id_analysis_and_timeline(self):
        with (
            patch(f"{MODULE}.boto3.client"),
            patch(f"{MODULE}._find_trace_id_for_document", return_value=TRACE_ID),
            patch(
                f"{MODULE}._get_trace_segments",
                return_value=[_segment(start=0.0, end=1.0)],
            ),
            _fixed_thresholds(),
        ):
            result = analyze_document_trace("report.pdf")
        assert result["trace_found"] is True
        assert result["trace_id"] == TRACE_ID
        assert result["detailed_analysis"]["total_segments"] == 1
        assert result["service_timeline"][0]["service_name"] == "OCRFunction"

    def test_an_unexpected_failure_becomes_an_error_response(self):
        with patch(f"{MODULE}.boto3.client", side_effect=RuntimeError("AccessDenied")):
            result = analyze_document_trace("report.pdf")
        assert result["success"] is False
        assert "AccessDenied" in result["error"]


@pytest.mark.unit
class TestAnalyzeSystemPerformance:
    """analyze_system_performance: stack-focused, with an infrastructure fallback."""

    def test_a_stack_with_traces_gives_a_stack_focused_analysis(self):
        with (
            patch(f"{MODULE}.boto3.client"),
            patch(f"{MODULE}._analyze_stack_traces", return_value={"traces_found": 3}),
            patch(
                f"{MODULE}._analyze_service_performance",
                return_value={"services_found": 5},
            ),
        ):
            result = analyze_system_performance(stack_name="MyStack")
        assert result["analysis_type"] == "stack_focused"
        assert result["stack_name"] == "MyStack"
        assert result["traces_found"] == 3
        assert result["services_found"] == 5

    def test_a_stack_with_no_traces_falls_back_to_the_infrastructure_view(self):
        # Reporting "0 traces for your stack" and stopping would leave the agent
        # with nothing, when the service map may still explain the problem.
        with (
            patch(f"{MODULE}.boto3.client"),
            patch(f"{MODULE}._analyze_stack_traces", return_value={"traces_found": 0}),
            patch(
                f"{MODULE}._analyze_service_performance",
                return_value={"services_found": 5},
            ),
        ):
            result = analyze_system_performance(stack_name="MyStack")
        assert result["analysis_type"] == "infrastructure_wide"
        assert result["stack_name"] == "MyStack"

    def test_no_stack_name_skips_the_stack_query_entirely(self):
        with (
            patch(f"{MODULE}.boto3.client"),
            patch(f"{MODULE}._analyze_stack_traces") as stack,
            patch(
                f"{MODULE}._analyze_service_performance",
                return_value={"services_found": 2},
            ),
        ):
            result = analyze_system_performance()
        stack.assert_not_called()
        assert result["analysis_type"] == "infrastructure_wide"
        assert result["stack_name"] == "not_provided"

    def test_the_hours_back_argument_sets_the_window(self):
        with (
            patch(f"{MODULE}.boto3.client"),
            patch(
                f"{MODULE}._analyze_service_performance",
                return_value={"services_found": 1},
            ) as service,
        ):
            analyze_system_performance(hours_back=4)
        start, end = service.call_args.args[1], service.call_args.args[2]
        assert end - start == timedelta(hours=4)

    def test_the_default_window_is_one_hour(self):
        with (
            patch(f"{MODULE}.boto3.client"),
            patch(
                f"{MODULE}._analyze_service_performance",
                return_value={"services_found": 1},
            ) as service,
        ):
            analyze_system_performance()
        start, end = service.call_args.args[1], service.call_args.args[2]
        assert end - start == timedelta(hours=1)

    def test_recommendations_are_always_present(self):
        with (
            patch(f"{MODULE}.boto3.client"),
            patch(
                f"{MODULE}._analyze_service_performance",
                return_value={"services_found": 0},
            ),
        ):
            result = analyze_system_performance()
        assert result["recommendations"]

    def test_an_unexpected_failure_becomes_an_error_response(self):
        with patch(f"{MODULE}.boto3.client", side_effect=RuntimeError("AccessDenied")):
            result = analyze_system_performance(stack_name="MyStack")
        assert result["success"] is False
        assert "AccessDenied" in result["error"]
