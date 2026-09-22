# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every suite that imports a first-party package asserts where it came from.

`scripts/tests/first_party_provenance.py` only protects the suites that call it. Wiring
it into a hand-written list of conftests would be a list that goes stale — a thirteenth
test root added next month would be silently uncovered, which is the same class of defect
the exemption registry exists for. So membership is **derived**: this module reads
`git ls-files`, finds every directory holding a `test_*.py` that imports a first-party
package, and fails if any of them is neither guarded nor registered below.

**Why this is not a theoretical gap.** It was measured. Before the guard was extended,
30 of 44 such directories were covered — all of them under `lib/idp_common_pkg/tests/`,
by one conftest — and 14 were not. The uncovered set included `scripts/tests`, whose
hermeticity suite had been failing for some runs and passing for others with differing
counts, which is what a shared editable pointer being rewritten by whoever last ran
`make test-cicd` predicts. Extending the guard immediately surfaced that `idp_sdk`,
`idp_cli` and `idp_feature_sdk` were all resolving to a *fourth* checkout
(`/home/ec2-user/projects/idp3`), 44 commits behind, for every suite that imported them.
No result had actually been wrong, because those three packages happened not to have
changed — luck, not safety.

**Every member is checked, not a representative.** That distinction is load-bearing: a
sibling gate's search-path assertion was defeated precisely because it derived a universe
and then inspected only the first member of it, so a foreign entry placed last was never
seen. It gave the right answer by inheritance rather than by measurement. Here the
per-directory verdict is computed for each directory.

**The guard is looked for up the conftest chain**, not only in the directory itself,
because pytest loads every `conftest.py` from the rootdir down to the collected file. One
conftest at `lib/idp_common_pkg/tests/` therefore covers the 29 directories beneath it,
and a guard at a package root covers both its `tests/` tree and any stray test module
inside its source.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Distribution-provided import names that this repository ships itself. A test importing
#: one of these is reading first-party code, and which checkout that code comes from is
#: decided by an editable-install pointer rather than by the test's location.
FIRST_PARTY = (
    "idp_common",
    "idp_sdk",
    "idp_cli",
    "idp_feature_sdk",
    "idp_mcp_connector",
)

#: The marker that makes a conftest a guard. Matching the call rather than the import,
#: because an import with no call is the inert wiring this module exists to rule out.
GUARD_CALL = "assert_resolves_in("

#: Directories that import a first-party package and deliberately carry no guard.
#:
#: Both entries rest on the same premise — **pytest never collects them**, so there is no
#: run whose result could be attributed to the wrong checkout. That premise is computable,
#: and :func:`test_every_exempt_directory_is_genuinely_uncollected` computes it rather
#: than trusting this comment: it runs a real collection from the relevant rootdir and
#: fails if any of the directory's test files show up. An entry whose files start being
#: collected therefore fails here instead of quietly losing its guard.
EXEMPT: dict[str, str] = {
    "lib/idp_common_pkg/manual_tests/agents": (
        "Operator-run scripts, not a suite: each drives real Bedrock, Athena or DynamoDB "
        "against a deployed stack and bills model calls. Excluded by "
        "lib/idp_common_pkg/pytest.ini's norecursedirs (which covers a bare `pytest` too) "
        "and registered in scripts/run_all_tests.py's QUARANTINE."
    ),
}

#: Rootdir to run a collection from, when verifying an exempt entry is uncollected.
#: A collection has to start where a real invocation would, or it proves nothing about
#: what a real invocation does.
EXEMPT_COLLECTION_ROOT = {
    "lib/idp_common_pkg/manual_tests/agents": "lib/idp_common_pkg",
}


def _tracked_python() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [Path(p) for p in out]


def first_party_test_directories() -> dict[Path, set[str]]:
    """Every directory holding a ``test_*.py`` that imports a first-party package."""
    found: dict[Path, set[str]] = {}
    for rel in _tracked_python():
        if not rel.name.startswith("test_"):
            continue
        try:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - unreadable file
            continue
        hits = {
            name
            for name in FIRST_PARTY
            if f"import {name}" in text or f"from {name}" in text
        }
        if hits:
            found.setdefault(rel.parent, set()).update(hits)
    return found


def guard_directory(directory: Path) -> Path | None:
    """The nearest ancestor conftest that calls the guard, or ``None``.

    Walks upward because pytest loads every conftest from the rootdir down to the
    collected file, so a guard above a directory protects it.
    """
    current = directory
    while True:
        conftest = REPO_ROOT / current / "conftest.py"
        if conftest.is_file() and GUARD_CALL in conftest.read_text(encoding="utf-8"):
            return current
        if current == Path("."):
            return None
        current = current.parent


@pytest.mark.unit
def test_the_universe_is_not_empty():
    """A derivation that silently finds nothing is the failure mode of a derived gate.

    If `git ls-files` changes shape, or the import-detection stops matching, every other
    assertion in this module passes vacuously. Pinned with a floor well below the current
    count so it does not need editing for ordinary growth.
    """
    directories = first_party_test_directories()
    assert len(directories) >= 30, (
        f"only {len(directories)} first-party-importing test directories were derived; "
        f"the derivation is probably broken rather than the tree having shrunk"
    )


@pytest.mark.unit
def test_every_first_party_test_directory_is_guarded_or_registered():
    """The closure. Every member is checked, not a sample of them."""
    unguarded = {
        str(directory): sorted(packages)
        for directory, packages in sorted(first_party_test_directories().items())
        if guard_directory(directory) is None and str(directory) not in EXEMPT
    }
    assert not unguarded, (
        "these test directories import a first-party package with no provenance guard "
        "reachable from any ancestor conftest, so a run there can silently exercise "
        "another checkout:\n"
        + "\n".join(f"    {d}  ({', '.join(p)})" for d, p in unguarded.items())
        + "\n\nAdd the guard stanza to a conftest.py at or above each directory (copy one "
        "from scripts/tests/conftest.py), or register it in EXEMPT with the reason and a "
        "collection root."
    )


@pytest.mark.unit
def test_no_exempt_entry_is_stale():
    """A registered exemption whose directory is gone, or which no longer needs one.

    Both directions: a path that has vanished, and a path that acquired a guard anyway --
    the second matters because a dead entry pre-exempts whatever next occupies that path.
    """
    universe = first_party_test_directories()
    for path, reason in EXEMPT.items():
        directory = Path(path)
        assert (REPO_ROOT / directory).is_dir(), (
            f"EXEMPT names {path}, which does not exist; delete the entry"
        )
        assert directory in universe, (
            f"EXEMPT names {path}, which no longer holds a test importing a first-party "
            f"package; delete the entry"
        )
        assert guard_directory(directory) is None, (
            f"{path} is now guarded, so its exemption is dead and should be deleted"
        )
        assert reason.strip(), f"EXEMPT[{path}] has no reason"
        assert path in EXEMPT_COLLECTION_ROOT, (
            f"EXEMPT[{path}] has no collection root, so its premise cannot be verified"
        )


@pytest.mark.unit
@pytest.mark.parametrize("path", sorted(EXEMPT))
def test_every_exempt_directory_is_genuinely_uncollected(path):
    """Compute the premise rather than trusting the reason written beside it.

    Both exemptions claim "pytest never collects this". That is checkable, so it is
    checked, from the rootdir a real invocation would use. If a configuration change
    starts collecting these files, this fails and the exemption has to be replaced with a
    guard.
    """
    root = REPO_ROOT / EXEMPT_COLLECTION_ROOT[path]
    result = subprocess.run(
        ["python3", "-m", "pytest", "-p", "no:cacheprovider", "-q", "--collect-only"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    collected = result.stdout + result.stderr
    test_modules = [
        rel.name
        for rel in _tracked_python()
        if rel.parent == Path(path) and rel.name.startswith("test_")
    ]
    assert test_modules, (
        f"{path} holds no test modules, so this premise is vacuous; the staleness check "
        f"should have caught that first"
    )
    offenders = [name for name in test_modules if name in collected]
    assert not offenders, (
        f"{path} is registered as never collected, but a collection from "
        f"{EXEMPT_COLLECTION_ROOT[path]} picked up {offenders}. Replace the exemption "
        f"with a guard."
    )


@pytest.mark.unit
def test_the_shared_helper_is_what_the_guards_call():
    """One mechanism, not a conftest-local reimplementation of the same check.

    A local copy would drift -- and the copies that existed before this helper each
    derived the checkout root differently, one of them by ancestry rather than identity,
    which accepted a nested worktree at a different revision.
    """
    universe = first_party_test_directories()
    guards = {guard_directory(d) for d in universe} - {None}
    assert guards, "no guard was found anywhere"
    for guard in sorted(guards):
        source = (REPO_ROOT / guard / "conftest.py").read_text(encoding="utf-8")
        assert "from first_party_provenance import assert_resolves_in" in source, (
            f"{guard}/conftest.py calls the guard without importing the shared helper, "
            f"so it is a local reimplementation"
        )
