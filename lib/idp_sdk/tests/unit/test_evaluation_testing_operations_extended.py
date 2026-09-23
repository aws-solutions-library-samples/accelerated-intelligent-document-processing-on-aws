# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the remaining ``evaluation`` and ``testing`` operations.

Two public namespaces are covered here, both thin wrappers over a ``_core``
processor.

``EvaluationOperation`` manages accuracy baselines: ``create_baseline`` writes an
expected-value document, ``delete_baseline`` removes one, ``list_baselines``
pages through them, and ``get_report``/``get_metrics`` read comparison results
back. ``tests/unit/test_evaluation_operations.py`` already covers the successful
shapes of the last three and all of ``use_as_baseline``; what is left, and what
this file covers, is ``create_baseline``, ``delete_baseline`` and the error
translation on each method.

``TestingOperation`` drives load tests and Test Studio runs. ``load_test`` copies
one source file into the input bucket at a rate (or on a schedule) and
``abort_test_run`` stops in-flight Test Studio runs;
``tests/unit/test_testing_operations.py`` covers ``get_test_result``,
``compare_test_runs`` and the processor cache.

**What shaped these tests.** These wrappers do three separable things — forward
arguments, map a result, and translate an exception — and the forwarding is
where the interesting mistakes are, because the processors take several
same-typed arguments in a row. So the fakes here record their keyword arguments
and the assertions name them, rather than checking that a call happened.

Two behaviours that read as bugs are pinned with their reasoning:
``load_test`` swallows every exception into a result object where the rest of the
SDK raises, and the duration it reports is the argument rather than the schedule.
"""

import pytest

from idp_sdk import IDPClient
from idp_sdk.exceptions import IDPProcessingError, IDPResourceNotFoundError
from idp_sdk.models import (
    EvaluationBaselineListResult,
    LoadTestResult,
)

pytestmark = pytest.mark.unit

STACK_NAME = "idp-eval-test"
REGION = "us-east-1"


def _client():
    return IDPClient(stack_name=STACK_NAME, region=REGION)


# ---------------------------------------------------------------------------
# A recording EvaluationProcessor
# ---------------------------------------------------------------------------


def _patch_evaluation_processor(monkeypatch, returns=None, raises=None):
    """Install a recording ``EvaluationProcessor``.

    ``returns``/``raises`` are keyed by method name so one fixture serves every
    method on the namespace.
    """
    calls = {}
    returns = returns or {}
    raises = raises or {}

    class FakeEvaluationProcessor:
        def __init__(self, stack_name, region=None, **kwargs):
            calls["init"] = {"stack_name": stack_name, "region": region}

        def _record(self, name, **kwargs):
            calls[name] = kwargs
            if name in raises:
                raise raises[name]
            return returns.get(name)

        def create_baseline(self, document_id, baseline_data, metadata=None):
            return self._record(
                "create_baseline",
                document_id=document_id,
                baseline_data=baseline_data,
                metadata=metadata,
            )

        def delete_baseline(self, document_id):
            return self._record("delete_baseline", document_id=document_id)

        def list_baselines(self, limit=100, next_token=None):
            return self._record("list_baselines", limit=limit, next_token=next_token)

        def get_report(self, document_id, section_id=1):
            return self._record(
                "get_report", document_id=document_id, section_id=section_id
            )

        def get_metrics(self, **kwargs):
            return self._record("get_metrics", **kwargs)

        def use_as_baseline(self, document_id):
            return self._record("use_as_baseline", document_id=document_id)

    monkeypatch.setattr(
        "idp_sdk._core.evaluation_processor.EvaluationProcessor",
        FakeEvaluationProcessor,
    )
    return calls


#: A baseline document: one section, its class, and the expected field values.
BASELINE_DATA = {
    "sections": [
        {
            "section_id": "1",
            "classification": "invoice",
            "attributes": {"total_amount": "1042.55", "vendor": "Acme Corp"},
        }
    ]
}


# ---------------------------------------------------------------------------
# create_baseline()
# ---------------------------------------------------------------------------


class TestCreateBaseline:
    def test_the_document_data_and_metadata_are_forwarded_by_keyword(
        self, aws_credentials, monkeypatch
    ):
        """``document_id`` and the two dictionaries are forwarded by name. A
        positional call would still typecheck and would put the metadata where the
        baseline belongs."""
        calls = _patch_evaluation_processor(
            monkeypatch,
            returns={
                "create_baseline": {
                    "document_id": "batch-1/invoice.pdf",
                    "s3_location": "s3://baselines/batch-1/invoice.pdf/",
                    "sections_written": 1,
                }
            },
        )

        result = _client().evaluation.create_baseline(
            document_id="batch-1/invoice.pdf",
            baseline_data=BASELINE_DATA,
            metadata={"source": "manual review", "reviewer": "qa"},
        )

        assert calls["create_baseline"] == {
            "document_id": "batch-1/invoice.pdf",
            "baseline_data": BASELINE_DATA,
            "metadata": {"source": "manual review", "reviewer": "qa"},
        }
        # The processor's dictionary is returned unchanged — this operation has no
        # typed model, unlike its siblings.
        assert result == {
            "document_id": "batch-1/invoice.pdf",
            "s3_location": "s3://baselines/batch-1/invoice.pdf/",
            "sections_written": 1,
        }
        assert calls["init"] == {"stack_name": STACK_NAME, "region": REGION}

    def test_metadata_is_optional_and_arrives_as_none(
        self, aws_credentials, monkeypatch
    ):
        """``None`` rather than ``{}``: the processor distinguishes "no metadata"
        from "empty metadata" when it writes the record."""
        calls = _patch_evaluation_processor(
            monkeypatch, returns={"create_baseline": {}}
        )

        _client().evaluation.create_baseline(
            document_id="invoice.pdf", baseline_data=BASELINE_DATA
        )

        assert calls["create_baseline"]["metadata"] is None

    def test_a_processor_failure_becomes_a_processing_error(
        self, aws_credentials, monkeypatch
    ):
        _patch_evaluation_processor(
            monkeypatch,
            raises={"create_baseline": RuntimeError("AccessDenied on baseline bucket")},
        )

        with pytest.raises(IDPProcessingError, match="Failed to create baseline"):
            _client().evaluation.create_baseline(
                document_id="invoice.pdf", baseline_data=BASELINE_DATA
            )

    def test_a_stack_override_reaches_the_processor(self, aws_credentials, monkeypatch):
        calls = _patch_evaluation_processor(
            monkeypatch, returns={"create_baseline": {}}
        )

        IDPClient(region=REGION).evaluation.create_baseline(
            document_id="invoice.pdf",
            baseline_data=BASELINE_DATA,
            stack_name="other-stack",
        )

        assert calls["init"]["stack_name"] == "other-stack"

    def test_a_missing_stack_name_is_refused(self, aws_credentials, monkeypatch):
        _patch_evaluation_processor(monkeypatch, returns={"create_baseline": {}})

        with pytest.raises(Exception, match="stack_name is required"):
            IDPClient().evaluation.create_baseline(
                document_id="invoice.pdf", baseline_data=BASELINE_DATA
            )


# ---------------------------------------------------------------------------
# delete_baseline()
# ---------------------------------------------------------------------------


class TestDeleteBaseline:
    def test_the_processor_result_is_returned_unchanged(
        self, aws_credentials, monkeypatch
    ):
        """The count of objects removed is the part a caller acts on, so it must
        survive the wrapper rather than being reduced to a boolean."""
        calls = _patch_evaluation_processor(
            monkeypatch,
            returns={
                "delete_baseline": {
                    "document_id": "batch-1/invoice.pdf",
                    "deleted": True,
                    "files_deleted": 3,
                }
            },
        )

        result = _client().evaluation.delete_baseline("batch-1/invoice.pdf")

        assert calls["delete_baseline"] == {"document_id": "batch-1/invoice.pdf"}
        assert result["files_deleted"] == 3
        assert result["deleted"] is True

    def test_nothing_to_delete_is_reported_rather_than_raised(
        self, aws_credentials, monkeypatch
    ):
        """Deleting a baseline that does not exist is idempotent, not an error:
        the processor says so in the result and the wrapper passes that through.
        Raising here would make a cleanup loop fail on its second pass."""
        _patch_evaluation_processor(
            monkeypatch,
            returns={
                "delete_baseline": {
                    "document_id": "absent.pdf",
                    "deleted": False,
                    "files_deleted": 0,
                }
            },
        )

        result = _client().evaluation.delete_baseline("absent.pdf")

        assert result["deleted"] is False
        assert result["files_deleted"] == 0

    def test_a_processor_failure_becomes_a_processing_error(
        self, aws_credentials, monkeypatch
    ):
        _patch_evaluation_processor(
            monkeypatch,
            raises={"delete_baseline": KeyError("EvaluationBaselineBucket")},
        )

        with pytest.raises(IDPProcessingError, match="Failed to delete baseline"):
            _client().evaluation.delete_baseline("invoice.pdf")


# ---------------------------------------------------------------------------
# error translation on the read methods
# ---------------------------------------------------------------------------


class TestReadMethodErrorTranslation:
    def test_a_report_read_failure_is_a_processing_error_not_a_not_found(
        self, aws_credentials, monkeypatch
    ):
        """``get_report`` maps ``FileNotFoundError`` to
        ``IDPResourceNotFoundError`` and everything else to
        ``IDPProcessingError``. The distinction is what lets a caller retry one
        and give up on the other, so the *other* branch is worth a test of its
        own."""
        _patch_evaluation_processor(
            monkeypatch,
            raises={"get_report": ValueError("Expecting value: line 1 column 1")},
        )

        with pytest.raises(IDPProcessingError, match="Failed to get evaluation report"):
            _client().evaluation.get_report("invoice.pdf")

    def test_a_missing_report_is_still_a_not_found(self, aws_credentials, monkeypatch):
        _patch_evaluation_processor(
            monkeypatch,
            raises={"get_report": FileNotFoundError("No evaluation for invoice.pdf")},
        )

        with pytest.raises(IDPResourceNotFoundError, match="No evaluation"):
            _client().evaluation.get_report("invoice.pdf")

    def test_a_baseline_listing_failure_is_a_processing_error(
        self, aws_credentials, monkeypatch
    ):
        _patch_evaluation_processor(
            monkeypatch, raises={"list_baselines": RuntimeError("bucket missing")}
        )

        with pytest.raises(IDPProcessingError, match="Failed to list baselines"):
            _client().evaluation.list_baselines()

    def test_the_listing_pagination_arguments_are_forwarded(
        self, aws_credentials, monkeypatch
    ):
        """``limit`` and ``next_token`` are the whole interface to a paged
        listing; a dropped cursor silently re-returns page one forever."""
        calls = _patch_evaluation_processor(
            monkeypatch,
            returns={
                "list_baselines": {
                    "baselines": [
                        {
                            "document_id": "batch-1/invoice.pdf",
                            "s3_location": "s3://baselines/batch-1/invoice.pdf/",
                        }
                    ],
                    "count": 1,
                    # Bandit's B105 matches the *identifier*, never the value, and
                    # `next_token` is the SDK's real pagination field name.
                    "next_token": "Y3Vyc29y",  # nosec B105 - page cursor, base64 of "cursor"
                }
            },
        )

        result = _client().evaluation.list_baselines(
            limit=25,
            # B106, not B105: as a *keyword argument* this is the funcarg check.
            next_token="cHJldg==",  # nosec B106 - inbound page cursor, not a credential
        )

        assert calls["list_baselines"] == {
            "limit": 25,
            "next_token": "cHJldg==",  # nosec B105 - echoed page cursor
        }
        assert isinstance(result, EvaluationBaselineListResult)
        assert result.count == 1
        assert result.baselines[0].document_id == "batch-1/invoice.pdf"
        assert result.next_token == "Y3Vyc29y"  # nosec B105 - cursor read back

    def test_a_metrics_failure_is_a_processing_error(
        self, aws_credentials, monkeypatch
    ):
        _patch_evaluation_processor(
            monkeypatch, raises={"get_metrics": RuntimeError("Athena query failed")}
        )

        with pytest.raises(IDPProcessingError, match="Failed to get evaluation"):
            _client().evaluation.get_metrics()


# ---------------------------------------------------------------------------
# testing.load_test()
# ---------------------------------------------------------------------------


def _patch_load_tester(monkeypatch, constant=None, scheduled=None, raises=None):
    calls = {}

    class FakeLoadTester:
        def __init__(self, stack_name, region=None, **kwargs):
            calls["init"] = {"stack_name": stack_name, "region": region}

        def run_constant_load(self, **kwargs):
            calls["run_constant_load"] = kwargs
            if raises is not None:
                raise raises
            return constant

        def run_scheduled_load(self, **kwargs):
            calls["run_scheduled_load"] = kwargs
            if raises is not None:
                raise raises
            return scheduled

    monkeypatch.setattr("idp_sdk._core.load_test.LoadTester", FakeLoadTester)
    return calls


class TestLoadTest:
    def test_a_constant_load_forwards_the_rate_and_duration(
        self, aws_credentials, monkeypatch
    ):
        calls = _patch_load_tester(
            monkeypatch, constant={"success": True, "total_files": 250}
        )

        result = _client().testing.load_test(
            source_file="./samples/lending_package.pdf",
            rate=50,
            duration=5,
            dest_prefix="soak",
        )

        assert calls["run_constant_load"] == {
            "source_file": "./samples/lending_package.pdf",
            "rate": 50,
            "duration": 5,
            "dest_prefix": "soak",
            "config_version": None,
        }
        assert "run_scheduled_load" not in calls
        assert isinstance(result, LoadTestResult)
        assert result.success is True
        assert result.total_files == 250
        assert result.duration_minutes == 5
        assert result.error is None

    def test_a_schedule_file_selects_the_scheduled_load_instead(
        self, aws_credentials, monkeypatch
    ):
        """The schedule carries its own per-minute counts, so ``rate`` is not
        forwarded — passing it would be ambiguous about which one wins."""
        calls = _patch_load_tester(
            monkeypatch, scheduled={"success": True, "total_files": 900}
        )

        result = _client().testing.load_test(
            source_file="./doc.pdf",
            schedule_file="./schedule.csv",
            rate=50,
            dest_prefix="ramp",
        )

        assert calls["run_scheduled_load"] == {
            "source_file": "./doc.pdf",
            "schedule_file": "./schedule.csv",
            "dest_prefix": "ramp",
            "config_version": None,
        }
        assert "run_constant_load" not in calls
        assert result.total_files == 900

    def test_a_scheduled_run_reports_the_duration_argument_not_the_schedule(
        self, aws_credentials, monkeypatch
    ):
        """DEFECT (operations/testing.py:102). ``duration_minutes`` in the result
        is the ``duration`` *argument*, which a scheduled run never uses — so a
        30-minute ramp reports ``duration_minutes=1``, the default. Anything
        dividing ``total_files`` by it to get a rate is out by a factor of the
        schedule length. Pinned as-is; the honest value would come from the
        tester's own result.
        """
        _patch_load_tester(monkeypatch, scheduled={"success": True, "total_files": 900})

        result = _client().testing.load_test(
            source_file="./doc.pdf", schedule_file="./30-minute-ramp.csv"
        )

        assert result.total_files == 900
        assert result.duration_minutes == 1

    def test_the_defaults_are_the_documented_ones(self, aws_credentials, monkeypatch):
        calls = _patch_load_tester(monkeypatch, constant={"success": True})

        _client().testing.load_test(source_file="./doc.pdf")

        assert calls["run_constant_load"]["rate"] == 100
        assert calls["run_constant_load"]["duration"] == 1
        assert calls["run_constant_load"]["dest_prefix"] == "load-test"

    def test_a_config_profile_is_forwarded_under_the_old_parameter_name(
        self, aws_credentials, monkeypatch
    ):
        """``config_profile`` is the current spelling and ``config_version`` the
        former one; the tester still takes the old name, so the alias has to be
        collapsed here rather than passed through."""
        calls = _patch_load_tester(monkeypatch, constant={"success": True})

        _client().testing.load_test(
            source_file="./doc.pdf", config_profile="bank-statement-sample"
        )

        assert calls["run_constant_load"]["config_version"] == "bank-statement-sample"

    def test_the_old_spelling_still_works(self, aws_credentials, monkeypatch):
        calls = _patch_load_tester(monkeypatch, constant={"success": True})

        _client().testing.load_test(source_file="./doc.pdf", config_version="rvl-cdip")

        assert calls["run_constant_load"]["config_version"] == "rvl-cdip"

    def test_both_spellings_agreeing_is_accepted(self, aws_credentials, monkeypatch):
        calls = _patch_load_tester(monkeypatch, constant={"success": True})

        _client().testing.load_test(
            source_file="./doc.pdf",
            config_profile="rvl-cdip",
            config_version="rvl-cdip",
        )

        assert calls["run_constant_load"]["config_version"] == "rvl-cdip"

    def test_conflicting_spellings_are_refused_before_anything_is_copied(
        self, aws_credentials, monkeypatch
    ):
        """Picking one silently would run thousands of documents against a
        configuration the caller did not ask for. The refusal happens above the
        ``try``, so it surfaces as ``ValueError`` rather than as a failed
        ``LoadTestResult`` — which is the right way round here, since a result
        object could be ignored."""
        calls = _patch_load_tester(monkeypatch, constant={"success": True})

        with pytest.raises(ValueError, match="two names for the same"):
            _client().testing.load_test(
                source_file="./doc.pdf",
                config_profile="rvl-cdip",
                config_version="bank-statement-sample",
            )

        assert calls == {}

    def test_a_missing_success_key_is_reported_as_failure(
        self, aws_credentials, monkeypatch
    ):
        _patch_load_tester(monkeypatch, constant={"total_files": 10})

        result = _client().testing.load_test(source_file="./doc.pdf")

        assert result.success is False
        assert result.total_files == 10

    def test_a_tester_error_is_carried_in_the_result(
        self, aws_credentials, monkeypatch
    ):
        _patch_load_tester(
            monkeypatch,
            constant={"success": False, "error": "source file not found"},
        )

        result = _client().testing.load_test(source_file="./missing.pdf")

        assert result.success is False
        assert result.error == "source file not found"
        assert result.total_files == 0

    def test_an_exception_is_swallowed_into_a_failed_result_rather_than_raised(
        self, aws_credentials, monkeypatch
    ):
        """DEFECT-adjacent, pinned deliberately (operations/testing.py:105-108).
        Every other method on this namespace turns an underlying exception into
        ``IDPProcessingError``; ``load_test`` alone returns
        ``LoadTestResult(success=False)``. A caller who writes
        ``client.testing.load_test(...)`` without inspecting the result — which
        raising would have made impossible — sees a load test that silently did
        nothing. The duration is still echoed, so the result looks populated.
        """
        _patch_load_tester(monkeypatch, raises=RuntimeError("input bucket missing"))

        result = _client().testing.load_test(source_file="./doc.pdf", duration=3)

        assert isinstance(result, LoadTestResult)
        assert result.success is False
        assert result.error == "input bucket missing"
        assert result.total_files == 0
        assert result.duration_minutes == 3

    def test_a_stack_override_reaches_the_tester(self, aws_credentials, monkeypatch):
        calls = _patch_load_tester(monkeypatch, constant={"success": True})

        IDPClient(region=REGION).testing.load_test(
            source_file="./doc.pdf", stack_name="other-stack"
        )

        assert calls["init"] == {"stack_name": "other-stack", "region": REGION}


# ---------------------------------------------------------------------------
# testing.abort_test_run()
# ---------------------------------------------------------------------------


def _patch_test_studio(monkeypatch, returns=None, raises=None):
    calls = {}

    class FakeTestStudioProcessor:
        def __init__(self, stack_name, region=None, **kwargs):
            calls.setdefault("init", []).append(
                {"stack_name": stack_name, "region": region}
            )

        def abort_test_runs(self, test_run_ids):
            calls["abort_test_runs"] = test_run_ids
            if raises is not None:
                raise raises
            return returns

    monkeypatch.setattr(
        "idp_sdk._core.test_studio_processor.TestStudioProcessor",
        FakeTestStudioProcessor,
    )
    return calls


class TestAbortTestRun:
    def test_the_processor_result_is_returned_unchanged(
        self, aws_credentials, monkeypatch
    ):
        """This is the one method on the namespace with no typed model, and the
        per-run counts are what a caller reports to a user — a partial abort has
        to stay visible as partial."""
        calls = _patch_test_studio(
            monkeypatch,
            returns={
                "success": True,
                "message": "Aborted 2 of 3 test runs",
                "aborted_count": 2,
                "failed_count": 1,
                "errors": ["tr-3 is already COMPLETE"],
            },
        )

        result = _client().testing.abort_test_run(["tr-1", "tr-2", "tr-3"])

        assert calls["abort_test_runs"] == ["tr-1", "tr-2", "tr-3"]
        assert result["aborted_count"] == 2
        assert result["failed_count"] == 1
        assert result["errors"] == ["tr-3 is already COMPLETE"]

    def test_a_single_run_is_still_passed_as_a_list(self, aws_credentials, monkeypatch):
        """The processor's parameter is plural; handing it a bare string would be
        iterated character by character into as many bogus run ids."""
        calls = _patch_test_studio(monkeypatch, returns={"success": True})

        _client().testing.abort_test_run(["tr-1"])

        assert calls["abort_test_runs"] == ["tr-1"]

    def test_a_failure_becomes_a_processing_error(self, aws_credentials, monkeypatch):
        _patch_test_studio(
            monkeypatch, raises=RuntimeError("AbortTestRunsResolverFunction not found")
        )

        with pytest.raises(IDPProcessingError, match="Failed to abort test runs"):
            _client().testing.abort_test_run(["tr-1"])

    def test_the_processor_is_reused_across_calls_for_one_stack(
        self, aws_credentials, monkeypatch
    ):
        """Constructing a ``TestStudioProcessor`` discovers stack resources, so
        the cache is what stops an abort of five runs re-reading the stack five
        times."""
        calls = _patch_test_studio(monkeypatch, returns={"success": True})

        client = _client()
        client.testing.abort_test_run(["tr-1"])
        client.testing.abort_test_run(["tr-2"])

        assert len(calls["init"]) == 1

    def test_a_stack_override_gets_its_own_cached_processor(
        self, aws_credentials, monkeypatch
    ):
        calls = _patch_test_studio(monkeypatch, returns={"success": True})

        client = _client()
        client.testing.abort_test_run(["tr-1"])
        client.testing.abort_test_run(["tr-2"], stack_name="other-stack")

        assert [entry["stack_name"] for entry in calls["init"]] == [
            STACK_NAME,
            "other-stack",
        ]
