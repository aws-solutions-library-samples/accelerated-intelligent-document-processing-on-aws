# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`operations/config.py` exercised against a real (moto) stack, end to end.

`ConfigOperation` is the whole programmatic surface for Configuration Profiles:
generate a template, validate a file, upload it, list what a stack holds,
activate one, download it back (head or an exact revision), delete it, and sync
BDA blueprints from it. Every one of those methods resolves the stack's
`ConfigurationTable` and `ConfigurationBucket` out of CloudFormation, exports
them into the environment, and then hands the work to `idp_common`'s
`ConfigurationManager`.

**What shaped this file: the mocks were hiding the part that matters.** The
pre-existing tests replace `ConfigurationManager` with a `MagicMock`, which
accepts any call, records it, and stores nothing. A `MagicMock` cannot tell you
that a profile was written under the wrong key, that `Managed` landed as the
string `"False"`, that activation stored `IsActive` on the wrong item, or that an
aborted BDA sync activated the profile anyway — and those are the failures that
produce no error message.

So the bulk of this file builds a **real** CloudFormation stack in `moto` whose
template declares a real DynamoDB table with the production key schema
(`Configuration` as the hash key) and a real S3 bucket, runs the operation, and
then reads the item back with `get_item` and asserts the stored attributes **and
their types**. The revision bodies really are gzipped into S3 by
`ConfigRevisionStore`, so `download(config_revision=...)` really does round-trip
through it.

Narrow patching is used in two places where a real fake cannot reach: the BDA
blueprint service, which talks to Bedrock Data Automation, and a handful of
single `ConfigurationManager` methods forced to raise so the operation's error
handling is exercised with a specific exception type. In both cases the
assertions are on the arguments passed and on the state left in DynamoDB
afterwards, not on the fact that a mock was called.

Coverage that is deliberately not repeated here: `upload()`'s revision reporting
and the download-by-revision path (`test_config_revisions_api.py`), the
"every construction site passes a region" invariant
(`test_config_operations_region.py`), and the managed-config rejection
(`test_config_operations.py`).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import pytest
import yaml
from moto import mock_aws

from idp_sdk import IDPClient
from idp_sdk.exceptions import IDPResourceNotFoundError
from idp_sdk.models import (
    ConfigCreateResult,
    ConfigDownloadResult,
    ConfigSyncBdaResult,
    ConfigValidationResult,
)

# The two logical IDs `_configure_config_env` looks for, with the production key
# schema. `Configuration` as a single hash key is what `ConfigurationManager`
# writes against, so a wrong schema here fails loudly instead of silently
# accepting whatever shape the code sends.
STACK_TEMPLATE = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Resources": {
        "ConfigurationTable": {
            "Type": "AWS::DynamoDB::Table",
            "Properties": {
                "KeySchema": [{"AttributeName": "Configuration", "KeyType": "HASH"}],
                "AttributeDefinitions": [
                    {"AttributeName": "Configuration", "AttributeType": "S"}
                ],
                "BillingMode": "PAY_PER_REQUEST",
            },
        },
        "ConfigurationBucket": {"Type": "AWS::S3::Bucket"},
    },
}

# A stack whose configuration table exists but whose Configuration bucket does
# not: an older deployment, where revision history is unavailable.
STACK_TEMPLATE_NO_BUCKET = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Resources": {
        "ConfigurationTable": STACK_TEMPLATE["Resources"]["ConfigurationTable"]
    },
}

# A stack with neither: nothing for the config layer to attach to.
STACK_TEMPLATE_EMPTY = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Resources": {"Unrelated": {"Type": "AWS::SQS::Queue"}},
}

STACK = "idp-cfg"


@pytest.fixture
def config_env(monkeypatch):
    """Contain the environment variables the operations write.

    `_configure_config_env` sets `CONFIGURATION_TABLE_NAME`,
    `CONFIGURATION_BUCKET` and (via `activate`/`sync_bda`) `STACK_NAME` with a
    bare `os.environ[...] = ...`, which no fixture would otherwise undo. Setting
    each one through `monkeypatch` first makes monkeypatch record the prior state
    — including its absence — so teardown restores it. Without this, one test's
    table name leaks into the next and a later test can pass by reading the
    earlier test's table.
    """
    for name in ("CONFIGURATION_TABLE_NAME", "CONFIGURATION_BUCKET", "STACK_NAME"):
        monkeypatch.setenv(name, "set-by-fixture-so-teardown-restores-it")
        monkeypatch.delenv(name)


def _create_stack(region: str, template: dict = STACK_TEMPLATE, name: str = STACK):
    boto3.client("cloudformation", region_name=region).create_stack(
        StackName=name, TemplateBody=json.dumps(template)
    )


def _table(region: str):
    """The one DynamoDB table CloudFormation created, as a boto3 resource."""
    names = boto3.client("dynamodb", region_name=region).list_tables()["TableNames"]
    assert len(names) == 1, f"expected exactly one table, got {names}"
    return boto3.resource("dynamodb", region_name=region).Table(names[0])


def _item(region: str, key: str):
    return _table(region).get_item(Key={"Configuration": key}).get("Item")


def _write_config(path: Path, **body) -> str:
    path.write_text(yaml.dump(body or {"classes": []}), encoding="utf-8")
    return str(path)


def _client(region: str) -> IDPClient:
    return IDPClient(stack_name=STACK, region=region)


# --------------------------------------------------------------------------
# create() and validate() — no stack, no AWS
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.config
class TestCreate:
    def test_a_comma_separated_feature_list_is_split(self):
        """`features="min,summarization"` is two feature names, not one.

        The split is what turns the CLI's `--features min,summarization` into the
        list `generate_config_template` expects, and it is load-bearing rather
        than cosmetic: handed the unsplit string the generator refuses it
        outright, which is asserted here directly so the reason this branch
        exists is visible. The observable effect of the split is that
        `summarization` comes out enabled.
        """
        from idp_common.config.merge_utils import generate_config_template

        client = IDPClient()

        one = client.config.create(features="min")
        several = client.config.create(features="min,summarization")

        assert isinstance(one, ConfigCreateResult)
        assert yaml.safe_load(one.yaml_content)["summarization"]["enabled"] is False
        assert yaml.safe_load(several.yaml_content)["summarization"]["enabled"] is True

        with pytest.raises(ValueError, match="Invalid feature set"):
            generate_config_template(
                features="min,summarization",
                pattern="pattern-2",
                include_prompts=False,
                include_comments=True,
            )

    def test_whitespace_around_a_feature_name_is_tolerated(self):
        """`--features "min, summarization"` is what a shell user types."""
        padded = IDPClient().config.create(features="min , summarization ")
        tight = IDPClient().config.create(features="min,summarization")

        assert padded.yaml_content == tight.yaml_content

    def test_the_generated_template_is_written_and_reparses(self, tmp_path):
        output = tmp_path / "config.yaml"

        result = IDPClient().config.create(features="min", output=str(output))

        assert result.output_path == str(output)
        assert yaml.safe_load(output.read_text(encoding="utf-8")) == yaml.safe_load(
            result.yaml_content
        )

    def test_no_output_path_writes_nothing(self, tmp_path):
        result = IDPClient().config.create(features="min")

        assert result.output_path is None
        assert list(tmp_path.iterdir()) == []


@pytest.mark.unit
@pytest.mark.config
class TestValidate:
    def test_broken_yaml_is_reported_as_a_syntax_error(self, tmp_path):
        """The message has to name YAML, or the user hunts for a schema problem."""
        bad = tmp_path / "broken.yaml"
        bad.write_text("classes: [unclosed\n  nested: {", encoding="utf-8")

        result = IDPClient().config.validate(str(bad))

        assert isinstance(result, ConfigValidationResult)
        assert result.valid is False
        assert len(result.errors) == 1
        assert result.errors[0].startswith("YAML syntax error:")

    def test_an_unreadable_file_is_reported_without_raising(self, tmp_path):
        """A typo'd path must not surface as a traceback from deep in a loader."""
        result = IDPClient().config.validate(str(tmp_path / "absent.yaml"))

        assert result.valid is False
        assert result.errors[0].startswith("Failed to load file:")

    def test_a_schema_violation_is_reported_as_invalid(self, tmp_path):
        config = tmp_path / "c.yaml"
        config.write_text("ocr: not-a-mapping\n", encoding="utf-8")

        result = IDPClient().config.validate(str(config))

        assert result.valid is False
        assert any("ocr" in error for error in result.errors)

    def test_deprecated_and_unknown_top_level_fields_are_separated(self, tmp_path):
        """The two lists mean different things to a caller.

        A deprecated field is one the pipeline used to honour and now ignores —
        the config is still correct, just carrying dead weight. An unknown field
        is very likely a typo, and the value the user meant to set is not set at
        all. Collapsing them into one list loses that distinction, and `strict`
        mode is documented to let the caller fail on them.
        """
        config = tmp_path / "c.yaml"
        config.write_text(
            yaml.dump({"classes": [], "output_bucket": "b", "clasification": {}}),
            encoding="utf-8",
        )

        result = IDPClient().config.validate(str(config))

        assert result.deprecated_fields == ["output_bucket"]
        assert result.unknown_fields == ["clasification"]
        # The prose comes from `idp_common`'s `_validate_ignored_keys`, the single
        # reporter both this method and `idp_cli`'s `config-validate` now consume.
        # Asserting on it here is what pins that the SDK forwards the warnings
        # rather than re-deriving its own set, which is what it used to do — a raw
        # `set(config) - set(IDPConfig.model_fields)` that named two keys the
        # loader honours.
        assert any(
            "Deprecated configuration key 'output_bucket'" in w for w in result.warnings
        )
        assert any(
            "Unknown configuration key 'clasification'" in w for w in result.warnings
        )
        # The nearest declared field, which only the central reporter knows.
        assert any("Did you mean 'classification'?" in w for w in result.warnings)

    def test_a_clean_config_reports_neither(self, tmp_path):
        config = tmp_path / "c.yaml"
        config.write_text(yaml.dump({"classes": [], "notes": "fine"}), encoding="utf-8")

        result = IDPClient().config.validate(str(config))

        assert result.valid is True
        assert result.deprecated_fields == []
        assert result.unknown_fields == []

    def test_show_merged_controls_whether_the_merged_config_is_returned(self, tmp_path):
        """The merged config is large; it is returned only when asked for."""
        config = tmp_path / "c.yaml"
        config.write_text(yaml.dump({"notes": "mine"}), encoding="utf-8")
        client = IDPClient()

        assert client.config.validate(str(config)).merged_config is None
        merged = client.config.validate(str(config), show_merged=True).merged_config
        assert merged is not None
        assert merged["notes"] == "mine"
        assert "ocr" in merged, "the merge filled in the system defaults"

    # There is no test here for `idp_common.config.models` being unavailable.
    # `validate()` used to import it itself, behind an `ImportError` guard, to
    # compute its own deprecated/unknown-field lists, and a test pinned that the
    # guard kept the method returning a verdict on a trimmed `idp_common` install.
    # Both the import and the guard are gone: the lists now come from
    # `ignored_keys` on `validate_config`'s result, and `_validate_ignored_keys`
    # imports the module unguarded one frame up. A trimmed install therefore
    # raises `ModuleNotFoundError` out of `validate()`, and pinning *that* would
    # record a gap as the intended contract. The behaviour worth having belongs to
    # `idp_common`, not to this method.


# --------------------------------------------------------------------------
# Stack resource resolution
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.config
class TestStackResourceResolution:
    @mock_aws
    def test_the_configuration_table_is_resolved_to_its_physical_name(
        self, aws_credentials, config_env
    ):
        _create_stack(aws_credentials)

        resolved = _client(aws_credentials).config._get_config_table(STACK)

        assert resolved == _table(aws_credentials).name

    @mock_aws
    def test_a_stack_without_a_configuration_table_is_reported_by_name(
        self, aws_credentials, config_env
    ):
        _create_stack(aws_credentials, STACK_TEMPLATE_EMPTY)

        with pytest.raises(IDPResourceNotFoundError, match=f"'{STACK}'"):
            _client(aws_credentials).config._get_config_table(STACK)

    @mock_aws
    def test_only_the_requested_logical_ids_come_back(
        self, aws_credentials, config_env
    ):
        """The lookup is one pass over the stack, filtered to what was asked for.

        Returning the whole stack would work and would be much more expensive on
        a real deployment, which has hundreds of resources across nested stacks.
        """
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config

        found = operation._lookup_stack_resources(STACK, {"ConfigurationBucket"})

        assert set(found) == {"ConfigurationBucket"}

    @mock_aws
    def test_a_missing_configuration_bucket_is_a_warning_not_a_failure(
        self, aws_credentials, config_env, caplog
    ):
        """History is optional; the table is not.

        An older deployment has no Configuration bucket, and every non-revision
        operation still has to work against it. What must not happen is silence:
        `ConfigRevisionStore` treats an unset `CONFIGURATION_BUCKET` as "history
        disabled" and then does nothing, so the warning is the only signal.
        """
        _create_stack(aws_credentials, STACK_TEMPLATE_NO_BUCKET)
        import os

        with caplog.at_level("WARNING"):
            table = _client(aws_credentials).config._configure_config_env(STACK)

        assert table == _table(aws_credentials).name
        assert os.environ["CONFIGURATION_TABLE_NAME"] == table
        assert "CONFIGURATION_BUCKET" not in os.environ
        assert "revision history is unavailable" in caplog.text

    @mock_aws
    def test_both_the_table_and_the_bucket_are_exported(
        self, aws_credentials, config_env
    ):
        _create_stack(aws_credentials)
        import os

        _client(aws_credentials).config._configure_config_env(STACK)

        assert os.environ["CONFIGURATION_TABLE_NAME"] == _table(aws_credentials).name
        buckets = boto3.client("s3", region_name=aws_credentials).list_buckets()
        assert os.environ["CONFIGURATION_BUCKET"] == buckets["Buckets"][0]["Name"]
        assert os.environ["AWS_DEFAULT_REGION"] == aws_credentials

    @mock_aws
    def test_a_stack_with_no_configuration_table_fails_the_env_setup(
        self, aws_credentials, config_env
    ):
        _create_stack(aws_credentials, STACK_TEMPLATE_EMPTY)

        with pytest.raises(IDPResourceNotFoundError, match="ConfigurationTable"):
            _client(aws_credentials).config._configure_config_env(STACK)


# --------------------------------------------------------------------------
# upload() — what actually lands in DynamoDB
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.config
class TestUploadWritesRealItems:
    @mock_aws
    def test_a_new_profile_is_stored_under_its_own_key_with_typed_attributes(
        self, aws_credentials, config_env, tmp_path
    ):
        """The item, its key and the types of its attributes.

        Every one of these is something a `MagicMock` would have accepted
        silently: the key `Config#<profile>` (not a bare `Config`, which is where
        the stack's own default lives and would be overwritten), `Managed` as a
        DynamoDB boolean rather than the string `"False"`, and the revision
        counters as numbers. `Managed` in particular is load-bearing — a profile
        stored as managed is treated as stack-controlled and overwritten on the
        next stack update, so a user's upload would vanish on deploy.
        """
        from decimal import Decimal

        _create_stack(aws_credentials)
        config = _write_config(tmp_path / "c.yaml", classes=[], notes="first upload")

        result = _client(aws_credentials).config.upload(
            config_file=config, config_profile="tuning", validate=False
        )

        assert result.success is True
        assert result.version == "tuning"
        assert result.version_created is True
        assert result.revision == 1

        item = _item(aws_credentials, "Config#tuning")
        assert item is not None, "nothing was written under Config#tuning"
        assert item["Managed"] is False
        assert isinstance(item["LatestRevision"], Decimal)
        assert item["LatestRevision"] == Decimal(1)
        assert _item(aws_credentials, "Config") is None, (
            "a named profile must not be written to the bare Config key"
        )

    @mock_aws
    def test_the_second_upload_of_a_profile_is_not_a_creation(
        self, aws_credentials, config_env, tmp_path
    ):
        """`version_created` distinguishes "made" from "updated".

        A caller that treats every upload as a creation would, for instance,
        re-announce a profile to its users on every tuning iteration.
        """
        _create_stack(aws_credentials)
        config = tmp_path / "c.yaml"
        operation = _client(aws_credentials).config

        first = operation.upload(
            config_file=_write_config(config, notes="v1"),
            config_profile="tuning",
            validate=False,
        )
        second = operation.upload(
            config_file=_write_config(config, notes="v2"),
            config_profile="tuning",
            validate=False,
        )

        assert (first.version_created, second.version_created) == (True, False)
        assert (first.revision, second.revision) == (1, 2)

    @mock_aws
    def test_revision_notes_and_author_are_recorded_on_the_revision(
        self, aws_credentials, config_env, tmp_path
    ):
        """Read back through `revisions()`, not from the call arguments.

        `revision_notes` and `created_by` have to survive the whole path —
        operation, manager, revision store, S3 and the DynamoDB index — before a
        history listing can show them. Asserting the keyword reached
        `handle_update_custom_configuration` would have passed even when the
        store dropped them.
        """
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        config = tmp_path / "c.yaml"

        operation.upload(
            config_file=_write_config(config, notes="v1"),
            config_profile="tuning",
            validate=False,
        )
        operation.upload(
            config_file=_write_config(config, notes="v2"),
            config_profile="tuning",
            validate=False,
            revision_notes="raised topK to 20",
            created_by="nightly-optimizer",
        )

        newest = operation.revisions(config_profile="tuning").revisions[0]
        assert newest.revision == 2
        assert newest.notes == "raised topK to 20"
        assert newest.created_by == "nightly-optimizer"

    @mock_aws
    def test_an_unreadable_config_file_is_reported_and_writes_nothing(
        self, aws_credentials, config_env, tmp_path
    ):
        _create_stack(aws_credentials)
        broken = tmp_path / "c.yaml"
        broken.write_text("classes: [unclosed\n  x: {", encoding="utf-8")

        result = _client(aws_credentials).config.upload(
            config_file=str(broken), config_profile="tuning"
        )

        assert result.success is False
        assert result.error is not None and result.error.startswith(
            "Failed to load config:"
        )
        assert _item(aws_credentials, "Config#tuning") is None

    @mock_aws
    def test_a_config_that_fails_validation_is_not_uploaded(
        self, aws_credentials, config_env, tmp_path
    ):
        """Validation is a gate, not a report: nothing reaches DynamoDB.

        This is the assertion a mock cannot make. With `ConfigurationManager`
        replaced, "was not uploaded" can only mean "a method was not called";
        here it means the table has no such item.
        """
        _create_stack(aws_credentials)
        config = tmp_path / "c.yaml"
        config.write_text("ocr: not-a-mapping\n", encoding="utf-8")

        result = _client(aws_credentials).config.upload(
            config_file=str(config), config_profile="tuning", validate=True
        )

        assert result.success is False
        assert result.error is not None and result.error.startswith(
            "Validation failed:"
        )
        assert _item(aws_credentials, "Config#tuning") is None

    @mock_aws
    def test_validate_false_skips_the_gate(self, aws_credentials, config_env, tmp_path):
        """The same file that was refused above is stored when validation is off."""
        _create_stack(aws_credentials)
        config = tmp_path / "c.yaml"
        config.write_text("classes: []\nnotes: not-validated\n", encoding="utf-8")

        result = _client(aws_credentials).config.upload(
            config_file=str(config), config_profile="tuning", validate=False
        )

        assert result.success is True
        assert _item(aws_credentials, "Config#tuning") is not None

    @mock_aws
    def test_a_json_config_file_is_parsed_as_json(
        self, aws_credentials, config_env, tmp_path
    ):
        """The `.json` suffix selects the parser, and the content round-trips."""
        _create_stack(aws_credentials)
        config = tmp_path / "c.json"
        config.write_text(json.dumps({"classes": [], "notes": "from json"}))

        operation = _client(aws_credentials).config
        assert operation.upload(
            config_file=str(config), config_profile="j", validate=False
        ).success
        assert operation.download(config_profile="j").config["notes"] == "from json"

    @mock_aws
    def test_an_unreadable_existence_check_is_treated_as_a_new_profile(
        self, aws_credentials, config_env, tmp_path, monkeypatch
    ):
        """A throttled `get_configuration` must not lose the `saveAsVersion` flag.

        `upload` probes for the profile to decide whether the save is creating a
        version record. If that probe raises — throttling, a transient
        `ClientError` — the safe answer is "assume new", because omitting
        `saveAsVersion` on a genuinely new profile stores a configuration that no
        version record points at. The item written below is what proves the flag
        took effect.
        """
        from idp_common.config.configuration_manager import ConfigurationManager

        _create_stack(aws_credentials)
        original = ConfigurationManager.get_configuration
        calls: list[int] = []

        def throttle_the_first_read(self, *args, **kwargs):
            """Fail once, then behave — the shape of a real throttle.

            Patching the method to raise permanently would break the save itself,
            which reads the profile again, and the test would then be asserting
            the outer error handler rather than the probe's fallback.
            """
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("throttled")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(
            ConfigurationManager, "get_configuration", throttle_the_first_read
        )
        config = _write_config(tmp_path / "c.yaml", classes=[], notes="n")

        result = _client(aws_credentials).config.upload(
            config_file=config, config_profile="tuning", validate=False
        )

        assert result.success is True
        assert result.version_created is True
        assert _item(aws_credentials, "Config#tuning") is not None

    @mock_aws
    def test_a_write_failure_is_returned_rather_than_raised(
        self, aws_credentials, config_env, tmp_path
    ):
        """The table named by CloudFormation does not exist any more.

        Produced for real by deleting the DynamoDB table out from under a live
        stack, which is what a half-rolled-back stack update looks like. The
        operation has to answer `success=False` with the underlying error rather
        than raise a `ClientError` at a caller that is handling a result object.
        """
        _create_stack(aws_credentials)
        table_name = _table(aws_credentials).name
        boto3.client("dynamodb", region_name=aws_credentials).delete_table(
            TableName=table_name
        )
        config = _write_config(tmp_path / "c.yaml", classes=[])

        result = _client(aws_credentials).config.upload(
            config_file=config, config_profile="tuning", validate=False
        )

        assert result.success is False
        assert result.error is not None
        assert table_name in result.error or "not found" in result.error.lower()


# --------------------------------------------------------------------------
# list()
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.config
class TestList:
    @mock_aws
    def test_profiles_are_listed_with_their_real_stored_metadata(
        self, aws_credentials, config_env, tmp_path
    ):
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        config = tmp_path / "c.yaml"

        operation.upload(
            config_file=_write_config(config, notes="a"),
            config_profile="alpha",
            validate=False,
            description="first profile",
        )
        operation.upload(
            config_file=_write_config(config, notes="b"),
            config_profile="beta",
            validate=False,
        )
        operation.activate(config_profile="beta")

        result = operation.list()
        by_name = {v.version_name: v for v in result.versions}

        assert result.count == len(result.versions) == 2
        assert by_name["alpha"].is_active is False
        assert by_name["beta"].is_active is True
        assert by_name["alpha"].description == "first profile"
        assert by_name["alpha"].managed is False
        assert by_name["alpha"].latest_revision == 1
        assert by_name["alpha"].created_at is not None

    @mock_aws
    def test_a_stack_with_no_profiles_lists_nothing(self, aws_credentials, config_env):
        _create_stack(aws_credentials)

        result = _client(aws_credentials).config.list()

        assert (result.count, result.versions) == (0, [])

    @mock_aws
    def test_a_deleted_table_is_reported_as_an_empty_stack(
        self, aws_credentials, config_env
    ):
        """DEFECT — `operations/config.py:617-619`, and one frame up.

        "No profiles" and "could not read the table" are different answers and
        this reports them identically. `list_config_versions` catches its own
        `ResourceNotFoundException`, logs it and returns `[]`, so the operation's
        `except` never fires and the caller receives `count=0` — which reads as
        "this stack has no configuration profiles". A caller acting on that goes
        on to create one, against a table that is not there.

        The remedy is upstream (the manager should not swallow a read failure) or
        here (verify the table exists before believing an empty list), so this is
        a pin rather than a fix. The narrow-patch test below covers the raise path
        that a failure escaping the manager *does* take.
        """
        _create_stack(aws_credentials)
        boto3.client("dynamodb", region_name=aws_credentials).delete_table(
            TableName=_table(aws_credentials).name
        )

        result = _client(aws_credentials).config.list()

        assert result.count == 0
        assert result.versions == []

    @mock_aws
    def test_a_failure_that_escapes_the_manager_is_raised_with_its_cause(
        self, aws_credentials, config_env, monkeypatch
    ):
        """Anything the manager does not swallow becomes IDPResourceNotFoundError.

        The original exception is kept as `__cause__` (`raise ... from e`), which
        is what makes a throttle distinguishable from a permissions failure in a
        traceback.
        """
        from idp_common.config.configuration_manager import ConfigurationManager

        _create_stack(aws_credentials)
        monkeypatch.setattr(
            ConfigurationManager,
            "list_config_versions",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("AccessDenied")),
        )

        with pytest.raises(IDPResourceNotFoundError, match="Failed to list") as caught:
            _client(aws_credentials).config.list()

        assert isinstance(caught.value.__cause__, RuntimeError)
        assert "AccessDenied" in str(caught.value.__cause__)


# --------------------------------------------------------------------------
# download()
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.config
class TestDownload:
    @mock_aws
    def test_no_profile_named_resolves_the_active_one(
        self, aws_credentials, config_env, tmp_path
    ):
        """`download()` with no arguments means "whatever the stack is running".

        Two profiles exist and only one is active. Getting this wrong hands back
        a configuration the deployment is not using, under a filename that says
        nothing about which profile it came from.
        """
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        config = tmp_path / "c.yaml"

        operation.upload(
            config_file=_write_config(config, classes=[], notes="idle-profile"),
            config_profile="idle",
            validate=False,
        )
        operation.upload(
            config_file=_write_config(config, classes=[], notes="live-profile"),
            config_profile="live",
            validate=False,
        )
        operation.activate(config_profile="live")

        result = operation.download()

        assert isinstance(result, ConfigDownloadResult)
        assert result.config["notes"] == "live-profile"
        assert result.revision is None

    @mock_aws
    def test_with_no_active_profile_the_default_name_is_used(
        self, aws_credentials, config_env, tmp_path
    ):
        """A stack where nothing has been activated falls back to `default`.

        There is no `Config#default` here, so the profile that does exist must
        *not* be returned — a "pick any" fallback would hand back a configuration
        the stack is not running, under a filename that says nothing about which
        one it is.

        What comes back instead is a result whose two representations of the same
        thing disagree, and that is worth pinning: `config` is `{}` because the
        model coerces the missing configuration with `config_data or {}`, while
        `yaml_content` was dumped *before* that coercion and is the string
        `"null\\n...\\n"`. Writing that to `output` produces a YAML file whose
        entire content is `null`, from a call that reported no error at all
        (`operations/config.py:372-398`).
        """
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(tmp_path / "c.yaml", notes="not-active"),
            config_profile="only",
            validate=False,
        )
        output = tmp_path / "downloaded.yaml"

        result = operation.download(output=str(output))

        assert result.config == {}, "the absent profile is not substituted"
        assert result.yaml_content.strip() == "null\n...", (
            "yaml_content still carries the un-coerced None"
        )
        assert yaml.safe_load(output.read_text(encoding="utf-8")) is None

    @mock_aws
    def test_the_written_file_carries_the_stack_and_format_provenance(
        self, aws_credentials, config_env, tmp_path
    ):
        """A downloaded file on disk has to say where it came from."""
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(tmp_path / "c.yaml", notes="n"),
            config_profile="p",
            validate=False,
        )
        output = tmp_path / "downloaded.yaml"

        operation.download(config_profile="p", output=str(output))

        lines = output.read_text(encoding="utf-8").splitlines()
        assert lines[0] == f"# Configuration downloaded from stack: {STACK}"
        assert lines[1] == "# Format: full"
        assert "revision" not in lines[1]
        assert yaml.safe_load(output.read_text(encoding="utf-8"))["notes"] == "n"

    @mock_aws
    def test_minimal_format_returns_only_what_differs_from_the_defaults(
        self, aws_credentials, config_env, tmp_path
    ):
        """The point of `minimal` is a file a human can read and re-upload.

        Asserted against the real `get_diff_dict` over the real pattern-2
        defaults, so the test says "this is the diff" rather than "this is
        smaller".
        """
        from idp_common.config.merge_utils import get_diff_dict, load_system_defaults

        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(
                tmp_path / "c.yaml", classes=[], notes="only-this-changed"
            ),
            config_profile="p",
            validate=False,
        )

        full = operation.download(config_profile="p")
        minimal = operation.download(config_profile="p", format="minimal")

        assert minimal.config == get_diff_dict(
            load_system_defaults("pattern-2"), full.config
        )
        assert minimal.config["notes"] == "only-this-changed"
        assert len(minimal.config) < len(full.config)

    @mock_aws
    def test_an_explicit_pattern_overrides_the_auto_detection(
        self, aws_credentials, config_env, tmp_path
    ):
        """`pattern=` short-circuits the `classificationMethod` sniff.

        Passing the pattern explicitly is also the only way a BDA-flavoured
        config can be downloaded as `minimal` today — see the defect pinned
        below.
        """
        from idp_common.config.merge_utils import get_diff_dict, load_system_defaults

        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(
                tmp_path / "c.yaml",
                classes=[],
                use_bda=True,
                classification={"classificationMethod": "bda"},
            ),
            config_profile="bda",
            validate=False,
        )

        minimal = operation.download(
            config_profile="bda", format="minimal", pattern="pattern-2"
        )

        assert minimal.config == get_diff_dict(
            load_system_defaults("pattern-2"),
            operation.download(config_profile="bda").config,
        )


# --------------------------------------------------------------------------
# activate()
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.config
class TestActivate:
    @mock_aws
    def test_activation_marks_the_profile_and_clears_the_previous_one(
        self, aws_credentials, config_env, tmp_path
    ):
        """Exactly one profile is active, verified on the stored items.

        The failure worth catching is a second activation that sets the new
        profile without clearing the old, leaving two records claiming
        `IsActive` and the queue-time lookup picking whichever it reaches first.
        """
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        config = tmp_path / "c.yaml"
        for name in ("first", "second"):
            operation.upload(
                config_file=_write_config(config, notes=name),
                config_profile=name,
                validate=False,
            )

        operation.activate(config_profile="first")
        result = operation.activate(config_profile="second")

        assert result.success is True
        assert result.activated_version == "second"
        assert result.bda_synced is False
        assert _item(aws_credentials, "Config#second")["IsActive"] is True
        assert _item(aws_credentials, "Config#first").get("IsActive") is not True

    @mock_aws
    def test_activating_a_profile_that_does_not_exist_changes_nothing(
        self, aws_credentials, config_env, tmp_path
    ):
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(tmp_path / "c.yaml", notes="n"),
            config_profile="real",
            validate=False,
        )

        result = operation.activate(config_profile="imaginary")

        assert result.success is False
        assert result.error is not None and "does not exist" in result.error
        assert _item(aws_credentials, "Config#real").get("IsActive") is not True

    @mock_aws
    def test_the_stack_name_is_exported_for_downstream_bda_lookups(
        self, aws_credentials, config_env, tmp_path
    ):
        """`STACK_NAME` is how BDA project naming finds the deployment."""
        import os

        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(tmp_path / "c.yaml", notes="n"),
            config_profile="p",
            validate=False,
        )

        operation.activate(config_profile="p")

        assert os.environ["STACK_NAME"] == STACK

    @mock_aws
    def test_a_manager_failure_is_returned_rather_than_raised(
        self, aws_credentials, config_env, tmp_path, monkeypatch
    ):
        from idp_common.config.configuration_manager import ConfigurationManager

        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(tmp_path / "c.yaml", notes="n"),
            config_profile="p",
            validate=False,
        )
        monkeypatch.setattr(
            ConfigurationManager,
            "activate_version",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("conditional check")),
        )

        result = operation.activate(config_profile="p")

        assert result.success is False
        assert result.error is not None and "conditional check" in result.error

    @mock_aws
    def test_a_resource_not_found_propagates_instead_of_becoming_a_result(
        self, aws_credentials, config_env, monkeypatch
    ):
        """Two failure kinds, deliberately handled differently.

        `IDPResourceNotFoundError` means the caller named something that is not
        there, which is a programming or configuration error and is raised.
        Anything else is an operational failure and is reported in the result, so
        a caller iterating over profiles is not stopped by one bad stack.
        """
        from idp_common.config.configuration_manager import ConfigurationManager

        _create_stack(aws_credentials)
        monkeypatch.setattr(
            ConfigurationManager,
            "get_configuration",
            lambda *a, **k: (_ for _ in ()).throw(
                IDPResourceNotFoundError("profile store is gone")
            ),
        )

        with pytest.raises(IDPResourceNotFoundError, match="profile store is gone"):
            _client(aws_credentials).config.activate(config_profile="p")

    @mock_aws
    def test_a_processing_error_also_propagates(
        self, aws_credentials, config_env, tmp_path, monkeypatch
    ):
        """The second of the two exception types `activate` re-raises.

        `IDPProcessingError` is what the config layer raises when the operation
        cannot be carried out as asked — the revision store being unavailable is
        the live example. Folding it into `success=False` would lose the
        distinction between "the activation was attempted and failed" and "the
        activation could not be attempted", which a retry loop needs.
        """
        from idp_common.config.configuration_manager import ConfigurationManager
        from idp_sdk.exceptions import IDPProcessingError

        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(tmp_path / "c.yaml", notes="n"),
            config_profile="p",
            validate=False,
        )
        monkeypatch.setattr(
            ConfigurationManager,
            "activate_version",
            lambda *a, **k: (_ for _ in ()).throw(
                IDPProcessingError("revision store unavailable")
            ),
        )

        with pytest.raises(IDPProcessingError, match="revision store unavailable"):
            operation.activate(config_profile="p")


# --------------------------------------------------------------------------
# activate() with BDA blueprint sync
# --------------------------------------------------------------------------


def _bda_service(
    sync_result, arn="arn:aws:bedrock:us-east-1:1:data-automation-project/p"
):
    """A stand-in for `BdaBlueprintService`.

    Patched rather than faked because the real one calls Bedrock Data Automation,
    which `moto` does not implement. What the tests assert is the state the
    operation leaves in the real DynamoDB table afterwards, plus the arguments
    the sync was asked for — not that a method was reached.
    """
    service = MagicMock()
    service.get_or_create_project_for_version.return_value = arn
    service.create_blueprints_from_custom_configuration.return_value = sync_result
    return service


def _real_sync_entries(*class_ids: str) -> list[dict]:
    """The per-class status entries the **real** sync emits, for `class_ids`.

    `sync_bda` reads the classes it processed out of these entries, so their keys
    are the contract under test — and a hand-written fixture can only show that
    this file and `operations/config.py` agree with each other. That is the state
    the fixtures here were in: they supplied `class_name`, a key no producer in
    the tree writes, which kept a read that could never succeed looking correct.

    So the entries are read off the producer instead, by driving the real
    `BdaBlueprintService.create_blueprints_from_custom_configuration` over its two
    AWS-touching collaborators doubled — `moto` implements no Bedrock Data
    Automation, which is why this is doubled rather than faked. `idp_to_bda` with
    an empty project is the shortest path through it that produces a `success`
    entry per class, and `max_workers` is pinned to 1 so the entries come back in
    the order the classes were given rather than in thread-completion order.
    """
    from idp_common.bda.bda_blueprint_service import BdaBlueprintService

    module = "idp_common.bda.bda_blueprint_service"
    with (
        patch(f"{module}.BDABlueprintCreator"),
        patch(f"{module}.ConfigurationManager"),
        patch.dict(
            os.environ,
            {"CONFIGURATION_TABLE_NAME": "config-table", "STACK_NAME": "idp"},
        ),
    ):
        service = BdaBlueprintService(
            dataAutomationProjectArn="arn:aws:bedrock:us-east-1:1:data-automation-project/p",
            region="us-east-1",
        )
    service.max_workers = 1

    creator = MagicMock()
    creator.list_blueprints.return_value = {"blueprints": []}
    creator.create_blueprint.side_effect = (
        lambda document_type, blueprint_name, schema: {
            "status": "success",
            "blueprint": {
                "blueprintArn": f"arn:aws:bedrock:::blueprint/{blueprint_name}",
                "blueprintName": blueprint_name,
            },
        }
    )
    creator.create_blueprint_version_without_project_update.return_value = {
        "blueprint": {"blueprintVersion": "1"}
    }
    service.blueprint_creator = creator

    config = MagicMock()
    config.classes = [
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": class_id,
            "x-aws-idp-document-type": class_id,
            "description": f"A {class_id}",
            "type": "object",
            "properties": {"total": {"type": "string", "description": "Total"}},
        }
        for class_id in class_ids
    ]
    service.config_manager = MagicMock()
    service.config_manager.get_configuration.return_value = config

    return service.create_blueprints_from_custom_configuration(
        version="derived", sync_direction="idp_to_bda", sync_mode="merge"
    )


@pytest.mark.unit
@pytest.mark.config
class TestActivateSyncsBdaBlueprints:
    @mock_aws
    def test_a_bda_config_syncs_blueprints_and_records_the_project(
        self, aws_credentials, config_env, tmp_path
    ):
        """Full sync: status `synced` and the project ARN persisted on the profile.

        Both attributes are read back out of DynamoDB. They are what the Web UI
        shows as the profile's BDA state and what the next activation reuses
        instead of creating a second BDA project.
        """
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(tmp_path / "c.yaml", classes=[], use_bda=True),
            config_profile="bda",
            validate=False,
        )
        service = _bda_service([{"status": "success", "class": "Invoice"}])

        with patch(
            "idp_common.bda.bda_blueprint_service.BdaBlueprintService",
            return_value=service,
        ) as constructor:
            result = operation.activate(config_profile="bda")

        assert result.success is True
        assert (result.bda_synced, result.bda_classes_synced) == (True, 1)
        assert result.bda_classes_failed == 0
        assert constructor.call_args.kwargs["region"] == aws_credentials
        service.create_blueprints_from_custom_configuration.assert_called_once_with(
            sync_direction="idp_to_bda", version="bda", sync_mode="replace"
        )

        item = _item(aws_credentials, "Config#bda")
        assert item["IsActive"] is True
        assert item["BdaSyncStatus"] == "synced"
        assert item["BdaProjectArn"].endswith("/p")

    @mock_aws
    def test_a_partial_sync_activates_anyway_and_says_so(
        self, aws_credentials, config_env, tmp_path
    ):
        """Some classes synced: activation proceeds, status recorded as `partial`.

        Refusing here would block a deployment over one malformed class; hiding
        it would leave the operator believing every class has a blueprint. The
        `partial` marker in DynamoDB is the durable record of the difference.
        """
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(tmp_path / "c.yaml", classes=[], use_bda=True),
            config_profile="bda",
            validate=False,
        )
        service = _bda_service(
            [
                {"status": "success", "class": "Invoice"},
                {"status": "error", "class": "Weird"},
            ]
        )

        with patch(
            "idp_common.bda.bda_blueprint_service.BdaBlueprintService",
            return_value=service,
        ):
            result = operation.activate(config_profile="bda")

        assert result.success is True
        assert (result.bda_classes_synced, result.bda_classes_failed) == (1, 1)
        item = _item(aws_credentials, "Config#bda")
        assert item["BdaSyncStatus"] == "partial"
        assert item["IsActive"] is True

    @mock_aws
    def test_a_total_sync_failure_aborts_the_activation(
        self, aws_credentials, config_env, tmp_path
    ):
        """Nothing synced: the profile must NOT become active.

        This is the assertion the whole BDA branch exists for. Activating a BDA
        profile whose blueprints do not exist sends every subsequent document to
        a project with nothing to extract with, and the run reports success with
        empty results. `IsActive` staying off, read from the item itself, is the
        only proof that the abort happened.
        """
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(tmp_path / "c.yaml", classes=[], use_bda=True),
            config_profile="bda",
            validate=False,
        )
        service = _bda_service([{"status": "error", "class": "Invoice"}])

        with patch(
            "idp_common.bda.bda_blueprint_service.BdaBlueprintService",
            return_value=service,
        ):
            result = operation.activate(config_profile="bda")

        assert result.success is False
        assert result.bda_synced is False
        assert result.bda_classes_failed == 1
        assert result.error is not None and "activation aborted" in result.error
        item = _item(aws_credentials, "Config#bda")
        assert item.get("IsActive") is not True, "aborted sync still activated"
        assert "BdaSyncStatus" not in item

    @mock_aws
    def test_an_exception_from_the_sync_aborts_the_activation(
        self, aws_credentials, config_env, tmp_path
    ):
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(tmp_path / "c.yaml", classes=[], use_bda=True),
            config_profile="bda",
            validate=False,
        )
        service = _bda_service([])
        service.create_blueprints_from_custom_configuration.side_effect = RuntimeError(
            "AccessDeniedException"
        )

        with patch(
            "idp_common.bda.bda_blueprint_service.BdaBlueprintService",
            return_value=service,
        ):
            result = operation.activate(config_profile="bda")

        assert result.success is False
        assert result.error is not None and "BDA sync error" in result.error
        assert _item(aws_credentials, "Config#bda").get("IsActive") is not True

    @mock_aws
    def test_an_existing_project_arn_is_reused_rather_than_recreated(
        self, aws_credentials, config_env, tmp_path
    ):
        """A profile that already has a BDA project keeps it.

        Creating a second project per activation would leak a BDA project on
        every deploy and detach the blueprints the previous one owned.
        """
        from idp_common.config.configuration_manager import ConfigurationManager

        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(tmp_path / "c.yaml", classes=[], use_bda=True),
            config_profile="bda",
            validate=False,
        )
        existing = "arn:aws:bedrock:us-east-1:1:data-automation-project/already-there"
        ConfigurationManager(region=aws_credentials).set_bda_project_arn(
            "bda", existing, "synced"
        )
        service = _bda_service([{"status": "success", "class": "Invoice"}])

        with patch(
            "idp_common.bda.bda_blueprint_service.BdaBlueprintService",
            return_value=service,
        ) as constructor:
            operation.activate(config_profile="bda")

        assert constructor.call_args.kwargs["dataAutomationProjectArn"] == existing
        service.get_or_create_project_for_version.assert_not_called()
        assert _item(aws_credentials, "Config#bda")["BdaProjectArn"] == existing

    @mock_aws
    def test_a_non_bda_config_does_not_reach_the_blueprint_service(
        self, aws_credentials, config_env, tmp_path
    ):
        """`use_bda: false` must not construct the service at all.

        Constructing it would make every activation on a pipeline-mode stack
        depend on Bedrock Data Automation being reachable and permitted.
        """
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(tmp_path / "c.yaml", classes=[], use_bda=False),
            config_profile="plain",
            validate=False,
        )

        with patch(
            "idp_common.bda.bda_blueprint_service.BdaBlueprintService"
        ) as constructor:
            result = operation.activate(config_profile="plain")

        constructor.assert_not_called()
        assert result.success is True
        assert result.bda_synced is False


# --------------------------------------------------------------------------
# delete()
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.config
class TestDelete:
    @pytest.mark.parametrize("spelling", ["config_version", "config_profile"])
    @mock_aws
    def test_deleting_a_profile_removes_its_item(
        self, aws_credentials, config_env, tmp_path, spelling
    ):
        """Both argument spellings reach the same delete.

        `config_profile` is the current name and `config_version` the former one;
        the parametrisation is here because the alias is resolved in a helper and
        a regression would only show up on one of the two.
        """
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(tmp_path / "c.yaml", notes="n"),
            config_profile="doomed",
            validate=False,
        )
        assert _item(aws_credentials, "Config#doomed") is not None

        result = operation.delete(**{spelling: "doomed"})

        assert result.success is True
        assert result.deleted_version == "doomed"
        assert _item(aws_credentials, "Config#doomed") is None

    @mock_aws
    def test_the_active_profile_cannot_be_deleted(
        self, aws_credentials, config_env, tmp_path
    ):
        """Refused by the manager, reported as a result, and the item survives.

        Deleting the running configuration would leave the pipeline pointing at
        nothing, so the item still being there after the call is the assertion
        that matters.
        """
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(tmp_path / "c.yaml", notes="n"),
            config_profile="live",
            validate=False,
        )
        operation.activate(config_profile="live")

        result = operation.delete(config_profile="live")

        assert result.success is False
        assert result.error is not None and "active" in result.error.lower()
        assert _item(aws_credentials, "Config#live") is not None

    @mock_aws
    def test_a_resource_not_found_propagates(
        self, aws_credentials, config_env, monkeypatch
    ):
        """Same split as `activate`: not-found raises, everything else reports."""
        from idp_common.config.configuration_manager import ConfigurationManager

        _create_stack(aws_credentials)
        monkeypatch.setattr(
            ConfigurationManager,
            "delete_configuration",
            lambda *a, **k: (_ for _ in ()).throw(
                IDPResourceNotFoundError("no such profile")
            ),
        )

        with pytest.raises(IDPResourceNotFoundError, match="no such profile"):
            _client(aws_credentials).config.delete(config_profile="ghost")


# --------------------------------------------------------------------------
# revisions()
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.config
class TestRevisions:
    @mock_aws
    def test_an_unreadable_index_raises_rather_than_reporting_no_history(
        self, aws_credentials, config_env, tmp_path
    ):
        """A failed read is not an empty history.

        The module already distinguishes "history disabled" (no Configuration
        bucket) from "no revisions"; this is the third case — the store is
        enabled and the read failed — and it has to be distinguishable too.
        """
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(tmp_path / "c.yaml", notes="n"),
            config_profile="p",
            validate=False,
        )
        boto3.client("dynamodb", region_name=aws_credentials).delete_table(
            TableName=_table(aws_credentials).name
        )

        with pytest.raises(IDPResourceNotFoundError, match="Failed to list revisions"):
            operation.revisions(config_profile="p")


# --------------------------------------------------------------------------
# sync_bda()
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.config
class TestSyncBda:
    def test_the_sync_names_each_class_under_the_key_the_result_reads(self):
        """The key `processed_classes` is built from, read off the producer.

        This is the assertion the rest of this class's fixtures rest on. The two
        keys `sync_bda` used to look for, `class_name` and `name`, are asserted
        absent as well as `class` present: while both were merely *missing*, the
        read fell through to a literal for every class, the counts beside it
        stayed right, and the field reported a list of placeholders that looked
        like a list of answers.
        """
        entries = _real_sync_entries("Invoice", "W2")

        assert [entry["class"] for entry in entries] == ["Invoice", "W2"]
        assert all(
            "class_name" not in entry and "name" not in entry for entry in entries
        )

    @mock_aws
    def test_a_full_sync_reports_every_class_and_records_synced(
        self, aws_credentials, config_env, tmp_path
    ):
        """The classes are reported by name, not just counted.

        Asserting `len(processed_classes) == 2` would pass against a read that
        named neither of them, which is how this went unnoticed; the names are
        what the CLI prints under "Classes synced" and the only part of the result
        that says *which* classes reached BDA.
        """
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(tmp_path / "c.yaml", classes=[], use_bda=True),
            config_profile="bda",
            validate=False,
        )
        service = _bda_service(_real_sync_entries("Invoice", "W2"))

        with patch(
            "idp_common.bda.bda_blueprint_service.BdaBlueprintService",
            return_value=service,
        ):
            result = operation.sync_bda(config_profile="bda")

        assert isinstance(result, ConfigSyncBdaResult)
        assert result.success is True
        assert (result.direction, result.mode) == ("bidirectional", "replace")
        assert (result.classes_synced, result.classes_failed) == (2, 0)
        assert result.processed_classes == ["Invoice", "W2"]
        assert result.error is None
        assert _item(aws_credentials, "Config#bda")["BdaSyncStatus"] == "synced"

    @mock_aws
    def test_the_direction_and_mode_are_passed_through_verbatim(
        self, aws_credentials, config_env, tmp_path
    ):
        """`merge` versus `replace` decides whether blueprints are deleted.

        Swapping them would delete blueprints a caller asked to keep, so the
        values have to reach the service unchanged rather than being
        re-derived.
        """
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(tmp_path / "c.yaml", classes=[], use_bda=True),
            config_profile="bda",
            validate=False,
        )
        service = _bda_service([{"status": "success", "class": "Invoice"}])

        with patch(
            "idp_common.bda.bda_blueprint_service.BdaBlueprintService",
            return_value=service,
        ):
            result = operation.sync_bda(
                direction="bda_to_idp", mode="merge", config_profile="bda"
            )

        service.create_blueprints_from_custom_configuration.assert_called_once_with(
            sync_direction="bda_to_idp", version="bda", sync_mode="merge"
        )
        assert (result.direction, result.mode) == ("bda_to_idp", "merge")

    @mock_aws
    def test_no_profile_named_syncs_the_active_one(
        self, aws_credentials, config_env, tmp_path
    ):
        """`sync_bda()` with no profile means the one the stack is running."""
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        config = tmp_path / "c.yaml"
        for name in ("idle", "live"):
            operation.upload(
                config_file=_write_config(config, classes=[], use_bda=True, notes=name),
                config_profile=name,
                validate=False,
            )
        service = _bda_service([{"status": "success", "class": "Invoice"}])

        # The activation is inside the patch too: these are BDA configs, so
        # activate() runs its own blueprint sync and would otherwise reach the
        # real service, abort, and leave no profile active at all — which is how
        # this test first failed, with sync_bda() then resolving `version=None`.
        with patch(
            "idp_common.bda.bda_blueprint_service.BdaBlueprintService",
            return_value=service,
        ):
            operation.activate(config_profile="live")
            operation.sync_bda()

        assert (
            service.create_blueprints_from_custom_configuration.call_args.kwargs[
                "version"
            ]
            == "live"
        )

    @mock_aws
    def test_a_partial_sync_is_not_a_success(
        self, aws_credentials, config_env, tmp_path
    ):
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(tmp_path / "c.yaml", classes=[], use_bda=True),
            config_profile="bda",
            validate=False,
        )
        service = _bda_service(
            [
                {"status": "success", "class": "Invoice"},
                {"status": "error", "class": "Broken"},
            ]
        )

        with patch(
            "idp_common.bda.bda_blueprint_service.BdaBlueprintService",
            return_value=service,
        ):
            result = operation.sync_bda(config_profile="bda")

        assert result.success is False
        assert result.error == "1 class(es) failed to sync"
        assert _item(aws_credentials, "Config#bda")["BdaSyncStatus"] == "partial"

    @mock_aws
    def test_a_total_failure_leaves_no_sync_status_behind(
        self, aws_credentials, config_env, tmp_path
    ):
        """Nothing synced: neither `synced` nor `partial` is recorded.

        Writing `partial` for a run in which zero classes succeeded would
        overstate the state of the BDA project on the profile a UI reads.
        """
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(tmp_path / "c.yaml", classes=[], use_bda=True),
            config_profile="bda",
            validate=False,
        )
        service = _bda_service([{"status": "error", "class": "Invoice"}])

        with patch(
            "idp_common.bda.bda_blueprint_service.BdaBlueprintService",
            return_value=service,
        ):
            result = operation.sync_bda(config_profile="bda")

        assert result.success is False
        assert (result.classes_synced, result.classes_failed) == (0, 1)
        assert "BdaSyncStatus" not in _item(aws_credentials, "Config#bda")

    @mock_aws
    def test_an_entry_that_names_no_class_fails_the_sync_rather_than_inventing_one(
        self, aws_credentials, config_env, tmp_path
    ):
        """An unreadable entry is reported as such, not filled in.

        Every entry the sync emits carries `class`, so the only way to reach this
        is a rename inside the producer — and the answer to that has to be
        distinguishable from a real class name. A placeholder is not: it is a
        plausible string in a list of plausible strings, which is why the field
        spent its whole life reporting one. The sync's own error path carries the
        failure out, naming the key, and `BdaSyncStatus` is deliberately left
        unwritten: a result nobody could build is not evidence the profile synced.
        """
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(tmp_path / "c.yaml", classes=[], use_bda=True),
            config_profile="bda",
            validate=False,
        )
        renamed = _real_sync_entries("Invoice")
        renamed[0]["klass"] = renamed[0].pop("class")
        service = _bda_service(renamed)

        with patch(
            "idp_common.bda.bda_blueprint_service.BdaBlueprintService",
            return_value=service,
        ):
            result = operation.sync_bda(config_profile="bda")

        assert result.success is False
        assert result.processed_classes == []
        assert result.error is not None
        assert "'class'" in result.error
        assert "BdaSyncStatus" not in _item(aws_credentials, "Config#bda")

    @mock_aws
    def test_a_sync_exception_is_returned_with_the_direction_preserved(
        self, aws_credentials, config_env, tmp_path
    ):
        """A failed sync still reports what was attempted.

        `direction` and `mode` in the failure result are how a caller retrying or
        logging knows which sync failed; losing them on the error path would make
        the result unactionable.
        """
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(tmp_path / "c.yaml", classes=[], use_bda=True),
            config_profile="bda",
            validate=False,
        )
        service = _bda_service([])
        service.create_blueprints_from_custom_configuration.side_effect = RuntimeError(
            "ValidationException: blueprint limit"
        )

        with patch(
            "idp_common.bda.bda_blueprint_service.BdaBlueprintService",
            return_value=service,
        ):
            result = operation.sync_bda(direction="idp_to_bda", config_profile="bda")

        assert result.success is False
        assert result.direction == "idp_to_bda"
        assert result.error is not None and "blueprint limit" in result.error
        assert result.classes_synced == 0


# --------------------------------------------------------------------------
# Defects pinned at their current behaviour
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.config
class TestConfigDefectsPinnedAtCurrentBehaviour:
    @mock_aws
    def test_download_leaks_the_dynamodb_partition_key_into_the_config(
        self, aws_credentials, config_env, tmp_path
    ):
        """DEFECT — `operations/config.py:337-348`.

        The revision branch immediately above strips `_config_format` and
        `config_type`, with a comment saying why: "neither is part of the
        configuration a caller edits or re-uploads". The head branch strips
        nothing, so the same profile read two ways differs by `config_type` *and*
        by `Configuration` — the DynamoDB partition key, which is not a
        configuration field under any reading and is written into the downloaded
        YAML.

        Two observable consequences, both asserted here. A caller diffing "what is
        live" against "what an earlier run used" sees changes in two keys nobody
        edited. And re-uploading the file the tool just produced — the documented
        edit-and-upload loop — reports `Unknown field 'Configuration'`, a warning
        about a key the user never wrote.
        """
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(tmp_path / "c.yaml", classes=[], notes="n"),
            config_profile="p",
            validate=False,
        )

        head = operation.download(config_profile="p")
        revision = operation.download(config_profile="p", config_revision=1)

        assert "Configuration" in head.config, "storage key leaks into the head form"
        assert "Configuration" not in revision.config, "the revision form strips it"
        assert set(head.config) - set(revision.config) == {
            "Configuration",
            "config_type",
        }

        output = tmp_path / "round-trip.yaml"
        operation.download(config_profile="p", output=str(output))
        assert "Configuration" in operation.validate(str(output)).unknown_fields, (
            "the tool's own output fails its own validation as an unknown field"
        )

    @mock_aws
    def test_minimal_download_of_a_bda_config_raises(
        self, aws_credentials, config_env, tmp_path
    ):
        """DEFECT — reached from `operations/config.py:356-369`.

        When no `pattern=` is given, `download(format="minimal")` sniffs
        `classification.classificationMethod` and asks for `pattern-1`'s system
        defaults for a BDA config. `pattern-1.yaml` still lists
        `base-assessment.yaml` in its `_inherits`, and that file was deleted in
        commit `e3d23471` (the v0.6 change that folded confidence and geometry
        into extraction), so `load_system_defaults("pattern-1")` raises
        `FileNotFoundError` for everyone.

        The fix belongs in `idp_common`'s system defaults, not here. The effect at
        this boundary is that `config-download --format minimal` is unusable on
        exactly the deployments it auto-detects — BDA ones — and fails with a
        message about a missing YAML file that names no profile and no stack. The
        workaround is an explicit `pattern=`, asserted in
        `test_an_explicit_pattern_overrides_the_auto_detection` above.
        """
        _create_stack(aws_credentials)
        operation = _client(aws_credentials).config
        operation.upload(
            config_file=_write_config(
                tmp_path / "c.yaml",
                classes=[],
                use_bda=True,
                classification={"classificationMethod": "bda"},
            ),
            config_profile="bda",
            validate=False,
        )

        with pytest.raises(FileNotFoundError, match="base-assessment.yaml"):
            operation.download(config_profile="bda", format="minimal")
