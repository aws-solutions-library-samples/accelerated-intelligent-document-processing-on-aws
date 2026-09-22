# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for config_library YAML/JSON files.

Validates that all configuration files in the config_library are:
1. Valid YAML/JSON that can be parsed without errors
2. Contain expected top-level keys
3. Have properly quoted string values (no YAML parsing ambiguities)

Run with: pytest config_library/test_config_library.py -v
Or via: make test-config-library
"""

import json
import os
import re
from pathlib import Path

import pytest
import yaml

# Find config_library root relative to this test file
CONFIG_LIBRARY_ROOT = Path(__file__).parent


def discover_config_files():
    """Discover all config.yaml and config.json files in the config_library."""
    config_files = []
    for root, _dirs, files in os.walk(CONFIG_LIBRARY_ROOT):
        for filename in files:
            if filename in ("config.yaml", "config.yml", "config.json"):
                filepath = Path(root) / filename
                # Create a readable test ID from the relative path
                rel_path = filepath.relative_to(CONFIG_LIBRARY_ROOT)
                config_files.append(pytest.param(filepath, id=str(rel_path)))
    return config_files


def discover_yaml_files():
    """Discover all YAML files in the config_library (configs + pricing)."""
    yaml_files = []
    for root, _dirs, files in os.walk(CONFIG_LIBRARY_ROOT):
        for filename in files:
            if filename.endswith((".yaml", ".yml")) and not filename.startswith(
                "test_"
            ):
                filepath = Path(root) / filename
                rel_path = filepath.relative_to(CONFIG_LIBRARY_ROOT)
                yaml_files.append(pytest.param(filepath, id=str(rel_path)))
    return yaml_files


class TestConfigLibraryYamlValidity:
    """Test that all YAML files in config_library are valid YAML."""

    @pytest.mark.parametrize("yaml_file", discover_yaml_files())
    def test_yaml_parses_successfully(self, yaml_file: Path):
        """Each YAML file must parse without errors."""
        content = yaml_file.read_text(encoding="utf-8")
        try:
            result = yaml.safe_load(content)
        except yaml.YAMLError as e:
            pytest.fail(
                f"YAML parse error in {yaml_file.relative_to(CONFIG_LIBRARY_ROOT)}: {e}"
            )

        # Verify it parsed to something (not empty)
        assert result is not None, (
            f"YAML file {yaml_file.name} parsed to None (empty file?)"
        )


class TestConfigFilesStructure:
    """Test that config files have expected structure."""

    @pytest.mark.parametrize("config_file", discover_config_files())
    def test_config_parses_to_dict(self, config_file: Path):
        """Each config file must parse to a dictionary."""
        content = config_file.read_text(encoding="utf-8")

        if config_file.suffix == ".json":
            parsed = json.loads(content)
        else:
            parsed = yaml.safe_load(content)

        assert isinstance(parsed, dict), (
            f"Config file {config_file.relative_to(CONFIG_LIBRARY_ROOT)} "
            f"should parse to a dict, got {type(parsed).__name__}"
        )

    @pytest.mark.parametrize("config_file", discover_config_files())
    def test_notes_field_is_string(self, config_file: Path):
        """If a notes field exists, it must be a plain string (not a dict from unquoted YAML)."""
        content = config_file.read_text(encoding="utf-8")

        if config_file.suffix == ".json":
            parsed = json.loads(content)
        else:
            parsed = yaml.safe_load(content)

        if "notes" in parsed:
            assert isinstance(parsed["notes"], str), (
                f"Config file {config_file.relative_to(CONFIG_LIBRARY_ROOT)}: "
                f"'notes' field should be a string, got {type(parsed['notes']).__name__}. "
                f"This usually means the YAML value contains unquoted colons - wrap it in quotes."
            )

    @pytest.mark.parametrize("config_file", discover_config_files())
    def test_classes_field_is_list(self, config_file: Path):
        """If a classes field exists, it must be a list."""
        content = config_file.read_text(encoding="utf-8")

        if config_file.suffix == ".json":
            parsed = json.loads(content)
        else:
            parsed = yaml.safe_load(content)

        if "classes" in parsed:
            assert isinstance(parsed["classes"], list), (
                f"Config file {config_file.relative_to(CONFIG_LIBRARY_ROOT)}: "
                f"'classes' field should be a list, got {type(parsed['classes']).__name__}"
            )

    @pytest.mark.parametrize("config_file", discover_config_files())
    def test_use_bda_field_is_boolean(self, config_file: Path):
        """If a use_bda field exists, it must be a boolean."""
        content = config_file.read_text(encoding="utf-8")

        if config_file.suffix == ".json":
            parsed = json.loads(content)
        else:
            parsed = yaml.safe_load(content)

        if "use_bda" in parsed:
            assert isinstance(parsed["use_bda"], bool), (
                f"Config file {config_file.relative_to(CONFIG_LIBRARY_ROOT)}: "
                f"'use_bda' field should be a boolean, got {type(parsed['use_bda']).__name__} "
                f"with value '{parsed['use_bda']}'"
            )


def _iter_model_values(node, path=""):
    """Yield every (dotted-path, value) for a key named `model` or `model_id`."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if key in ("model", "model_id") and isinstance(value, str):
                yield here, value
            else:
                yield from _iter_model_values(value, here)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _iter_model_values(item, f"{path}[{i}]")


# A Bedrock ARN carrying an account id names a resource that exists only in that
# account: a custom-model-deployment, a provisioned model, an imported model, an
# application inference profile. A *foundation* model ARN has an EMPTY account
# segment (`arn:aws:bedrock:us-east-1::foundation-model/...`) and is therefore fine,
# which is why this keys on the account segment being populated rather than on a list
# of resource types that would go stale as Bedrock adds them.
ACCOUNT_SCOPED_BEDROCK_ARN = re.compile(r"^arn:[^:]*:bedrock:[^:]*:\d{12}:")

# The presets whose whole purpose is to demonstrate a fine-tuned model, and which
# therefore have to express "substitute your own deployment" without shipping one.
# Named so that the check below cannot silently stop covering them.
FINE_TUNED_PRESETS = (
    "unified/docsplit/docsplit_finedtuned_config.yaml",
    "unified/ocr-benchmark/fine_tuned_config.yaml",
    "unified/ocr-benchmark/ocr_fine_tuned_config.yaml",
)


class TestNoAccountScopedModelArns:
    """A shipped preset must not pin a model to someone else's account.

    `config_library/` is published into every deployment's configuration bucket, so a
    `model` value naming a Bedrock resource in one specific account cannot work for
    any other reader: Bedrock answers `AccessDeniedException` ("the provided resource
    ARN is from a different account") and the stage fails outright rather than
    degrading. Three fine-tuned presets shipped that way (#1093).

    Nothing else catches this. `model` is declared as a bare `str` on every config
    model with no validator, the configuration-load path merges with
    `validate=False`, and `idp-cli config-validate` deliberately downgrades an ARN it
    cannot resolve offline to a *warning* ("unverifiable, not wrong") — so all three
    presets validated clean. This check runs in `make test-config-library` and in
    `make test-packages-cicd`, so it fires in both CIs before a preset ships.
    """

    @pytest.mark.parametrize("yaml_file", discover_yaml_files())
    def test_no_model_names_an_account_scoped_bedrock_resource(self, yaml_file: Path):
        parsed = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
        offenders = [
            field
            for field, value in _iter_model_values(parsed)
            if ACCOUNT_SCOPED_BEDROCK_ARN.match(value.strip())
        ]
        assert not offenders, (
            f"{yaml_file.relative_to(CONFIG_LIBRARY_ROOT)} pins "
            f"{len(offenders)} model value(s) to an account-scoped Bedrock ARN: "
            f"{offenders}. Such an ARN resolves only in the account that owns the "
            f"resource, so every other deployment fails at inference. Ship the base "
            f"model and document the substitution in a comment — see the fine-tuned "
            f"presets for the pattern. Redacting the account id is not a fix: the "
            f"ARN still would not resolve."
        )

    def test_the_check_can_actually_see_the_fine_tuned_presets(self):
        """The three presets this check exists for must be in its universe.

        `discover_yaml_files()` is what makes the check above cover more than
        `config.yaml`, and these three are not named `config.yaml` — they are reachable
        only by an explicit `--custom-config` path, which is exactly why they went
        unexamined. If discovery narrows, this fails rather than the check quietly
        passing over nothing.
        """
        discovered = {
            str(param.values[0].relative_to(CONFIG_LIBRARY_ROOT))
            for param in discover_yaml_files()
        }
        missing = [p for p in FINE_TUNED_PRESETS if p not in discovered]
        assert not missing, (
            f"{missing} are no longer discovered, so the account-scoped-ARN check no "
            f"longer covers them. If a preset was renamed or removed, update "
            f"FINE_TUNED_PRESETS; do not leave the check pointing at nothing."
        )

    @pytest.mark.parametrize("preset", FINE_TUNED_PRESETS)
    def test_each_fine_tuned_preset_says_how_to_substitute_its_own_deployment(
        self, preset: str
    ):
        """Shipping the base model silently would lose the preset's whole point.

        The value that makes these runnable is not the one they are demonstrating, so
        a reader has to be told which line to change. Asserted on the text rather than
        the parsed document because the instruction is necessarily a comment.
        """
        text = (CONFIG_LIBRARY_ROOT / preset).read_text(encoding="utf-8")
        assert "custom-model-deployment/<deployment-id>" in text, (
            f"{preset} no longer tells a reader how to point it at their own "
            f"fine-tuned deployment. Without that, the preset looks like an ordinary "
            f"base-model config and its reason for existing is invisible."
        )


class TestPolicyClassRegexCoverage:
    """Every policy class in a preset must be reachable.

    ``PolicyClassificationService`` requires a document-matching regex once a
    config holds more than one policy class, and it evaluates ONLY the classes
    whose regex matches. A class with no regex among several is therefore dead
    weight: its rules never evaluate, silently, with the job still reporting
    success. The ``rule-validation`` preset shipped with 7 classes but a regex
    on only the first, so its own bundled sample evaluated 2 of 14 rules.
    """

    @pytest.mark.parametrize("config_file", discover_config_files())
    def test_every_policy_class_has_a_matching_regex(self, config_file: Path):
        """With >1 policy class, each one needs a name or page-content regex."""
        content = config_file.read_text(encoding="utf-8")
        parsed = (
            json.loads(content)
            if config_file.suffix == ".json"
            else yaml.safe_load(content)
        )

        policy_classes = (parsed or {}).get("policy_classes") or []
        # A single class matches unconditionally, so a regex is optional there.
        if len(policy_classes) < 2:
            return

        missing = [
            pc.get("x-aws-idp-policy-type", f"index {i}")
            for i, pc in enumerate(policy_classes)
            if not pc.get("x-aws-idp-document-name-regex")
            and not pc.get("x-aws-idp-page-content-regex")
        ]
        assert not missing, (
            f"Config file {config_file.relative_to(CONFIG_LIBRARY_ROOT)}: "
            f"{len(missing)} of {len(policy_classes)} policy classes have no "
            f"x-aws-idp-document-name-regex and no x-aws-idp-page-content-regex: "
            f"{missing}. With multiple policy classes only regex-matched classes "
            f"are evaluated, so these classes' rules can never fire."
        )

    def test_rule_validation_preset_matches_its_own_sample(self):
        """The rule-validation preset must evaluate ALL its rules on its sample.

        Guards the specific regression: the preset's regex used `prior_auth` /
        `pa_packet` with underscores only, and sat on just one of 7 classes.
        """
        config_file = (
            CONFIG_LIBRARY_ROOT / "unified" / "rule-validation" / "config.yaml"
        )
        parsed = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        policy_classes = parsed["policy_classes"]

        # Filenames the preset is expected to recognize, including the shipped
        # sample and the hyphenated form real intake systems produce.
        for doc_name in (
            "medicare_respiratory_pa_packet.pdf",
            "Prior-Auth-123789456.pdf",
            "prior auth packet.pdf",
        ):
            matched = [
                pc["x-aws-idp-policy-type"]
                for pc in policy_classes
                if re.search(pc.get("x-aws-idp-document-name-regex", "$^"), doc_name)
            ]
            assert len(matched) == len(policy_classes), (
                f"'{doc_name}' matched only {len(matched)} of "
                f"{len(policy_classes)} policy classes ({matched}); the unmatched "
                f"classes' rules would never be evaluated."
            )

        # An unrelated document must still match nothing — the regexes narrow
        # the preset to policy documents rather than matching everything.
        assert not [
            pc["x-aws-idp-policy-type"]
            for pc in policy_classes
            if re.search(pc.get("x-aws-idp-document-name-regex", "$^"), "invoice.pdf")
        ], "an unrelated document matched a policy class; the regex is too broad"
