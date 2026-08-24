# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Rule Translator component for Z3-based business rule validation system.

This module provides the RuleTranslator class that uses Amazon Bedrock LLM
to translate natural language business rules into structured Rule_JSON containing:
- SMT-LIB constraint logic
- Variable definitions (name, type)
- Variable mappings (variable → data path)

The translator includes:
- Full Bedrock integration with boto3
- Retry logic with exponential backoff
- Complete prompt building with template substitution
- JSON parsing and validation
- Comprehensive error handling
"""

import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any, Dict, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .config_loader import ConfigLoader
from .exceptions import TranslationError
from .models import Parameter, PathMapping, RuleJSON

if TYPE_CHECKING:
    from .models import RuleWithValues

# Configure logging
logger = logging.getLogger(__name__)


class RuleTranslator:
    """
    Translates natural language rules into structured Rule_JSON using LLM.

    The translator:
    1. Loads configuration from YAML file
    2. Builds prompts using templates and few-shot examples
    3. Invokes Amazon Bedrock with retry logic
    4. Parses and validates LLM JSON response
    5. Returns validated RuleJSON object

    Attributes:
        translator_config: TranslatorConfig with model settings and prompts for rule translation
        extraction_config: ValueExtractionConfig with settings for value extraction
        bedrock_client: Boto3 Bedrock Runtime client
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        translator_config=None,
        extraction_config=None,
        region: str = None,
    ):
        """
        Initialize RuleTranslator with configuration.

        Args:
            config_path: Path to YAML configuration file.
                        If None, uses default: "config/translator_config.yaml"
            translator_config: Pre-built TranslatorConfig (overrides config_path)
            extraction_config: Pre-built ValueExtractionConfig (overrides config_path)
            region: AWS region for Bedrock client

        Raises:
            FileNotFoundError: If config file doesn't exist
            ValueError: If config is invalid
        """
        if translator_config and extraction_config:
            # Use pre-built configs (from IDP config)
            self.translator_config = translator_config
            self.extraction_config = extraction_config
        else:
            # Load from YAML file
            if config_path is None:
                config_path = ConfigLoader.get_default_config_path()

            try:
                full_config = ConfigLoader.load(config_path)
                self.translator_config = full_config.rule_translator
                self.extraction_config = full_config.value_extraction
            except Exception as e:
                raise TranslationError(
                    message=f"Failed to load configuration: {e}",
                    operation="load_config",
                    context={"config_path": config_path},
                )

        # Initialize Bedrock client
        try:
            self.bedrock_client = boto3.client(
                service_name="bedrock-runtime",
                region_name=region or os.environ.get("AWS_REGION", "us-east-1"),
            )
        except Exception as e:
            raise TranslationError(
                message=f"Failed to initialize Bedrock client: {e}",
                operation="init_bedrock_client",
                context={"error_type": type(e).__name__},
            )

    def translate_rule(
        self,
        natural_language_rule: str,
        data_example: dict,
        rule_id: Optional[str] = None,
        version: str = "1.0",
        description: Optional[str] = None,
        extract_paths: bool = True,
    ) -> RuleJSON:
        """
        Translate natural language rule to Rule_JSON in single LLM call.

        Supports two workflows:
        - Workflow A (extract_paths=True): Generate path_mappings for structured data
        - Workflow B (extract_paths=False): Only generate parameters, no path_mappings

        The LLM receives the rule and data example, and returns:
        - SMT-LIB constraint logic
        - Variable definitions (name, type)
        - Variable mappings (variable → data path) [only if extract_paths=True]

        Args:
            natural_language_rule: Business rule in natural language
            data_example: Example JSON data document (not just schema)
            rule_id: Optional rule identifier (auto-generated if not provided)
            version: Rule version string (default: "1.0")
            description: Optional rule description (uses rule text if not provided)
            extract_paths: If True, generate path_mappings; if False, only parameters

        Returns:
            RuleJSON object containing constraints, parameters, and optionally mappings

        Raises:
            TranslationError: If LLM fails or returns invalid output
        """
        # Generate rule_id if not provided
        if rule_id is None:
            rule_id = self._generate_rule_id(natural_language_rule)

        # Use rule text as description if not provided
        if description is None:
            description = natural_language_rule[:200]  # Truncate if too long

        # Build prompt
        try:
            prompt = self._build_prompt(
                natural_language_rule, data_example, extract_paths
            )
        except Exception as e:
            raise TranslationError(
                message=f"Failed to build prompt: {e}",
                operation="build_prompt",
                rule_id=rule_id,
                natural_language_rule=natural_language_rule,
            )

        # Invoke Bedrock with retry logic
        try:
            llm_response = self._invoke_bedrock(prompt, rule_id)
        except TranslationError:
            raise  # Re-raise TranslationError as-is
        except Exception as e:
            raise TranslationError(
                message=f"Unexpected error during Bedrock invocation: {e}",
                operation="invoke_bedrock",
                rule_id=rule_id,
                natural_language_rule=natural_language_rule,
            )

        # Parse LLM output
        try:
            parsed_output = self._parse_llm_output(llm_response, rule_id, extract_paths)
        except TranslationError:
            raise  # Re-raise TranslationError as-is
        except Exception as e:
            raise TranslationError(
                message=f"Unexpected error during output parsing: {e}",
                operation="parse_llm_output",
                rule_id=rule_id,
                llm_response=llm_response,
            )

        # Build RuleJSON
        try:
            rule_json = self._build_rule_json(
                parsed_output=parsed_output,
                rule_id=rule_id,
                version=version,
                description=description,
                natural_language_rule=natural_language_rule,
                extract_paths=extract_paths,
            )
        except Exception as e:
            raise TranslationError(
                message=f"Failed to build RuleJSON: {e}",
                operation="build_rule_json",
                rule_id=rule_id,
                context={"parsed_output": parsed_output},
            )

        # Validate RuleJSON
        try:
            self._validate_rule_json(rule_json)
        except Exception as e:
            raise TranslationError(
                message=f"RuleJSON validation failed: {e}",
                operation="validate_rule_json",
                rule_id=rule_id,
                context={"rule_json": rule_json.to_dict()},
            )

        return rule_json

    def extract_values_with_llm(self, rule_json: RuleJSON, data: Any) -> Dict[str, Any]:
        """
        Extract parameter values from data using LLM (works with any format).

        This method uses LLM to extract values for the rule's parameters from
        data in ANY format:
        - Structured JSON (any schema)
        - Unstructured text
        - Mixed formats

        The LLM understands what values are needed based on parameter names,
        types, and the rule description, then extracts them from the data.

        Args:
            rule_json: Rule with parameters that need values
            data: Data in any format (dict, string, list, etc.)

        Returns:
            Dictionary mapping parameter names to extracted values

        Raises:
            TranslationError: If LLM fails or returns invalid values

        Example:
            # Extract from structured JSON
            values = translator.extract_values_with_llm(
                rule_json=rule,
                data={"some": {"nested": {"structure": "value"}}}
            )

            # Extract from unstructured text
            values = translator.extract_values_with_llm(
                rule_json=rule,
                data="The municipal tax is $5,100 and SAP shows $5,200"
            )

            # Use extracted values with validator
            result = validator.validate(rule_json, values)
        """
        # Build prompt for value extraction
        try:
            prompt = self._build_extraction_prompt(rule_json, data)
        except Exception as e:
            raise TranslationError(
                message=f"Failed to build extraction prompt: {e}",
                operation="build_extraction_prompt",
                rule_id=rule_json.rule_id,
            )

        # Invoke Bedrock
        try:
            llm_response = self._invoke_bedrock(
                prompt, rule_json.rule_id, use_extraction_config=True
            )
        except TranslationError:
            raise
        except Exception as e:
            raise TranslationError(
                message=f"Unexpected error during Bedrock invocation: {e}",
                operation="invoke_bedrock_extraction",
                rule_id=rule_json.rule_id,
            )

        # Parse extracted values
        try:
            extracted_values = self._parse_extraction_output(
                llm_response=llm_response, rule_json=rule_json
            )
        except TranslationError:
            raise
        except Exception as e:
            raise TranslationError(
                message=f"Unexpected error during output parsing: {e}",
                operation="parse_extraction_output",
                rule_id=rule_json.rule_id,
                llm_response=llm_response,
            )

        return extracted_values

    def translate_rule_with_data(
        self, rule_json: RuleJSON, data: Any
    ) -> "RuleWithValues":
        """
        Extract values from data and create RuleWithValues (ready for validation).

        This method combines a rule with extracted values, producing a complete
        validation package that can be:
        - Saved as JSON
        - Validated without additional data
        - Reused for multiple validations

        Args:
            rule_json: Rule specification (parameters + constraints)
            data: Data in any format (structured JSON, text, etc.)

        Returns:
            RuleWithValues with extracted values ready for validation

        Raises:
            TranslationError: If LLM extraction fails

        Example:
            # Step 1: Generate rule for original schema
            rule = translator.translate_rule(nl_rule, schema_example)

            # Step 2: Schema changed - extract values with LLM
            rule_with_values = translator.translate_rule_with_data(
                rule_json=rule,
                data=new_schema_data
            )

            # Step 3: Save for later use
            with open("rule_with_values.json", "w") as f:
                json.dump(rule_with_values.to_dict(), f)

            # Step 4: Validate (no data needed)
            result = validator.validate(rule_with_values)
        """
        from .models import RuleWithValues

        # Extract values using LLM
        extracted_values = self.extract_values_with_llm(rule_json, data)

        # Create RuleWithValues
        rule_with_values = RuleWithValues.from_rule_json(rule_json, extracted_values)

        # Add extraction metadata
        rule_with_values.metadata = {
            **rule_with_values.metadata,
            "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "extraction_method": "llm",
            "data_format": type(data).__name__,
        }

        return rule_with_values

    def _generate_rule_id(self, rule_text: str) -> str:
        """
        Generate a rule ID from rule text.

        Args:
            rule_text: Natural language rule text

        Returns:
            Generated rule ID (e.g., "rule_1234567890")
        """
        import hashlib

        # Use hash of rule text + timestamp for uniqueness
        timestamp = str(int(time.time() * 1000))
        hash_input = f"{rule_text}{timestamp}".encode("utf-8")
        hash_digest = hashlib.md5(hash_input, usedforsecurity=False).hexdigest()[:8]  # nosec B324 - non-security hash for rule ID generation

        return f"rule_{hash_digest}"

    def _build_prompt(
        self, rule: str, data_example: dict, extract_paths: bool = True
    ) -> str:
        """
        Build complete prompt using configuration template and few-shot examples.

        Args:
            rule: Natural language rule text
            data_example: Example JSON data document
            extract_paths: If True, request path_mappings; if False, only parameters

        Returns:
            Complete prompt string with system prompt, few-shot examples, and task
        """
        # Format data example as JSON
        data_example_json = json.dumps(data_example, indent=2)

        # Build few-shot examples section
        few_shot_section = ""
        if self.translator_config.few_shot_examples:
            few_shot_section = "\n\nHere are some examples of correct translations:\n\n"

            for i, example in enumerate(self.translator_config.few_shot_examples, 1):
                example_rule = example["rule"]
                example_data = json.dumps(example["data_example"], indent=2)
                example_output = json.dumps(example["output"], indent=2)

                few_shot_section += f"Example {i}:\n"
                few_shot_section += f"Rule: {example_rule}\n\n"
                few_shot_section += f"Data:\n{example_data}\n\n"
                few_shot_section += f"Output:\n{example_output}\n\n"

        # Build task prompt using template
        task_prompt = self.translator_config.task_prompt_template.format(
            rule=rule, data_example=data_example_json
        )

        # Add workflow-specific instructions
        if not extract_paths:
            workflow_instruction = """
IMPORTANT: For this translation, do NOT generate path_mappings. 
Only output:
- parameters: Variable definitions with types
- constraints: SMT-LIB constraint logic

Output format:
{
  "parameters": [...],
  "constraints": [...]
}

The data example is provided only to help you understand the parameter types and names.
Do not extract paths from the data structure.
"""
            task_prompt = task_prompt + "\n" + workflow_instruction

        # Combine all parts
        full_prompt = (
            f"{self.translator_config.system_prompt}\n\n{few_shot_section}{task_prompt}"
        )

        return full_prompt

    def _invoke_bedrock(
        self,
        prompt: str,
        rule_id: Optional[str] = None,
        max_retries: int = 2,
        initial_backoff: float = 1.0,
        use_extraction_config: bool = False,
    ) -> str:
        """
        Invoke Amazon Bedrock with retry logic and exponential backoff.

        Handles:
        - Transient errors (timeouts, rate limits, service unavailable)
        - Exponential backoff with jitter
        - Maximum retry attempts

        Args:
            prompt: Complete prompt to send to LLM
            rule_id: Optional rule ID for error context
            max_retries: Maximum number of retry attempts (default: 3)
            initial_backoff: Initial backoff delay in seconds (default: 1.0)
            use_extraction_config: If True, use extraction config; otherwise use translator config

        Returns:
            LLM response text

        Raises:
            TranslationError: If all retries fail or non-retryable error occurs
        """
        # Select config based on operation type
        config = (
            self.extraction_config if use_extraction_config else self.translator_config
        )
        operation_type = "extraction" if use_extraction_config else "translation"

        logger.info(f"Invoking Bedrock for {operation_type} (rule_id: {rule_id})")
        logger.debug(
            f"Model: {config.model_id}, Temperature: {config.temperature}, Max tokens: {config.max_tokens}"
        )

        # Log the full prompt at DEBUG level
        logger.debug("=" * 80)
        logger.debug(f"LLM INPUT ({operation_type}):")
        logger.debug("=" * 80)
        logger.debug(prompt)
        logger.debug("=" * 80)

        last_error = None
        backoff = initial_backoff

        for attempt in range(max_retries):
            try:
                # Prepare request body for Claude model
                request_body = {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": config.max_tokens,
                    "temperature": config.temperature,
                    "messages": [{"role": "user", "content": prompt}],
                }

                logger.debug(
                    f"Attempt {attempt + 1}/{max_retries}: Sending request to Bedrock"
                )

                # Invoke Bedrock
                response = self.bedrock_client.invoke_model(
                    modelId=config.model_id,
                    body=json.dumps(request_body),
                    contentType="application/json",
                    accept="application/json",
                )

                # Parse response
                response_body = json.loads(response["body"].read())

                # Extract text from Claude response
                if "content" in response_body and len(response_body["content"]) > 0:
                    llm_output = response_body["content"][0]["text"]

                    # Extract token usage information
                    input_tokens = response_body.get("usage", {}).get("input_tokens", 0)
                    output_tokens = response_body.get("usage", {}).get(
                        "output_tokens", 0
                    )
                    total_tokens = input_tokens + output_tokens

                    # Log the full response at DEBUG level
                    logger.debug("=" * 80)
                    logger.debug(f"LLM OUTPUT ({operation_type}):")
                    logger.debug("=" * 80)
                    logger.debug(llm_output)
                    logger.debug("=" * 80)

                    # Log token usage at INFO level
                    logger.info(
                        f"Bedrock invocation successful (attempt {attempt + 1}): "
                        f"Input tokens: {input_tokens}, Output tokens: {output_tokens}, "
                        f"Total tokens: {total_tokens}"
                    )

                    return llm_output
                else:
                    raise TranslationError(
                        message="Bedrock response missing 'content' field",
                        operation="invoke_bedrock",
                        rule_id=rule_id,
                        llm_response=json.dumps(response_body),
                        context={"attempt": attempt + 1},
                    )

            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "Unknown")
                error_message = e.response.get("Error", {}).get("Message", str(e))

                logger.warning(
                    f"Bedrock API error on attempt {attempt + 1}: {error_code} - {error_message}"
                )

                # Check if error is retryable
                retryable_errors = {
                    "ThrottlingException",
                    "ServiceUnavailableException",
                    "TooManyRequestsException",
                    "RequestTimeoutException",
                }

                if error_code in retryable_errors and attempt < max_retries - 1:
                    # Retry with exponential backoff
                    last_error = e
                    logger.info(f"Retrying after {backoff}s backoff...")
                    time.sleep(backoff)
                    backoff *= 2  # Exponential backoff
                    continue
                else:
                    # Non-retryable error or max retries reached
                    logger.error(
                        f"Bedrock invocation failed: {error_code} - {error_message}"
                    )
                    raise TranslationError(
                        message=f"Bedrock API error: {error_code} - {error_message}",
                        operation="invoke_bedrock",
                        rule_id=rule_id,
                        context={
                            "error_code": error_code,
                            "error_message": error_message,
                            "attempt": attempt + 1,
                            "max_retries": max_retries,
                        },
                    )

            except BotoCoreError as e:
                # BotoCore errors (connection issues, etc.)
                logger.warning(
                    f"Bedrock connection error on attempt {attempt + 1}: {e}"
                )

                if attempt < max_retries - 1:
                    last_error = e
                    logger.info(f"Retrying after {backoff}s backoff...")
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                else:
                    logger.error(
                        f"Bedrock connection failed after {max_retries} attempts"
                    )
                    raise TranslationError(
                        message=f"Bedrock connection error: {e}",
                        operation="invoke_bedrock",
                        rule_id=rule_id,
                        context={
                            "error_type": type(e).__name__,
                            "attempt": attempt + 1,
                            "max_retries": max_retries,
                        },
                    )

            except json.JSONDecodeError as e:
                # Failed to parse response body
                logger.error(f"Failed to parse Bedrock response JSON: {e}")
                raise TranslationError(
                    message=f"Failed to parse Bedrock response JSON: {e}",
                    operation="invoke_bedrock",
                    rule_id=rule_id,
                    context={"error": str(e), "attempt": attempt + 1},
                )

            except Exception as e:
                # Unexpected error
                logger.error(
                    f"Unexpected error during Bedrock invocation: {type(e).__name__} - {e}"
                )
                raise TranslationError(
                    message=f"Unexpected error during Bedrock invocation: {e}",
                    operation="invoke_bedrock",
                    rule_id=rule_id,
                    context={"error_type": type(e).__name__, "attempt": attempt + 1},
                )

        # Should not reach here, but just in case
        logger.error(f"Failed after {max_retries} attempts")
        raise TranslationError(
            message=f"Failed after {max_retries} attempts: {last_error}",
            operation="invoke_bedrock",
            rule_id=rule_id,
            context={"max_retries": max_retries},
        )

    def _parse_llm_output(
        self,
        llm_response: str,
        rule_id: Optional[str] = None,
        extract_paths: bool = True,
    ) -> dict:
        """
        Parse and validate LLM JSON response.

        Extracts JSON from response text (handles markdown code blocks)
        and validates required fields based on workflow.

        Args:
            llm_response: Raw LLM response text
            rule_id: Optional rule ID for error context
            extract_paths: If True, expect path_mappings; if False, only parameters

        Returns:
            Parsed dictionary with parameters, constraints, and optionally path_mappings

        Raises:
            TranslationError: If parsing fails or required fields are missing
        """
        # Try to extract JSON from response
        # LLM might wrap JSON in markdown code blocks
        json_text = llm_response.strip()

        # Remove markdown code blocks if present
        if json_text.startswith("```json"):
            json_text = json_text[7:]  # Remove ```json
        elif json_text.startswith("```"):
            json_text = json_text[3:]  # Remove ```

        if json_text.endswith("```"):
            json_text = json_text[:-3]  # Remove trailing ```

        json_text = json_text.strip()

        # Parse JSON
        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError as e:
            raise TranslationError(
                message=f"Failed to parse LLM output as JSON: {e}",
                operation="parse_llm_output",
                rule_id=rule_id,
                llm_response=llm_response,
                context={"json_error": str(e)},
            )

        # Validate required fields based on workflow
        if extract_paths:
            required_fields = ["parameters", "path_mappings", "constraints"]
        else:
            required_fields = ["parameters", "constraints"]

        missing_fields = [field for field in required_fields if field not in parsed]

        if missing_fields:
            raise TranslationError(
                message=f"LLM output missing required fields: {', '.join(missing_fields)}",
                operation="parse_llm_output",
                rule_id=rule_id,
                llm_response=llm_response,
                validation_errors=[
                    f"Missing field: {field}" for field in missing_fields
                ],
            )

        # Validate field types
        validation_errors = []

        if not isinstance(parsed["parameters"], list):
            validation_errors.append(
                f"'parameters' must be a list, got {type(parsed['parameters']).__name__}"
            )

        if extract_paths and not isinstance(parsed.get("path_mappings", []), list):
            validation_errors.append(
                f"'path_mappings' must be a list, got {type(parsed['path_mappings']).__name__}"
            )

        if not isinstance(parsed["constraints"], list):
            validation_errors.append(
                f"'constraints' must be a list, got {type(parsed['constraints']).__name__}"
            )

        if validation_errors:
            raise TranslationError(
                message="LLM output has invalid field types",
                operation="parse_llm_output",
                rule_id=rule_id,
                llm_response=llm_response,
                validation_errors=validation_errors,
            )

        # Validate non-empty lists
        if len(parsed["parameters"]) == 0:
            validation_errors.append("'parameters' list is empty")

        if len(parsed["constraints"]) == 0:
            validation_errors.append("'constraints' list is empty")

        # Note: path_mappings can be empty (LLM decides based on rule complexity)
        # Empty path_mappings → automatic LLM extraction fallback

        if validation_errors:
            raise TranslationError(
                message="LLM output has empty required lists",
                operation="parse_llm_output",
                rule_id=rule_id,
                llm_response=llm_response,
                validation_errors=validation_errors,
            )

        # For Workflow B, ensure path_mappings is empty list
        if not extract_paths:
            parsed["path_mappings"] = []

        return parsed

    def _build_rule_json(
        self,
        parsed_output: dict,
        rule_id: str,
        version: str,
        description: str,
        natural_language_rule: str,
        extract_paths: bool = True,
    ) -> RuleJSON:
        """
        Build RuleJSON object from parsed LLM output.

        Args:
            parsed_output: Parsed dictionary from LLM
            rule_id: Rule identifier
            version: Rule version
            description: Rule description
            natural_language_rule: Original rule text
            extract_paths: Whether path_mappings were extracted

        Returns:
            RuleJSON object

        Raises:
            ValueError: If RuleJSON construction fails
        """
        # Parse parameters
        parameters = []
        for param_dict in parsed_output["parameters"]:
            try:
                param = Parameter.from_dict(param_dict)
                parameters.append(param)
            except Exception as e:
                raise ValueError(f"Failed to parse parameter {param_dict}: {e}")

        # Parse path mappings (if present)
        path_mappings = []
        if extract_paths and "path_mappings" in parsed_output:
            for mapping_dict in parsed_output["path_mappings"]:
                try:
                    mapping = PathMapping.from_dict(mapping_dict)
                    path_mappings.append(mapping)
                except Exception as e:
                    raise ValueError(
                        f"Failed to parse path mapping {mapping_dict}: {e}"
                    )

        # Extract constraints
        constraints = parsed_output["constraints"]

        # Build metadata
        metadata = {
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "translator_version": "1.0",
            "model_id": self.translator_config.model_id,
            "workflow": "path_based" if extract_paths else "llm_based",
        }

        # Create RuleJSON
        rule_json = RuleJSON(
            rule_id=rule_id,
            version=version,
            description=description,
            natural_language_rule=natural_language_rule,
            parameters=parameters,
            constraints=constraints,
            path_mappings=path_mappings,
            metadata=metadata,
        )

        return rule_json

    def _validate_rule_json(self, rule_json: RuleJSON):
        """
        Perform additional validation on RuleJSON.

        The RuleJSON class already validates structure in __post_init__,
        but this method can perform additional semantic validation.

        Args:
            rule_json: RuleJSON to validate

        Raises:
            ValueError: If validation fails
        """
        # RuleJSON.__post_init__ already validates:
        # - Required fields are non-empty
        # - Parameter types are valid
        # - Parameter-path mapping consistency
        # - Constraint parameter references

        # Additional validation can be added here if needed
        # For now, rely on RuleJSON's built-in validation
        pass

    def _build_extraction_prompt(self, rule_json: RuleJSON, data: Any) -> str:
        """
        Build prompt for extracting parameter values from any data format.

        Uses configurable prompts from config file.

        Args:
            rule_json: Rule with parameters to extract
            data: Data in any format

        Returns:
            Complete prompt for LLM
        """
        # Format data (handle different types)
        if isinstance(data, dict):
            data_str = json.dumps(data, indent=2)
            data_type = "structured JSON"
        elif isinstance(data, str):
            data_str = data
            data_type = "text"
        elif isinstance(data, list):
            data_str = json.dumps(data, indent=2)
            data_type = "list/array"
        else:
            data_str = str(data)
            data_type = "unknown format"

        # Format parameters
        params_info = []
        for param in rule_json.parameters:
            params_info.append(
                {"name": param.name, "type": param.type, "required": param.required}
            )
        params_json = json.dumps(params_info, indent=2)

        # Build task prompt using template from config
        task_prompt = self.extraction_config.task_prompt_template.format(
            rule_description=rule_json.description,
            natural_language_rule=rule_json.natural_language_rule,
            parameters_json=params_json,
            data_type=data_type,
            data=data_str,
        )

        # Combine system prompt and task prompt
        full_prompt = f"{self.extraction_config.system_prompt}\n\n{task_prompt}"

        return full_prompt

    def _parse_extraction_output(
        self, llm_response: str, rule_json: RuleJSON
    ) -> Dict[str, Any]:
        """
        Parse LLM output for extracted values.

        Args:
            llm_response: Raw LLM response text
            rule_json: Rule for validation

        Returns:
            Dictionary mapping parameter names to values

        Raises:
            TranslationError: If parsing fails or values are invalid
        """
        # Extract JSON from response
        json_text = llm_response.strip()

        # Remove markdown code blocks if present
        if json_text.startswith("```json"):
            json_text = json_text[7:]
        elif json_text.startswith("```"):
            json_text = json_text[3:]

        if json_text.endswith("```"):
            json_text = json_text[:-3]

        json_text = json_text.strip()

        # Parse JSON
        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError as e:
            raise TranslationError(
                message=f"Failed to parse LLM output as JSON: {e}",
                operation="parse_extraction_output",
                rule_id=rule_json.rule_id,
                llm_response=llm_response,
                context={"json_error": str(e)},
            )

        # Validate structure
        if "extracted_values" not in parsed:
            raise TranslationError(
                message="LLM output missing 'extracted_values' field",
                operation="parse_extraction_output",
                rule_id=rule_json.rule_id,
                llm_response=llm_response,
            )

        extracted_values = parsed["extracted_values"]

        if not isinstance(extracted_values, dict):
            raise TranslationError(
                message="'extracted_values' must be a dictionary",
                operation="parse_extraction_output",
                rule_id=rule_json.rule_id,
                llm_response=llm_response,
            )

        # Validate required parameters are present
        validation_errors = []
        for param in rule_json.parameters:
            if param.required and param.name not in extracted_values:
                validation_errors.append(f"Missing required parameter: {param.name}")
            elif (
                param.name in extracted_values
                and extracted_values[param.name] is None
                and param.required
            ):
                validation_errors.append(f"Required parameter '{param.name}' is null")

        if validation_errors:
            raise TranslationError(
                message="Extracted values missing required parameters",
                operation="parse_extraction_output",
                rule_id=rule_json.rule_id,
                llm_response=llm_response,
                validation_errors=validation_errors,
            )

        return extracted_values
