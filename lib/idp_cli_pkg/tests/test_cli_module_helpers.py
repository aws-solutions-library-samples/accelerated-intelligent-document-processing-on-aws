# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
The module-level helpers ``idp_cli/cli.py`` builds its commands out of.

Five pieces of machinery live above the first ``@cli.command()`` and are shared
by everything below it, so each of them is wrong for every command at once:

``emit_raw`` / ``emit_json``
    The stdout seam for machine-readable payloads. Their contract — write the
    payload unrendered, never through Rich — is covered behaviourally in
    ``test_machine_readable_output.py``; what is added here is the serialization
    edge that nothing else reaches, a payload holding a value ``json.dumps``
    cannot encode on its own.

``_build_from_local_code``
    Runs a publish build and answers with the template to deploy. It has three
    return paths (plain, headless, GovCloud) chosen by flags, and three exits. A
    wrong choice here does not fail — it deploys a correct template that is the
    wrong *variant* — so the GovCloud case, where the wrong variant means
    CloudFront resources in a partition that has none, is an exit rather than a
    choice: a ``--govcloud`` build that produced no GovCloud template refuses.

``_display_deployment_failure``
    The only thing a user sees when a deploy fails. It accepts either a result
    object or a plain dict (the two shapes the SDK returns across code paths) and
    it must never raise, because it runs on the path where something already went
    wrong: an exception here replaces a diagnosis with a traceback.

``_default_email_mutable_for_new_federated_stack``
    The #835 fix, which supplies a Cognito flag that can only be set at User Pool
    creation. Its whole value is in *not* acting — on an update, on headless, on a
    non-federated stack, or when the caller chose a value.

``_parse_tags`` and ``TEMPLATE_URLS``
    A parser whose input comes straight from a shell argument, and a three-entry
    table where a copy-paste slip is invisible unless every entry is checked.

The one test that is not about a helper is the tier-1 import guard at the top of
the module. ``click``, ``boto3`` and ``rich`` are used by the decorators at module
scope, so they cannot be stubbed to ``None`` the way ``IDPClient`` is — a stub
would crash inside ``@click.group()`` with an ``AttributeError`` about
``NoneType`` and bury the real cause. The module therefore prints the setup help
and exits, and that is asserted by executing ``cli.py`` again from its own file
with one of the three blocked, which records the behaviour without disturbing the
``idp_cli.cli`` already imported into this process.
"""

import builtins
import importlib.util
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import click
import pytest

from idp_cli import cli as cli_module
from idp_cli.cli import (
    TEMPLATE_URLS,
    _build_from_local_code,
    _default_email_mutable_for_new_federated_stack,
    _display_deployment_failure,
    _parse_tags,
    emit_json,
    emit_raw,
)


class TestTheTierOneImportGuard:
    """A missing ``click`` / ``boto3`` / ``rich`` must explain itself and exit 1.

    These three are used at module scope, so the module cannot finish importing
    without them and there is no handler left to run later. Exiting here with the
    ``make setup`` instructions is the only place the real cause is still known.
    """

    @staticmethod
    def _exec_cli_with_blocked(package, monkeypatch):
        """Execute ``cli.py`` from its own file with ``package`` unimportable.

        ``sys.modules`` is deliberately left alone: the module object is thrown
        away, so the ``idp_cli.cli`` this suite is otherwise testing — and the
        console the autouse fixture pinned on it — are untouched.
        """
        real_import = builtins.__import__

        def guarded(name, globals=None, locals=None, fromlist=(), level=0):
            if name == package or name.startswith(f"{package}."):
                raise ImportError(f"No module named '{package}'")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", guarded)
        spec = importlib.util.spec_from_file_location(
            "idp_cli._tier_one_import_probe", cli_module.__file__
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

    @pytest.mark.parametrize("package", ["boto3", "click", "rich"])
    def test_a_missing_tier_one_dependency_exits_1_with_the_setup_help(
        self, package, monkeypatch, capsys
    ):
        with pytest.raises(SystemExit) as exit_info:
            self._exec_cli_with_blocked(package, monkeypatch)
        assert exit_info.value.code == 1
        err = capsys.readouterr().err
        # The remedy, and the actual cause underneath it.
        assert "make setup" in err
        assert "docs/idp-cli.md" in err
        assert f"Underlying import error: No module named '{package}'" in err
        # And not the misleading symptom this replaced.
        assert "NoneType" not in err

    def test_the_guard_does_not_disturb_the_already_imported_module(
        self, monkeypatch, capsys
    ):
        """Running the probe must leave this process's ``idp_cli.cli`` intact.

        If it did not, every test after it in the session would be measuring a
        half-executed module — which is the failure mode that makes an
        import-behaviour test expensive rather than useful.
        """
        import sys

        before = sys.modules["idp_cli.cli"]
        with pytest.raises(SystemExit):
            self._exec_cli_with_blocked("rich", monkeypatch)
        capsys.readouterr()
        assert sys.modules["idp_cli.cli"] is before
        assert "idp_cli._tier_one_import_probe" not in sys.modules
        assert cli_module.click is not None
        assert cli_module.console is not None


class TestEmitHelpers:
    """The stdout seam for payloads a consumer parses.

    The behavioural guarantees (no ANSI, no wrapping, no markup interpretation)
    are asserted in ``test_machine_readable_output.py`` against the commands that
    use these. What is here is the serialization contract itself.
    """

    def test_a_value_json_cannot_encode_is_stringified_rather_than_raising(
        self, capsys
    ):
        """A ``datetime`` in a payload must not take the command down.

        ``emit_json`` passes ``default=str``, so a stack's ``deploy_start_time``
        or a DynamoDB timestamp serializes instead of raising ``TypeError`` from
        inside a success path — where the user has already got what they asked
        for and only the printing is left.
        """
        moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        emit_json({"started": moment, "count": 2})
        payload = json.loads(capsys.readouterr().out)
        assert payload == {"started": str(moment), "count": 2}

    def test_an_object_payload_is_indented_for_a_human_reading_the_same_stream(
        self, capsys
    ):
        """Machine-readable and unreadable are not the same thing.

        The payload is pretty-printed at two spaces, so the same output serves
        ``| jq`` and a person who ran the command without a pipe.
        """
        emit_json({"a": {"b": 1}})
        out = capsys.readouterr().out
        assert out == '{\n  "a": {\n    "b": 1\n  }\n}\n'

    def test_emit_raw_adds_exactly_one_trailing_newline(self, capsys):
        emit_raw("one line")
        assert capsys.readouterr().out == "one line\n"


def _build_result(**overrides):
    """A publish-build result with every variant field explicitly absent.

    A ``MagicMock`` is the wrong tool here: every attribute of one is truthy, so
    ``result.govcloud_template_path`` would be a path in every test and the flag
    precedence under test would be unobservable.
    """
    fields = {
        "success": True,
        "error": None,
        "template_path": "/build/idp-main.yaml",
        "template_url": "https://s3.example.invalid/idp-main.yaml",
        "headless_template_path": None,
        "headless_template_url": None,
        "govcloud_template_path": None,
        "govcloud_template_url": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class TestBuildFromLocalCode:
    """Which template a ``--from-code`` build hands back, and when it exits.

    ``IDPClient`` is patched: the build itself shells out to ``publish.py``,
    uploads artifacts and can take minutes, none of which is what this helper
    decides. What it decides is the variant, and the three return paths are
    chosen by two flags plus what the build actually produced.
    """

    @staticmethod
    def _run(result, **kwargs):
        with patch.object(cli_module, "IDPClient") as client_cls:
            client = client_cls.return_value
            client.publish.build.return_value = result
            returned = _build_from_local_code(
                "/src/project", "us-west-2", "s", **kwargs
            )
        return returned, client_cls, client

    def test_the_plain_template_is_returned_and_every_flag_is_forwarded(self, capsys):
        """The build's options are the helper's kwargs, one for one.

        Asserted as an exact kwargs dict because a dropped one is silent: a
        ``clean_build`` that never arrives produces a stale build that deploys,
        and a ``no_validate`` that never arrives makes the build slower rather
        than failing.
        """
        returned, client_cls, client = self._run(
            _build_result(),
            bucket_basename="my-artifacts",
            prefix="v1",
            public=True,
            max_workers=4,
            clean_build=True,
            no_validate=True,
            verbose=True,
            lint=False,
        )
        assert returned == (
            "/build/idp-main.yaml",
            "https://s3.example.invalid/idp-main.yaml",
        )
        client_cls.assert_called_once_with(region="us-west-2")
        assert client.publish.build.call_args.kwargs == {
            "source_dir": "/src/project",
            "bucket": "my-artifacts",
            "prefix": "v1",
            "region": "us-west-2",
            "headless": False,
            "govcloud": False,
            "public": True,
            "max_workers": 4,
            "clean_build": True,
            "no_validate": True,
            "verbose": True,
            "lint": False,
        }
        out = capsys.readouterr().out
        assert "Building project from source" in out
        assert "Source: /src/project" in out
        assert "Bucket: my-artifacts" in out
        assert "Prefix: v1" in out
        assert "Build complete. Template: /build/idp-main.yaml" in out

    def test_headless_returns_the_headless_variant(self, capsys):
        returned, _client_cls, _client = self._run(
            _build_result(
                headless_template_path="/build/idp-main-headless.yaml",
                headless_template_url="https://s3.example.invalid/headless.yaml",
            ),
            headless=True,
        )
        assert returned == (
            "/build/idp-main-headless.yaml",
            "https://s3.example.invalid/headless.yaml",
        )
        out = capsys.readouterr().out
        assert "Mode: headless" in out
        assert "Build complete (headless)" in out

    def test_govcloud_returns_the_govcloud_variant(self, capsys):
        returned, _client_cls, _client = self._run(
            _build_result(
                govcloud_template_path="/build/idp-main-govcloud.yaml",
                govcloud_template_url="https://s3.example.invalid/govcloud.yaml",
            ),
            govcloud=True,
        )
        assert returned == (
            "/build/idp-main-govcloud.yaml",
            "https://s3.example.invalid/govcloud.yaml",
        )
        assert "Build complete (GovCloud)" in capsys.readouterr().out

    def test_govcloud_wins_when_both_variants_were_built(self):
        """Precedence, for completeness rather than because it should happen.

        ``deploy`` refuses ``--headless --govcloud`` together, so this
        combination is not reachable from the CLI; the helper is importable and
        its own answer is GovCloud, because that check comes first.
        """
        returned, _client_cls, _client = self._run(
            _build_result(
                headless_template_path="/build/headless.yaml",
                headless_template_url="https://s3.example.invalid/headless.yaml",
                govcloud_template_path="/build/govcloud.yaml",
                govcloud_template_url="https://s3.example.invalid/govcloud.yaml",
            ),
            headless=True,
            govcloud=True,
        )
        assert returned[0] == "/build/govcloud.yaml"

    def test_govcloud_refuses_when_the_build_produced_no_govcloud_variant(self, capsys):
        """No GovCloud variant means no deploy — not the commercial template.

        The refusal is the whole point, so both halves of it are asserted: the
        exit status, and a message that names the variant that is missing. A test
        that only checked "nothing was deployed" would pass on any unrelated
        failure, and a message reading only "variant not available" would leave
        the operator with nothing to do next — so the two commands that produce
        and then deploy the transformed template are asserted as well.

        Falling back here would deploy a template whose ``AWS::CloudFront::*``
        resources do not exist in a GovCloud partition, and the first sign of it
        would be a CloudFormation failure minutes in, on a resource that looks
        unrelated to the flag that was ignored. Issue #1233.
        """
        with pytest.raises(SystemExit) as exit_info:
            self._run(_build_result(), govcloud=True)
        assert exit_info.value.code == 1

        out = capsys.readouterr().out
        assert "--govcloud was requested but the build did not produce" in out
        assert "GovCloud template variant" in out
        assert "(/src/project/.aws-sam/idp-govcloud.yaml)" in out
        # And it never claims to have built or chosen anything.
        assert "Build complete" not in out
        # Actionable: how to produce the variant, and how to deploy it once made.
        assert (
            "idp-cli publish --source-dir /src/project --region us-west-2 --govcloud"
            in out
        )
        assert "--template-file /src/project/.aws-sam/idp-govcloud.yaml" in out

    def test_a_headless_build_without_the_variant_falls_back_to_the_plain_template(
        self, capsys
    ):
        """The headless fallback is unchanged, and still silent.

        ``--headless`` is returned only ``if headless and
        result.headless_template_path``, so a build that succeeded without
        emitting the variant deploys the ordinary full-UI template with the
        ordinary "Build complete" line. Pinned as current behaviour rather than
        endorsed: unlike the GovCloud case above, the result is a working stack
        in the right partition with more in it than was asked for, and whether
        that should refuse or warn is a product decision that is not taken here.
        """
        returned, _client_cls, _client = self._run(_build_result(), headless=True)
        assert returned == (
            "/build/idp-main.yaml",
            "https://s3.example.invalid/idp-main.yaml",
        )
        out = capsys.readouterr().out
        assert "Build complete. Template: /build/idp-main.yaml" in out
        assert "(headless)" not in out

    @pytest.mark.parametrize(
        "flags", [{}, {"govcloud": True}], ids=["plain", "govcloud"]
    )
    def test_a_failed_build_exits_1_with_the_build_error(self, flags, capsys):
        """And is not re-reported as an unexpected exception, or as a refusal.

        ``sys.exit`` raises ``SystemExit``, which is not an ``Exception``, so it
        passes through the surrounding ``except Exception`` rather than being
        relabelled "Error during build" — which would hide which of the two
        failure modes happened.

        The ``govcloud`` case is the same assertion about the *other* message
        that can now be printed instead. A failed build produces no variant, so
        both conditions hold at once and only the order of the two checks decides
        which the operator is told about; the build error is the one that says
        what went wrong, and reporting the missing variant in its place would
        send them off to re-run the transform over a build that never finished.
        """
        with pytest.raises(SystemExit) as exit_info:
            self._run(_build_result(success=False, error="sam build failed"), **flags)
        assert exit_info.value.code == 1
        out = capsys.readouterr().out
        assert "Build failed: sam build failed" in out
        assert "Error during build" not in out
        assert "--govcloud was requested" not in out

    def test_an_exception_inside_the_build_exits_1_with_its_message(self, capsys):
        with patch.object(cli_module, "IDPClient") as client_cls:
            client_cls.return_value.publish.build.side_effect = RuntimeError(
                "docker daemon is not running"
            )
            with pytest.raises(SystemExit) as exit_info:
                _build_from_local_code("/src/project", "us-west-2", "s")
        assert exit_info.value.code == 1
        assert (
            "Error during build: docker daemon is not running"
            in capsys.readouterr().out
        )


class TestDisplayDeploymentFailure:
    """The diagnosis a failed deploy prints — from an object or from a dict.

    Both shapes are real: ``client.stack.deploy`` returns a
    ``StackDeploymentResult``, ``client.stack.monitor`` a ``StackMonitorResult``,
    and the SDK's internals pass plain dicts around, so this helper does a
    ``hasattr`` dance over ``operation`` / ``status`` / ``error``. It also must
    not raise for any of them: this runs after something has already failed, and
    an exception here costs the user the reason.
    """

    @staticmethod
    def _cause(**overrides):
        fields = {
            "resource": "OCRFunction",
            "resource_type": "AWS::Lambda::Function",
            "reason": "Resource creation cancelled",
            "stack_path": "idp-test/PATTERNSTACK",
        }
        fields.update(overrides)
        return SimpleNamespace(**fields)

    def _client(self, analysis):
        client = MagicMock()
        if isinstance(analysis, Exception):
            client.stack.get_failure_analysis.side_effect = analysis
        else:
            client.stack.get_failure_analysis.return_value = analysis
        return client

    def test_a_result_object_with_several_root_causes_and_cascades(self, capsys):
        client = self._client(
            SimpleNamespace(
                root_causes=[
                    self._cause(),
                    self._cause(
                        resource="ExtractionFunction",
                        reason="Rate exceeded",
                        stack_path="",
                    ),
                ],
                cascade_count=7,
            )
        )
        started = datetime(2026, 1, 2, tzinfo=timezone.utc)
        _display_deployment_failure(
            client,
            "idp-test",
            SimpleNamespace(
                operation="CREATE",
                status="ROLLBACK_COMPLETE",
                error="see events",
                deploy_start_time=started,
            ),
        )
        out = capsys.readouterr().out
        assert "Stack CREATE failed" in out
        assert "Status: ROLLBACK_COMPLETE" in out
        # A nested failure names the path to it; a top-level one names only itself.
        assert "idp-test/PATTERNSTACK → OCRFunction (AWS::Lambda::Function)" in out
        assert "ExtractionFunction (AWS::Lambda::Function)" in out
        assert "idp-test/PATTERNSTACK → ExtractionFunction" not in out
        assert "7 additional resource(s) cancelled" in out
        # The events are filtered to this deployment, so a previous failure's
        # events cannot be reported as this one's cause.
        client.stack.get_failure_analysis.assert_called_once_with(
            "idp-test", deploy_start_time=started
        )

    def test_a_cause_with_no_resource_type_prints_no_empty_parentheses(self, capsys):
        client = self._client(
            SimpleNamespace(
                root_causes=[self._cause(resource_type="", stack_path="")],
                cascade_count=0,
            )
        )
        _display_deployment_failure(
            client,
            "idp-test",
            SimpleNamespace(
                operation="UPDATE",
                status="UPDATE_FAILED",
                error=None,
                deploy_start_time=None,
            ),
        )
        out = capsys.readouterr().out
        assert "OCRFunction" in out
        assert "()" not in out
        # cascade_count 0 means no cascade line at all, not "0 additional".
        assert "additional resource(s)" not in out

    def test_a_dict_result_is_read_through_get_rather_than_attributes(self, capsys):
        """The dict shape must produce the same diagnosis, not ``UNKNOWN``.

        ``hasattr(dict, "operation")`` is False, so every field falls through to
        ``.get`` — including ``deploy_start_time``, which is only looked up on a
        dict and would otherwise silently become ``None`` and let stale events
        from a previous deployment be reported as this one's cause.
        """
        started = datetime(2026, 3, 4, tzinfo=timezone.utc)
        client = self._client(
            SimpleNamespace(root_causes=[self._cause()], cascade_count=0)
        )
        _display_deployment_failure(
            client,
            "idp-test",
            {
                "operation": "UPDATE",
                "status": "UPDATE_ROLLBACK_COMPLETE",
                "error": "one resource failed",
                "deploy_start_time": started,
            },
        )
        out = capsys.readouterr().out
        assert "Stack UPDATE failed" in out
        assert "Status: UPDATE_ROLLBACK_COMPLETE" in out
        assert "UNKNOWN" not in out
        client.stack.get_failure_analysis.assert_called_once_with(
            "idp-test", deploy_start_time=started
        )

    def test_a_dict_missing_every_field_falls_back_to_unknown(self, capsys):
        client = self._client(SimpleNamespace(root_causes=[], cascade_count=0))
        _display_deployment_failure(client, "idp-test", {})
        out = capsys.readouterr().out
        assert "Stack UNKNOWN failed" in out
        assert "Status: UNKNOWN" in out
        assert "Error: Unknown" in out

    def test_no_root_causes_falls_back_to_the_plain_error(self, capsys):
        """An analysis that finds nothing must still print what is known.

        This is the common case for a failure CloudFormation reports on the stack
        itself — a template validation error, an insufficient-capabilities
        refusal — where there are no resource-level events to analyse.
        """
        client = self._client(SimpleNamespace(root_causes=[], cascade_count=0))
        _display_deployment_failure(
            client,
            "idp-test",
            SimpleNamespace(
                operation="CREATE",
                status="ROLLBACK_COMPLETE",
                error="Requires capabilities: [CAPABILITY_NAMED_IAM]",
                deploy_start_time=None,
            ),
        )
        out = capsys.readouterr().out
        assert "Root Cause Analysis" not in out
        assert "Error: Requires capabilities: [CAPABILITY_NAMED_IAM]" in out

    def test_an_analysis_that_raises_still_reports_the_failure(self, capsys):
        """The analysis is best-effort; the failure report is not optional.

        ``get_failure_analysis`` walks nested stacks and can fail on its own
        (throttling, a stack deleted under it, missing permissions). If that
        exception escaped, a deploy failure would print a traceback instead of a
        reason and the exit code would be the only signal left.
        """
        client = self._client(RuntimeError("Rate exceeded"))
        _display_deployment_failure(
            client,
            "idp-test",
            SimpleNamespace(
                operation="CREATE",
                status="CREATE_FAILED",
                error="the original failure",
                deploy_start_time=None,
            ),
        )
        out = capsys.readouterr().out
        assert "Stack CREATE failed" in out
        assert "Error: the original failure" in out
        assert "Rate exceeded" not in out

    def test_an_object_without_a_deploy_start_time_passes_none(self, capsys):
        """The attribute is genuinely optional on the older result shapes."""
        client = self._client(SimpleNamespace(root_causes=[], cascade_count=0))
        _display_deployment_failure(
            client,
            "idp-test",
            SimpleNamespace(operation="CREATE", status="CREATE_FAILED", error="x"),
        )
        capsys.readouterr()
        client.stack.get_failure_analysis.assert_called_once_with(
            "idp-test", deploy_start_time=None
        )


class TestDefaultEmailMutableForNewFederatedStack:
    """#835: supply the Cognito flag on a create, and never otherwise.

    Cognito rewrites the IdP-mapped ``email`` attribute on every federated
    sign-in, so a User Pool created with ``Mutable: false`` lets each federated
    user sign in exactly once. The flag is fixed at pool creation, so the template
    cannot default it to ``true`` without breaking the next update of every
    existing stack — and the CLI, which knows create from update, supplies it for
    a new stack only.
    """

    @pytest.mark.parametrize(
        ("params", "stack_exists", "headless", "expected"),
        [
            ({"ExternalIdPType": "OIDC"}, False, False, "true"),
            ({"ExternalIdPType": "OIDC"}, True, False, None),
            ({"ExternalIdPType": "OIDC"}, False, True, None),
            ({}, False, False, None),
            ({"ExternalIdPType": ""}, False, False, None),
            (
                {"ExternalIdPType": "SAML", "ExternalIdPEmailMutable": "false"},
                False,
                False,
                None,
            ),
        ],
        ids=[
            "new-federated",
            "update",
            "headless",
            "not-federated",
            "empty-idp-type",
            "caller-chose",
        ],
    )
    def test_the_five_ways_of_not_acting_and_the_one_way_of_acting(
        self, params, stack_exists, headless, expected
    ):
        assert (
            _default_email_mutable_for_new_federated_stack(
                params, stack_exists=stack_exists, headless=headless
            )
            == expected
        )

    def test_it_mutates_the_caller_s_dict_in_place(self):
        """The caller deploys ``additional_params``, not the return value.

        ``deploy`` uses the return value only to decide whether to print the
        warning; the parameter that actually reaches CloudFormation is the one
        written into the dict here. A version that returned a new dict would warn
        correctly and deploy nothing.
        """
        params = {"ExternalIdPType": "OIDC", "ExternalIdPName": "Okta"}
        _default_email_mutable_for_new_federated_stack(
            params, stack_exists=False, headless=False
        )
        assert params == {
            "ExternalIdPType": "OIDC",
            "ExternalIdPName": "Okta",
            "ExternalIdPEmailMutable": "true",
        }

    def test_an_explicit_false_survives_untouched(self):
        params = {"ExternalIdPType": "SAML", "ExternalIdPEmailMutable": "false"}
        _default_email_mutable_for_new_federated_stack(
            params, stack_exists=False, headless=False
        )
        assert params["ExternalIdPEmailMutable"] == "false"


class TestParseTags:
    """``--tags`` is split on commas and then on the first ``=``, not by a regex.

    Tag keys are far more permissive than CloudFormation parameter names — spaces
    and ``. : / + - _`` are all legal, and ``aws:``-prefixed keys are ordinary —
    so the ``--parameters`` approach of matching a key pattern would silently drop
    most real tags. The cost of that permissiveness is that a malformed segment
    cannot be told apart from a value, which is why the two malformed shapes
    raise instead.
    """

    def test_a_key_may_contain_spaces_and_punctuation(self):
        parsed = _parse_tags("Cost Center=1234,aws:created.by/team+role-x_y=platform")
        assert parsed == {
            "Cost Center": "1234",
            "aws:created.by/team+role-x_y": "platform",
        }

    def test_only_the_first_equals_splits_a_pair(self):
        """A value may legitimately contain ``=`` — a filter, a base64 pad."""
        assert _parse_tags("Expression=a=b=c") == {"Expression": "a=b=c"}

    def test_surrounding_whitespace_is_trimmed_from_both_halves(self):
        assert _parse_tags("  Owner = docs-team  ") == {"Owner": "docs-team"}

    def test_empty_and_whitespace_only_segments_are_skipped(self):
        """A trailing comma or a double comma is a typo, not a failure.

        Skipping them keeps ``--tags "$TAGS,"`` working when ``$TAGS`` is built up
        in a shell loop, which is the shape that produces them.
        """
        assert _parse_tags("A=1,, ,B=2,") == {"A": "1", "B": "2"}

    def test_a_segment_with_no_equals_is_refused(self):
        with pytest.raises(click.BadParameter) as exc_info:
            _parse_tags("Owner=docs-team,JustAKey")
        assert "JustAKey" in str(exc_info.value)

    def test_an_empty_key_is_refused(self):
        """``=value`` would otherwise create a tag CloudFormation rejects later."""
        with pytest.raises(click.BadParameter) as exc_info:
            _parse_tags("=orphaned")
        assert "empty key" in str(exc_info.value)

    def test_an_empty_value_is_allowed(self):
        """An empty tag value is legal in CloudFormation, unlike an empty key."""
        assert _parse_tags("Owner=") == {"Owner": ""}

    @pytest.mark.parametrize("empty", [None, "", "   ", ",", " , "])
    def test_nothing_in_means_no_tags_out(self, empty):
        """An empty result must be ``{}`` so ``deploy`` sends no ``Tags`` key.

        ``deploy`` passes ``tags_dict or None``, and the SDK omits ``Tags``
        entirely for ``None`` — which is what stops an update from deleting the
        stack's existing tags.
        """
        assert _parse_tags(empty) == {}


class TestTemplateUrlTable:
    """The three regions with a published template, one entry each.

    Every entry is checked rather than a representative one: the values differ
    only by the region substring, which appears twice in each, so a duplicated
    line is a perfectly valid URL that deploys the wrong region's artifact or
    fails with a cross-region S3 error that names neither the CLI nor this table.
    """

    def test_exactly_the_three_published_regions_are_listed(self):
        assert set(TEMPLATE_URLS) == {"us-west-2", "us-east-1", "eu-central-1"}

    @pytest.mark.parametrize("region", sorted(TEMPLATE_URLS))
    def test_each_url_names_its_own_region_in_both_places(self, region):
        url = TEMPLATE_URLS[region]
        assert url == (
            f"https://s3.{region}.amazonaws.com/aws-ml-blog-{region}"
            "/artifacts/genai-idp/idp-main.yaml"
        )
        for other in TEMPLATE_URLS:
            if other != region:
                assert other not in url

    def test_the_urls_are_distinct(self):
        assert len(set(TEMPLATE_URLS.values())) == len(TEMPLATE_URLS)
