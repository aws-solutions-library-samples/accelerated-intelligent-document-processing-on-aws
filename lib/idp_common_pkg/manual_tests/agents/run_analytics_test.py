#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Wrapper script that loads .env file and runs the analytics test.

This script provides a convenient way to run analytics tests with environment
variables loaded from a .env file, similar to how you might run tests locally
during development.

Usage:
    python manual_tests/agents/run_analytics_test.py -q "How many documents were processed today?"
    python manual_tests/agents/run_analytics_test.py -q "Show me accuracy trends" --verbose
    python manual_tests/agents/run_analytics_test.py -q "Create a chart of document types" --strands-debug
"""

import sys
from pathlib import Path


def main():
    """Main entry point that loads .env and delegates to test_analytics.main()"""

    # Try to load .env file if it exists
    try:
        from dotenv import load_dotenv

        env_file = Path(__file__).parent / ".env"
        if env_file.exists():
            load_dotenv(env_file)
            print(f"Loaded environment variables from {env_file}")
        else:
            print(f"No .env file found at {env_file}")
            print(
                "You can create one by copying .env.example to .env and updating the values"
            )
            print("Or set environment variables directly:")
            print("  export ATHENA_DATABASE='your_database_name'")
            print("  export ATHENA_OUTPUT_LOCATION='s3://your-bucket/athena-results/'")
            print("  export AWS_REGION='us-east-1'  # optional")
    except ImportError:
        print("python-dotenv not available, using system environment variables")
        print("To use .env files, install python-dotenv: pip install python-dotenv")

    # Add the idp_common_pkg root to Python path
    pkg_root = Path(__file__).parent.parent.parent
    sys.path.insert(0, str(pkg_root))

    # Import after path modification to avoid E402 linting error
    try:
        from test_analytics import (
            main as test_main,  # noqa: E402
        )

        # Delegate to the actual test script
        test_main()

    except ImportError as e:
        # test_analytics is a sibling FILE, not a package member, so it resolves
        # only when this script's own directory is on sys.path -- which the
        # interpreter arranges for `python .../run_analytics_test.py` and does not
        # for `python -m ...` or `python -P ...`. Installing the package cannot
        # help, so this message does not suggest it.
        print(f"Error importing test_analytics: {e}")
        print(
            "Run this script by path, e.g.\n"
            "  cd lib/idp_common_pkg && python manual_tests/agents/run_analytics_test.py -q '...'\n"
            "Running it with `python -m` or `python -P` leaves its own directory off "
            "sys.path, so the sibling module cannot be found."
        )
        sys.exit(1)
    except Exception as e:
        print(f"Error running analytics test: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
