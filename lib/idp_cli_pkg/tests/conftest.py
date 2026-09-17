# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Test configuration and fixtures for idp_cli tests
"""

import sys
from pathlib import Path

import pytest

# Add idp_common_pkg to Python path for testing
# This mirrors the production code's approach of dynamically adding the path
# Since idp_cli_pkg is in lib/, we go up to lib/ level and find idp_common_pkg there
project_root = Path(__file__).parent.parent.parent  # lib/
idp_common_path = project_root / "idp_common_pkg"

if idp_common_path.exists():
    sys.path.insert(0, str(idp_common_path))
else:
    raise RuntimeError(
        f"idp_common_pkg not found at expected location: {idp_common_path}"
    )


@pytest.fixture(autouse=True)
def unstyled_cli_console():
    """
    Render CLI output unstyled at a fixed width, so assertions are about CONTENT.

    Two environment-dependent renderings used to decide whether the suite passed
    (issues 714 and 905):

    - **Colour.** `CliRunner` captures into a non-tty stream, so Rich normally
      disables styling by itself and the suite passes. Set `FORCE_COLOR=1` — or
      run under a pty, as some CI runners do — and Rich highlights, so
      `assert "--config-revision 7" in output` fails on the `\\x1b[1;36m` sitting
      between the flag and its value.
    - **Width.** With no tty Rich falls back to 80 columns and ellipsizes the
      wider table columns, so a test passed standalone and failed in the suite.

    `Console(force_terminal=False)` settles the first: an explicit `False` beats
    `FORCE_COLOR` in the environment, and a non-terminal console emits no ANSI at
    all. Note `no_color=True` is NOT enough — it drops colour but keeps `bold`,
    so escapes still land mid-assertion. `width=200` settles the second; assigning
    the width overrides Rich's detection outright, which is why `COLUMNS` has no
    effect here.

    Autouse and package-wide on purpose. As a per-test fixture this was easy to
    omit from a new test, which then carried the same sensitivity back in.
    """
    from rich.console import Console

    from idp_cli import cli as cli_module

    original = cli_module.console
    cli_module.console = Console(width=200, force_terminal=False)
    try:
        yield cli_module.console
    finally:
        cli_module.console = original
