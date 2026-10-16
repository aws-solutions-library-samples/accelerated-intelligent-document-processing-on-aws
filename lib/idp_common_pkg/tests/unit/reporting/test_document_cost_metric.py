#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The ``DocumentEstimatedCost`` CloudWatch metric (GitHub issue #934, item 2).

Cost here was measurable and not alarmable: ``save_metering_data`` computes an
``estimated_cost`` per metering row and writes it to parquet for Athena, which is
a ledger. This metric is the same number on a surface a CloudWatch alarm can
read.

Everything below is driven through the **real** ``save_metering_data`` with a
fake CloudWatch client, not through the helper in isolation, because every
property worth pinning is a property of the wiring rather than of the helper:

* **The emitted value is the one already written to the parquet row.** Not a
  recomputation — the test reads the records handed to ``_save_records_as_parquet``
  and asserts the metric equals their sum, so a second pricing implementation
  creeping in here would show up as a disagreement rather than as two
  self-consistent wrong answers.
* **A row with no price does not become a zero.** The metering columns already
  write NULL rather than 0.00 for an unpriced row, on the grounds that a zero is
  indistinguishable from something genuinely free and silently understates spend.
  A metric is worse: a 0.00 datapoint drags an average down and makes a rolling
  sum under-read. So an all-unpriced document emits no cost datapoint at all,
  while a *measured* zero still does.
* **An incomplete cost figure is visible.** If the pricing table does not cover
  the model in use, the cost metric reads low — or vanishes — and nothing on a
  dashboard says why. ``UnpricedMeteringRows`` is emitted unconditionally,
  including zero, so "the figure is complete" is an observable datapoint and not
  an absence.
* **Dimensions stay empty.** CloudWatch bills per metric per dimension
  combination. A ``DocumentId``, ``DocumentClass`` or ``Model`` dimension here
  grows without bound and can cost more than the spend it reports, so the
  cardinality is pinned at the shape that is flat in document volume.
* **CloudWatch cannot fail a document's reporting write.** Cost telemetry is
  best-effort; a PutMetricData error must not propagate.
"""

from unittest.mock import MagicMock, patch

import pytest

from idp_common.config.models import IDPConfig
from idp_common.models import Document
from idp_common.reporting.save_reporting_data import SaveReportingData

# Two priced units and one service with no pricing entry at all, so a single
# document can exercise the priced and unpriced paths together.
_PRICING = {
    "pricing": [
        {
            "name": "bedrock/test-model",
            "units": [
                {"name": "inputTokens", "price": "2.0E-6"},
                {"name": "outputTokens", "price": "1.0E-5"},
            ],
        },
        {
            "name": "textract/detect_document_text",
            "units": [{"name": "pages", "price": "0.0015"}],
        },
        {
            # A genuinely free priced unit: the rate is zero, so the row's cost is
            # a MEASURED zero rather than an unknown one.
            "name": "freebie/service",
            "units": [{"name": "calls", "price": "0.0"}],
        },
    ]
}


def _reporter():
    with patch("boto3.client"):
        return SaveReportingData(
            "test-bucket", config=IDPConfig.model_validate(_PRICING)
        )


def _run(metering):
    """Drive the real ``save_metering_data`` and capture both write surfaces.

    Returns ``(put_metric_data_calls, parquet_records)``.
    """
    doc = Document(id="doc-cost-1", input_key="test/doc.pdf")
    doc.initial_event_time = "2026-01-01T00:00:00Z"
    doc.num_pages = 2
    doc.metering = metering

    reporter = _reporter()
    fake_cw = MagicMock()
    with (
        patch("idp_common.metrics.get_cloudwatch_client", return_value=fake_cw),
        patch.object(reporter, "_save_records_as_parquet") as mock_parquet,
    ):
        reporter.save_metering_data(doc)

    records = mock_parquet.call_args[0][0] if mock_parquet.call_args else []
    return [c.kwargs for c in fake_cw.put_metric_data.call_args_list], records


def _datum(calls, metric_name):
    """The single datum for ``metric_name`` across every call, or None."""
    found = [
        d
        for call in calls
        for d in call["MetricData"]
        if d["MetricName"] == metric_name
    ]
    assert len(found) <= 1, f"{metric_name} emitted {len(found)} times, expected <= 1"
    return found[0] if found else None


@pytest.mark.unit
class TestDocumentEstimatedCostMetric:
    def test_emits_the_cost_already_written_to_the_parquet_row(self):
        """The datapoint equals the sum of the rows' own ``estimated_cost``.

        Compared against the records the writer was handed rather than against a
        hand-multiplied constant: the requirement is that the metric and the
        Athena ledger are the same number, and only a comparison between the two
        surfaces can show that. The hand-computed value is asserted as well, so a
        bug that zeroed *both* surfaces identically still fails.
        """
        calls, records = _run(
            {
                "extraction/bedrock/test-model": {
                    "inputTokens": 100_000.0,
                    "outputTokens": 2_000.0,
                },
                "ocr/textract/detect_document_text": {"pages": 2.0},
            }
        )

        from_records = sum(
            r["estimated_cost"] for r in records if r["estimated_cost"] is not None
        )
        datum = _datum(calls, "DocumentEstimatedCost")
        assert datum is not None, "no DocumentEstimatedCost datapoint was emitted"
        assert datum["Value"] == pytest.approx(from_records)
        # 100000*2.0E-6 + 2000*1.0E-5 + 2*0.0015 = 0.2 + 0.02 + 0.003
        assert datum["Value"] == pytest.approx(0.223)
        assert datum["Unit"] == "None", (
            "CloudWatch has no currency unit; 'None' is its dimensionless unit"
        )

    def test_a_document_with_no_priced_row_emits_no_cost_datapoint(self):
        """Not a zero. An unpriced row writes NULL to parquet, and the metric has
        to agree: a 0.00 datapoint would read as a free document, pull an average
        down and make a rolling sum under-report, with nothing to distinguish it
        from a document that genuinely cost nothing."""
        calls, records = _run(
            {"extraction/bedrock/unpriced-model": {"inputTokens": 500.0}}
        )

        assert all(r["estimated_cost"] is None for r in records)
        assert _datum(calls, "DocumentEstimatedCost") is None, (
            "an all-unpriced document must emit no cost datapoint, not a zero"
        )
        # ...but the gap itself is still reported.
        unpriced = _datum(calls, "UnpricedMeteringRows")
        assert unpriced is not None and unpriced["Value"] == 1.0

    def test_a_measured_zero_is_still_emitted(self):
        """The rule is "no price" vs "priced at zero", not "cost == 0".

        A unit with a published rate of 0.0 is a measured zero and belongs on the
        metric; suppressing it would hide the datapoint that says the document
        completed.
        """
        calls, _ = _run({"post/freebie/service": {"calls": 3.0}})

        datum = _datum(calls, "DocumentEstimatedCost")
        assert datum is not None, "a priced-at-zero document must still emit"
        assert datum["Value"] == 0.0
        assert _datum(calls, "UnpricedMeteringRows")["Value"] == 0.0

    def test_a_partly_unpriced_document_emits_the_priced_part_and_the_gap(self):
        """Mixed rows: the cost covers the priced ones and the count names the
        rest. Reporting only the cost would be a figure that is quietly short by
        an unknown amount."""
        calls, records = _run(
            {
                "ocr/textract/detect_document_text": {"pages": 4.0},
                "extraction/bedrock/unpriced-model": {
                    "inputTokens": 10.0,
                    "outputTokens": 20.0,
                },
            }
        )

        from_records = sum(
            r["estimated_cost"] for r in records if r["estimated_cost"] is not None
        )
        assert _datum(calls, "DocumentEstimatedCost")["Value"] == pytest.approx(
            from_records
        )
        assert _datum(calls, "DocumentEstimatedCost")["Value"] == pytest.approx(0.006)
        assert _datum(calls, "UnpricedMeteringRows")["Value"] == 2.0, (
            "two unpriced ROWS — the per-document warning set de-duplicates on "
            "(service, unit), which is not a row count"
        )

    def test_unpriced_rows_is_emitted_even_when_there_are_none(self):
        """Zero is the datapoint that says the cost figure is complete. Emitting
        only on a non-zero makes a healthy deployment indistinguishable from one
        whose metric stopped being written."""
        calls, _ = _run({"ocr/textract/detect_document_text": {"pages": 1.0}})
        datum = _datum(calls, "UnpricedMeteringRows")
        assert datum is not None and datum["Value"] == 0.0
        assert datum["Unit"] == "Count"

    def test_no_dimensions_on_either_metric(self):
        """Cardinality is the cost. Every candidate dimension here grows with use
        — ``DocumentId`` is one metric per document, ``DocumentClass`` is a
        customer-defined taxonomy, ``Model``/``ServiceApi`` grows with each model
        and processing step — and CloudWatch bills per dimension combination, so a
        dimension added here can cost more than the spend it reports. The
        per-model and per-class breakdown already exists in the metering parquet
        and the UI's per-document panel; what those cannot do is drive an alarm,
        which is all this metric is for.
        """
        calls, _ = _run(
            {
                "extraction/bedrock/test-model": {"inputTokens": 1_000.0},
                "ocr/textract/detect_document_text": {"pages": 1.0},
            }
        )
        data = [d for call in calls for d in call["MetricData"]]
        assert data, "expected at least one datum"
        for d in data:
            assert d["Dimensions"] == [], (
                f"{d['MetricName']} gained dimensions {d['Dimensions']!r}; see the "
                f"docstring before adding one"
            )

    def test_emitted_in_one_api_call(self):
        """Both metrics ride one PutMetricData. Two calls would double the
        request charge on a per-document code path for no benefit."""
        calls, _ = _run({"extraction/bedrock/test-model": {"inputTokens": 10.0}})
        assert len(calls) == 1, f"expected 1 PutMetricData call, got {len(calls)}"

    def test_namespace_comes_from_the_environment(self):
        """The stack grants this Lambda ``cloudwatch:PutMetricData`` under a
        ``cloudwatch:namespace`` StringEquals condition set to the stack name, so
        a hardcoded namespace is denied at the IAM boundary — and the denial is a
        logged warning, i.e. a metric that silently never appears. Reading
        ``METRIC_NAMESPACE`` is what keeps the grant usable.
        """
        doc = Document(id="doc-ns", input_key="k")
        doc.metering = {"ocr/textract/detect_document_text": {"pages": 1.0}}
        reporter = _reporter()
        fake_cw = MagicMock()
        with (
            patch.dict("os.environ", {"METRIC_NAMESPACE": "my-stack-name"}),
            patch("idp_common.metrics.get_cloudwatch_client", return_value=fake_cw),
            patch.object(reporter, "_save_records_as_parquet"),
        ):
            reporter.save_metering_data(doc)
        assert fake_cw.put_metric_data.call_args.kwargs["Namespace"] == "my-stack-name"

    def test_the_namespace_falls_back_the_same_way_put_metric_does(self):
        """With ``METRIC_NAMESPACE`` unset the namespace is ``GENAIDP``.

        A mutation run found this unasserted, and it is worth having: the
        fallback is what a non-Lambda caller and a function whose environment
        variable went missing both land on, and the failure it prevents is quiet.
        An empty namespace is rejected by CloudWatch, and this module swallows
        that rejection as a logged warning — so the metric would simply never
        appear. The value also has to stay equal to :func:`put_metric`'s default,
        or two emitters in the same process would write to two namespaces.
        """
        import os

        from idp_common.metrics import put_metric

        doc = Document(id="doc-default-ns", input_key="k")
        doc.metering = {"ocr/textract/detect_document_text": {"pages": 1.0}}
        reporter = _reporter()
        fake_cw = MagicMock()
        env = {k: v for k, v in os.environ.items() if k != "METRIC_NAMESPACE"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("idp_common.metrics.get_cloudwatch_client", return_value=fake_cw),
            patch.object(reporter, "_save_records_as_parquet"),
        ):
            reporter.save_metering_data(doc)
            ours = fake_cw.put_metric_data.call_args.kwargs["Namespace"]
            fake_cw.put_metric_data.reset_mock()
            put_metric("Probe", 1.0)
            theirs = fake_cw.put_metric_data.call_args.kwargs["Namespace"]

        assert ours == "GENAIDP", f"fallback namespace is {ours!r}"
        assert ours == theirs, (
            f"this helper falls back to {ours!r} while put_metric falls back to "
            f"{theirs!r}; two emitters in one process would split the namespace"
        )

    def test_a_cloudwatch_failure_does_not_fail_the_reporting_write(self):
        """Cost visibility is best-effort. A PutMetricData error that propagated
        would turn a CloudWatch problem into a failed document."""
        doc = Document(id="doc-boom", input_key="k")
        doc.metering = {"ocr/textract/detect_document_text": {"pages": 1.0}}
        reporter = _reporter()
        fake_cw = MagicMock()
        fake_cw.put_metric_data.side_effect = RuntimeError("throttled")
        with (
            patch("idp_common.metrics.get_cloudwatch_client", return_value=fake_cw),
            patch.object(reporter, "_save_records_as_parquet") as mock_parquet,
        ):
            result = reporter.save_metering_data(doc)

        assert result is not None and result["statusCode"] == 200
        assert mock_parquet.called, "the parquet write must still happen"

    def test_a_document_with_no_metering_emits_nothing(self):
        """``save_metering_data`` returns early for a document with no metering,
        so there is no spend to report and no datapoint — including no
        ``UnpricedMeteringRows`` zero, which would otherwise make every
        no-metering document look like a completed priced one."""
        doc = Document(id="doc-empty", input_key="k")
        doc.metering = {}
        reporter = _reporter()
        fake_cw = MagicMock()
        with (
            patch("idp_common.metrics.get_cloudwatch_client", return_value=fake_cw),
            patch.object(reporter, "_save_records_as_parquet"),
        ):
            assert reporter.save_metering_data(doc) is None
        assert not fake_cw.put_metric_data.called


@pytest.mark.unit
class TestMetricIsReportingOnly:
    """Item 3 of #934 — wiring spend to the circuit breaker — is an undecided
    product question, and the decision recorded on the issue is that the metric
    comes first and enforcement only afterwards, throttling rather than halting.
    So this change must not have smuggled in an enforcement path: nothing in the
    emit path may touch the breaker, the concurrency counter or the queue.
    """

    def test_the_emit_path_takes_no_action_on_cost(self):
        import inspect

        from idp_common import metrics

        source = inspect.getsource(metrics.emit_document_cost_metrics)
        forbidden = (
            "circuit_breaker",
            "CircuitBreaker",
            "MAX_CONCURRENT",
            "max_concurrent",
            "ConcurrencyTable",
            "put_alarm",
            "PutMetricAlarm",
            "set_alarm_state",
            "update_function_configuration",
        )
        for token in forbidden:
            assert token not in source, (
                f"{token!r} appears in the cost-emit path. This metric reports; "
                f"it must not act. See #934 item 3."
            )
