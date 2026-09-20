# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Find every gate exemption in this repository, whatever it is spelled or written in.

This is the discovery half of the exemption registry. Its job is to make the
registry's *membership* derived, so that only the *reasons* are authored — a
hand-maintained inventory of exemptions would be the same kind of list that went
stale in the first place.

**Why discovery is deliberately broader than a name match.** Two surveys of these
lists both swept Python files for constants whose NAME carried exemption vocabulary,
and both missed real exemptions:

* ``ARN_PARTITION_EXEMPT`` lives in the **Makefile**, so a Python sweep could not
  see it at all — while reporting its mirror image, the ``/scripts/sdlc/`` entry in
  ``check_python_arn_partitions.py``, and even quoting the comment that says
  "Mirrors the /scripts/sdlc/ exclusion". Both halves of one carve-out point at each
  other; the sweep audited one and never followed the pointer. Hence
  :data:`TEXT_SOURCES`, and hence ``crossReferences`` in the registry.
* ``DEPLOYMENT_ROLE_TEMPLATES`` is exempted in the *same boolean expression* as
  ``PASS_ROLE_WILDCARD_ALLOWED``, which the vocabulary does match. One name says
  what it does and the other does not, so a name filter found the empty list and
  missed the populated one beside it. Hence :data:`EXEMPTION_PROSE` — a constant
  whose attached comment argues for an exclusion is an exclusion, whatever it is
  called.

**Discovery reads source, never a live module.** Two of these constants are mutated
at import time — ``KNOWN_UI_GAPS`` grows five entries in a module-scope loop, and
``CUSTOM_RESOURCE_ONLY`` is injected into by its own tests — so introspecting the
imported object would report a membership that no reader of the file can see, and in
one case phantom members that exist only mid-test. The registry therefore records
the *constant*, not its members; per-member premise evaluation belongs in the owning
gate, which is the only place that knows what a member means.

**Discovery goes through git** (:func:`gate_premises.tracked_files`). A gate that
walks the filesystem finds sibling worktrees and build output; that mistake produced
157 false failures in one release validation, and it is the one this module must not
repeat. ``git ls-files`` resolves relative to the checkout it runs in, so a checkout
that itself sits under a pruned directory still finds its own files.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import gate_premises

REPO_ROOT = gate_premises.REPO_ROOT

#: Python trees whose module-level constants are scanned. Every directory holding a
#: repo-wide gate, plus the four ``scripts/`` subtrees that hold one. ``scripts/srt``
#: is included for symmetry rather than because it has a hit today: leaving it out is
#: exactly the aperture that hid the Makefile half of the ARN carve-out.
PYTHON_PATHSPECS = (
    "scripts/*.py",
    "scripts/tests/*.py",
    "scripts/sdlc/*.py",
    "scripts/sdlc/tests/*.py",
    "scripts/security/*.py",
    "scripts/security/tests/*.py",
    "scripts/srt/*.py",
    "scripts/srt/tests/*.py",
    "scripts/hooks/*.py",
    "lib/idp_common_pkg/tests/**/*.py",
)

#: Non-Python files that carry exemptions, and the pattern that finds one in each.
#: A gate exemption is not a Python idea; it lives wherever the gate does.
TEXT_SOURCES: tuple[tuple[str, str], ...] = (
    # Make variables: `NAME := value` / `NAME = value`.
    ("Makefile", r"^([A-Z][A-Z0-9_]*(?:EXEMPT|IGNORE|SKIP|EXCLUDE|ALLOW)[A-Z0-9_]*)\s*[:?]?="),
    ("make/*.mk", r"^([A-Z][A-Z0-9_]*(?:EXEMPT|IGNORE|SKIP|EXCLUDE|ALLOW)[A-Z0-9_]*)\s*[:?]?="),
    # Shell arrays and variables in the gate scripts.
    ("scripts/*.sh", r"^([a-zA-Z_][a-zA-Z0-9_]*(?:exempt|ignore|skip|exclude|allow)[a-zA-Z0-9_]*)="),
    # Linter configuration is an exemption surface too: `extend-exclude` decides
    # which files ruff never sees, which is a path exemption by another name.
    ("ruff.toml", r"^(extend-exclude|per-file-ignores)\b"),
    ("pyrightconfig.json", r'^\s*"(exclude|ignore)"\s*:'),
)

#: JSON registries that are themselves the exemption list. Recorded by file rather
#: than by constant, because the members live in data, not in code.
JSON_REGISTRIES = (
    "scripts/sdlc/retired_services.json",
    "scripts/security/dep_audit_allowlist.json",
    "scripts/srt/issues.json",
)

#: Name fragments that mark a constant as an exemption candidate. The union of two
#: independent surveys' vocabularies -- neither alone was sufficient.
NAME_VOCABULARY = (
    "EXEMPT",
    "ALLOW",
    "SKIP",
    "IGNORE",
    "QUARANTINE",
    "PRUNE",
    "EXCLUD",
    "KNOWN_",
    "EXPECTED_",
    "WAIV",
    "SUPPRESS",
    "_WITHOUT_",
    "PINNED_",
    "PENDING_",
    "UNCOVERED",
)

#: Prose in a constant's attached comment that marks it as an exclusion regardless of
#: its name. This is the half that closes the aperture: ``DEPLOYMENT_ROLE_TEMPLATES``
#: is found by "are assumed by CloudFormation ... never by stack runtime code", not
#: by anything in its name.
EXEMPTION_PROSE = (
    "does not cover",
    "does not scan",
    "not covered",
    "deliberately not",
    "deliberately excludes",
    "never part of",
    "exempt",
    "exclusion",
    "excluded",
    "allowlist",
    "allow-list",
    "out of scope",
    "skipped so",
    "are assumed",
    "should never have",
    "may legitimately",
    "not real aws services",
    "not a defect",
)

#: Value node types that can hold a set of members. A plain string or a ``Path`` is a
#: pointer to an exemption rather than one, and is not matched here.
_CONTAINER = (ast.Set, ast.Dict, ast.Tuple, ast.List, ast.SetComp, ast.DictComp, ast.ListComp)

#: Callables that WRAP a container, so ``frozenset({...})`` counts. Leaving these out
#: was a live gap while this module was being written: four of the repo's prune sets
#: are spelled ``frozenset({...})``, which is an ``ast.Call``, so a literal-only check
#: silently found none of them — a discovery aperture of exactly the kind this module
#: exists to close.
_CONTAINER_CALLS = frozenset(
    {"frozenset", "set", "dict", "tuple", "list", "OrderedDict", "defaultdict"}
)


def _is_container(node: ast.expr | None) -> bool:
    if node is None:
        return False
    if isinstance(node, _CONTAINER):
        return True
    if isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        return name in _CONTAINER_CALLS
    return False


@dataclass(frozen=True)
class Discovered:
    """One exemption surface found in the tree."""

    #: Repo-relative path of the file that declares it.
    path: str
    #: Constant name, Make variable, shell variable, or config key.
    name: str
    #: 1-based line of the declaration.
    line: int
    #: How it was found: ``"name"``, ``"prose"``, ``"text"`` or ``"json"``.
    via: str

    @property
    def key(self) -> str:
        """The registry key: ``<path>::<name>``."""
        return f"{self.path}::{self.name}"


def _attached_comment(lines: list[str], index: int) -> str:
    """The contiguous comment block immediately above line ``index`` (0-based)."""
    collected = []
    i = index - 1
    while i >= 0:
        stripped = lines[i].strip()
        if stripped.startswith("#"):
            collected.append(stripped.lstrip("#").strip())
            i -= 1
            continue
        break
    return "\n".join(reversed(collected))


def _inner_comment(lines: list[str], start: int, end: int) -> str:
    """Comments *inside* the literal, where per-member reasons usually sit."""
    return "\n".join(
        line.strip().lstrip("#").strip()
        for line in lines[start:end]
        if line.strip().startswith("#")
    )


def _matches_name(name: str) -> bool:
    upper = name.upper()
    if "DISALLOW" in upper or "SWALLOW" in upper:
        # Polarity inversions: a denylist matched on "ALLOW" inside "DISALLOWED",
        # and a fixture named for a swallowed failure. Neither is an exemption.
        return False
    return any(fragment in upper for fragment in NAME_VOCABULARY)


def discover_python() -> list[Discovered]:
    """Module-level container constants that look like an exemption."""
    found: list[Discovered] = []
    for rel in gate_premises.tracked_files(*PYTHON_PATHSPECS):
        path = REPO_ROOT / rel
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=rel)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        lines = source.splitlines()
        # Module level for both name and prose matching. FUNCTION level for name
        # matching only, and only in gate IMPLEMENTATIONS rather than test modules.
        #
        # A gate exemption does not stop being one for living inside the function that
        # applies it: `ignore_services` in validate_service_role_permissions.py is a
        # local, and its premise ("not real AWS services") was false for two of its
        # three members. But a local in a *test* function is nearly always fixture data
        # -- `expected_result`, `allowed`, `skipped` -- and matching those added 20-odd
        # entries with nothing to decide about. A registry that asks for a judgement on
        # noise trains people to rubber-stamp it, which costs more than the coverage
        # gains. So the local scan is scoped, and that scoping is a declared limit
        # rather than an accident: a function-local exemption inside a test module is
        # not found, and `scripts/tests/gate_exemptions.json` records that gap.
        #
        # Prose matching is not extended inside functions at all, for the same reason.
        is_test_module = Path(rel).name.startswith("test_")
        module_level = {id(node) for node in tree.body}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = [t for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target]
            else:
                continue
            if not _is_container(node.value):
                continue
            at_module_level = id(node) in module_level
            end = node.end_lineno or node.lineno
            context = (
                _attached_comment(lines, node.lineno - 1)
                + "\n"
                + _inner_comment(lines, node.lineno, end)
            ).lower()
            for target in targets:
                if _matches_name(target.id):
                    if not at_module_level and is_test_module:
                        continue
                    via = "name" if at_module_level else "local-name"
                    found.append(Discovered(rel, target.id, node.lineno, via))
                elif at_module_level and any(p in context for p in EXEMPTION_PROSE):
                    found.append(Discovered(rel, target.id, node.lineno, "prose"))
    return found


def discover_text() -> list[Discovered]:
    """Exemptions declared outside Python: Make, shell, linter configuration."""
    found: list[Discovered] = []
    for pathspec, pattern in TEXT_SOURCES:
        compiled = re.compile(pattern, re.M)
        for rel in gate_premises.tracked_files(pathspec):
            try:
                source = (REPO_ROOT / rel).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for match in compiled.finditer(source):
                name = match.group(1)
                line = source.count("\n", 0, match.start()) + 1
                found.append(Discovered(rel, name, line, "text"))
    return found


def discover_json() -> list[Discovered]:
    """Data-file registries whose contents are the exemption list."""
    return [
        Discovered(rel, Path(rel).name, 1, "json")
        for rel in JSON_REGISTRIES
        if gate_premises.is_tracked(rel)
    ]


def discover_all() -> dict[str, Discovered]:
    """Every exemption surface in the tree, keyed by ``<path>::<name>``."""
    found: dict[str, Discovered] = {}
    for item in discover_python() + discover_text() + discover_json():
        found.setdefault(item.key, item)
    return found


if __name__ == "__main__":  # pragma: no cover - a convenience for authoring
    for key, item in sorted(discover_all().items()):
        print(f"{item.via:6s} {item.path}:{item.line}  {item.name}")
