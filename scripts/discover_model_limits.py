#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Discover and verify Bedrock model max_tokens limits through empirical testing.

This script tests actual model limits by making API calls with progressively
larger max_tokens values until hitting the limit, then generates the
model_config_limits.yaml configuration file.

Usage:
    python scripts/discover_model_limits.py [--region us-east-1] [--output config_library/model_config_limits.yaml]

    # Test specific models only
    python scripts/discover_model_limits.py --models "claude-sonnet-4" "nova-lite"

    # Test all supported models (default)
    python scripts/discover_model_limits.py
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from botocore.exceptions import ClientError

# Add lib/idp_common_pkg to path
sys.path.insert(0, str(Path(__file__).parent.parent / "lib" / "idp_common_pkg"))

from idp_common.bedrock.client import BedrockClient

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# Model IDs to test (cross-region inference profile IDs preferred)
DEFAULT_MODELS_TO_TEST = [
    # Claude 4.x models - Opus 4.7 (128K output)
    "us.anthropic.claude-opus-4-7-20250514-v1:0",
    "us.anthropic.claude-opus-4-7-20250514-v1:0:1m",
    # Claude 4.x models - Opus 4.8 (128K output)
    "us.anthropic.claude-opus-4-8-20251201-v1:0",
    "us.anthropic.claude-opus-4-8-20251201-v1:0:1m",
    # Claude 4.x models - other versions (64K output)
    "us.anthropic.claude-opus-4-20250514-v1:0",
    "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    # Claude 4.x with extended context :1m
    "us.anthropic.claude-opus-4-20250514-v1:0:1m",
    "us.anthropic.claude-sonnet-4-20250514-v1:0:1m",
    # Claude 3.x models
    "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
    "us.anthropic.claude-3-haiku-20240307-v1:0",
    "us.anthropic.claude-3-opus-20240229-v1:0",
    # Amazon Nova models (1st gen)
    "us.amazon.nova-pro-v1:0",
    "us.amazon.nova-lite-v1:0",
    "us.amazon.nova-micro-v1:0",
    # Amazon Nova 2 models
    "us.amazon.nova-2-lite-v1:0",
]


def test_model_limit(
    bedrock_client: BedrockClient,
    model_id: str,
    min_tokens: int = 1000,
    max_tokens: int = 200000,
    step: int = 10000,
) -> Optional[int]:
    """
    Test a model to find its actual max_tokens limit.

    Uses binary search approach:
    1. Start with known working value
    2. Try progressively larger values until failure
    3. Binary search to find exact limit

    Args:
        bedrock_client: Bedrock client instance
        model_id: Model ID to test
        min_tokens: Minimum known working value
        max_tokens: Maximum value to test
        step: Initial step size for probing

    Returns:
        Maximum supported output tokens, or None if testing failed
    """
    logger.info(f"Testing {model_id}...")

    # Simple test prompt
    test_prompt = "Please respond with a single word: 'yes'"

    # Find upper bound by stepping up
    current = min_tokens
    last_working = min_tokens

    while current <= max_tokens:
        try:
            logger.debug(f"  Trying max_tokens={current:,}")
            # `invoke_model`, not `call_model`: the latter has never existed on
            # BedrockClient, so every run of this script died here with
            # "'BedrockClient' object has no attribute 'call_model'" — reported to
            # the operator as "Check AWS credentials and permissions" by the handler
            # below. The signature differs too: a system prompt plus Converse-format
            # `content`, not a `messages` list.
            _ = bedrock_client.invoke_model(
                model_id=model_id,
                system_prompt="Answer as briefly as possible.",
                content=[{"text": test_prompt}],
                max_tokens=current,
                temperature=0.0,
                context="ModelLimitDiscovery",
            )

            # Success - this limit works
            last_working = current
            logger.debug(f"  ✓ {current:,} tokens works")

            # Try larger value
            if current == max_tokens:
                break
            current = min(current + step, max_tokens)

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            error_msg = e.response.get("Error", {}).get("Message", "")

            if "ValidationException" in error_code and "max_tokens" in error_msg.lower():
                # Hit the limit - last_working is our answer
                logger.debug(f"  ✗ {current:,} tokens exceeds limit")
                logger.info(f"  Found limit: {last_working:,} tokens")
                return last_working
            else:
                # Different error - might be throttling, permissions, etc.
                logger.warning(f"  Error testing {model_id}: {error_code} - {error_msg}")
                return None
        except Exception as e:
            logger.error(f"  Unexpected error testing {model_id}: {e}")
            return None

    # Reached max_tokens without hitting limit
    logger.info(f"  Limit >= {last_working:,} tokens (reached test max)")
    return last_working


def generate_yaml_config(
    model_limits: Dict[str, int], output_path: Path
) -> None:
    """
    Generate model_config_limits.yaml from discovered limits.

    Groups models by family and creates regex patterns.

    Args:
        model_limits: Dict mapping model_id to max_output_tokens
        output_path: Path to write YAML file
    """
    # Group models by detected limit
    limits_map: Dict[int, List[str]] = {}
    for model_id, limit in model_limits.items():
        if limit not in limits_map:
            limits_map[limit] = []
        limits_map[limit].append(model_id)

    # Generate YAML structure
    config = {
        "model_limits": []
    }

    # Sort by limit (highest first) for better pattern matching
    for limit in sorted(limits_map.keys(), reverse=True):
        models = limits_map[limit]

        # Try to identify model family
        if any("opus-4-8" in m for m in models):
            pattern = "claude-opus-4-8"
            description = "Claude Opus 4.8 with extended 128K output"
        elif any("opus-4-7" in m for m in models):
            pattern = "claude-opus-4-7"
            description = "Claude Opus 4.7 with extended 128K output"
        elif any("claude-4" in m or "opus-4" in m or "sonnet-4" in m or "haiku-4" in m for m in models):
            pattern = "claude-(opus|sonnet|haiku)-4"
            description = "Claude 4.x models (Sonnet, Haiku, Opus 4.5/4.6) - all variants including :1m"
        elif any("claude-3" in m for m in models):
            pattern = "claude-3"
            description = "Claude 3.x models (3.5 Sonnet, 3 Opus, 3 Haiku)"
        elif any("nova" in m for m in models):
            pattern = "nova-(premier|pro|lite|micro|2)"
            description = "Amazon Nova models (Premier, Pro, Lite, Micro, Nova 2)"
        else:
            # Generic pattern from first model
            pattern = models[0].split("-")[0]
            description = f"Models with {limit:,} token limit"

        config["model_limits"].append({
            "pattern": pattern,
            "max_output_tokens": limit,
            "description": description,
            "reference": f"Empirically tested on {datetime.now().strftime('%Y-%m-%d')}",
            "tested_models": sorted(models),
        })

    # Write YAML with header
    with open(output_path, "w") as f:
        f.write("# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.\n")
        f.write("# SPDX-License-Identifier: MIT-0\n\n")
        f.write("# " + "=" * 77 + "\n")
        f.write("# Model Configuration Limits\n")
        f.write("# " + "=" * 77 + "\n")
        f.write("# This file contains technical limits and capabilities for Bedrock models.\n")
        f.write("# These limits are enforced by the Bedrock API and used for config validation.\n")
        f.write("#\n")
        f.write(f"# AUTO-GENERATED by scripts/discover_model_limits.py on {datetime.now().strftime('%Y-%m-%d')}\n")
        f.write("# Limits verified through empirical API testing.\n")
        f.write("#\n")
        f.write("# IMPORTANT: These are API limits from AWS documentation. Verify current limits at:\n")
        f.write("# - Bedrock Models: https://docs.aws.amazon.com/bedrock/latest/userguide/models-supported.html\n")
        f.write("# - Claude Models: https://docs.anthropic.com/claude/docs/models-overview\n")
        f.write("# - Nova Models: https://docs.aws.amazon.com/nova/latest/userguide/what-is-nova.html\n")
        f.write("# " + "=" * 77 + "\n\n")
        f.write("# Model limits are matched by pattern (case-insensitive regex)\n")
        f.write("# Patterns are evaluated in order - first match wins\n")
        f.write("# Each entry has a 'pattern' (regex) and 'max_output_tokens' (integer)\n\n")

        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

        f.write("\n# " + "-" * 77 + "\n")
        f.write("# Unknown Models\n")
        f.write("# " + "-" * 77 + "\n")
        f.write("# Models not matched above will cause validation to fail with an error.\n")
        f.write("# This prevents unknown models from being used with incorrect limits.\n")
        f.write("#\n")
        f.write("# To add a new model family, run this script to test its actual limits.\n")

    logger.info(f"Generated config: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Discover Bedrock model max_tokens limits through API testing"
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region to use for testing (default: us-east-1)",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=DEFAULT_MODELS_TO_TEST,
        help="Model IDs to test (default: all supported models)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("config_library/model_config_limits.yaml"),
        help="Output path for generated YAML (default: config_library/model_config_limits.yaml)",
    )
    parser.add_argument(
        "--min-tokens",
        type=int,
        default=1000,
        help="Minimum tokens to start testing (default: 1000)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=200000,
        help="Maximum tokens to test (default: 200000)",
    )
    parser.add_argument(
        "--step",
        type=int,
        default=10000,
        help="Step size for probing (default: 10000)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Initialize Bedrock client
    logger.info(f"Initializing Bedrock client in {args.region}...")
    bedrock_client = BedrockClient(region=args.region)

    # Test each model
    model_limits = {}
    for model_id in args.models:
        try:
            limit = test_model_limit(
                bedrock_client,
                model_id,
                min_tokens=args.min_tokens,
                max_tokens=args.max_tokens,
                step=args.step,
            )
            if limit:
                model_limits[model_id] = limit
        except KeyboardInterrupt:
            logger.warning("\nInterrupted by user")
            break
        except Exception as e:
            logger.error(f"Failed to test {model_id}: {e}")
            continue

    if not model_limits:
        logger.error("No model limits discovered. Check AWS credentials and permissions.")
        return 1

    # Print summary
    logger.info("\n" + "=" * 80)
    logger.info("DISCOVERED LIMITS:")
    logger.info("=" * 80)
    for model_id, limit in sorted(model_limits.items(), key=lambda x: (-x[1], x[0])):
        logger.info(f"  {model_id:<60} {limit:>10,} tokens")

    # Generate YAML config
    logger.info("\n" + "=" * 80)
    generate_yaml_config(model_limits, args.output)
    logger.info("=" * 80)
    logger.info(f"\n✅ Done! Generated {args.output}")
    logger.info(f"   Tested {len(model_limits)} models")
    logger.info(f"   Run 'git diff {args.output}' to review changes")

    return 0


if __name__ == "__main__":
    sys.exit(main())
