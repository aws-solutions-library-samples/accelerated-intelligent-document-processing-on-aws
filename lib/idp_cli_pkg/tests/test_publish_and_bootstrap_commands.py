# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for `idp-cli publish`, `idp-cli chat` and `idp-cli bootstrap`.

These three commands share a shape: each is a thin CLI layer over something
expensive — a full SAM build, a live multi-agent chat session, a Bedrock
authoring call — so the layer's whole job is translating options into arguments
and translating a result back into output and an exit code. That is what these
tests check, and they never let the expensive thing run.

What shaped them:

- **`publish` has twelve options and passes eleven of them through to one call.**
  An option that silently fails to reach `publish.build` produces the wrong
  artifact, uploaded to the wrong place, with a green terminal — so each flag is
  asserted at the call boundary by keyword, and the defaults are asserted too.
  The default matters as much as the override: `--lint/--no-lint` defaults to
  `True`, and a build that quietly skipped linting would look identical.
- **`bootstrap` has two modes that differ in which stream carries what.** Without
  `--stack-name` it prints a JSON schema to stdout and its progress lines to
  stderr, so `idp-cli bootstrap -p "..." > schema.json` yields parseable JSON;
  with `--stack-name` nothing machine-readable reaches stdout and the progress
  lines stay there. `CliRunner` combines the two streams by default, so the tests
  that care read `result.stdout` specifically — asserting on the combined text
  cannot tell the two modes apart, which is the bug the split exists to prevent.
- **`chat` is argument forwarding and one refusal.** `idp_cli/chat.py` itself is
  covered elsewhere; here the question is only whether the four options arrive
  and what happens when the optional agents dependency is absent.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from idp_cli import cli as cli_module


@pytest.fixture
def runner():
    return CliRunner()


def _publish_result(**overrides):
    """A `PublishResult` as `client.publish.build` really returns one.

    Built from the real pydantic model rather than a `MagicMock` so that a field
    the command reads but the model does not have fails here instead of silently
    yielding a `MagicMock` in the output.
    """
    from idp_sdk.models.publish import PublishResult

    fields = {
        "success": True,
        "template_url": "https://s3.us-east-1.amazonaws.com/b/idp-main.yaml",
        # A path-shaped value only; nothing reads or creates it. Deliberately not
        # under /tmp, which both ruff (S108) and bandit (B108) flag on sight.
        "template_path": "build/idp-main.yaml",
        "bucket": "b-us-east-1",
        "prefix": "idp-cli",
        "version": "0.6.9",
    }
    fields.update(overrides)
    return PublishResult(**fields)


class TestPublishOptionTranslation:
    """Every `publish` option must arrive at `client.publish.build` intact."""

    def test_defaults_are_passed_explicitly(self, runner, tmp_path):
        """
        The defaults are part of the contract, not an absence.

        `lint` defaults to True and `no_validate`/`public`/`clean_build` to False.
        A build that silently skipped linting or validation would produce the same
        console output as one that did not, so the defaults are asserted at the
        boundary rather than assumed.
        """
        client = MagicMock()
        client.publish.build.return_value = _publish_result()
        with patch.object(cli_module, "IDPClient", return_value=client) as factory:
            result = runner.invoke(
                cli_module.cli,
                ["publish", "--source-dir", str(tmp_path), "--region", "us-east-1"],
            )

        assert result.exit_code == 0, result.output
        factory.assert_called_once_with(region="us-east-1")
        kwargs = client.publish.build.call_args.kwargs
        assert kwargs == {
            "source_dir": str(tmp_path),
            "bucket": None,
            "prefix": None,
            "region": "us-east-1",
            "headless": False,
            "govcloud": False,
            "public": False,
            "max_workers": None,
            "clean_build": False,
            "no_validate": False,
            "verbose": False,
            "lint": True,
        }

    def test_every_flag_reaches_the_build_call(self, runner, tmp_path):
        client = MagicMock()
        client.publish.build.return_value = _publish_result()
        with patch.object(cli_module, "IDPClient", return_value=client):
            result = runner.invoke(
                cli_module.cli,
                [
                    "publish",
                    "--source-dir",
                    str(tmp_path),
                    "--region",
                    "eu-central-1",
                    "--bucket-basename",
                    "my-artifacts",
                    "--prefix",
                    "v1",
                    "--headless",
                    "--govcloud",
                    "--public",
                    "--max-workers",
                    "3",
                    "--clean-build",
                    "--no-validate",
                    "--verbose",
                    "--no-lint",
                ],
            )

        assert result.exit_code == 0, result.output
        kwargs = client.publish.build.call_args.kwargs
        assert kwargs == {
            "source_dir": str(tmp_path),
            "bucket": "my-artifacts",
            "prefix": "v1",
            "region": "eu-central-1",
            "headless": True,
            "govcloud": True,
            "public": True,
            "max_workers": 3,
            "clean_build": True,
            "no_validate": True,
            "verbose": True,
            "lint": False,
        }

    def test_headless_and_govcloud_together_are_accepted(self, runner, tmp_path):
        """
        `publish` accepts both variants at once, unlike `deploy`, which refuses them.

        The asymmetry is deliberate and worth pinning in both directions: `publish`
        *generates* template variants, so asking for two is meaningful, while
        `deploy` *deploys one stack* and could only pick one. A test asserting a
        refusal here would be asserting the wrong contract.
        """
        client = MagicMock()
        client.publish.build.return_value = _publish_result()
        with patch.object(cli_module, "IDPClient", return_value=client):
            result = runner.invoke(
                cli_module.cli,
                [
                    "publish",
                    "--source-dir",
                    str(tmp_path),
                    "--region",
                    "us-east-1",
                    "--headless",
                    "--govcloud",
                ],
            )

        assert result.exit_code == 0, result.output
        assert client.publish.build.call_args.kwargs["headless"] is True
        assert client.publish.build.call_args.kwargs["govcloud"] is True

    def test_region_is_required(self, runner, tmp_path):
        result = runner.invoke(
            cli_module.cli, ["publish", "--source-dir", str(tmp_path)]
        )
        assert result.exit_code != 0
        assert "--region" in result.output

    def test_a_source_dir_that_does_not_exist_is_refused_before_any_client(
        self, runner, tmp_path
    ):
        """
        `--source-dir` is a `click.Path(exists=True, file_okay=False)`, so a missing
        directory — or a file where a directory belongs — is refused by argument
        parsing, before an `IDPClient` is constructed. Asserting the client was
        never built is the half that matters: a refusal that happens after the
        client is built has already resolved a stack and a region.
        """
        missing = tmp_path / "nope"
        a_file = tmp_path / "afile"
        a_file.write_text("x", encoding="utf-8")

        with patch.object(cli_module, "IDPClient") as factory:
            for bad in (missing, a_file):
                result = runner.invoke(
                    cli_module.cli,
                    ["publish", "--source-dir", str(bad), "--region", "us-east-1"],
                )
                assert result.exit_code == 2, result.output
            factory.assert_not_called()


class TestPublishResultHandling:
    def test_deployment_urls_are_printed_with_all_three_variants(
        self, runner, tmp_path
    ):
        client = MagicMock()
        client.publish.build.return_value = _publish_result(
            headless_template_url="https://s3/idp-headless.yaml",
            govcloud_template_url="https://s3/idp-govcloud.yaml",
        )
        with patch.object(cli_module, "IDPClient", return_value=client):
            result = runner.invoke(
                cli_module.cli,
                ["publish", "--source-dir", str(tmp_path), "--region", "us-east-1"],
            )

        assert result.exit_code == 0, result.output
        assert "Publish complete" in result.output
        client.publish.print_deployment_urls.assert_called_once_with(
            template_url="https://s3.us-east-1.amazonaws.com/b/idp-main.yaml",
            region="us-east-1",
            headless_template_url="https://s3/idp-headless.yaml",
            govcloud_template_url="https://s3/idp-govcloud.yaml",
        )

    def test_a_missing_template_url_becomes_an_empty_string_not_none(
        self, runner, tmp_path
    ):
        """
        `print_deployment_urls` is called with `result.template_url or ""`.

        Pinned because the coalesce is load-bearing: the parameter is typed as a
        string downstream, and a successful build that reported no URL would
        otherwise pass `None` into string formatting.
        """
        client = MagicMock()
        client.publish.build.return_value = _publish_result(template_url=None)
        with patch.object(cli_module, "IDPClient", return_value=client):
            result = runner.invoke(
                cli_module.cli,
                ["publish", "--source-dir", str(tmp_path), "--region", "us-east-1"],
            )

        assert result.exit_code == 0, result.output
        assert (
            client.publish.print_deployment_urls.call_args.kwargs["template_url"] == ""
        )

    def test_a_failed_build_exits_one_and_prints_the_error(self, runner, tmp_path):
        client = MagicMock()
        client.publish.build.return_value = _publish_result(
            success=False, error="ruff found 3 problems"
        )
        with patch.object(cli_module, "IDPClient", return_value=client):
            result = runner.invoke(
                cli_module.cli,
                ["publish", "--source-dir", str(tmp_path), "--region", "us-east-1"],
            )

        assert result.exit_code == 1
        assert "Publish failed: ruff found 3 problems" in result.output
        client.publish.print_deployment_urls.assert_not_called()

    def test_an_exception_during_build_exits_one_and_prints_the_error(
        self, runner, tmp_path
    ):
        client = MagicMock()
        client.publish.build.side_effect = RuntimeError("docker daemon not running")
        with patch.object(cli_module, "IDPClient", return_value=client):
            result = runner.invoke(
                cli_module.cli,
                ["publish", "--source-dir", str(tmp_path), "--region", "us-east-1"],
            )

        assert result.exit_code == 1
        assert "docker daemon not running" in result.output


class TestChatCommandForwarding:
    """`idp-cli chat` forwards four options to `idp_cli.chat.run_chat` and nothing else."""

    def test_the_four_options_are_forwarded_by_keyword(self, runner):
        with patch("idp_cli.chat.run_chat") as run_chat:
            result = runner.invoke(
                cli_module.cli,
                [
                    "chat",
                    "--stack-name",
                    "IDP",
                    "--region",
                    "us-west-2",
                    "--prompt",
                    "how many documents?",
                    "--enable-code-intelligence",
                ],
            )

        assert result.exit_code == 0, result.output
        run_chat.assert_called_once_with(
            stack_name="IDP",
            region="us-west-2",
            prompt="how many documents?",
            enable_code_intelligence=True,
        )

    def test_the_defaults_are_forwarded_as_none_and_false(self, runner):
        """
        `--region` and `--prompt` default to `None` and the flag to `False`.

        `prompt=None` is what selects the interactive REPL over single-shot mode, so
        a default that arrived as `""` instead would silently run one empty turn and
        exit.
        """
        with patch("idp_cli.chat.run_chat") as run_chat:
            result = runner.invoke(cli_module.cli, ["chat", "--stack-name", "IDP"])

        assert result.exit_code == 0, result.output
        run_chat.assert_called_once_with(
            stack_name="IDP",
            region=None,
            prompt=None,
            enable_code_intelligence=False,
        )

    def test_stack_name_is_required(self, runner):
        result = runner.invoke(cli_module.cli, ["chat"])
        assert result.exit_code != 0
        assert "--stack-name" in result.output

    def test_defect_the_missing_dependency_message_loses_the_agents_extra(self, runner):
        """
        DEFECT (pinned as current behaviour, not fixed). `chat` guards its import of
        the optional agents dependency tree and prints a remedy, but the remedy is
        passed through Rich's console markup with the pip extra written as a bare
        `[agents]`. Rich reads `[agents]` as a style tag, fails to parse it as a
        style, and drops it silently — so both lines lose the qualifier and the user
        is told:

            Chat requires idp_common to be installed.
              Run: pip install -e 'lib/idp_common_pkg'

        The observable consequence is that the printed remedy does not fix the
        problem: `idp_common` is already installed in the situation that produces
        this message, and installing it again without the `[agents]` extra changes
        nothing, so the user runs the suggested command, sees it succeed, retries
        `idp-cli chat` and gets the same error. The fix is to escape the brackets
        (`\\[agents]`, as line 1111 already does for `\\[n]`) or to pass
        `markup=False`; `idp_cli/cli.py:6231-6232` is the only place in this package
        that prints a pip extra through Rich.

        Forced by putting `None` at `sys.modules["idp_cli.chat"]`, which the import
        system treats as "this module is known to be unimportable" and turns into an
        `ImportError` at the `from .chat import run_chat` statement.
        """
        import sys

        with patch.dict(sys.modules, {"idp_cli.chat": None}):
            result = runner.invoke(cli_module.cli, ["chat", "--stack-name", "IDP"])

        assert result.exit_code == 1
        assert "Chat requires idp_common to be installed." in result.output
        assert "Run: pip install -e 'lib/idp_common_pkg'" in result.output
        # The qualifier the user actually needs is absent from both lines.
        assert "[agents]" not in result.output


class TestBootstrapLocalMode:
    """Without `--stack-name`, bootstrap authors a schema, prints it, and saves nothing."""

    def test_the_schema_goes_to_stdout_as_parseable_json(self, runner):
        """
        This is the assertion the stdout/stderr split exists for.

        `idp-cli bootstrap -p "..." > schema.json` has to yield a file
        `json.load` accepts, which means not one progress line may reach stdout.
        Parsing `result.stdout` is what proves that; `result.output` combines both
        streams and would pass with the header lines interleaved.
        """
        schema = {"$id": "invoice", "properties": {"total": {"type": "number"}}}
        with (
            patch(
                "idp_common.synthesis.bootstrap.resolve_schema",
                return_value=(schema, "catalog", "invoice-template"),
            ),
            patch(
                "idp_common.synthesis.engine.generator_available",
                return_value=(True, ""),
            ),
        ):
            result = runner.invoke(
                cli_module.cli,
                ["bootstrap", "-p", "an invoice"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == schema

    def test_progress_and_the_catalog_match_go_to_stderr(self, runner):
        schema = {"$id": "invoice"}
        with (
            patch(
                "idp_common.synthesis.bootstrap.resolve_schema",
                return_value=(schema, "catalog", "invoice-template"),
            ),
            patch(
                "idp_common.synthesis.engine.generator_available",
                return_value=(True, ""),
            ),
        ):
            result = runner.invoke(cli_module.cli, ["bootstrap", "-p", "an invoice"])

        assert result.exit_code == 0, result.output
        combined = result.output
        assert "Local mode — schema will not be saved" in combined
        assert "Schema authored (tier: catalog)" in combined
        assert "Catalog match: invoice-template" in combined
        # None of those lines may be on stdout, which carries only the payload.
        assert "Local mode" not in result.stdout
        assert "Catalog match" not in result.stdout

    def test_an_unavailable_generator_is_reported_but_does_not_stop_local_mode(
        self, runner
    ):
        """
        Local mode never generates documents, so an unavailable generator is a note
        rather than a failure. A refusal here would make `bootstrap` unusable for
        exactly the case it is most useful for: seeing the schema before installing
        the heavy optional dependency.
        """
        with (
            patch(
                "idp_common.synthesis.bootstrap.resolve_schema",
                return_value=({"$id": "x"}, "llm", None),
            ),
            patch(
                "idp_common.synthesis.engine.generator_available",
                return_value=(False, "pdf2image missing"),
            ),
        ):
            result = runner.invoke(cli_module.cli, ["bootstrap", "-p", "a form"])

        assert result.exit_code == 0, result.output
        assert "document generator unavailable (pdf2image missing)" in result.output
        assert json.loads(result.stdout) == {"$id": "x"}

    def test_a_schema_that_could_not_be_authored_exits_one(self, runner):
        with (
            patch(
                "idp_common.synthesis.bootstrap.resolve_schema",
                return_value=(None, "none", None),
            ),
            patch(
                "idp_common.synthesis.engine.generator_available",
                return_value=(True, ""),
            ),
        ):
            result = runner.invoke(cli_module.cli, ["bootstrap", "-p", "???"])

        assert result.exit_code == 1
        assert "Failed to author a schema" in result.output
        assert result.stdout.strip() == ""

    def test_the_status_callback_renders_a_percentage_and_a_message(self, runner):
        """
        The nested `_status(pct, msg)` callback formats as `[ 42%] message` with the
        percentage right-aligned in three columns. It is the only progress a long
        authoring run shows, so a callback that raised on a float — `:3.0f` on a
        string, say — would turn a slow success into a traceback.
        """
        captured = []

        def fake_resolve(request, status_cb=None):
            captured.append(status_cb)
            status_cb(42.4, "Searching catalog")
            status_cb(100.0, "Done")
            return {"$id": "x"}, "catalog", None

        with (
            patch(
                "idp_common.synthesis.bootstrap.resolve_schema",
                side_effect=fake_resolve,
            ),
            patch(
                "idp_common.synthesis.engine.generator_available",
                return_value=(True, ""),
            ),
        ):
            result = runner.invoke(cli_module.cli, ["bootstrap", "-p", "a form"])

        assert result.exit_code == 0, result.output
        assert captured and captured[0] is not None
        assert "[ 42%] Searching catalog" in result.output
        assert "[100%] Done" in result.output

    def test_the_request_carries_every_option(self, runner):
        """
        `BootstrapRequest` is the single object every option lands in, so one wrong
        field name here is an option that silently does nothing. Repeatable
        `--field-hint` must accumulate into a list, and both spellings of the
        profile options (`--config-profile`/`--config-version`) must reach the same
        field.
        """
        seen = {}

        def fake_resolve(request, status_cb=None):
            seen["request"] = request
            return {"$id": "x"}, "catalog", None

        with (
            patch(
                "idp_common.synthesis.bootstrap.resolve_schema",
                side_effect=fake_resolve,
            ),
            patch(
                "idp_common.synthesis.engine.generator_available",
                return_value=(True, ""),
            ),
        ):
            result = runner.invoke(
                cli_module.cli,
                [
                    "bootstrap",
                    "-p",
                    "a bank statement",
                    "--class-name",
                    "BankStatement",
                    "--field-hint",
                    "AccountNumber",
                    "--field-hint",
                    "ClosingBalance",
                    "--config-version",
                    "src-profile",
                    "--target-profile",
                    "new-profile",
                    "--count",
                    "7",
                    "--threshold",
                    "9",
                    "--augment",
                    "--model-id",
                    "us.anthropic.claude-sonnet-4-20250514-v1:0",
                ],
            )

        assert result.exit_code == 0, result.output
        request = seen["request"]
        assert request.prompt == "a bank statement"
        assert request.class_name == "BankStatement"
        assert request.field_hints == ["AccountNumber", "ClosingBalance"]
        assert request.config_version == "src-profile"
        assert request.target_version == "new-profile"
        assert request.doc_count == 7
        assert request.quality_threshold == 9
        assert request.augment is True
        assert request.model_id == "us.anthropic.claude-sonnet-4-20250514-v1:0"

    def test_prompt_is_required(self, runner):
        result = runner.invoke(cli_module.cli, ["bootstrap"])
        assert result.exit_code != 0
        assert "--prompt" in result.output


class TestBootstrapStackMode:
    """With `--stack-name`, bootstrap writes a config profile and maybe a test set."""

    def _patched(self, run_bootstrap_result, *, config_table="IDP-Configuration"):
        client = MagicMock()
        client.discovery._get_config_table.return_value = config_table
        return (
            patch("idp_sdk.IDPClient", return_value=client),
            patch(
                "idp_common.synthesis.bootstrap.run_bootstrap",
                return_value=run_bootstrap_result,
            ),
            patch(
                "idp_common.synthesis.engine.generator_available",
                return_value=(True, ""),
            ),
            patch("idp_common.config.configuration_manager.ConfigurationManager"),
            client,
        )

    def _result(self, **overrides):
        from idp_common.synthesis.bootstrap import BootstrapResult

        fields = {
            "success": True,
            "config_version": "bootstrap-invoice",
            "resolution_tier": "catalog",
            "generator_available": True,
        }
        fields.update(overrides)
        return BootstrapResult(**fields)

    def test_a_successful_run_reports_the_profile_the_test_set_and_the_tier(
        self, runner
    ):
        p_client, p_run, p_gen, p_cfg, client = self._patched(
            self._result(
                catalog_match="invoice-template",
                test_set_id="ts-123",
                docs_generated=3,
            )
        )
        with p_client, p_run, p_gen, p_cfg:
            result = runner.invoke(
                cli_module.cli,
                ["bootstrap", "-p", "an invoice", "--stack-name", "IDP"],
            )

        assert result.exit_code == 0, result.output
        assert "Config profile: bootstrap-invoice" in result.output
        assert "Resolution tier: catalog" in result.output
        assert "Catalog match: invoice-template" in result.output
        assert "Test set: ts-123 (3 doc(s))" in result.output

    def test_the_configuration_table_is_resolved_and_exported_for_the_manager(
        self, runner, monkeypatch
    ):
        """
        The table name is resolved through the SDK and exported as
        `CONFIGURATION_TABLE_NAME`, because `ConfigurationManager` reads it from the
        environment. Both halves are asserted: the lookup uses the stack the user
        named, and the manager is constructed for the same region the lookup used —
        a manager built in a different region reads a table that does not exist
        there, which surfaces as an empty configuration rather than an error.
        """
        monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
        p_client, p_run, p_gen, p_cfg, client = self._patched(
            self._result(), config_table="IDP-Config-abc123"
        )
        with p_client, p_run, p_gen, p_cfg as manager:
            result = runner.invoke(
                cli_module.cli,
                [
                    "bootstrap",
                    "-p",
                    "an invoice",
                    "--stack-name",
                    "IDP",
                    "--region",
                    "eu-central-1",
                ],
            )

        assert result.exit_code == 0, result.output
        client.discovery._get_config_table.assert_called_once_with("IDP")
        import os

        assert os.environ["CONFIGURATION_TABLE_NAME"] == "IDP-Config-abc123"
        manager.assert_called_once_with(region="eu-central-1")

    def test_the_test_set_bucket_comes_from_the_environment(self, runner, monkeypatch):
        monkeypatch.setenv("TEST_SET_BUCKET", "my-test-sets")
        p_client, p_run, p_gen, p_cfg, client = self._patched(self._result())
        with p_client, p_run as run_bootstrap, p_gen, p_cfg:
            result = runner.invoke(
                cli_module.cli,
                ["bootstrap", "-p", "an invoice", "--stack-name", "IDP"],
            )

        assert result.exit_code == 0, result.output
        assert run_bootstrap.call_args.kwargs["test_set_bucket"] == "my-test-sets"

    def test_an_unavailable_generator_explains_the_skipped_test_set(self, runner):
        p_client, p_run, p_gen, p_cfg, client = self._patched(
            self._result(test_set_id=None, generator_available=False)
        )
        with p_client, p_run, p_gen, p_cfg:
            result = runner.invoke(
                cli_module.cli,
                ["bootstrap", "-p", "an invoice", "--stack-name", "IDP"],
            )

        assert result.exit_code == 0, result.output
        assert "Test set skipped (generator unavailable)" in result.output
        assert "Config is ready" in result.output

    def test_a_non_fatal_note_is_reported_alongside_success(self, runner):
        """
        `BootstrapResult` can carry both `success=True` and an `error` string — a
        partial run where the config landed but something secondary did not. The
        command reports it as a note and still exits 0, which is the right answer
        and the surprising one, so it is pinned.
        """
        p_client, p_run, p_gen, p_cfg, client = self._patched(
            self._result(error="test set upload was truncated")
        )
        with p_client, p_run, p_gen, p_cfg:
            result = runner.invoke(
                cli_module.cli,
                ["bootstrap", "-p", "an invoice", "--stack-name", "IDP"],
            )

        assert result.exit_code == 0, result.output
        assert "Note: test set upload was truncated" in result.output

    def test_a_failed_run_exits_one(self, runner):
        p_client, p_run, p_gen, p_cfg, client = self._patched(
            self._result(success=False, error="no catalog match and authoring failed")
        )
        with p_client, p_run, p_gen, p_cfg:
            result = runner.invoke(
                cli_module.cli,
                ["bootstrap", "-p", "an invoice", "--stack-name", "IDP"],
            )

        assert result.exit_code == 1
        assert (
            "Bootstrap failed: no catalog match and authoring failed" in result.output
        )

    def test_an_exception_exits_one_with_the_message(self, runner):
        client = MagicMock()
        client.discovery._get_config_table.side_effect = RuntimeError(
            "stack IDP not found"
        )
        with (
            patch("idp_sdk.IDPClient", return_value=client),
            patch(
                "idp_common.synthesis.engine.generator_available",
                return_value=(True, ""),
            ),
        ):
            result = runner.invoke(
                cli_module.cli,
                ["bootstrap", "-p", "an invoice", "--stack-name", "IDP"],
            )

        assert result.exit_code == 1
        assert "stack IDP not found" in result.output

    def test_stack_mode_keeps_progress_lines_on_stdout(self, runner):
        """
        The mirror of the local-mode test. With `--stack-name` nothing
        machine-readable is written, so the progress lines stay on stdout; the two
        modes choosing different streams is the behaviour, and a test that only
        read the combined output could not distinguish them.
        """
        p_client, p_run, p_gen, p_cfg, client = self._patched(self._result())
        with p_client, p_run, p_gen, p_cfg:
            result = runner.invoke(
                cli_module.cli,
                ["bootstrap", "-p", "an invoice", "--stack-name", "IDP"],
            )

        assert result.exit_code == 0, result.output
        assert "IDP Config Bootstrap" in result.stdout
        assert "Stack: IDP" in result.stdout
