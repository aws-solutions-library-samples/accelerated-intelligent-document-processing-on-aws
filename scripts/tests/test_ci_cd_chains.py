# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No CI script leaves a ``cd <dir> && ...`` chain for a later command to run after.

Both CIs run a job's script with errexit on, and errexit does not fire for a command
that fails before the last one in an ``&&`` list. So in::

    - cd src/ui && npm ci --prefer-offline --no-audit && cd ../..
    - make lint-cicd

a failed ``npm ci`` skips the ``cd ../..``, the list's failure is ignored, and the
next line runs inside ``src/ui``. That is what an npm registry timeout produced in
``static_checks`` and ``package_tests``: each reported ``No rule to make target``
for its own gate, with the real cause further up the log. Both went red only because
the next command happened to fail as well; one that succeeds in the wrong directory
leaves the job green.

``(cd src/ui && npm ci ...)`` has neither problem. The subshell's status is the
failing command's, the subshell is the whole command so errexit fires on it, and its
directory change ends with it.

**The shape refused** is a ``cd`` or ``pushd`` run in the job's own shell — not
inside a subshell or a command substitution — whose arguments are followed by
``&&``, when the job runs anything after that list. ``cd <dir> && cmd`` is
included, because there the failure ignored is the ``cd``'s own. The same list as
the job's *last* command is not refused: its status is then the script's, and the
job fails on the real cause. :func:`test_the_rule_agrees_with_bash` measures both halves under
bash, with the wrapper the GitLab runner puts round a job's script, rather than
asserting them.

**What one shell is** differs by platform, and each is read as its runner runs it.
On GitLab, ``before_script`` followed by ``script`` is one shell and ``after_script``
is another, which starts back in the project directory. On GitHub every ``run:``
step is its own ``bash -e`` process, in a workflow and in a composite action alike,
so a single-line step ending in a chain cannot hand a failure or a directory on.

The script text is read by the small lexer below. It follows quoting, comments,
heredoc bodies, ``$(...)``, backticks, ``$((...))``, ``${...}``, subshells and
``case`` patterns, and it raises on text it cannot balance rather than guessing: a
script this module cannot read fails here instead of passing.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
GITLAB = REPO_ROOT / ".gitlab-ci.yml"
GITHUB = REPO_ROOT / ".github"

DIRECTORY_CHANGERS = frozenset({"cd", "pushd"})

_OPERATORS = (
    ";;&",
    ";;",
    ";&",
    "&&",
    "||",
    "|&",
    "&>>",
    "&>",
    ">>",
    "<<<",
    "<<-",
    "<<",
    ">&",
    "<&",
    "<>",
    ">|",
    ";",
    "&",
    "|",
    "<",
    ">",
)
_SEPARATORS = frozenset(
    {";", "&", "\n", "&&", "||", "|", "|&", ";;", ";&", ";;&", ")", "()"}
)
_CONTINUATIONS = frozenset({"&&", "||", "|", "|&"})
_LIST_ENDS = frozenset({";", "&", "\n", ";;", ";&", ";;&", ")"})
_REDIRECTIONS = frozenset({"<", ">", ">>", ">&", "<&", "<>", ">|", "&>", "&>>", "<<<"})
_HEREDOC_OPERATORS = frozenset({"<<", "<<-"})
_PREFIX_WORDS = frozenset(
    {"!", "{", "if", "then", "else", "elif", "do", "while", "until", "time"}
)
_CLOSING_WORDS = frozenset({"fi", "done", "esac", "}"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
_WORD_ENDS = " \t\r\n;&|<>()"

GITLAB_RUNNER_WRAPPER = (
    "set -eo pipefail\n"
    "set +o noclobber\n"
    "(trap 'exit 1' TERM; eval {script}) < /dev/null\n"
    "exit 0\n"
)
GITHUB_RUN_SHELL = ("--noprofile", "--norc", "-eo", "pipefail")


class Token(NamedTuple):
    kind: str
    text: str
    command: bool


class Lexer:
    """Shell text read into the tokens of its outermost nesting level.

    A subshell, command substitution, arithmetic or parameter expansion is read to
    its end and kept as part of one word, so nothing inside it can look like a
    command of the outer shell. Heredoc bodies and comments produce no tokens.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def fail(self, problem: str) -> ValueError:
        start = max(self.pos - 40, 0)
        return ValueError(f"{problem} near {self.text[start : self.pos + 40]!r}")

    def read(self, nested: bool = False) -> list[Token]:
        text = self.text
        tokens: list[Token] = []
        word: list[str] = []
        heredocs: list[tuple[str, bool]] = []
        command = True
        cases = 0

        def end_word() -> None:
            nonlocal command, cases
            if not word:
                return
            value = "".join(word)
            word.clear()
            tokens.append(Token("word", value, command))
            if command and value == "case":
                cases += 1
            elif value == "esac" and cases:
                cases -= 1
            command = command and (
                value in _PREFIX_WORDS or bool(_ASSIGNMENT.match(value))
            )

        def operator(value: str) -> None:
            nonlocal command
            end_word()
            tokens.append(Token("op", value, False))
            command = value in _SEPARATORS

        while self.pos < len(text):
            char = text[self.pos]
            start = self.pos
            if char in " \t\r":
                end_word()
                self.pos += 1
            elif char == "\n":
                operator("\n")
                self.pos += 1
                for delimiter, tabs in heredocs:
                    self.skip_heredoc(delimiter, tabs)
                heredocs.clear()
            elif char == "#" and not word:
                newline = text.find("\n", self.pos)
                self.pos = len(text) if newline < 0 else newline
            elif char == "\\":
                if not text.startswith("\\\n", self.pos):
                    word.append(text[self.pos : self.pos + 2])
                self.pos += 2
            elif char in "'\"`":
                self.skip_quoted()
                word.append(text[start : self.pos])
            elif char == "$" and text.startswith(("$(", "${"), self.pos):
                self.pos += 1
                self.skip_expansion()
                word.append(text[start : self.pos])
            elif char == "(" and word and text.startswith("()", self.pos):
                operator("()")
                self.pos += 2
            elif char == "(":
                self.skip_expansion()
                word.append(text[start : self.pos])
                if len(word) == 1:
                    end_word()
            elif char == ")":
                end_word()
                if cases:
                    operator(")")
                elif nested:
                    self.pos += 1
                    return tokens
                else:
                    raise self.fail("unmatched )")
                self.pos += 1
            elif char in ";&|<>":
                value = next(op for op in _OPERATORS if text.startswith(op, self.pos))
                operator(value)
                self.pos += len(value)
                if value in _HEREDOC_OPERATORS:
                    heredocs.append((self.heredoc_delimiter(), value == "<<-"))
            else:
                word.append(char)
                self.pos += 1
        if nested:
            raise self.fail("unclosed (")
        if heredocs:
            raise self.fail(f"heredoc {heredocs[0][0]!r} has no body")
        end_word()
        return tokens

    def skip_quoted(self) -> None:
        quote = self.text[self.pos]
        self.pos += 1
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == quote:
                self.pos += 1
                return
            if quote == "'":
                self.pos += 1
            elif char == "\\":
                self.pos += 2
            elif quote == '"' and char == "`":
                self.skip_quoted()
            elif quote == '"' and self.text.startswith(("$(", "${"), self.pos):
                self.pos += 1
                self.skip_expansion()
            else:
                self.pos += 1
        raise self.fail(f"unclosed {quote}")

    def skip_expansion(self) -> None:
        if self.text.startswith("((", self.pos):
            self.skip_arithmetic()
        elif self.text[self.pos] == "(":
            self.pos += 1
            self.read(nested=True)
        else:
            self.skip_braces()

    def skip_arithmetic(self) -> None:
        depth = 0
        while self.pos < len(self.text):
            char = self.text[self.pos]
            self.pos += 1
            depth += {"(": 1, ")": -1}.get(char, 0)
            if depth == 0:
                return
        raise self.fail("unclosed ((")

    def skip_braces(self) -> None:
        depth = 0
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == "\\":
                self.pos += 2
            elif char in "'\"`":
                self.skip_quoted()
            elif self.text.startswith(("$(", "${"), self.pos):
                self.pos += 1
                self.skip_expansion()
            else:
                self.pos += 1
                depth += {"{": 1, "}": -1}.get(char, 0)
                if depth == 0:
                    return
        raise self.fail("unclosed ${")

    def heredoc_delimiter(self) -> str:
        while self.pos < len(self.text) and self.text[self.pos] in " \t":
            self.pos += 1
        start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] not in _WORD_ENDS:
            if self.text[self.pos] in "'\"":
                self.skip_quoted()
            else:
                self.pos += 1
        delimiter = re.sub(r"[\"'\\]", "", self.text[start : self.pos])
        if not delimiter:
            raise self.fail("heredoc without a delimiter")
        return delimiter

    def skip_heredoc(self, delimiter: str, tabs: bool) -> None:
        while self.pos < len(self.text):
            newline = self.text.find("\n", self.pos)
            end = len(self.text) if newline < 0 else newline
            line = self.text[self.pos : end]
            self.pos = end + 1
            if (line.lstrip("\t") if tabs else line) == delimiter:
                return
        raise self.fail(f"heredoc {delimiter!r} is never closed")


def directory_chains(tokens: list[Token]) -> list[tuple[int, int]]:
    """``(first, last)`` span from each ``cd <dir> &&`` run in this shell to the end
    of its list."""
    spans: list[tuple[int, int]] = []
    for first, token in enumerate(tokens):
        if not (
            token.kind == "word" and token.command and token.text in DIRECTORY_CHANGERS
        ):
            continue
        index = first + 1
        while index < len(tokens) and (
            tokens[index].kind == "word" or tokens[index].text in _REDIRECTIONS
        ):
            index += 1
        if index == len(tokens) or tokens[index].text != "&&":
            continue
        last = index
        previous = tokens[index].text
        while last + 1 < len(tokens):
            following = tokens[last + 1]
            if following.text in _LIST_ENDS and following.kind == "op":
                if following.text != "\n" or previous not in _CONTINUATIONS:
                    break
            last += 1
            if following.text != "\n":
                previous = following.text
        spans.append((first, last))
    return spans


def chains_followed_by_more(commands: list[str]) -> list[str]:
    """Each ``cd`` chain in one shell's ``commands`` that a later command runs after.

    ``commands`` is everything one shell runs, in order. Raises ``ValueError`` on
    text the lexer cannot read.
    """
    lexed = [Lexer(command).read() for command in commands]
    found: list[str] = []
    for position, tokens in enumerate(lexed):
        later = any(t.kind == "word" for rest in lexed[position + 1 :] for t in rest)
        for first, last in directory_chains(tokens):
            after = tokens[last + 1 :]
            if later or any(
                t.kind == "word" and t.text not in _CLOSING_WORDS for t in after
            ):
                found.append(
                    " ".join(t.text for t in tokens[first : last + 1] if t.text != "\n")
                )
    return found


def _flatten(items: object) -> list[str]:
    """A GitLab script list with anchors expanded (an alias arrives as a list)."""
    if items is None:
        return []
    if isinstance(items, str):
        return [items]
    flat: list[str] = []
    for item in items if isinstance(items, list) else [items]:
        flat.extend(_flatten(item) if isinstance(item, list) else [str(item)])
    return flat


def gitlab_shells(text: str) -> dict[str, list[str]]:
    """``{label: commands}`` for every shell a GitLab configuration runs."""
    loaded = yaml.safe_load(text) or {}
    default = loaded.get("default") or {}
    shells: dict[str, list[str]] = {}
    for name, body in loaded.items():
        if not isinstance(body, dict) or "script" not in body:
            continue
        shells[f"{name}: before_script + script"] = _flatten(
            body.get("before_script", default.get("before_script"))
        ) + _flatten(body["script"])
        after = _flatten(body.get("after_script", default.get("after_script")))
        if after:
            shells[f"{name}: after_script"] = after
    return shells


def github_shells(text: str) -> dict[str, list[str]]:
    """``{label: commands}`` for every ``run:`` step of a workflow or action."""
    loaded = yaml.safe_load(text) or {}
    groups = [
        (name, body.get("steps")) for name, body in (loaded.get("jobs") or {}).items()
    ]
    groups.append(("runs", (loaded.get("runs") or {}).get("steps")))
    shells: dict[str, list[str]] = {}
    for job, steps in groups:
        for number, step in enumerate(steps or [], start=1):
            if "run" in step:
                label = f"{job}: step {number} ({step.get('name', 'unnamed')})"
                shells[label] = [str(step["run"])]
    return shells


def ci_shells() -> dict[str, dict[str, list[str]]]:
    """``{config path: {label: commands}}`` for every CI configuration in the tree."""
    readers = {GITLAB: gitlab_shells}
    for path in sorted(GITHUB.glob("workflows/*.y*ml")):
        readers[path] = github_shells
    for path in sorted(GITHUB.glob("actions/**/action.y*ml")):
        readers[path] = github_shells
    return {
        str(path.relative_to(REPO_ROOT)): read(path.read_text(encoding="utf-8"))
        for path, read in readers.items()
    }


@pytest.mark.unit
def test_no_ci_shell_runs_anything_after_a_cd_chain() -> None:
    """Every GitLab job and every GitHub step, as its runner runs it."""
    shells = ci_shells()
    assert shells.get(GITLAB.name), f"no job with a script was read from {GITLAB}"
    assert any(path.startswith(".github/") and jobs for path, jobs in shells.items()), (
        f"no `run:` step was read from {GITHUB}, so GitHub was not checked at all"
    )
    offenders: list[str] = []
    unreadable: list[str] = []
    for path, by_label in shells.items():
        for label, commands in by_label.items():
            try:
                chains = chains_followed_by_more(commands)
            except ValueError as error:
                unreadable.append(f"{path}: {label}: {error}")
                continue
            offenders.extend(f"{path}: {label}: `{chain}`" for chain in chains)
    assert not unreadable, (
        "this module's lexer could not read these scripts, so it cannot say whether "
        "they hold a cd chain. Fix the lexer for the construct named; do not narrow "
        "what is read:\n  " + "\n  ".join(unreadable)
    )
    assert not offenders, (
        "these CI scripts change directory in an `&&` chain that something later in "
        "the same shell runs after. errexit ignores a failure anywhere but the last "
        "command of such a list, so a failure there is not reported, and the next "
        "command runs in the wrong directory:\n  " + "\n  ".join(offenders) + "\n\n"
        "Run the chain in a subshell, `(cd <dir> && ...)`: its status is the failing "
        "command's, errexit fires on it, and its directory change ends with it. A "
        "GitHub step can use `working-directory:` instead."
    )


ORIGINAL_GITLAB = """
.node_setup: &node_setup
  - node --version

static_checks:
  before_script:
    - *node_setup
    - npm install -g basedpyright
    - cd src/ui && npm ci --prefer-offline --no-audit && cd ../..
  script:
    - pip install 'cfn-lint==1.51.0'
    - make lint-cicd SKIP_NPM_CI=1

unit_tests:
  before_script:
    - *node_setup
    - cd src/ui && npm ci --prefer-offline --no-audit && cd ../..
  script:
    - make test-cicd -C lib/idp_common_pkg SKIP_INSTALL=1

package_tests:
  before_script:
    - *node_setup
    - cd src/ui && npm ci --prefer-offline --no-audit && cd ../..
  script:
    - make test-packages-cicd

ui_tests:
  before_script:
    - *node_setup
  script:
    - cd src/ui && npm ci --prefer-offline --no-audit && npx vitest run --silent && cd ../..

ux_test_build:
  script:
    - python3 scripts/ux_runner/ui_change.py --out ui-change.json
    - |
      if python3 -c 'import json, sys; sys.exit(0 if json.load(open("ui-change.json"))["skip"] else 1)'; then
        echo "Only tests changed under src/ui; nothing to build."
        exit 0
      fi
      if [ -z "${UX_TEST_UI_ENV:-}" ]; then
        echo "SKIPPED: UX_TEST_UI_ENV is not set in this job."
        exit 0
      fi
      printf '%s' "$UX_TEST_UI_ENV" | base64 -d > src/ui/.env.production.local
      cd src/ui && without_job_secrets npm ci --prefer-offline --no-audit && without_job_secrets npm run build:only
"""

ORIGINAL_GITHUB = """
jobs:
  ui_tests:
    steps:
      - name: Run UI unit tests
        run: cd src/ui && npm ci --prefer-offline --no-audit && npx vitest run --silent
"""


@pytest.mark.unit
def test_the_lines_that_hid_an_npm_failure_are_caught() -> None:
    """The ``.gitlab-ci.yml`` lines this module was written for, verbatim.

    The three ``before_script`` installs are refused: each is followed by the job's
    gate, which is the command that reported a missing ``make`` target in place of
    the registry timeout. The ``ui_tests`` line and the end of ``ux_test_build``'s
    block are each their job's last command, so a failure in them was the job's
    status and was reported as itself; the subshell form those lines use now is
    for uniformity, and this rule does not demand it of them.
    """
    flagged = {
        label.split(":")[0]
        for label, commands in gitlab_shells(ORIGINAL_GITLAB).items()
        if chains_followed_by_more(commands)
    }
    assert flagged == {"static_checks", "unit_tests", "package_tests"}, flagged
    for commands in github_shells(ORIGINAL_GITHUB).values():
        assert chains_followed_by_more(commands) == []


AGREEMENT_CASES = [
    ("gitlab", ["cd sub && false && cd ..", "echo AFTER"], True),
    ("gitlab", ["cd missing && echo x", "echo AFTER"], True),
    ("gitlab", ["(cd sub && false && echo x)", "echo AFTER"], False),
    ("gitlab", ["(cd sub && false)", "echo AFTER"], False),
    ("gitlab", ["echo BEFORE", "cd sub && false && echo x && cd .."], False),
    ("gitlab", ["if false; then\n  exit 0\nfi\ncd sub && false && echo x"], False),
    ("gitlab", ["cd sub && false && cd ..\necho AFTER"], True),
    ("gitlab", ["cd sub &&\n  false &&\n  cd ..", "echo AFTER"], True),
    ("gitlab", ["echo BEFORE; cd sub && false && cd ..; echo AFTER"], True),
    ("gitlab", ["if true; then cd sub && false && cd ..; fi", "echo AFTER"], True),
    ("gitlab", ["{ cd sub && false && cd ..; }", "echo AFTER"], True),
    ("gitlab", ["for x in 1; do cd sub && false && cd ..; done", "echo AFTER"], True),
    ("gitlab", ["case x in x) cd sub && false && cd .. ;; esac", "echo AFTER"], True),
    ("gitlab", ["pushd sub && false && popd", "echo AFTER"], True),
    ("gitlab", ["cd sub || exit 1", "false", "echo AFTER"], False),
    ("github", ["cd sub && false && echo vitest"], False),
    ("github", ["cd sub && false && cd ..\necho AFTER"], True),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("platform", "commands", "refused"),
    AGREEMENT_CASES,
    ids=[" | ".join(case[1]).replace("\n", " ") for case in AGREEMENT_CASES],
)
def test_the_rule_agrees_with_bash(
    tmp_path: Path, platform: str, commands: list[str], refused: bool
) -> None:
    """Refused exactly when bash lets a later command run after the failure.

    ``false`` stands in for the failing ``npm ci`` and ``echo AFTER`` for the next
    command. A script this module accepts must also end the job non-zero, so the
    failure is what the job reports.
    """
    assert (chains_followed_by_more(commands) != []) is refused
    bash = shutil.which("bash")
    assert bash, "bash runs both CIs' scripts and is on every Linux and macOS runner"
    (tmp_path / "sub").mkdir()
    if platform == "gitlab":
        script = GITLAB_RUNNER_WRAPPER.format(script=shlex.quote("\n".join(commands)))
        argv = [bash, "-c", script]
    else:
        (step,) = commands
        (tmp_path / "step.sh").write_text(step, encoding="utf-8")
        argv = [bash, *GITHUB_RUN_SHELL, str(tmp_path / "step.sh")]
    ran = subprocess.run(
        argv,
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert ("AFTER" in ran.stdout.splitlines()) is refused, ran.stdout + ran.stderr
    if not refused:
        assert ran.returncode != 0, ran.stdout + ran.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    ("script", "refused"),
    [
        ("cat <<'EOF'\ncd sub && npm ci && cd ..\nEOF\nmake lint", False),
        ("cat <<-EOF\n\tcd sub && npm ci\n\tEOF\nmake lint", False),
        ("python3 - <<'PY'\nprint('(')\nPY\ncd sub && npm ci\nmake lint", True),
        ("# cd sub && npm ci && cd ..\nmake lint", False),
        ("make a  # then cd sub && npm ci\nmake lint", False),
        ("echo v1.0#beta; cd sub && npm ci\nmake lint", True),
        ("echo 'cd sub && npm ci'\nmake lint", False),
        ('echo "cd sub && npm ci"\nmake lint', False),
        ("echo cd sub && npm ci\nmake lint", False),
        ("V=$(cd sub && pwd)\nmake lint", False),
        ("V=`cd sub && pwd`\nmake lint", False),
        ('echo "$(cd sub && pwd)"\nmake lint', False),
        ("diff <(cd sub && ls) out\nmake lint", False),
        ('echo "${X:-a b}"; cd sub && npm ci\nmake lint', True),
        ('case "$x" in a) cd sub && npm ci ;; esac\nmake lint', True),
        (
            'A=$(case "$(uname -m)" in x86_64) echo x;; *) echo y;; esac)\n'
            "cd sub && npm ci\nmake lint",
            True,
        ),
        ("N=$((N+1))\ncd sub && npm ci\nmake lint", True),
        ("X=1 cd sub && npm ci\nmake lint", True),
        ("cd sub \\\n  && npm ci\nmake lint", True),
        ("cd sub 2>/dev/null && npm ci\nmake lint", True),
        ("f() {\n  cd sub && npm ci\n}\nf", True),
        ("cd sub || exit 1\nnpm ci\nmake lint", False),
        ("cd sub\nnpm ci", False),
        ("if true; then\n  cd sub && npm ci\nfi", False),
    ],
)
def test_the_lexer_reads_what_the_shell_runs(script: str, refused: bool) -> None:
    """Data, comments, quoted text and substitutions are not commands of the shell;
    a chain after an expansion, a ``case`` or a line continuation still is."""
    assert (chains_followed_by_more([script]) != []) is refused


@pytest.mark.unit
@pytest.mark.parametrize(
    "script",
    [
        "echo 'unclosed",
        'echo "unclosed',
        "echo `unclosed",
        "echo )",
        "(echo unclosed",
        "echo $(unclosed",
        "echo ${unclosed",
        "echo $((1 + 2)",
        "cat <<EOF\nno terminator",
        "cat <<EOF",
        "cat <<",
    ],
)
def test_an_unreadable_script_is_a_failure_not_a_pass(script: str) -> None:
    with pytest.raises(ValueError):
        chains_followed_by_more([script, "make lint"])
