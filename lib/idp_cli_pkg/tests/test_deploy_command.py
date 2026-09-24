# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
What ``idp-cli deploy`` actually submits to CloudFormation, and what it refuses.

``deploy`` is the command in this package with the most expensive failure modes.
It creates and updates the accelerator's root stack, so a wrong answer is not a
wrong line of output: submitting a parameter the user never named changes the
deployed system, dropping one they did name silently leaves the stack on the
publish-time default, and omitting a capability flag or passing a tag set the
user did not ask for fails — or mutates — a real stack. None of those are visible
from ``describe_stacks`` afterwards, because CloudFormation (and moto) fill in
every parameter the template declares with its default, so a parameter that was
never submitted looks exactly like one submitted at its default value.

The tests are therefore written against the **submitted request**, read back off
the ``api_calls`` fixture, which records what botocore received while still
letting the call reach moto. The happy paths run the real ``idp_sdk`` deploy code
against moto's CloudFormation with a genuinely valid template
(``cfn_template_file``), so the assertions cover the CLI's argument translation
*and* the SDK's request building together, the way a user meets them.

Three groups need a patched ``idp_cli.cli.IDPClient`` instead of moto, and the
reason is stated at each: the lifecycle states (``check_in_progress`` and a
failing ``monitor``) cannot be produced in moto, which completes every stack
operation instantly; region/template-URL resolution must be observed *without*
letting CloudFormation try to fetch a real ``https://`` template; and the
``--from-code`` build is a subprocess in the SDK.

Two of the tests below pin behaviour that is wrong on purpose. The
``--parameters`` regex at ``cli.py:896`` drops a pair written with a space before
the ``=`` and mangles a key containing ``_``; both are issue #1220, both are
currently silent, and both are asserted here as they behave today so that fixing
them is a deliberate act that turns these tests red rather than an accident.
"""

import json
import re
import time
from types import SimpleNamespace
from unittest.mock import patch

import boto3
import pytest
from click.testing import CliRunner
from moto import mock_aws

from idp_cli import cli as cli_module
from idp_cli.cli import TEMPLATE_URLS

STACK = "idp-test"
REGION = "us-west-2"
EMAIL = "admin@example.invalid"

#: The three capability flags CloudFormation needs for this template. The root
#: template creates named IAM roles and uses SAM transforms in nested stacks, so a
#: missing flag is not a lint nit — it is an ``InsufficientCapabilities`` failure at
#: CreateStack with nothing deployed.
REQUIRED_CAPABILITIES = [
    "CAPABILITY_IAM",
    "CAPABILITY_NAMED_IAM",
    "CAPABILITY_AUTO_EXPAND",
]


@pytest.fixture(autouse=True)
def never_sleep(monkeypatch):
    """No test here may spend wall clock in the SDK's stack-polling loop.

    ``StackDeployer._wait_for_completion`` sleeps 10 seconds between
    ``describe_stacks`` polls. Under moto the first poll already sees
    ``CREATE_COMPLETE`` so the sleep is not reached today, but a test that starts
    taking ten seconds because a status mapping changed is a defect in the test,
    not a slow machine.
    """
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


def _deploy(*args: str):
    """Invoke ``idp-cli deploy`` through the real group, as a user would."""
    return CliRunner().invoke(cli_module.cli, ["deploy", *args])


def _explicit(call) -> dict:
    """The parameters the request submitted a VALUE for, as ``{key: value}``.

    On an update the SDK also sends ``UsePreviousValue`` entries for every
    parameter the stack already has; those carry no value and are not what a test
    about "which parameters did the user change" is asking about.
    """
    return {
        p["ParameterKey"]: p["ParameterValue"]
        for p in call.params["Parameters"]
        if "ParameterValue" in p
    }


def _use_previous(call) -> set:
    """The parameters the request asked CloudFormation to keep unchanged."""
    return {
        p["ParameterKey"]
        for p in call.params["Parameters"]
        if p.get("UsePreviousValue")
    }


def _patched_client(*, exists=False, in_progress=None, deploy_result=None):
    """A patch context for ``idp_cli.cli.IDPClient`` in its ordinary state.

    Returns the ``patch`` object; entering it yields the patched class, whose
    ``return_value`` is the client the command will use. The default is the
    common case: nothing in progress, stack absent, deploy initiated without
    ``--wait``.
    """
    patcher = patch.object(cli_module, "IDPClient")
    client_cls = patcher.start()
    client = client_cls.return_value
    client.stack.check_in_progress.return_value = in_progress
    client.stack.exists.return_value = exists
    client.stack.deploy.return_value = deploy_result or SimpleNamespace(
        success=False,
        status="INITIATED",
        operation="CREATE",
        outputs={},
        error=None,
    )
    return patcher, client_cls, client


class TestSubmittedParameters:
    """Which CloudFormation parameters a create actually carries.

    Every assertion is on the exact submitted set rather than on a subset,
    because the interesting defects in this area are *extra* parameters (a
    mangled key, an injected default) as much as missing ones.
    """

    def test_a_bare_create_submits_the_admin_email_and_nothing_else(
        self, api_calls, cfn_template_file
    ):
        """No ``--max-concurrent``, no ``--log-level``: neither may be invented.

        ``LogLevel`` in particular must be absent rather than sent at the CLI's
        idea of a default: the template's default moved from ``INFO`` to
        ``WARN``, so a CLI that helpfully submitted ``INFO`` would deploy a
        different logging level than the template documents.
        """
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert _explicit(api_calls.only("CreateStack")) == {"AdminEmail": EMAIL}

    def test_max_concurrent_at_its_default_of_100_submits_nothing_for_it(
        self, api_calls, cfn_template_file
    ):
        """``--max-concurrent 100`` submits no ``MaxConcurrentWorkflows`` at all.

        This is deliberate and surprising, so it is pinned: ``cli.py`` passes
        ``max_concurrent if max_concurrent != 100 else None``, which makes the
        flag's own default indistinguishable from the user typing it. The
        consequence is benign on a create (the template's default is also 100)
        and is the point on an update, where sending the value would overwrite a
        deliberately-raised concurrency limit with 100. What it also means is
        that a user who has raised the limit and wants it back at 100 cannot say
        so with this flag.
        """
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--max-concurrent",
                "100",
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert _explicit(api_calls.only("CreateStack")) == {"AdminEmail": EMAIL}

    def test_a_non_default_max_concurrent_is_submitted_as_a_string(
        self, api_calls, cfn_template_file
    ):
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--max-concurrent",
                "250",
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert _explicit(api_calls.only("CreateStack")) == {
            "AdminEmail": EMAIL,
            "MaxConcurrentWorkflows": "250",
        }

    @pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARN", "ERROR"])
    def test_an_explicit_log_level_reaches_cloudformation_verbatim(
        self, api_calls, cfn_template_file, level
    ):
        """Every accepted choice must survive, ``INFO`` included.

        ``INFO`` is the one worth parametrising for: the command used to drop the
        parameter whenever it equalled ``INFO``, which stopped being a no-op when
        the template default became ``WARN``.
        """
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--log-level",
                level,
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert _explicit(api_calls.only("CreateStack")) == {
            "AdminEmail": EMAIL,
            "LogLevel": level,
        }

    def test_an_s3_custom_config_becomes_customconfigpath(
        self, api_calls, cfn_template_file
    ):
        """An ``s3://`` config is forwarded as-is; a local one would be uploaded.

        The S3 form is used here on purpose: it is the branch that makes no AWS
        call of its own, so what this test measures is the CLI's translation of
        ``--custom-config`` into the ``CustomConfigPath`` stack parameter.
        """
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--custom-config",
                "s3://my-configs/lending/config.yaml",
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert _explicit(api_calls.only("CreateStack")) == {
            "AdminEmail": EMAIL,
            "CustomConfigPath": "s3://my-configs/lending/config.yaml",
        }
        assert "CustomConfig = s3://my-configs/lending/config.yaml" in result.output

    def test_named_options_and_free_form_parameters_are_merged(
        self, api_calls, cfn_template_file
    ):
        """``--parameters`` adds to the named options rather than replacing them."""
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--max-concurrent",
                "42",
                "--log-level",
                "DEBUG",
                "--parameters",
                "DataRetentionInDays=90,ErrorThreshold=5",
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert _explicit(api_calls.only("CreateStack")) == {
            "AdminEmail": EMAIL,
            "MaxConcurrentWorkflows": "42",
            "LogLevel": "DEBUG",
            "DataRetentionInDays": "90",
            "ErrorThreshold": "5",
        }


class TestTags:
    """``--tags`` reaches the request as a tag list — or not at all.

    The omission is the load-bearing half. ``update_stack`` replaces the whole
    tag set with whatever ``Tags`` carries and has no per-tag
    ``UsePreviousValue``, so a request that always sent ``Tags`` would delete
    every tag on the stack on any update that did not restate them.
    """

    def test_tags_are_submitted_as_key_value_pairs_in_order(
        self, api_calls, cfn_template_file
    ):
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--tags",
                "Owner=docs-team,Environment=prod",
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert api_calls.only("CreateStack").params["Tags"] == [
            {"Key": "Owner", "Value": "docs-team"},
            {"Key": "Environment", "Value": "prod"},
        ]

    def test_no_tags_omits_the_tags_key_from_the_request_entirely(
        self, api_calls, cfn_template_file
    ):
        """Not ``Tags: []`` — absent. An empty list is still a replacement."""
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert "Tags" not in api_calls.only("CreateStack").params

    def test_a_malformed_tag_is_refused_before_any_cloudformation_call(
        self, api_calls, cfn_template_file
    ):
        """``--tags Owner`` (no ``=``) must not reach a half-tagged deploy."""
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--tags",
                "OwnerWithNoValue",
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code != 0
        assert "Invalid tag" in result.output
        assert api_calls.of("CreateStack") == []


class TestTheParametersRegexIsDefective:
    """``--parameters`` is parsed by a regex that silently mis-reads two shapes.

    ``cli.py:896`` uses::

        ([A-Za-z][A-Za-z0-9]*)=((?:(?![A-Za-z][A-Za-z0-9]*=).)*)

    with ``re.finditer``, so anything the pattern does not match is not an error
    — it is simply absent from the parsed dict, and nothing warns. Both
    consequences below are issue #1220. The tests assert today's wrong behaviour
    deliberately: fixing the regex should turn them red and be a considered
    change, not something that slips in.
    """

    def test_a_space_before_the_equals_silently_parses_nothing(
        self, api_calls, cfn_template_file
    ):
        """DEFECT (#1220): ``--parameters "MaxConcurrentWorkflows = 200"`` is ignored.

        ``finditer`` finds no match at all, ``additional_params`` stays empty, the
        deploy proceeds, and no warning is printed. The user believes they raised
        the concurrency limit; the stack keeps the publish-time default. The only
        signal is in the CloudFormation console afterwards.
        """
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--parameters",
                "MaxConcurrentWorkflows = 200",
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        submitted = _explicit(api_calls.only("CreateStack"))
        assert submitted == {"AdminEmail": EMAIL}, (
            "pinning the defect: the space-separated pair matched nothing, so "
            f"nothing was submitted for it. Got {submitted}"
        )
        assert "MaxConcurrentWorkflows" not in result.output

    def test_an_underscore_in_a_key_submits_a_parameter_the_user_never_named(
        self, api_calls, cfn_template_file
    ):
        """DEFECT (#1220): ``Log_Level=DEBUG`` is submitted as ``Level=DEBUG``.

        ``_`` is not in the key character class, so the match starts after it and
        the leading ``Log`` is discarded. The observable consequence is worse than
        a dropped parameter: a real parameter name is sent that the user did not
        write, and if the template happens to declare it the value lands on the
        wrong setting. Here the template does not declare ``Level``, so
        CloudFormation rejects the whole create with "Parameters: [Level] do not
        exist in the template" — accurate about a name the user never typed.
        """
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--parameters",
                "Log_Level=DEBUG",
                "--template-file",
                cfn_template_file(extra_parameters=("Level",)),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        submitted = _explicit(api_calls.only("CreateStack"))
        assert submitted == {"AdminEmail": EMAIL, "Level": "DEBUG"}, (
            "pinning the defect: the key was truncated to 'Level' and submitted "
            f"anyway. Got {submitted}"
        )
        assert "LogLevel" not in submitted
        assert "Log_Level" not in submitted

    def test_a_value_containing_commas_survives_intact(
        self, api_calls, cfn_template_file
    ):
        """What the regex is FOR: a subnet list is one value, not three pairs.

        This is the case that motivated the lookahead — splitting on every comma
        would turn ``SubnetIds=subnet-a,subnet-b`` into a parameter whose value is
        one subnet and a nonsense pair after it.
        """
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--parameters",
                "SubnetIds=subnet-a,subnet-b,VpcId=vpc-1",
                "--template-file",
                cfn_template_file(extra_parameters=("SubnetIds", "VpcId")),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert _explicit(api_calls.only("CreateStack")) == {
            "AdminEmail": EMAIL,
            "SubnetIds": "subnet-a,subnet-b",
            "VpcId": "vpc-1",
        }


class TestRefusals:
    """What ``deploy`` refuses, with what exit code, and having deployed nothing.

    Each case asserts all three: a non-zero exit, the message a user needs to
    act on, and — read off ``api_calls`` rather than a mock — that no
    ``CreateStack`` was submitted. A refusal that still creates a stack is the
    failure mode worth guarding against.
    """

    def test_more_than_one_template_source_is_refused(
        self, api_calls, cfn_template_file, tmp_path
    ):
        """``--from-code``, ``--template-url`` and ``--template-file`` conflict.

        Silently picking one would deploy a template the user did not choose;
        which one depends only on the order of the ``elif`` chain.
        """
        source_dir = tmp_path / "project"
        source_dir.mkdir()
        template = cfn_template_file()
        combinations = [
            [
                "--from-code",
                str(source_dir),
                "--template-url",
                "https://example/t.yaml",
            ],
            ["--from-code", str(source_dir), "--template-file", template],
            ["--template-url", "https://example/t.yaml", "--template-file", template],
            [
                "--from-code",
                str(source_dir),
                "--template-url",
                "https://example/t.yaml",
                "--template-file",
                template,
            ],
        ]
        with mock_aws():
            for extra in combinations:
                result = _deploy(
                    "--stack-name",
                    STACK,
                    "--admin-email",
                    EMAIL,
                    "--region",
                    REGION,
                    *extra,
                )
                assert result.exit_code == 1, result.output
                assert "Cannot specify more than one of" in result.output
        assert api_calls.of("CreateStack") == []

    def test_headless_and_govcloud_are_mutually_exclusive(
        self, api_calls, cfn_template_file
    ):
        """They disagree about the UI, so there is no sensible combination.

        ``--headless`` removes the web UI entirely; ``--govcloud`` keeps it and
        removes CloudFront instead. The message has to say which is which,
        because the flags' names do not.
        """
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--headless",
                "--govcloud",
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 1, result.output
        assert "--headless and --govcloud are mutually exclusive" in result.output
        assert api_calls.of("CreateStack") == []

    def test_enable_hitl_true_is_refused_with_where_hitl_moved_to(
        self, api_calls, cfn_template_file
    ):
        """HITL stopped being a stack parameter in v0.4.11.

        Before this refusal existed the flag reached CloudFormation and died with
        "Parameters: [EnableHITL] do not exist in the template" after the build
        and upload, which told the user nothing about where the setting went.
        """
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--enable-hitl",
                "true",
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 1, result.output
        assert "no longer a stack parameter" in result.output
        assert "Assessment & HITL Configuration" in result.output
        assert api_calls.of("CreateStack") == []

    def test_enable_hitl_false_is_still_accepted_and_submits_no_parameter(
        self, api_calls, cfn_template_file
    ):
        """The flag stays accepted as ``false`` so existing scripts keep working."""
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--enable-hitl",
                "false",
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert _explicit(api_calls.only("CreateStack")) == {"AdminEmail": EMAIL}

    def test_a_new_stack_without_an_admin_email_is_refused(
        self, api_calls, cfn_template_file
    ):
        """There is no way to recover the admin user after the fact.

        The Cognito admin user is created from this parameter, so a stack created
        without it has no one who can sign in — hence a refusal rather than a
        default.
        """
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 1, result.output
        assert "--admin-email is required when creating a new stack" in result.output
        assert api_calls.of("CreateStack") == []

    @pytest.mark.parametrize(
        ("extra", "expected"),
        [
            ([], "is not supported"),
            (["--headless"], "is not supported for headless mode"),
            (["--govcloud"], "is not supported for --govcloud"),
        ],
        ids=["plain", "headless", "govcloud"],
    )
    def test_an_unsupported_region_without_a_template_url_is_refused(
        self, api_calls, extra, expected
    ):
        """Published templates exist in three regions only.

        There is no template to deploy in ``ap-south-1``, so each of the three
        paths that would otherwise reach for ``TEMPLATE_URLS`` raises instead,
        and each message names the supported regions and the way out
        (``--template-url`` or ``--from-code``).
        """
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--region",
                "ap-south-1",
                *extra,
            )
        assert result.exit_code == 1, result.output
        assert expected in result.output
        assert "us-west-2" in result.output and "eu-central-1" in result.output
        assert api_calls.of("CreateStack") == []


class TestRegionAndTemplateResolution:
    """Which region the command settles on, and which template URL follows.

    ``IDPClient`` is patched here rather than using moto, for a specific reason:
    these paths end in a published ``https://s3...`` template URL, and letting a
    real ``CreateStack`` see it would mean CloudFormation fetching it over the
    network. What matters is the URL the command chose and the region it built
    the client with, both of which are visible at that seam.
    """

    def test_no_region_flag_falls_back_to_the_boto3_session_region(self, monkeypatch):
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
        monkeypatch.setenv("AWS_REGION", "eu-central-1")
        patcher, client_cls, client = _patched_client()
        try:
            result = _deploy("--stack-name", STACK, "--admin-email", EMAIL)
        finally:
            patcher.stop()
        assert result.exit_code == 0, result.output
        assert client_cls.call_args.kwargs == {
            "stack_name": STACK,
            "region": "eu-central-1",
        }
        assert (
            client.stack.deploy.call_args.kwargs["template_url"]
            == TEMPLATE_URLS["eu-central-1"]
        )

    def test_an_undeterminable_region_names_both_ways_to_supply_one(
        self, monkeypatch, api_calls
    ):
        """With no region anywhere, the error has to be actionable.

        ``conftest`` already points ``AWS_CONFIG_FILE`` at ``os.devnull``, so
        deleting the two environment variables leaves ``boto3`` with no source of
        a region at all — which is exactly the state a CI runner is in.
        """
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)
        patcher, client_cls, _client = _patched_client()
        try:
            result = _deploy("--stack-name", STACK, "--admin-email", EMAIL)
        finally:
            patcher.stop()
        assert result.exit_code == 1, result.output
        assert "Region could not be determined" in result.output
        assert "--region" in result.output
        assert "AWS_DEFAULT_REGION" in result.output
        client_cls.assert_not_called()
        assert api_calls.of("CreateStack") == []

    @pytest.mark.parametrize("region", sorted(TEMPLATE_URLS))
    def test_each_supported_region_resolves_to_its_own_template(self, region):
        """A copy-paste slip in ``TEMPLATE_URLS`` is invisible from one region.

        The bucket name and the S3 endpoint host both carry the region, so the
        assertion is that the resolved URL names *this* region — which is what a
        duplicated entry would break, while still returning a perfectly valid URL
        that deploys in the wrong place (or fails with a cross-region S3 error).
        """
        patcher, _client_cls, client = _patched_client()
        try:
            result = _deploy(
                "--stack-name", STACK, "--admin-email", EMAIL, "--region", region
            )
        finally:
            patcher.stop()
        assert result.exit_code == 0, result.output
        url = client.stack.deploy.call_args.kwargs["template_url"]
        assert url == TEMPLATE_URLS[region]
        assert url.count(region) == 2, url
        assert f"Using template for region: {region}" in result.output

    def test_an_explicit_template_url_wins_over_the_table(self):
        patcher, _client_cls, client = _patched_client()
        try:
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--region",
                REGION,
                "--template-url",
                "https://example.invalid/my-own.yaml",
            )
        finally:
            patcher.stop()
        assert result.exit_code == 0, result.output
        kwargs = client.stack.deploy.call_args.kwargs
        assert kwargs["template_url"] == "https://example.invalid/my-own.yaml"
        assert kwargs["template_path"] is None

    def test_a_local_template_file_is_passed_as_an_absolute_path(
        self, cfn_template_file
    ):
        """The SDK reads the file itself, and may be running from elsewhere."""
        path = cfn_template_file()
        patcher, _client_cls, client = _patched_client()
        try:
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--region",
                REGION,
                "--template-file",
                path,
            )
        finally:
            patcher.stop()
        assert result.exit_code == 0, result.output
        kwargs = client.stack.deploy.call_args.kwargs
        assert kwargs["template_path"] == path
        assert kwargs["template_path"].startswith("/")
        assert f"Using local template: {path}" in result.output


class TestAnOperationAlreadyInProgress:
    """A stack mid-operation is attached to, not deployed over.

    CloudFormation refuses a second operation on a stack that is already
    updating, so the command detects that state and switches to monitoring.
    ``IDPClient`` is patched because moto completes every operation instantly and
    cannot hold a stack in ``UPDATE_IN_PROGRESS``.

    The critical assertion is the negative one: while monitoring, no
    ``CreateStack`` or ``UpdateStack`` may be submitted at all.
    """

    OUTPUTS = {
        "ApplicationWebURL": "https://idp.example.invalid/",
        "S3InputBucketName": "in-bucket",
        "S3OutputBucketName": "out-bucket",
    }

    def _run(self, operation, monitor_result, *, analysis=None):
        in_progress = SimpleNamespace(
            operation=operation, status=f"{operation}_IN_PROGRESS"
        )
        patcher, _client_cls, client = _patched_client(in_progress=in_progress)
        client.stack.monitor.return_value = monitor_result
        if analysis is not None:
            client.stack.get_failure_analysis.return_value = analysis
        try:
            return (
                _deploy(
                    "--stack-name",
                    STACK,
                    "--admin-email",
                    EMAIL,
                    "--region",
                    REGION,
                ),
                client,
            )
        finally:
            patcher.stop()

    def test_monitoring_replaces_the_deploy_and_shows_the_outputs(self, api_calls):
        result, client = self._run(
            "UPDATE",
            SimpleNamespace(
                success=True,
                operation="UPDATE",
                status="UPDATE_COMPLETE",
                outputs=self.OUTPUTS,
                error=None,
            ),
        )
        assert result.exit_code == 0, result.output
        assert "has an operation in progress" in result.output
        assert "Switching to monitoring mode" in result.output
        client.stack.monitor.assert_called_once_with(operation="UPDATE")
        client.stack.deploy.assert_not_called()
        assert api_calls.of("CreateStack") == []
        assert api_calls.of("UpdateStack") == []
        assert "Important Outputs" in result.output
        assert "https://idp.example.invalid/" in result.output
        assert "Next Steps" in result.output

    def test_a_delete_in_progress_prints_no_outputs_block(self):
        """A deleted stack has no outputs to report, only the completion.

        The outputs of a stack that has just been deleted are either gone or
        stale, so printing an ``ApplicationWebURL`` for one would be actively
        misleading.
        """
        result, _client = self._run(
            "DELETE",
            SimpleNamespace(
                success=True,
                operation="DELETE",
                status="DELETE_COMPLETE",
                outputs=self.OUTPUTS,
                error=None,
            ),
        )
        assert result.exit_code == 0, result.output
        assert "Important Outputs" not in result.output
        assert "https://idp.example.invalid/" not in result.output

    def test_an_operation_with_no_outputs_skips_the_block_but_keeps_next_steps(self):
        result, _client = self._run(
            "CREATE",
            SimpleNamespace(
                success=True,
                operation="CREATE",
                status="CREATE_COMPLETE",
                outputs={},
                error=None,
            ),
        )
        assert result.exit_code == 0, result.output
        assert "Important Outputs" not in result.output
        assert "Next Steps" in result.output

    def test_a_failed_monitor_exits_1_with_the_root_cause(self):
        """Attaching to somebody else's failing update must not exit 0.

        A zero exit here would tell a calling script that the deployment
        succeeded when the operation it attached to rolled back.
        """
        analysis = SimpleNamespace(
            root_causes=[
                SimpleNamespace(
                    resource="OCRFunction",
                    resource_type="AWS::Lambda::Function",
                    reason="Resource handler returned message: limit exceeded",
                    stack_path="idp-test/PATTERNSTACK",
                )
            ],
            cascade_count=3,
        )
        result, client = self._run(
            "UPDATE",
            SimpleNamespace(
                success=False,
                operation="UPDATE",
                status="UPDATE_ROLLBACK_COMPLETE",
                outputs={},
                error="see events",
                deploy_start_time=None,
            ),
            analysis=analysis,
        )
        assert result.exit_code == 1, result.output
        assert "Stack UPDATE failed" in result.output
        assert "Root Cause Analysis" in result.output
        assert "idp-test/PATTERNSTACK → OCRFunction" in result.output
        assert "3 additional resource(s) cancelled" in result.output
        client.stack.deploy.assert_not_called()


#: Values a pre-existing stack was created with, for the update tests. Every
#: parameter the template declares must be given one: the SDK sends
#: ``UsePreviousValue`` for each parameter the stack already reports, and moto
#: resolves that against the values the create supplied rather than against the
#: template defaults.
PRE_EXISTING_PARAMETER_VALUES = {
    "AdminEmail": "original@example.invalid",
    "MaxConcurrentWorkflows": "200",
    "LogLevel": "ERROR",
    "ExternalIdPEmailMutable": "false",
}


def _create_stack_in_moto(template_path, stack_name=STACK):
    """Put a stack in place so the next deploy takes the UPDATE path.

    The parameter set is derived from the template rather than listed, so a
    parameter added to the shared ``cfn_template_file`` fixture does not turn
    these tests into a moto ``MissingParameterError`` that says nothing about the
    CLI.
    """
    with open(template_path, encoding="utf-8") as handle:
        body = handle.read()
    declared = json.loads(body)["Parameters"]
    boto3.client("cloudformation", region_name=REGION).create_stack(
        StackName=stack_name,
        TemplateBody=body,
        Capabilities=REQUIRED_CAPABILITIES,
        Parameters=[
            {
                "ParameterKey": name,
                "ParameterValue": PRE_EXISTING_PARAMETER_VALUES.get(name, "seed"),
            }
            for name in declared
        ],
    )


class TestCreateVersusUpdate:
    """``--admin-email`` is required to create a stack and optional to update one.

    Both halves run against moto with a real template so the distinction is
    drawn where the code draws it — ``client.stack.exists()`` — and so the
    resulting request can be read back.
    """

    def test_an_update_needs_no_admin_email_and_changes_only_what_was_asked(
        self, api_calls, cfn_template_file
    ):
        """Everything not named must go as ``UsePreviousValue``.

        This is the property that makes ``idp-cli deploy --log-level DEBUG``
        against a live stack safe: a parameter the user did not mention keeps the
        value the stack already has, rather than reverting to the template
        default.
        """
        path = cfn_template_file()
        with mock_aws():
            _create_stack_in_moto(path)
            result = _deploy(
                "--stack-name",
                STACK,
                "--log-level",
                "DEBUG",
                "--template-file",
                path,
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert "Updating existing IDP stack" in result.output
        call = api_calls.only("UpdateStack")
        assert _explicit(call) == {"LogLevel": "DEBUG"}
        assert "AdminEmail" in _use_previous(call)
        assert "MaxConcurrentWorkflows" in _use_previous(call)

    def test_an_update_echoes_an_admin_email_if_one_was_given(
        self, api_calls, cfn_template_file
    ):
        path = cfn_template_file()
        with mock_aws():
            _create_stack_in_moto(path)
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--template-file",
                path,
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert f"Admin Email: {EMAIL}" in result.output
        assert _explicit(api_calls.only("UpdateStack")) == {"AdminEmail": EMAIL}

    def test_a_create_says_so_and_echoes_the_admin_email(
        self, api_calls, cfn_template_file
    ):
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert "Creating new IDP stack" in result.output
        assert f"Admin Email: {EMAIL}" in result.output
        assert api_calls.of("UpdateStack") == []


class TestHeadlessOnCreate:
    """``--headless`` drops ``--admin-email`` rather than forwarding it.

    The headless template removes Cognito, so it declares no ``AdminEmail``
    parameter and CloudFormation would reject the create outright with a
    ValidationError. Dropping it with a warning is the behaviour; both halves are
    asserted, because a warning that still submits the parameter fixes nothing
    and a silent drop leaves the user expecting an admin invitation email.
    """

    def test_a_supplied_admin_email_is_dropped_with_a_warning(
        self, api_calls, cfn_template_file
    ):
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--headless",
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert "--admin-email is ignored with --headless" in result.output
        assert _explicit(api_calls.only("CreateStack")) == {}

    def test_headless_without_an_admin_email_is_accepted(
        self, api_calls, cfn_template_file
    ):
        """The create-path requirement does not apply: there is no admin user."""
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--headless",
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert "--admin-email is required" not in result.output
        assert _explicit(api_calls.only("CreateStack")) == {}


class TestFederatedEmailMutableDefault:
    """#835, end to end: a NEW federated stack gets ``ExternalIdPEmailMutable=true``.

    Cognito rewrites the IdP-mapped ``email`` attribute on every federated
    sign-in, so a pool created with the template default (``Mutable: false``)
    lets each federated user sign in exactly once. The flag is fixed at pool
    creation, which is why the template cannot default it and why the CLI — which
    knows create from update — supplies it on creates only.
    """

    def test_a_new_federated_stack_submits_the_flag_and_says_so(
        self, api_calls, cfn_template_file
    ):
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--parameters",
                "ExternalIdPType=OIDC",
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert _explicit(api_calls.only("CreateStack")) == {
            "AdminEmail": EMAIL,
            "ExternalIdPType": "OIDC",
            "ExternalIdPEmailMutable": "true",
        }
        assert "defaulting ExternalIdPEmailMutable=true" in result.output

    def test_an_explicit_value_is_left_alone(self, api_calls, cfn_template_file):
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--parameters",
                "ExternalIdPType=SAML,ExternalIdPEmailMutable=false",
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert (
            _explicit(api_calls.only("CreateStack"))["ExternalIdPEmailMutable"]
            == "false"
        )
        assert "defaulting ExternalIdPEmailMutable=true" not in result.output

    def test_an_update_never_injects_it(self, api_calls, cfn_template_file):
        """Flipping the flag on an existing pool fails the update.

        Worse than failing: a failed update on this parameter can leave the stack
        in ``UPDATE_ROLLBACK_FAILED``, which needs manual intervention. So the
        injection is create-only.
        """
        path = cfn_template_file()
        with mock_aws():
            _create_stack_in_moto(path)
            result = _deploy(
                "--stack-name",
                STACK,
                "--parameters",
                "ExternalIdPType=OIDC",
                "--template-file",
                path,
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert _explicit(api_calls.only("UpdateStack")) == {"ExternalIdPType": "OIDC"}
        assert "ExternalIdPEmailMutable" in _use_previous(api_calls.only("UpdateStack"))
        assert "defaulting ExternalIdPEmailMutable=true" not in result.output

    def test_headless_never_injects_it(self, api_calls, cfn_template_file):
        """The headless template strips Cognito and this parameter with it."""
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--headless",
                "--parameters",
                "ExternalIdPType=OIDC",
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert _explicit(api_calls.only("CreateStack")) == {"ExternalIdPType": "OIDC"}


class TestTheRequestShapeAroundParameters:
    """Capabilities, rollback and the service role: the rest of the request.

    These are not parameters, and each has a failure mode of its own — a missing
    capability flag fails the create at CloudFormation, a wrong
    ``DisableRollback`` destroys the evidence a failed create leaves behind, and
    a ``RoleARN`` sent when none was asked for changes which identity
    CloudFormation acts as.
    """

    def test_all_three_capabilities_are_always_declared(
        self, api_calls, cfn_template_file
    ):
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert api_calls.only("CreateStack").params["Capabilities"] == (
            REQUIRED_CAPABILITIES
        )

    def test_no_rollback_disables_rollback_on_the_create(
        self, api_calls, cfn_template_file
    ):
        """``--no-rollback`` is how a failed create keeps its failed resources.

        Without it CloudFormation deletes them on failure and the reason often
        goes with them; with it the stack stays in ``CREATE_FAILED`` and can be
        inspected.
        """
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--no-rollback",
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert api_calls.only("CreateStack").params["DisableRollback"] is True

    def test_without_the_flag_rollback_stays_enabled(
        self, api_calls, cfn_template_file
    ):
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert api_calls.only("CreateStack").params["DisableRollback"] is False

    def test_a_role_arn_is_submitted_when_given_and_omitted_when_not(
        self, api_calls, cfn_template_file
    ):
        """``RoleARN`` must be absent, not empty, when no role was asked for."""
        role = f"arn:aws:iam::123456789012:role/{STACK}-deployer"
        path = cfn_template_file()
        with mock_aws():
            with_role = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--role-arn",
                role,
                "--template-file",
                path,
                "--region",
                REGION,
            )
            assert with_role.exit_code == 0, with_role.output
            assert api_calls.only("CreateStack").params["RoleARN"] == role

        with mock_aws():
            without = _deploy(
                "--stack-name",
                f"{STACK}-2",
                "--admin-email",
                EMAIL,
                "--template-file",
                path,
                "--region",
                REGION,
            )
            assert without.exit_code == 0, without.output
        second = api_calls.of("CreateStack")[1]
        assert "RoleARN" not in second.params


class TestWaitAndResultReporting:
    """The two success shapes, and the failure one.

    Without ``--wait`` the SDK returns ``status="INITIATED"`` and the command
    reports an initiated deploy plus how to watch it; with ``--wait`` it returns
    a completed result and the command prints the stack outputs. Both are exit 0.
    A result that is neither exits 1 after a failure analysis.
    """

    def test_without_wait_the_command_reports_an_initiated_deploy(
        self, cfn_template_file
    ):
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert "initiated successfully" in result.output
        assert "Or use --wait flag to monitor in CLI" in result.output
        assert "Important Outputs" not in result.output

    def test_with_wait_the_command_prints_the_stack_outputs(self, cfn_template_file):
        """The three outputs a user needs next: the UI URL and both buckets."""
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--wait",
                "--template-file",
                cfn_template_file(),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert "completed successfully" in result.output
        assert "Important Outputs" in result.output
        assert "https://idp.example.invalid/" in result.output
        assert "input-bucket" in result.output
        assert "output-bucket" in result.output

    def test_outputs_the_stack_does_not_declare_are_reported_as_not_available(
        self, cfn_template_file
    ):
        """A headless or partial stack has no ``ApplicationWebURL``.

        The three lines are printed from ``outputs.get(name, "N/A")``, so a stack
        that declares some other output still gets the block — with ``N/A`` where
        a value is missing rather than a ``KeyError`` or a bare blank.
        """
        with mock_aws():
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--wait",
                "--template-file",
                cfn_template_file(outputs={"SomethingElse": "value"}),
                "--region",
                REGION,
            )
        assert result.exit_code == 0, result.output
        assert "Important Outputs" in result.output
        assert "Application URL: N/A" in result.output
        assert "Input Bucket: N/A" in result.output

    def test_a_completed_deploy_with_no_outputs_at_all_skips_the_block(self):
        """No outputs means no block — and still the Next Steps.

        Driven through a patched client because every template moto will accept
        here declares outputs; what is under test is the CLI's ``if outputs:``.
        """
        patcher, _client_cls, _client = _patched_client(
            deploy_result=SimpleNamespace(
                success=True,
                status="CREATE_COMPLETE",
                operation="CREATE",
                outputs={},
                error=None,
            )
        )
        try:
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--wait",
                "--region",
                REGION,
            )
        finally:
            patcher.stop()
        assert result.exit_code == 0, result.output
        assert "completed successfully" in result.output
        assert "Important Outputs" not in result.output
        assert "Next Steps" in result.output

    def test_a_failed_deploy_exits_1_and_reports_the_root_cause(self):
        """A failed deploy must not exit 0, and must say which resource failed.

        ``IDPClient`` is patched because moto has no way to fail a create: every
        stack it accepts reaches ``CREATE_COMPLETE``.
        """
        patcher, _client_cls, client = _patched_client(
            deploy_result=SimpleNamespace(
                success=False,
                status="ROLLBACK_COMPLETE",
                operation="CREATE",
                outputs={},
                error="one resource failed",
                deploy_start_time="2026-01-01T00:00:00Z",
            )
        )
        client.stack.get_failure_analysis.return_value = SimpleNamespace(
            root_causes=[
                SimpleNamespace(
                    resource="ConfigurationTable",
                    resource_type="AWS::DynamoDB::Table",
                    reason="Table already exists",
                    stack_path="",
                )
            ],
            cascade_count=0,
        )
        try:
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--region",
                REGION,
            )
        finally:
            patcher.stop()
        assert result.exit_code == 1, result.output
        assert "Stack CREATE failed" in result.output
        assert "ConfigurationTable (AWS::DynamoDB::Table)" in result.output
        assert "Table already exists" in result.output
        client.stack.get_failure_analysis.assert_called_once_with(
            STACK, deploy_start_time="2026-01-01T00:00:00Z"
        )


class TestFromCode:
    """``--from-code`` builds first, then deploys what the build produced.

    The build itself runs ``publish.py`` in a subprocess and is tested against
    ``_build_from_local_code`` directly in ``test_cli_module_helpers.py``. What
    is asserted here is the wiring: every build-related flag reaches the helper,
    and the template the helper returns is what gets deployed.
    """

    def test_the_build_flags_are_forwarded_and_its_template_is_deployed(self, tmp_path):
        source = tmp_path / "project"
        source.mkdir()
        patcher, _client_cls, client = _patched_client()
        try:
            with patch.object(
                cli_module,
                "_build_from_local_code",
                return_value=("/built/idp-main.yaml", "https://s3/built.yaml"),
            ) as build:
                result = _deploy(
                    "--stack-name",
                    STACK,
                    "--admin-email",
                    EMAIL,
                    "--region",
                    REGION,
                    "--from-code",
                    str(source),
                    "--bucket-basename",
                    "my-artifacts",
                    "--prefix",
                    "v1",
                    "--public",
                    "--build-max-workers",
                    "4",
                    "--clean-build",
                    "--no-validate-template",
                )
        finally:
            patcher.stop()
        assert result.exit_code == 0, result.output
        args, kwargs = build.call_args
        assert args == (str(source), REGION, STACK)
        assert kwargs == {
            "headless": False,
            "govcloud": False,
            "bucket_basename": "my-artifacts",
            "prefix": "v1",
            "public": True,
            "max_workers": 4,
            "clean_build": True,
            "no_validate": True,
        }
        deploy_kwargs = client.stack.deploy.call_args.kwargs
        assert deploy_kwargs["template_path"] == "/built/idp-main.yaml"
        assert deploy_kwargs["template_url"] == "https://s3/built.yaml"

    @pytest.mark.parametrize("flag", ["--headless", "--govcloud"])
    def test_the_variant_flag_reaches_the_build_rather_than_downloading(
        self, tmp_path, flag
    ):
        """With ``--from-code`` the variant is built, not downloaded and patched.

        The download-and-transform paths exist only for a pre-built template;
        this asserts that supplying source code takes the build path instead,
        which matters because the two produce the variant by different means.
        """
        source = tmp_path / "project"
        source.mkdir()
        patcher, _client_cls, _client = _patched_client()
        try:
            with patch.object(
                cli_module,
                "_build_from_local_code",
                return_value=("/built/variant.yaml", None),
            ) as build:
                result = _deploy(
                    "--stack-name",
                    STACK,
                    "--admin-email",
                    EMAIL,
                    "--region",
                    REGION,
                    "--from-code",
                    str(source),
                    flag,
                )
        finally:
            patcher.stop()
        assert result.exit_code == 0, result.output
        kwargs = build.call_args.kwargs
        assert kwargs["headless"] is (flag == "--headless")
        assert kwargs["govcloud"] is (flag == "--govcloud")


class TestPreBuiltVariantTemplates:
    """``--headless`` / ``--govcloud`` with no source: download, transform, upload.

    This path fetches the published template over HTTPS, transforms it through
    the SDK, uploads the result to a per-account artifacts bucket and deploys
    *that* URL. ``requests.get`` is patched (it is imported inside the function,
    so the module attribute is the seam) and STS plus S3 are moto-backed, so
    nothing leaves the machine.

    The assertion that matters is the uploaded URL, because it is constructed by
    string interpolation from the account id and region: a wrong bucket or key
    there does not fail here, it fails later inside CloudFormation with a
    template-not-found error that says nothing about the CLI.
    """

    ACCOUNT = "123456789012"

    def _expected_url(self, variant):
        bucket = f"idp-accelerator-artifacts-{self.ACCOUNT}-{REGION}"
        return f"https://s3.{REGION}.amazonaws.com/{bucket}/idp-cli/idp-{variant}.yaml"

    def _run(self, flag, variant, *, transform_succeeds=True):
        transformed_body = json.dumps({"Resources": {"T": {"Type": "AWS::SNS::Topic"}}})

        def _transform(*, source_template, output_path, **kwargs):
            # The real transform writes the output file, and the upload that
            # follows needs it to exist.
            with open(output_path, "w", encoding="utf-8") as handle:
                handle.write(transformed_body)
            return SimpleNamespace(
                success=transform_succeeds,
                error=None if transform_succeeds else "unsupported resource",
            )

        patcher, _client_cls, client = _patched_client()
        client.publish.transform_template_govcloud.side_effect = _transform
        client.publish.transform_template_headless.side_effect = _transform
        try:
            with mock_aws():
                boto3.client("s3", region_name=REGION).create_bucket(
                    Bucket=f"idp-accelerator-artifacts-{self.ACCOUNT}-{REGION}",
                    CreateBucketConfiguration={"LocationConstraint": REGION},
                )
                with patch("requests.get") as http_get:
                    http_get.return_value = SimpleNamespace(
                        content=b"Resources: {}\n",
                        raise_for_status=lambda: None,
                    )
                    result = _deploy(
                        "--stack-name",
                        STACK,
                        "--admin-email",
                        EMAIL,
                        "--region",
                        REGION,
                        flag,
                    )
            return result, client, http_get
        finally:
            patcher.stop()

    @pytest.mark.parametrize(
        ("flag", "variant"),
        [("--govcloud", "govcloud"), ("--headless", "headless")],
    )
    def test_the_uploaded_template_url_is_what_gets_deployed(self, flag, variant):
        result, client, http_get = self._run(flag, variant)
        assert result.exit_code == 0, result.output
        # The published template for the deploy region is what was fetched.
        assert http_get.call_args.args[0] == TEMPLATE_URLS[REGION]
        assert client.stack.deploy.call_args.kwargs["template_url"] == (
            self._expected_url(variant)
        )
        # template_path is deliberately left None: the transformed file lives in a
        # TemporaryDirectory that is gone by the time the deploy runs.
        assert client.stack.deploy.call_args.kwargs["template_path"] is None
        assert self._expected_url(variant) in result.output

    def test_the_govcloud_transform_lints_against_the_sdk_default_region(self):
        """The deploy region is commercial here, so the lint region is the default.

        ``cli.py`` lints against the deploy region only when it starts with
        ``us-gov-``, and that branch is unreachable from this path: the region
        must be one of the three in ``TEMPLATE_URLS``, none of which is a
        GovCloud region.
        """
        from idp_sdk.operations.publish import DEFAULT_GOVCLOUD_LINT_REGION

        result, client, _http_get = self._run("--govcloud", "govcloud")
        assert result.exit_code == 0, result.output
        kwargs = client.publish.transform_template_govcloud.call_args.kwargs
        assert kwargs["lint_region"] == DEFAULT_GOVCLOUD_LINT_REGION

    def test_the_headless_transform_is_not_told_to_rewrite_govcloud_config(self):
        result, client, _http_get = self._run("--headless", "headless")
        assert result.exit_code == 0, result.output
        kwargs = client.publish.transform_template_headless.call_args.kwargs
        assert kwargs["update_govcloud_config"] is False

    @pytest.mark.parametrize(
        ("flag", "expected"),
        [
            ("--govcloud", "GovCloud transformation failed"),
            ("--headless", "Headless transformation failed"),
        ],
    )
    def test_a_failed_transform_exits_1_without_deploying(self, flag, expected):
        result, client, _http_get = self._run(
            flag, flag.lstrip("-"), transform_succeeds=False
        )
        assert result.exit_code == 1, result.output
        assert expected in result.output
        assert "unsupported resource" in result.output
        client.stack.deploy.assert_not_called()


class TestErrorHandling:
    """The two outer handlers: a missing file, and anything else.

    Both exist so that a failure prints one line a user can act on instead of a
    traceback. The distinction is only in the wording — a ``FileNotFoundError``
    is reported as itself, everything else is prefixed ``Error:`` — and both exit
    1, which is what a calling script reads.
    """

    def test_a_missing_template_is_reported_without_a_traceback(self):
        patcher, _client_cls, client = _patched_client()
        client.stack.deploy.side_effect = FileNotFoundError(
            "Template not found: /gone/idp-main.yaml"
        )
        try:
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--region",
                REGION,
            )
        finally:
            patcher.stop()
        assert result.exit_code == 1, result.output
        assert "Template not found: /gone/idp-main.yaml" in result.output
        assert "Traceback" not in result.output

    def test_an_unexpected_exception_exits_1_with_its_message(self):
        patcher, _client_cls, client = _patched_client()
        client.stack.check_in_progress.side_effect = RuntimeError(
            "ExpiredToken: the security token included in the request is expired"
        )
        try:
            result = _deploy(
                "--stack-name",
                STACK,
                "--admin-email",
                EMAIL,
                "--region",
                REGION,
            )
        finally:
            patcher.stop()
        assert result.exit_code == 1, result.output
        assert "Error: ExpiredToken" in result.output
        client.stack.deploy.assert_not_called()


class TestTheCommandSurface:
    """The option surface itself, where a rename is a breaking change.

    Asserted as a set rather than one by one: a removed option breaks every
    script that passes it, and this is the cheapest place for that to show up.
    """

    def test_every_documented_option_is_still_accepted(self):
        from idp_cli.cli import deploy

        assert {p.name for p in deploy.params} == {
            "stack_name",
            "admin_email",
            "from_code",
            "template_url",
            "template_file",
            "max_concurrent",
            "log_level",
            "enable_hitl",
            "custom_config",
            "parameters",
            "tags",
            "wait",
            "no_rollback",
            "region",
            "role_arn",
            "headless",
            "govcloud",
            "bucket_basename",
            "prefix",
            "public",
            "build_max_workers",
            "clean_build",
            "no_validate_template",
        }

    def test_only_stack_name_is_required(self):
        """Everything else is optional because an update may change one thing.

        ``--admin-email`` in particular is required only on a create, and that is
        a runtime decision (it depends on whether the stack exists), so it cannot
        be expressed as a required option.
        """
        from idp_cli.cli import deploy

        assert {p.name for p in deploy.params if p.required} == {"stack_name"}

    def test_the_parameters_regex_is_the_one_these_tests_pin(self):
        """Guard the premise of the two #1220 tests above.

        They assert a consequence of one specific pattern. If the pattern is
        changed the consequence may no longer follow, and a reader needs to be
        sent here rather than left puzzled by a passing test that no longer
        measures anything.
        """
        import inspect

        source = inspect.getsource(cli_module.deploy.callback)
        assert r"([A-Za-z][A-Za-z0-9]*)=((?:(?![A-Za-z][A-Za-z0-9]*=).)*)" in source, (
            "the --parameters pattern changed; re-check the two #1220 pins above"
        )
        # And the two shapes, measured on the pattern directly.
        pattern = r"([A-Za-z][A-Za-z0-9]*)=((?:(?![A-Za-z][A-Za-z0-9]*=).)*)"
        assert re.findall(pattern, "MaxConcurrentWorkflows = 200") == []
        assert re.findall(pattern, "Log_Level=DEBUG") == [("Level", "DEBUG")]
