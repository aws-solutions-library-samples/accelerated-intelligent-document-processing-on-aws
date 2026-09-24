# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ``idp_sdk.operations.publish``.

``PublishOperation`` is the SDK's build-and-upload namespace. ``build`` resolves a
region and an artifact bucket, turns its keyword arguments into the argument
*vector* that ``idp_sdk._core.publish.IDPPublisher`` parses, runs the publisher
from inside the source tree, and then — when asked — transforms the built
template into the headless (no-UI) and GovCloud (CloudFront-free) variants,
uploads each to S3 and validates it through the CloudFormation API.
``transform_template_headless`` and ``transform_template_govcloud`` are the two
transforms on their own, and ``print_deployment_urls`` prints the console
one-click links.

**What shaped these tests.** Most of ``build`` is argument marshalling whose only
consumer is a CLI-style argument parser, so the tests assert the *vector*
element by element rather than that the publisher was called: an omitted
``--no-validate`` or a ``--max-workers`` whose value landed as an ``int`` is
invisible to a ``MagicMock`` and fatal to ``argparse``. The S3 and
CloudFormation halves run against ``moto`` and the assertions read the uploaded
object back, because the key and the ``ContentType`` are what make the resulting
URL usable as a CloudFormation ``TemplateURL`` — a mock accepts any key. The
existing ``test_govcloud_lint_gate.py`` already covers the region-aware cfn-lint
gate inside ``transform_template_govcloud``; this file covers the rest of that
method and does not repeat it.

One defect is pinned rather than fixed: a failing headless transform is
swallowed and reported as a successful publish. See
``test_a_failed_headless_transform_is_swallowed_and_reported_as_success``.
"""

import os
from urllib.parse import quote

import boto3
import pytest
from moto import mock_aws

import idp_sdk.operations.publish as pub
from idp_sdk import IDPClient
from idp_sdk.exceptions import IDPConfigurationError, IDPStackError
from idp_sdk.models.publish import PublishResult, TemplateTransformResult

pytestmark = pytest.mark.unit

BUCKET_BASENAME = "idp-artifacts"
PREFIX = "idp-cli"
REGION = "us-east-1"
BUCKET_FULL = f"{BUCKET_BASENAME}-{REGION}"

#: A body ``cloudformation:ValidateTemplate`` rejects, for the two tests that need
#: the validation step to fail. Anything structurally valid passes.
NOT_A_TEMPLATE = "this is not a CloudFormation template"


def _create_bucket(name, region):
    """Create a bucket in ``region``.

    Every region except ``us-east-1`` requires an explicit
    ``LocationConstraint``, and ``us-east-1`` rejects one — so the two GovCloud
    regions used below cannot share the plain ``create_bucket`` call the rest of
    this file makes.
    """
    boto3.client("s3", region_name=region).create_bucket(
        Bucket=name, CreateBucketConfiguration={"LocationConstraint": region}
    )


@pytest.fixture
def source_tree(tmp_path):
    """A source directory holding the two files ``build`` reads: the SAM-built
    main template and ``VERSION``."""
    (tmp_path / ".aws-sam").mkdir()
    (tmp_path / ".aws-sam" / "idp-main.yaml").write_text(
        "AWSTemplateFormatVersion: '2010-09-09'\nResources:\n  Q:\n"
        "    Type: AWS::SQS::Queue\n"
    )
    (tmp_path / "VERSION").write_text("0.6.9\n")
    return tmp_path


class RecordingPublisher:
    """Stand-in for ``IDPPublisher`` that records how it was driven.

    ``build`` communicates with the publisher three ways — a constructor keyword,
    two attribute assignments and one positional argument vector — and all three
    are part of the contract, so all three are recorded.
    """

    instances: list = []

    def __init__(self, verbose=False):
        self.verbose = verbose
        self.headless = None
        self.govcloud = None
        self.args = None
        self.cwd_during_run = None
        RecordingPublisher.instances.append(self)

    def run(self, args):
        self.args = args
        self.cwd_during_run = os.getcwd()


@pytest.fixture
def publisher(monkeypatch):
    RecordingPublisher.instances = []
    monkeypatch.setattr("idp_sdk._core.publish.IDPPublisher", RecordingPublisher)
    return RecordingPublisher


def _fake_transformer(monkeypatch, attr, body="Resources: {}\n", result=True):
    """Replace one of the two template transformers with a recorder that writes
    ``body`` to the output path it is given and returns ``result``."""
    calls = {}

    class FakeTransformer:
        def __init__(self, verbose=False):
            calls["verbose"] = verbose

        def transform(self, input_path, output_path, **kwargs):
            calls["input_path"] = input_path
            calls["output_path"] = output_path
            calls["kwargs"] = kwargs
            if result:
                with open(output_path, "w") as handle:
                    handle.write(body)
            return result

    monkeypatch.setattr(f"idp_sdk._core.template_transform.{attr}", FakeTransformer)
    return calls


@pytest.fixture
def no_cfn_lint(monkeypatch):
    """Pretend cfn-lint is absent so the GovCloud lint gate skips.

    The gate itself is covered by ``test_govcloud_lint_gate.py``; here it would
    only introduce a dependency on whether cfn-lint happens to be installed.
    """
    monkeypatch.setattr(pub.shutil, "which", lambda _: None)


# ---------------------------------------------------------------------------
# build(): region and bucket resolution
# ---------------------------------------------------------------------------


class TestBuildResolvesRegionAndBucket:
    def test_no_region_anywhere_is_a_configuration_error(
        self, tmp_path, monkeypatch, publisher
    ):
        """``build`` must refuse before doing any work, naming the three places a
        region can come from.

        ``boto3.Session`` is replaced rather than having the environment
        cleared, because botocore's region chain includes an IMDS provider: on an
        EC2 host the real session answers with the instance's region and the test
        would pass locally for the wrong reason and fail in CI.
        """

        class NoRegionSession:
            region_name = None

        monkeypatch.setattr(pub.boto3, "Session", NoRegionSession)

        client = IDPClient()
        with pytest.raises(IDPConfigurationError, match="Region is required"):
            client.publish.build(source_dir=str(tmp_path))

        assert RecordingPublisher.instances == []

    def test_the_boto3_session_region_is_used_when_none_is_passed(
        self, source_tree, monkeypatch, publisher, aws_credentials
    ):
        class SessionInOregon:
            region_name = "us-west-2"

        monkeypatch.setattr(pub.boto3, "Session", SessionInOregon)

        client = IDPClient()
        result = client.publish.build(
            source_dir=str(source_tree), bucket=BUCKET_BASENAME, prefix=PREFIX
        )

        assert result.bucket == f"{BUCKET_BASENAME}-us-west-2"
        assert RecordingPublisher.instances[0].args[2] == "us-west-2"

    def test_an_explicit_region_beats_the_client_region(
        self, source_tree, publisher, aws_credentials
    ):
        client = IDPClient(region="eu-central-1")
        result = client.publish.build(
            source_dir=str(source_tree),
            bucket=BUCKET_BASENAME,
            prefix=PREFIX,
            region="us-west-2",
        )

        assert result.bucket == f"{BUCKET_BASENAME}-us-west-2"
        assert "us-west-2" in (result.template_url or "")

    def test_an_absent_bucket_is_derived_from_the_account_id(
        self, source_tree, publisher, aws_credentials
    ):
        """The default bucket basename embeds the caller's account id, so the
        value has to come from STS rather than from a constant."""
        with mock_aws():
            client = IDPClient(region=REGION)
            result = client.publish.build(source_dir=str(source_tree))

        account = "123456789012"  # moto's fixed account
        assert result.bucket == f"idp-accelerator-artifacts-{account}-{REGION}"
        assert (
            RecordingPublisher.instances[0].args[0]
            == f"idp-accelerator-artifacts-{account}"
        )

    def test_an_absent_prefix_defaults_to_idp_cli(
        self, source_tree, publisher, aws_credentials
    ):
        client = IDPClient(region=REGION)
        result = client.publish.build(
            source_dir=str(source_tree), bucket=BUCKET_BASENAME
        )

        assert result.prefix == "idp-cli"
        assert RecordingPublisher.instances[0].args[1] == "idp-cli"


# ---------------------------------------------------------------------------
# build(): the publisher argument vector
# ---------------------------------------------------------------------------


class TestBuildArgumentVector:
    def _build(self, client, source_tree, **kwargs):
        client.publish.build(
            source_dir=str(source_tree),
            bucket=BUCKET_BASENAME,
            prefix=PREFIX,
            region=REGION,
            **kwargs,
        )
        return RecordingPublisher.instances[-1]

    def test_the_three_positionals_come_first_and_in_order(
        self, source_tree, publisher, aws_credentials
    ):
        """``IDPPublisher.run`` parses these positionally, so the order is the
        contract: bucket, prefix, region."""
        instance = self._build(IDPClient(), source_tree)

        assert instance.args[:3] == [BUCKET_BASENAME, PREFIX, REGION]

    def test_every_flag_is_spelled_the_way_the_parser_expects(
        self, source_tree, publisher, aws_credentials
    ):
        instance = self._build(
            IDPClient(),
            source_tree,
            public=True,
            max_workers=4,
            verbose=True,
            clean_build=True,
            no_validate=True,
            lint=False,
        )

        assert instance.args == [
            BUCKET_BASENAME,
            PREFIX,
            REGION,
            "public",
            "--max-workers",
            "4",
            "--verbose",
            "--no-validate",
            "--clean-build",
            "--lint",
            "off",
        ]
        # A bare int here would make argparse's own join blow up, and a MagicMock
        # would accept it.
        assert isinstance(instance.args[instance.args.index("--max-workers") + 1], str)

    def test_the_default_call_passes_no_flags_at_all(
        self, source_tree, publisher, aws_credentials
    ):
        """Linting is on by default, so ``--lint off`` must be absent rather than
        present with a different value."""
        instance = self._build(IDPClient(), source_tree)

        assert instance.args == [BUCKET_BASENAME, PREFIX, REGION]

    def test_zero_workers_is_passed_through_rather_than_treated_as_unset(
        self, source_tree, publisher, aws_credentials
    ):
        """The guard is ``is not None``, so ``max_workers=0`` reaches the
        publisher. A truthiness test would silently drop it, and 0 and "auto" are
        different requests."""
        instance = self._build(IDPClient(), source_tree, max_workers=0)

        assert "--max-workers" in instance.args
        assert instance.args[instance.args.index("--max-workers") + 1] == "0"

    @pytest.mark.parametrize("variant", ["headless", "govcloud"])
    def test_a_variant_build_skips_main_template_validation(
        self, source_tree, publisher, aws_credentials, monkeypatch, no_cfn_lint, variant
    ):
        """``--no-validate`` is forced for either variant even when the caller did
        not ask for it: the base template still declares resources that do not
        validate in the target region, and the transformed template is validated
        instead."""
        _fake_transformer(monkeypatch, "HeadlessTemplateTransformer")
        _fake_transformer(monkeypatch, "GovCloudTemplateTransformer")
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET_FULL)
            instance = self._build(
                IDPClient(), source_tree, no_validate=False, **{variant: True}
            )

        assert "--no-validate" in instance.args

    def test_the_variant_flags_are_set_on_the_publisher_not_in_the_vector(
        self, source_tree, publisher, aws_credentials, monkeypatch, no_cfn_lint
    ):
        """``headless`` and ``govcloud`` are attributes, not argv entries; the
        publisher reads them to decide what to build."""
        _fake_transformer(monkeypatch, "HeadlessTemplateTransformer")
        _fake_transformer(monkeypatch, "GovCloudTemplateTransformer")
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET_FULL)
            instance = self._build(
                IDPClient(), source_tree, headless=True, govcloud=True
            )

        assert instance.headless is True
        assert instance.govcloud is True
        assert "--headless" not in instance.args
        assert "--govcloud" not in instance.args

    def test_verbose_reaches_the_constructor_as_well_as_the_vector(
        self, source_tree, publisher, aws_credentials
    ):
        instance = self._build(IDPClient(), source_tree, verbose=True)

        assert instance.verbose is True
        assert "--verbose" in instance.args


# ---------------------------------------------------------------------------
# build(): working directory, exit codes, and the plain result
# ---------------------------------------------------------------------------


class TestBuildExecution:
    def test_the_publisher_runs_inside_the_source_tree(
        self, source_tree, publisher, aws_credentials
    ):
        """``IDPPublisher`` resolves every path relative to the process working
        directory, so ``build`` chdirs. Recording the cwd *during* ``run`` is the
        only way to see that it did."""
        before = os.getcwd()

        IDPClient(region=REGION).publish.build(
            source_dir=str(source_tree), bucket=BUCKET_BASENAME
        )

        instance = RecordingPublisher.instances[0]
        assert instance.cwd_during_run == os.path.realpath(str(source_tree))
        assert os.getcwd() == before

    def test_the_working_directory_is_restored_after_a_failed_build(
        self, source_tree, monkeypatch, aws_credentials
    ):
        """The ``finally`` is what stops one failed publish leaving every later
        call in the wrong directory."""

        class ExitingPublisher(RecordingPublisher):
            def run(self, args):
                raise SystemExit(2)

        monkeypatch.setattr("idp_sdk._core.publish.IDPPublisher", ExitingPublisher)
        before = os.getcwd()

        result = IDPClient(region=REGION).publish.build(
            source_dir=str(source_tree), bucket=BUCKET_BASENAME
        )

        assert os.getcwd() == before
        assert result.success is False
        assert result.error is not None and "exit code 2" in result.error

    def test_a_zero_exit_is_not_treated_as_a_failure(
        self, source_tree, monkeypatch, aws_credentials
    ):
        """``sys.exit(0)`` is how a clean run can end; treating any SystemExit as
        failure would report every successful publish as broken."""

        class CleanExitPublisher(RecordingPublisher):
            def run(self, args):
                raise SystemExit(0)

        monkeypatch.setattr("idp_sdk._core.publish.IDPPublisher", CleanExitPublisher)

        result = IDPClient(region=REGION).publish.build(
            source_dir=str(source_tree), bucket=BUCKET_BASENAME
        )

        assert result.success is True
        assert result.error is None

    def test_an_unexpected_exception_becomes_an_idp_stack_error(
        self, source_tree, monkeypatch, aws_credentials
    ):
        class BrokenPublisher(RecordingPublisher):
            def run(self, args):
                raise RuntimeError("docker daemon not running")

        monkeypatch.setattr("idp_sdk._core.publish.IDPPublisher", BrokenPublisher)

        with pytest.raises(IDPStackError, match="docker daemon not running"):
            IDPClient(region=REGION).publish.build(
                source_dir=str(source_tree), bucket=BUCKET_BASENAME
            )

    def test_a_plain_build_reports_paths_urls_and_version(
        self, source_tree, publisher, aws_credentials
    ):
        result = IDPClient(region=REGION).publish.build(
            source_dir=str(source_tree), bucket=BUCKET_BASENAME, prefix=PREFIX
        )

        assert isinstance(result, PublishResult)
        assert result.success is True
        assert result.version == "0.6.9"
        assert result.template_path == str(source_tree / ".aws-sam" / "idp-main.yaml")
        assert result.template_url == (
            f"https://s3.{REGION}.amazonaws.com/{BUCKET_FULL}/{PREFIX}/idp-main.yaml"
        )
        # Nothing was asked for, so neither variant is reported.
        assert result.headless_template_path is None
        assert result.govcloud_template_url is None

    def test_a_missing_version_file_leaves_the_version_empty(
        self, source_tree, publisher, aws_credentials
    ):
        """Not an error: a source tree without a VERSION file still publishes."""
        (source_tree / "VERSION").unlink()

        result = IDPClient(region=REGION).publish.build(
            source_dir=str(source_tree), bucket=BUCKET_BASENAME
        )

        assert result.success is True
        assert result.version == ""

    def test_a_relative_source_dir_is_resolved_before_the_chdir(
        self, source_tree, publisher, aws_credentials, monkeypatch
    ):
        """``template_path`` is built by joining ``source_dir``, so a relative
        value that was not absolutised would produce a path relative to whatever
        directory the caller happened to be in."""
        monkeypatch.chdir(source_tree.parent)

        result = IDPClient(region=REGION).publish.build(
            source_dir=source_tree.name, bucket=BUCKET_BASENAME
        )

        assert result.template_path is not None
        assert os.path.isabs(result.template_path)
        assert os.path.isfile(result.template_path)


# ---------------------------------------------------------------------------
# build(): the GovCloud variant
# ---------------------------------------------------------------------------


class TestBuildGovCloudVariant:
    def test_the_transformed_template_is_uploaded_and_reported(
        self, source_tree, publisher, aws_credentials, monkeypatch, no_cfn_lint
    ):
        """The uploaded key and its ``ContentType`` are read back from S3: the URL
        in the result is only usable as a ``TemplateURL`` if the object is
        actually there under that key."""
        calls = _fake_transformer(monkeypatch, "GovCloudTemplateTransformer")

        with mock_aws():
            s3 = boto3.client("s3", region_name=REGION)
            s3.create_bucket(Bucket=BUCKET_FULL)

            result = IDPClient(region=REGION).publish.build(
                source_dir=str(source_tree),
                bucket=BUCKET_BASENAME,
                prefix=PREFIX,
                govcloud=True,
            )

            stored = s3.get_object(
                Bucket=BUCKET_FULL, Key=f"{PREFIX}/idp-govcloud.yaml"
            )
            assert stored["ContentType"] == "text/yaml"
            assert stored["Body"].read() == b"Resources: {}\n"

        assert result.success is True
        assert result.govcloud_template_path == str(
            source_tree / ".aws-sam" / "idp-govcloud.yaml"
        )
        assert result.govcloud_template_url == (
            f"https://s3.{REGION}.amazonaws.com/{BUCKET_FULL}/{PREFIX}/idp-govcloud.yaml"
        )
        assert calls["input_path"] == str(source_tree / ".aws-sam" / "idp-main.yaml")

    def test_a_commercial_build_region_still_lints_against_govcloud(
        self, source_tree, publisher, aws_credentials, monkeypatch
    ):
        """The common case is building in a commercial region for deployment into
        GovCloud. Linting against the *build* region would never surface E3006
        for a resource type GovCloud lacks, which is the whole point of the gate.
        """
        _fake_transformer(monkeypatch, "GovCloudTemplateTransformer")
        seen = {}

        def fake_lint(path, region):
            seen["region"] = region
            return (True, [], "ran")

        monkeypatch.setattr(pub, "_cfn_lint_region_errors", fake_lint)

        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET_FULL)
            IDPClient(region=REGION).publish.build(
                source_dir=str(source_tree), bucket=BUCKET_BASENAME, govcloud=True
            )

        assert seen["region"] == pub.DEFAULT_GOVCLOUD_LINT_REGION
        assert seen["region"].startswith("us-gov-")

    def test_a_govcloud_build_region_lints_against_itself(
        self, source_tree, publisher, aws_credentials, monkeypatch
    ):
        _fake_transformer(monkeypatch, "GovCloudTemplateTransformer")
        seen = {}
        monkeypatch.setattr(
            pub,
            "_cfn_lint_region_errors",
            lambda path, region: (seen.update(region=region), (True, [], "ran"))[1],
        )

        with mock_aws():
            _create_bucket(f"{BUCKET_BASENAME}-us-gov-east-1", "us-gov-east-1")
            IDPClient().publish.build(
                source_dir=str(source_tree),
                bucket=BUCKET_BASENAME,
                region="us-gov-east-1",
                govcloud=True,
            )

        assert seen["region"] == "us-gov-east-1"

    def test_a_failed_transform_fails_the_publish_and_keeps_the_main_template(
        self, source_tree, publisher, aws_credentials, monkeypatch, no_cfn_lint
    ):
        """The main template was still built and uploaded, so the result must
        report it alongside the failure — otherwise the caller cannot tell a
        transform failure from a build that produced nothing."""
        _fake_transformer(monkeypatch, "GovCloudTemplateTransformer", result=False)

        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET_FULL)
            result = IDPClient(region=REGION).publish.build(
                source_dir=str(source_tree),
                bucket=BUCKET_BASENAME,
                prefix=PREFIX,
                govcloud=True,
            )

        assert result.success is False
        assert result.error is not None
        assert "GovCloud template transformation failed" in result.error
        assert result.template_path is not None
        assert result.version == "0.6.9"
        assert result.govcloud_template_path is None

    def test_a_template_cloudformation_rejects_fails_the_publish(
        self, source_tree, publisher, aws_credentials, monkeypatch, no_cfn_lint
    ):
        """``validate_template`` is called against the uploaded URL, so this is
        the step that catches a transform which produced a syntactically broken
        template. The GovCloud paths are reported even though validation failed,
        so the caller can inspect the artifact."""
        _fake_transformer(
            monkeypatch, "GovCloudTemplateTransformer", body=NOT_A_TEMPLATE
        )

        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET_FULL)
            result = IDPClient(region=REGION).publish.build(
                source_dir=str(source_tree),
                bucket=BUCKET_BASENAME,
                prefix=PREFIX,
                govcloud=True,
            )

        assert result.success is False
        assert result.error is not None
        assert "GovCloud template validation failed" in result.error
        assert result.govcloud_template_path is not None
        assert result.govcloud_template_url is not None

    def test_no_validate_skips_the_cloudformation_call(
        self, source_tree, publisher, aws_credentials, monkeypatch, no_cfn_lint
    ):
        """With ``no_validate=True`` the same broken template publishes
        successfully — which is what shows the validation really was skipped
        rather than merely passing."""
        _fake_transformer(
            monkeypatch, "GovCloudTemplateTransformer", body=NOT_A_TEMPLATE
        )

        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET_FULL)
            result = IDPClient(region=REGION).publish.build(
                source_dir=str(source_tree),
                bucket=BUCKET_BASENAME,
                govcloud=True,
                no_validate=True,
            )

        assert result.success is True
        assert result.govcloud_template_url is not None


# ---------------------------------------------------------------------------
# build(): the headless variant
# ---------------------------------------------------------------------------


class TestBuildHeadlessVariant:
    def test_the_transformed_template_is_uploaded_and_reported(
        self, source_tree, publisher, aws_credentials, monkeypatch
    ):
        calls = _fake_transformer(monkeypatch, "HeadlessTemplateTransformer")

        with mock_aws():
            s3 = boto3.client("s3", region_name=REGION)
            s3.create_bucket(Bucket=BUCKET_FULL)

            result = IDPClient(region=REGION).publish.build(
                source_dir=str(source_tree),
                bucket=BUCKET_BASENAME,
                prefix=PREFIX,
                headless=True,
            )

            stored = s3.get_object(
                Bucket=BUCKET_FULL, Key=f"{PREFIX}/idp-headless.yaml"
            )
            assert stored["ContentType"] == "text/yaml"

        assert result.success is True
        assert result.headless_template_url == (
            f"https://s3.{REGION}.amazonaws.com/{BUCKET_FULL}/{PREFIX}/idp-headless.yaml"
        )
        # A commercial build region means no GovCloud config rewrite.
        assert calls["kwargs"] == {"update_govcloud_config": False}

    def test_a_govcloud_region_asks_the_transform_to_rewrite_the_config(
        self, source_tree, publisher, aws_credentials, monkeypatch
    ):
        """A headless build *into* GovCloud needs the configuration maps
        rewritten; the flag is derived from the region rather than asked for, so
        a wrong derivation is silent."""
        calls = _fake_transformer(monkeypatch, "HeadlessTemplateTransformer")

        with mock_aws():
            _create_bucket(f"{BUCKET_BASENAME}-us-gov-west-1", "us-gov-west-1")
            IDPClient().publish.build(
                source_dir=str(source_tree),
                bucket=BUCKET_BASENAME,
                region="us-gov-west-1",
                headless=True,
            )

        assert calls["kwargs"] == {"update_govcloud_config": True}

    def test_a_template_cloudformation_rejects_fails_the_publish(
        self, source_tree, publisher, aws_credentials, monkeypatch
    ):
        _fake_transformer(
            monkeypatch, "HeadlessTemplateTransformer", body=NOT_A_TEMPLATE
        )

        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET_FULL)
            result = IDPClient(region=REGION).publish.build(
                source_dir=str(source_tree),
                bucket=BUCKET_BASENAME,
                headless=True,
            )

        assert result.success is False
        assert result.error is not None
        assert "Headless template validation failed" in result.error
        assert result.headless_template_path is not None

    def test_a_failed_headless_transform_is_swallowed_and_reported_as_success(
        self, source_tree, publisher, aws_credentials, monkeypatch
    ):
        """DEFECT (operations/publish.py:278-279). The headless branch is
        ``if headless_result.success and headless_result.output_path:`` with no
        ``else``, so a transform that fails is discarded: ``build`` returns
        ``success=True``, ``error=None`` and ``headless_template_path=None``, and
        the transform's own error message is lost. The GovCloud branch a few lines
        above returns a failure for the same condition.

        Observable consequence: ``idp-cli publish --headless`` exits successfully
        having published no headless template, and the operator finds out when the
        1-click launch URL 404s. Pinned as-is — the asymmetry with the GovCloud
        branch is the evidence that success is not the intended answer.
        """
        _fake_transformer(monkeypatch, "HeadlessTemplateTransformer", result=False)

        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET_FULL)
            result = IDPClient(region=REGION).publish.build(
                source_dir=str(source_tree),
                bucket=BUCKET_BASENAME,
                headless=True,
            )

        assert result.success is True
        assert result.error is None
        assert result.headless_template_path is None
        assert result.headless_template_url is None

    def test_both_variants_can_be_produced_in_one_build(
        self, source_tree, publisher, aws_credentials, monkeypatch, no_cfn_lint
    ):
        _fake_transformer(monkeypatch, "HeadlessTemplateTransformer")
        _fake_transformer(monkeypatch, "GovCloudTemplateTransformer")

        with mock_aws():
            s3 = boto3.client("s3", region_name=REGION)
            s3.create_bucket(Bucket=BUCKET_FULL)
            result = IDPClient(region=REGION).publish.build(
                source_dir=str(source_tree),
                bucket=BUCKET_BASENAME,
                prefix=PREFIX,
                headless=True,
                govcloud=True,
            )
            keys = {
                o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET_FULL)["Contents"]
            }

        assert keys == {
            f"{PREFIX}/idp-headless.yaml",
            f"{PREFIX}/idp-govcloud.yaml",
        }
        assert result.headless_template_url is not None
        assert result.govcloud_template_url is not None
        assert result.headless_template_url != result.govcloud_template_url


# ---------------------------------------------------------------------------
# transform_template_headless()
# ---------------------------------------------------------------------------


class TestTransformTemplateHeadless:
    def test_a_successful_transform_reports_both_paths(self, tmp_path, monkeypatch):
        calls = _fake_transformer(monkeypatch, "HeadlessTemplateTransformer")
        source = tmp_path / "main.yaml"
        source.write_text("Resources: {}\n")
        out = tmp_path / "out.yaml"

        result = IDPClient().publish.transform_template_headless(
            source_template=str(source), output_path=str(out), verbose=True
        )

        assert isinstance(result, TemplateTransformResult)
        assert result.success is True
        assert result.input_path == str(source)
        assert result.output_path == str(out)
        assert out.is_file()
        assert calls["verbose"] is True
        assert calls["kwargs"] == {"update_govcloud_config": False}

    def test_the_default_output_path_suffixes_the_source_name(
        self, tmp_path, monkeypatch
    ):
        """The suffix goes before the extension, so the result is still a
        ``.yaml`` file — ``main.yaml`` becomes ``main-headless.yaml``, not
        ``main.yaml-headless``."""
        _fake_transformer(monkeypatch, "HeadlessTemplateTransformer")
        source = tmp_path / "main.yaml"
        source.write_text("Resources: {}\n")

        result = IDPClient().publish.transform_template_headless(str(source))

        assert result.output_path == str(tmp_path / "main-headless.yaml")

    def test_a_missing_source_is_reported_without_raising(self, tmp_path):
        """These transform methods report failure in the result rather than
        raising, because ``build`` inspects ``.success``."""
        result = IDPClient().publish.transform_template_headless(
            str(tmp_path / "absent.yaml")
        )

        assert result.success is False
        assert result.error is not None
        assert "Source template not found" in result.error
        assert result.output_path is None

    def test_a_transformer_returning_false_names_the_validation_failure(
        self, tmp_path, monkeypatch
    ):
        _fake_transformer(monkeypatch, "HeadlessTemplateTransformer", result=False)
        source = tmp_path / "main.yaml"
        source.write_text("Resources: {}\n")

        result = IDPClient().publish.transform_template_headless(str(source))

        assert result.success is False
        assert result.error == "Template transformation failed validation"
        # The intended output path is still reported so the caller can look.
        assert result.output_path == str(tmp_path / "main-headless.yaml")

    def test_a_raising_transformer_is_reported_as_its_message(
        self, tmp_path, monkeypatch
    ):
        class ExplodingTransformer:
            def __init__(self, verbose=False):
                pass

            def transform(self, *args, **kwargs):
                raise KeyError("Resources")

        monkeypatch.setattr(
            "idp_sdk._core.template_transform.HeadlessTemplateTransformer",
            ExplodingTransformer,
        )
        source = tmp_path / "main.yaml"
        source.write_text("Resources: {}\n")

        result = IDPClient().publish.transform_template_headless(str(source))

        assert result.success is False
        assert result.error is not None and "Resources" in result.error


# ---------------------------------------------------------------------------
# transform_template_govcloud(): the parts the lint-gate tests do not reach
# ---------------------------------------------------------------------------


class TestTransformTemplateGovCloud:
    def test_a_missing_source_is_reported_without_raising(self, tmp_path):
        result = IDPClient().publish.transform_template_govcloud(
            str(tmp_path / "absent.yaml")
        )

        assert result.success is False
        assert result.error is not None
        assert "Source template not found" in result.error

    def test_the_default_output_path_suffixes_the_source_name(
        self, tmp_path, monkeypatch, no_cfn_lint
    ):
        _fake_transformer(monkeypatch, "GovCloudTemplateTransformer")
        source = tmp_path / "main.yaml"
        source.write_text("Resources: {}\n")

        result = IDPClient().publish.transform_template_govcloud(str(source))

        assert result.success is True
        assert result.output_path == str(tmp_path / "main-govcloud.yaml")

    def test_a_transformer_returning_false_skips_the_lint_gate(
        self, tmp_path, monkeypatch
    ):
        """There is nothing to lint if the transform failed, and linting a
        half-written file would report a confusing second error."""
        _fake_transformer(monkeypatch, "GovCloudTemplateTransformer", result=False)
        linted = []
        monkeypatch.setattr(
            pub,
            "_cfn_lint_region_errors",
            lambda path, region: (linted.append(path), (True, [], ""))[1],
        )
        source = tmp_path / "main.yaml"
        source.write_text("Resources: {}\n")

        result = IDPClient().publish.transform_template_govcloud(str(source))

        assert result.success is False
        assert result.error == "GovCloud template transformation failed validation"
        assert linted == []

    def test_a_raising_transformer_is_reported_as_its_message(
        self, tmp_path, monkeypatch
    ):
        class ExplodingTransformer:
            def __init__(self, verbose=False):
                pass

            def transform(self, *args, **kwargs):
                raise RuntimeError("cannot parse !Ref shorthand")

        monkeypatch.setattr(
            "idp_sdk._core.template_transform.GovCloudTemplateTransformer",
            ExplodingTransformer,
        )
        source = tmp_path / "main.yaml"
        source.write_text("Resources: {}\n")

        result = IDPClient().publish.transform_template_govcloud(str(source))

        assert result.success is False
        assert result.error is not None
        assert "cannot parse" in result.error
        # No output path: nothing was written.
        assert result.output_path is None


# ---------------------------------------------------------------------------
# print_deployment_urls()
# ---------------------------------------------------------------------------

MAIN_URL = f"https://s3.{REGION}.amazonaws.com/{BUCKET_FULL}/{PREFIX}/idp-main.yaml"


class TestPrintDeploymentUrls:
    def test_a_commercial_region_gets_the_commercial_console_domain(self, capsys):
        IDPClient().publish.print_deployment_urls(MAIN_URL, REGION)

        out = capsys.readouterr().out
        assert MAIN_URL in out
        assert f"https://{REGION}.console.aws.amazon.com/cloudformation/home" in out
        assert "amazonaws-us-gov.com" not in out

    def test_a_govcloud_region_gets_the_govcloud_console_domain(self, capsys):
        """A commercial console host cannot reach a GovCloud account, so this
        substitution is the difference between a working link and a dead one."""
        IDPClient().publish.print_deployment_urls(MAIN_URL, "us-gov-west-1")

        out = capsys.readouterr().out
        assert "us-gov-west-1.console.amazonaws-us-gov.com" in out
        assert "console.aws.amazon.com" not in out

    def test_the_launch_url_keeps_the_console_fragment_and_query_unencoded(
        self, capsys
    ):
        """``#``, ``?``, ``&`` and ``=`` are in the ``safe`` set on purpose: the
        console link is a fragment-routed URL, and percent-encoding the ``#``
        would send the browser to a query string the console does not read."""
        IDPClient().publish.print_deployment_urls(MAIN_URL, REGION, stack_name="MyIDP")

        out = capsys.readouterr().out
        assert "#/stacks/create/review?" in out
        assert f"templateURL={MAIN_URL}" in out
        assert "stackName=MyIDP" in out

    def test_characters_outside_the_safe_set_are_percent_encoded(self, capsys):
        """An S3 key may contain a space; unencoded it truncates the console's
        ``templateURL`` parameter."""
        spaced = (
            f"https://s3.{REGION}.amazonaws.com/{BUCKET_FULL}/my prefix/idp-main.yaml"
        )

        IDPClient().publish.print_deployment_urls(spaced, REGION)

        out = capsys.readouterr().out
        assert quote(spaced, safe=":/?#[]@!$&'()*+,;=") in out
        assert "my%20prefix" in out

    def test_neither_variant_section_is_printed_when_not_supplied(self, capsys):
        IDPClient().publish.print_deployment_urls(MAIN_URL, REGION)

        out = capsys.readouterr().out
        assert "Headless" not in out
        assert "GovCloud" not in out

    def test_each_variant_gets_its_own_url_and_launch_link(self, capsys):
        headless_url = MAIN_URL.replace("idp-main", "idp-headless")
        govcloud_url = MAIN_URL.replace("idp-main", "idp-govcloud")

        IDPClient().publish.print_deployment_urls(
            MAIN_URL,
            REGION,
            headless_template_url=headless_url,
            govcloud_template_url=govcloud_url,
            stack_name="IDP",
        )

        out = capsys.readouterr().out
        assert headless_url in out
        assert govcloud_url in out
        # The headless launch link names a distinct stack so the two variants do
        # not collide in one account; the GovCloud one is a replacement, not an
        # addition, and keeps the base name.
        assert "stackName=IDP-Headless" in out
        assert f"templateURL={quote(govcloud_url, safe=":/?#[]@!$&'()*+,;=")}" in out
