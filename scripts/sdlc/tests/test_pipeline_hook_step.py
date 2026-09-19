# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for Step 14, the pipeline-hook end-to-end CI step.

Step 14 itself needs a live stack, so what is testable here is the part that
fails *silently in CI* if it is wrong — the hook Lambda's package and its handler
logic. Both bugs these tests pin were real, found while writing the step:

1. **The zip built without pydantic.** The first version located dependencies by
   assuming they were siblings of `idp_common` in one site-packages directory.
   With an editable install they are not, so it produced a zip containing
   `idp_common` but no `pydantic` — which imports fine locally and dies with
   `ModuleNotFoundError` only at Lambda cold start, in CI.

2. **The marker was written to a field that is never serialized.** The handler
   first wrote its marker into `Document.metadata`, which `Document.to_dict()`
   drops entirely — so the mutation could never reach the persisted document and
   the step's central assertion would always have failed.

Together these are the "does the test itself work" layer: a broken Step 14 that
always fails is noisy, but a broken Step 14 that always *passes* would be worse
than having no test at all.
"""

import ast
import json
import os
import zipfile

import pytest


@pytest.mark.unit
class TestHookZipBuild:
    """The Lambda package must actually contain what the handler imports."""

    def test_zip_contains_handler_and_runtime_deps(self, cbd, tmp_path):
        """idp_common alone is not enough — its import chain reaches pydantic."""
        pytest.importorskip("idp_common")
        path = cbd._build_hook_zip(str(tmp_path / "hook.zip"))
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()

        assert "index.py" in names
        assert "idp_common/hooks/__init__.py" in names, (
            "the handler imports idp_common.hooks"
        )
        # config.models imports pydantic at module scope, and models.py is on
        # the load_hook_document path.
        assert any(n.startswith("pydantic/") for n in names), (
            "pydantic missing — the Lambda would fail at import time"
        )
        assert any(n.startswith("pydantic_core/") for n in names)

    def test_zip_stays_under_the_direct_upload_limit(self, cbd, tmp_path):
        """create_function with ZipFile= caps at 50MB; a site-packages sweep
        would blow past it, so the dependency list is deliberately explicit."""
        pytest.importorskip("idp_common")
        path = cbd._build_hook_zip(str(tmp_path / "hook.zip"))
        assert os.path.getsize(path) < 50 * 1024 * 1024

    def test_zip_excludes_bytecode(self, cbd, tmp_path):
        pytest.importorskip("idp_common")
        path = cbd._build_hook_zip(str(tmp_path / "hook.zip"))
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
        assert not [n for n in names if n.endswith((".pyc", ".pyo"))]
        assert not [n for n in names if "__pycache__" in n]

    def test_build_fails_loudly_when_a_required_dep_is_missing(
        self, cbd, tmp_path, monkeypatch
    ):
        """The whole point of the validation step: a zip missing a required
        package must raise HERE, not at Lambda cold start in CI."""
        pytest.importorskip("idp_common")
        import importlib

        real_import = importlib.import_module

        def fake_import(name, *a, **k):
            # Fail ONLY pydantic — idp_common must still import, so the builder
            # gets far enough to produce the exact silent-failure zip this
            # validation exists to catch.
            if name == "pydantic":
                raise ImportError("simulated missing pydantic")
            return real_import(name, *a, **k)

        monkeypatch.setattr(importlib, "import_module", fake_import)
        with pytest.raises(RuntimeError, match="pydantic"):
            cbd._build_hook_zip(str(tmp_path / "hook.zip"))


@pytest.mark.unit
class TestHookHandlerSource:
    """The inline handler source is shipped as a string, so nothing type-checks
    or imports it in the normal build — these tests are its only guard."""

    def test_source_parses(self, cbd):
        ast.parse(cbd._HOOK_SOURCE)

    def _run(self, cbd, monkeypatch, point, document):
        monkeypatch.setenv("MARKER_KEY", cbd._HOOK_MARKER_KEY)
        # Required: the handler refuses to run without it (see
        # TestWorkingBucketWiring). Inline documents never touch S3, so any
        # non-empty value is fine here.
        monkeypatch.setenv("WORKING_BUCKET", "test-working-bucket")
        ns = {}
        exec(  # noqa: S102 — executing our own shipped source under test  # nosec B102 - executes this repo's own shipped source under test
            compile(cbd._HOOK_SOURCE, "index.py", "exec"), ns
        )
        event = {
            "hookPoint": point,
            "args": [{"key": "note", "value": f"ci-{point}"}],
            "document": document,
        }
        return ns["lambda_handler"](event, None)

    def _doc(self, **over):
        doc = {
            "id": "w2.pdf",
            "input_key": "w2.pdf",
            "status": "COMPLETED",
            "num_pages": 2,
            "sections": [
                {"section_id": "1", "classification": "Invoice", "page_ids": ["1"]}
            ],
        }
        doc.update(over)
        return doc

    def test_marker_survives_serialization(self, cbd, monkeypatch):
        """The regression: `Document.metadata` is a runtime-only field that
        to_dict() drops, so a marker written there never reaches the persisted
        document. It must land somewhere that round-trips."""
        pytest.importorskip("idp_common")
        result = self._run(cbd, monkeypatch, "postprocessing", self._doc())
        serialized = json.dumps(result["updatedDocument"])
        assert cbd._HOOK_MARKER_KEY in serialized, (
            "marker absent from the serialized document — Step 14's persisted-"
            "marker assertion could never pass"
        )

    def test_marker_lands_in_section_attributes(self, cbd, monkeypatch):
        """Section attributes are what a real mutating hook changes, and they
        round-trip — so that is where the marker goes."""
        pytest.importorskip("idp_common")
        result = self._run(cbd, monkeypatch, "postprocessing", self._doc())
        section = result["updatedDocument"]["sections"][0]
        assert cbd._HOOK_MARKER_KEY in (section.get("attributes") or {})

    def test_marker_records_the_hook_point(self, cbd, monkeypatch):
        """Both points share one Lambda, so the marker must say which fired."""
        pytest.importorskip("idp_common")
        result = self._run(cbd, monkeypatch, "postprocessing", self._doc())
        section = result["updatedDocument"]["sections"][0]
        marker = section["attributes"][cbd._HOOK_MARKER_KEY]
        assert marker["hookPoint"] == "postprocessing"
        assert marker["note"] == "ci-postprocessing"
        assert result["ciHookPoint"] == "postprocessing"

    def test_returns_the_documented_update_key(self, cbd, monkeypatch):
        """The dispatcher only honors `updatedDocument`; any other shape is a
        silent no-op mutation."""
        pytest.importorskip("idp_common")
        result = self._run(cbd, monkeypatch, "postprocessing", self._doc())
        assert "updatedDocument" in result
        assert result["ciHookRan"] is True

    def test_survives_a_sectionless_document(self, cbd, monkeypatch):
        """At `preprocessing` there are no sections yet (OCR/classification have
        not run). The handler must not crash, and must still write the
        document-level backstop."""
        pytest.importorskip("idp_common")
        result = self._run(
            cbd, monkeypatch, "preprocessing", self._doc(sections=[])
        )
        doc = result["updatedDocument"]
        assert doc["summary_report_uri"] == f"{cbd._HOOK_MARKER_KEY}:preprocessing"

    def test_reports_the_hitl_status_it_observed(self, cbd, monkeypatch):
        """postprocessing fires while a HITL review is pending, so the hook has
        to be able to see that state to branch on it."""
        pytest.importorskip("idp_common")
        result = self._run(
            cbd, monkeypatch, "postprocessing", self._doc(hitl_status="PendingReview")
        )
        marker = result["updatedDocument"]["sections"][0]["attributes"][
            cbd._HOOK_MARKER_KEY
        ]
        assert marker["saw_hitl_status"] == "PendingReview"

    def test_absent_hitl_status_reads_as_none(self, cbd, monkeypatch):
        """HITL fields are omitted when falsy, so absent must mean "no HITL"
        rather than raising."""
        pytest.importorskip("idp_common")
        result = self._run(cbd, monkeypatch, "postprocessing", self._doc())
        marker = result["updatedDocument"]["sections"][0]["attributes"][
            cbd._HOOK_MARKER_KEY
        ]
        assert marker["saw_hitl_status"] is None


@pytest.mark.unit
class TestStepRegistration:
    """A step that exists but is never registered is dead code — and the suite
    derives its summary and failure analysis from these lists."""

    def test_step14_is_in_the_parallel_pool(self, cbd):
        names = [entry[1] for entry in cbd.PARALLEL_TEST_STEPS]
        assert "Step 14" in names

    def test_step14_is_reachable_from_all_test_steps(self, cbd):
        funcs = [entry[0] for entry in cbd.ALL_TEST_STEPS]
        assert cbd.test_step14_pipeline_hooks in funcs

    def test_hook_resources_are_named_for_the_ci_role_scope(self, cbd):
        """The CI CodeBuild role scopes `iam:*` to `role/idp-*` and `lambda:*` to
        `function:idp-*`, so a `GENAIIDP-` prefixed name is AccessDenied — which
        is exactly how this step first failed in the pipeline. The `idp-` prefix
        is therefore load-bearing, not cosmetic."""
        assert cbd._HOOK_FN_PREFIX.startswith("idp-")
        assert not cbd._HOOK_FN_PREFIX.startswith("GENAIIDP-"), (
            "GENAIIDP-* is outside the CI role's iam:*/lambda:* resource scope"
        )

    def test_hook_uses_the_tag_path_to_clear_the_dispatcher_iam_condition(self, cbd):
        """Because the function is NOT named `GENAIIDP-*`, the dispatcher's other
        allow path — the `idp:feature-id` ABAC tag — is the only thing
        authorizing the invoke. A non-empty tag value is required (the policy
        condition is StringLike '*')."""
        assert cbd._HOOK_FEATURE_ID
        assert isinstance(cbd._HOOK_FEATURE_ID, str)

    def test_step_tags_the_function_it_creates(self, cbd):
        """Guard the wiring itself: the step must actually apply the tag, or the
        dispatcher fails closed with AccessDenied at every dispatch. Asserted on
        the source because the call needs live AWS to execute."""
        import inspect

        src = inspect.getsource(cbd.test_step14_pipeline_hooks)
        assert "tag_resource" in src, (
            "the hook Lambda must be tagged idp:feature-id — without it the "
            "dispatcher cannot invoke a function not named GENAIIDP-*"
        )
        assert "list_tags" in src, "the tag should be verified, not assumed"


@pytest.mark.unit
class TestStepInvocationCorrectness:
    """Pins the three bugs found auditing Step 14 before its second pipeline run.

    All three would have failed the step (or, worse, passed/failed it for the
    wrong reason) and none are reachable without a live stack, so they are
    asserted against the step's source.
    """

    def _src(self, cbd):
        import inspect

        return inspect.getsource(cbd.test_step14_pipeline_hooks)

    def test_run_inference_uses_dir_plus_file_pattern(self, cbd):
        """`run-inference` declares NO short flags and its `--dir` is
        `file_okay=False`, so `-d samples/lending_package.pdf` is rejected twice
        over: unknown option, and a file where a directory is required."""
        src = self._src(cbd)
        assert "--dir samples/" in src
        assert "--file-pattern lending_package.pdf" in src
        assert "-d samples/" not in src, "run-inference has no -d short flag"

    def test_scan_is_scoped_to_one_identified_execution(self, cbd):
        """Step 14 shares the state machine with the other parallel steps, whose
        hook-less documents ALSO emit `{hookPoint: ..., invoked: 0}` at both
        points — PreprocessingHook is StartAt and PostprocessingHook is on the
        shared tail, so EVERY execution emits both. Collecting payloads across
        executions can therefore latch onto a foreign one. The scan must first
        identify our execution, then read only that one's history."""
        src = self._src(cbd)
        assert "target_arn" in src, (
            "the step must resolve a single target execution before reading history"
        )
        assert 'executionArn": target_arn' in src or "executionArn\": target_arn" in src or (
            "target_arn" in src and "get_execution_history" in src
        )

    def test_target_is_resolved_before_history_is_read(self, cbd):
        """Ordering matters: resolving the target first is what makes the
        collected payloads unambiguously ours. (The matching itself now lives in
        _find_target_execution — see TestExecutionWaitBarrier.)"""
        src = self._src(cbd)
        resolve_at = src.index("_find_target_execution(sfn")
        # Match the CALL, not the explanatory comment that names the old
        # reverseOrder form above it.
        history_at = src.index("sfn.get_execution_history(**hkw)")
        assert resolve_at < history_at

    def test_feature_id_is_shared_between_tag_and_config(self, cbd):
        """The Lambda's idp:feature-id tag and the config section's featureId must
        come from the same constant; drift would leave the ABAC grant and the
        registered owner disagreeing."""
        src = self._src(cbd)
        assert '"featureId": _HOOK_FEATURE_ID' in src


@pytest.mark.unit
class TestLambdaRuntimeAlignment:
    """pydantic_core is a COMPILED extension, so the zip and the Lambda runtime
    must agree on the Python minor version or the hook dies at cold start."""

    def test_runtime_matches_the_building_interpreter(self, cbd):
        import sys

        assert cbd._hook_lambda_runtime() == f"python3.{sys.version_info.minor}"

    def test_runtime_is_a_supported_lambda_value(self, cbd):
        assert cbd._hook_lambda_runtime() in {
            f"python3.{m}" for m in range(9, 14)
        }

    def test_create_function_does_not_hardcode_a_runtime(self, cbd):
        import inspect

        src = inspect.getsource(cbd.test_step14_pipeline_hooks)
        assert "Runtime=_hook_lambda_runtime()" in src
        assert 'Runtime="python3.12"' not in src, (
            "a hardcoded runtime silently breaks when the buildspec python moves"
        )


@pytest.mark.unit
class TestWorkingBucketWiring:
    """The hook cannot read the document without WORKING_BUCKET.

    At `postprocessing` the dispatcher always hands over a COMPRESSED document
    reference, and `idp_common.hooks.load_hook_document` raises outright
    ("carries a compressed document reference but no working bucket was given")
    unless it is set. A hook that raises still counts as `invoked`, so this would
    have surfaced as the confusing "hook ran but recorded no documentUpdatedBy"
    rather than as a missing-config error.
    """

    def test_hook_source_requires_working_bucket_explicitly(self, cbd):
        assert "WORKING_BUCKET" in cbd._HOOK_SOURCE
        assert "working_bucket=working_bucket" in cbd._HOOK_SOURCE, (
            "pass it explicitly rather than relying on the env var lookup inside "
            "idp_common, so a missing value fails with our own message"
        )

    def test_hook_fails_loudly_without_working_bucket(self, cbd, monkeypatch):
        """Fail with a clear message rather than a deep idp_common traceback."""
        monkeypatch.setenv("MARKER_KEY", cbd._HOOK_MARKER_KEY)
        monkeypatch.delenv("WORKING_BUCKET", raising=False)
        ns = {}
        exec(  # noqa: S102 — our own shipped source under test  # nosec B102 - executes this repo's own shipped source under test
            compile(cbd._HOOK_SOURCE, "index.py", "exec"), ns
        )
        with pytest.raises(RuntimeError, match="WORKING_BUCKET"):
            ns["lambda_handler"]({"hookPoint": "postprocessing", "document": {"id": "a"}}, None)

    def test_step_resolves_and_verifies_the_bucket(self, cbd):
        import inspect

        src = inspect.getsource(cbd.test_step14_pipeline_hooks)
        assert "_resolve_working_bucket(stack_name)" in src
        # Set unconditionally: the reuse path skips create_function's Environment.
        assert "update_function_configuration" in src
        assert 'env_now.get("WORKING_BUCKET") != working_bucket' in src, (
            "verify the env var actually landed, don't assume"
        )

    def test_lambda_updates_are_serialised(self, cbd):
        """Lambda rejects a mutating call while another is in flight, so the
        create -> tag -> configure sequence must wait between steps."""
        import inspect

        src = inspect.getsource(cbd.test_step14_pipeline_hooks)
        assert src.count("_wait_lambda_ready(") >= 2

    def test_working_bucket_resolver_targets_the_right_resource(self, cbd):
        import inspect

        src = inspect.getsource(cbd._resolve_working_bucket)
        assert 'LogicalResourceId") == "WorkingBucket"' in src
        assert "AWS::S3::Bucket" in src


@pytest.mark.unit
class TestFailureDiagnostics:
    """A pipeline round-trip is ~70 minutes, so a failure must carry the hook's
    own traceback out with it rather than forcing another cycle to learn one
    fact."""

    def test_logs_are_dumped_on_failure_only(self, cbd):
        import inspect

        src = inspect.getsource(cbd.test_step14_pipeline_hooks)
        assert '_dump_hook_logs(fn_name)' in src
        assert 'if not outcome["ok"] and created_fn:' in src, (
            "dump on failure only — a passing run should stay quiet"
        )

    def test_success_path_marks_the_outcome(self, cbd):
        import inspect

        src = inspect.getsource(cbd.test_step14_pipeline_hooks)
        assert 'outcome["ok"] = True' in src

    def test_log_dump_never_raises(self, cbd):
        """Diagnostics must not mask the real failure."""
        import inspect

        src = inspect.getsource(cbd._dump_hook_logs)
        assert "except Exception" in src

    def test_log_dump_reports_a_missing_log_group_meaningfully(self, cbd):
        """No log group means the hook was never invoked, which is the answer."""
        import inspect

        src = inspect.getsource(cbd._dump_hook_logs)
        assert "never invoked" in src


@pytest.mark.unit
class TestHookZipDependencyClosure:
    """PyYAML was missing from the zip, and no offline test caught it.

    The failure only appears at Lambda cold start:
    `load_hook_document -> Document.decompress -> idp_common.utils ->
    idp_common.config.models -> configuration_manager` imports yaml, so the hook
    died with `ModuleNotFoundError: No module named 'yaml'` — found by deploying
    the zip to a real Lambda and invoking it against a real compressed document.
    """

    def test_yaml_is_vendored(self, cbd, tmp_path):
        pytest.importorskip("idp_common")
        pytest.importorskip("yaml")
        path = cbd._build_hook_zip(str(tmp_path / "hook.zip"))
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
        assert any(n.startswith("yaml/") for n in names), (
            "PyYAML is on the load_hook_document import path"
        )

    def test_self_check_covers_every_required_package(self, cbd, tmp_path, monkeypatch):
        """The original self-check only looked for pydantic, which is how the yaml
        omission shipped. Dropping ANY required package must now raise."""
        pytest.importorskip("idp_common")
        import importlib

        real = importlib.import_module

        def fake(name, *a, **k):
            if name == "yaml":
                raise ImportError("simulated missing yaml")
            return real(name, *a, **k)

        monkeypatch.setattr(importlib, "import_module", fake)
        with pytest.raises(RuntimeError, match="yaml"):
            cbd._build_hook_zip(str(tmp_path / "hook.zip"))

    def test_runtime_libs_are_not_vendored(self, cbd, tmp_path):
        """boto3/botocore come from the Lambda runtime; vendoring them would push
        the zip past the 50MB direct-upload limit."""
        pytest.importorskip("idp_common")
        path = cbd._build_hook_zip(str(tmp_path / "hook.zip"))
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
        assert not any(n.startswith("botocore/") for n in names)


@pytest.mark.unit
class TestExecutionWaitBarrier:
    """`--monitor` is not a dependable barrier.

    In the pipeline it aborted after 21s with "Monitoring error: 1 validation
    error for DocumentStatus" (a runtime status missing from the SDK's
    DocumentState enum) and still exited 0, so the step scanned for the execution
    while the document was still QUEUED and reported a false
    "No SUCCEEDED execution found". The step must wait on the thing it needs, not
    on the CLI's exit code.
    """

    def test_step_polls_for_its_execution_with_a_deadline(self, cbd):
        import inspect

        src = inspect.getsource(cbd.test_step14_pipeline_hooks)
        assert "_TARGET_WAIT_SECS" in src
        assert "_find_target_execution(sfn" in src
        assert "time.sleep(_TARGET_POLL_SECS)" in src

    def test_wait_budget_exceeds_typical_processing_time(self, cbd):
        """The document takes ~80-120s; the budget must leave real headroom."""
        assert cbd._TARGET_WAIT_SECS >= 300
        assert 5 <= cbd._TARGET_POLL_SECS <= 60

    def test_finder_matches_on_the_input_config_version(self, cbd):
        import inspect

        src = inspect.getsource(cbd._find_target_execution)
        assert 'doc_in.get("config_version") == config_version' in src
        assert "describe_execution" in src

    def test_finder_returns_scanned_count_for_diagnostics(self, cbd):
        """The count is what made the previous failure diagnosable."""
        import inspect

        src = inspect.getsource(cbd._find_target_execution)
        assert "return None, scanned" in src

    def test_finder_passes_the_status_filter_through(self, cbd):
        """The onError:fail phase looks for a FAILED execution, so the status is
        a parameter rather than a hardcoded SUCCEEDED."""
        seen = {}

        class _Sfn:
            def list_executions(self, **kwargs):
                seen.update(kwargs)
                return {"executions": []}

        cbd._find_target_execution(_Sfn(), "arn:sm", "v1", status_filter="FAILED")
        assert seen["statusFilter"] == "FAILED"
        cbd._find_target_execution(_Sfn(), "arn:sm", "v1")
        assert seen["statusFilter"] == "SUCCEEDED", "default must not change"


@pytest.mark.unit
class TestOnErrorFailPhase:
    """Step 14's second phase: `onError: fail` must ABORT the document (#919).

    The policy is the documented way for a hook to GATE the pipeline, but each
    post-step hook state caught `States.ALL` and routed FORWARD — and States.ALL
    matches the dispatcher's fail-policy error too, so the document continued as
    though the hook had succeeded. Phase 1 registers both hooks with
    `onError: continue` deliberately (a hook fault must not make a coverage test
    flaky), which is exactly why nothing exercised the fail policy live.

    The phase as a whole needs a stack, but its DECISIONS do not. The config it
    registers, the history it reads and the two verdicts it reaches are extracted
    into `_onerror_fail_hook_config`, `_read_execution_history`,
    `_judge_onerror_fail_abort` and `_judge_onerror_fail_no_failed_execution`, so
    the tests below drive them directly with fake `sfn` responses and real
    dictionaries. Mutating any of those four functions fails a test here.

    Four tests remain source-level change-detectors, named `test_source_*` and
    marked as such in their docstrings: they cover the AWS-shaped glue that has no
    offline seam (which status filter the poll asks boto3 for, whether the phase
    is wired into Step 14 at all, whether it copies the sample document, and
    whether it passes `check=False` to `run_command`). Read them as "this line has
    not silently changed", not as proof the behaviour is right.
    """

    def _src(self, cbd):
        import inspect

        return inspect.getsource(cbd._assert_onerror_fail_aborts)

    # ---- behavioural: the config the phase registers -------------------

    def test_registers_the_ci_hook_as_a_gate_at_a_post_step_point(self, cbd):
        """Drive the extracted builder: a `<step>.postHook` entry with the fail
        policy and the `fail=true` arg that makes the hook raise."""
        base = {"ocr": {"model": "whatever"}, "classification": {}}
        cfg = cbd._onerror_fail_hook_config(base, "arn:aws:lambda:::function:hook")
        hooks = cfg["ocr"]["postHook"]
        assert len(hooks) == 1
        hook = hooks[0]
        assert hook["onError"] == "fail", (
            "the phase must register the GATING policy; with any other value the "
            "execution SUCCEEDS and the phase proves nothing"
        )
        assert {"key": "fail", "value": "true"} in hook["args"], (
            "without the fail=true arg the hook succeeds and no gate is exercised"
        )
        assert hook["arn"] == "arn:aws:lambda:::function:hook"
        assert hook["featureId"] == cbd._HOOK_FEATURE_ID
        assert hook["allowDocumentUpdate"] is False

    def test_registration_is_at_a_post_step_point_not_a_flat_one(self, cbd):
        """`preprocessing`/`postprocessing` were already fail-closed before the
        fix, so registering there would exercise nothing."""
        cfg = cbd._onerror_fail_hook_config({}, "arn:hook")
        assert "postHook" in cfg.get("ocr", {})
        for flat in ("preprocessing", "postprocessing"):
            assert flat not in cfg, (
                f"the fail phase registered at the flat point {flat!r}; "
                f"PreprocessingHook routed States.ALL to a Fail state even before "
                f"#919 was fixed, so this would pass on the broken graph too"
            )

    def test_registration_does_not_mutate_the_callers_config(self, cbd):
        """Phase 1's config is still live on the stack and the caller reuses the
        same dict; mutating it in place would corrupt the earlier assertions."""
        base = {"ocr": {"model": "keepme"}}
        snapshot = json.dumps(base, sort_keys=True)
        cbd._onerror_fail_hook_config(base, "arn:hook")
        assert json.dumps(base, sort_keys=True) == snapshot

    # ---- behavioural: reading the execution history --------------------

    @staticmethod
    def _fake_sfn(pages):
        """A minimal stepfunctions client returning canned history pages."""

        class _Sfn:
            def __init__(self):
                self.calls = []

            def get_execution_history(self, **kwargs):
                self.calls.append(kwargs)
                return pages[len(self.calls) - 1]

        return _Sfn()

    def test_history_reader_collects_states_and_the_failure(self, cbd):
        sfn = self._fake_sfn(
            [
                {
                    "events": [
                        {"stateEnteredEventDetails": {"name": "OCRStep"}},
                        {"stateEnteredEventDetails": {"name": "PostOcrHook"}},
                        {
                            "executionFailedEventDetails": {
                                "error": "HookFatalError",
                                "cause": "feature=x onError=fail",
                            }
                        },
                    ]
                }
            ]
        )
        entered, error, cause = cbd._read_execution_history(sfn, "arn:exec")
        assert entered == {"OCRStep", "PostOcrHook"}
        assert error == "HookFatalError"
        assert "onError=fail" in cause
        assert sfn.calls == [{"executionArn": "arn:exec", "maxResults": 1000}]

    def test_history_reader_follows_pagination(self, cbd):
        """A gated document's history is short, but a long one must not truncate
        before the ExecutionFailed event — which is always on the LAST page."""
        sfn = self._fake_sfn(
            [
                {
                    "events": [{"stateEnteredEventDetails": {"name": "OCRStep"}}],
                    "nextToken": "t1",
                },
                {
                    "events": [
                        {"stateEnteredEventDetails": {"name": "ClassificationStep"}},
                        {"executionFailedEventDetails": {"error": "Boom"}},
                    ]
                },
            ]
        )
        entered, error, _cause = cbd._read_execution_history(sfn, "arn:exec")
        assert entered == {"OCRStep", "ClassificationStep"}
        assert error == "Boom"
        assert sfn.calls[1].get("nextToken") == "t1"

    def test_history_reader_stops_at_the_page_bound(self, cbd):
        """The bound exists so a pathological history cannot hang the pipeline."""
        endless = [{"events": [], "nextToken": "more"}] * 50
        sfn = self._fake_sfn(endless)
        cbd._read_execution_history(sfn, "arn:exec", max_pages=3)
        assert len(sfn.calls) == 3

    # ---- behavioural: the verdicts -------------------------------------

    def test_verdict_passes_when_the_gate_held(self, cbd):
        assert (
            cbd._judge_onerror_fail_abort(
                {"OCRStep", "PostOcrHook"}, cbd._HOOK_FATAL_ERROR, ""
            )
            is None
        )

    def test_verdict_rejects_a_document_that_went_forward(self, cbd):
        """The class-closing assertion: a FAILED execution alone does not prove
        the gate held, because States.ALL could have routed forward and the
        execution failed later for another reason."""
        verdict = cbd._judge_onerror_fail_abort(
            {"OCRStep", "PostOcrHook", "ClassificationStep"},
            cbd._HOOK_FATAL_ERROR,
            "",
        )
        assert verdict and "ClassificationStep" in verdict

    def test_verdict_rejects_a_different_error_name(self, cbd):
        """A catcher on a name that never surfaces reproduces #919 one level up."""
        verdict = cbd._judge_onerror_fail_abort(
            {"OCRStep"}, "States.Timeout", "lambda timed out"
        )
        assert verdict and "States.Timeout" in verdict
        assert cbd._HOOK_FATAL_ERROR in verdict

    def test_verdict_truncates_a_long_cause(self, cbd):
        verdict = cbd._judge_onerror_fail_abort({}, "Other", "x" * 5000)
        assert verdict and len(verdict) < 1000

    def test_missing_execution_verdict_distinguishes_fail_open(self, cbd):
        """A SUCCEEDED execution pinned to the fail config version IS #919; no
        execution at all means the policy was never exercised. Collapsing the two
        sends the next reader to the wrong place."""
        ignored = cbd._judge_onerror_fail_no_failed_execution(
            "arn:aws:states:us-east-1:1:execution:sm:abc123"
        )
        assert "IGNORED" in ignored and "#919" in ignored
        assert "abc123" in ignored, "the operator needs the execution name"

        never_started = cbd._judge_onerror_fail_no_failed_execution(None)
        assert "never have started" in never_started
        assert "IGNORED" not in never_started
        assert str(cbd._TARGET_WAIT_SECS) in never_started

    def test_fatal_error_name_matches_the_dispatcher_exception(self, cbd):
        """The name is load-bearing in three places: the exception class the
        dispatcher raises, the `ErrorEquals` in workflow.asl.json, and this
        step's assertion. Read the class from the Lambda source so a rename
        cannot leave this constant behind (which would make the phase assert a
        name that never surfaces — #919 one level up)."""
        import importlib.util

        repo_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "..")
        )
        hook_errors_path = os.path.join(
            repo_root,
            "patterns",
            "unified",
            "src",
            "pipeline_hooks_function",
            "hook_errors.py",
        )
        spec = importlib.util.spec_from_file_location("hook_errors", hook_errors_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert cbd._HOOK_FATAL_ERROR == module.HookFatalError.__name__

    # ---- source-level change-detectors: NOT proof of behaviour ---------
    #
    # Each of the four below reads `inspect.getsource`. They cannot fail for a
    # behavioural reason, only because a line moved, and they cannot pass for a
    # behavioural reason either. They exist because the code they cover is a
    # boto3 argument or a call site with no offline seam. Treat a failure here as
    # "check this deliberately", not "the gate broke".

    def test_source_polls_for_a_failed_execution(self, cbd):
        """CHANGE-DETECTOR, not proof. The status filter is an argument to
        `list_executions` via `_find_target_execution`; driving it offline would
        only re-test `_find_target_execution`, which
        `TestExecutionTargeting::test_finder_passes_the_status_filter_through`
        already covers. What is asserted here is that THIS phase asks for FAILED
        first and then SUCCEEDED — the second query is what turns a miss into the
        fail-open diagnosis proved by
        `test_missing_execution_verdict_distinguishes_fail_open`."""
        src = self._src(cbd)
        assert 'status_filter="FAILED"' in src
        assert 'status_filter="SUCCEEDED"' in src

    def test_source_wires_the_phase_into_step14(self, cbd):
        """CHANGE-DETECTOR, not proof. Step 14 needs a live stack, so the only
        offline way to see that phase 2 runs at all — and that its verdict FAILS
        the step rather than being logged and dropped — is to read the call
        site."""
        import inspect

        src = inspect.getsource(cbd.test_step14_pipeline_hooks)
        assert "_assert_onerror_fail_aborts(" in src
        assert 'return {"success": False, "error": fail_err}' in src

    def test_source_uses_its_own_config_version_and_document_copy(self, cbd):
        """Partly a real assertion, partly a CHANGE-DETECTOR. The config-version
        inequality is behavioural: sharing phase 1's version would overwrite the
        live config the other parallel steps read. The `mkdtemp`/`hookfail-`
        substrings are a change-detector for the uniquely named document copy,
        which cannot be observed without a filesystem and a stack."""
        assert cbd._HOOK_FAIL_CONFIG_VERSION != "test-pipeline-hooks"
        src = self._src(cbd)
        assert "_HOOK_FAIL_CONFIG_VERSION" in src
        assert "mkdtemp" in src and "hookfail-" in src

    def test_source_does_not_gate_on_run_inference_exit_code(self, cbd):
        """CHANGE-DETECTOR, not proof. The document is MEANT to fail, so
        `run-inference --monitor` may exit non-zero; `check=True` there would
        abort the step before any verdict ran. Proving that behaviourally means
        faking `run_command`, which would test the fake."""
        src = self._src(cbd)
        run_at = src.index("run-inference")
        assert "check=False" in src[run_at:]

    def test_hook_source_fails_on_demand(self, cbd, monkeypatch):
        """The `fail=true` arg is what makes the hook fail, and it must raise
        before touching S3 so the failure is unambiguous."""
        monkeypatch.setenv("MARKER_KEY", cbd._HOOK_MARKER_KEY)
        monkeypatch.setenv("WORKING_BUCKET", "test-working-bucket")
        ns = {}
        exec(  # noqa: S102 — our own shipped source under test  # nosec B102 - executes this repo's own shipped source under test
            compile(cbd._HOOK_SOURCE, "index.py", "exec"), ns
        )
        event = {
            "hookPoint": "postOcr",
            "args": [{"key": "fail", "value": "true"}],
            # A compressed reference the handler could not resolve offline: if it
            # reached load_hook_document this would raise something else.
            "document": {"compressed": True, "s3_uri": "s3://nope/x.json"},
        }
        with pytest.raises(RuntimeError, match="deliberate failure"):
            ns["lambda_handler"](event, None)

    def test_hook_source_does_not_fail_without_the_arg(self, cbd, monkeypatch):
        """Guard the other side: phase 1 (and every real hook) must be unaffected
        by the failure switch."""
        monkeypatch.setenv("MARKER_KEY", cbd._HOOK_MARKER_KEY)
        monkeypatch.setenv("WORKING_BUCKET", "test-working-bucket")
        ns = {}
        exec(  # noqa: S102 — our own shipped source under test  # nosec B102 - executes this repo's own shipped source under test
            compile(cbd._HOOK_SOURCE, "index.py", "exec"), ns
        )
        out = ns["lambda_handler"](
            {
                "hookPoint": "postprocessing",
                "args": [{"key": "note", "value": "ci"}, {"key": "fail", "value": ""}],
                "document": {"id": "w2.pdf", "num_pages": 1, "sections": []},
            },
            None,
        )
        assert out["ciHookRan"] is True
