# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Stickler version tracking.

Derived from the installed distribution metadata at import time. A hand-maintained
constant here (as this module previously carried) drifted from what was actually
installed — the source of truth is the resolver, not a Python literal. The
``stickler-eval==`` pin lives in ``pyproject.toml``; a unit test asserts it
agrees with what's installed.
"""

from importlib.metadata import PackageNotFoundError, version

# Stickler repository information
STICKLER_GITHUB_REPO = "https://github.com/awslabs/stickler"

try:
    STICKLER_VERSION = version("stickler-eval")
except PackageNotFoundError:
    STICKLER_VERSION = "0.0.0+unknown"

# Installation method
STICKLER_INSTALLATION = f"stickler-eval=={STICKLER_VERSION}"


def get_stickler_version_info() -> dict:
    """Return the installed Stickler version metadata."""
    return {
        "repository": STICKLER_GITHUB_REPO,
        "version": STICKLER_VERSION,
        "installation": STICKLER_INSTALLATION,
    }


def print_stickler_version_info():
    """Print Stickler version information in a readable format."""
    info = get_stickler_version_info()
    print("=" * 80)
    print("Stickler Version Information")
    print("=" * 80)
    print(f"Repository: {info['repository']}")
    print(f"Version: {info['version']}")
    print(f"Installation: {info['installation']}")
    print("=" * 80)


if __name__ == "__main__":
    print_stickler_version_info()
