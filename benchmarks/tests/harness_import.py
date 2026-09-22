# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Import a ``benchmarks/harness`` module from a test, in a way that cannot skip silently.

Why this exists (GitHub #1079). The suites here used to reach the harness with

    sys.path.insert(0, "benchmarks/harness")      # relative to the WORKING DIRECTORY
    analyze = pytest.importorskip("analyze")

which is the same absence-versus-failure defect as the harness sites the issue is
about, in the layer that is meant to detect them. Run from anywhere but the repository
root the insert points at nothing, the import fails, ``importorskip`` reports a skip,
and the file collapses to ``1 skipped`` with a green exit — output indistinguishable
from a suite that ran. Both CIs invoke pytest from the repository root, so CI never saw
it; a developer running the file from ``benchmarks/`` or from an editor with a different
working directory did.

Two halves to the fix, and they are separate concerns:

* the path is derived from ``__file__``, so it is right from every working directory;
* the import distinguishes three outcomes rather than two. A module that imports is
  returned. A genuinely absent third-party dependency is a **skip**, which is
  deliberate — the harness's own contract is that scoring must not require the
  ``[evaluation]`` extra, and ``boto3``/``yaml`` are not always present in a bare
  checkout. Anything else — the harness file not on disk, a harness module failing to
  import, a ``SyntaxError``, a first-party package missing — is an **error**, because
  those mean the suite could not run and nobody would learn it.

Which of the last two a failure is, is decided by :func:`skippable`, from the name of
the missing module, rather than from a list of names allowed to be missing.
"""

from __future__ import annotations

import glob
import importlib
import os
import sys

import pytest

BENCHMARKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(BENCHMARKS, "harness")
REPO = os.path.dirname(BENCHMARKS)


def first_party_import_names() -> frozenset[str]:
    """Import names this repository ships, read off the tree rather than listed.

    A missing one is a fact about the tree and must be an error, never a skip. That
    makes an authored list the wrong shape: it would err **permissive** the moment a
    sixth package is added — the new name would be unrecognised, and unrecognised is
    what becomes a skip. Deriving it means a package cannot be added without being
    covered, which is the property the rest of this change is about.

    Every one of them is a ``lib/<distribution>/<import name>/`` package, so the
    import name is the directory holding an ``__init__.py`` one level under ``lib/``.
    Test packages match that shape too and are harmless here: the effect of
    membership is to report an ERROR rather than a skip, so a name wrongly included
    fails loudly and a name wrongly omitted is the direction that hides.
    """
    return frozenset(
        os.path.basename(os.path.dirname(path))
        for path in glob.glob(os.path.join(REPO, "lib", "*", "*", "__init__.py"))
    )


def harness_path() -> str:
    """The absolute path to ``benchmarks/harness``, and put it on ``sys.path``."""
    if HARNESS not in sys.path:
        sys.path.insert(0, HARNESS)
    return HARNESS


def skippable(missing: str | None) -> bool:
    """May a failed import of ``missing`` be reported as a skip?

    Decided by a rule rather than by a list of blessed package names, deliberately.
    A list would be a registered carve-out that goes stale in both directions — a
    member nothing imports any more silently pre-exempts whatever next takes that
    name, and a genuinely optional dependency added tomorrow errors until somebody
    edits the list. The two cases that must never be skipped are properties of the
    name itself, so they are computed:

    * a module of this harness — the suites exist to exercise those, so one that will
      not import is the finding, not a reason to stop looking;
    * a package this repository ships, per :func:`first_party_import_names`.
      ``idp_common`` in particular: the confidence and coverage measurements are
      defined as calls into the shipped rule rather than a lookalike, so skipping for
      it would retire the very comparison that makes them worth anything.

    Anything else is a statement about the environment — a bare checkout without
    ``boto3`` or ``yaml`` — and the harness's own contract is that scoring must not
    require the ``[evaluation]`` extra, so a skip there is intended. The skip names
    the module, so a reader can tell which case they are in.
    """
    if not missing:
        return False
    root = missing.split(".")[0]
    if root in first_party_import_names():
        return False
    return not os.path.exists(os.path.join(HARNESS, f"{root}.py"))


def harness_module(name: str):
    """Import ``benchmarks/harness/<name>.py``. Skips only for a missing optional dep."""
    harness_path()
    source = os.path.join(HARNESS, f"{name}.py")
    if not os.path.exists(source):
        # Never a skip. Either the module was renamed or this file is not where it
        # thinks it is, and both are findings.
        raise RuntimeError(
            f"benchmarks/harness/{name}.py does not exist (looked in {HARNESS}). "
            "This is a broken test bootstrap, not a reason to skip."
        )
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        missing = getattr(exc, "name", None)
        if skippable(missing):
            pytest.skip(
                f"{name} needs {missing}, which is not installed here",
                allow_module_level=True,
            )
        raise RuntimeError(
            f"benchmarks/harness/{name}.py is present but would not import "
            f"({type(exc).__name__}: {exc}). Skipping this would hide it."
        ) from exc
