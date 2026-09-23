#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Verify first-party packages were installed from source, not from public PyPI.

Why this exists
---------------
Several packages in this repo depend on their siblings by bare name — e.g.
``idp_cli_pkg`` requires ``"idp-sdk"`` and ``idp_sdk`` requires ``"idp_common"``.
Those packages are first-party: they live in ``lib/`` and are NOT published to
PyPI. The names ARE registered on public PyPI by a third party.

That combination is a dependency-confusion hazard. If the packages are installed
one ``pip install`` at a time, pip resolves a sibling that is not yet installed
from public PyPI and silently installs the squatted package instead of the local
one. The failure is quiet: the import succeeds, but the module is a stub, so the
real breakage surfaces much later as a confusing, unrelated error.

How the check works
-------------------
PEP 610: pip records a ``direct_url.json`` in the ``.dist-info`` of any package
installed from a local path or a VCS URL, and records NOTHING for a package
resolved from an index (PyPI). So for these first-party names:

  * ``direct_url.json`` present, ``file://``      -> local checkout          OK
  * ``direct_url.json`` present, trusted git repo -> official source         OK
  * ``direct_url.json`` absent                    -> came from an INDEX    FAIL

This works for editable and non-editable installs alike, which a
path-based check cannot do (a non-editable local install lands in
site-packages, indistinguishable by path from a PyPI install).

Which source tree — the second question
---------------------------------------
"from source" and "from *this* checkout" are different questions, and answering only
the first is how this check stayed green while ``idp_common`` resolved into another
worktree of this repository and the other four into a different project entirely
(#1094). Both are local paths, so PEP 610 is satisfied either way.

That matters because an **editable** install's recorded path is where the import will
actually read from, every time, for as long as the pointer stands. So for editable
installs this also compares the recorded checkout against the one this script belongs
to, and fails when they differ. Non-editable installs are not compared: their code was
copied into ``site-packages`` at install time, so the recorded path says where it came
from once and nothing about what imports now.

The comparison is checkout **identity**, not ancestry: a git worktree of this
repository lives at ``<root>/.claude/worktrees/<name>/`` — a path the tooling here
creates — and is a different revision, so asking whether the recorded path is *under*
this root accepts precisely the case most likely to occur.

Run after install (``make setup`` / ``make setup-venv`` do) and in CI.
Exit codes: 0 = all good, 1 = something is missing, came from an index, or points at
another checkout.
"""

from __future__ import annotations

import json
import os
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from urllib.parse import unquote, urlparse

# Distribution names that must never be satisfied from a package index.
# Keep in sync with FIRST_PARTY_EDITABLES in the Makefile.
FIRST_PARTY = [
    "idp_common",
    "idp-sdk",
    "idp-accelerator-cli",  # console command is still `idp-cli`
    "idp_feature_sdk",
    "idp_mcp_connector",
]

# Distribution names we USED to publish under, mapped to their replacement.
# Renaming a distribution does not uninstall the old one: pip keeps the previous
# dist-info, so `pip list` shows both names pointing at the same source tree. That
# is harmless but confusing, and a stale record could later be satisfied from an
# index. Report it so the user can clean up.
RETIRED_NAMES = {
    "idp-cli": "idp-accelerator-cli",
}

# Git hosts/repos that legitimately serve this source (installs that track the
# public accelerator repo are fine — they are the same first-party code).
TRUSTED_URL_FRAGMENTS = (
    "accelerated-intelligent-document-processing-on-aws",
    "genaiic-idp-accelerator",
)

# Downgrades a foreign-checkout finding to a note. Same variable the pytest-side
# provenance guard reads (scripts/tests/first_party_provenance.py), because one switch
# for one decision is easier to reason about than two: "I am deliberately testing an
# installed copy from elsewhere". Like that one it is NOT registered in
# scripts/tests/gate_exemptions.json — it is a per-invocation switch on a local
# convention rather than a gate turned off for a named file, line or rule.
ESCAPE_HATCH = "IDP_ALLOW_FOREIGN_FIRST_PARTY"
_AFFIRMATIVE = frozenset({"1", "true", "yes", "y", "on"})


def _checkout_root(path: Path) -> Path:
    """The root of the checkout containing ``path``: the nearest ancestor with ``.git``.

    ``.git`` is a FILE in a worktree and a directory in a primary checkout, so
    existence rather than type is the test. Falls back to the path itself when there is
    no ``.git`` above it, which makes an unrelated directory compare unequal rather
    than raising.
    """
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return resolved


#: The checkout this script belongs to — what an editable pointer has to agree with.
THIS_CHECKOUT = _checkout_root(Path(__file__).resolve().parent)

#: Whether the which-tree comparison can be made at all. An exported source tree with
#: no ``.git`` — a downloaded archive, a container build context — has no checkout
#: identity to compare against, and `make setup` runs this script in exactly that
#: situation. Answering "foreign" there would be a false failure on a first install,
#: so the comparison is skipped and said to be skipped. The dependency-confusion half
#: above still applies, since it reads packaging metadata rather than paths.
CAN_COMPARE_CHECKOUTS = (THIS_CHECKOUT / ".git").exists()


def _local_path(url: str) -> Path | None:
    """The filesystem path in a ``file://`` URL, or ``None`` if it is not one."""
    if not url.startswith("file://"):
        return None
    parsed = urlparse(url)
    return Path(unquote(parsed.path))


def _direct_url(dist_name: str) -> dict | None:
    """Return the parsed PEP 610 direct_url.json, or None if absent.

    Raises PackageNotFoundError if the distribution is not installed at all.
    """
    dist = distribution(dist_name)
    try:
        raw = dist.read_text("direct_url.json")
    except Exception:  # noqa: BLE001 - treat unreadable metadata as absent
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _classify(name: str) -> tuple[str, str]:
    """Classify one distribution as "ok", "absent", or "bad", with detail.

    "absent" is deliberately NOT a failure. Installing a subset of the
    first-party packages is legitimate (CI installs only the ones whose suites it
    runs; a Lambda bundle installs one). The security property this script
    enforces is narrower and stricter: *nothing* that IS installed may have come
    from a package index.
    """
    try:
        info = _direct_url(name)
    except PackageNotFoundError:
        return "absent", "not installed (skipped)"

    if info is None:
        return "bad", (
            "installed from a package INDEX (no PEP 610 direct_url.json).\n"
            "      This name is squatted on public PyPI — it is almost certainly "
            "the wrong package."
        )

    url = info.get("url", "")

    if url.startswith("file://"):
        editable = bool(info.get("dir_info", {}).get("editable"))
        kind = "editable local" if editable else "local"
        path = _local_path(url)
        if (
            editable
            and CAN_COMPARE_CHECKOUTS
            and path is not None
            and _checkout_root(path) != THIS_CHECKOUT
        ):
            return "foreign", (
                f"editable install points at another checkout -> {path}\n"
                f"      Every `import {name.replace('-', '_')}` in this environment "
                f"reads that tree, not {THIS_CHECKOUT}."
            )
        return "ok", f"{kind} -> {url}"

    if "vcs_info" in info:
        if any(frag in url for frag in TRUSTED_URL_FRAGMENTS):
            commit = info["vcs_info"].get("commit_id", "")[:12]
            return "ok", f"git -> {url}@{commit}"
        return "bad", f"installed from an UNTRUSTED VCS URL -> {url}"

    return "bad", f"installed from an unrecognized source -> {url or '(unknown)'}"


def _escape_hatch_set() -> bool:
    return os.environ.get(ESCAPE_HATCH, "").strip().lower() in _AFFIRMATIVE


def main() -> int:
    failures: list[str] = []
    foreign: list[str] = []
    waived = _escape_hatch_set()
    checked = 0

    # Report every package first, then the error block — otherwise the stderr
    # failure text interleaves with buffered stdout and reads out of order.
    for name in FIRST_PARTY:
        status, detail = _classify(name)
        if status == "ok":
            checked += 1
            print(f"  ✓ {name}: {detail}")
        elif status == "absent":
            print(f"  - {name}: {detail}")
        elif status == "foreign":
            checked += 1
            print(f"  {'!' if waived else '✗'} {name}: see below")
            foreign.append(f"{name}: {detail}")
        else:
            checked += 1
            print(f"  ✗ {name}: see error below")
            failures.append(f"{name}: {detail}")

    # A leftover install under a retired distribution name is not a failure — the
    # code is the same — but it is stale and worth clearing.
    stale = []
    for old, new in RETIRED_NAMES.items():
        try:
            distribution(old)
        except PackageNotFoundError:
            continue
        stale.append((old, new))
        print(f"  ! {old}: retired distribution name (renamed to {new})")

    sys.stdout.flush()

    if stale:
        names = " ".join(old for old, _ in stale)
        print(
            "\nNOTE: a retired distribution name is still installed. Renaming a\n"
            "distribution does not remove the old dist-info, so pip lists both\n"
            f"names for the same source tree. Harmless, but clear it with:\n\n"
            f"  pip uninstall -y {names}\n",
            file=sys.stderr,
        )

    if foreign:
        print(
            f"\n{'NOTE' if waived else 'ERROR'}: an editable install points at another "
            "checkout.\n\n"
            "The package came from source, so the dependency-confusion question above\n"
            "is answered — but not the one that decides what your next command reads.\n"
            "An editable pointer is followed on every import, so tests, coverage and\n"
            "type checks describe the tree it names, and they do it quietly: that tree\n"
            "is a real revision of this one, so most of them still pass.\n\n"
            "Details:",
            file=sys.stderr,
        )
        for item in foreign:
            print(f"  {'!' if waived else '✗'} {item}", file=sys.stderr)
        if waived:
            print(
                f"\n{ESCAPE_HATCH} is set, so this is a note rather than a failure.",
                file=sys.stderr,
            )
        else:
            print(
                "\nTo fix, reinstall from THIS checkout, naming the interpreter you\n"
                "mean rather than whichever pip is first on PATH — a venv can be\n"
                "active by environment variable and still behind another interpreter\n"
                "on PATH, which is how one host ends up with one shared pointer:\n\n"
                "  <your-venv>/bin/python -m pip install -e "
                f'"{THIS_CHECKOUT}/lib/idp_common_pkg[all,dev,test]" ...\n'
                "  # or, all five in one pass:  make install-first-party\n\n"
                "Install by PATH, never by bare name: these distribution names on\n"
                "public PyPI belong to unrelated parties (docs/dependency-confusion.md).\n"
                f"Set {ESCAPE_HATCH}=1 if you are deliberately using a copy installed\n"
                "from somewhere else.\n",
                file=sys.stderr,
            )

    if failures:
        print(
            "\nERROR: first-party dependency check FAILED.\n\n"
            "One or more first-party packages did not come from source. The likely\n"
            "cause is dependency confusion: pip resolved a bare requirement (e.g.\n"
            "'idp-sdk' or 'idp_common') from public PyPI, where those names are\n"
            "squatted by a third party. See docs/dependency-confusion.md.\n\n"
            "Details:",
            file=sys.stderr,
        )
        for failure in failures:
            print(f"  ✗ {failure}", file=sys.stderr)
        print(
            "\nTo fix, reinstall ALL first-party packages in ONE pip invocation so\n"
            "pip resolves the sibling names from the local checkout:\n\n"
            "  pip uninstall -y idp_common idp-sdk idp-accelerator-cli "
            "idp_feature_sdk idp_mcp_connector\n"
            "  make setup        # or: make setup-venv\n",
            file=sys.stderr,
        )
        return 1

    if foreign and not waived:
        return 1

    if checked == 0:
        print(
            "\nWARNING: no first-party packages are installed — nothing to verify.",
            file=sys.stderr,
        )
        return 0

    if CAN_COMPARE_CHECKOUTS:
        print(
            f"\nAll {checked} installed first-party package(s) resolved from source "
            f"(not from an index), from {THIS_CHECKOUT}."
        )
    else:
        print(
            f"\nAll {checked} installed first-party package(s) resolved from source "
            "(not from an index). WHICH source tree was not checked: this directory is "
            "not a git checkout, so there is no identity to compare an editable "
            "pointer against."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
