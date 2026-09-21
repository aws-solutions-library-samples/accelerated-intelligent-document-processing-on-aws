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

**The Markdown universe comes from git**, via ``repo_files.tracked_paths``. A
filesystem walk finds gitignored build output and sibling agent worktrees, each of
which holds a whole copy of this repository; see ``repo_files.py`` for the failure
that convention exists to prevent.

**Scope: fenced code blocks.** A command inside a fence is something a reader
copies and runs. The same text in inline backticks in a sentence is the document
*naming* a command in order to discuss it — which is how the bare-name hazard is
explained in the first place, in this repository's own security page, in
``CONTRIBUTING.md`` and in ``lib/idp_common_pkg/README.md``. Treating those as
instructions would make the warnings unwritable. The residual aperture is stated
plainly: an unsafe command written only in prose is not detected. Nothing else is
excluded — there is no per-file or per-line exemption list here, and no gate
exemption surface to register.

**Resolution of a bare ``.``**, as in ``pip install -e ".[test]"``, follows what a
reader's shell would do: the nearest preceding ``cd`` in the same fence, and failing
that the package directory the Markdown file itself lives in. An unresolvable ``.``
names no package and yields no finding, which is the conservative direction.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path

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
#: shell separator. Tolerates the shapes this repository's docs actually use — a
#: ``$`` transcript prompt, a notebook ``%``/``!`` magic, ``python -m pip``,
#: ``uv pip``, and a ``cd X && pip install`` chain.
#:
#: A command position is required rather than a bare substring search because a
#: fenced block is not always a script. Directory-tree listings and annotated
#: file inventories are fenced too, and one of them describes a container build as
#: "(pip install seed-data + idp_common)" — an English parenthetical, not an
#: instruction, and the only false positive this gate produced on the tree. A
#: parenthesised subshell would consequently be missed; nothing in these docs
#: writes one, and the alternative is flagging prose.
_PIP_INSTALL = re.compile(
    r"""(?:^|\||&&|;)\s*
        (?:\$\s+|[!%]\s*)?
        (?:sudo\s+)?
        (?:(?:python|python3|py)\s+-m\s+)?
        (?:uv\s+)?
        pip3?\s+install(?![\w-])""",
    re.VERBOSE,
)

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
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

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.problem}\n    {self.command}"


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """``(1-based line number, joined command)`` for each fenced-block line.

    Backslash continuations are joined, because a ``pip install`` whose
    requirements wrap onto the next line is one command to the reader's shell.
    """
    out: list[tuple[int, str]] = []
    fence: str | None = None
    pending_no: int | None = None
    pending: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        marker = _FENCE.match(line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token[0] * 3
            elif token.startswith(fence):
                fence = None
                if pending:
                    out.append((pending_no or number, " ".join(pending)))
                    pending, pending_no = [], None
            continue
        if fence is None:
            continue
        stripped = line.rstrip()
        if pending_no is None:
            pending_no = number
        if stripped.endswith("\\"):
            pending.append(stripped[:-1].strip())
            continue
        pending.append(stripped.strip())
        out.append((pending_no, " ".join(p for p in pending if p)))
        pending, pending_no = [], None
    if pending:
        out.append((pending_no or len(text.splitlines()), " ".join(pending)))
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
    md_path: str,
    lines: list[tuple[int, str]],
    position: int,
    prefix: str,
) -> Package | None:
    """What ``.`` means: the nearest preceding ``cd``, else the file's own package."""
    for text in [prefix] + [lines[i][1] for i in range(position - 1, -1, -1)]:
        target = _cd_target(text)
        if target is None:
            continue
        pkg = index.by_basename.get(Path(target.strip("\"'")).name)
        return pkg
    for pkg in index.packages:
        if md_path == pkg.directory or md_path.startswith(pkg.directory + "/"):
            return pkg
    return None


def scan_markdown(text: str, md_path: str, index: PackageIndex) -> list[Finding]:
    """Findings for one Markdown document."""
    findings: list[Finding] = []
    lines = _logical_lines(text)
    for position, (number, command) in enumerate(lines):
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
                            index, md_path, lines, position, command[: match.start()]
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
                            md_path,
                            number,
                            command.strip(),
                            f"installs the first-party package {name!r} by bare "
                            f"name, which resolves from the configured index; use "
                            f"a path under lib/ instead",
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
                            md_path,
                            number,
                            command.strip(),
                            f"installs {pkg.directory} from a path but not "
                            f"{', '.join(missing)}, which it requires by name; pip "
                            f"resolves the missing sibling(s) from the index. Put "
                            f"every first-party package on the same command line, "
                            f"or use make setup",
                        )
                    )
    return findings


def sweep(root: Path = REPO_ROOT) -> list[Finding]:
    """Every finding across every tracked Markdown file under ``root``."""
    index = PackageIndex(discover_packages(root))
    findings: list[Finding] = []
    for path in tracked_paths(root, "*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover
            continue
        findings.extend(scan_markdown(text, path.relative_to(root).as_posix(), index))
    return findings


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_no_documented_install_can_resolve_a_first_party_name_from_an_index() -> None:
    """Every ``pip install`` in a fenced block must be index-proof."""
    findings = sweep()
    assert not findings, (
        "documented pip install command(s) can resolve a first-party name from a "
        "package index (dependency confusion — see docs/dependency-confusion.md):"
        "\n\n" + "\n".join(str(f) for f in findings)
    )


@pytest.mark.unit
def test_the_sweep_reads_a_nonempty_universe() -> None:
    """A sweep that finds nothing to check passes vacuously.

    Both halves are asserted, because either one going to zero makes the gate above
    meaningless while leaving it green: the package model and the Markdown corpus.
    """
    packages = discover_packages()
    assert len(packages) >= 5, f"first-party package discovery found {packages}"
    assert any(pkg.requires for pkg in packages), (
        "no package was found to require a sibling by bare name; either the "
        "pyproject requirements changed or the derivation is broken"
    )
    assert len(tracked_paths(REPO_ROOT, "*.md")) > 50


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
