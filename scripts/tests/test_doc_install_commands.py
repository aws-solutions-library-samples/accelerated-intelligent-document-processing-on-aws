# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No documented ``pip install`` may resolve a first-party name from an index.

The hazard
----------
This repository ships first-party Python packages under ``lib/`` that are **not**
published to PyPI, and that require each other by bare name — ``lib/idp_cli_pkg``
requires ``"idp-sdk"``, ``lib/idp_sdk`` requires ``"idp_common"``. Both of those
names on public PyPI are registered by unrelated third parties; that is the
repository's own assertion, recorded in the ``# SECURITY:`` comment beside each bare
requirement in ``lib/idp_cli_pkg/pyproject.toml`` and ``lib/idp_sdk/pyproject.toml``,
and it is why ``docs/dependency-confusion.md`` exists.

Two shapes of documented command therefore hand a reader somebody else's code:

* a **bare name** — ``pip install "idp_common[core]"`` resolves the name from the
  configured index, full stop;
* a **path install that omits a sibling** — ``pip install -e lib/idp_sdk`` installs
  the local SDK, whose own ``idp_common`` requirement pip then satisfies from the
  index because no local copy is on the command line and none is installed yet.

The second is the one that reads as safe. It follows the "always use a path" rule
and still fails, which is exactly how ``cd lib/idp_cli_pkg && pip install -e .``
survived in the project README's Quick Start and in the deployment guide.

Why the runtime checker cannot cover this
-----------------------------------------
``scripts/check_first_party_deps.py`` is the sibling control and it runs in both CI
systems, but it inspects PEP 610 ``direct_url.json`` in an **already-installed
environment**. CI installs correctly and then asks it, so it can only ever confirm
CI's own install. It is structurally incapable of reading a command out of a
Markdown file, and a reader following Quick Start never runs it. That gap is the
whole reason for this module.

Design
------
**The package model is derived, never listed.** Every tracked ``pyproject.toml`` is
parsed for its distribution name, its declared import names, and which of its
requirements — in ``dependencies`` and in *every* extra — name another package in
this repository. A new first-party package with a bare sibling requirement is
covered the moment its ``pyproject.toml`` lands; nothing here needs editing. A
hardcoded name list is the defect this repository keeps rediscovering: a control
that exists as an artifact but is never consulted where the decision is made.
``scripts/check_first_party_deps.py``'s ``FIRST_PARTY`` list is such an artifact —
it carries a "keep in sync with the Makefile" comment — and this module deliberately
does not read it, so the two agree by derivation or not at all.

**The universe comes from git**, via ``repo_files.tracked_paths``, and covers both
tracked Markdown and tracked Jupyter notebooks. A filesystem walk finds gitignored
build output and sibling agent worktrees, each of which holds a whole copy of this
repository; see ``repo_files.py`` for the failure that convention exists to prevent.
Notebooks are in because ``notebooks/`` carries a dozen live ``%pip install -e
"{ROOTDIR}/lib/idp_common_pkg[...]"`` cells, and a notebook is exactly where someone
would write ``%pip install idp_common`` instead. (``ruff.toml`` excludes
``**/*.ipynb``, but that is a decision about linting the Python *inside* a notebook
and has no bearing here: this gate reads the notebook's JSON, not its code.)

**Scope: copyable commands.** In Markdown that means a fenced code block; in a
notebook, a code cell — which *is* the command context, so no fence is looked for —
plus any fence inside a markdown cell. What is deliberately outside scope:

* **Inline backticks in prose.** The same text in a sentence is the document
  *naming* a command in order to discuss it, which is how the bare-name hazard is
  explained in the first place — in this repository's security page, in
  ``CONTRIBUTING.md`` and in ``lib/idp_common_pkg/README.md``. Treating those as
  instructions would make the warnings unwritable.
* **A command that is not in a command position** on its line. See the comment on
  :data:`_PIP_INSTALL` for the exact shapes that costs — a parenthesised subshell, a
  ``sudo`` carrying its own options, a command assembled by a loop or held in a
  variable — and why each was judged not worth handling. The prefixes that *are*
  handled are the ones a reader of these docs would plausibly meet: a transcript
  prompt, a notebook magic, an environment-variable assignment, and a dotted-minor
  interpreter.
* **Installers other than pip.** ``uv add``, ``poetry add`` and ``pipx`` resolve
  names the same way and are not matched. Nothing in this tree uses them; ``uv pip``,
  which it does use, is matched.
* **A commented-out line**, e.g. ``# %pip install ...`` in a notebook cell. That is
  not an instruction, and several notebooks carry one.

Those are the residuals, and they are the whole list. Nothing is excluded by path:
there is no per-file, per-directory or per-line exemption here, and so no gate
exemption surface to register in ``gate_exemptions.json``.

**Resolution of a bare ``.``**, as in ``pip install -e ".[test]"``, follows what a
reader's shell would do: the nearest preceding ``cd`` in the **same** fenced block or
code cell, and failing that the package directory the document itself lives in. A
``cd`` deliberately does not cross a block boundary, because a reader who copies one
fence does not inherit a working directory another fence set. An unresolvable ``.``
names no package and yields no finding, which is the conservative direction.
"""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import pytest
from repo_files import tracked_paths

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Options whose *next* token is a value rather than a requirement. ``-e`` is
#: deliberately absent: its argument IS a requirement, and dropping it was how an
#: early draft of this gate passed over every editable install in the tree.
_VALUE_OPTIONS = frozenset(
    {
        "-r",
        "--requirement",
        "-c",
        "--constraint",
        "-i",
        "--index-url",
        "--extra-index-url",
        "-f",
        "--find-links",
        "-t",
        "--target",
        "--prefix",
        "--root",
        "--src",
        "--cache-dir",
        "--log",
        "--proxy",
        "--python",
        "--python-version",
        "--platform",
        "--implementation",
        "--abi",
        "--report",
        "--config-settings",
        "-C",
        "--upgrade-strategy",
        "--progress-bar",
        "--timeout",
        "--retries",
    }
)

_EDITABLE_OPTIONS = frozenset({"-e", "--editable"})

#: ``pip install`` in a command position: the start of the logical line, or after a
#: shell separator. Tolerates the shapes this repository's docs and notebooks
#: actually use — a ``$`` transcript prompt, a notebook ``%``/``!`` magic, an
#: environment-variable prefix (``PIP_INDEX_URL=... pip install``), ``sudo``, a
#: dotted-minor interpreter (``python3.12 -m pip``, and 3.12 is what this repo
#: pins), ``uv pip``, and a ``cd X && pip install`` chain.
#:
#: A command position is required rather than a bare substring search because a
#: fenced block is not always a script. Directory-tree listings and annotated file
#: inventories are fenced too, and one of them describes a container build as
#: "(pip install seed-data + idp_common)" — an English parenthetical, not an
#: instruction, and the only false positive this gate produced on the tree.
#:
#: What that costs, so the residual is stated where it is created rather than only
#: in the module docstring: a parenthesised subshell ``(pip install ...)``, a
#: ``sudo`` carrying its own options (``sudo -H pip install``), and a command
#: assembled at runtime (a shell loop, or a name held in a variable) are not
#: matched. Nor are non-pip installers — ``uv add``, ``poetry add``, ``pipx``. Each
#: is a shape nothing in this tree writes, and each would have to be paid for either
#: in prose false positives or in speculative machinery; see the docstring.
_PIP_INSTALL = re.compile(
    r"""(?:^|\||&&|;)\s*
        (?:\$\s+|[!%]\s*)?
        (?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*
        (?:sudo\s+)?
        (?:(?:py|python(?:3(?:\.\d+)?)?)\s+-m\s+)?
        (?:uv\s+)?
        pip3?\s+install(?![\w-])""",
    re.VERBOSE,
)

#: Indentation is deliberately unbounded. CommonMark allows a fence to be indented
#: up to three spaces *relative to its containing block*, so a fence inside a
#: second-level list item legitimately sits at five or six columns — which several
#: documents here do, and which an anchor measuring from column zero does not see at
#: all. Over-scanning is the safe direction for this gate: the worst case is reading
#: an indented literal block as a fence, and a ``pip install`` in one of those is
#: still a command a reader copies.
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_CD = re.compile(r"(?:^|\||&&|;)\s*cd\s+([^\s&|;]+)")
_EXTRAS = re.compile(r"\[[^\]]*\]\s*$")
_VERSION_SPEC = re.compile(r"[<>=!~;@\s].*$")
_REQUIREMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def normalise(name: str) -> str:
    """PEP 503 normalisation, so ``idp_common`` and ``idp-common`` are one name."""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


@dataclass(frozen=True)
class Package:
    """A first-party package, as read out of its ``pyproject.toml``."""

    directory: str
    """Repo-relative POSIX path, e.g. ``lib/idp_sdk``."""

    names: frozenset[str]
    """Normalised names a requirement could refer to it by: the distribution name
    plus any import packages the build backend is told to ship."""

    requires: frozenset[str]
    """Normalised names of OTHER first-party packages this one requires by bare
    name, across ``dependencies`` and every extra."""

    @property
    def basename(self) -> str:
        return self.directory.rsplit("/", 1)[-1]


def _requirement_name(spec: str) -> str:
    """The distribution name at the head of a requirement specifier."""
    head = spec.split(";", 1)[0].strip()
    match = _REQUIREMENT_NAME.match(head)
    return normalise(match.group(0)) if match else ""


def _declared_import_names(tool: dict) -> set[str]:
    """Top-level import package names a ``[tool.setuptools]`` table names.

    ``packages`` is either a list of import names or a ``find`` table; only the
    former names anything, and an ``include`` glob under ``find`` names a prefix.
    Import names matter because a document can say ``pip install idp_cli`` — the
    import name — for a distribution called ``idp-accelerator-cli``.
    """
    packages = (tool.get("setuptools") or {}).get("packages")
    found: set[str] = set()
    if isinstance(packages, list):
        found |= {normalise(str(p).split(".", 1)[0]) for p in packages}
    elif isinstance(packages, dict):
        include = (packages.get("find") or {}).get("include") or []
        found |= {normalise(str(p).rstrip("*").split(".", 1)[0]) for p in include}
    return {name for name in found if name}


def discover_packages(root: Path = REPO_ROOT) -> tuple[Package, ...]:
    """Every first-party package in ``root``, derived from its ``pyproject.toml``.

    Includes ``scripts/pypi-placeholders/*`` on purpose. Those declare the same
    distribution names as their real counterparts and no dependencies, so they add
    an alias and nothing else — and including them keeps this function free of any
    path-shaped carve-out, which is the kind of thing that later turns out to have
    hidden the one member that mattered.
    """
    raw: list[tuple[str, set[str], list[str]]] = []
    for path in tracked_paths(root, "*pyproject.toml"):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):  # pragma: no cover
            continue
        project = data.get("project") or {}
        dist = project.get("name")
        if not isinstance(dist, str) or not dist:
            continue
        names = {normalise(dist)} | _declared_import_names(data.get("tool") or {})
        specs: list[str] = list(project.get("dependencies") or [])
        for extra in (project.get("optional-dependencies") or {}).values():
            specs.extend(extra)
        raw.append((path.parent.relative_to(root).as_posix(), names, specs))

    all_names = {name for _, names, _ in raw for name in names}
    return tuple(
        sorted(
            (
                Package(
                    directory=directory,
                    names=frozenset(names),
                    # A self-referencing extra (``idp_common[agentic-extraction]``)
                    # is resolved against the package being installed, not the
                    # index, so it is not a sibling requirement.
                    requires=frozenset(
                        n
                        for spec in specs
                        if (n := _requirement_name(spec)) in all_names
                        and n not in names
                    ),
                )
                for directory, names, specs in raw
            ),
            key=lambda pkg: pkg.directory,
        )
    )


class PackageIndex:
    """Lookups over a set of :class:`Package` objects."""

    def __init__(self, packages: tuple[Package, ...]) -> None:
        self.packages = packages
        self.by_name: dict[str, list[Package]] = {}
        self.by_basename: dict[str, Package] = {}
        for pkg in packages:
            for name in pkg.names:
                self.by_name.setdefault(name, []).append(pkg)
            self.by_basename.setdefault(pkg.basename, pkg)

    def required_names(self, pkg: Package) -> frozenset[str]:
        """``pkg``'s first-party requirements, transitively.

        The CLI requires the SDK, which requires ``idp_common``; installing the CLI
        with only the SDK beside it still sends pip to the index for the third.
        """
        seen: set[str] = set()
        pending = list(pkg.requires)
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            for provider in self.by_name.get(name, []):
                pending.extend(provider.requires - seen)
        return frozenset(seen)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    command: str
    problem: str
    cell: int | None = None

    def __str__(self) -> str:
        where = (
            f"{self.path}:{self.line}"
            if self.cell is None
            else f"{self.path} cell {self.cell} line {self.line}"
        )
        return f"{where}: {self.problem}\n    {self.command}"


class CommandLine(NamedTuple):
    """One command a reader could copy, with the block it came from.

    ``block`` identifies the enclosing fenced code block or notebook code cell, and
    is unique within a document. A ``cd`` only applies to a ``.`` in the *same*
    block: a reader who copies one fence does not inherit the working directory
    another fence set.
    """

    number: int
    text: str
    block: int
    cell: int | None = None


def _join_continuations(
    numbered: list[tuple[int, str]], block: int, cell: int | None
) -> list[CommandLine]:
    """Fold backslash continuations, so a wrapped command is one command."""
    out: list[CommandLine] = []
    pending_no: int | None = None
    pending: list[str] = []
    for number, line in numbered:
        stripped = line.rstrip()
        if pending_no is None:
            pending_no = number
        if stripped.endswith("\\"):
            pending.append(stripped[:-1].strip())
            continue
        pending.append(stripped.strip())
        out.append(
            CommandLine(pending_no, " ".join(p for p in pending if p), block, cell)
        )
        pending, pending_no = [], None
    if pending and pending_no is not None:
        out.append(CommandLine(pending_no, " ".join(pending), block, cell))
    return out


def _logical_lines(
    text: str, first_block: int = 0, cell: int | None = None
) -> list[CommandLine]:
    """Every command line inside a fenced code block of ``text``."""
    out: list[CommandLine] = []
    fence: str | None = None
    block = first_block
    numbered: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        marker = _FENCE.match(line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token[0] * 3
                numbered = []
            elif token.startswith(fence):
                fence = None
                out.extend(_join_continuations(numbered, block, cell))
                numbered = []
                block += 1
            continue
        if fence is not None:
            numbered.append((number, line))
    out.extend(_join_continuations(numbered, block, cell))
    return out


def _notebook_lines(text: str) -> list[CommandLine]:
    """Every command line in a Jupyter notebook: code cells, and markdown fences.

    A code cell **is** the command context — there is no fence to look for — so its
    source lines are commands directly, which is what makes ``%pip install``
    reachable. Markdown cells go through the fence scanner like any document.

    Line numbers are cell-relative and reported as ``cell N line M``, because a
    notebook's physical line numbers are an artifact of JSON formatting and would
    not help anyone find the cell.
    """
    try:
        notebook = json.loads(text)
    except json.JSONDecodeError:  # pragma: no cover - malformed notebook
        return []
    out: list[CommandLine] = []
    next_block = 0
    for position, cell in enumerate(notebook.get("cells") or [], start=1):
        if not isinstance(cell, dict):  # pragma: no cover
            continue
        source = cell.get("source")
        if isinstance(source, list):
            source = "".join(str(part) for part in source)
        if not isinstance(source, str):  # pragma: no cover
            continue
        kind = cell.get("cell_type")
        if kind == "code":
            numbered = list(enumerate(source.splitlines(), start=1))
            produced = _join_continuations(numbered, next_block, position)
        elif kind == "markdown":
            produced = _logical_lines(source, first_block=next_block, cell=position)
        else:  # pragma: no cover - raw cells hold no commands
            continue
        out.extend(produced)
        # Blocks stay unique across cells, so a ``cd`` cannot leak between them.
        next_block = max((line.block for line in produced), default=next_block) + 1
    return out


def _split_tokens(argument_text: str) -> list[str]:
    try:
        return shlex.split(argument_text, comments=True)
    except ValueError:
        return argument_text.split("#", 1)[0].split()


def _requirement_tokens(argument_text: str) -> tuple[list[str], bool]:
    """Requirement tokens on a ``pip install``, and whether ``--no-deps`` is set."""
    tokens = _split_tokens(argument_text)
    requirements: list[str] = []
    no_deps = False
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token in ("--no-deps", "--no-dependencies"):
            no_deps = True
            continue
        if token in _VALUE_OPTIONS:
            skip_next = True
            continue
        if token in _EDITABLE_OPTIONS:
            continue
        if token.startswith("-"):
            continue
        requirements.append(token)
    return requirements, no_deps


def _looks_like_path(base: str) -> bool:
    return (
        base in (".", "..")
        or "/" in base
        or base.startswith(("./", "../", "~"))
        or base.endswith((".whl", ".tar.gz", ".zip"))
    )


def _cd_target(text: str) -> str | None:
    matches = _CD.findall(text)
    return matches[-1] if matches else None


def _resolve_dot(
    index: PackageIndex,
    doc_path: str,
    lines: list[CommandLine],
    position: int,
    prefix: str,
) -> Package | None:
    """What ``.`` means: the nearest preceding ``cd`` **in the same block**, else the
    package directory the document itself lives in."""
    block = lines[position].block
    earlier = [
        lines[i].text for i in range(position - 1, -1, -1) if lines[i].block == block
    ]
    for text in [prefix] + earlier:
        target = _cd_target(text)
        if target is None:
            continue
        return index.by_basename.get(Path(target.strip("\"'")).name)
    for pkg in index.packages:
        if doc_path == pkg.directory or doc_path.startswith(pkg.directory + "/"):
            return pkg
    return None


def scan_lines(
    lines: list[CommandLine], doc_path: str, index: PackageIndex
) -> list[Finding]:
    """Findings for a document already reduced to its copyable command lines."""
    findings: list[Finding] = []
    for position, line in enumerate(lines):
        command = line.text
        for match in _PIP_INSTALL.finditer(command):
            argument_text = re.split(
                r"&&|\|\||[|;]", command[match.end() :], maxsplit=1
            )[0]
            requirements, no_deps = _requirement_tokens(argument_text)
            local: list[Package] = []
            for token in requirements:
                base = _EXTRAS.sub("", token.strip("\"'")).strip()
                if _looks_like_path(base):
                    if base in (".", ".."):
                        pkg = _resolve_dot(
                            index, doc_path, lines, position, command[: match.start()]
                        )
                    else:
                        pkg = index.by_basename.get(Path(base).name)
                    if pkg is not None:
                        local.append(pkg)
                    continue
                name = _requirement_name(_VERSION_SPEC.sub("", base))
                if name in index.by_name:
                    findings.append(
                        Finding(
                            doc_path,
                            line.number,
                            command.strip(),
                            f"installs the first-party package {name!r} by bare "
                            f"name, which resolves from the configured index; use "
                            f"a path under lib/ instead",
                            line.cell,
                        )
                    )
            if no_deps or not local:
                continue
            provided = {name for pkg in local for name in pkg.names}
            for pkg in local:
                missing = sorted(index.required_names(pkg) - provided)
                if missing:
                    findings.append(
                        Finding(
                            doc_path,
                            line.number,
                            command.strip(),
                            f"installs {pkg.directory} from a path but not "
                            f"{', '.join(missing)}, which it requires by name; pip "
                            f"resolves the missing sibling(s) from the index. Put "
                            f"every first-party package on the same command line, "
                            f"or use make setup",
                            line.cell,
                        )
                    )
    return findings


def scan_markdown(text: str, doc_path: str, index: PackageIndex) -> list[Finding]:
    """Findings for one Markdown document."""
    return scan_lines(_logical_lines(text), doc_path, index)


def scan_notebook(text: str, doc_path: str, index: PackageIndex) -> list[Finding]:
    """Findings for one Jupyter notebook."""
    return scan_lines(_notebook_lines(text), doc_path, index)


def sweep(root: Path = REPO_ROOT) -> list[Finding]:
    """Every finding across every tracked Markdown file and notebook under ``root``."""
    index = PackageIndex(discover_packages(root))
    findings: list[Finding] = []
    for path in tracked_paths(root, "*.md", "*.ipynb"):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover
            continue
        scan = scan_notebook if path.suffix == ".ipynb" else scan_markdown
        findings.extend(scan(text, path.relative_to(root).as_posix(), index))
    return findings


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_no_documented_install_can_resolve_a_first_party_name_from_an_index() -> None:
    """Every copyable ``pip install`` in the tree must be index-proof."""
    findings = sweep()
    assert not findings, (
        "documented pip install command(s) can resolve a first-party name from a "
        "package index (dependency confusion — see docs/dependency-confusion.md):"
        "\n\n" + "\n".join(str(f) for f in findings)
    )


@pytest.mark.unit
def test_the_sweep_reads_a_nonempty_universe() -> None:
    """A sweep that finds nothing to check passes vacuously.

    Every half is asserted, because any one of them going to zero makes the gate
    above meaningless while leaving it green: the package model, the Markdown corpus,
    the notebook corpus, and — the one a walk-vs-git or fence-anchor change would
    break silently — the count of commands actually reduced out of each corpus.
    """
    packages = discover_packages()
    assert len(packages) >= 5, f"first-party package discovery found {packages}"
    assert any(pkg.requires for pkg in packages), (
        "no package was found to require a sibling by bare name; either the "
        "pyproject requirements changed or the derivation is broken"
    )

    markdown = tracked_paths(REPO_ROOT, "*.md")
    notebooks = tracked_paths(REPO_ROOT, "*.ipynb")
    assert len(markdown) > 50
    assert len(notebooks) > 10

    index = PackageIndex(packages)
    md_installs = sum(
        1
        for path in markdown
        for line in _logical_lines(path.read_text(encoding="utf-8"))
        if _PIP_INSTALL.search(line.text)
    )
    nb_installs = sum(
        1
        for path in notebooks
        for line in _notebook_lines(path.read_text(encoding="utf-8"))
        if _PIP_INSTALL.search(line.text)
    )
    assert md_installs > 50, f"only {md_installs} pip installs found in Markdown"
    assert nb_installs > 5, f"only {nb_installs} pip installs found in notebooks"
    assert index.by_name, "the name index is empty, so nothing can ever be flagged"


@pytest.mark.unit
def test_sibling_requirements_are_derived_from_pyproject() -> None:
    """The derivation reproduces the two bare requirements the repo documents.

    Asserted as a property of the model rather than of a name list: if
    ``lib/idp_sdk`` stops requiring ``idp_common`` by name this test is what says
    so, and if a third package starts requiring a sibling the gate covers it with
    no edit here.
    """
    index = PackageIndex(discover_packages())
    by_dir = {pkg.directory: pkg for pkg in index.packages}

    sdk = by_dir["lib/idp_sdk"]
    cli = by_dir["lib/idp_cli_pkg"]
    common = by_dir["lib/idp_common_pkg"]

    assert "idp-common" in sdk.requires
    assert "idp-sdk" in cli.requires
    # Transitively, so installing the CLI beside only the SDK is still unsafe.
    assert {"idp-sdk", "idp-common"} <= index.required_names(cli)
    # idp_common stands alone; its only self-named extra is not a sibling.
    assert common.requires == frozenset()


# --------------------------------------------------------------------------- #
# Can it fail? -- the detector is exercised against a synthetic tree in tmp_path.
# Never inside the repository: a repo-internal probe is found by the sweep above
# under `pytest -n auto`, and leaks on a hard kill. Same reasoning as
# test_lambda_log_groups.py::test_discovery_actually_finds_an_unlisted_template.
# --------------------------------------------------------------------------- #


def _probe_tree(root: Path, markdown: str) -> Path:
    """Two first-party packages where the second requires the first by name."""
    (root / "lib" / "probe_common_pkg").mkdir(parents=True)
    (root / "lib" / "probe_common_pkg" / "pyproject.toml").write_text(
        '[project]\nname = "probe_common"\ndependencies = ["boto3>=1.0"]\n'
    )
    (root / "lib" / "probe_sdk").mkdir(parents=True)
    (root / "lib" / "probe_sdk" / "pyproject.toml").write_text(
        '[project]\nname = "probe-sdk"\ndependencies = ["probe_common", "click"]\n'
        '[tool.setuptools]\npackages = ["probe_sdk"]\n'
    )
    doc = root / "docs" / "guide.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(markdown)
    return doc


def _probe_findings(root: Path, markdown: str) -> list[Finding]:
    _probe_tree(root, markdown)
    return sweep(root)


@pytest.mark.unit
def test_detector_finds_a_path_install_missing_its_sibling(tmp_path: Path) -> None:
    """The subtle half: every requirement is a path, and it is still unsafe."""
    findings = _probe_findings(
        tmp_path, "Install it:\n\n```bash\npip install -e lib/probe_sdk\n```\n"
    )
    assert len(findings) == 1, findings
    assert "probe-common" in findings[0].problem
    assert findings[0].line == 4


@pytest.mark.unit
def test_detector_finds_a_bare_name_install(tmp_path: Path) -> None:
    findings = _probe_findings(tmp_path, '```\npip install "probe_common[core]"\n```\n')
    assert len(findings) == 1, findings
    assert "by bare name" in findings[0].problem


@pytest.mark.unit
def test_detector_finds_an_install_by_import_name(tmp_path: Path) -> None:
    """``probe_sdk`` is the import name, ``probe-sdk`` the distribution name."""
    findings = _probe_findings(tmp_path, "```bash\nuv pip install probe_sdk\n```\n")
    assert len(findings) == 1, findings
    assert "by bare name" in findings[0].problem


@pytest.mark.unit
def test_detector_resolves_a_bare_dot_through_cd(tmp_path: Path) -> None:
    findings = _probe_findings(
        tmp_path,
        "```bash\ncd lib/probe_sdk\npython3 -m pip install -e '.[dev]'\n```\n",
    )
    assert len(findings) == 1, findings
    assert "probe-common" in findings[0].problem


@pytest.mark.unit
def test_detector_follows_a_backslash_continuation(tmp_path: Path) -> None:
    findings = _probe_findings(
        tmp_path, "```bash\npip install \\\n    -e lib/probe_sdk\n```\n"
    )
    assert len(findings) == 1, findings


@pytest.mark.unit
@pytest.mark.parametrize(
    "markdown",
    [
        pytest.param(
            "1. First step:\n\n   ```bash\n   pip install -e lib/probe_sdk\n   ```\n",
            id="fence-indented-3-inside-a-numbered-list",
        ),
        pytest.param(
            "1. Do this:\n\n   - and this:\n\n     ```bash\n"
            "     pip install -e lib/probe_sdk\n     ```\n",
            id="fence-indented-5-inside-a-nested-list",
        ),
        pytest.param(
            "- Outer:\n\n  1. Inner:\n\n      ```bash\n"
            "      pip install -e lib/probe_sdk\n      ```\n",
            id="fence-indented-6-inside-a-nested-list",
        ),
    ],
)
def test_detector_sees_a_fence_indented_inside_a_list(
    tmp_path: Path, markdown: str
) -> None:
    """CommonMark indents a fence relative to its container, not to column zero.

    Several documents here legitimately sit at five or six columns. Anchoring the
    fence pattern at column zero made every one of them invisible.
    """
    assert len(_probe_findings(tmp_path, markdown)) == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            "PIP_INDEX_URL=https://mirror/simple pip install -e lib/probe_sdk",
            id="env-var-prefix",
        ),
        pytest.param(
            "PIP_NO_CACHE_DIR=1 PIP_INDEX_URL=x pip install -e lib/probe_sdk",
            id="two-env-var-prefixes",
        ),
        pytest.param(
            "python3.12 -m pip install -e lib/probe_sdk", id="dotted-minor-interpreter"
        ),
        pytest.param("sudo pip install -e lib/probe_sdk", id="sudo"),
        pytest.param("$ pip install -e lib/probe_sdk", id="transcript-prompt"),
        pytest.param("cd /tmp && pip3 install -e lib/probe_sdk", id="chained-after-cd"),
    ],
)
def test_detector_sees_each_accepted_command_prefix(
    tmp_path: Path, command: str
) -> None:
    """Each prefix the module claims to tolerate must actually be reached.

    A claimed-but-unreachable capability is the defect class this gate exists for,
    so every shape named in :data:`_PIP_INSTALL`'s comment is asserted here.
    """
    assert len(_probe_findings(tmp_path, f"```bash\n{command}\n```\n")) == 1


@pytest.mark.unit
def test_a_cd_does_not_leak_across_block_boundaries(tmp_path: Path) -> None:
    """A reader who copies the second fence never ran the first fence's ``cd``.

    Resolving ``.`` against it would report a finding for a command that installs
    nothing first-party — a spurious red on an unrelated docs change.
    """
    assert (
        _probe_findings(
            tmp_path,
            "```bash\ncd lib/probe_sdk\n```\n\nThen, elsewhere:\n\n"
            "```bash\npip install -e .\n```\n",
        )
        == []
    )


@pytest.mark.unit
def test_detector_reads_a_notebook_code_cell(tmp_path: Path) -> None:
    """``%pip install`` in a code cell is as copyable as a fenced command.

    ``notebooks/`` carries a dozen live ``%pip install -e`` cells, and a notebook is
    exactly where someone writes the bare-name form.
    """
    _probe_tree(tmp_path, "nothing here\n")
    notebook = tmp_path / "notebooks" / "demo.ipynb"
    notebook.parent.mkdir(parents=True)
    notebook.write_text(
        json.dumps(
            {
                "cells": [
                    {"cell_type": "markdown", "source": ["# Setup\n"]},
                    {
                        "cell_type": "code",
                        "source": [
                            'ROOTDIR = "../.."\n',
                            "# %pip install probe_common   <- commented out\n",
                            '%pip install -q -e "{ROOTDIR}/lib/probe_sdk"\n',
                        ],
                    },
                ]
            }
        )
    )
    findings = sweep(tmp_path)
    assert len(findings) == 1, findings
    assert findings[0].cell == 2
    assert findings[0].line == 3
    assert "cell 2 line 3" in str(findings[0])


@pytest.mark.unit
def test_detector_reads_a_fence_in_a_notebook_markdown_cell(tmp_path: Path) -> None:
    _probe_tree(tmp_path, "nothing here\n")
    notebook = tmp_path / "notebooks" / "demo.ipynb"
    notebook.parent.mkdir(parents=True)
    notebook.write_text(
        json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "markdown",
                        "source": [
                            "Install:\n",
                            "```bash\n",
                            "pip install probe-sdk\n",
                            "```\n",
                        ],
                    }
                ]
            }
        )
    )
    findings = sweep(tmp_path)
    assert len(findings) == 1, findings
    assert "by bare name" in findings[0].problem


@pytest.mark.unit
@pytest.mark.parametrize(
    "markdown",
    [
        pytest.param(
            "```bash\npip install -e lib/probe_common_pkg -e lib/probe_sdk\n```\n",
            id="siblings-on-one-command-line",
        ),
        pytest.param(
            "```bash\npip install -e lib/probe_sdk --no-deps\n```\n",
            id="no-deps-resolves-nothing",
        ),
        pytest.param(
            "```bash\npip install -e lib/probe_common_pkg\n```\n",
            id="package-with-no-first-party-requirements",
        ),
        pytest.param(
            "```bash\npip install -r requirements.txt\n```\n",
            id="requirements-file-argument-is-not-a-requirement",
        ),
        pytest.param(
            "```bash\npip install --index-url https://mirror/simple probe-other\n```\n",
            id="third-party-name",
        ),
        pytest.param(
            "Never write `pip install probe_common`: it is not ours.\n",
            id="inline-prose-is-out-of-scope",
        ),
        pytest.param(
            "```\nlib/\n  Dockerfile   builds the image (pip install probe_common)\n```\n",
            id="english-parenthetical-inside-a-tree-listing",
        ),
        pytest.param(
            "```bash\ncd lib/probe_common_pkg\npip install -e .\n```\n",
            id="bare-dot-resolving-to-a-standalone-package",
        ),
    ],
)
def test_detector_accepts_safe_forms(tmp_path: Path, markdown: str) -> None:
    assert _probe_findings(tmp_path, markdown) == []
