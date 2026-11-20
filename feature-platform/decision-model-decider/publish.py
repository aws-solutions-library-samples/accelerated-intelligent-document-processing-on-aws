#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Thin wrapper around `idp-feature-cli publish`.

Customise if your feature needs pre/post-publish steps. Otherwise, just run
`idp-feature-cli publish .` directly from this directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

from idp_feature_sdk.cli import main as cli_main

if __name__ == "__main__":
    # Rewrite argv so click sees `publish .` as the default.
    if len(sys.argv) == 1:
        sys.argv = [sys.argv[0], "publish", str(Path(__file__).resolve().parent)]
    cli_main()
