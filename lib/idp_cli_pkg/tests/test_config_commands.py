# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
The ten `config-*` commands of `idp-cli`.

What the code under test does
-----------------------------
`config-create` and `config-validate` are offline: the first generates a YAML
configuration template from the system defaults shipped in `config_library/`, the
second loads a file, merges it with those defaults, and reports whether the result
is a usable IDP configuration. The other eight talk to a deployed stack. They
resolve the stack's `ConfigurationTable` and `ConfigurationBucket` from
CloudFormation and then read or write Configuration Profile records in DynamoDB —
`config-upload` and `config-activate` and `config-delete` mutate deployed state,
`config-download` and `config-list` and `config-revisions` read it, and
`config-sync-bda` pushes document classes out to Bedrock Data Automation
blueprints.

What shaped these tests
-----------------------
Three things.

First, **most of these commands run for real here.** The whole path from the
command line down to a DynamoDB item is exercised under `moto`: a CloudFormation
stack declaring a `ConfigurationTable` and a `ConfigurationBucket` is created, real
profile records are written into it, and what a command did is read back with
`get_item` / `scan` rather than off a mock. That distinction matters for this
command group specifically, because the interesting questions are *which profile
record was written* and *whether the record the user asked for was the record that
moved* — and a `MagicMock` answers neither. `idp_sdk.IDPClient` is patched in only
where the real dependency is Bedrock Data Automation, which `moto` does not
implement, and in the two `config-activate` cases whose result shape (a partial BDA
sync) cannot be produced any other way.

Second, **`config-validate` is a validator, so what matters is whether it says no.**
There is a test per class of problem it detects — malformed YAML, an unknown
Bedrock model id, a `task_prompt` with no document placeholder, a `max_tokens`
above the model's output limit, a value of the wrong type, and extra fields under
`--strict` — each asserting the exit code and the specific message. A validator
that exits 0 on a broken config is the defect it exists to prevent.

Third, **a config command that operates on the wrong version is the recurring
defect in this repository.** So wherever a command takes a profile or a revision,
there is a test that the value the user named is the value that reached the service
— asserted by content, from two profiles whose configurations differ — and a test
for a value that does not exist. That second kind found a live defect:
`config-download` does not refuse an unknown profile, it exits 0 having emitted the
YAML null document. See `test_download_of_an_unknown_profile_is_not_refused` and
the three tests around it, all of which pin current behaviour and say plainly that
it is wrong.

Defects pinned here rather than fixed
-------------------------------------
Each of these has a test whose docstring states the consequence:

- `config-download` accepts a profile that does not exist (exit 0, `null`).
- `config-upload --config-profile ""` writes an unreachable record and reports
  the configuration active.
- `config-upload --config-profile DEFAULT` warns that it will update the default
  profile and then creates a different one.
- `config-validate` reports and refuses unknown fields only at the top level, so
  `--strict` passes a mistyped nested key.
- Two diagnostics are raw `AttributeError` text.
- `config-create --features "a,typo"` drops the unknown section silently.
"""

import json
import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import boto3
import pytest
import yaml
from click.testing import CliRunner
from moto import mock_aws

from idp_cli.cli import _profile_is_managed, cli

REGION = "us-east-1"
STACK = "idp-config-test"

#: A CloudFormation template declaring the two resources the SDK's config
#: operations look up by logical id. It is a real template, created through moto's
#: CloudFormation, so `list_stack_resources` returns generated physical ids exactly
#: as it would against a deployed stack — which is the lookup under test. Building
#: the table directly and patching the lookup would skip it.
_STACK_TEMPLATE = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Description": "Test double for the IDP configuration resources",
    "Resources": {
        "ConfigurationTable": {
            "Type": "AWS::DynamoDB::Table",
            "Properties": {
                "AttributeDefinitions": [
                    {"AttributeName": "Configuration", "AttributeType": "S"}
                ],
                "KeySchema": [{"AttributeName": "Configuration", "KeyType": "HASH"}],
                "BillingMode": "PAY_PER_REQUEST",
            },
        },
        "ConfigurationBucket": {"Type": "AWS::S3::Bucket", "Properties": {}},
    },
}

#: Environment variables the SDK's `_configure_config_env` writes into the process
#: so that `idp_common` can find the stack's table and bucket. They are set by
#: production code rather than by the test, so `monkeypatch` does not know about
#: them and cannot undo them; `config_stack` saves and restores them itself. Left
#: behind, `CONFIGURATION_TABLE_NAME` names a table from a torn-down moto mock and
#: the next test in the file fails for a reason that has nothing to do with it.
_ENV_WRITTEN_BY_THE_SDK = (
    "CONFIGURATION_TABLE_NAME",
    "CONFIGURATION_BUCKET",
    "STACK_NAME",
    "AWS_DEFAULT_REGION",
)


class ConfigStack:
    """A moto-backed IDP configuration table, with the seeding this file needs."""

    def __init__(self, name: str = STACK, with_bucket: bool = True):
        template = json.loads(json.dumps(_STACK_TEMPLATE))
        if not with_bucket:
            # A stack deployed before revision history existed. The SDK treats a
            # missing bucket as "history unavailable", which is a different answer
            # from "this profile has no revisions".
            del template["Resources"]["ConfigurationBucket"]

        self.name = name
        cfn = boto3.client("cloudformation", region_name=REGION)
        cfn.create_stack(StackName=name, TemplateBody=json.dumps(template))
        self.resources = {
            resource["LogicalResourceId"]: resource["PhysicalResourceId"]
            for resource in cfn.list_stack_resources(StackName=name)[
                "StackResourceSummaries"
            ]
        }
        self.table_name = self.resources["ConfigurationTable"]
        self.bucket_name = self.resources.get("ConfigurationBucket")

        # Seeding goes through the real ConfigurationManager, which reads these.
        # The command under test sets them again to the same values.
        os.environ["CONFIGURATION_TABLE_NAME"] = self.table_name
        if self.bucket_name:
            os.environ["CONFIGURATION_BUCKET"] = self.bucket_name

        self.table = boto3.resource("dynamodb", region_name=REGION).Table(
            self.table_name
        )

    @property
    def manager(self):
        from idp_common.config.configuration_manager import ConfigurationManager

        return ConfigurationManager(region=REGION)

    def seed(
        self,
        profile: str,
        class_name: str | None = None,
        description: str | None = None,
        managed: bool = False,
        active: bool = False,
    ) -> None:
        """Write a real Configuration Profile record, as a save through the UI would."""
        classes = [{"name": class_name}] if class_name else []
        self.manager.save_configuration(
            "Config", {"classes": classes}, version=profile, description=description
        )
        if managed:
            # Set directly rather than through the config body: `save_configuration`
            # is the user-save path, and a stack-managed profile is written by a
            # stack update. What the command reads is this attribute.
            self.table.update_item(
                Key={"Configuration": f"Config#{profile}"},
                UpdateExpression="SET Managed = :true",
                ExpressionAttributeValues={":true": True},
            )
        if active:
            self.manager.activate_version(profile)

    def item(self, profile: str) -> dict | None:
        """The profile's record, or None — for asserting presence and absence."""
        return self.table.get_item(Key={"Configuration": f"Config#{profile}"}).get(
            "Item"
        )

    def attrs(self, profile: str) -> dict:
        """The profile's record, failing if it is absent — for reading attributes.

        Separate from `item` so that an attribute assertion against a profile that
        was never written fails saying so, rather than as a `TypeError` on `None`.
        """
        item = self.item(profile)
        assert item is not None, (
            f"no record for profile {profile!r}; the table holds {self.keys()}"
        )
        return item

    def keys(self) -> list[str]:
        return sorted(item["Configuration"] for item in self.table.scan()["Items"])


@contextmanager
def config_stack(**kwargs):
    """A moto account holding one IDP configuration stack, environment restored."""
    saved = {name: os.environ.get(name) for name in _ENV_WRITTEN_BY_THE_SDK}
    try:
        with mock_aws():
            yield ConfigStack(**kwargs)
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def invoke(args, **kwargs):
    return CliRunner().invoke(cli, [*args, "--region", REGION], **kwargs)


@contextmanager
def write_config(text: str, filename: str = "config.yaml"):
    """A CliRunner in an isolated directory holding one config file."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        with open(filename, "w", encoding="utf-8") as handle:
            handle.write(text)
        yield runner


@contextmanager
def patched_client():
    """Patch `IDPClient` where the CLI imports it, yielding the client instance.

    Used only where the real dependency is Bedrock Data Automation (which `moto`
    does not implement) or where the SDK result shape cannot be produced from
    DynamoDB state. `cli.py` imports `IDPClient` inside each command body, so the
    patch target is `idp_sdk.IDPClient` rather than `idp_cli.cli.IDPClient`.
    """
    with patch("idp_sdk.IDPClient") as client_cls:
        client = MagicMock()
        client_cls.return_value = client
        yield client


VALID_MINIMAL = "classes:\n  - name: invoice\n    description: A supplier invoice\n"


# --------------------------------------------------------------------------- #
# config-create
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_create_writes_only_loadable_yaml_to_stdout():
    """
    `config-create > config.yaml` is the documented usage, so stdout must be the
    template and nothing else. The header is written as YAML comments, which is
    what lets it coexist with the redirect.
    """
    result = CliRunner().invoke(cli, ["config-create"])
    assert result.exit_code == 0, result.output

    loaded = yaml.safe_load(result.stdout)
    assert "classes" in loaded and "extraction" in loaded
    assert result.stderr == "", (
        f"config-create wrote to stderr on success: {result.stderr!r}"
    )
    assert result.stdout.lstrip().startswith("#")


@pytest.mark.unit
def test_create_to_a_file_parses_and_names_the_next_steps():
    with write_config("", filename="unused.txt") as runner:
        result = runner.invoke(cli, ["config-create", "--output", "gen.yaml"])
        assert result.exit_code == 0, result.output
        loaded = yaml.safe_load(open("gen.yaml", encoding="utf-8").read())

    assert "classes" in loaded
    assert "gen.yaml" in result.output
    # The three follow-on commands, because a generated template on its own is not
    # yet a deployable configuration.
    assert "config-validate --config-file gen.yaml" in result.output
    assert "deploy" in result.output


@pytest.mark.unit
def test_create_feature_presets_select_different_section_sets():
    """`--features all` must be a superset of `min`, or the preset means nothing."""
    minimal = yaml.safe_load(
        CliRunner().invoke(cli, ["config-create", "--features", "min"]).stdout
    )
    everything = yaml.safe_load(
        CliRunner().invoke(cli, ["config-create", "--features", "all"]).stdout
    )
    assert "ocr" not in minimal
    assert "ocr" in everything and "discovery" in everything


@pytest.mark.unit
def test_create_accepts_a_comma_separated_section_list():
    result = CliRunner().invoke(
        cli, ["config-create", "--features", "classification,summarization"]
    )
    assert result.exit_code == 0, result.output
    loaded = yaml.safe_load(result.stdout)
    assert "classification" in loaded and "summarization" in loaded
    assert "extraction" not in loaded, (
        "a section list must select exactly what it names; selecting more makes the "
        "option pointless"
    )


@pytest.mark.unit
def test_create_drops_an_unknown_section_from_a_list_without_warning():
    """
    DEFECT (`idp_cli/cli.py:4281`, `generate_config_template`): a comma-separated
    `--features` list containing a section that does not exist is accepted, the
    unknown name is dropped from the output, and the command exits 0. The name is
    echoed in the generated header comment — as one of the sections that were
    *requested* — but there is no warning and nothing distinguishes it from the
    sections that were actually produced.

    Consequence: `--features "classification,extration"` — one transposed letter —
    produces a template with no extraction section at all, reported as a success. A
    single unknown name IS rejected outright (the next test), so the two spellings
    of the same mistake behave oppositely.

    This test pins the current behaviour.
    """
    result = CliRunner().invoke(
        cli, ["config-create", "--features", "classification,not-a-real-section"]
    )
    assert result.exit_code == 0
    loaded = yaml.safe_load(result.stdout)
    assert "classification" in loaded
    assert "not-a-real-section" not in loaded, (
        "the unknown section is dropped from the template body"
    )
    # It survives only inside the header comment, which is why `yaml.safe_load`
    # above cannot see it — and no line calls it out as a problem.
    assert "not-a-real-section" in result.stdout
    for marker in ("Invalid", "Unknown", "⚠", "not recognized"):
        assert marker not in result.stdout, f"{marker!r} would be a warning"


@pytest.mark.unit
def test_create_refuses_an_unknown_feature_preset_on_stderr():
    result = CliRunner().invoke(cli, ["config-create", "--features", "everything"])
    assert result.exit_code == 1
    assert "Invalid feature set 'everything'" in result.stderr
    assert result.stdout == "", (
        "a diagnostic on stdout lands in the redirected config file; that is why "
        "this command's errors go to err_console"
    )


@pytest.mark.unit
def test_create_no_comments_omits_the_header():
    with_comments = CliRunner().invoke(cli, ["config-create"]).stdout
    without = CliRunner().invoke(cli, ["config-create", "--no-comments"]).stdout
    assert with_comments.lstrip().startswith("#")
    assert not without.lstrip().startswith("#")
    assert (
        yaml.safe_load(without)["classes"] == yaml.safe_load(with_comments)["classes"]
    )


@pytest.mark.unit
def test_create_include_prompts_produces_a_larger_template():
    """Prompts are stripped by default for readability; the flag has to restore them."""
    stripped = CliRunner().invoke(cli, ["config-create", "--features", "core"]).stdout
    full = (
        CliRunner()
        .invoke(cli, ["config-create", "--features", "core", "--include-prompts"])
        .stdout
    )
    assert len(full) > len(stripped) * 1.5
    assert "{DOCUMENT_TEXT}" in full


@pytest.mark.unit
def test_create_without_the_system_defaults_explains_where_to_run_it():
    """
    The defaults live in `config_library/` in the repository, so the command fails
    when run from an installed package with no project root. The diagnostic has to
    say that, and it has to go to stderr for the same reason as above.
    """
    with patch(
        "idp_common.config.merge_utils.generate_config_template",
        side_effect=FileNotFoundError("System defaults directory not found"),
    ):
        result = CliRunner().invoke(cli, ["config-create"])
    assert result.exit_code == 1
    assert "System defaults directory not found" in result.stderr
    assert "IDP_PROJECT_ROOT" in result.stderr
    assert result.stdout == ""


# --------------------------------------------------------------------------- #
# config-validate — the refusals are the point
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_validate_accepts_a_genuinely_valid_config():
    """The positive control. Without it, a validator that refuses everything passes."""
    with write_config(VALID_MINIMAL) as runner:
        result = runner.invoke(cli, ["config-validate", "--config-file", "config.yaml"])
    assert result.exit_code == 0, result.output
    assert "YAML syntax valid" in result.output
    assert "Pydantic validation passed" in result.output
    assert "Config is valid!" in result.output
    assert "1 document class(es) defined" in result.output


@pytest.mark.unit
def test_validate_refuses_malformed_yaml():
    with write_config("classes: [\n  - name: x\n :::broken\n") as runner:
        result = runner.invoke(cli, ["config-validate", "--config-file", "config.yaml"])
    assert result.exit_code == 1
    assert "YAML syntax error" in result.output
    # The parse error has to name a position, or a large config is unfixable.
    assert "line 2" in result.output


@pytest.mark.unit
def test_validate_refuses_a_config_that_is_not_a_mapping_but_says_so_badly():
    """
    A YAML document whose top level is a list is refused, which is right, but the
    diagnostic is still the raw exception text.

    Two separate things are asserted, and only the first is a guarantee. The
    guarantee: the refusal arrives on the **validation** path, not as an unhandled
    exception in the command's own bookkeeping. `config-validate` hands the loaded
    document straight to `validate_config` and reports what comes back, so a
    top-level list is refused by the merge with a bulleted error under
    `✗ Validation failed` — the same shape as every other refusal in this section.
    It used to compute `set(user_config.keys())` itself before validating, which
    raised `AttributeError` on anything that was not a dict and fell out of the
    generic `except Exception` handler as a bare `✗ Error:` line.

    The residual, pinned rather than fixed: whichever path it takes, the text the
    user sees is a Python attribute error about the type rather than a sentence
    about their file. If someone teaches the command to say "top level must be a
    mapping", the last assertion here is the one that will fail — update this test,
    it is not defending that wording.
    """
    with write_config("- first\n- second\n") as runner:
        result = runner.invoke(cli, ["config-validate", "--config-file", "config.yaml"])
    assert result.exit_code == 1
    # The document parses, so the refusal must not be filed under syntax, and it
    # must come from the validator rather than from an unhandled AttributeError.
    assert "YAML syntax valid" in result.output
    assert "YAML syntax error" not in result.output
    assert "Validation failed" in result.output
    assert "Config is valid!" not in result.output
    # RESIDUAL: the diagnostic still does not describe the file. See the docstring.
    assert "not a mapping" not in result.output


@pytest.mark.unit
def test_validate_refuses_an_unknown_bedrock_model_id():
    """
    A model id that is not in `config_library/pricing.yaml` is a typo, and every
    call under it would fail at request time with ResourceNotFoundException.
    """
    with write_config(
        "extraction:\n  model: us.anthropic.claude-nope-v9:0\nclasses: []\n"
    ) as runner:
        result = runner.invoke(cli, ["config-validate", "--config-file", "config.yaml"])
    assert result.exit_code == 1
    assert "Validation failed" in result.output
    assert "extraction.model has invalid model ID: us.anthropic.claude-nope-v9:0" in (
        " ".join(result.output.split())
    )


@pytest.mark.unit
def test_validate_refuses_a_task_prompt_with_no_document_placeholder():
    """
    An extraction prompt with neither `{DOCUMENT_TEXT}` nor `{DOCUMENT_IMAGE}` sends
    the model no document at all. It fails silently at run time — the model answers
    from the instructions alone — so catching it here is the only cheap chance.
    """
    with write_config(
        "extraction:\n  task_prompt: 'Extract the fields.'\nclasses: []\n"
    ) as runner:
        result = runner.invoke(cli, ["config-validate", "--config-file", "config.yaml"])
    assert result.exit_code == 1
    flat = " ".join(result.output.split())
    assert "extraction.task_prompt must include {DOCUMENT_TEXT}" in flat
    assert "silent extraction failures" in flat


@pytest.mark.unit
def test_validate_refuses_max_tokens_above_the_model_limit():
    """Bedrock rejects the request outright, so this is a pre-flight error."""
    with write_config(
        "classification:\n"
        "  model: us.amazon.nova-lite-v1:0\n"
        "  max_tokens: 999999\n"
        "classes: []\n"
    ) as runner:
        result = runner.invoke(cli, ["config-validate", "--config-file", "config.yaml"])
    assert result.exit_code == 1
    flat = " ".join(result.output.split())
    assert "classification.max_tokens (999999) exceeds model limit (10,000)" in flat
    # The remedy has to carry the number, or the user guesses.
    assert "Reduce max_tokens to 10,000 or less" in flat


@pytest.mark.unit
def test_validate_refuses_a_value_of_the_wrong_type():
    with write_config(
        "classification:\n  temperature: not-a-number\nclasses: []\n"
    ) as (runner):
        result = runner.invoke(cli, ["config-validate", "--config-file", "config.yaml"])
    assert result.exit_code == 1
    flat = " ".join(result.output.split())
    assert "Pydantic validation failed" in flat
    assert "classification.temperature" in flat


@pytest.mark.unit
def test_validate_warns_about_an_unknown_top_level_field_but_still_passes():
    """
    Without `--strict` an unknown field is a warning, because an old config carrying
    a field this release dropped should still validate. It must say the field will
    be IGNORED, though — silently accepting it would let a user believe a setting
    took effect.
    """
    with write_config("banana: 1\nclasses: []\n") as runner:
        result = runner.invoke(cli, ["config-validate", "--config-file", "config.yaml"])
    flat = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    # Asserted on substance, not on the sentence: the field is named, the reader is
    # told it will be ignored, it is reported as a WARNING rather than an error, and
    # the run still passes. A copy-edit to the wording leaves all four true.
    assert "banana" in flat
    assert "ignored" in flat
    assert "Warnings:" in result.output
    assert "Config is valid!" in result.output
    # A key the models do not declare is still classified as unknown rather than as
    # deprecated — the two kinds carry different advice.
    assert "Deprecated" not in flat


@pytest.mark.unit
def test_validate_strict_refuses_an_unknown_top_level_field():
    with write_config("banana: 1\nclasses: []\n") as runner:
        result = runner.invoke(
            cli, ["config-validate", "--config-file", "config.yaml", "--strict"]
        )
    assert result.exit_code == 1
    assert "Strict mode: config contains extra fields" in result.output
    assert "run without --strict" in result.output


@pytest.mark.unit
def test_validate_strict_refuses_a_deprecated_field_too():
    """
    `--strict` is documented as failing on "unknown OR deprecated" fields, and a
    deprecated field is the one a real user hits: it is what an older saved config
    carries.
    """
    from idp_common.config.models import IDP_CONFIG_DEPRECATED_FIELDS

    deprecated = sorted(IDP_CONFIG_DEPRECATED_FIELDS)[0]
    with write_config(f"{deprecated}: {{}}\nclasses: []\n") as runner:
        plain = runner.invoke(cli, ["config-validate", "--config-file", "config.yaml"])
        strict = runner.invoke(
            cli, ["config-validate", "--config-file", "config.yaml", "--strict"]
        )
    plain_flat = " ".join(plain.output.split())
    strict_flat = " ".join(strict.output.split())
    # Without --strict: a warning that names the field and classifies it as
    # deprecated, and the run passes. The classification is the substance — a
    # deprecated field is one to delete, an unknown one is probably a typo to fix.
    assert plain.exit_code == 0, plain.output
    assert deprecated in plain_flat
    assert "eprecated" in plain_flat
    assert "ignored" in plain_flat
    assert "Config is valid!" in plain.output
    # With --strict: refused, and the refusal NAMES the field. Reporting only that
    # "config contains extra fields" left the user to find it in a large document.
    assert strict.exit_code == 1
    assert "Strict mode" in strict.output
    assert deprecated in strict_flat


@pytest.mark.unit
def test_validate_reports_a_mistyped_key_inside_a_section_by_its_dotted_path():
    """
    A key one level down that no model declares is REPORTED, and reported by its
    dotted path.

    This is the guarantee #1134 exists to provide, and nested keys are where nearly
    every real configuration typo lives. Every section model takes pydantic's
    default `extra="ignore"`, so `classification.maxPagesForClassifcation` (one
    missing 'i') used to be dropped in silence: the setting did not apply, the
    config validated, and the output said nothing. `validate_config` now walks the
    whole model tree and names each unread key by its path, so the author is told
    where to look.

    The path, not just the leaf, is what is asserted. A mis-nested key is the more
    damaging half of this class — the value is routed around the validator that
    would have rejected it, so `ocr.dpi: "abc"` is accepted while `ocr.image.dpi:
    "abc"` raises — and naming only `dpi` would not tell the author which of the two
    they wrote.

    It reports rather than rejects, by design and in both directions:

    * Without `--strict` the run still passes. `extra` is unchanged on every model,
      so a configuration that loads today still loads and a key a later release
      removes needs no migration story.
    * `--strict` **also** still passes, and that is deliberate rather than a
      leftover: its documented contract is top-level fields only, and widening it
      downwards would start failing configurations that pass today — a decision for
      a release, not for a fix. The key is reported either way, which is the part
      that matters. If that contract is deliberately widened later, change the
      `strict.exit_code` expectation here and say so in the CHANGELOG; do not
      delete the test.
    """
    with write_config("classification:\n  notARealSetting: 3\nclasses: []\n") as runner:
        plain = runner.invoke(cli, ["config-validate", "--config-file", "config.yaml"])
        strict = runner.invoke(
            cli, ["config-validate", "--config-file", "config.yaml", "--strict"]
        )
    plain_flat = " ".join(plain.output.split())
    strict_flat = " ".join(strict.output.split())
    # The guarantee: the key is reported, by its full dotted path, as something that
    # will be ignored — so the author learns the default is still in force.
    assert "classification.notARealSetting" in plain_flat, plain.output
    assert "ignored" in plain_flat
    assert "Warnings:" in plain.output
    # Reported, not rejected.
    assert plain.exit_code == 0, plain.output
    # --strict's contract is top-level only; the nested key is still reported there.
    assert strict.exit_code == 0, strict.output
    assert "classification.notARealSetting" in strict_flat, strict.output


@pytest.mark.unit
def test_validate_emit_migrated_writes_a_loadable_current_format_file():
    """
    `--emit-migrated` exists to hand a user a pre-migrated copy of an older config,
    so the output has to be loadable YAML stamped with the format it was migrated
    to. It runs BEFORE the deprecated-field checks, deliberately: the input is
    expected to be an old file.
    """
    with write_config(VALID_MINIMAL) as runner:
        result = runner.invoke(
            cli,
            [
                "config-validate",
                "--config-file",
                "config.yaml",
                "--emit-migrated",
                "migrated.yaml",
            ],
        )
        written = open("migrated.yaml", encoding="utf-8").read()

    assert result.exit_code == 0, result.output
    assert "Migrated config written to: migrated.yaml" in result.output
    loaded = yaml.safe_load(written)
    assert loaded["config_format_version"]
    assert loaded["classes"][0]["name"] == "invoice"
    # Provenance: which file this was migrated from, as a YAML comment so it does
    # not become an extra top-level key.
    assert written.startswith("# Migrated to config_format_version")


@pytest.mark.unit
def test_validate_show_merged_prints_the_merged_configuration():
    with write_config(VALID_MINIMAL) as runner:
        result = runner.invoke(
            cli, ["config-validate", "--config-file", "config.yaml", "--show-merged"]
        )
    assert result.exit_code == 0, result.output
    assert "Merged configuration:" in result.output
    # Merged means merged with the defaults, so sections the user never mentioned
    # have to be present.
    assert "classification:" in result.output
    assert "extraction:" in result.output


@pytest.mark.unit
def test_validate_rejects_a_missing_file_before_it_starts():
    """click's `exists=True` answers this, and exit 2 is the usage-error code."""
    result = CliRunner().invoke(
        cli, ["config-validate", "--config-file", "/nonexistent/nope.yaml"]
    )
    assert result.exit_code == 2
    assert "does not exist" in result.output


@pytest.mark.unit
def test_validate_separates_an_unreadable_file_from_a_malformed_one():
    """
    A file that exists but cannot be read — a permission error, a directory, a
    decoding failure — is a different problem from bad YAML, and the remedy is
    different too, so it gets its own message rather than being reported as a
    syntax error.
    """
    with write_config(VALID_MINIMAL) as runner:
        with patch(
            "idp_common.config.merge_utils.load_yaml_file",
            side_effect=PermissionError("Permission denied: config.yaml"),
        ):
            result = runner.invoke(
                cli, ["config-validate", "--config-file", "config.yaml"]
            )
    assert result.exit_code == 1
    assert "Failed to load file: Permission denied" in result.output
    assert "YAML syntax error" not in result.output


@pytest.mark.unit
def test_validate_without_the_system_defaults_explains_where_to_run_it():
    """
    Validation merges against `config_library/`, so it fails the same way
    `config-create` does when run outside a repository checkout. The tip is the only
    actionable part of that failure.
    """
    with write_config(VALID_MINIMAL) as runner:
        with patch(
            "idp_common.config.merge_utils.validate_config",
            side_effect=FileNotFoundError("System defaults directory not found"),
        ):
            result = runner.invoke(
                cli, ["config-validate", "--config-file", "config.yaml"]
            )
    assert result.exit_code == 1
    assert "System defaults directory not found" in result.output
    assert "IDP_PROJECT_ROOT" in result.output


@pytest.mark.unit
def test_validate_accepts_an_empty_file_as_pure_defaults():
    """
    An empty config means "use every default", which is legitimate, so this is
    accepted. It must still say no classes are defined: a config with no document
    classes cannot extract anything, and that is the one thing a user has to add.
    """
    with write_config("") as runner:
        result = runner.invoke(cli, ["config-validate", "--config-file", "config.yaml"])
    assert result.exit_code == 0, result.output
    assert "No document classes defined" in result.output


# --------------------------------------------------------------------------- #
# config-upload — a write to deployed state
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_upload_creates_the_named_profile_and_the_record_holds_the_uploaded_classes():
    """
    Read back from DynamoDB rather than off a mock: the question is whether the
    record written was `Config#<the profile the user named>` and whether it carries
    the classes from the file.
    """
    with config_stack() as stack:
        with write_config(
            "classes:\n  - name: bank-statement\n    description: A statement\n"
        ) as runner:
            result = runner.invoke(
                cli,
                [
                    "config-upload",
                    "--stack-name",
                    STACK,
                    "--config-file",
                    "config.yaml",
                    "--config-profile",
                    "tuning-run-4",
                    "--no-validate",
                    "--version-description",
                    "Fourth tuning iteration",
                    "--region",
                    REGION,
                ],
            )
        assert result.exit_code == 0, result.output
        assert "Configuration profile 'tuning-run-4' created!" in result.output

        item = stack.attrs("tuning-run-4")
        assert item["Description"] == "Fourth tuning iteration"
        # The upload forces managed=False so a stack update cannot claim the record.
        assert item["Managed"] is False

        stored = stack.manager.get_configuration("Config", version="tuning-run-4")
        assert [cls["name"] for cls in stored.classes] == ["bank-statement"]


@pytest.mark.unit
def test_upload_to_an_existing_profile_updates_it_and_cuts_the_next_revision():
    """
    Two things a caller depends on: the word is "updated" rather than "created", and
    the revision number printed is the NEW one, so a script can pin the run to
    exactly what it just uploaded.
    """
    with config_stack() as stack:
        stack.seed("lending", class_name="original-class")
        assert stack.attrs("lending")["PublishedRevision"] == 1

        with write_config("classes:\n  - name: replacement-class\n") as runner:
            result = runner.invoke(
                cli,
                [
                    "config-upload",
                    "--stack-name",
                    STACK,
                    "--config-file",
                    "config.yaml",
                    "--config-profile",
                    "lending",
                    "--no-validate",
                    "--revision-notes",
                    "swapped the class",
                    "--region",
                    REGION,
                ],
            )
        assert result.exit_code == 0, result.output
        assert "'lending' updated!" in result.output
        assert "Revision: r2" in result.output
        assert "--config-profile lending --config-revision 2" in result.output

        assert stack.attrs("lending")["PublishedRevision"] == 2
        stored = stack.manager.get_configuration("Config", version="lending")
        assert [cls["name"] for cls in stored.classes] == ["replacement-class"]


@pytest.mark.unit
def test_upload_of_malformed_yaml_writes_nothing(api_calls):
    """A file that does not parse must not leave a half-written profile behind."""
    with config_stack() as stack:
        before = stack.keys()
        api_calls.clear()  # drop the calls this test's own setup made
        with write_config("classes: [\n  - name: x\n :::broken\n") as runner:
            result = runner.invoke(
                cli,
                [
                    "config-upload",
                    "--stack-name",
                    STACK,
                    "--config-file",
                    "config.yaml",
                    "--config-profile",
                    "p1",
                    "--region",
                    REGION,
                ],
            )
        assert result.exit_code == 1
        assert "Failed to load config" in result.output
        assert api_calls.of("PutItem") == [], (
            "a config file that does not parse must not reach DynamoDB"
        )
        assert stack.keys() == before


@pytest.mark.unit
def test_upload_refuses_a_config_that_declares_itself_stack_managed(api_calls):
    """
    A managed profile is owned by a stack and rewritten on every update, so a config
    carrying `managed: true` would be silently discarded later. The refusal says
    how to fix the file.
    """
    with config_stack() as stack:
        api_calls.clear()
        with write_config("managed: true\nclasses: []\n") as runner:
            result = runner.invoke(
                cli,
                [
                    "config-upload",
                    "--stack-name",
                    STACK,
                    "--config-file",
                    "config.yaml",
                    "--config-profile",
                    "p1",
                    "--region",
                    REGION,
                ],
            )
        assert result.exit_code == 1
        flat = " ".join(result.output.split())
        assert "Cannot upload managed configuration via CLI" in flat
        assert "Remove 'managed: true'" in flat
        assert api_calls.of("PutItem") == []
        assert stack.item("p1") is None


@pytest.mark.unit
def test_upload_validation_failure_blocks_the_write_and_offers_the_escape_hatch(
    api_calls,
):
    """
    Validation is on by default, so an invalid config is refused before anything is
    written — and the message has to name `--no-validate`, because the legitimate
    case (a model newer than this release's pricing.yaml) is otherwise a dead end.
    """
    with config_stack() as stack:
        api_calls.clear()
        with write_config(
            "extraction:\n  model: us.anthropic.claude-nope-v9:0\nclasses: []\n"
        ) as runner:
            result = runner.invoke(
                cli,
                [
                    "config-upload",
                    "--stack-name",
                    STACK,
                    "--config-file",
                    "config.yaml",
                    "--config-profile",
                    "p1",
                    "--region",
                    REGION,
                ],
            )
        assert result.exit_code == 1
        assert "Failed to upload configuration" in result.output
        assert "--no-validate" in result.output
        assert api_calls.of("PutItem") == []
        assert stack.item("p1") is None


@pytest.mark.unit
def test_upload_requires_a_profile_and_an_existing_file():
    """Both are usage errors, so both are exit 2 and neither reaches AWS."""
    with write_config(VALID_MINIMAL) as runner:
        no_profile = runner.invoke(
            cli,
            ["config-upload", "--stack-name", STACK, "--config-file", "config.yaml"],
        )
        no_file = runner.invoke(
            cli,
            [
                "config-upload",
                "--stack-name",
                STACK,
                "--config-file",
                "absent.yaml",
                "--config-profile",
                "p1",
            ],
        )
    assert no_profile.exit_code == 2
    assert "--config-profile" in no_profile.output
    assert no_file.exit_code == 2
    assert "does not exist" in no_file.output


@pytest.mark.unit
def test_upload_to_a_profile_named_DEFAULT_warns_about_a_profile_it_does_not_touch():
    """
    DEFECT, two of them on one line (`idp_cli/cli.py:4591-4594`).

    The guard is `config_version.lower() == "default"`, but profile names are
    DynamoDB key material and case-sensitive. So `--config-profile DEFAULT` prints
    "this will update the default [system default] config profile" and then creates
    a brand-new profile at `Config#DEFAULT`, leaving `Config#default` untouched.
    Consequence: the warning describes a destructive act that did not happen, and
    hides the one that did — a duplicate profile differing only in case.

    Pinned. The warning does now name the profile it means: `[system default]` was
    being read by Rich as a style tag and deleted, leaving "...update the default
    config profile" with a doubled space and no indication of which profile that was,
    and the bracket is escaped so the text survives. That is a separate defect from
    the case-sensitivity one this test exists for, which remains.
    """
    with config_stack() as stack:
        stack.seed("default", class_name="the-real-default")
        with write_config("classes:\n  - name: mine\n") as runner:
            result = runner.invoke(
                cli,
                [
                    "config-upload",
                    "--stack-name",
                    STACK,
                    "--config-file",
                    "config.yaml",
                    "--config-profile",
                    "DEFAULT",
                    "--no-validate",
                    "--region",
                    REGION,
                ],
            )
        assert result.exit_code == 0, result.output
        assert "This will update the default" in result.output
        assert "[system default]" in result.output, (
            "Rich swallowed the bracketed text as a style tag"
        )
        assert "Configuration profile 'DEFAULT' created!" in result.output

        assert "Config#DEFAULT" in stack.keys()
        untouched = stack.manager.get_configuration("Config", version="default")
        assert [cls["name"] for cls in untouched.classes] == ["the-real-default"]


@pytest.mark.unit
def test_upload_with_an_empty_profile_name_writes_an_unreachable_record():
    """
    DEFECT (`idp_cli/cli.py:4616-4635` with `idp_sdk/operations/config.py:443`).

    `--config-profile` is `required=True`, but click only requires the option to be
    PRESENT, and `""` is present. An empty value survives
    `resolve_config_profile(..., required=True)` — which tests for `None` — and then
    `ConfigurationManager` builds its key as `f"Config#{version}"` only when the
    version is truthy, so the configuration lands on the bare `Config` key.

    Consequence, and it is the bad kind: the command exits 0 and prints
    "Configuration is now active! New documents will use this configuration
    immediately." Nothing reads that record. `config-list` filters on
    `begins_with(Configuration, "Config#")` so it never appears, and the active
    profile is unchanged. A write was reported as a live configuration change and it
    changed nothing.

    Reaching `cli.py:4634-4635` at all requires this bug: every other invocation has
    a truthy profile. Pinned, not fixed.
    """
    with config_stack() as stack:
        stack.seed("lending", class_name="lending-class", active=True)
        with write_config("classes:\n  - name: went-nowhere\n") as runner:
            result = runner.invoke(
                cli,
                [
                    "config-upload",
                    "--stack-name",
                    STACK,
                    "--config-file",
                    "config.yaml",
                    "--config-profile",
                    "",
                    "--no-validate",
                    "--region",
                    REGION,
                ],
            )
        assert result.exit_code == 0, result.output
        assert "Configuration is now active!" in result.output

        # The record exists, under a key that is not a profile.
        assert "Config" in stack.keys()
        assert "Config#" not in stack.keys()
        # And the profile listing cannot see it.
        assert [v["versionName"] for v in stack.manager.list_config_versions()] == [
            "lending"
        ]


@pytest.mark.unit
def test_upload_to_a_stack_without_revision_history_prints_no_revision_number():
    """
    A stack deployed before revision history existed has no Configuration bucket, so
    the save cuts no revision. The command must not invent a number — a caller that
    pinned a fabricated `--config-revision` would be refused later, or worse, served
    somebody else's revision — so it falls back to naming the profile instead.
    """
    with config_stack(with_bucket=False) as stack:
        with write_config("classes:\n  - name: z\n") as runner:
            result = runner.invoke(
                cli,
                [
                    "config-upload",
                    "--stack-name",
                    STACK,
                    "--config-file",
                    "config.yaml",
                    "--config-profile",
                    "p1",
                    "--no-validate",
                    "--region",
                    REGION,
                ],
            )
        assert result.exit_code == 0, result.output
        assert "Configuration profile 'p1' created!" in result.output
        assert "Revision:" not in result.output
        assert "--config-revision" not in result.output
        assert "Use --config-profile to process documents with this profile." in (
            result.output
        )
        assert stack.item("p1") is not None


@pytest.mark.unit
def test_upload_uses_the_region_it_was_given():
    """
    A DynamoDB table name is not region-qualified, so resolving the stack in the
    wrong region is how an upload used to report success against another account's
    table. Naming a region the stack is not in must fail rather than fall back.
    """
    with config_stack():
        with write_config(VALID_MINIMAL) as runner:
            result = runner.invoke(
                cli,
                [
                    "config-upload",
                    "--stack-name",
                    STACK,
                    "--config-file",
                    "config.yaml",
                    "--config-profile",
                    "p1",
                    "--no-validate",
                    "--region",
                    "eu-west-3",
                ],
            )
    assert result.exit_code == 1
    assert "does not exist" in result.output


# --------------------------------------------------------------------------- #
# config-download — stdout is the payload
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_download_returns_the_profile_asked_for_not_the_active_one():
    """
    The assertion is on CONTENT, from two profiles whose configurations differ. A
    command that ignored `--config-profile` and served the active profile would
    pass any assertion about the argument reaching the SDK, and the caller would
    compare results from a configuration they never selected.
    """
    with config_stack() as stack:
        stack.seed("lending", class_name="lending-only", active=True)
        stack.seed("claims", class_name="claims-only")

        claims = invoke(
            ["config-download", "--stack-name", STACK, "--config-profile", "claims"]
        )
        lending = invoke(
            ["config-download", "--stack-name", STACK, "--config-profile", "lending"]
        )

    assert claims.exit_code == 0, claims.output
    assert [c["name"] for c in yaml.safe_load(claims.stdout)["classes"]] == [
        "claims-only"
    ]
    assert [c["name"] for c in yaml.safe_load(lending.stdout)["classes"]] == [
        "lending-only"
    ]


@pytest.mark.unit
def test_download_without_a_profile_resolves_the_active_one():
    with config_stack() as stack:
        stack.seed("lending", class_name="lending-only")
        stack.seed("claims", class_name="claims-only", active=True)
        result = invoke(["config-download", "--stack-name", STACK])

    assert result.exit_code == 0, result.output
    assert [c["name"] for c in yaml.safe_load(result.stdout)["classes"]] == [
        "claims-only"
    ]


@pytest.mark.unit
def test_download_to_stdout_keeps_progress_on_stderr():
    """
    `config-download > config.yaml` is the documented usage. A progress line on
    stdout parses as an extra top-level key, so the file is silently wrong — which
    is why the parsed keys, not the raw text, are what this asserts.
    """
    with config_stack() as stack:
        stack.seed("lending", class_name="lending-only", active=True)
        result = invoke(["config-download", "--stack-name", STACK])

    loaded = yaml.safe_load(result.stdout)
    assert "Downloading config from stack" not in loaded
    assert "classes" in loaded and "extraction" in loaded
    assert "Downloading config from stack" in result.stderr


@pytest.mark.unit
def test_download_to_a_file_writes_loadable_yaml_with_the_header_as_comments():
    with config_stack() as stack:
        stack.seed("lending", class_name="lending-only", active=True)
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                [
                    "config-download",
                    "--stack-name",
                    STACK,
                    "--config-profile",
                    "lending",
                    "--output",
                    "out.yaml",
                    "--region",
                    REGION,
                ],
            )
            written = open("out.yaml", encoding="utf-8").read()

    assert result.exit_code == 0, result.output
    assert "Configuration saved to: out.yaml" in result.output
    loaded = yaml.safe_load(written)
    assert [c["name"] for c in loaded["classes"]] == ["lending-only"]
    assert "Configuration downloaded from stack" not in loaded, (
        "the provenance header must be YAML comments, not a key"
    )


@pytest.mark.unit
def test_download_refuses_a_revision_that_is_not_retained():
    """
    Falling back to the profile head would hand back a DIFFERENT configuration than
    the one asked for, under the filename the caller chose. The revision path gets
    this right; the profile path (next test) does not.
    """
    with config_stack() as stack:
        stack.seed("lending", class_name="lending-only", active=True)
        result = invoke(
            [
                "config-download",
                "--stack-name",
                STACK,
                "--config-profile",
                "lending",
                "--config-revision",
                "99",
            ]
        )
    assert result.exit_code == 1
    flat = " ".join(result.output.split())
    assert "Revision r99 of configuration profile 'lending' is not available" in flat
    assert "Profile: lending (revision r99)" in result.stderr


@pytest.mark.unit
def test_download_of_an_unknown_profile_is_not_refused():
    """
    DEFECT (`idp_sdk/operations/config.py:346`, surfacing at
    `idp_cli/cli.py:4725-4736`).

    `ConfigurationReader.get_configuration` returns `None` for a profile that does
    not exist and `download()` never checks — unlike its own revision branch twelve
    lines above, which raises `IDPResourceNotFoundError` for exactly this reason.
    `yaml.dump(None)` is the string `"null\\n...\\n"`, and the CLI emits it.

    Consequence: `config-download --config-profile lendnig > config.yaml` exits 0,
    prints a success-shaped progress line on stderr, and leaves a file that
    `yaml.safe_load` reads as `None`. Every downstream step then operates on an
    empty configuration, and the exit code said it worked. This is the exact failure
    mode the revision branch was written to prevent.

    Pinned, not fixed. If this starts exiting non-zero, the defect is fixed and this
    test should be replaced by the refusal assertion, not loosened.
    """
    with config_stack() as stack:
        stack.seed("lending", class_name="lending-only", active=True)
        result = invoke(
            ["config-download", "--stack-name", STACK, "--config-profile", "lendnig"]
        )

    assert result.exit_code == 0, "the defect: an unknown profile is not refused"
    assert yaml.safe_load(result.stdout) is None
    assert "lendnig" not in result.output, (
        "and it does not even name the profile it failed to find"
    )


@pytest.mark.unit
def test_download_of_an_unknown_profile_to_a_file_reports_success():
    """
    The same defect through `--output`, which is worse because it leaves an artifact.
    The file is written, the command prints "✓ Configuration saved to", and the
    content is the YAML null document under a header claiming provenance from the
    stack.
    """
    with config_stack() as stack:
        stack.seed("lending", class_name="lending-only", active=True)
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                [
                    "config-download",
                    "--stack-name",
                    STACK,
                    "--config-profile",
                    "no-such-profile",
                    "--output",
                    "out.yaml",
                    "--region",
                    REGION,
                ],
            )
            written = open("out.yaml", encoding="utf-8").read()

    assert result.exit_code == 0
    assert "Configuration saved to: out.yaml" in result.output
    assert yaml.safe_load(written) is None
    assert "Configuration downloaded from stack" in written


@pytest.mark.unit
def test_download_of_an_unknown_profile_in_minimal_format_leaks_an_attributeerror():
    """
    The third shape of the same defect. `--format minimal` diffs the downloaded
    config against the defaults, so `get_diff_dict(defaults, None)` raises
    `AttributeError: 'NoneType' object has no attribute 'items'` and the CLI's
    generic handler prints it verbatim.

    Consequence: the exit code is at least non-zero here, which makes `--format
    minimal` the only one of the three spellings that fails — but the user is shown
    an internal type error instead of "that profile does not exist".
    """
    with config_stack() as stack:
        stack.seed("lending", class_name="lending-only", active=True)
        result = invoke(
            [
                "config-download",
                "--stack-name",
                STACK,
                "--config-profile",
                "no-such-profile",
                "--format",
                "minimal",
            ]
        )
    assert result.exit_code == 1
    assert "'NoneType' object has no attribute 'items'" in result.output
    assert "no-such-profile" not in result.output


# --------------------------------------------------------------------------- #
# config-activate
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_activate_moves_the_active_flag_and_writes_the_pointer():
    """
    Read back all three things activation touches: the target's `IsActive`, the
    previous holder's `IsActive`, and the `Config#__active` sentinel the queue path
    reads once per document. Leaving two profiles active, or a stale pointer, both
    mean documents process under a configuration nobody selected.
    """
    with config_stack() as stack:
        stack.seed("lending", class_name="lending-only", active=True)
        stack.seed("claims", class_name="claims-only")

        result = invoke(
            ["config-activate", "--stack-name", STACK, "--config-profile", "claims"]
        )
        assert result.exit_code == 0, result.output
        assert "Successfully activated configuration profile: claims" in result.output

        assert stack.attrs("claims")["IsActive"] is True
        assert stack.attrs("lending")["IsActive"] is False
        pointer = stack.table.get_item(Key={"Configuration": "Config#__active"})["Item"]
        assert pointer["ActiveVersion"] == "claims"


@pytest.mark.unit
def test_activate_refuses_a_profile_that_does_not_exist(api_calls):
    """
    Falling back to anything here would silently redirect every new document. The
    refusal must also point at `config-list`, since the likely cause is a typo and
    the user has no other way to see the available names.
    """
    with config_stack() as stack:
        stack.seed("lending", class_name="lending-only", active=True)
        api_calls.clear()
        result = invoke(
            ["config-activate", "--stack-name", STACK, "--config-profile", "clams"]
        )

        assert result.exit_code == 1
        flat = " ".join(result.output.split())
        assert "Failed to activate configuration profile 'clams'" in flat
        assert "does not exist" in flat
        assert f"idp-cli config-list --stack-name {STACK}" in flat

        assert api_calls.of("UpdateItem") == [], (
            "a refused activation must not touch any profile's IsActive"
        )
        assert stack.attrs("lending")["IsActive"] is True


@pytest.mark.unit
def test_activate_against_a_stack_that_does_not_exist_fails_with_its_own_wording():
    """
    A stack that cannot be resolved raises rather than returning a failed result, so
    it lands in the command's exception handler. That handler's message is
    deliberately different from the "profile does not exist" one — the first is about
    the stack, the second about a name inside it, and conflating them sends the user
    to `config-list` on a stack that is not there.
    """
    with config_stack():
        result = invoke(
            [
                "config-activate",
                "--stack-name",
                "no-such-stack",
                "--config-profile",
                "claims",
            ]
        )
    assert result.exit_code == 1
    assert "Failed to activate configuration:" in result.output
    assert "does not exist" in result.output
    assert "config-list" not in result.output


@pytest.mark.unit
def test_activate_reports_a_full_bda_sync():
    """
    A BDA-backed configuration syncs blueprints before activation. The counts are
    the only feedback a user gets, so they have to be printed — Bedrock Data
    Automation has no `moto` backend, hence the patched client here.
    """
    with patched_client() as client:
        client.config.activate.return_value = MagicMock(
            success=True,
            error=None,
            bda_synced=True,
            bda_classes_synced=3,
            bda_classes_failed=0,
        )
        result = invoke(
            ["config-activate", "--stack-name", STACK, "--config-profile", "bda-v2"]
        )
    assert result.exit_code == 0, result.output
    assert "Successfully synced 3 classes to BDA" in result.output
    assert client.config.activate.call_args.kwargs["config_version"] == "bda-v2"


@pytest.mark.unit
def test_activate_reports_a_partial_bda_sync_as_a_warning_but_still_activates():
    """
    A partial sync is deliberately not fatal — the SDK continues and activates — so
    the user has to be told which classes did not make it, or a silently
    half-synced BDA project looks like a clean activation.
    """
    with patched_client() as client:
        client.config.activate.return_value = MagicMock(
            success=True,
            error=None,
            bda_synced=True,
            bda_classes_synced=2,
            bda_classes_failed=1,
        )
        result = invoke(
            ["config-activate", "--stack-name", STACK, "--config-profile", "bda-v2"]
        )
    assert result.exit_code == 0, result.output
    assert "BDA sync partial: 2 succeeded, 1 failed" in result.output
    assert "Successfully activated configuration profile: bda-v2" in result.output


# --------------------------------------------------------------------------- #
# config-list
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_list_on_a_stack_with_no_profiles_says_so_and_exits_zero():
    """
    A stack with no profiles is a normal state, not an error: a script asking what
    exists must not fail. It also must not print an empty table, which reads as a
    rendering bug.
    """
    with config_stack():
        result = invoke(["config-list", "--stack-name", STACK])
    assert result.exit_code == 0, result.output
    assert "No configuration profiles found" in result.output
    assert "Profile Name" not in result.output


@pytest.mark.unit
def test_list_renders_every_profile_with_its_status_and_description():
    with config_stack() as stack:
        stack.seed("lending", class_name="a", description="Lending package")
        stack.seed("claims", class_name="b", description="Claims pack", active=True)

        result = invoke(["config-list", "--stack-name", STACK])

    assert result.exit_code == 0, result.output
    assert "Found 2 configuration profile(s)" in result.output
    assert "lending" in result.output and "claims" in result.output
    assert "Lending package" in result.output and "Claims pack" in result.output
    # Exactly one profile is active, and the table has to say which.
    assert result.output.count("ACTIVE") == 1
    claims_row = next(line for line in result.output.splitlines() if "claims" in line)
    assert "ACTIVE" in claims_row


@pytest.mark.unit
def test_list_fails_loudly_when_the_stack_does_not_exist():
    """Exit 0 with an empty listing would be indistinguishable from a real answer."""
    with config_stack():
        result = invoke(["config-list", "--stack-name", "no-such-stack"])
    assert result.exit_code == 1
    assert "Failed to list configurations" in result.output
    assert "does not exist" in result.output


# --------------------------------------------------------------------------- #
# config-revisions — only the failure path, the rest is test_config_revisions_cli.py
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_revisions_distinguishes_unreadable_history_from_no_history():
    """
    A stack with no Configuration bucket cannot store revision bodies, so every
    history read comes back empty. Reporting that as "this profile has no
    revisions" would be a confident wrong answer about a profile that may have
    plenty, so the SDK raises and the CLI exits 1 saying which of the two it is.
    """
    with config_stack(with_bucket=False) as stack:
        stack.seed("lending", class_name="a")
        result = invoke(
            ["config-revisions", "--stack-name", STACK, "--config-profile", "lending"]
        )
    assert result.exit_code == 1
    flat = " ".join(result.output.split())
    assert "Failed to list revisions" in flat
    assert "revision history is unavailable" in flat
    assert "not the same as the profile having no revisions" in flat


# --------------------------------------------------------------------------- #
# _profile_is_managed — advisory, and must never be the thing that fails
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_profile_is_managed_finds_a_managed_profile():
    client = MagicMock()
    client.config.list.return_value = MagicMock(
        versions=[
            MagicMock(version_name="lending", managed=False),
            MagicMock(version_name="claims-pack-v0.2.0", managed=True),
        ]
    )
    assert _profile_is_managed(client, "claims-pack-v0.2.0") is True


@pytest.mark.unit
@pytest.mark.parametrize("profile", ["lending", "never-heard-of-it"])
def test_profile_is_managed_is_false_for_unmanaged_and_unknown_profiles(profile):
    """
    The name has to match as well as the flag. Answering True for any managed
    profile in the stack would warn about the wrong thing on every delete.
    """
    client = MagicMock()
    client.config.list.return_value = MagicMock(
        versions=[
            MagicMock(version_name="lending", managed=False),
            MagicMock(version_name="claims-pack-v0.2.0", managed=True),
        ]
    )
    assert _profile_is_managed(client, profile) is False


@pytest.mark.unit
def test_profile_is_managed_swallows_a_failed_listing():
    """
    The answer is advisory — it only decides whether to print an extra warning — so
    a listing failure must not block a delete the user explicitly asked for. It
    returns False and logs at debug.
    """
    client = MagicMock()
    client.config.list.side_effect = RuntimeError("AccessDenied on Scan")
    assert _profile_is_managed(client, "anything") is False


# --------------------------------------------------------------------------- #
# config-delete
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_delete_answered_no_deletes_nothing(api_calls):
    """
    The negative case read off the recorded API calls rather than off a mock: no
    `DeleteItem` was submitted at all, and the record is still there afterwards. A
    mock would let a delete through if the confirmation were checked after the call
    instead of before it.
    """
    with config_stack() as stack:
        stack.seed("lending", class_name="a", active=True)
        stack.seed("claims", class_name="b")
        api_calls.clear()

        result = invoke(
            ["config-delete", "--stack-name", STACK, "--config-profile", "claims"],
            input="n\n",
        )

        assert result.exit_code == 0, result.output
        assert "Deletion cancelled" in result.output
        assert "DeleteItem" not in api_calls.operations()
        assert stack.item("claims") is not None


@pytest.mark.unit
def test_delete_answered_yes_removes_the_record():
    with config_stack() as stack:
        stack.seed("lending", class_name="a", active=True)
        stack.seed("claims", class_name="b")

        result = invoke(
            ["config-delete", "--stack-name", STACK, "--config-profile", "claims"],
            input="y\n",
        )

        assert result.exit_code == 0, result.output
        assert "Successfully deleted configuration profile: claims" in result.output
        assert stack.item("claims") is None
        # Only the named profile.
        assert stack.item("lending") is not None


@pytest.mark.unit
def test_delete_with_force_skips_the_prompt_entirely(api_calls):
    """
    `--force` is for scripts, so it must not read stdin — and it must not pay for
    the managed-profile listing either, since that is a table scan whose only
    purpose is the interactive warning.
    """
    with config_stack() as stack:
        stack.seed("lending", class_name="a", active=True)
        stack.seed("claims", class_name="b")
        api_calls.clear()

        result = invoke(
            [
                "config-delete",
                "--stack-name",
                STACK,
                "--config-profile",
                "claims",
                "--force",
            ]
        )

        assert result.exit_code == 0, result.output
        assert "Are you sure" not in result.output
        assert api_calls.of("Scan") == [], (
            "--force must not trigger the managed-profile lookup"
        )
        assert stack.item("claims") is None


@pytest.mark.unit
def test_delete_warns_before_removing_a_stack_managed_profile_and_then_does_it():
    """
    The Web UI refuses a managed profile outright, so the CLI is the only route for
    clearing up after an uninstalled extension. It is allowed, deliberately, but the
    warning has to say the owning stack would recreate it — otherwise this is how a
    built-in preset gets deleted and quietly restored on the next update.
    """
    with config_stack() as stack:
        stack.seed("lending", class_name="a", active=True)
        stack.seed("claims-pack-v0.2.0", class_name="b", managed=True)

        result = invoke(
            [
                "config-delete",
                "--stack-name",
                STACK,
                "--config-profile",
                "claims-pack-v0.2.0",
            ],
            input="y\n",
        )

        assert result.exit_code == 0, result.output
        flat = " ".join(result.output.split())
        assert "'claims-pack-v0.2.0' is a stack-managed profile." in flat
        assert "recreated on the next update" in flat
        assert stack.item("claims-pack-v0.2.0") is None


@pytest.mark.unit
def test_delete_does_not_warn_for_an_ordinary_profile():
    with config_stack() as stack:
        stack.seed("lending", class_name="a", active=True)
        stack.seed("claims", class_name="b")
        result = invoke(
            ["config-delete", "--stack-name", STACK, "--config-profile", "claims"],
            input="y\n",
        )
    assert result.exit_code == 0, result.output
    assert "stack-managed" not in result.output


@pytest.mark.unit
@pytest.mark.parametrize(
    "profile,expected",
    [
        ("default", "Cannot delete the 'default' configuration version"),
        ("lending", "Cannot delete active version lending"),
        ("never-existed", "Version: never-existed not found in configurations"),
    ],
)
def test_delete_refuses_the_three_profiles_it_must_not_remove(profile, expected):
    """
    Three separate refusals, each with its own reason, all at exit 1:

    - `default` is the fallback every document lands on when nothing is active.
    - the active profile is what new documents are processed under right now.
    - a profile that does not exist is a typo, and exiting 0 would tell a script
      that a profile it wanted gone is gone when something else still holds that
      name.

    The message matters as much as the code: "cannot delete" and "not found" call
    for different actions.
    """
    with config_stack() as stack:
        stack.seed("default", class_name="d")
        stack.seed("lending", class_name="a", active=True)

        result = invoke(
            [
                "config-delete",
                "--stack-name",
                STACK,
                "--config-profile",
                profile,
                "--force",
            ]
        )

        assert result.exit_code == 1
        assert expected in " ".join(result.output.split())
        # Nothing was removed, whichever refusal fired.
        assert stack.item("default") is not None
        assert stack.item("lending") is not None


@pytest.mark.unit
def test_delete_fails_loudly_when_the_stack_does_not_exist():
    with config_stack():
        result = invoke(
            [
                "config-delete",
                "--stack-name",
                "no-such-stack",
                "--config-profile",
                "claims",
                "--force",
            ]
        )
    assert result.exit_code == 1
    assert "does not exist" in result.output


# --------------------------------------------------------------------------- #
# config-sync-bda
# --------------------------------------------------------------------------- #


@contextmanager
def patched_bda(sync_result=None, project_error=None):
    """Patch only `BdaBlueprintService`, so the rest of the path runs for real.

    `moto` has no Bedrock Data Automation backend — `CreateBlueprint` answers 404
    "Not yet implemented" — so this is the narrowest seam that lets the DynamoDB
    half of `sync_bda` (resolving the active profile, recording `BdaSyncStatus`)
    run against real state while the blueprint calls are stubbed.

    `sync_result` entries must be keyed the way the real sync keys them —
    `{"status": ..., "class": ...}` — since that is what `sync_bda` reads the class
    names out of. Entries written with a key it does not emit made the assertions
    on the printed names below agree with a read that could never work:
    `test_config_operations_extended.py` derives the entries from the producer, and
    that is the test to change first if the key moves.
    """
    with patch("idp_common.bda.bda_blueprint_service.BdaBlueprintService") as svc_cls:
        service = MagicMock()
        svc_cls.return_value = service
        if project_error is not None:
            service.get_or_create_project_for_version.side_effect = project_error
        else:
            service.get_or_create_project_for_version.return_value = (
                "arn:aws:bedrock:us-east-1:123456789012:data-automation-project/p1"
            )
        service.create_blueprints_from_custom_configuration.return_value = (
            sync_result or []
        )
        yield service


@pytest.mark.unit
def test_sync_bda_submits_the_underscored_direction_for_the_active_profile():
    """
    The CLI spells directions with dashes and the SDK/BDA layer with underscores, so
    `idp-to-bda` has to arrive as `idp_to_bda`. An unrecognised direction does not
    error — it simply syncs nothing — so this translation is the whole request.

    Also asserts the two things only real state can show: that omitting
    `--config-profile` resolves the ACTIVE profile, and that a clean sync records
    `BdaSyncStatus` and the project ARN on that profile's record.
    """
    with config_stack() as stack:
        stack.seed("lending", class_name="invoice", active=True)
        stack.seed("claims", class_name="claim")

        with patched_bda(
            sync_result=[
                {"status": "success", "class": "invoice"},
                {"status": "success", "class": "receipt"},
            ]
        ) as service:
            result = invoke(
                [
                    "config-sync-bda",
                    "--stack-name",
                    STACK,
                    "--direction",
                    "idp-to-bda",
                    "--mode",
                    "merge",
                ]
            )

        assert result.exit_code == 0, result.output
        submitted = service.create_blueprints_from_custom_configuration.call_args.kwargs
        assert submitted == {
            "sync_direction": "idp_to_bda",
            "version": "lending",
            "sync_mode": "merge",
        }

        assert "BDA sync completed successfully" in result.output
        assert "Classes synced: 2" in result.output
        assert "• invoice" in result.output and "• receipt" in result.output

        lending = stack.attrs("lending")
        assert lending["BdaSyncStatus"] == "synced"
        assert lending["BdaProjectArn"].endswith("data-automation-project/p1")
        # The profile that was not named must be untouched.
        assert "BdaSyncStatus" not in stack.attrs("claims")


@pytest.mark.unit
def test_sync_bda_syncs_the_named_profile_rather_than_the_active_one():
    with config_stack() as stack:
        stack.seed("lending", class_name="invoice", active=True)
        stack.seed("claims", class_name="claim")

        with patched_bda(
            sync_result=[{"status": "success", "class": "claim"}]
        ) as service:
            result = invoke(
                [
                    "config-sync-bda",
                    "--stack-name",
                    STACK,
                    "--config-profile",
                    "claims",
                ]
            )

        assert result.exit_code == 0, result.output
        assert "Config profile: claims" in result.output
        submitted = service.create_blueprints_from_custom_configuration.call_args.kwargs
        assert submitted["version"] == "claims"
        assert submitted["sync_direction"] == "bidirectional"
        assert stack.attrs("claims")["BdaSyncStatus"] == "synced"
        assert "BdaSyncStatus" not in stack.attrs("lending")


@pytest.mark.unit
def test_sync_bda_with_nothing_to_change_succeeds_and_records_nothing():
    """
    A sync that finds no classes to align is a success, not an error: a script that
    syncs on every deploy must not fail on a no-op. The status attribute is
    deliberately NOT written in that case — there is nothing to claim was synced.
    """
    with config_stack() as stack:
        stack.seed("lending", class_name="invoice", active=True)

        with patched_bda(sync_result=[]):
            result = invoke(["config-sync-bda", "--stack-name", STACK])

        assert result.exit_code == 0, result.output
        assert "BDA sync completed successfully" in result.output
        assert "Classes synced: 0" in result.output
        assert "BdaSyncStatus" not in stack.attrs("lending")


@pytest.mark.unit
def test_sync_bda_fails_when_the_bda_project_cannot_be_resolved():
    """
    No project means no blueprint can be written, so this has to exit non-zero and
    surface the underlying reason — a sync that "completed" against a project that
    does not exist would leave the IDP classes and BDA silently divergent.
    """
    with config_stack() as stack:
        stack.seed("lending", class_name="invoice", active=True)

        with patched_bda(
            project_error=RuntimeError("Project not found: idp-lending")
        ) as service:
            result = invoke(
                ["config-sync-bda", "--stack-name", STACK, "--direction", "idp-to-bda"]
            )

        assert result.exit_code == 1
        assert "BDA sync completed with issues" in result.output
        assert "Project not found: idp-lending" in result.output
        assert not service.create_blueprints_from_custom_configuration.called
        assert "BdaSyncStatus" not in stack.attrs("lending")


@pytest.mark.unit
def test_sync_bda_reports_a_partial_sync_as_a_failure():
    """
    One class that did not sync means the IDP configuration and the BDA project
    disagree about that class, so the command exits 1 and prints both counts. The
    SDK still records `partial` on the profile, which is how a later run can tell
    this happened.
    """
    with config_stack() as stack:
        stack.seed("lending", class_name="invoice", active=True)

        with patched_bda(
            sync_result=[
                {"status": "success", "class": "invoice"},
                {"status": "failed", "class": "receipt"},
            ]
        ):
            result = invoke(["config-sync-bda", "--stack-name", STACK])

        assert result.exit_code == 1
        assert "Classes synced: 1" in result.output
        assert "Classes failed: 1" in result.output
        assert "1 class(es) failed to sync" in result.output
        assert stack.attrs("lending")["BdaSyncStatus"] == "partial"


@pytest.mark.unit
def test_sync_bda_against_a_stack_that_does_not_exist_fails():
    """
    Stack resolution happens before `sync_bda`'s own error handling, so this reaches
    the command's exception handler rather than the "completed with issues" path. It
    must still be exit 1 — a sync reported as done against a stack that is not there
    would leave a caller believing its classes are in BDA.
    """
    with config_stack():
        result = invoke(["config-sync-bda", "--stack-name", "no-such-stack"])
    assert result.exit_code == 1
    assert "does not exist" in result.output
    assert "completed successfully" not in result.output


@pytest.mark.unit
def test_sync_bda_rejects_an_unknown_direction_and_mode_up_front():
    """
    Both are `click.Choice`, so a typo is a usage error rather than a sync that
    quietly does nothing. Exit 2, and no AWS call at all.
    """
    bad_direction = CliRunner().invoke(
        cli, ["config-sync-bda", "--stack-name", STACK, "--direction", "sideways"]
    )
    bad_mode = CliRunner().invoke(
        cli, ["config-sync-bda", "--stack-name", STACK, "--mode", "obliterate"]
    )
    assert bad_direction.exit_code == 2
    assert "sideways" in bad_direction.output
    assert bad_mode.exit_code == 2
    assert "obliterate" in bad_mode.output
