# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The loader these suites share, asserted rather than assumed.

Each lambda here is deployed without a layer, so each carries its **own** copy of every
flat helper it imports by bare name. Those copies collide in ``sys.modules`` exactly the
way ``index`` does, so ``conftest._load_module`` evicts them all before importing — and
the set it evicts is derived from the tree rather than written down, which is the property
worth holding.

Why it is worth a test of its own: with the eviction set reduced to anything that omits
``log_sanitizer``, the first lambda a worker process loads leaves its copy installed under
the bare name and every lambda after it in that process runs that copy instead of its own.
Measured consequence — seven of the eight copies stop being executed, this tree's coverage
becomes a function of pytest-xdist's work distribution, and the 234 tests in this suite
**all still pass**. The coverage ratchet does catch it, loudly, by naming each copy the
report no longer mentions; but that is one gate further out and needs a measured run, and
nothing here said so.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# `tests/` is on sys.path via the root conftest, not a package -- the same bare-name
# import every sibling test module here uses (`from _helpers import ...`).
from conftest import _LAMBDAS_DIR, _SHARED_FLAT_MODULES, _load_module

#: Bare-name modules that more than one lambda directory carries a copy of.
#:
#: Derived, not listed: these are the collisions, so every one of them MUST be evicted.
#: A helper that exists in exactly one directory cannot collide and so is not required
#: here — the loader evicts it anyway, which costs nothing.
DUPLICATED_FLAT_MODULES = {
    stem
    for stem in {
        path.stem
        for path in _LAMBDAS_DIR.glob("*/*.py")
        if path.name != "index.py" and not path.name.startswith("_")
    }
    if len(list(_LAMBDAS_DIR.glob(f"*/{stem}.py"))) > 1
}


@pytest.mark.unit
def test_every_flat_helper_duplicated_across_lambdas_is_evicted():
    """The assertion, in the direction the defect went.

    ``_SHARED_FLAT_MODULES`` is a superset of this on purpose, so the comparison is
    containment rather than equality: evicting a helper only one lambda has is harmless,
    and failing on it would make adding one a gate failure for no reason.
    """
    assert DUPLICATED_FLAT_MODULES, (
        "no flat helper is duplicated across lambda directories, so this check would "
        "pass vacuously. If the duplication is genuinely gone, delete this module; do "
        "not leave a check that cannot fail."
    )
    missing = sorted(DUPLICATED_FLAT_MODULES - set(_SHARED_FLAT_MODULES))
    assert not missing, (
        f"these bare-name modules exist in more than one lambda directory and are not "
        f"evicted from sys.modules before each import: {missing}. Whichever lambda a "
        f"worker process loads first will leave its copy installed under that name, and "
        f"every lambda loaded after it in that process will run that copy instead of its "
        f"own — with every test in this suite still passing."
    )


@pytest.mark.unit
def test_each_lambda_runs_its_own_copy_of_a_duplicated_helper(aws_credentials):
    """The behaviour the set above exists to produce, observed rather than inferred.

    Loading two lambdas that both import the same bare-name helper has to leave the
    second one's module object coming from the second one's **directory**. Checking the
    eviction list alone would pass over a loader that built the list correctly and then
    did not use it.

    ``aws_credentials`` is required for the same reason every other test here takes it:
    these ``index.py`` modules construct boto3 clients at **import** time, so without a
    region in the environment the import raises. That is not incidental — the gated
    coverage producer runs each suite through ``$(HERMETIC_AWS)``, which strips exactly
    those variables, so a test that happens to work from a developer's shell fails there.
    Which is how this one was caught.
    """
    stem = next(iter(sorted(DUPLICATED_FLAT_MODULES)))
    carriers = sorted(
        path.parent
        for path in _LAMBDAS_DIR.glob(f"*/{stem}.py")
        if path.parent.is_dir()
    )
    assert len(carriers) > 1, carriers
    seen = []
    for directory in carriers[:2]:
        if not (directory / "index.py").is_file():
            continue
        _load_module(directory, f"_loader_probe_{directory.name}")
        loaded = sys.modules.get(stem)
        assert loaded is not None, (
            f"{directory.name}/index.py was imported and `{stem}` is not in sys.modules, "
            f"so this probe is measuring nothing"
        )
        seen.append(Path(loaded.__file__ or "").resolve().parent)
    assert len(seen) == 2, (
        f"needed two lambdas carrying `{stem}` and an index.py: {seen}"
    )
    assert seen[0] != seen[1], (
        f"both lambdas ran the same copy of `{stem}`, from {seen[0]} — so one of them "
        f"never executed its own file and that file's coverage is credited to the other"
    )
    assert seen == [carriers[0], carriers[1]], (
        f"each lambda must run the copy in its own directory; got {seen} for {carriers[:2]}"
    )
