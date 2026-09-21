#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Test script for the analytics agent functionality.
Similar to your local main.py but using the idp_common package.
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import boto3

# Add the idp_common_pkg root to Python path so we can import idp_common
pkg_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(pkg_root))

# Import after path modification to avoid E402 linting error
from idp_common.agents.analytics import (  # noqa: E402
    create_analytics_agent,
    get_analytics_config,
    parse_agent_response,
)
from idp_common.agents.common.config import configure_logging  # noqa: E402


def main():
    """
    Main entry point for testing the analytics agent.
    """
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="Test Analytics Agent using IDP Common Package"
    )
    parser.add_argument(
        "--question",
        "-q",
        type=str,
        help="Natural language question to convert to SQL and visualize",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging for the application",
    )
    parser.add_argument(
        "--strands-debug",
        "-s",
        action="store_true",
        help="Enable debug logging for the Strands framework",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default=None,
        help="Set the application logging level",
    )
    parser.add_argument(
        "--strands-log-level",
        type=str,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default=None,
        help="Set the Strands framework logging level",
    )
    args = parser.parse_args()

    # Determine log levels
    if args.log_level:
        log_level = getattr(logging, args.log_level)
    else:
        log_level = logging.DEBUG if args.verbose else logging.INFO

    if args.strands_log_level:
        strands_log_level = getattr(logging, args.strands_log_level)
    else:
        strands_log_level = logging.DEBUG if args.strands_debug else logging.INFO

    # Configure logging for both application and Strands
    configure_logging(log_level=log_level, strands_log_level=strands_log_level)

    logger = logging.getLogger(__name__)
    logger.info("Starting Analytics Agent test")
    logger.info(f"Application log level: {logging.getLevelName(log_level)}")
    logger.info(
        f"Strands framework log level: {logging.getLevelName(strands_log_level)}"
    )

    # Check for required environment variables
    required_env_vars = ["ATHENA_DATABASE", "ATHENA_OUTPUT_LOCATION"]
    missing_vars = [var for var in required_env_vars if not os.getenv(var)]

    if missing_vars:
        logger.error(
            f"Missing required environment variables: {', '.join(missing_vars)}"
        )
        logger.error("Please set the following environment variables:")
        logger.error("  ATHENA_DATABASE - Your Athena database name")
        logger.error(
            "  ATHENA_OUTPUT_LOCATION - S3 location for Athena query results (e.g., s3://your-bucket/athena-results/)"
        )
        logger.error("  AWS_REGION - AWS region (optional, defaults to us-east-1)")
        sys.exit(1)

    try:
        # Get analytics configuration
        logger.info("Loading analytics configuration...")
        config = get_analytics_config()
        logger.info(
            f"Configuration loaded: database={config['athena_database']}, region={config['aws_region']}"
        )

        # Create boto3 session
        logger.info("Creating boto3 session...")
        session = boto3.Session(region_name=config.get("aws_region", "us-east-1"))
        logger.info(f"Boto3 session created for region: {session.region_name}")

        # Create the analytics agent with monitoring enabled
        logger.info("Creating analytics agent...")
        # Add test job_id and user_id to enable monitoring
        test_job_id = "test-analytics-job0"
        test_user_id = "test-user"
        logger.info(
            f"Enabling monitoring with job_id={test_job_id}, user_id={test_user_id}"
        )
        agent = create_analytics_agent(
            config, session, job_id=test_job_id, user_id=test_user_id
        )
        logger.info("Analytics agent created successfully with monitoring enabled")

        # If a question was provided, process it
        if args.question:
            logger.info(f"Processing question: {args.question}")
            print("\n" + "=" * 60)
            print("PROCESSING QUERY...")
            print("=" * 60)

            try:
                response = agent(args.question)

                print("\n" + "=" * 60)
                print("AGENT RESPONSE:")
                print("=" * 60)
                print(response)
                print("=" * 60)

                # Parse the response using our new parsing function
                try:
                    parsed_response = parse_agent_response(response)
                    response_type = parsed_response.get("responseType", "unknown")
                    print(f"\nResponse Type: {response_type}")

                    if response_type == "text":
                        print(
                            f"Content: {parsed_response.get('content', 'No content')}"
                        )
                    elif response_type == "table":
                        headers = parsed_response.get("headers", [])
                        rows = parsed_response.get("rows", [])
                        print(f"Table: {len(headers)} columns, {len(rows)} rows")
                    elif response_type == "plotData":
                        plot_type = parsed_response.get("type", "unknown")
                        print(f"Plot Type: {plot_type}")

                except (json.JSONDecodeError, ValueError) as e:
                    print(f"\nWarning: Failed to parse response: {e}")

            except Exception as e:
                logger.error(f"Error processing question: {str(e)}")
                print(f"\nError: {str(e)}")
                sys.exit(1)
        else:
            print("No question provided. Use --question or -q to specify a question.")
            print(
                "Example: python manual_tests/agents/test_analytics.py -q 'How many documents have I processed each day of the last week?'"
            )
            print("\nLogging options:")
            print("  --verbose, -v                  Enable verbose application logging")
            print(
                "  --strands-debug, -s            Enable debug logging for Strands framework"
            )
            print(
                "  --log-level LEVEL              Set application log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)"
            )
            print(
                "  --strands-log-level LEVEL      Set Strands framework log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)"
            )
            print("\nRequired environment variables:")
            print("  ATHENA_DATABASE=your_database_name")
            print("  ATHENA_OUTPUT_LOCATION=s3://your-bucket/athena-results/")
            print("  AWS_REGION=us-east-1  # optional")
            print("  LOG_LEVEL=INFO  # optional, application logging level")
            print(
                "  STRANDS_LOG_LEVEL=INFO  # optional, Strands framework logging level"
            )

    except Exception as e:
        logger.error(f"Error: {str(e)}")
        sys.exit(1)

    logger.info("Analytics agent test finished")


if __name__ == "__main__":
    main()
