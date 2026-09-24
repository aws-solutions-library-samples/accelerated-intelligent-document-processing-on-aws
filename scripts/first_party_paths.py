#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The first-party package roots of one checkout, and the PYTHONPATH that pins them.

``import idp_common`` does not read the checkout a suite lives in. These packages are
editable installs, so the import follows whichever pointer currently sits in the
active interpreter's ``site-packages``; where ``python3`` resolves to an interpreter
shared between checkouts, every ``pip install -e`` on the host rewrites that pointer
for all of them and the last writer wins. One of this repository's own gates is a
writer, so running the test gate in one checkout repoints the import for every other
one (#1094).

Pinning ``PYTHONPATH`` is what makes a run describe the tree it was started from. The
three properties it needs were each a way of getting it wrong:

* **absolute**, because a relative entry does not survive into a subprocess started
  with a different working directory, and several suites here start one;
* **all** of the roots, because the packages import each other — pinning only
  ``idp_common`` leaves ``idp_sdk`` resolving wherever it was pointing, which is what
  ``scripts/tests/conftest.py``'s provenance guard then refuses;
* **derived**, not listed. ``lib/*/pyproject.toml`` is what makes a directory an
  installable first-party root, so the rule that decides what ``FIRST_PARTY_EDITABLES``
  installs decides what is pinned, and a package added under ``lib/`` is covered
  without anyone remembering to add it here.

The same rule is expressed twice more, because the two other readers cannot import
this module: ``make/hermetic_aws.mk`` computes it in ``make`` for every pytest
invocation in both Makefiles, and ``scripts/tests/first_party_provenance.py`` computes
it with no imports at all, since ``conftest.py`` loads that file by path.
``scripts/tests/test_first_party_pythonpath.py`` asserts all three agree with each
other and with ``FIRST_PARTY_EDITABLES``, so the duplication cannot drift.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["checkout_pythonpath", "first_party_roots", "pinned_environment"]


def first_party_roots(repo_root: str | Path) -> list[Path]:
    """Every first-party package root in ``repo_root``, sorted.

    A directory under ``lib/`` holding a ``pyproject.toml`` is one. Sorted so that
    two readers of the same checkout produce byte-identical answers; the order
    between them does not otherwise matter, since the package directories they
    contain have distinct names.
    """
    root = Path(repo_root).resolve()
    return sorted(p.parent for p in root.glob("lib/*/pyproject.toml"))


def checkout_pythonpath(repo_root: str | Path) -> str:
    """The ``PYTHONPATH`` value that pins ``repo_root``'s own packages, absolute."""
    return os.pathsep.join(str(p) for p in first_party_roots(repo_root))


def pinned_environment(
    repo_root: str | Path, env: dict[str, str] | None = None
) -> dict[str, str]:
    """``env`` with this checkout's roots prepended to ``PYTHONPATH``.

    The caller's own ``PYTHONPATH`` is kept, after ours: a pin somebody set for
    another reason still applies, and the checkout under test still wins. Returns a
    copy; ``os.environ`` is not modified.
    """
    base = dict(os.environ if env is None else env)
    pin = checkout_pythonpath(repo_root)
    if not pin:
        return base
    inherited = base.get("PYTHONPATH", "")
    base["PYTHONPATH"] = f"{pin}{os.pathsep}{inherited}" if inherited else pin
    return base
