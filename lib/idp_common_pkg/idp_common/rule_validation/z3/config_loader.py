# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Configuration loader for Rule Translator.

This module provides functionality to load and validate YAML configuration files
for the Rule Translator component. The configuration includes:
- Bedrock model settings (model_id, temperature, max_tokens)
- System and task prompts
- Few-shot examples for prompt engineering
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import yaml


@dataclass
class TranslatorConfig:
    """
    Configuration for Rule Translator service.

    Attributes:
        model_id: Bedrock model identifier
        temperature: LLM temperature setting (0.0 = deterministic, 1.0 = creative)
        max_tokens: Maximum tokens in LLM response
        system_prompt: System-level instructions for the LLM
        task_prompt_template: Template for task-specific prompts
        few_shot_examples: List of example translations for few-shot learning
    """

    model_id: str
    temperature: float
    max_tokens: int
    system_prompt: str
    task_prompt_template: str
    few_shot_examples: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        """Validate configuration after initialization."""
        self._validate()

    def _validate(self):
        """
        Validate configuration fields.

        Raises:
            ValueError: If validation fails
        """
        # Validate model_id
        if not self.model_id or not isinstance(self.model_id, str):
            raise ValueError(
                f"model_id must be a non-empty string, got: {self.model_id}"
            )

        # Validate temperature
        if not isinstance(self.temperature, (int, float)):
            raise ValueError(
                f"temperature must be a number, got: {type(self.temperature)}"
            )

        if not (0.0 <= self.temperature <= 1.0):
            raise ValueError(
                f"temperature must be between 0.0 and 1.0, got: {self.temperature}"
            )

        # Validate max_tokens
        if not isinstance(self.max_tokens, int):
            raise ValueError(
                f"max_tokens must be an integer, got: {type(self.max_tokens)}"
            )

        if self.max_tokens <= 0:
            raise ValueError(f"max_tokens must be positive, got: {self.max_tokens}")

        # Validate system_prompt
        if not self.system_prompt or not isinstance(self.system_prompt, str):
            raise ValueError("system_prompt must be a non-empty string")

        # Validate task_prompt_template
        if not self.task_prompt_template or not isinstance(
            self.task_prompt_template, str
        ):
            raise ValueError("task_prompt_template must be a non-empty string")

        # Check that template contains required placeholders
        if "{rule}" not in self.task_prompt_template:
            raise ValueError("task_prompt_template must contain '{rule}' placeholder")

        if "{data_example}" not in self.task_prompt_template:
            raise ValueError(
                "task_prompt_template must contain '{data_example}' placeholder"
            )

        # Validate few_shot_examples
        if not isinstance(self.few_shot_examples, list):
            raise ValueError(
                f"few_shot_examples must be a list, got: {type(self.few_shot_examples)}"
            )

        # Validate each example has required fields
        for i, example in enumerate(self.few_shot_examples):
            if not isinstance(example, dict):
                raise ValueError(f"few_shot_examples[{i}] must be a dictionary")

            required_fields = ["rule", "data_example", "output"]
            for field_name in required_fields:
                if field_name not in example:
                    raise ValueError(
                        f"few_shot_examples[{i}] must contain '{field_name}' field"
                    )

            # Validate output structure
            output = example["output"]
            if not isinstance(output, dict):
                raise ValueError(f"few_shot_examples[{i}].output must be a dictionary")

            output_required_fields = ["parameters", "path_mappings", "constraints"]
            for field_name in output_required_fields:
                if field_name not in output:
                    raise ValueError(
                        f"few_shot_examples[{i}].output must contain '{field_name}' field"
                    )


@dataclass
class ValueExtractionConfig:
    """
    Configuration for Value Extraction service.

    Attributes:
        model_id: Bedrock model identifier
        temperature: LLM temperature setting (0.0 = deterministic, 1.0 = creative)
        max_tokens: Maximum tokens in LLM response
        system_prompt: System-level instructions for the LLM
        task_prompt_template: Template for extraction tasks
    """

    model_id: str
    temperature: float
    max_tokens: int
    system_prompt: str
    task_prompt_template: str

    def __post_init__(self):
        """Validate configuration after initialization."""
        self._validate()

    def _validate(self):
        """
        Validate configuration fields.

        Raises:
            ValueError: If validation fails
        """
        # Validate model_id
        if not self.model_id or not isinstance(self.model_id, str):
            raise ValueError(
                f"model_id must be a non-empty string, got: {self.model_id}"
            )

        # Validate temperature
        if not isinstance(self.temperature, (int, float)):
            raise ValueError(
                f"temperature must be a number, got: {type(self.temperature)}"
            )

        if not (0.0 <= self.temperature <= 1.0):
            raise ValueError(
                f"temperature must be between 0.0 and 1.0, got: {self.temperature}"
            )

        # Validate max_tokens
        if not isinstance(self.max_tokens, int):
            raise ValueError(
                f"max_tokens must be an integer, got: {type(self.max_tokens)}"
            )

        if self.max_tokens <= 0:
            raise ValueError(f"max_tokens must be positive, got: {self.max_tokens}")

        # Validate system_prompt
        if not self.system_prompt or not isinstance(self.system_prompt, str):
            raise ValueError("system_prompt must be a non-empty string")

        # Validate task_prompt_template
        if not self.task_prompt_template or not isinstance(
            self.task_prompt_template, str
        ):
            raise ValueError("task_prompt_template must be a non-empty string")

        # Check that extraction template contains required placeholders
        required_placeholders = [
            "{rule_description}",
            "{natural_language_rule}",
            "{parameters_json}",
            "{data_type}",
            "{data}",
        ]
        for placeholder in required_placeholders:
            if placeholder not in self.task_prompt_template:
                raise ValueError(
                    f"task_prompt_template must contain '{placeholder}' placeholder"
                )


@dataclass
class Config:
    """
    Complete configuration containing both services.

    Attributes:
        rule_translator: Configuration for rule translation service
        value_extraction: Configuration for value extraction service
    """

    rule_translator: TranslatorConfig
    value_extraction: ValueExtractionConfig


class ConfigLoader:
    """
    Loads and validates YAML configuration files for Rule Translator.
    """

    @staticmethod
    def load(config_path: str) -> Config:
        """
        Load configuration from YAML file.

        Args:
            config_path: Path to YAML configuration file

        Returns:
            Config instance with both service configurations

        Raises:
            FileNotFoundError: If config file doesn't exist
            ValueError: If config file is invalid or missing required fields
            yaml.YAMLError: If YAML parsing fails
        """
        path = Path(config_path)

        # Check if file exists
        if not path.exists():
            raise FileNotFoundError(
                f"Configuration file not found: {config_path}\n"
                f"Please create a configuration file at this location or use the default: "
                f"config/translator_config.yaml"
            )

        # Check if file is readable
        if not path.is_file():
            raise ValueError(f"Configuration path is not a file: {config_path}")

        # Load YAML
        try:
            with open(path, "r", encoding="utf-8") as f:
                config_data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"Failed to parse YAML configuration: {e}")
        except Exception as e:
            raise ValueError(f"Failed to read configuration file: {e}")

        # Validate that config_data is a dictionary
        if not isinstance(config_data, dict):
            raise ValueError(
                f"Configuration file must contain a YAML dictionary, got: {type(config_data)}"
            )

        # Validate required top-level sections
        required_sections = ["rule_translator", "value_extraction"]
        missing_sections = [
            section for section in required_sections if section not in config_data
        ]
        if missing_sections:
            raise ValueError(
                f"Configuration file missing required sections: {', '.join(missing_sections)}"
            )

        # Load rule_translator config
        translator_data = config_data["rule_translator"]
        if not isinstance(translator_data, dict):
            raise ValueError("'rule_translator' section must be a dictionary")

        required_translator_fields = [
            "model_id",
            "temperature",
            "max_tokens",
            "system_prompt",
            "task_prompt_template",
        ]
        missing_fields = [
            field
            for field in required_translator_fields
            if field not in translator_data
        ]
        if missing_fields:
            raise ValueError(
                f"rule_translator section missing required fields: {', '.join(missing_fields)}"
            )

        try:
            # Allow env override for model_id (supports non-US regions/GovCloud)
            import os

            translator_model = os.environ.get(
                "Z3_TRANSLATOR_MODEL_ID", translator_data["model_id"]
            )
            translator_config = TranslatorConfig(
                model_id=translator_model,
                temperature=translator_data["temperature"],
                max_tokens=translator_data["max_tokens"],
                system_prompt=translator_data["system_prompt"],
                task_prompt_template=translator_data["task_prompt_template"],
                few_shot_examples=translator_data.get("few_shot_examples", []),
            )
        except Exception as e:
            raise ValueError(f"Invalid rule_translator configuration: {e}")

        # Load value_extraction config
        extraction_data = config_data["value_extraction"]
        if not isinstance(extraction_data, dict):
            raise ValueError("'value_extraction' section must be a dictionary")

        required_extraction_fields = [
            "model_id",
            "temperature",
            "max_tokens",
            "system_prompt",
            "task_prompt_template",
        ]
        missing_fields = [
            field
            for field in required_extraction_fields
            if field not in extraction_data
        ]
        if missing_fields:
            raise ValueError(
                f"value_extraction section missing required fields: {', '.join(missing_fields)}"
            )

        try:
            extraction_model = os.environ.get(
                "Z3_EXTRACTION_MODEL_ID", extraction_data["model_id"]
            )
            extraction_config = ValueExtractionConfig(
                model_id=extraction_model,
                temperature=extraction_data["temperature"],
                max_tokens=extraction_data["max_tokens"],
                system_prompt=extraction_data["system_prompt"],
                task_prompt_template=extraction_data["task_prompt_template"],
            )
        except Exception as e:
            raise ValueError(f"Invalid value_extraction configuration: {e}")

        # Create complete config
        return Config(
            rule_translator=translator_config, value_extraction=extraction_config
        )

    @staticmethod
    def get_default_config_path() -> str:
        """
        Get the default configuration file path.

        Returns:
            Absolute path to the default translator_config.yaml
        """
        return str(Path(__file__).parent / "config" / "translator_config.yaml")
