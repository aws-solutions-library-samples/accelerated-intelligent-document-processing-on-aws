# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for `IDPPublisher.run` and the public-access endgame of a publish.

Two things are covered here, and they meet at the end of a publish.

**`run` is the orchestrator.** It is ~340 lines of straight-line phase calls with
a handful of decisions embedded in it: whether linting gates the rest, whether
the Lambda layers are built or reused, whether the config library is uploaded at
all, and what happens when a category of builds fails. Those decisions are what
the tests below measure. They are written by replacing each phase method on the
instance with a recorder, so the assertion is on **which phases ran, in what
order, and with what arguments** — not on the internals of any phase, which have
their own tests. There is deliberately no attempt to cover every line of `run`:
the timing arithmetic and the log formatting at the end carry no decision.

**The public-access endgame.** `set_public_acls`, `_set_optional_key_public` and
`verify_public_readability` decide whether the artifacts a release advertises can
actually be fetched by the anonymous callers that fetch them — the CloudFormation
console pulling a template, and every deployed Web UI polling the version
pointer. Two shipped defects (#962, #963) were of exactly this shape: a key the
publish advertised was never made readable, the publish reported success, and the
consumer failed silently much later. So these tests use `moto` and **read the ACL
back out of the fake bucket**, rather than asserting that a call was made: a
`MagicMock` accepts an ACL applied to the wrong key just as happily as the right
one, which is the mistake that produced the original defects.

One test pins a defect rather than intended behaviour; see its docstring.
"""

from __future__ import annotations

import io
import threading

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from moto import mock_aws
from rich.console import Console

from idp_sdk._core import publish as publish_mod
from idp_sdk._core.publish import IDPPublisher

_BUCKET = "artifacts-bucket"
_PREFIX = "idp"
_VERSION = "0.6.9"
_REGION = "us-east-1"
_ACCOUNT = "123456789012"


def _publisher(s3=None, public=False):
    pub = IDPPublisher(verbose=False)
    pub.bucket = _BUCKET
    pub.prefix = _PREFIX
    pub.version = _VERSION
    pub.prefix_and_version = f"{_PREFIX}/{_VERSION}"
    pub.main_template = "idp-main.yaml"
    pub.region = _REGION
    pub.public = public
    pub.s3_client = s3
    pub.console = Console(file=io.StringIO(), width=1000, no_color=True)
    return pub


def _text(pub):
    return pub.console.file.getvalue()


def _acl_is_public(s3, bucket, key):
    grants = s3.get_object_acl(Bucket=bucket, Key=key)["Grants"]
    return any(
        g.get("Grantee", {}).get("URI", "").endswith("AllUsers")
        and g.get("Permission") == "READ"
        for g in grants
    )


# ---------------------------------------------------------------------------
# run — phase orchestration
# ---------------------------------------------------------------------------

# Every phase `run` calls that has no decision attached to it. Each is replaced
# by a recorder so the order they ran in can be asserted.
_SIMPLE_PHASES = (
    "check_parameters",
    "_prepare_for_build_at_start",
    "setup_environment",
    "check_prerequisites",
    "setup_artifacts_bucket",
    "upload_config_library",
    "package_multi_doc_discovery_source",
    "write_catalog_file",
    "_upload_catalog_to_artifacts",
    "generate_samples_manifest",
    "_upload_samples_manifest_to_artifacts",
    "upload_samples",
    "build_main_template",
    "update_component_checksum",
    "print_outputs",
    "print_error_summary",
    "build_and_package_template",
)

# Phase names that run on worker threads, so their position in the recorded
# sequence is not deterministic.
_CONCURRENT_PHASES = {"build_components_with_smart_detection"}


class _Harness:
    """A publisher with every phase of `run` replaced by a recorder.

    The recorded list is the object under test: `run`'s content is its ordering
    and its branches, so a harness that reports "what happened, in order, with
    which arguments" is the only thing that can measure it.
    """

    def __init__(
        self, monkeypatch, pub, rebuild=(), lib_changed=False, discovered=None
    ):
        self.pub = pub
        self.calls = []
        self._lock = threading.Lock()
        self.rebuild = list(rebuild)
        self.build_results = {"nested": True, "patterns": True}
        self.discovered = discovered if discovered is not None else {}
        self.linting_ok = True
        self.ui_future = None
        self.ui_executor = None

        pub.account_id = _ACCOUNT
        pub.max_workers = 3
        pub._is_lib_changed = lib_changed

        for name in _SIMPLE_PHASES:
            monkeypatch.setattr(pub, name, self._recorder(name))

        monkeypatch.setattr(pub, "_validate_python_linting", self._linting)
        monkeypatch.setattr(
            pub, "_validate_cfn_lint", self._recorder("_validate_cfn_lint", True)
        )
        monkeypatch.setattr(pub, "smart_rebuild_detection", self._smart_rebuild)
        monkeypatch.setattr(pub, "start_ui_validation_parallel", self._start_ui)
        monkeypatch.setattr(
            pub, "clear_component_cache", self._recorder("clear_component_cache")
        )
        monkeypatch.setattr(pub, "build_all_lambda_layers", self._build_layers)
        monkeypatch.setattr(pub, "_discover_existing_layer_zips", self._discover)
        monkeypatch.setattr(
            pub, "build_components_with_smart_detection", self._build_category
        )
        monkeypatch.setattr(
            pub, "package_ui", self._recorder("package_ui", "webui-1234.zip")
        )
        monkeypatch.setattr(
            pub,
            "package_unified_source",
            self._recorder("package_unified_source", "unified-source-abcd.zip"),
        )
        monkeypatch.setattr(
            pub,
            "build_and_upload_sample_features",
            self._recorder(
                "build_and_upload_sample_features", ("feathash", ["sf"], [])
            ),
        )

    def _record(self, name, args=None):
        with self._lock:
            self.calls.append((name, args))

    def _recorder(self, name, result=None):
        def phase(*args, **kwargs):
            self._record(name, args[0] if args else None)
            return result

        return phase

    def _linting(self):
        self._record("_validate_python_linting")
        return self.linting_ok

    def _smart_rebuild(self):
        self._record("smart_rebuild_detection")
        return self.rebuild

    def _start_ui(self):
        self._record("start_ui_validation_parallel")
        return self.ui_future, self.ui_executor

    def _build_layers(self):
        self._record("build_all_lambda_layers")
        return {
            name: {"zip_name": f"{name}.zip"}
            for name in ("base", "reporting", "agents", "multi_document_discovery")
        }

    def _discover(self):
        self._record("_discover_existing_layer_zips")
        return self.discovered

    def _build_category(self, components, category, max_workers):
        self._record("build_components_with_smart_detection", (category, max_workers))
        return self.build_results[category]

    @property
    def sequence(self):
        """Phase names in order, with the thread-scheduled ones removed."""
        return [name for name, _ in self.calls if name not in _CONCURRENT_PHASES]

    def args_for(self, name):
        return [args for phase, args in self.calls if phase == name]


def _args():
    return ["bucket-base", "idp", _REGION]


def test_run_executes_the_publish_phases_in_order(monkeypatch, tmp_path):
    """The happy path, with one component to rebuild and the layers reused.

    Order is load-bearing in several places and each is asserted by this one
    sequence: linting gates the S3 setup, rebuild detection precedes the cache
    clearing it drives, every packaging step precedes `build_main_template`
    (which substitutes their output filenames into template tokens),
    `_validate_cfn_lint` runs on the *packaged* templates so it must follow that
    build, and `update_component_checksum` runs only after cfn-lint passes — a
    checksum written before the last gate would mark a failed build as current.
    """
    monkeypatch.chdir(tmp_path)
    pub = _publisher()
    harness = _Harness(
        monkeypatch,
        pub,
        rebuild=[{"component": "patterns/unified"}],
        discovered={
            n: {} for n in ("base", "reporting", "agents", "multi_document_discovery")
        },
    )

    pub.run(_args())

    assert harness.sequence == [
        "check_parameters",
        "_prepare_for_build_at_start",
        "setup_environment",
        "check_prerequisites",
        "_validate_python_linting",
        "setup_artifacts_bucket",
        "smart_rebuild_detection",
        "start_ui_validation_parallel",
        "clear_component_cache",
        "_discover_existing_layer_zips",
        "upload_config_library",
        "package_ui",
        "package_unified_source",
        "package_multi_doc_discovery_source",
        "build_and_upload_sample_features",
        "write_catalog_file",
        "_upload_catalog_to_artifacts",
        "generate_samples_manifest",
        "_upload_samples_manifest_to_artifacts",
        "upload_samples",
        "build_main_template",
        "_validate_cfn_lint",
        "update_component_checksum",
        "print_outputs",
    ]
    assert "Done!" in _text(pub)
    assert "TOTAL TIME" in _text(pub)


def test_run_builds_both_component_categories_with_the_configured_worker_count(
    monkeypatch, tmp_path
):
    """`nested` and `patterns` are submitted concurrently, each with max_workers.

    They run on threads, so their order relative to each other is not asserted —
    only that both were requested, and that the worker count reaches them. A
    publisher that passed `None` would fall back to an unbounded pool.
    """
    monkeypatch.chdir(tmp_path)
    pub = _publisher()
    harness = _Harness(
        monkeypatch,
        pub,
        rebuild=[{"component": "nested/bedrockkb"}],
        discovered={
            n: {} for n in ("base", "reporting", "agents", "multi_document_discovery")
        },
    )

    pub.run(_args())

    assert set(harness.args_for("build_components_with_smart_detection")) == {
        ("nested", 3),
        ("patterns", 3),
    }


def test_run_auto_detects_the_worker_count_when_none_was_requested(
    monkeypatch, tmp_path
):
    """`--max-workers` unset means 2x CPU capped at 8, and the cap must hold.

    SAM builds are I/O bound, hence the 2x; the cap exists because each worker
    holds a Docker build slot. `os.cpu_count` is forced to a value above the cap
    so the cap itself is what the assertion sees.
    """
    monkeypatch.chdir(tmp_path)
    pub = _publisher()
    harness = _Harness(
        monkeypatch,
        pub,
        rebuild=[{"component": "nested/bedrockkb"}],
        discovered={
            n: {} for n in ("base", "reporting", "agents", "multi_document_discovery")
        },
    )
    pub.max_workers = None
    monkeypatch.setattr(publish_mod.os, "cpu_count", lambda: 64)

    pub.run(_args())

    assert pub.max_workers == 8
    assert set(harness.args_for("build_components_with_smart_detection")) == {
        ("nested", 8),
        ("patterns", 8),
    }
    assert "Auto-detected 8 concurrent workers (CPUs: 64)" in _text(pub)


def test_run_skips_the_config_library_upload_when_nothing_needs_rebuilding(
    monkeypatch, tmp_path
):
    """No rebuild means no config-library upload, and no cache clearing either.

    The config library is hundreds of small objects; uploading it on every
    no-op publish is the cost this branch avoids. `build_main_template` still
    runs — it has its own up-to-date path that re-uploads the template if the
    version prefix moved.
    """
    monkeypatch.chdir(tmp_path)
    pub = _publisher()
    harness = _Harness(
        monkeypatch,
        pub,
        rebuild=[],
        discovered={
            n: {} for n in ("base", "reporting", "agents", "multi_document_discovery")
        },
    )

    pub.run(_args())

    assert "upload_config_library" not in harness.sequence
    assert "clear_component_cache" not in harness.sequence
    assert "build_main_template" in harness.sequence


def test_run_never_clears_the_cache_for_the_lib_component(monkeypatch, tmp_path):
    """`lib` has no SAM build, so `clear_component_cache("lib")` would be wrong.

    It would resolve to removing `lib/.aws-sam`, which does not exist — harmless
    today, but the reason the guard is there is that `lib`'s artifacts are the
    layer zips under `.aws-sam/layers`, and a future widening of that helper
    would delete them.
    """
    monkeypatch.chdir(tmp_path)
    pub = _publisher()
    harness = _Harness(
        monkeypatch,
        pub,
        rebuild=[
            {"component": "lib"},
            {"component": "main"},
            {"component": "patterns/unified"},
        ],
        lib_changed=True,
    )

    pub.run(_args())

    assert harness.args_for("clear_component_cache") == ["main", "patterns/unified"]


def test_run_builds_the_layers_when_the_shared_library_changed(monkeypatch, tmp_path):
    """A changed `idp_common` must produce new layer zips, not reuse discovery.

    This is the branch `_is_lib_changed` exists for. Taking the discovery path
    instead would publish a stack whose Lambdas attach a layer built from the
    previous library source — the exact stale-artifact failure the checksum
    machinery is there to prevent.
    """
    monkeypatch.chdir(tmp_path)
    pub = _publisher()
    harness = _Harness(
        monkeypatch, pub, rebuild=[{"component": "lib"}], lib_changed=True
    )

    pub.run(_args())

    assert "build_all_lambda_layers" in harness.sequence
    assert "_discover_existing_layer_zips" not in harness.sequence
    assert set(pub._layer_arns) == {
        "base",
        "reporting",
        "agents",
        "multi_document_discovery",
    }


def test_run_falls_back_to_a_layer_build_when_discovery_finds_too_few(
    monkeypatch, tmp_path
):
    """Fewer than three discovered layers forces a rebuild.

    Partial discovery is the dangerous state: the template needs a zip name for
    every layer token, and a missing one would be substituted with the hardcoded
    fallback name (`idp-common-base.zip`) that no publish ever uploads.
    """
    monkeypatch.chdir(tmp_path)
    pub = _publisher()
    harness = _Harness(
        monkeypatch,
        pub,
        rebuild=[{"component": "main"}],
        discovered={"base": {}, "reporting": {}},
    )

    pub.run(_args())

    assert harness.sequence.count("_discover_existing_layer_zips") == 1
    assert "build_all_lambda_layers" in harness.sequence
    assert harness.sequence.index(
        "_discover_existing_layer_zips"
    ) < harness.sequence.index("build_all_lambda_layers")
    assert "Layer discovery incomplete" in _text(pub)


def test_run_stops_at_a_failed_lint_before_touching_s3(monkeypatch, tmp_path):
    """Linting gates everything after it, including the bucket setup.

    The point of running the gate this early is that nothing has been uploaded
    yet, so a failure leaves the target bucket exactly as it was. A gate placed
    after `setup_artifacts_bucket` would create a bucket for a publish that never
    happens.
    """
    monkeypatch.chdir(tmp_path)
    pub = _publisher()
    harness = _Harness(monkeypatch, pub)
    harness.linting_ok = False

    with pytest.raises(SystemExit) as excinfo:
        pub.run(_args())

    assert excinfo.value.code == 1
    assert harness.sequence == [
        "check_parameters",
        "_prepare_for_build_at_start",
        "setup_environment",
        "check_prerequisites",
        "_validate_python_linting",
    ]
    assert "Python linting validation failed" in _text(pub)


def test_run_reports_a_prerequisite_failure_with_a_traceback_and_exits(
    monkeypatch, tmp_path
):
    """A raising phase is caught by the outermost handler, printed, and exits 1.

    The traceback is printed because the phases raise for a wide range of
    reasons — a missing `sam`, an unreachable bucket, a bad parameter — and the
    message alone is frequently not enough to tell them apart.
    """
    monkeypatch.chdir(tmp_path)
    pub = _publisher()
    harness = _Harness(monkeypatch, pub)

    def failing():
        harness._record("check_prerequisites")
        raise Exception("SAM CLI is not installed")

    monkeypatch.setattr(pub, "check_prerequisites", failing)

    with pytest.raises(SystemExit) as excinfo:
        pub.run(_args())

    assert excinfo.value.code == 1
    assert harness.sequence[-1] == "check_prerequisites"
    text = _text(pub)
    assert "SAM CLI is not installed" in text
    assert "Traceback" in text


def test_run_exits_on_a_nested_stack_build_failure_and_summarises_the_errors(
    monkeypatch, tmp_path
):
    """A failed category stops the publish before anything is packaged or uploaded.

    `print_error_summary` is the only place the per-component build output is
    shown in a non-verbose run, so its absence would leave the operator with
    "Failed to build one or more nested stacks" and nothing else.
    """
    monkeypatch.chdir(tmp_path)
    pub = _publisher()
    harness = _Harness(
        monkeypatch, pub, rebuild=[{"component": "nested/bedrockkb"}], lib_changed=True
    )
    harness.build_results["nested"] = False

    with pytest.raises(SystemExit) as excinfo:
        pub.run(_args())

    assert excinfo.value.code == 1
    assert "print_error_summary" in harness.sequence
    assert "package_ui" not in harness.sequence
    assert "update_component_checksum" not in harness.sequence
    text = _text(pub)
    assert "Failed to build one or more nested stacks" in text
    assert "Use --verbose flag for detailed error information" in text


def test_run_exits_on_a_pattern_build_failure(monkeypatch, tmp_path):
    """The patterns category is checked separately, with its own message."""
    monkeypatch.chdir(tmp_path)
    pub = _publisher()
    harness = _Harness(
        monkeypatch, pub, rebuild=[{"component": "patterns/unified"}], lib_changed=True
    )
    harness.build_results["patterns"] = False

    with pytest.raises(SystemExit):
        pub.run(_args())

    assert "Failed to build one or more patterns" in _text(pub)
    assert "update_component_checksum" not in harness.sequence


def test_run_exits_when_the_parallel_ui_validation_fails_and_shuts_its_pool_down(
    monkeypatch, tmp_path
):
    """The UI validation runs on its own executor, so it must be shut down either way.

    The failure arrives late — the future is only resolved after the component
    builds — and the `finally` that shuts the pool down is what stops the
    interpreter hanging on a non-daemon worker thread after `sys.exit`.
    """
    monkeypatch.chdir(tmp_path)
    pub = _publisher()
    harness = _Harness(
        monkeypatch, pub, rebuild=[{"component": "main"}], lib_changed=True
    )

    shutdowns = []

    class _Future:
        def result(self):
            raise Exception("npm run lint found 3 problems")

    class _Executor:
        def shutdown(self, wait=True):
            shutdowns.append(wait)

    harness.ui_future = _Future()
    harness.ui_executor = _Executor()

    with pytest.raises(SystemExit) as excinfo:
        pub.run(_args())

    assert excinfo.value.code == 1
    assert shutdowns == [True]
    text = _text(pub)
    assert "UI validation failed" in text
    assert "npm run lint found 3 problems" in text
    assert "package_ui" not in harness.sequence


def test_run_waits_for_a_successful_ui_validation_before_packaging_the_ui(
    monkeypatch, tmp_path
):
    """Packaging the UI before its validation resolved would ship an unlinted build."""
    monkeypatch.chdir(tmp_path)
    pub = _publisher()
    harness = _Harness(
        monkeypatch, pub, rebuild=[{"component": "main"}], lib_changed=True
    )

    resolved = []

    class _Future:
        def result(self):
            resolved.append("ui")
            return True

    class _Executor:
        def shutdown(self, wait=True):
            resolved.append("shutdown")

    harness.ui_future = _Future()
    harness.ui_executor = _Executor()

    pub.run(_args())

    assert resolved == ["ui", "shutdown"]
    assert "UI validation completed successfully" in _text(pub)
    assert "package_ui" in harness.sequence


def test_run_builds_the_feature_platform_stack_only_when_its_directory_exists(
    monkeypatch, tmp_path
):
    """That nested stack carries its own parameters, so it is built outside the pool.

    It is also built unconditionally (`force_rebuild=True`) because the main
    template references its packaged URL whether or not the feature platform is
    enabled — an absent packaged template makes the main stack fail to create.
    A trimmed checkout without the directory must not fail.
    """
    monkeypatch.chdir(tmp_path)

    pub = _publisher()
    harness = _Harness(
        monkeypatch, pub, rebuild=[{"component": "main"}], lib_changed=True
    )
    pub.run(_args())
    assert "build_and_package_template" not in harness.sequence

    (tmp_path / "feature-platform" / "main-stack-extensions").mkdir(parents=True)
    pub = _publisher()
    harness = _Harness(
        monkeypatch, pub, rebuild=[{"component": "main"}], lib_changed=True
    )
    pub.run(_args())
    assert harness.args_for("build_and_package_template") == [
        "feature-platform/main-stack-extensions"
    ]


def test_run_passes_the_sample_feature_results_into_the_main_template_build(
    monkeypatch, tmp_path
):
    """The bundled-feature hash re-triggers the deploy-time PublishSampleFeature.

    If the hash did not reach `build_main_template`, CloudFormation would see no
    change to that custom resource on an update and the feature bucket would
    keep the previous feature source.
    """
    monkeypatch.chdir(tmp_path)
    pub = _publisher()
    recorded = {}

    harness = _Harness(
        monkeypatch, pub, rebuild=[{"component": "main"}], lib_changed=True
    )

    def build_main(webui, unified, components, **kwargs):
        recorded["webui"] = webui
        recorded["unified"] = unified
        recorded["components"] = components
        recorded.update(kwargs)
        harness._record("build_main_template")

    monkeypatch.setattr(pub, "build_main_template", build_main)
    pub.run(_args())

    assert recorded["webui"] == "webui-1234.zip"
    assert recorded["unified"] == "unified-source-abcd.zip"
    assert recorded["components"] == [{"component": "main"}]
    assert recorded["sample_features_hash"] == "feathash"
    assert recorded["sample_features_list"] == ["sf"]


def test_run_exits_when_cfn_lint_finds_errors_without_writing_checksums(
    monkeypatch, tmp_path
):
    """The last gate is still a gate: a bad template must not be recorded as built.

    `update_component_checksum` after a cfn-lint failure would mark every
    component current, so the next publish would skip the rebuild and leave the
    invalid packaged template in place.
    """
    monkeypatch.chdir(tmp_path)
    pub = _publisher()
    harness = _Harness(
        monkeypatch, pub, rebuild=[{"component": "main"}], lib_changed=True
    )
    monkeypatch.setattr(pub, "_validate_cfn_lint", lambda: False)

    with pytest.raises(SystemExit) as excinfo:
        pub.run(_args())

    assert excinfo.value.code == 1
    assert "update_component_checksum" not in harness.sequence
    assert "print_outputs" not in harness.sequence
    assert "CloudFormation linting validation failed" in _text(pub)


def test_run_reports_a_user_interrupt_distinctly_from_a_failure(monkeypatch, tmp_path):
    """Ctrl-C is not an error, and must not print a traceback.

    A traceback on a deliberate cancellation is how an operator ends up filing a
    bug for their own keystroke.
    """
    monkeypatch.chdir(tmp_path)
    pub = _publisher()
    _Harness(monkeypatch, pub)

    def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(pub, "setup_environment", interrupted)

    with pytest.raises(SystemExit) as excinfo:
        pub.run(_args())

    assert excinfo.value.code == 1
    text = _text(pub)
    assert "Operation cancelled by user" in text
    assert "Traceback" not in text


@mock_aws
def test_run_resolves_the_account_id_from_sts_when_it_is_not_already_known(
    monkeypatch, tmp_path, aws_credentials
):
    """The account id is needed for the ECR image placeholder, so it is resolved once.

    It is looked up only when unset, because the operations layer often already
    knows it. A real (fake-AWS) STS call is used so the shape of the response
    key (`Account`) is part of what the test pins.
    """
    monkeypatch.chdir(tmp_path)
    pub = _publisher()
    harness = _Harness(monkeypatch, pub, rebuild=[], lib_changed=True)
    pub.account_id = None
    pub.sts_client = None

    pub.run(_args())

    assert pub.account_id == _ACCOUNT
    assert pub.sts_client is not None
    assert "smart_rebuild_detection" in harness.sequence


# ---------------------------------------------------------------------------
# print_outputs
# ---------------------------------------------------------------------------


def test_print_outputs_advertises_the_template_url_and_a_one_click_launch_url(
    monkeypatch, tmp_path
):
    """Both URLs are what an operator copies out of the log, so both are checked.

    The launch URL embeds the template URL as a query parameter and is the
    documented "Launch Stack" route; the template URL is what an existing stack
    is updated from. The unversioned `<prefix>/idp-main.yaml` key is used, not
    the versioned one, because that is the key the console pins to.
    """
    monkeypatch.chdir(tmp_path)
    pub = _publisher(public=False)
    pub.print_outputs()

    text = _text(pub)
    template_url = (
        f"https://s3.{_REGION}.amazonaws.com/{_BUCKET}/{_PREFIX}/idp-main.yaml"
    )
    assert template_url in text
    assert (
        f"https://{_REGION}.console.aws.amazon.com/cloudformation/home?region={_REGION}"
        f"#/stacks/create/review?templateURL={template_url}&stackName=IDP"
    ) in text

    assert f"Region: {_REGION}" in text
    assert f"Bucket: {_BUCKET}" in text
    assert f"Template Path: {_PREFIX}/idp-main.yaml" in text
    assert "Public Access: No" in text


@mock_aws
def test_print_outputs_applies_the_acls_before_verifying_readability(
    monkeypatch, tmp_path, aws_credentials
):
    """Verification has to follow the ACL pass, or it would always fail.

    The ordering is the whole point of doing both in one method: the check is
    meant to confirm the ACLs that were just applied, not to report the state
    before them.
    """
    monkeypatch.chdir(tmp_path)
    order = []
    pub = _publisher(public=True)
    monkeypatch.setattr(pub, "set_public_acls", lambda: order.append("acls"))
    monkeypatch.setattr(
        pub, "verify_public_readability", lambda: order.append("verify")
    )

    pub.print_outputs()

    assert order == ["acls", "verify"]
    assert "Public Access: Yes" in _text(pub)


# ---------------------------------------------------------------------------
# _set_optional_key_public
# ---------------------------------------------------------------------------


@mock_aws
def test_an_optional_key_that_exists_is_made_publicly_readable(
    monkeypatch, tmp_path, aws_credentials
):
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    key = f"{_PREFIX}/idp-main-latest.json"
    s3.put_object(Bucket=_BUCKET, Key=key, Body=b"{}")
    assert not _acl_is_public(s3, _BUCKET, key)

    _publisher(s3, public=True)._set_optional_key_public(key)

    assert _acl_is_public(s3, _BUCKET, key)


@mock_aws
def test_an_absent_optional_key_is_skipped_rather_than_failing_the_publish(
    monkeypatch, tmp_path, aws_credentials
):
    """The version pointer is best-effort, so its absence must not fail a release.

    The explicit-key loop in `set_public_acls` heads then ACLs and would raise;
    that behaviour is right for the main templates, whose absence means a broken
    publish, and wrong for the update indicator.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    pub = _publisher(s3, public=True)
    pub._set_optional_key_public(f"{_PREFIX}/idp-main-latest.json")

    assert "not present; skipping its ACL" in _text(pub)


@mock_aws
def test_an_s3_error_that_is_not_a_missing_key_propagates(
    monkeypatch, tmp_path, aws_credentials
):
    """Only absence is tolerated. AccessDenied or a missing bucket must be raised.

    Swallowing those would turn "the bucket rejects your ACLs" into a silent
    pass, which is how a key ends up advertised and unreadable.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    pub = _publisher(s3, public=True)

    with pytest.raises(ClientError) as excinfo:
        pub._set_optional_key_public(f"{_PREFIX}/idp-main-latest.json")
    assert excinfo.value.response["Error"]["Code"] not in (
        "404",
        "NoSuchKey",
        "NotFound",
    )


# ---------------------------------------------------------------------------
# set_public_acls
# ---------------------------------------------------------------------------


def _seed_public_publish(s3, extra_objects=()):
    """A published tree: versioned artifacts, extensions, templates, pointer."""
    s3.create_bucket(Bucket=_BUCKET)
    keys = [
        f"{_PREFIX}/{_VERSION}/config_library/pattern/config.yaml",
        f"{_PREFIX}/{_VERSION}/layers/idp-common-base-abcd1234.zip",
        f"{_PREFIX}/extensions/sample-feature/template.yaml",
        f"{_PREFIX}/idp-main.yaml",
        f"{_PREFIX}/idp-main_{_VERSION}.yaml",
        f"{_PREFIX}/idp-main-latest.json",
        *extra_objects,
    ]
    for key in keys:
        s3.put_object(Bucket=_BUCKET, Key=key, Body=b"artifact")
    return keys


@mock_aws
def test_set_public_acls_does_nothing_on_a_private_publish(
    monkeypatch, tmp_path, aws_credentials
):
    """Most publishes target a developer's own bucket and must stay private."""
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    keys = _seed_public_publish(s3)

    _publisher(s3, public=False).set_public_acls()

    assert not any(_acl_is_public(s3, _BUCKET, key) for key in keys)


@mock_aws
def test_set_public_acls_covers_the_versioned_tree_the_extensions_and_the_templates(
    monkeypatch, tmp_path, aws_credentials
):
    """Three prefixes, three different reasons, one pass — read back from S3.

    `<prefix>/<version>/` holds the code the stack fetches at deploy time.
    `<prefix>/extensions/` is version-free and a *sibling* of that tree, so the
    versioned pass alone never reaches it — without it a cross-account public
    deploy hits 403 the moment CloudFormation fetches an extension's template.
    The two main templates and the version pointer sit at `<prefix>/`, a
    *parent*, so neither paginated prefix reaches them either. All three had to
    be enumerated separately, and a regression in any one is invisible from the
    publish output.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    # A decoy under a sibling prefix that must not be touched: another release's
    # artifacts published into the same bucket.
    decoy = f"{_PREFIX}/0.5.16/layers/idp-common-base-old.zip"
    unrelated = "some-other-product/template.yaml"
    keys = _seed_public_publish(s3, extra_objects=(decoy, unrelated))

    pub = _publisher(s3, public=True)
    pub.set_public_acls()

    for key in keys:
        if key in (decoy, unrelated):
            continue
        assert _acl_is_public(s3, _BUCKET, key), key
    assert not _acl_is_public(s3, _BUCKET, decoy)
    assert not _acl_is_public(s3, _BUCKET, unrelated)
    assert "Public ACLs set successfully" in _text(pub)


@mock_aws
def test_set_public_acls_reports_progress_every_ten_objects(
    monkeypatch, tmp_path, aws_credentials
):
    """A real publish ACLs thousands of objects, so progress has to be visible."""
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    _seed_public_publish(s3)
    for i in range(20):
        s3.put_object(
            Bucket=_BUCKET, Key=f"{_PREFIX}/{_VERSION}/sam-objects/{i}", Body=b"x"
        )

    pub = _publisher(s3, public=True)
    pub.set_public_acls()

    text = _text(pub)
    assert "Setting ACLs on 23 files" in text
    assert "Progress: 10/23" in text
    assert "Progress: 23/23" in text


@mock_aws
def test_set_public_acls_tolerates_a_missing_version_pointer(
    monkeypatch, tmp_path, aws_credentials
):
    """A publish whose pointer write failed is still a usable release."""
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    for key in (
        f"{_PREFIX}/{_VERSION}/layers/base.zip",
        f"{_PREFIX}/idp-main.yaml",
        f"{_PREFIX}/idp-main_{_VERSION}.yaml",
    ):
        s3.put_object(Bucket=_BUCKET, Key=key, Body=b"artifact")

    pub = _publisher(s3, public=True)
    pub.set_public_acls()

    assert _acl_is_public(s3, _BUCKET, f"{_PREFIX}/idp-main.yaml")
    assert "not present; skipping its ACL" in _text(pub)
    assert "Public ACLs set successfully" in _text(pub)


@mock_aws
def test_a_missing_main_template_fails_the_acl_pass(
    monkeypatch, tmp_path, aws_credentials
):
    """The main templates are not optional: their absence is a broken publish.

    The versioned copy is the one that goes missing in practice — it is uploaded
    under a name derived from `VERSION`, so a mismatch between the version the
    upload used and the version this pass expects shows up exactly here.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    s3.put_object(
        Bucket=_BUCKET, Key=f"{_PREFIX}/{_VERSION}/layers/base.zip", Body=b"x"
    )
    s3.put_object(Bucket=_BUCKET, Key=f"{_PREFIX}/idp-main.yaml", Body=b"x")

    pub = _publisher(s3, public=True)
    with pytest.raises(Exception, match="Failed to set public ACLs"):
        pub.set_public_acls()


@mock_aws
def test_an_empty_versioned_prefix_returns_before_the_template_acls_are_applied(
    monkeypatch, tmp_path, aws_credentials
):
    """DEFECT: the "no objects" short-circuit skips the keys consumers actually fetch.

    `set_public_acls` (publish.py:4106-4108) paginates
    `<prefix>/<version>/` and `<prefix>/extensions/`, and returns early when both
    are empty. The three keys handled *after* that point — `<prefix>/idp-main.yaml`,
    `<prefix>/idp-main_<version>.yaml` and the version pointer — live at
    `<prefix>/`, a parent of both paginated prefixes, so the early return leaves
    all three with whatever ACL they were uploaded with.

    Observable consequence: a publish in which the versioned artifact tree is
    empty or unreadable silently produces a release whose main template is not
    anonymously readable, and prints "No objects found to set ACLs on" followed
    by no error. That is the same defect class as #962 and #963 — an advertised
    key left unreadable, with the publish reporting success — and it is the class
    `verify_public_readability` was added to catch. It would catch this one too,
    which is why this is a latent defect rather than an active one.

    The test pins the current behaviour: the templates exist and stay private.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    main_key = f"{_PREFIX}/idp-main.yaml"
    versioned_key = f"{_PREFIX}/idp-main_{_VERSION}.yaml"
    pointer_key = f"{_PREFIX}/idp-main-latest.json"
    for key in (main_key, versioned_key, pointer_key):
        s3.put_object(Bucket=_BUCKET, Key=key, Body=b"artifact")

    pub = _publisher(s3, public=True)
    pub.set_public_acls()

    assert "No objects found to set ACLs on" in _text(pub)
    assert not _acl_is_public(s3, _BUCKET, main_key)
    assert not _acl_is_public(s3, _BUCKET, versioned_key)
    assert not _acl_is_public(s3, _BUCKET, pointer_key)
    # And nothing in the output suggests anything was left undone.
    assert "Failed" not in _text(pub)


# ---------------------------------------------------------------------------
# verify_public_readability
# ---------------------------------------------------------------------------


class _UnreachableS3:
    """An anonymous client whose every call fails at the transport layer.

    Stands for the real conditions this branch exists for: a publishing shell
    behind a proxy, or one with no egress to the public S3 endpoint.
    """

    def head_object(self, **kwargs):
        raise EndpointConnectionError(endpoint_url="https://s3.amazonaws.com/")


class _Boto3Shim:
    def __init__(self, client):
        self._client = client
        self.calls = []

    def client(self, service, **kwargs):
        self.calls.append((service, kwargs))
        return self._client


def test_readability_is_not_checked_at_all_on_a_private_publish(monkeypatch, tmp_path):
    """No anonymous client is even constructed, so no egress is needed.

    Asserted through the client factory rather than the output, because the
    expensive part of this method is the network call and a private publish must
    not make one.
    """
    monkeypatch.chdir(tmp_path)
    shim = _Boto3Shim(_UnreachableS3())
    monkeypatch.setattr(publish_mod, "boto3", shim)

    _publisher(public=False).verify_public_readability()

    assert shim.calls == []


@mock_aws
def test_readability_passes_when_all_three_advertised_keys_answer(
    monkeypatch, tmp_path, aws_credentials
):
    """The three checked keys are exactly the ones the release advertises.

    The main template is what the console fetches, the versioned template is
    what the update indicator points at, and the pointer itself is what every
    deployed Web UI polls. A fourth key being checked, or one of these three
    being dropped, changes what "the release is usable" means.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    for key in (
        f"{_PREFIX}/idp-main.yaml",
        f"{_PREFIX}/idp-main_{_VERSION}.yaml",
        f"{_PREFIX}/idp-main-latest.json",
    ):
        s3.put_object(Bucket=_BUCKET, Key=key, Body=b"artifact", ACL="public-read")

    pub = _publisher(s3, public=True)
    pub.verify_public_readability()

    text = _text(pub)
    assert "All advertised keys are anonymously readable" in text
    assert "main template (Launch Stack)" in text
    assert "versioned main template" in text
    assert "version pointer (update indicator)" in text


@mock_aws
def test_an_unreadable_advertised_key_fails_the_publish_and_names_it(
    monkeypatch, tmp_path, aws_credentials
):
    """A 403/404 means the artifacts are up but the release must not be announced.

    This is the check that closes #962 and #963: both were cases where a key the
    run advertised was never made readable and the publish reported success. The
    exception has to name the key and the status, because the remedy depends on
    which — a 404 is a missing upload, a 403 is Object Ownership or a Public
    Access Block.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    s3.put_object(
        Bucket=_BUCKET, Key=f"{_PREFIX}/idp-main.yaml", Body=b"x", ACL="public-read"
    )
    # The versioned template and the pointer were never written.

    pub = _publisher(s3, public=True)
    with pytest.raises(Exception) as excinfo:
        pub.verify_public_readability()

    message = str(excinfo.value)
    assert "must not be announced" in message
    assert f"{_PREFIX}/idp-main_{_VERSION}.yaml" in message
    assert f"{_PREFIX}/idp-main-latest.json" in message
    # The key that *was* readable is not listed as a failure.
    assert f"{_PREFIX}/idp-main.yaml (main template" not in message
    assert "Object Ownership / Public Access" in message


def test_a_transport_failure_is_reported_as_unverified_rather_than_failed(
    monkeypatch, tmp_path
):
    """No egress says nothing about the object's ACL, so it must not fail the run.

    Failing here would red-line every publish from a network without public S3
    egress, for a condition that is not about the release at all. The distinction
    has to survive in the output, though — an operator reading "UNVERIFIED" needs
    to know to re-check from a credential-free shell before announcing.
    """
    monkeypatch.chdir(tmp_path)
    shim = _Boto3Shim(_UnreachableS3())
    monkeypatch.setattr(publish_mod, "boto3", shim)

    pub = _publisher(public=True)
    pub.verify_public_readability()  # must not raise

    text = _text(pub)
    assert "Anonymous readability UNVERIFIED for 3 key(s)" in text
    assert "could not check" in text
    assert "All advertised keys are anonymously readable" not in text


def test_the_readability_check_uses_an_unsigned_client_in_the_publish_region(
    monkeypatch, tmp_path
):
    """Credentials in the publishing shell must not be able to mask the answer.

    An ordinary client would be signed with the publisher's own credentials,
    which can read the object regardless of its ACL — so the check would pass on
    exactly the bucket configuration it exists to detect.
    """
    monkeypatch.chdir(tmp_path)
    shim = _Boto3Shim(_UnreachableS3())
    monkeypatch.setattr(publish_mod, "boto3", shim)

    _publisher(public=True).verify_public_readability()

    assert len(shim.calls) == 1
    service, kwargs = shim.calls[0]
    assert service == "s3"
    assert kwargs["region_name"] == _REGION
    assert kwargs["config"].signature_version is publish_mod.UNSIGNED
