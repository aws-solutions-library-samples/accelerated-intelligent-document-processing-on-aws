"""Pytest configuration for the agentic_idp extraction tests.

These tests need the **real** ``strands`` and ``pyarrow`` packages rather than the
``MagicMock`` stand-ins ``tests/conftest.py`` installs for a bare environment, so
collection here is skipped when either is genuinely unavailable.

⚠️ **The skip condition is the dependency, and nothing else.** It used to begin
``if os.getenv("CI"): return True``, which skipped these 64 cases in **both** CIs
unconditionally — GitHub Actions and GitLab each set ``CI`` — even though both jobs
install ``strands-agents`` and ``pyarrow``. So the tests covering the agentic
extraction path ran on developer machines and nowhere else, which is the inverse of
what a test suite is for: a change could break them and go green on every pull
request. It surfaced only indirectly, as the per-file coverage ratchet failing on
`extraction/agentic_idp.py`, `extraction/service.py` and `schema/__init__.py` — the
three modules these tests are what covers — on every branch at once, with no change
to any of them (#1175).

Measured, with both packages importable: ``CI`` unset collects 1314 cases under
``tests/unit/extraction`` and ``CI=true`` collects 1250, and the 64 in the
difference are all in this directory. That is why the pytest summary line cannot
show the problem — these cases are dropped during collection rather than reported as
skips, so both sides print the same skip count.

To run only these:
    pytest -m agentic tests/unit/extraction/agentic_idp/
"""

import sys

#: The packages these tests exercise for real. ``pyarrow`` was named in this file's
#: docstring and unmocked below, but never actually checked, so an environment
#: missing it got collection errors where the docstring promised a clean skip.
_REQUIRED_REAL_PACKAGES = ("strands", "pyarrow")


def _is_genuinely_importable(module_name: str) -> bool:
    """True when the module imports AND is not one of the suite's MagicMock stubs."""
    from unittest.mock import MagicMock

    try:
        module = __import__(module_name)
    except ImportError:
        return False
    return not isinstance(module, MagicMock)


def _should_skip_collection(config):
    """Skip only when a required package is absent or stubbed.

    Deliberately reads no environment variable. See the module docstring for what
    reading ``CI`` here cost.
    """
    # An explicit `-m agentic` is a request to run these, so honour it even if the
    # check below would skip: the failure a missing package produces is then the
    # answer the caller asked for rather than a silent no-op.
    if config.option.markexpr and "agentic" in config.option.markexpr:
        return False

    return not all(_is_genuinely_importable(name) for name in _REQUIRED_REAL_PACKAGES)


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

    # Drop the modules that imported the stubs, so they re-import against the real
    # packages. PIL is popped ONLY when it is itself a mock: dropping the REAL
    # `PIL.Image` from sys.modules while its plugin modules (PIL.PngImagePlugin,
    # ...) stay cached yields a fresh Image module whose plugin registry is empty
    # — `Image.open` then raises UnidentifiedImageError on a valid PNG for every
    # later test in the session (seen as tests/unit/extraction/
    # test_image_downscale_metadata.py failing only when tests/unit/assessment
    # ran first).
    #
    # ⚠️ Both CIs now reach this hook, where they used to return early from it, so
    # that hazard is no longer developer-machine-only. It stays closed by the
    # `isinstance(..., MagicMock)` test rather than by CI not getting here: CI
    # installs Pillow, so PIL is real there and is never popped. Keep that test on
    # any module added to the list below.
    modules_to_reload = [
        "idp_common.extraction.agentic_idp",
        "idp_common.extraction.service",
    ]
    for module_name in ("PIL", "PIL.Image", "PIL.ImageEnhance", "PIL.ImageOps"):
        if isinstance(sys.modules.get(module_name), MagicMock):
            modules_to_reload.append(module_name)

    # One pass. This loop was written out four times over, with the list rebuilt in
    # the middle of the run; popping an absent key is a no-op, so the repeats did
    # nothing except make the PIL guard above look conditional on something.
    for module_name in modules_to_reload:
        sys.modules.pop(module_name, None)
