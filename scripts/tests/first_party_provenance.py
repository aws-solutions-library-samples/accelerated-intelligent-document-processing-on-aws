# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Assert that a first-party import resolves inside the checkout under test.

``idp_common`` and its siblings are **editable installs**, so ``import idp_common``
does not read this checkout — it reads whatever ``__editable__.idp_common-*.pth``
currently sits in the active interpreter's ``site-packages``. On a machine where
several checkouts of this repository are worked on at once, that is the difference
between a measurement and a coincidence, and nothing else in the tree reports it.

**Why one shared interpreter makes this a global race.** Where ``python3`` resolves to
a shared interpreter rather than a per-project virtualenv — which happens whenever
``.venv/bin`` is behind it on ``PATH``, even with ``VIRTUAL_ENV`` set, since that
variable alone does not change which binary runs — every checkout on the host reads
ONE ``site-packages``. Each ``pip install -e`` from any of them rewrites the pointer
for all of them, and the last writer wins.

**The test gate is the churn source.** ``lib/idp_common_pkg/Makefile``'s
``test-unit-cicd`` target runs ``pip install -e ".[test]"`` unless ``SKIP_INSTALL=1``,
so merely running the test gate repoints that pointer at the running checkout as a side
effect. Two people alternating between ``make test-cicd`` runs take it from each other,
and neither is doing anything wrong. ``SKIP_INSTALL=1`` avoids the side effect when the
dependencies are already present.

**Why this has to be an error rather than a warning.** The failure is not an
``ImportError`` that would announce itself: the imported package is real, complete and
self-consistent, just a different revision of this one. It presents either as an
ordinary assertion failure in an unrelated-looking test, or — worse — as a silent pass.
A suite run against a sibling checkout that happens to agree reports green, and a
coverage percentage measured that way describes the wrong tree while looking exactly
like a real number. That last one is why this exists. It has already cost real time in
both directions: two failures in
``feature-platform/main-stack-extensions/tests/test_apply_feature_config_preset.py``
were read as a code defect when the imported tree simply predated
``idp_common/config/hook_reachability.py``, and the graceful-degradation path for that
import meant the only signal was a log line.

**This is not a new mechanism.** ``scripts/tests/test_model_surface_consistency.py``
already asserts exactly this about the ``idp_common.config.models`` it imports, for the
same stated reason, and that assertion is what caught the condition first. This module
is that guard hoisted to somewhere other suites can call it, so the repository has one
answer rather than a growing number of local ones.

**The anchor is derived per-call, never hardcoded.** Callers pass their own
``__file__``, and the checkout root is found by walking up to the ``.git`` entry. A git
worktree therefore validates against *itself* and passes — which matters because
worktrees under ``.claude/worktrees/`` and ``/tmp`` are a normal way to work here, and a
check keyed to one canonical path would fail every one of them and train people to set
the escape hatch by default. ``.git`` is a **file** in a worktree and a directory in a
primary checkout, so existence rather than type is the test.
"""

from __future__ import annotations

import os
import warnings
from importlib import import_module
from pathlib import Path

#: Environment variable that downgrades a mismatch to a warning.
#:
#: Deliberately a plain string rather than a container: this is a per-invocation switch
#: on a local convention, in the same family as ``ALLOW_SHARED_BRANCH`` in
#: ``scripts/hooks/check_shared_branch.py``, and like that one it is NOT registered in
#: ``scripts/tests/gate_exemptions.json``. That registry governs a gate turned off for a
#: named file, line or rule, where one authored reason outlives what it described. This
#: is decided by whoever runs pytest and recorded nowhere, so an entry would be one no
#: ratchet could test and no audit could act on. What it CAN do quietly — be exported
#: once by a profile or a CI runner and then disable the check for every later command in
#: that environment — is handled where it happens, by the warning below.
#:
#: That warning is raised with ``warnings.warn`` rather than written to stderr, and the
#: difference is not cosmetic: pytest captures output at the FILE DESCRIPTOR level by
#: default, so a message written during conftest import is swallowed whether it goes to
#: ``sys.stderr``, ``sys.__stderr__`` or ``os.write(2, ...)``. All three were measured
#: producing no output at all. A warning nobody can see would leave an honoured override
#: exactly as silent as having no notice at all, which is the failure this is meant to
#: prevent; ``warnings.warn`` reaches pytest's warnings summary and survives.
ESCAPE_HATCH = "IDP_ALLOW_FOREIGN_FIRST_PARTY"

_AFFIRMATIVE = frozenset({"1", "true", "yes", "y", "on"})


class ForeignCheckoutError(RuntimeError):
    """A first-party package resolved outside the checkout under test."""


def checkout_root(anchor: str | Path) -> Path:
    """The root of the checkout containing ``anchor``.

    Walks up from ``anchor`` to the nearest ``.git``. Falls back to the filesystem
    root's child rather than raising, so a caller in an exported source tree with no
    ``.git`` degrades to "cannot determine" and is handled by :func:`assert_resolves_in`
    as a skip rather than a failure.
    """
    path = Path(anchor).resolve()
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return path.parent


def assert_resolves_in(module_name: str, anchor: str | Path) -> None:
    """Fail unless ``module_name`` resolves inside the checkout containing ``anchor``.

    ``anchor`` is the calling file's ``__file__``. Raises
    :class:`ForeignCheckoutError` on a mismatch, unless :data:`ESCAPE_HATCH` is set
    affirmatively, in which case it warns through ``warnings.warn`` and returns.

    **The comparison is checkout identity, not ancestry.** An earlier version asked
    whether the module's path was *under* ``root``, which is a different and weaker
    question: a git worktree lives at ``<root>/.claude/worktrees/<name>/`` — a path
    ``.gitignore`` reserves and that tooling here creates — so a module from a worktree
    nested inside the checkout under test satisfied ancestry and was accepted, although
    it is a different revision. That is the layout the foreign tree on the machine this
    was written on actually had, one checkout over, so the hole was in the case most
    likely to occur rather than a corner. Deriving the module's OWN checkout root and
    requiring the two to be equal answers the intended question, and reuses the walk
    above rather than adding a second rule.

    A module that cannot be imported at all is left alone: that is an environment
    problem this function has nothing useful to add to, and raising here would mask the
    real ``ImportError`` from whichever test actually needs the module.
    """
    root = checkout_root(anchor)
    if not (root / ".git").exists():
        return

    try:
        module = import_module(module_name)
    except ImportError:
        return

    source = getattr(module, "__file__", None)
    if source is None:
        # A namespace package has no single file to attribute, so there is nothing to
        # compare. Silence here is correct rather than lenient.
        return

    resolved = Path(source).resolve()
    if checkout_root(resolved) == root:
        return

    message = (
        f"{module_name} resolves OUTSIDE the checkout under test, so this run would "
        f"report on a different revision of it.\n"
        f"  checkout under test : {root}\n"
        f"  {module_name} came from : {resolved}\n"
        f"Any pass, failure or coverage figure from this run describes that tree, not "
        f"this one.\n"
        f"Fix it for this invocation:\n"
        f"    PYTHONPATH={root / 'lib' / 'idp_common_pkg'} python -m pytest ...\n"
        f"or durably, by installing with the interpreter you actually want rather than "
        f"whichever one is first on PATH:\n"
        f"    <your-venv>/bin/python -m pip install -e "
        f"'{root / 'lib' / 'idp_common_pkg'}[test]'\n"
        f"Install by PATH, never by bare name: these distribution names on public PyPI "
        f"belong to unrelated parties (docs/dependency-confusion.md).\n"
        f"Set {ESCAPE_HATCH}=1 to downgrade this to a warning if you are deliberately "
        f"testing an installed copy."
    )

    if os.environ.get(ESCAPE_HATCH, "").strip().lower() in _AFFIRMATIVE:
        warnings.warn(
            f"{ESCAPE_HATCH} is set, so the first-party provenance check was skipped. "
            f"Testing {resolved}, not a copy under {root}.",
            UserWarning,
            stacklevel=2,
        )
        return

    raise ForeignCheckoutError(message)
