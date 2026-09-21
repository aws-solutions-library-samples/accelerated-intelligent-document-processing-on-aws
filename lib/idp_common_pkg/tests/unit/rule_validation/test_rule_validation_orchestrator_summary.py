# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the rule-validation orchestrator's consolidation and reporting
layer: _generate_consolidated_summary, _format_summary_as_markdown, the three S3
persistence helpers, and the lazy semaphore.

These are the steps between "the model answered" and "the operator reads a
verdict", so a defect here changes a published compliance report without
changing any model call. They are also the deterministic part of the
orchestrator: no Bedrock, and S3 is stubbed at the module boundary
(idp_common.rule_validation.orchestrator.s3), which is the only AWS surface these
methods touch.

The markdown formatter is asserted on content rather than on an exact string.
Pinning the whole document would make every CSS tweak a test failure; what
matters is that each rule reaches the table, that the counts shown agree with the
counts computed, and that the two cells the formatter escapes -- `rule` and
`reasoning` -- are escaped, in the right order.

Note the scope of that last claim. `recommendation` and `supporting_pages` are
interpolated into their `<td>` cells **raw**, and both also originate in model
output, so "document text cannot inject HTML" is not a property this formatter has.
The web UI pairs `rehypeRaw` with `rehypeSanitize`, which is what stands between
those two cells and the browser; these tests assert what the formatter does, not
that the rendered page is safe.
"""

from unittest.mock import MagicMock, patch

import pytest

from idp_common.rule_validation.orchestrator import RuleValidationOrchestratorService


def _service(config=None) -> RuleValidationOrchestratorService:
    return RuleValidationOrchestratorService(config if config is not None else {})


def _response(rule, recommendation, pages=None, reasoning="because"):
    return {
        "rule": rule,
        "recommendation": recommendation,
        "supporting_pages": pages if pages is not None else ["1"],
        "reasoning": reasoning,
    }


@pytest.mark.unit
class TestOrchestratorConstruction:
    """__init__ and the lazily-bound semaphore."""

    def test_a_plain_dict_config_is_accepted(self):
        service = _service({})
        assert service.token_metrics == {}
        assert service.semaphore_limit >= 1

    def test_no_config_falls_back_to_declared_defaults(self):
        assert RuleValidationOrchestratorService().semaphore_limit >= 1

    def test_an_already_built_config_model_is_used_as_is(self):
        from idp_common.config.models import IDPConfig

        model = IDPConfig()
        assert _service(model).config is model

    def test_the_configured_semaphore_limit_is_honoured(self):
        service = _service({"rule_validation": {"semaphore": 3}})
        assert service.semaphore_limit == 3

    def test_the_semaphore_is_not_created_until_it_is_asked_for(self):
        # Creating an asyncio.Semaphore at __init__ time binds it to whatever loop
        # exists then, which is why this is lazy.
        assert _service()._semaphore is None

    def test_the_semaphore_is_usable_as_a_context_manager(self):
        import asyncio

        async def probe():
            service = _service({"rule_validation": {"semaphore": 2}})
            async with service.semaphore:
                return True

        assert asyncio.run(probe()) is True

    def test_a_semaphore_bound_to_a_dead_loop_is_replaced(self):
        # A notebook rerun leaves a semaphore attached to a closed loop; reusing it
        # would raise on the first acquire.
        #
        # The first loop CONTENDS the semaphore deliberately. asyncio assigns
        # Semaphore._loop only when an acquire has to wait -- a non-blocking
        # `async with` leaves it None -- so without contention there is no loop
        # binding for the guard to detect, and the test would pass because the
        # property discards its cache on every access (issue #1053) rather than
        # because a stale binding was found. Contending it is what makes the
        # assertion about the behaviour its name claims.
        import asyncio

        service = _service({"rule_validation": {"semaphore": 1}})

        async def first_loop():
            semaphore = service.semaphore

            async def hold():
                async with semaphore:
                    await asyncio.sleep(0)

            await asyncio.gather(hold(), hold())
            assert semaphore._loop is asyncio.get_running_loop(), (
                "the semaphore was never actually bound to this loop, so this test "
                "cannot be observing a stale binding"
            )
            return semaphore

        first = asyncio.run(first_loop())

        async def second_loop():
            return service.semaphore

        second = asyncio.run(second_loop())
        assert second is not first

    @pytest.mark.xfail(
        strict=True,
        reason="The stale-loop guard fires on every access, because a Semaphore's "
        "_loop is None until first awaited, so the cache is cleared and the "
        "configured concurrency limit never applies. See "
        "https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1053",
    )
    def test_repeated_access_within_one_loop_returns_the_same_semaphore(self):
        # Both call sites write `async with self.semaphore:`, so the property is
        # re-evaluated per task. If it hands back a fresh Semaphore each time,
        # every task acquires its own and nothing is rate limited.
        import asyncio

        async def probe():
            service = _service({"rule_validation": {"semaphore": 1}})
            return service.semaphore is service.semaphore

        assert asyncio.run(probe()) is True


@pytest.mark.unit
class TestGenerateConsolidatedSummary:
    """_generate_consolidated_summary: responses -> statistics."""

    def test_counts_and_percentages_for_a_single_policy_type(self):
        summary = _service()._generate_consolidated_summary(
            {
                "Lending": [
                    _response("r1", "Pass"),
                    _response("r2", "Pass"),
                    _response("r3", "Fail"),
                    _response("r4", "Information Not Found"),
                ]
            }
        )
        overall = summary["overall_statistics"]
        assert overall["total_rules"] == 4
        assert overall["pass_count"] == 2
        assert overall["fail_count"] == 1
        assert overall["information_not_found_count"] == 1
        assert overall["pass_percentage"] == 50.0
        assert summary["overall_status"] == "COMPLETE"
        assert summary["total_policy_types"] == 1

    def test_totals_are_summed_across_policy_types(self):
        summary = _service()._generate_consolidated_summary(
            {
                "Lending": [_response("r1", "Pass")],
                "Insurance": [_response("r2", "Fail"), _response("r3", "Pass")],
            }
        )
        assert summary["overall_statistics"]["total_rules"] == 3
        assert summary["overall_statistics"]["pass_count"] == 2
        assert set(summary["rule_details"]) == {"Lending", "Insurance"}

    def test_per_policy_type_statistics_are_kept_separate(self):
        summary = _service()._generate_consolidated_summary(
            {
                "Lending": [_response("r1", "Pass"), _response("r2", "Fail")],
                "Insurance": [_response("r3", "Pass")],
            }
        )
        assert summary["rule_details"]["Lending"]["pass_percentage"] == 50.0
        assert summary["rule_details"]["Insurance"]["pass_percentage"] == 100.0

    def test_a_dict_of_per_rule_responses_is_flattened(self):
        # A multi-section document arrives as {rule_key: [responses]} rather than
        # a flat list; both shapes must produce the same counts.
        as_dict = _service()._generate_consolidated_summary(
            {
                "Lending": {
                    "rule_a": [_response("r1", "Pass")],
                    "rule_b": [_response("r2", "Fail")],
                }
            }
        )
        as_list = _service()._generate_consolidated_summary(
            {"Lending": [_response("r1", "Pass"), _response("r2", "Fail")]}
        )
        assert as_dict["overall_statistics"]["total_rules"] == 2
        assert (
            as_dict["overall_statistics"]["recommendation_counts"]
            == as_list["overall_statistics"]["recommendation_counts"]
        )

    def test_a_bare_response_object_inside_the_dict_form_is_accepted(self):
        summary = _service()._generate_consolidated_summary(
            {"Lending": {"rule_a": _response("r1", "Pass")}}
        )
        assert summary["overall_statistics"]["total_rules"] == 1

    def test_no_responses_at_all_gives_zero_rather_than_a_division_error(self):
        summary = _service()._generate_consolidated_summary({})
        assert summary["overall_statistics"]["total_rules"] == 0
        assert summary["overall_statistics"]["pass_percentage"] == 0.0
        assert summary["rule_details"] == {}

    def test_a_policy_type_with_an_empty_response_list_gives_zero_percent(self):
        summary = _service()._generate_consolidated_summary({"Lending": []})
        assert summary["rule_details"]["Lending"]["pass_percentage"] == 0.0
        assert summary["rule_details"]["Lending"]["total_rules"] == 0

    def test_an_unrecognised_recommendation_is_counted_under_its_own_name(self):
        # The recommendation vocabulary is user-configurable, so the counter is
        # built dynamically rather than from a fixed set.
        summary = _service()._generate_consolidated_summary(
            {"Lending": [_response("r1", "Needs Review")]}
        )
        assert (
            summary["overall_statistics"]["recommendation_counts"]["Needs Review"] == 1
        )
        assert summary["overall_statistics"]["pass_count"] == 0

    def test_a_response_missing_every_field_is_defaulted_not_dropped(self):
        summary = _service()._generate_consolidated_summary({"Lending": [{}]})
        rule = summary["rule_details"]["Lending"]["rules"][0]
        assert rule["recommendation"] == "Unknown"
        assert rule["rule"] == "Unknown rule"
        assert rule["reasoning"] == "No reasoning provided"
        assert summary["overall_statistics"]["total_rules"] == 1

    def test_supporting_pages_are_deduplicated_and_sorted_numerically(self):
        summary = _service()._generate_consolidated_summary(
            {
                "Lending": [
                    _response("r1", "Pass", pages=["10", "2"]),
                    _response("r2", "Pass", pages=["2", "1"]),
                ]
            }
        )
        assert summary["supporting_pages"] == ["1", "2", "10"]

    def test_pass_percentage_is_rounded_to_two_decimals(self):
        summary = _service()._generate_consolidated_summary(
            {
                "Lending": [
                    _response(f"r{i}", "Pass" if i == 0 else "Fail") for i in range(3)
                ]
            }
        )
        assert summary["overall_statistics"]["pass_percentage"] == 33.33

    def test_rule_summary_carries_the_dynamic_counts_per_policy_type(self):
        summary = _service()._generate_consolidated_summary(
            {"Lending": [_response("r1", "Pass"), _response("r2", "Fail")]}
        )
        entry = summary["rule_summary"]["Lending"]
        assert entry["status"] == "COMPLETE"
        assert entry["total_rules"] == 2
        assert entry["Pass"] == 1
        assert entry["Fail"] == 1

    def test_a_generation_timestamp_is_always_present(self):
        assert _service()._generate_consolidated_summary({})["generated_at"]

    def test_an_unexpected_shape_degrades_to_an_error_summary_rather_than_raising(self):
        # The caller writes whatever comes back to S3, so this method never
        # raises. It reports overall_status ERROR instead.
        summary = _service()._generate_consolidated_summary({"Lending": "not-a-list"})
        assert summary["overall_status"] == "ERROR"
        assert "error" in summary
        assert summary["generated_at"]

    @pytest.mark.xfail(
        strict=True,
        reason="Integer supporting_pages collapse the whole summary to ERROR: the "
        "sort key calls x.isdigit(), which int does not have. See "
        "https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1052",
    )
    def test_integer_supporting_pages_do_not_discard_the_summary(self):
        # An LLM returning "supporting_pages": [1, 2] rather than ["1", "2"] is
        # enough to lose every statistic in the report, because the AttributeError
        # is caught by the method's own catch-all.
        summary = _service()._generate_consolidated_summary(
            {"Lending": [_response("r1", "Pass", pages=[1, 2])]}
        )
        assert summary["overall_status"] == "COMPLETE"
        assert summary["overall_statistics"]["pass_count"] == 1


def _summary_for_markdown(**overrides):
    summary = {
        "document_id": "lending_package.pdf",
        "overall_statistics": {
            "total_rules": 3,
            "pass_count": 1,
            "fail_count": 1,
            "information_not_found_count": 1,
            "pass_percentage": 33.33,
        },
        "rule_details": {
            "lending_policy": {
                "total_rules": 3,
                "pass_count": 1,
                "fail_count": 1,
                "information_not_found_count": 1,
                "pass_percentage": 33.33,
                "rules": [
                    _response("Income documented", "Pass", ["1"], "Found on page 1"),
                    _response("LTV under 80%", "Fail", ["2", "3"], "LTV is 92%"),
                    _response(
                        "Appraisal present", "Information Not Found", [], "Absent"
                    ),
                ],
            }
        },
        "generated_at": "2026-01-01T00:00:00",
    }
    summary.update(overrides)
    return summary


@pytest.mark.unit
class TestFormatSummaryAsMarkdown:
    """_format_summary_as_markdown: the document the web UI renders."""

    def test_the_document_id_becomes_the_title(self):
        markdown = _service()._format_summary_as_markdown(_summary_for_markdown())
        assert "# Rule Validation Summary: lending_package.pdf" in markdown

    def test_a_missing_document_id_falls_back_to_a_generic_title(self):
        markdown = _service()._format_summary_as_markdown(
            _summary_for_markdown(document_id=None)
        )
        assert "# Rule Validation Summary:" in markdown

    def test_the_overall_counts_are_shown(self):
        markdown = _service()._format_summary_as_markdown(_summary_for_markdown())
        assert "## Overall Statistics" in markdown
        assert "33.33%" in markdown

    def test_pass_and_fail_counts_are_colour_coded_and_info_is_not(self):
        markdown = _service()._format_summary_as_markdown(_summary_for_markdown())
        assert "#16ab39" in markdown  # green, pass
        assert "#d13212" in markdown  # red, fail

    def test_a_table_of_contents_links_each_policy_type(self):
        markdown = _service()._format_summary_as_markdown(
            _summary_for_markdown(
                rule_details={
                    "lending_policy": {"rules": []},
                    "insurance_policy": {"rules": []},
                }
            )
        )
        assert "## Table of Contents" in markdown
        assert "1. [Lending Policy](#lending-policy)" in markdown
        assert "2. [Insurance Policy](#insurance-policy)" in markdown

    def test_no_table_of_contents_when_there_are_no_policy_types(self):
        markdown = _service()._format_summary_as_markdown(
            _summary_for_markdown(rule_details={})
        )
        assert "## Table of Contents" not in markdown

    def test_each_policy_type_gets_an_anchored_heading(self):
        markdown = _service()._format_summary_as_markdown(_summary_for_markdown())
        assert '## 1. Lending Policy <a id="lending-policy"></a>' in markdown

    def test_every_rule_reaches_the_table(self):
        markdown = _service()._format_summary_as_markdown(_summary_for_markdown())
        for rule in ("Income documented", "LTV under 80%", "Appraisal present"):
            assert rule in markdown

    def test_each_recommendation_gets_its_own_status_icon(self):
        markdown = _service()._format_summary_as_markdown(_summary_for_markdown())
        assert "✅ Pass" in markdown
        assert "❌ Fail" in markdown
        assert "ℹ️ Information Not Found" in markdown

    def test_supporting_pages_are_joined_and_an_empty_list_reads_n_a(self):
        markdown = _service()._format_summary_as_markdown(_summary_for_markdown())
        assert "2, 3" in markdown
        assert "N/A" in markdown

    def test_no_rules_table_is_emitted_for_a_policy_type_with_no_rules(self):
        markdown = _service()._format_summary_as_markdown(
            _summary_for_markdown(rule_details={"lending_policy": {"rules": []}})
        )
        assert "### Rules" not in markdown

    def test_html_in_a_rule_name_is_escaped(self):
        # Rule text and reasoning both originate in document content and model
        # output, and this markdown is rendered in the browser.
        markdown = _service()._format_summary_as_markdown(
            _summary_for_markdown(
                rule_details={
                    "p": {
                        "rules": [
                            _response("<script>alert(1)</script>", "Pass", ["1"], "ok")
                        ]
                    }
                }
            )
        )
        assert "<script>alert(1)</script>" not in markdown
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in markdown

    def test_html_in_the_reasoning_is_escaped(self):
        markdown = _service()._format_summary_as_markdown(
            _summary_for_markdown(
                rule_details={
                    "p": {
                        "rules": [
                            _response("r", "Pass", ["1"], '<img src=x onerror="y">')
                        ]
                    }
                }
            )
        )
        assert "<img src=x" not in markdown
        assert "&lt;img src=x onerror=&quot;y&quot;&gt;" in markdown

    def test_an_ampersand_is_escaped_before_the_angle_brackets_are(self):
        # Escaping & after < would produce "&amp;lt;" and show the entity as text.
        #
        # The fixture must contain BOTH an ampersand and an angle bracket. With only
        # an ampersand there is no "&lt;" for a late '&' replacement to turn into
        # "&amp;lt;", so the reordered code passes and this test says nothing about
        # order -- the reorder would then be caught only by
        # test_html_in_a_rule_name_is_escaped, which is not what this test is for.
        markdown = _service()._format_summary_as_markdown(
            _summary_for_markdown(
                rule_details={
                    "p": {"rules": [_response("a & <b>", "Pass", ["1"], "x & <y>")]}
                }
            )
        )
        assert "a &amp; &lt;b&gt;" in markdown
        assert "x &amp; &lt;y&gt;" in markdown
        assert "&amp;lt;" not in markdown
        assert "&amp;amp;" not in markdown

    def test_newlines_in_reasoning_are_flattened_so_the_table_row_survives(self):
        markdown = _service()._format_summary_as_markdown(
            _summary_for_markdown(
                rule_details={
                    "p": {
                        "rules": [_response("r", "Pass", ["1"], "line one\nline two")]
                    }
                }
            )
        )
        assert "line one line two" in markdown

    def test_a_separator_appears_between_sections_but_not_after_the_last(self):
        # Absolute counts, not a relative comparison. Emitting a separator after the
        # last section too would still leave `three > one`, so the "not after the
        # last" half of the name would be untested. The footer contributes one
        # occurrence of its own, so n sections give n separators rather than n - 1.
        three = _service()._format_summary_as_markdown(
            _summary_for_markdown(
                rule_details={
                    "a": {"rules": []},
                    "b": {"rules": []},
                    "c": {"rules": []},
                }
            )
        )
        one = _service()._format_summary_as_markdown(
            _summary_for_markdown(rule_details={"a": {"rules": []}})
        )
        assert one.count("\n---\n\n") == 1  # the footer only
        assert three.count("\n---\n\n") == 3  # two between sections, plus the footer

    def test_the_document_does_not_end_with_a_section_separator(self):
        # Asserted with the footer suppressed, so the only thing that could put a
        # separator at the end is the per-section loop.
        markdown = _service()._format_summary_as_markdown(
            _summary_for_markdown(
                rule_details={"a": {"rules": []}, "b": {"rules": []}},
                generated_at="",
            )
        )
        assert not markdown.rstrip().endswith("---")

    def test_the_generation_timestamp_is_in_the_footer_when_present(self):
        markdown = _service()._format_summary_as_markdown(_summary_for_markdown())
        assert "Report generated at: 2026-01-01T00:00:00" in markdown

    def test_no_footer_is_emitted_when_there_is_no_timestamp(self):
        markdown = _service()._format_summary_as_markdown(
            _summary_for_markdown(generated_at="")
        )
        assert "Report generated at" not in markdown

    def test_a_summary_with_nothing_in_it_still_produces_a_document(self):
        markdown = _service()._format_summary_as_markdown({})
        assert markdown.startswith("<style>")
        assert "# Rule Validation Summary: Document" in markdown

    def test_the_generated_summary_feeds_the_formatter_without_adaptation(self):
        # The two methods are used back to back in save_consolidated_summary, so
        # the shape one produces must be the shape the other reads.
        service = _service()
        summary = service._generate_consolidated_summary(
            {"lending_policy": [_response("Income documented", "Pass", ["1"])]}
        )
        markdown = service._format_summary_as_markdown(summary)
        assert "Income documented" in markdown
        assert "✅ Pass" in markdown
        assert "100.0%" in markdown


@pytest.mark.unit
class TestSectionResultPersistence:
    """load_section_results / save_policy_type_responses / save_consolidated_summary."""

    def test_section_files_are_merged_into_one_response_map(self):
        with patch("idp_common.rule_validation.orchestrator.s3") as s3_mock:
            s3_mock.find_matching_files.return_value = [
                "doc/rule_validation/sections/section_1_responses.json",
                "doc/rule_validation/sections/section_2_responses.json",
            ]
            s3_mock.get_json_content.side_effect = [
                {"responses": {"Lending": [_response("r1", "Pass")]}},
                {"responses": {"Lending": [_response("r2", "Fail")]}},
            ]
            responses, chunked = _service().load_section_results("doc", "bucket")
        assert len(responses["Lending"]) == 2
        assert chunked is False

    def test_responses_for_different_policy_types_stay_separate(self):
        with patch("idp_common.rule_validation.orchestrator.s3") as s3_mock:
            s3_mock.find_matching_files.return_value = [
                "doc/rule_validation/sections/section_1_responses.json"
            ]
            s3_mock.get_json_content.return_value = {
                "responses": {
                    "Lending": [_response("r1", "Pass")],
                    "Insurance": [_response("r2", "Fail")],
                }
            }
            responses, _ = _service().load_section_results("doc", "bucket")
        assert set(responses) == {"Lending", "Insurance"}

    def test_a_single_response_object_is_wrapped_into_a_list(self):
        with patch("idp_common.rule_validation.orchestrator.s3") as s3_mock:
            s3_mock.find_matching_files.return_value = [
                "doc/rule_validation/sections/section_1_responses.json"
            ]
            s3_mock.get_json_content.return_value = {
                "responses": {"Lending": _response("r1", "Pass")}
            }
            responses, _ = _service().load_section_results("doc", "bucket")
        assert isinstance(responses["Lending"], list)

    def test_chunking_in_any_one_section_sets_the_flag_for_the_document(self):
        with patch("idp_common.rule_validation.orchestrator.s3") as s3_mock:
            s3_mock.find_matching_files.return_value = [
                "doc/rule_validation/sections/section_1_responses.json",
                "doc/rule_validation/sections/section_2_responses.json",
            ]
            s3_mock.get_json_content.side_effect = [
                {"responses": {}, "chunking_occurred": False},
                {"responses": {}, "chunking_occurred": True},
            ]
            _, chunked = _service().load_section_results("doc", "bucket")
        assert chunked is True

    def test_a_file_that_is_not_a_section_result_is_skipped(self):
        with patch("idp_common.rule_validation.orchestrator.s3") as s3_mock:
            s3_mock.find_matching_files.return_value = [
                "doc/rule_validation/sections/manifest.json"
            ]
            responses, _ = _service().load_section_results("doc", "bucket")
        assert responses == {}
        s3_mock.get_json_content.assert_not_called()

    def test_an_s3_failure_yields_an_empty_result_rather_than_raising(self):
        # The caller continues to the summary step, which must report zero rules
        # rather than crashing the workflow.
        with patch("idp_common.rule_validation.orchestrator.s3") as s3_mock:
            s3_mock.find_matching_files.side_effect = RuntimeError("AccessDenied")
            assert _service().load_section_results("doc", "bucket") == ({}, False)

    def test_one_object_is_written_per_policy_type(self):
        with patch("idp_common.rule_validation.orchestrator.s3") as s3_mock:
            uris = _service().save_policy_type_responses(
                {
                    "Lending": [_response("r1", "Pass")],
                    "Insurance": [_response("r2", "Fail")],
                },
                "doc",
                "bucket",
            )
        assert len(uris) == 2
        assert s3_mock.write_content.call_count == 2
        assert all(
            u.startswith("s3://bucket/doc/rule_validation/consolidated/") for u in uris
        )

    def test_metadata_keys_are_not_written_as_policy_types(self):
        with patch("idp_common.rule_validation.orchestrator.s3") as s3_mock:
            uris = _service().save_policy_type_responses(
                {
                    "Lending": [_response("r1", "Pass")],
                    "section_id": "1",
                    "chunking_occurred": True,
                    "chunks_created": 2,
                    "responses": {},
                },
                "doc",
                "bucket",
            )
        assert len(uris) == 1
        assert s3_mock.write_content.call_count == 1

    def test_internal_underscore_keys_are_stripped_before_persisting(self):
        # Keys beginning with "_" are in-flight bookkeeping, not part of the
        # published result.
        with patch("idp_common.rule_validation.orchestrator.s3") as s3_mock:
            response = _response("r1", "Pass")
            response["_internal_chunk"] = "3"
            _service().save_policy_type_responses(
                {"Lending": [response]}, "doc", "bucket"
            )
        written = s3_mock.write_content.call_args[0][0]
        assert "_internal_chunk" not in written[0]
        assert written[0]["rule"] == "r1"

    def test_both_a_json_and_a_markdown_object_are_written_and_markdown_is_returned(
        self,
    ):
        with patch("idp_common.rule_validation.orchestrator.s3") as s3_mock:
            uri = _service().save_consolidated_summary(
                _summary_for_markdown(), "doc", "bucket"
            )
        assert uri.endswith("consolidated_summary.md")
        assert s3_mock.write_content.call_count == 2
        content_types = [
            c.kwargs["content_type"] for c in s3_mock.write_content.call_args_list
        ]
        assert content_types == ["application/json", "text/markdown"]

    def test_the_markdown_written_is_the_formatted_summary(self):
        with patch("idp_common.rule_validation.orchestrator.s3") as s3_mock:
            _service().save_consolidated_summary(
                _summary_for_markdown(), "doc", "bucket"
            )
        markdown = s3_mock.write_content.call_args_list[1][0][0]
        assert "# Rule Validation Summary: lending_package.pdf" in markdown


@pytest.mark.unit
class TestConsolidateAndSave:
    """consolidate_and_save: the synchronous wrapper over the async workflow.

    Its whole job is to run `consolidate_and_save_all` to completion from
    synchronous code, in three environments: no event loop (a Lambda handler), a
    loop that exists but is not running, and a loop that is already running (a
    notebook, where it has to hand the coroutine to a worker thread). The
    coroutine itself is stubbed, because what is under test is the loop handling.
    """

    def _service_with_stubbed_workflow(self, returned):
        service = _service()
        seen: list[tuple[object, object, object]] = []

        async def _workflow(document, config, multiple_sections=None):
            seen.append((document, config, multiple_sections))
            return returned

        service.consolidate_and_save_all = _workflow
        return service, seen

    def test_the_document_config_and_flag_are_forwarded_and_the_result_returned(self):
        sentinel = MagicMock(name="updated-document")
        service, seen = self._service_with_stubbed_workflow(sentinel)
        document = MagicMock(name="document")
        config = {"rule_validation": {"semaphore": 1}}

        assert service.consolidate_and_save(document, config, True) is sentinel
        assert seen == [(document, config, True)]

    def test_multiple_sections_defaults_to_none_when_not_given(self):
        service, seen = self._service_with_stubbed_workflow(MagicMock())
        service.consolidate_and_save(MagicMock(), {})
        assert seen[0][2] is None

    def test_it_works_from_inside_a_running_event_loop(self):
        # The notebook case: asyncio.run would raise here, so the wrapper offloads
        # the coroutine to a thread.
        import asyncio

        sentinel = MagicMock(name="updated-document")
        service, _ = self._service_with_stubbed_workflow(sentinel)

        async def probe():
            return service.consolidate_and_save(MagicMock(), {})

        assert asyncio.run(probe()) is sentinel

    def test_a_failure_inside_the_workflow_propagates_to_the_caller(self):
        service = _service()

        async def _workflow(document, config, multiple_sections=None):
            raise RuntimeError("consolidation failed")

        service.consolidate_and_save_all = _workflow
        with pytest.raises(RuntimeError, match="consolidation failed"):
            service.consolidate_and_save(MagicMock(), {})
