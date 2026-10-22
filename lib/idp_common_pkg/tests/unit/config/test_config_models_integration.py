# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Integration tests for configuration Pydantic models.

These tests validate that all existing sample and pattern configurations
can be successfully parsed by the Pydantic models.
"""

from pathlib import Path

import pytest
import yaml

from idp_common.config.merge_utils import merge_config_with_defaults
from idp_common.config.models import IDPConfig


class TestConfigModelsIntegration:
    """Integration tests for configuration models with real config files"""

    @pytest.fixture
    def config_root(self):
        """Get the root directory for config files"""
        # From lib/idp_common_pkg/tests/unit/config -> repo root
        test_dir = Path(__file__).parent
        repo_root = test_dir.parent.parent.parent.parent.parent
        config_library = repo_root / "config_library"

        if not config_library.exists():
            pytest.skip(f"Config library not found at {config_library}")

        return config_library

    @pytest.fixture
    def all_config_files(self, config_root):
        """Find all config.yaml files in the config_library"""
        config_files = list(config_root.rglob("config.yaml"))

        if not config_files:
            pytest.skip("No config.yaml files found in config_library")

        return config_files

    def test_all_pattern_configs_parse(self, all_config_files):
        """
        Test that all pattern and sample configurations can be parsed.

        This is the main integration test that ensures backward compatibility
        with all existing configurations.
        """
        results = []
        errors = []

        for config_file in all_config_files:
            relative_path = config_file.relative_to(config_file.parents[4])

            # Skip criteria-validation config as it has custom fields not in IDPConfig
            if "criteria-validation" in str(relative_path):
                continue

            try:
                # Load YAML config
                with open(config_file, "r") as f:
                    config_dict = yaml.safe_load(f)

                # Validate with Pydantic model
                config = IDPConfig.model_validate(config_dict)

                # Basic validation - classes should always be a list
                if not isinstance(config.classes, list):
                    raise ValueError(f"Classes should be a list in {relative_path}")

                # Pattern 2 and 3 should have these fields (except special configs like criteria-validation)
                is_standard_pattern = (
                    "pattern-2" in str(relative_path)
                ) and "criteria-validation" not in str(relative_path)

                if is_standard_pattern:
                    if config.ocr is None:
                        raise ValueError(f"OCR config missing in {relative_path}")
                    if config.classification is None:
                        raise ValueError(
                            f"Classification config missing in {relative_path}"
                        )
                    if config.extraction is None:
                        raise ValueError(
                            f"Extraction config missing in {relative_path}"
                        )

                results.append(
                    {
                        "file": str(relative_path),
                        "status": "PASS",
                        "num_classes": len(config.classes),
                    }
                )

            except Exception as e:
                errors.append({"file": str(relative_path), "error": str(e)})

        # Print summary
        print("\n" + "=" * 80)
        print("CONFIG PARSING RESULTS")
        print("=" * 80)

        for result in results:
            print(f"✓ {result['file']}")
            print(f"  Classes: {result['num_classes']}")

        if errors:
            print("\nERRORS:")
            for error in errors:
                print(f"✗ {error['file']}")
                print(f"  Error: {error['error']}")

        print(f"\nTotal: {len(results)} passed, {len(errors)} failed")
        print("=" * 80)

        # Fail if any errors
        assert len(errors) == 0, f"Failed to parse {len(errors)} config file(s)"

    def test_pattern1_lending_config(self, config_root):
        """Test Pattern 1 lending package configuration"""
        config_file = (
            config_root / "pattern-1" / "lending-package-sample" / "config.yaml"
        )

        if not config_file.exists():
            pytest.skip(f"Config file not found: {config_file}")

        with open(config_file, "r") as f:
            config_dict = yaml.safe_load(f)

        config = IDPConfig.model_validate(config_dict)

        # Pattern 1 has a different structure - it may not have all standard fields
        # Just validate it parses successfully
        assert config is not None
        assert isinstance(config.classes, list)

    def test_pattern2_bank_statement_config(self, config_root):
        """Test Pattern 2 bank statement configuration"""
        config_file = (
            config_root / "pattern-2" / "bank-statement-sample" / "config.yaml"
        )

        if not config_file.exists():
            pytest.skip(f"Config file not found: {config_file}")

        with open(config_file, "r") as f:
            config_dict = yaml.safe_load(f)

        config = IDPConfig.model_validate(config_dict)

        # Validate numeric conversions work
        assert isinstance(config.extraction.temperature, float)
        assert isinstance(config.extraction.top_p, float)
        assert isinstance(config.extraction.top_k, float)
        # max_tokens is no longer an extraction field (output = model maximum)
        assert not hasattr(config.extraction, "max_tokens")

        # Validate ranges
        assert 0.0 <= config.extraction.temperature <= 1.0
        assert 0.0 <= config.extraction.top_p <= 1.0

    def test_pattern2_with_few_shot_examples(self, config_root):
        """Test Pattern 2 with few-shot examples configuration"""
        config_file = (
            config_root
            / "pattern-2"
            / "rvl-cdip-with-few-shot-examples"
            / "config.yaml"
        )

        if not config_file.exists():
            pytest.skip(f"Config file not found: {config_file}")

        with open(config_file, "r") as f:
            config_dict = yaml.safe_load(f)

        config = IDPConfig.model_validate(config_dict)

        # Validate that classes with examples work
        assert len(config.classes) > 0

    def test_rule_validation_config(self, config_root):
        """Test rule validation configuration"""
        config_file = config_root / "pattern-2" / "criteria-validation" / "config.yaml"

        if not config_file.exists():
            pytest.skip(f"Config file not found: {config_file}")

        with open(config_file, "r") as f:
            config_dict = yaml.safe_load(f)

        config = IDPConfig.model_validate(config_dict)

        # Validate that confidence config works (v0.6: confidence is under extraction)
        assert config.extraction.confidence is not None
        assert isinstance(config.extraction.confidence.enabled, bool)

        # Validate list batching setting (v0.6: extraction.confidence.list_batch_size;
        # granular assessment has been retired).
        assert config.extraction.confidence.list_batch_size > 0
        assert not hasattr(config.extraction.confidence, "granular")

    def test_config_with_all_optional_fields(self, config_root):
        """Test that configs work even if optional fields are missing"""
        # Create a minimal config
        minimal_config = {
            "ocr": {"backend": "textract", "features": []},
            "classification": {"model": "us.amazon.nova-pro-v1:0"},
            "extraction": {"model": "us.amazon.nova-pro-v1:0"},
            "assessment": {"model": "us.amazon.nova-lite-v1:0"},
            "classes": [],
        }

        config = IDPConfig.model_validate(minimal_config)

        # Check defaults are applied
        assert config.extraction.temperature == 0.0
        assert not hasattr(config.extraction, "max_tokens")
        assert config.extraction.agentic.enabled is False
        # v0.6: assessment.enabled migrated to extraction.confidence.enabled
        assert config.extraction.confidence.enabled is True

    def test_config_type_coercion(self):
        """Test that type coercion works for all numeric fields"""
        config_dict = {
            "ocr": {"backend": "textract"},
            "classification": {
                "model": "test",
                "temperature": "0.5",  # String
                "top_p": 0.1,  # Float
                "top_k": "5",  # String
                "max_tokens": "1000",  # String
            },
            "extraction": {
                "model": "test",
                "temperature": 0.7,  # Float
                "top_p": "0.2",  # String
                "top_k": 3.0,  # Float
                "max_tokens": 2000,  # Int
            },
            "assessment": {
                "model": "test",
                "inshard_list_batch_size": "5",  # String (v0.5 key)
            },
            "classes": [],
        }

        config = IDPConfig.model_validate(config_dict)

        # All should be properly typed
        # The flat classification keys migrate into every call's block, and are
        # coerced there.
        for call in (
            config.classification.one_stage.llm,
            config.classification.two_stage.segmentation.llm,
            config.classification.two_stage.section_classification.llm,
        ):
            assert call.temperature == 0.5
            assert isinstance(call.temperature, float)
            assert call.max_tokens == 1000
            assert isinstance(call.max_tokens, int)

        assert config.extraction.top_p == 0.2
        assert isinstance(config.extraction.top_p, float)

        # v0.6: assessment.inshard_list_batch_size migrates to
        # extraction.confidence.list_batch_size
        assert config.extraction.confidence.list_batch_size == 5
        assert isinstance(config.extraction.confidence.list_batch_size, int)

    def test_boolean_variations(self):
        """Test various boolean representations"""
        test_cases = [
            (True, True),
            (False, False),
            ("true", True),
            ("True", True),
            ("TRUE", True),
            ("false", False),
            ("False", False),
            ("FALSE", False),
            (1, True),
            (0, False),
        ]

        for input_val, expected in test_cases:
            config_dict = {
                "ocr": {"backend": "textract"},
                "classification": {"model": "test"},
                "extraction": {"model": "test", "agentic": {"enabled": input_val}},
                "assessment": {"model": "test"},
                "classes": [],
            }

            config = IDPConfig.model_validate(config_dict)
            assert config.extraction.agentic.enabled == expected, (
                f"Input {input_val} should yield {expected}"
            )

    def test_config_serialization_roundtrip(self, config_root):
        """Test that configs can be serialized and deserialized"""
        config_file = config_root / "unified" / "lending-package-sample" / "config.yaml"
        # A shipped preset: a missing file is a broken test, not a reason to skip.
        assert config_file.exists(), f"Config file not found: {config_file}"

        # Load original. The preset is a sparse overlay, so it is completed with
        # the system defaults the way the stack completes it on deploy.
        with open(config_file, "r") as f:
            original_dict = merge_config_with_defaults(
                yaml.safe_load(f), validate=False
            )

        # Parse to model
        config = IDPConfig.model_validate(original_dict)

        # Serialize back to dict
        serialized = config.model_dump()

        # Parse again
        config2 = IDPConfig.model_validate(serialized)

        # Should be equivalent
        assert config.extraction.model == config2.extraction.model
        assert config.extraction.agentic.enabled == config2.extraction.agentic.enabled
        assert (
            config.classification.one_stage.llm.temperature
            == config2.classification.one_stage.llm.temperature
        )
        assert config.classification.one_stage.llm.temperature is not None
