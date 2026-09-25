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

Untracked-but-not-ignored files are **included**, the same choice
``scripts/discover_templates.sh`` makes, and for a sharper reason than convenience:
with them excluded, a newly written gate is invisible to this scan until it is
committed, so the verdict changes at ``git add`` time. This module found that out on
itself — the constants below were untracked when it was first run, and it discovered
five of its own the moment they were committed, passing locally and failing in CI. A
gate whose answer depends on whether you have committed yet is not a gate.

**This module's own vocabulary constants are registered rather than excluded.** Making
the scanner skip itself would be the one hole nothing could see, and narrowing
:data:`NAME_VOCABULARY` or :data:`_CONTAINER` really is a coverage decision — the
latter was a live bug here, since ``frozenset({...})`` is a ``Call`` and a
literal-only check found none of the repo's four ``frozenset`` prune sets.
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
#: Note that a git pathspec's ``*`` crosses ``/``, so ``scripts/*.py`` already covers
#: every subtree under ``scripts/``. The four subtree entries this list used to spell out
#: separately were redundant; what it actually MISSED was everything outside ``scripts/``
#: and ``lib/idp_common_pkg/tests/`` -- seven live exemptions, one of them a repo-walking
#: gate that discovers ``.claude/worktrees/`` and ``scratch/``, i.e. an unregistered
#: instance of the very class this registry cites. A stated aperture that is narrower than
#: the tree is the shape of the original defect, so this now covers every first-party tree
#: that holds a gate.
PYTHON_PATHSPECS = (
    "scripts/*.py",
    "lib/*/tests/*.py",
    "patterns/*/tests/*.py",
    "nested/*/tests/*.py",
    "notebooks/*.py",
    "feature-platform/*/shared/*.py",
    "feature-platform/*/tests/*.py",
    "security/threat-modeling/scripts/*.py",
    "benchmarks/tests/*.py",
    # config_library holds a gate of its own now — test_config_library.py checks the
    # shipped presets, including that none pins a model to an account-scoped Bedrock
    # ARN. Without this glob an exclusion list added to that gate would be invisible
    # to test_gate_exemption_registry.py, which is the one place such a list is meant
    # to be impossible to add unregistered.
    "config_library/*.py",
    # The free-metering-unit declaration (#1212) is production code, not a test
    # constant: both cost implementations import it, so it has to live where a
    # Lambda can reach it. It is nonetheless a list of members that a check stops
    # applying to, which is exactly what this registry is for, so discovery has to
    # reach the one file.
    #
    # Named rather than widened to `lib/*/idp_common/*.py` on a measurement: that
    # glob finds 33 further constants in the shared library — required-key sets,
    # status enumerations, retry-code pins — none of which has ever been triaged
    # against this registry. Adding them all unregistered would fail the meta-test
    # in one direction, and registering 33 surfaces nobody has read is how a
    # reviewer learns to rubber-stamp it. The wider glob is worth doing; it is its
    # own change.
    "lib/*/idp_common/metering_units.py",
)

#: Non-Python files that carry exemptions, and the pattern that finds one in each.
#: A gate exemption is not a Python idea; it lives wherever the gate does.
#: Name fragments that mark a constant as an exemption candidate. The union of two
#: independent surveys' vocabularies -- neither alone was sufficient.
#:
#: ``EXCLU``, not ``EXCLUD``. The longer fragment matches ``EXCLUDE``/``EXCLUDED`` and
#: not ``EXCLUSION``, which is the ordinary English noun for one of these lists and the
#: spelling ``TYPECHECK_SCOPE_EXCLUSIONS`` uses -- a live carve-out from the typecheck
#: coverage gate that this scan therefore could not see, and that consequently sat
#: unregistered while every meta-test passed. One letter of aperture, and it went the
#: way a too-narrow aperture always goes here: quiet.
#:
#: **The second group names an exclusion without using either of those words.** Three
#: constants spelled ``NOT_A_PRESET``, ``NOT_AN_IDP_CONFIG`` and ``READ_ELSEWHERE`` --
#: each a live carve-out from a preset-scanning gate, each with a comment stating the
#: exclusion in plain terms -- were matched by nothing here, and became discoverable only
#: when they were renamed. Whether a carve-out is registered must not depend on whether
#: its author happened to reach for a word in this list, so the list now covers the ways
#: an exclusion gets named when the author is describing what the members *are* rather
#: than what the gate does with them. ``OPT_OUT`` found a live unregistered one on
#: arrival (a named CloudFormation condition allowed to suppress an alarm subscription);
#: the rest match nothing today and are forward-looking, which is the point of a
#: vocabulary and is why "matches something in this tree" is not the liveness rule
#: applied to them. What *is* pinned is that every fragment here demonstrably works:
#: ``test_exemption_discovery.py`` drives the real collector over a synthetic checkout
#: per fragment, on all three surfaces it has to work on, so a fragment that can never
#: match -- wrong case, a regex metacharacter that breaks :func:`_text_pattern`, a
#: polarity guard that eats it -- fails there instead of sitting quietly dead.
NAME_VOCABULARY = (
    "EXEMPT",
    "ALLOW",
    "SKIP",
    "IGNORE",
    "QUARANTINE",
    "PRUNE",
    "EXCLU",
    "KNOWN_",
    "EXPECTED_",
    "WAIV",
    "SUPPRESS",
    "_WITHOUT_",
    "PINNED_",
    "PENDING_",
    "UNCOVERED",
    # Named for what the members are, not for what the gate does with them.
    "NOT_A",
    # ``NON_`` is the same semantic family as ``NOT_A`` one prefix apart, and leaving it
    # out while adding ``NOT_A`` would have been the ``EXCLU``-not-``EXCLUD`` mistake
    # again. It is the widest fragment here -- it finds required-key sets and orderings
    # as well as carve-outs -- and that cost is accepted: the registry can record a
    # ``fixture`` in one line, and the alternative was ``NON_MODEL_CHOICES`` and
    # ``NON_SELECTABLE_DEFAULTS`` staying invisible beside a ``NOT_A`` that was added to
    # catch exactly their shape.
    "NON_",
    # ``OPEN_`` is here for the authorization case specifically:
    # ``FUNCTION_URL_OPEN_ROUTES`` turns the per-user identity check off for named
    # routes, its own comment cites ``ALLOWED_UNAUTH_METHODS`` in the same file as the
    # model it follows, and that sibling was discovered while this one was not -- purely
    # because one name happens to contain ``ALLOW``.
    "OPEN_",
    # A permitted difference is an exclusion from an equality assertion.
    "PERMIT",
    "_ELSEWHERE",
    "TOLERAT",
    "BENIGN",
    "FALSE_POSITIV",
    "BYPASS",
    "GRANDFATHER",
    "ACCEPTED",
    "DEMOT",
    "OPT_OUT",
)


#: Built from :data:`NAME_VOCABULARY` rather than a hand-written subset of it. The subset
#: spelled out here covered five of the fifteen fragments, so a Make variable named
#: ``SUPPRESSED_TEMPLATES`` or ``QUARANTINE_ROOTS`` or ``PINNED_THINGS`` was invisible while
#: the identically-named Python constant was found -- one vocabulary in two places, drifting,
#: which is the shape this whole change is about.
def _text_pattern(case: str) -> str:
    """The Make/shell variable pattern for :data:`NAME_VOCABULARY`, verbatim.

    Fragments go in **unaltered** apart from case, so these two surfaces match exactly
    what :func:`_matches_name` matches on the Python one. They used to have their
    underscores stripped, which made the text surfaces quietly BROADER: ``KNOWN_`` also
    matched ``WELL_KNOWN``, and — the case that surfaced it — ``NON_`` became a bare
    ``non``, so the shell surface reported a variable named ``canonical`` as an exemption.
    A false positive here is not harmless: the registry's remedy is to write a judgement
    for it, and a registry that asks for judgements on noise trains people to rubber-stamp
    it.
    """
    alternation = "|".join(NAME_VOCABULARY)
    if case == "upper":
        return rf"^([A-Z][A-Z0-9_]*(?:{alternation})[A-Z0-9_]*)\s*[:?+]?="
    return rf"^([a-zA-Z_][a-zA-Z0-9_]*(?:{alternation.lower()})[a-zA-Z0-9_]*)="


TEXT_SOURCES: tuple[tuple[str, str], ...] = (
    ("Makefile", _text_pattern("upper")),
    ("make/*.mk", _text_pattern("upper")),
    ("scripts/*.sh", _text_pattern("lower")),
    # Linter and type-checker configuration is an exemption surface too: `extend-exclude`
    # decides which files ruff never sees, which is a path exemption by another name.
    ("pyrightconfig.json", r'^\s*"(exclude|ignore)"\s*:'),
    ("pytest.ini", r"^(norecursedirs|ignore)\b"),
    # The per-package pytest.ini files, which is where this surface is actually
    # used. These strings become `git ls-files` pathspecs, and a bare "pytest.ini"
    # has no wildcard, so it matched only the repo-root file -- which carries no
    # norecursedirs. Four pytest.ini files are tracked; the exemption surface was
    # discoverable in one of them, and not the one holding an exemption. TOML_SOURCES
    # below already pairs `pyproject.toml` with `*/pyproject.toml` for this reason.
    ("*/pytest.ini", r"^(norecursedirs|ignore)\b"),
    (".pre-commit-config.yaml", r"^\s*(exclude|exclude_types)\s*:"),
    (".ash/.ash.yaml", r"^\s*(ignore-findings|suppressions|ignore_findings)\s*:"),
)

#: TOML files whose exemption surfaces are keys, and the keys that are one.
#:
#: Regex alone got this wrong twice, in opposite directions. ``per-file-ignores`` never
#: matched, because in TOML it is a SECTION HEADER (``[lint.per-file-ignores]``) rather
#: than an assignment -- so the four directory globs holding this repo's banned-import
#: grants were never discovered, and the registry's claim to discover the key was false.
#: And a bare ``exclude`` key appears under both ``[lint]`` and ``[format]`` with
#: different contents, so a pattern capturing the key alone collapses two surfaces into
#: one. Section-qualifying the name fixes both.
TOML_SOURCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ruff.toml", ("exclude", "extend-exclude", "per-file-ignores")),
    (
        "pyproject.toml",
        ("exclude", "extend-exclude", "per-file-ignores", "norecursedirs"),
    ),
    (
        "*/pyproject.toml",
        ("exclude", "extend-exclude", "per-file-ignores", "norecursedirs"),
    ),
)


#: JSON registries that are themselves the exemption list. Recorded by file rather
#: than by constant, because the members live in data, not in code.
#: Discovered by NAME rather than listed, for the same reason everything else here is:
#: a hardcoded three-tuple is a list that goes stale, and a fourth triage baseline added
#: under a new name would be invisible.
JSON_REGISTRY_GLOBS = (
    "*allowlist*.json",
    "*_services.json",
    "scripts/srt/issues.json",
    "*suppressions*.json",
    # A generated lint-debt or findings baseline is an exemption list by another name:
    # every line in it is a finding the gate agrees not to fail on.
    "*_debt.json",
    "*_baseline.json",
)


#: Prose in a constant's attached comment that marks it as an exclusion regardless of
#: its name. This is the half that closes the aperture: ``DEPLOYMENT_ROLE_TEMPLATES``
#: is found by "are assumed by CloudFormation ... never by stack runtime code", not
#: by anything in its name.
#:
#: **What it reads, stated rather than left to be inferred from the list.** The claim
#: that discovery finds "a constant whose comment argues for an exclusion, whatever it
#: is called" is broader than any phrase list can be, so here is the actual reach, in
#: four families:
#:
#: 1. The gate's coverage described as an absence: "does not cover", "does not scan",
#:    "not covered", "never part of", "out of scope".
#: 2. The absence marked as intentional: "deliberately not", "deliberately excludes",
#:    "deliberately absent", "skipped so". The third of these is how an exclusion reads
#:    when it is expressed as an omission from an *inclusion* list rather than as an
#:    entry on an exclusion list, and nothing else here reaches that spelling.
#: 3. The vocabulary of exclusion itself, in prose rather than in a name: "exempt",
#:    "exclusion", "excluded", "allowlist", "allow-list".
#: 4. A claim about the members that is the reason for the carve-out: "are assumed",
#:    "should never have", "may legitimately", "not real aws services", "not a defect".
#:
#: Matching is substring, against the comment lowercased, so a phrase carrying an
#: uppercase letter can never fire. That is one of the ways a phrase is silently dead,
#: and ``test_exemption_discovery.py`` pins every phrase against it by driving the real
#: collector over a synthetic comment per phrase.
#:
#: The boundary is real and worth knowing rather than guessing at: a comment reading
#: "read by a different consumer, so the gate does not flag it" is a comment arguing for
#: an exclusion and is matched by none of the above. Naming a constant in the vocabulary
#: above is the reliable route; this is the safety net, not the mechanism.
EXEMPTION_PROSE = (
    "does not cover",
    "does not scan",
    "not covered",
    "deliberately not",
    "deliberately excludes",
    # "X is deliberately absent" is how an exclusion reads when it is expressed as an
    # omission from an inclusion list rather than as an entry on an exclusion list.
    # Neither the constant's name nor any other phrase here reaches that spelling, so a
    # gate whose scope is narrowed by leaving one member out went unregistered while
    # this discovery module — which exists to catch exactly that — passed.
    "deliberately absent",
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
_CONTAINER = (
    ast.Set,
    ast.Dict,
    ast.Tuple,
    ast.List,
    ast.SetComp,
    ast.DictComp,
    ast.ListComp,
)

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
    if isinstance(node, ast.BinOp):
        # `A | B`, `A + B`: a set union or list concatenation is still a container, and
        # `NEW_EXEMPT = OLD | EXTRA` was invisible while `NEW_EXEMPT = {...}` was found.
        return _is_container(node.left) or _is_container(node.right)
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


def discover_python(root: Path | None = None) -> list[Discovered]:
    """Container constants that look like an exemption.

    ``root`` is taken as an argument so the self-tests can drive the REAL collector
    against a synthetic checkout, rather than asserting that discovery works by
    reading the code that implements it. That distinction is not academic here: the
    first version of the uncommitted-file case called the tracked-file helper directly
    and passed even with this function's own ``include_untracked`` removed.
    """
    root = root or REPO_ROOT
    found: list[Discovered] = []
    for rel in gate_premises.tracked_files(
        *PYTHON_PATHSPECS, root=root, include_untracked=True
    ):
        path = root / rel
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


def discover_text(root: Path | None = None) -> list[Discovered]:
    """Exemptions declared outside Python: Make, shell, linter configuration."""
    root = root or REPO_ROOT
    found: list[Discovered] = []
    for pathspec, pattern in TEXT_SOURCES:
        compiled = re.compile(pattern, re.M)
        for rel in gate_premises.tracked_files(
            pathspec, root=root, include_untracked=True
        ):
            try:
                source = (root / rel).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for match in compiled.finditer(source):
                name = match.group(1)
                line = source.count("\n", 0, match.start()) + 1
                found.append(Discovered(rel, name, line, "text"))
    return found


def discover_toml(root: Path | None = None) -> list[Discovered]:
    """Exemption keys in TOML configuration, qualified by their section.

    Tracks the current ``[section]`` while scanning so that a key is reported as
    ``section.key``, and so that a section header which IS an exemption surface
    (``[lint.per-file-ignores]``) is reported at all.
    """
    found: list[Discovered] = []
    for pathspec, keys in TOML_SOURCES:
        for rel in gate_premises.tracked_files(
            pathspec, root=root, include_untracked=True
        ):
            try:
                lines = (
                    ((root or REPO_ROOT) / rel).read_text(encoding="utf-8").splitlines()
                )
            except (OSError, UnicodeDecodeError):
                continue
            section = ""
            for number, raw in enumerate(lines, start=1):
                line = raw.strip()
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1].strip().strip('"')
                    leaf = section.rsplit(".", 1)[-1]
                    if leaf in keys:
                        found.append(Discovered(rel, section, number, "text"))
                    continue
                key = line.split("=", 1)[0].strip().strip('"') if "=" in line else ""
                if key in keys:
                    qualified = f"{section}.{key}" if section else key
                    found.append(Discovered(rel, qualified, number, "text"))
    return found


def discover_json(root: Path | None = None) -> list[Discovered]:
    """Data-file registries whose contents are the exemption list."""
    found = []
    for glob in JSON_REGISTRY_GLOBS:
        for rel in gate_premises.tracked_files(glob, root=root, include_untracked=True):
            found.append(Discovered(rel, Path(rel).name, 1, "json"))
    return found


def discover_all(root: Path | None = None) -> dict[str, Discovered]:
    """Every exemption surface in the tree, keyed by ``<path>::<name>``."""
    found: dict[str, Discovered] = {}
    for item in (
        discover_python(root)
        + discover_text(root)
        + discover_toml(root)
        + discover_json(root)
    ):
        found.setdefault(item.key, item)
    return found


if __name__ == "__main__":  # pragma: no cover - a convenience for authoring
    for key, item in sorted(discover_all().items()):
        print(f"{item.via:6s} {item.path}:{item.line}  {item.name}")
