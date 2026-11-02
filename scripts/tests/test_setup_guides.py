# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The setup guides and setup scripts install against the lock and name real paths.

``make setup-venv`` installs every package against the export of
``lib/idp_common_pkg/uv.lock`` and checks afterwards that no first-party package came
from an index, which is what makes a local environment hold the versions CI tests.
Setup instructions that wrote their own ``pip install`` lines resolved versions afresh
(#1747, #1758), and two guides told the reader to run a setup script at a path where
it is not.

Two rules apply to every setup surface:

* **No unconstrained install of anything the lock decides.** A ``pip install`` that
  does not pass the lock's export as its constraints file (``-c``, ``--constraint`` or
  ``PIP_CONSTRAINT`` naming ``lock-constraints/`` or ``$LOCK_CONSTRAINTS``) may install only
  plain PyPI names that are neither a first-party distribution nor a package
  ``uv.lock`` records — tools outside the environment under test, such as ``uv`` or
  ``virtualenv``. A path, a URL, a requirements file or a locked name needs the
  constraints file. Both sets are read from ``lib/*/pyproject.toml`` and the lock.
* **Every repository path a surface names exists.** A token ending in ``.sh``, ``.py``
  or ``.ps1`` is a repository path when it starts with ``./``, when its first
  directory is one the repository has, or when its file name is a tracked file's; it
  must then exist relative to the repository root — or, for a script, to its own
  directory, and for a page, when it is a bare file name. Paths an ignore rule covers
  (``.venv``) are output, not repository paths.

The surfaces are derived: every ``docs/setup-development-env-*.md`` guide, every
Markdown page and shell script under ``scripts/setup/``, and every script a guide
names. ``README.md`` is the one named file — the repository's front page carries an
install step and is not a guide by name.

Bounds, so a green run is not over-read: code is fenced blocks (also inside a
blockquote), inline code spans and script lines — not indented code blocks or HTML
``<code>``/``<pre>``; the installer is pip (``uv add``, ``pipx``, ``conda`` and the like
are not read), and an install assembled from a variable is invisible; a guide named
outside ``docs/setup-development-env-*.md`` is not a surface.

The WSL guide keeps the stricter rule #1747 gave it: it and its script invoke ``pip``
in no form at all. WSL is Linux, so ``make`` is available and there is no reason to
differ from the Linux guide.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GUIDE = REPO / "docs" / "setup-development-env-WSL.md"
SETUP_SCRIPT = REPO / "scripts" / "setup" / "wsl_setup.sh"

_WORD = re.compile(r"[\w.+-]+")
_PIP = re.compile(r"pip[\d.]*")
_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE = re.compile(r"`([^`]+)`")


def _invokes_pip(text: str) -> bool:
    return any(_PIP.fullmatch(word) for word in _WORD.findall(text))


def _guide_code(markdown: str, inline: bool = True) -> list[str]:
    """Every fenced line and, with ``inline``, every inline code span: what a
    reader copies."""
    code, fence = [], None
    for line in markdown.splitlines():
        # A fence inside a blockquote is a fence: strip the quote markers first.
        line = re.sub(r"^(\s*>)+ ?", "", line)
        opener = _FENCE.match(line)
        if opener and fence in (None, opener.group(1)):
            fence = None if fence else opener.group(1)
            continue
        if fence:
            code.append(line)
        elif inline:
            code.extend(_INLINE_CODE.findall(line))
    return code


def test_the_wsl_guide_gives_no_pip_command():
    code = _guide_code(GUIDE.read_text(encoding="utf-8"))
    # Non-vacuity: the parse found the guide's commands.
    assert any("aws configure" in line for line in code)
    offending = [line for line in code if _invokes_pip(line)]
    assert not offending, (
        f"{GUIDE.relative_to(REPO)} installs with pip, not against the lock: "
        f"{offending}. Use `make setup-venv`."
    )
    # A step that runs it, not a mention of it in prose.
    fenced = _guide_code(GUIDE.read_text(encoding="utf-8"), inline=False)
    assert any(line.strip() == "make setup-venv" for line in fenced)


def test_the_wsl_guide_names_a_setup_script_that_exists():
    code = _guide_code(GUIDE.read_text(encoding="utf-8"))
    scripts = [
        line.strip() for line in code if re.match(r"\s*\./scripts/\S+\.sh", line)
    ]
    assert scripts, "the guide no longer runs a setup script; update this test"
    for script in scripts:
        assert (REPO / script.split()[0]).is_file(), f"{script} does not exist"


def test_the_wsl_setup_script_never_invokes_pip():
    lines = [
        line
        for line in SETUP_SCRIPT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    # Non-vacuity: the apt line naming python3-pip is read and is not pip.
    assert any("python3-pip" in line for line in lines)
    offending = [line for line in lines if _invokes_pip(line)]
    assert not offending, f"{SETUP_SCRIPT.relative_to(REPO)} invokes pip: {offending}"
    # The printed next step itself, not a mention of the target in another step.
    assert any(re.search(r':\s*make setup-venv"\s*$', line) for line in lines)


# --------------------------------------------------------------------------------------
# Every setup guide and setup script
# --------------------------------------------------------------------------------------

_LOCK = REPO / "lib" / "idp_common_pkg" / "uv.lock"
_SCRIPT_SUFFIX = re.compile(r"""[^\s"'`()<>|;=,]+\.(?:sh|py|ps1)\b""")
_OPERATORS = {"&&", "||", ";", "|", "&", "(", ")", ";;"}
_REDIRECTS = re.compile(r"^\d*(?:>>?|<|>&|&>)$")
#: pip and uv-pip options that take a separate value. After ``install``, an option
#: missing from this table never consumes its value, so the value is read as a
#: requirement and the check errs towards a false red. Before ``install`` a missing
#: one cannot hide the subcommand either: see :func:`_pip_installs`.
_VALUE_OPTIONS = {
    "-c", "--constraint", "-r", "--requirement", "-e", "--editable", "-t",
    "--target", "-i", "--index-url", "--extra-index-url", "--index", "-f",
    "--find-links", "--prefix", "--root", "--src", "--platform", "--python-version",
    "--implementation", "--abi", "--upgrade-strategy", "--progress-bar", "--log",
    "--proxy", "--retries", "--timeout", "--exists-action", "--trusted-host",
    "--cert", "--client-cert", "--cache-dir", "--python", "-p", "--report",
    "-C", "--config-settings", "--global-option", "--no-binary", "--only-binary",
}  # fmt: skip
#: ``NAME=value`` in front of a command; ``ruff==0.15.13`` is a requirement, not this.
_ASSIGNMENT = re.compile(r"^[A-Za-z_]\w*=(?!=)")
_PLAIN_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?([<>=!~;].*)?$")


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _tracked() -> list[str]:
    """Tracked files plus untracked ones no ignore rule covers, so a guide being
    written is judged before ``git add`` rather than after."""
    return subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()


def _first_party_names() -> set[str]:
    names = set()
    for pyproject in sorted((REPO / "lib").glob("*/pyproject.toml")):
        names.add(_normalise(tomllib.loads(pyproject.read_text())["project"]["name"]))
    return names


def _locked_names() -> set[str]:
    lock = tomllib.loads(_LOCK.read_text(encoding="utf-8"))
    return {_normalise(package["name"]) for package in lock["package"]}


def _guides(tracked: list[str]) -> list[str]:
    guides = [
        path
        for path in tracked
        if re.fullmatch(r"docs/setup-development-env-[^/]+\.md", path)
        or re.fullmatch(r"scripts/setup/.+\.md", path)
    ]
    return sorted(guides) + ["README.md"]


def _joined(lines: list[str]) -> list[str]:
    """Lines with their continuations folded in — a trailing backslash (shell) or
    backtick (PowerShell) — so a command split over several lines is read as one."""
    joined, pending = [], ""
    for line in lines:
        if line.rstrip().endswith(("\\", "`")):
            pending += line.rstrip()[:-1] + " "
            continue
        joined.append(pending + line)
        pending = ""
    if pending:
        joined.append(pending)
    return joined


def _script_lines(text: str) -> list[str]:
    return [line for line in _joined(text.splitlines()) if line.strip()]


def _uncommented(lines: list[str]) -> list[str]:
    return [line for line in lines if not line.lstrip().startswith("#")]


def _tokens(line: str) -> list[str]:
    # A backslash is a Windows path separator in these guides far more often than a
    # shell escape, and POSIX shlex would delete it: `lib\idp_sdk` became the plain
    # name `libidp_sdk`.
    line = line.replace("\\", "/")
    # Backtick command substitution: what is inside runs as a command of its own.
    line = line.replace("`", " ; ")
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:  # an unbalanced quote: read the words as they stand
        return line.split()


def _commands(line: str, depth: int = 0) -> list[list[str]]:
    """The simple commands on a line, with redirections removed. A quoted argument
    holding a space (``echo "pip install …"``, ``bash -c "…"``) is read as a command
    line of its own, because text a script prints or hands to a shell is still an
    instruction."""
    commands, current, tokens = [], [], _tokens(line)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _OPERATORS:
            commands.append(current)
            current = []
        elif _REDIRECTS.match(token):
            index += 1  # and its target
        else:
            current.append(token)
            if depth == 0 and " " in token:
                commands.extend(_commands(token, depth + 1))
        index += 1
    commands.append(current)
    return [command for command in commands if command]


def _pip_installs(command: list[str]) -> list[list[str]]:
    """The argument lists of every ``pip install`` in a command, with any
    ``NAME=value`` assignments in front of it prepended."""
    found = []
    for index, token in enumerate(command):
        leaf = re.split(r"[\\/]", token)[-1].lower().removesuffix(".exe")
        if not _PIP.fullmatch(leaf):
            continue
        rest = command[index + 1 :]
        # ``install`` anywhere after pip is the subcommand: a global option with a
        # value (``--use-feature fast-deps``) would otherwise hide it.
        if "install" in rest:
            position = rest.index("install")
            assignments = [t for t in command[:index] if _ASSIGNMENT.match(t)]
            # Requirements ``xargs`` supplies are not on the line, so they cannot be
            # shown to be tools outside the lock.
            supplied = ["<from xargs>"] if "xargs" in command[:index] else []
            found.append(assignments + rest[position + 1 :] + supplied)
    return found


def _read_install(
    arguments: list[str], first_party: set[str], locked: set[str]
) -> tuple[bool, list[str]]:
    """``(constrained, decided)`` for one install: whether it carries a constraints
    file, and the requirements it names that the lock decides — a path, a URL, a
    requirements file, a first-party name or a name ``uv.lock`` records. A plain PyPI
    name that is none of those is a tool outside the environment under test."""
    constrained, requirements, index = False, [], 0
    while index < len(arguments):
        token = arguments[index]
        attached_constraint = re.fullmatch(
            r"(?:PIP_CONSTRAINT=|--constraint=|-c)(.+)", token
        )
        if attached_constraint:
            constrained = constrained or _names_the_lock(attached_constraint.group(1))
        elif _ASSIGNMENT.match(token):
            pass
        elif token in ("-c", "--constraint") and index + 1 < len(arguments):
            constrained = constrained or _names_the_lock(arguments[index + 1])
            index += 1
        elif token in ("-r", "--requirement") and index + 1 < len(arguments):
            requirements.append(f"-r {arguments[index + 1]}")
            index += 1
        elif token in ("-e", "--editable") and index + 1 < len(arguments):
            requirements.append(arguments[index + 1])
            index += 1
        elif token in _VALUE_OPTIONS:
            index += 1
        elif not token.startswith("-"):
            requirements.append(token)
        else:
            # The attached spellings: --editable=PATH, -ePATH, --requirement=FILE, -rFILE.
            attached = re.fullmatch(
                r"(--editable=|-e)(.+)|(--requirement=|-r)(.+)", token
            )
            if attached and attached.group(2):
                requirements.append(attached.group(2))
            elif attached:
                requirements.append(f"-r {attached.group(4)}")
        index += 1
    decided = []
    for requirement in requirements:
        plain = _PLAIN_NAME.match(requirement)
        archive = re.search(r"\.(whl|zip|tgz|tar\.\w+)$", requirement, re.IGNORECASE)
        if (
            plain
            and not archive
            and _normalise(plain.group(1)) not in first_party | locked
        ):
            continue
        decided.append(requirement)
    return constrained, decided


def _names_the_lock(value: str) -> bool:
    """Whether a constraints argument is the lock's export — the file
    ``make lock-constraints`` writes, or the variable naming it — rather than any
    file at all, which would constrain nothing to the lock."""
    return bool(re.search(r"lock-constraints/|LOCK_CONSTRAINTS", value))


def _unconstrained_requirements(
    arguments: list[str], first_party: set[str], locked: set[str]
) -> list[str]:
    """What an install would take from outside the lock, or ``[]``."""
    constrained, decided = _read_install(arguments, first_party, locked)
    return [] if constrained else decided


def _surface_lines() -> dict[str, tuple[list[str], list[str]]]:
    """``{path: (install lines, path-naming lines)}`` for every setup surface."""
    tracked = _tracked()
    surfaces: dict[str, tuple[list[str], list[str]]] = {}
    scripts = {p for p in tracked if re.fullmatch(r"scripts/setup/.+\.(sh|ps1)", p)}
    for guide in _guides(tracked):
        code = _joined(_guide_code((REPO / guide).read_text(encoding="utf-8")))
        surfaces[guide] = (code, code)
        for line in code:
            for token in _SCRIPT_SUFFIX.findall(line):
                candidate = token.replace("\\", "/").lstrip("./")
                if candidate.endswith(".sh") and candidate in tracked:
                    scripts.add(candidate)
    for script in sorted(scripts):
        lines = _script_lines((REPO / script).read_text(encoding="utf-8"))
        surfaces[script] = (_uncommented(lines), lines)
    return surfaces


def _ignored(paths: list[str]) -> set[str]:
    if not paths:
        return set()
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        cwd=REPO,
        input="\n".join(paths),
        capture_output=True,
        text=True,
        check=False,
    )
    return set(result.stdout.splitlines())


def _missing_paths(
    surface: str, lines: list[str], tracked: list[str]
) -> tuple[list[str], list[str]]:
    """``(checked, missing)`` repository paths among the lines of one surface."""
    top_dirs = {path.split("/")[0] for path in tracked if "/" in path}
    basenames = {path.rsplit("/", 1)[-1] for path in tracked}
    base = (REPO / surface).parent
    # A guide's commands are run from the repository root, so in Markdown only a bare
    # file name (a table listing the files beside it) may be read from the page's own
    # directory; ``./mac_setup.sh`` in scripts/setup/README.md is the #1758 bug. A
    # script's usage comment names itself relative to where it sits.
    own_directory = not surface.endswith(".md")
    candidates = []
    for line in lines:
        for token in _SCRIPT_SUFFIX.findall(line):
            if re.search(r"://|[$~*{<]|^[A-Za-z]:", token) or token.startswith("/"):
                continue
            path = token.replace("\\", "/")
            relative = re.sub(r"^(\./)+", "", path)
            if (
                path.startswith("./")
                or ("/" in relative and relative.split("/")[0] in top_dirs)
                or relative.rsplit("/", 1)[-1] in basenames
            ):
                candidates.append((relative, path))
    ignored = _ignored([relative for relative, _ in candidates])
    checked = [pair for pair in candidates if pair[0] not in ignored]
    missing = [
        path
        for path, written in checked
        if not (REPO / path).is_file()
        and not (
            (own_directory or ("/" not in written and not written.startswith(".")))
            and (base / path).is_file()
        )
    ]
    checked = [path for path, _ in checked]
    return checked, missing


def test_the_surfaces_are_derived_from_the_tree():
    surfaces = _surface_lines()
    guides = [s for s in surfaces if s.startswith("docs/setup-development-env-")]
    scripts = [s for s in surfaces if s.endswith(".sh")]
    # Non-vacuity: the globs found guides and scripts, and every guide yields code.
    assert guides and scripts, surfaces.keys()
    assert GUIDE.relative_to(REPO).as_posix() in surfaces
    assert SETUP_SCRIPT.relative_to(REPO).as_posix() in surfaces
    for surface, (install_lines, _) in surfaces.items():
        assert install_lines, f"{surface}: no code read; the parse has stopped working"
    # The two name sets the install rule reads, from their authorities.
    assert len(_first_party_names()) == len(
        list((REPO / "lib").glob("*/pyproject.toml"))
    )
    assert {"boto3", "ruff", "typer"} <= _locked_names()


def test_no_setup_surface_installs_outside_the_lock():
    first_party, locked = _first_party_names(), _locked_names()
    installs, offending = [], []
    for surface, (lines, _) in _surface_lines().items():
        for line in lines:
            for command in _commands(line):
                for arguments in _pip_installs(command):
                    installs.append((surface, arguments))
                    outside = _unconstrained_requirements(
                        arguments, first_party, locked
                    )
                    if outside:
                        offending.append(f"{surface}: {line.strip()} -> {outside}")
    assert not offending, (
        "these setup instructions install a first-party or locked package without "
        "the lock's constraints file, so the environment they build does not hold the "
        "versions CI tests. Use `make setup-venv`, or pass "
        f"`-c build/lock-constraints/idp_common_pkg.txt`: {offending}"
    )
    # Non-vacuity: both kinds of install the rule passes were read — a constrained
    # install of packages the lock decides (the Windows guide's), and an
    # unconstrained install of a tool it does not.
    readings = [_read_install(a, first_party, locked) for _, a in installs]
    assert any(constrained and decided for constrained, decided in readings), installs
    assert any(not constrained and not decided for constrained, decided in readings)


def test_every_path_a_setup_surface_names_exists():
    tracked = _tracked()
    checked_all, missing_all = [], []
    for surface, (_, lines) in _surface_lines().items():
        checked, missing = _missing_paths(surface, lines, tracked)
        checked_all += checked
        missing_all += [f"{surface}: {path}" for path in missing]
    assert not missing_all, (
        "these setup instructions name a script that does not exist at that path "
        f"(paths are read from the repository root or the file's own directory): "
        f"{missing_all}"
    )
    # Non-vacuity: the Linux guide's two setup scripts are among the paths checked.
    assert {
        "scripts/setup/dev_setup_al2023.sh",
        "scripts/setup/dev_setup_ubuntu.sh",
    } <= set(checked_all), checked_all


@pytest.mark.parametrize(
    ("line", "outside"),
    [
        ("pip install -e lib/idp_common_pkg -e lib/idp_sdk", True),
        ("py -m pip install -e lib\\idp_sdk", True),
        ("python3 -m pip install --upgrade 'lib/idp_common_pkg[all]'", True),
        ("cd lib/idp_common_pkg && pip install -e '.[test]'", True),
        ("pip3 install virtualenv typer 'ruff==0.15.13' --break-system-packages", True),
        ("uv pip install ruff", True),
        ("pip install ruff==0.15.13", True),
        ("pip install --editable=lib/idp_common_pkg", True),
        ("pip install -elib/idp_sdk", True),
        ("pip install --requirement=requirements.txt", True),
        ("pip install -rrequirements.txt", True),
        ("pip --python .venv install typer", True),
        ("pip install -U typer", True),
        ("pip install git+https://github.com/example/example.git", True),
        ("pip install idp-common", True),
        (".venv/bin/pip install -e lib/idp_cli_pkg", True),
        (".\\.venv\\Scripts\\pip.exe install -e lib\\idp_common_pkg", True),
        ("pip3 install virtualenv && pip3 install typer", True),
        ("printf '%s\\n' 'pip install typer'", True),
        ("pip install -e lib/idp_common_pkg -c", True),
        ("pip install --upgrade \\\n    typer", True),
        ("pip install --upgrade \\\n    virtualenv", False),
        ("python -m pip install `\n  -e lib\\idp_common_pkg -e lib\\idp_sdk", True),
        ("pip install `\n  virtualenv", False),
        ("pip install idp_common-0.1.tar.bz2", True),
        ("sudo -H pip3.12 install boto3", True),
        ("pip install -r requirements.txt", True),
        ("pip install idp_common", True),
        ('echo "Next: pip install -e lib/idp_cli_pkg"', True),
        ("pip --disable-pip-version-check install typer >/dev/null 2>&1", True),
        (
            "pip install -c build/lock-constraints/idp_common_pkg.txt -e lib/idp_sdk",
            False,
        ),
        ('pip install -c "$LOCK_CONSTRAINTS" ruff', False),
        ("pip install --constraint=build/lock-constraints/x.txt ruff", False),
        ("PIP_CONSTRAINT=build/lock-constraints/x.txt pip install ruff", False),
        ("pip install -c /dev/null -e lib/idp_common_pkg", True),
        ("pip install -c c.txt ruff", True),
        ("PIP_CONSTRAINT= pip install typer", True),
        ("pip --use-feature fast-deps install typer", True),
        ("PIP.EXE install typer", True),
        ("pip install idp_common-0.1-py3-none-any.whl", True),
        ("x=`pip install typer`", True),
        ("echo typer | xargs pip install", True),
        ("python -m pip install --upgrade pip", False),
        ("python -m pip install uv virtualenv", False),
        ("python -m pip show open-interpreter >/dev/null 2>&1", False),
    ],
)
def test_the_install_rule_reads_each_form(line, outside):
    first_party, locked = _first_party_names(), _locked_names()
    verdicts = [
        bool(_unconstrained_requirements(arguments, first_party, locked))
        for joined in _joined(line.splitlines())
        for command in _commands(joined)
        for arguments in _pip_installs(command)
    ]
    if outside:
        assert any(verdicts), f"{line!r} was not read as installing outside the lock"
    else:
        assert not any(verdicts), f"{line!r} was read as installing outside the lock"


@pytest.mark.parametrize(
    ("line", "missing"),
    [
        ("./mac_setup.sh", True),
        ("chmod +x ./dev_setup_al2023.sh", True),
        ("./scripts/wsl_setup.sh", True),
        ("python scripts/check_first_party_dep.py", True),
        ("./scripts/setup/mac_setup.sh", False),
        ("python scripts/check_first_party_deps.py", False),
        (".\\.venv\\Scripts\\Activate.ps1", False),
        ('[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"', False),
        ("curl -o- https://example.com/install.sh | bash", False),
    ],
)
def test_the_path_rule_reads_each_form(line, missing):
    _, found = _missing_paths("docs/setup-development-env-linux.md", [line], _tracked())
    assert bool(found) is missing, (line, found)


@pytest.mark.parametrize(
    ("line", "missing"),
    [
        ("./mac_setup.sh", True),
        ("mac_setup.sh", False),
        ("./scripts/setup/mac_setup.sh", False),
    ],
)
def test_a_page_beside_the_scripts_still_names_them_from_the_root(line, missing):
    _, found = _missing_paths("scripts/setup/README.md", [line], _tracked())
    assert bool(found) is missing, (line, found)


def test_a_fence_inside_a_blockquote_is_read():
    code = _guide_code("> Run:\n>\n> ```bash\n> pip install typer\n> ```\n")
    assert "pip install typer" in code, code
