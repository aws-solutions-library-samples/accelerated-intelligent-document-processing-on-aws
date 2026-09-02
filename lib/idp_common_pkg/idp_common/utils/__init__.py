# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import random
import time
import logging
from typing import Tuple, Dict, Any, Optional

from idp_common.config.models import IDPConfig

# Import yaml with fallback for systems that don't have it installed
try:
    import yaml
except ImportError:
    yaml = None

# Import Lambda metering utility
from .lambda_metering import calculate_lambda_metering

# Import settings helper utilities
from .settings_helper import get_settings, get_setting, clear_cache

logger = logging.getLogger(__name__)

# Common backoff constants
MAX_RETRIES = 7
INITIAL_BACKOFF = 2  # seconds
MAX_BACKOFF = 300  # 5 minutes


def calculate_backoff(
    attempt: int,
    initial_backoff: float = INITIAL_BACKOFF,
    max_backoff: float = MAX_BACKOFF,
) -> float:
    """
    Calculate exponential backoff with jitter

    Args:
        attempt: The current retry attempt number (0-based)
        initial_backoff: Starting backoff in seconds
        max_backoff: Maximum backoff cap in seconds

    Returns:
        Backoff time in seconds
    """
    backoff = min(max_backoff, initial_backoff * (2**attempt))
    jitter = random.uniform(0, 0.1 * backoff)  # nosec B311 - retry jitter
    return backoff + jitter


def parse_s3_uri(s3_uri: str) -> Tuple[str, str]:
    """
    Parse an S3 URI into bucket and key

    Args:
        s3_uri: The S3 URI in format s3://bucket/key

    Returns:
        Tuple of (bucket, key)
    """
    if not s3_uri.startswith("s3://"):
        raise ValueError(f"Invalid S3 URI: {s3_uri}. Must start with s3://")

    parts = s3_uri.split("/", 3)
    if len(parts) < 4:
        raise ValueError(f"Invalid S3 URI: {s3_uri}. Format should be s3://bucket/key")

    bucket = parts[2]
    key = parts[3]
    return bucket, key


def build_s3_uri(bucket: str, key: str) -> str:
    """
    Build an S3 URI from bucket and key

    Args:
        bucket: The S3 bucket name
        key: The S3 object key

    Returns:
        S3 URI in format s3://bucket/key
    """
    return f"s3://{bucket}/{key}"


def merge_metering_data(
    existing_metering: Dict[str, Any], new_metering: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Merge metering data from multiple sources

    Args:
        existing_metering: Existing metering data to merge into
        new_metering: New metering data to add

    Returns:
        Merged metering data
    """
    merged = existing_metering.copy()

    for service_api, metrics in new_metering.items():
        if isinstance(metrics, dict):
            for unit, value in metrics.items():
                if service_api not in merged:
                    merged[service_api] = {}

                # Convert both values to numbers to handle string vs int mismatch
                try:
                    existing_value = merged[service_api].get(unit, 0)
                    # Handle both string and numeric values
                    if isinstance(existing_value, str):
                        existing_value = float(existing_value)
                    if isinstance(value, str):
                        value = float(value)

                    merged[service_api][unit] = existing_value + value
                except (ValueError, TypeError) as e:
                    logger.warning(
                        f"Error converting metering values for {service_api}.{unit}: existing={merged[service_api].get(unit)}, new={value}, error={e}"
                    )
                    # Fallback to new value if conversion fails
                    merged[service_api][unit] = value
        else:
            logger.warning(
                f"Unexpected metering data format for {service_api}: {metrics}"
            )

    return merged


def extract_json_from_text(text: str) -> str:
    """
    Extract JSON string from LLM response text with improved multi-line handling.

    This enhanced version handles JSON with literal newlines and provides
    multiple fallback strategies for robust JSON extraction.

    This function handles multiple common formats:
    - JSON wrapped in ```json code blocks
    - JSON wrapped in ``` code blocks
    - Raw JSON objects with proper brace matching
    - Multi-line JSON with literal newlines in string values

    Args:
        text: The text response from the model

    Returns:
        Extracted JSON string, or original text if no JSON found
    """
    import json
    import re

    if not text:
        logger.warning("Empty text provided to extract_json_from_text")
        return text

    # Strategy 1: Check for code block format with json tag
    if "```json" in text:
        start_idx = text.find("```json") + len("```json")
        end_idx = text.find("```", start_idx)
        if end_idx > start_idx:
            json_str = text[start_idx:end_idx].strip()
            try:
                # Test if it's valid JSON
                json.loads(json_str)
                return json_str
            except json.JSONDecodeError:
                logger.debug(
                    "Found code block but content is not valid JSON, trying other strategies"
                )

    # Strategy 2: Check for generic code block format
    elif "```" in text:
        start_idx = text.find("```") + len("```")
        end_idx = text.find("```", start_idx)
        if end_idx > start_idx:
            json_str = text[start_idx:end_idx].strip()
            try:
                # Test if it's valid JSON
                json.loads(json_str)
                return json_str
            except json.JSONDecodeError:
                logger.debug(
                    "Found code block but content is not valid JSON, trying other strategies"
                )

    # Strategy 3: Extract JSON between braces and try direct parsing
    if "{" in text and "}" in text:
        start_idx = text.find("{")
        # Find matching closing brace
        open_braces = 0
        in_string = False
        escape_next = False

        for i in range(start_idx, len(text)):
            char = text[i]

            if escape_next:
                escape_next = False
                continue

            if char == "\\":
                escape_next = True
                continue

            if char == '"' and not escape_next:
                in_string = not in_string
                continue

            if not in_string:
                if char == "{":
                    open_braces += 1
                elif char == "}":
                    open_braces -= 1
                    if open_braces == 0:
                        json_str = text[start_idx : i + 1].strip()
                        try:
                            # Test if it's valid JSON as-is
                            json.loads(json_str)
                            return json_str
                        except json.JSONDecodeError:
                            # If direct parsing fails, continue to next strategy
                            logger.debug(
                                "Found JSON-like content but direct parsing failed, trying normalization"
                            )
                            break

    # Strategy 4: Try to extract JSON using more aggressive methods
    try:
        # Find the outermost braces
        if "{" in text and "}" in text:
            start_idx = text.find("{")
            end_idx = text.rfind("}")  # Use rfind to get the last closing brace
            if end_idx > start_idx:
                json_str = text[start_idx : end_idx + 1]

                # Try parsing as-is first
                try:
                    json.loads(json_str)
                    return json_str
                except json.JSONDecodeError:
                    pass

                # Try normalizing the JSON string
                try:
                    # Method 1: Handle literal newlines by replacing with spaces
                    normalized_json = " ".join(
                        line.strip() for line in json_str.splitlines()
                    )
                    json.loads(normalized_json)
                    return normalized_json
                except json.JSONDecodeError:
                    pass

                # Method 2: Try a more aggressive approach with regex
                try:
                    # Remove extra whitespace but preserve structure
                    normalized_json = re.sub(r"\s+", " ", json_str)
                    json.loads(normalized_json)
                    return normalized_json
                except json.JSONDecodeError:
                    logger.debug("All normalization attempts failed")
    except Exception as e:
        logger.warning(f"Error during JSON extraction: {str(e)}")

    # If all strategies fail, return the original text
    logger.warning("Could not extract valid JSON, returning original text")
    return text


def normalize_boolean_value(value: Any) -> bool:
    """
    Normalize a value to a boolean, handling string representations.

    This function is useful for configuration values that may come as strings
    (e.g., from JSON config files or environment variables) but need to be
    treated as booleans.

    Args:
        value: Value to normalize (can be bool, str, or other)

    Returns:
        Boolean value
    """
    if isinstance(value, bool):
        return value
    elif isinstance(value, str):
        return value.lower() in ("true", "1", "yes", "on")
    else:
        return bool(value)


def extract_yaml_from_text(text: str) -> str:
    """
    Extract YAML string from LLM response text with robust multi-strategy handling.

    This function handles multiple common formats:
    - YAML wrapped in ```yaml code blocks
    - YAML wrapped in ``` code blocks
    - Raw YAML with document markers (---)
    - Raw YAML content with proper indentation detection

    Args:
        text: The text response from the model

    Returns:
        Extracted YAML string, or original text if no YAML found
    """
    import re

    if yaml is None:
        logger.error(
            "YAML library not available. Please install PyYAML to use YAML parsing functionality."
        )
        return text

    if not text:
        logger.warning("Empty text provided to extract_yaml_from_text")
        return text

    # Strategy 1: Check for code block format with yaml tag
    if "```yaml" in text:
        start_idx = text.find("```yaml") + len("```yaml")
        end_idx = text.find("```", start_idx)
        if end_idx > start_idx:
            yaml_str = text[start_idx:end_idx].strip()
            try:
                # Test if it's valid YAML
                yaml.safe_load(yaml_str)
                return yaml_str
            except yaml.YAMLError:
                logger.debug(
                    "Found yaml code block but content is not valid YAML, falling back to original text"
                )
                return text

    # Strategy 2: Check for code block format with yml tag
    elif "```yml" in text:
        start_idx = text.find("```yml") + len("```yml")
        end_idx = text.find("```", start_idx)
        if end_idx > start_idx:
            yaml_str = text[start_idx:end_idx].strip()
            try:
                # Test if it's valid YAML
                yaml.safe_load(yaml_str)
                return yaml_str
            except yaml.YAMLError:
                logger.debug(
                    "Found yml code block but content is not valid YAML, trying other strategies"
                )

    # Strategy 3: Check for generic code block format and validate as YAML
    elif "```" in text:
        start_idx = text.find("```") + len("```")
        end_idx = text.find("```", start_idx)
        if end_idx > start_idx:
            yaml_str = text[start_idx:end_idx].strip()
            try:
                # Test if it's valid YAML
                yaml.safe_load(yaml_str)
                return yaml_str
            except yaml.YAMLError:
                logger.debug(
                    "Found code block but content is not valid YAML, trying other strategies"
                )

    # Strategy 4: Look for YAML document markers (---)
    if "---" in text:
        # Find YAML document start
        start_marker = text.find("---")
        if start_marker != -1:
            # Look for document end marker or end of text
            start_idx = start_marker
            end_marker = text.find("---", start_marker + 3)
            if end_marker != -1:
                # Found end marker
                yaml_str = text[start_idx:end_marker].strip()
            else:
                # No end marker, take rest of text
                yaml_str = text[start_idx:].strip()

            try:
                # Test if it's valid YAML
                yaml.safe_load(yaml_str)
                return yaml_str
            except yaml.YAMLError:
                logger.debug(
                    "Found YAML document markers but content is not valid YAML, trying other strategies"
                )

    # Strategy 5: Try to detect YAML by looking for key indicators
    # Look for patterns like "key:" at the start of lines
    yaml_indicators = [
        r"^\s*\w+\s*:",  # key: value patterns
        r"^\s*-\s+\w+",  # list item patterns
        r"^\s*-\s*$",  # empty list items
    ]

    lines = text.split("\n")
    yaml_like_lines = 0
    total_non_empty_lines = 0

    for line in lines:
        if line.strip():
            total_non_empty_lines += 1
            for pattern in yaml_indicators:
                if re.match(pattern, line):
                    yaml_like_lines += 1
                    break

    # If more than 50% of non-empty lines look like YAML and we have at least 2 lines, try to parse the whole text
    if total_non_empty_lines >= 2 and yaml_like_lines / total_non_empty_lines > 0.5:
        try:
            yaml.safe_load(text)
            return text
        except yaml.YAMLError:
            logger.debug("Text appears YAML-like but is not valid YAML")

    # Strategy 6: Try to extract YAML-like content by finding indented blocks
    try:
        # Look for blocks that start with a key: pattern and have consistent indentation
        yaml_block_pattern = r"(?:^|\n)(\w+\s*:(?:\s*\n(?:\s{2,}.*\n?)*|\s*.*(?:\n|$))(?:\w+\s*:(?:\s*\n(?:\s{2,}.*\n?)*|\s*.*(?:\n|$)))*)"
        matches = re.findall(yaml_block_pattern, text, re.MULTILINE)

        for match in matches:
            try:
                yaml.safe_load(match)
                return match.strip()
            except yaml.YAMLError:
                continue

    except Exception as e:
        logger.debug(f"Error during YAML block extraction: {str(e)}")

    # If all strategies fail, return the original text
    logger.warning("Could not extract valid YAML, returning original text")
    return text


def detect_format(text: str) -> str:
    """
    Detect whether text contains JSON or YAML format.

    Args:
        text: The text to analyze

    Returns:
        'json', 'yaml', or 'unknown'
    """
    import json
    import re

    if yaml is None:
        logger.warning(
            "YAML library not available. Format detection will only work for JSON."
        )

    if not text or not text.strip():
        return "unknown"

    text = text.strip()

    # Check for explicit format indicators in code blocks
    if "```json" in text.lower():
        return "json"
    elif "```yaml" in text.lower() or "```yml" in text.lower():
        return "yaml"

    # Check for YAML document markers
    if text.startswith("---"):
        return "yaml"

    # Check for JSON structural indicators
    if (text.startswith("{") and text.endswith("}")) or (
        text.startswith("[") and text.endswith("]")
    ):
        # Try to parse as JSON first
        try:
            json.loads(text)
            return "json"
        except json.JSONDecodeError:
            pass

    # Check for YAML structural indicators (only if yaml is available)
    if yaml is not None:
        yaml_patterns = [
            r"^\s*\w+\s*:",  # key: value at start of line
            r"^\s*-\s+",  # list items
            r":\s*\n\s+",  # multiline values
        ]

        for pattern in yaml_patterns:
            if re.search(pattern, text, re.MULTILINE):
                # Try to parse as YAML
                try:
                    yaml.safe_load(text)
                    return "yaml"
                except yaml.YAMLError:
                    pass

    # Try parsing both formats to determine which works
    json_works = False
    yaml_works = False

    try:
        json.loads(text)
        json_works = True
    except (json.JSONDecodeError, TypeError):
        pass

    if yaml is not None:
        try:
            parsed_yaml = yaml.safe_load(text)
            # Only consider it valid YAML if it's a dict or list (structured data)
            # Plain strings are not considered structured YAML
            if isinstance(parsed_yaml, (dict, list)):
                yaml_works = True
        except yaml.YAMLError:
            pass

    # Return the format that works, preferring JSON if both work
    if json_works and yaml_works:
        return "json"  # Prefer JSON if both formats are valid
    elif json_works:
        return "json"
    elif yaml_works:
        return "yaml"
    else:
        return "unknown"


def parse_confidence(value: Any, *, context: str = "") -> Optional[float]:
    """Parse an OPTIONAL model-reported classification confidence.

    The classification prompt is free to ask the model for a ``confidence`` key
    (``docs/classification.md`` has documented that shape for a long time, and
    the value was silently discarded until GitHub #673). This is the one place
    that turns whatever a model — or a service such as Bedrock Data Automation,
    whose matched-blueprint confidence plays the same role — actually reported
    into a usable number, or into ``None``, which means NOT SCORED and is not an
    error condition.

    Lives in core utils, next to ``extract_structured_data_from_text``, so the
    BDA result processor can share it without depending on the classification
    extra. Re-exported from ``idp_common.classification.service``.

    Accepts a float/int, a numeric string, and a percentage: models asked for a
    probability routinely answer ``95`` or ``"95%"`` rather than ``0.95``, so a
    value in ``(1, 100]`` is read as a percentage. That heuristic makes ``1``
    ambiguous (1 % or certainty?) and it is resolved as ``1.0`` — full
    confidence — because a model emitting a bare ``1`` in a field it was told is
    a 0-1 probability means certainty, and a genuine 1 % answer would be
    written ``0.01``.

    Anything unparseable or out of range returns ``None`` rather than raising: a
    malformed confidence must not fail a classification that otherwise
    succeeded.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip().rstrip("%").strip()
        if not value:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring unparseable classification confidence %r%s",
            value,
            f" ({context})" if context else "",
        )
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        logger.warning(
            "Ignoring non-finite classification confidence %r%s",
            value,
            f" ({context})" if context else "",
        )
        return None
    if 1.0 < number <= 100.0:
        number /= 100.0
    if not 0.0 <= number <= 1.0:
        logger.warning(
            "Ignoring out-of-range classification confidence %r%s",
            value,
            f" ({context})" if context else "",
        )
        return None
    return number


def extract_structured_data_from_text(
    text: str, preferred_format: str = "auto"
) -> Tuple[Any, str]:
    """
    Extract structured data from text, supporting both JSON and YAML formats.

    This function automatically detects the format and parses the content,
    returning the parsed data structure and the detected format.

    Args:
        text: The text response from the model
        preferred_format: 'json', 'yaml', or 'auto' for automatic detection

    Returns:
        Tuple of (parsed_data, detected_format)
        - parsed_data: The parsed data structure (dict, list, etc.) or original text if parsing fails
        - detected_format: 'json', 'yaml', or 'unknown'
    """
    import json

    if yaml is None:
        logger.warning(
            "YAML library not available. Structured data extraction will only work for JSON."
        )

    if not text:
        logger.warning("Empty text provided to extract_structured_data_from_text")
        return text, "unknown"

    # Determine format to use
    if preferred_format == "auto":
        detected_format = detect_format(text)
    else:
        detected_format = preferred_format.lower()

    # Extract and parse based on detected/preferred format
    if detected_format == "json":
        try:
            json_str = extract_json_from_text(text)
            parsed_data = json.loads(json_str)
            return parsed_data, "json"
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(f"Failed to parse as JSON: {e}")
            # Fallback to YAML if JSON parsing fails
            try:
                yaml_str = extract_yaml_from_text(text)
                parsed_data = yaml.safe_load(yaml_str)
                return parsed_data, "yaml"
            except yaml.YAMLError as yaml_e:
                logger.warning(f"Fallback YAML parsing also failed: {yaml_e}")
                return text, "unknown"

    elif detected_format == "yaml":
        try:
            yaml_str = extract_yaml_from_text(text)
            # Check if YAML extraction actually found structured content
            if yaml_str == text:
                # YAML extraction fell back to original text
                # Check if the original text was detected as JSON format initially
                original_format = detect_format(text)
                if original_format == "json":
                    # This is actually JSON, not YAML
                    raise yaml.YAMLError("Text is actually JSON format, not YAML")
                # If it's unknown format, also fall back
                elif original_format == "unknown":
                    raise yaml.YAMLError("No valid YAML structure found")

            parsed_data = yaml.safe_load(yaml_str)
            # Only consider it successful if we got structured data (dict or list)
            if isinstance(parsed_data, (dict, list)):
                return parsed_data, "yaml"
            else:
                # Got a simple string, not structured data
                raise yaml.YAMLError(
                    "YAML parsing returned simple string, not structured data"
                )
        except yaml.YAMLError as e:
            logger.warning(f"Failed to parse as YAML: {e}")
            # Fallback to JSON if YAML parsing fails
            try:
                json_str = extract_json_from_text(text)
                parsed_data = json.loads(json_str)
                return parsed_data, "json"
            except (json.JSONDecodeError, TypeError) as json_e:
                logger.warning(f"Fallback JSON parsing also failed: {json_e}")
                return text, "unknown"

    else:
        # Unknown format - try both
        logger.info("Unknown format detected, trying both JSON and YAML parsing")

        # Try JSON first
        try:
            json_str = extract_json_from_text(text)
            parsed_data = json.loads(json_str)
            return parsed_data, "json"
        except (json.JSONDecodeError, TypeError):
            pass

        # Try YAML second
        try:
            yaml_str = extract_yaml_from_text(text)
            # Check if YAML extraction actually found structured content
            if yaml_str == text:
                # YAML extraction fell back to original text, check if it's actually structured
                parsed_data = yaml.safe_load(yaml_str)
                if not isinstance(parsed_data, (dict, list)):
                    # Got a simple string, not structured data
                    raise yaml.YAMLError(
                        "YAML parsing returned simple string, not structured data"
                    )
            else:
                parsed_data = yaml.safe_load(yaml_str)
            return parsed_data, "yaml"
        except yaml.YAMLError:
            pass

        # If both fail, return original text
        logger.warning(
            "Could not parse as either JSON or YAML, returning original text"
        )
        return text, "unknown"


def repair_truncated_json(text: str) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """
    Attempt to repair and parse truncated JSON from LLM output.

    This function handles cases where LLM output is cut off mid-stream (typically due to
    hitting max_tokens limit), resulting in invalid JSON. It attempts multiple repair
    strategies to recover as much data as possible.

    Args:
        text: Raw text that may contain truncated JSON

    Returns:
        Tuple of (parsed_data, repair_info)
        - parsed_data: Parsed dict if successful, None if repair failed
        - repair_info: Dict with keys:
            - was_truncated: True if JSON appeared to be truncated
            - repair_succeeded: True if repair was successful
            - repair_method: Description of method used (if successful)
            - fields_recovered: Number of top-level fields recovered (if successful)
            - error: Error message (if repair failed)
    """
    import json
    import re

    repair_info = {
        "was_truncated": False,
        "repair_succeeded": False,
        "repair_method": None,
        "fields_recovered": 0,
        "error": None,
    }

    if not text or not text.strip():
        repair_info["error"] = "Empty text provided"
        return None, repair_info

    # First, try standard extraction - maybe it's actually valid
    try:
        json_str = extract_json_from_text(text)
        parsed = json.loads(json_str)
        if isinstance(parsed, dict):
            # Successfully parsed without repair
            repair_info["fields_recovered"] = len(parsed)
            return parsed, repair_info
    except (json.JSONDecodeError, TypeError):
        pass

    # If we reach here, JSON is likely truncated
    repair_info["was_truncated"] = True
    logger.info("JSON appears truncated, attempting repair...")

    # Extract JSON-like content from text (handle markdown code blocks)
    json_text = text
    if "```json" in text:
        start_idx = text.find("```json") + len("```json")
        end_idx = text.find("```", start_idx)
        if end_idx == -1:
            # No closing block - take everything after ```json
            json_text = text[start_idx:].strip()
        else:
            json_text = text[start_idx:end_idx].strip()
    elif "```" in text:
        start_idx = text.find("```") + len("```")
        end_idx = text.find("```", start_idx)
        if end_idx == -1:
            json_text = text[start_idx:].strip()
        else:
            json_text = text[start_idx:end_idx].strip()

    # Find the start of JSON object
    if "{" not in json_text:
        repair_info["error"] = "No JSON object found in text"
        return None, repair_info

    start_idx = json_text.find("{")
    json_text = json_text[start_idx:]

    # Strategy 1: Close open brackets/braces progressively
    def try_close_json(text: str) -> Optional[Dict[str, Any]]:
        """Try to close open JSON structures."""
        # Track open structures
        stack = []
        in_string = False
        escape_next = False
        last_valid_pos = 0

        for i, char in enumerate(text):
            if escape_next:
                escape_next = False
                continue

            if char == "\\":
                escape_next = True
                continue

            if char == '"' and not escape_next:
                in_string = not in_string
                continue

            if not in_string:
                if char == "{":
                    stack.append("}")
                    last_valid_pos = i + 1
                elif char == "[":
                    stack.append("]")
                    last_valid_pos = i + 1
                elif char == "}":
                    if stack and stack[-1] == "}":
                        stack.pop()
                        last_valid_pos = i + 1
                elif char == "]":
                    if stack and stack[-1] == "]":
                        stack.pop()
                        last_valid_pos = i + 1
                elif char == ",":
                    # Track position after comma for potential truncation point
                    last_valid_pos = i + 1

        # If we're in a string, close it
        if in_string:
            text = text + '"'

        # Close all open structures
        closing = "".join(reversed(stack))
        repaired = text + closing

        try:
            parsed = json.loads(repaired)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        return None

    # Try Strategy 1
    result = try_close_json(json_text)
    if result:
        repair_info["repair_succeeded"] = True
        repair_info["repair_method"] = "closed_open_brackets"
        repair_info["fields_recovered"] = len(result)
        logger.info(
            f"JSON repair successful (closed brackets): recovered {len(result)} fields"
        )
        return result, repair_info

    # Strategy 2: Find last complete object/array element and truncate there
    def find_last_complete_element(text: str) -> Optional[str]:
        """Find the last position where we had complete JSON elements."""
        # Look for patterns that indicate end of complete elements
        # Pattern: }, followed by possible whitespace, then either } or ]
        # This indicates a complete nested object

        # Try progressively shorter versions
        for end_pos in range(len(text), 0, -1):
            candidate = text[:end_pos]

            # Count brackets to see if we can close it
            open_braces = candidate.count("{") - candidate.count("}")
            open_brackets = candidate.count("[") - candidate.count("]")

            # Check if we're in a string (simple heuristic: odd number of unescaped quotes)
            quote_count = len(re.findall(r'(?<!\\)"', candidate))
            in_string = quote_count % 2 == 1

            if in_string:
                # Close the string
                candidate = candidate + '"'

            # Close remaining structures
            closing = "}" * max(0, open_braces) + "]" * max(0, open_brackets)

            # Make sure we're at a valid truncation point (after a comma, colon, or bracket)
            last_char = candidate.rstrip()[-1] if candidate.rstrip() else ""
            if last_char in ",:[{":
                # We're in the middle of something, try removing the incomplete part
                # Find the last comma or bracket before this
                for back_pos in range(len(candidate) - 1, 0, -1):
                    c = candidate[back_pos]
                    if c in ",}]":
                        truncated = candidate[: back_pos + 1]
                        # Recount brackets
                        open_braces = truncated.count("{") - truncated.count("}")
                        open_brackets = truncated.count("[") - truncated.count("]")
                        closing = "}" * max(0, open_braces) + "]" * max(
                            0, open_brackets
                        )

                        try:
                            result = json.loads(truncated + closing)
                            if isinstance(result, dict):
                                return truncated + closing
                        except json.JSONDecodeError:
                            continue
            else:
                try:
                    result = json.loads(candidate + closing)
                    if isinstance(result, dict):
                        return candidate + closing
                except json.JSONDecodeError:
                    continue

        return None

    repaired_json = find_last_complete_element(json_text)
    if repaired_json:
        try:
            result = json.loads(repaired_json)
            repair_info["repair_succeeded"] = True
            repair_info["repair_method"] = "truncated_to_last_complete_element"
            repair_info["fields_recovered"] = len(result)
            logger.info(
                f"JSON repair successful (truncated): recovered {len(result)} fields"
            )
            return result, repair_info
        except json.JSONDecodeError:
            pass

    # Strategy 3: Try to extract at least the top-level complete fields
    def extract_complete_fields(text: str) -> Optional[Dict[str, Any]]:
        """Extract fields that are completely defined."""
        # Start fresh - look for field patterns
        result = {}

        # Find field definitions: "field_name": value
        # We'll extract fields one by one
        pos = text.find("{")
        if pos == -1:
            return None

        pos += 1  # Move past opening brace

        while pos < len(text):
            # Skip whitespace
            while pos < len(text) and text[pos] in " \t\n\r":
                pos += 1

            if pos >= len(text):
                break

            # Look for field name
            if text[pos] != '"':
                pos += 1
                continue

            # Find field name
            name_start = pos + 1
            name_end = text.find('"', name_start)
            if name_end == -1:
                break

            field_name = text[name_start:name_end]
            pos = name_end + 1

            # Skip to colon
            while pos < len(text) and text[pos] in " \t\n\r":
                pos += 1

            if pos >= len(text) or text[pos] != ":":
                break

            pos += 1  # Move past colon

            # Skip whitespace
            while pos < len(text) and text[pos] in " \t\n\r":
                pos += 1

            if pos >= len(text):
                break

            # Extract value
            value_start = pos
            value = None

            if text[pos] == '"':
                # String value
                string_end = pos + 1
                while string_end < len(text):
                    if text[string_end] == "\\" and string_end + 1 < len(text):
                        string_end += 2
                        continue
                    if text[string_end] == '"':
                        break
                    string_end += 1

                if string_end < len(text) and text[string_end] == '"':
                    try:
                        value = json.loads(text[value_start : string_end + 1])
                        pos = string_end + 1
                    except json.JSONDecodeError:
                        break
                else:
                    break  # Incomplete string

            elif text[pos] in "{[":
                # Object or array - find matching bracket
                bracket_type = text[pos]
                close_bracket = "}" if bracket_type == "{" else "]"
                depth = 1
                scan_pos = pos + 1
                in_str = False
                escape = False

                while scan_pos < len(text) and depth > 0:
                    c = text[scan_pos]
                    if escape:
                        escape = False
                        scan_pos += 1
                        continue
                    if c == "\\":
                        escape = True
                        scan_pos += 1
                        continue
                    if c == '"':
                        in_str = not in_str
                    elif not in_str:
                        if c == bracket_type:
                            depth += 1
                        elif c == close_bracket:
                            depth -= 1
                    scan_pos += 1

                if depth == 0:
                    try:
                        value = json.loads(text[value_start:scan_pos])
                        pos = scan_pos
                    except json.JSONDecodeError:
                        break
                else:
                    break  # Incomplete object/array

            elif text[pos : pos + 4] == "null":
                value = None
                pos += 4
            elif text[pos : pos + 4] == "true":
                value = True
                pos += 4
            elif text[pos : pos + 5] == "false":
                value = False
                pos += 5
            else:
                # Try to parse as number
                num_match = re.match(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", text[pos:])
                if num_match:
                    try:
                        num_str = num_match.group()
                        value = (
                            float(num_str)
                            if "." in num_str or "e" in num_str.lower()
                            else int(num_str)
                        )
                        pos += len(num_str)
                    except ValueError:
                        break
                else:
                    break

            if value is not None or text[value_start : value_start + 4] == "null":
                result[field_name] = value

            # Skip whitespace and comma
            while pos < len(text) and text[pos] in " \t\n\r,":
                pos += 1

        return result if result else None

    result = extract_complete_fields(json_text)
    if result:
        repair_info["repair_succeeded"] = True
        repair_info["repair_method"] = "extracted_complete_fields"
        repair_info["fields_recovered"] = len(result)
        logger.info(
            f"JSON repair successful (field extraction): recovered {len(result)} fields"
        )
        return result, repair_info

    repair_info["error"] = "All repair strategies failed"
    logger.warning("JSON repair failed - all strategies exhausted")
    return None, repair_info


def check_token_limit(
    document_text: str, extraction_results: Dict[str, Any], config: IDPConfig
) -> Optional[str]:
    """
    Create token limit warning message based on the configured value of max_tokens

    Args:
        document_text: The document text content
        extraction_results: Extraction results dictionary
        config: Configuration dictionary containing model and assessment settings

    Returns:
        Error message based on the configured_max_tokens, None otherwise
    """
    # Information for logging and troubleshooting
    assessment_config = config.assessment
    logger.info(f"assessment_config: {assessment_config}")
    model_id = assessment_config.model or "unknown"
    logger.info(f"model_id: {model_id}")
    configured_max_tokens = assessment_config.max_tokens
    logger.info(f"configured_max_tokens: {configured_max_tokens}")
    estimated_tokens = (len(document_text) + len(str(extraction_results))) / 4
    logger.info(f"Estimated tokens: {estimated_tokens}")
    if configured_max_tokens and int(configured_max_tokens) < estimated_tokens:
        return f"The max_tokens value of {configured_max_tokens} is too low for this document."
    else:
        logger.info(
            f"This document is configured with {int(configured_max_tokens)} max_tokens, "
            f" requires approximately {int(estimated_tokens)} tokens."
        )
    return None
