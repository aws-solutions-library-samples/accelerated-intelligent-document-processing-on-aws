"""
Pytest configuration for agentic_idp extraction tests.
These tests require real strands/pyarrow packages and are automatically skipped when unavailable.

To run these tests locally:
    pytest -m agentic tests/unit/extraction/agentic_idp/
"""

import os
import sys


def _should_skip_collection(config):
    """Determine if we should skip collection based on environment and pytest marks."""
    # Check if user explicitly requested agentic tests via -m agentic
    if config.option.markexpr and "agentic" in config.option.markexpr:
        return False

    # Skip in CI
    if os.getenv("CI"):
        return True

    # Check if strands is available and not mocked
    try:
        from unittest.mock import MagicMock

        import strands

        if isinstance(strands, MagicMock):
            return True
    except ImportError:
        return True

    return False


def pytest_ignore_collect(collection_path, config):
    """Skip collection of test files in this directory if strands unavailable."""
    if _should_skip_collection(config) and "agentic_idp" in str(collection_path):
        return True
    return False


def pytest_configure(config):
    """Remove mocked modules to allow real imports."""
    config.addinivalue_line(
        "markers",
        "agentic: mark test as requiring real strands package (run with -m agentic)",
    )

    if _should_skip_collection(config):
        return

    # Only remove modules that are actually MagicMock instances
    from unittest.mock import MagicMock

    modules_to_unmock = [
        "strands",
        "strands.models",
        "strands.models.bedrock",
        "strands.types",
        "strands.types.content",
        "strands.types.media",
        "strands.hooks",
        "strands.hooks.events",
        "pyarrow",
    ]

    # Remove mocked modules
    for module_name in modules_to_unmock:
        if module_name in sys.modules and isinstance(
            sys.modules[module_name], MagicMock
        ):
            sys.modules.pop(module_name, None)

    # Remove any modules that imported the mocked modules so they get re-imported
    # fresh. PIL is only ever popped when it is itself a mock: dropping the REAL
    # `PIL.Image` from sys.modules while its plugin modules (PIL.PngImagePlugin,
    # ...) stay cached yields a fresh Image module whose plugin registry is empty
    # — `Image.open` then raises UnidentifiedImageError on a valid PNG for every
    # later test in the session (seen as tests/unit/extraction/
    # test_image_downscale_metadata.py failing only when tests/unit/assessment
    # ran first). CI never hit it because this hook returns early there.
    modules_to_reload = [
        "idp_common.extraction.agentic_idp",
        "idp_common.extraction.service",
    ]
    for module_name in ("PIL", "PIL.Image", "PIL.ImageEnhance", "PIL.ImageOps"):
        if isinstance(sys.modules.get(module_name), MagicMock):
            modules_to_reload.append(module_name)

    for module_name in modules_to_reload:
        sys.modules.pop(module_name, None)

    # Remove any modules that imported the mocked modules so they get re-imported fresh
    modules_to_reload = [
        "idp_common.extraction.agentic_idp",
        "idp_common.extraction.service",
    ]

    for module_name in modules_to_reload:
        sys.modules.pop(module_name, None)

    # Remove modules that depend on the unmocked modules so they get re-imported fresh
    for module_name in modules_to_reload:
        sys.modules.pop(module_name, None)

    # Remove modules that depend on the unmocked modules
    for module_name in modules_to_reload:
        sys.modules.pop(module_name, None)
