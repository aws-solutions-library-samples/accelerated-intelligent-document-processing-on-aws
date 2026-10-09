#!/usr/bin/env python3
"""
AWS CodeBuild buildspec.yml validator

This script validates AWS CodeBuild buildspec files for:
- Valid YAML syntax
- Required fields (version, phases)
- Correct structure and data types
- Common mistakes and best practices
- A loop in a multi-line command that cannot report its own failure (issue #1310)

Dependencies:
    PyYAML (install with: pip install pyyaml)

Usage:
    python3 scripts/sdlc/validate_buildspec.py <path-to-buildspec.yml>
    make validate-buildspec          # every buildspec in the tree

Exit codes:
    0 - All buildspec files are valid
    1 - One or more buildspec files have errors
"""

import glob
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

try:
    import yaml
except ImportError:
    print("Error: PyYAML is not installed.")
    print("Install it with: pip install pyyaml")
    print("Or use the system Python with yaml pre-installed")
    sys.exit(1)


class BuildspecValidator:
    """Validator for AWS CodeBuild buildspec files"""

    SUPPORTED_VERSIONS = [0.1, 0.2]
    VALID_PHASES = [
        "install",
        "pre_build",
        "build",
        "post_build",
    ]
    PHASE_FIELDS = ["commands", "runtime-versions", "finally"]

    # A loop opening a statement. The prefix set is the point to get right: a
    # `while` fed by a PIPE (`cat list | while read f; do ...; done`) carries the
    # same defect and is the most natural way to write this loop, so `|` has to be
    # here, as do the subshell and group openers.
    #
    # A `for` in a comment does not match, because `#` displaces the `^` anchor.
    # A `for` opening a line INSIDE a double-quoted string does match, and that is
    # a deliberate false positive rather than a claim about strings: telling a
    # quoted continuation line from a statement needs a shell parser.
    _LOOP = re.compile(
        r"(?:^|[;&|({]|\bthen\b|\bdo\b)[ \t]*(for|while|until)\s", re.MULTILINE
    )
    # `set -e` in any option word of the command, so `set -o pipefail -e` counts,
    # as do `set -e`, `set -eu`, `set -euo pipefail` and `set -o errexit`. A bare
    # `-o pipefail` changes how a pipeline's status is computed and does NOT make
    # the shell exit, so it must not satisfy this. `[^#\n]*?` rather than
    # `[^\n]*?`, so a `-e` that only appears in a trailing comment --
    # `set -x  # remember -e someday` -- does not count as enabling it.
    _ERREXIT = re.compile(
        r"^[ \t]*set\s+(?:[^#\n]*?(?:(?<![-\w])-[a-zA-Z]*e[a-zA-Z]*(?![\w])|-o\s+errexit\b))",
        re.MULTILINE,
    )
    # `set +e` turns errexit back off. A check that only asked whether a `set -e`
    # precedes the loop would pass a block that re-disabled it in between, which is
    # the realistic way this protection gets removed later.
    _NO_ERREXIT = re.compile(
        r"^[ \t]*set\s+(?:[^#\n]*?(?:(?<![-\w])\+[a-zA-Z]*e[a-zA-Z]*(?![\w])|\+o\s+errexit\b))",
        re.MULTILINE,
    )
    # A heredoc body is another language's source as often as it is shell -- an
    # embedded Python `for i in range(3):` is not a shell loop and `set -e` is not
    # a remedy for it -- so bodies are blanked before scanning. The delimiter may
    # be quoted (`<<'EOF'`) and may be indented (`<<-`).
    #
    # ⚠️ `(?!<)` and the trailing `(?=[\s;)&|]|$)` are both load-bearing, and the
    # reason is that a FALSE heredoc match blanks every line after it, which turns
    # this whole check off for the rest of the command. `$(( a << b ))` must not
    # read as a heredoc -- arithmetic shift is the realistic case here, since
    # `sleep $((1 << attempt))` is the natural next edit to the retry ladder in
    # `buildspec.yml`, and landing it above the image loop would silence the gate
    # that protects that loop. `<<<` is a herestring and has no body at all.
    _HEREDOC_START = re.compile(
        r"<<-?(?!<)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1(?=[\s;)&|]|$)"
    )

    def __init__(self, filepath: str):
        self.filepath = Path(filepath)
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.buildspec: Dict[str, Any] = {}

    def validate(self) -> bool:
        """Run all validation checks. Returns True if valid."""
        try:
            # Load YAML
            with open(self.filepath, "r") as f:
                self.buildspec = yaml.safe_load(f)
        except yaml.YAMLError as e:
            self.errors.append(f"YAML parsing error: {e}")
            return False
        except Exception as e:
            self.errors.append(f"Error reading file: {e}")
            return False

        # Run validation checks
        self._validate_version()
        self._validate_phases()
        self._validate_env()
        self._validate_artifacts()

        return len(self.errors) == 0

    def _validate_version(self):
        """Validate version field"""
        if "version" not in self.buildspec:
            self.errors.append("Missing required 'version' field")
            return

        version = self.buildspec["version"]
        if version not in self.SUPPORTED_VERSIONS:
            self.errors.append(
                f"Invalid version '{version}'. Supported versions: {self.SUPPORTED_VERSIONS}"
            )

    def _validate_phases(self):
        """Validate phases section"""
        if "phases" not in self.buildspec:
            self.errors.append("Missing required 'phases' field")
            return

        phases = self.buildspec["phases"]
        if not isinstance(phases, dict):
            self.errors.append("'phases' must be a dictionary")
            return

        if len(phases) == 0:
            self.warnings.append("'phases' section is empty")

        # Validate each phase
        for phase_name, phase_content in phases.items():
            if phase_name not in self.VALID_PHASES:
                self.warnings.append(
                    f"Unknown phase '{phase_name}'. Valid phases: {self.VALID_PHASES}"
                )

            if not isinstance(phase_content, dict):
                self.errors.append(f"Phase '{phase_name}' must be a dictionary")
                continue

            # Validate phase content
            self._validate_phase_content(phase_name, phase_content)

    def _validate_phase_content(self, phase_name: str, phase_content: Dict):
        """Validate content within a phase"""
        # Check for commands
        if "commands" in phase_content:
            commands = phase_content["commands"]
            if not isinstance(commands, list):
                self.errors.append(f"Phase '{phase_name}': 'commands' must be a list")
            else:
                # Validate each command is a string
                for idx, cmd in enumerate(commands, 1):
                    if not isinstance(cmd, str):
                        self.errors.append(
                            f"Phase '{phase_name}', command #{idx} must be a string, got {type(cmd).__name__}"
                        )

        # Check that any loop runs under errexit
        self._validate_loops_fail_fast(phase_name, phase_content)

        # Check for unknown fields
        unknown_fields = set(phase_content.keys()) - set(self.PHASE_FIELDS)
        if unknown_fields:
            self.warnings.append(
                f"Phase '{phase_name}' has unknown fields: {', '.join(unknown_fields)}"
            )

    @classmethod
    def _strip_heredocs(cls, cmd: str) -> str:
        """Blank out heredoc bodies, keeping line numbering intact.

        Everything between `<<DELIM` and a line holding only `DELIM` is data, not
        shell, so neither a loop nor a `set -e` inside it means what it looks
        like. Lines are replaced rather than removed so a reported command index
        and any line number still line up.

        ⚠️ An UNTERMINATED heredoc is restored rather than blanked to the end of
        the command. Blanking it would switch this check off for every remaining
        line, so a pattern that matched a heredoc opener by mistake would silence
        the gate instead of merely mis-reading one line — which is the most
        expensive direction for a false positive here. Erring the other way costs
        at worst a false error on a genuinely unterminated heredoc, which is
        itself a broken buildspec.
        """
        out: List[str] = []
        lines = cmd.split("\n")
        delimiter: str = ""
        opened_at = -1
        for index, line in enumerate(lines):
            if delimiter:
                out.append("")
                if line.strip() == delimiter:
                    delimiter = ""
                continue
            out.append(line)
            match = cls._HEREDOC_START.search(line)
            if match:
                delimiter = match.group(2)
                opened_at = index
        if delimiter:
            # Never closed: put the lines back exactly as they were.
            out[opened_at + 1 :] = lines[opened_at + 1 :]
        return "\n".join(out)

    def _validate_loops_fail_fast(self, phase_name: str, phase_content: Dict):
        """A command containing a loop must enable errexit.

        CodeBuild aborts a phase when a command exits non-zero, but a multi-line
        command is ONE command and its exit status is that of the last statement
        it runs -- the last loop iteration. So a loop that builds and pushes N
        images reports only the Nth result, and a failure in any of the other N-1
        is skipped over silently: the phase succeeds, the build reports SUCCEEDED,
        and a consumer that expects all N artifacts to exist waits for one that was
        never produced. That is issue #1310, where the consumer is the
        ``DockerBuildRun`` custom resource and the wait is its 1-hour timeout.

        The rule is deliberately coarse -- a loop whose every statement is already
        ``||``-guarded does not need errexit and is flagged anyway -- because the
        remedy is one line and the failure mode it prevents is a silent one.

        Three bounds, stated so a green run is not over-read. It reads ``commands``
        and ``finally`` of each phase, which is every command list CodeBuild
        executes from a buildspec FILE; a buildspec held inline in a
        CloudFormation ``BuildSpec`` property is never opened by this validator.
        It is a regex rather than a shell parser, so a line opening with ``for``
        inside a quoted string is a false positive. And it checks that errexit is
        on *at the loop*, not that it stays on inside it: a ``set +e`` within the
        loop body is not detected.
        """
        for field in ("commands", "finally"):
            commands = phase_content.get(field)
            if not isinstance(commands, list):
                continue
            where = phase_name if field == "commands" else f"{phase_name}.finally"
            for idx, cmd in enumerate(commands, 1):
                if not isinstance(cmd, str) or "\n" not in cmd:
                    # A single-line command is judged by CodeBuild on its own exit
                    # status, so the phase already aborts on it.
                    continue
                self._check_one_command(where, idx, self._strip_heredocs(cmd))

    def _check_one_command(self, where: str, idx: int, cmd: str):
        """Judge EVERY loop in the command, not just the first.

        ⚠️ `finditer`, not `search`, and the difference is not academic: the
        #1310 fix added a `build_with_retry` helper whose `until` loop sits
        *above* the image `for` loop in all three buildspecs. A check that
        stopped at the first match therefore stopped reading before the loop
        this gate exists to protect, and a `set +e` inserted in between passed
        green. Measured on the shipped file before this changed.
        """
        for loop in self._LOOP.finditer(cmd):
            # The LAST `set` before THIS loop is the one in force, so take the
            # latest enabling and the latest disabling and compare them. Checking
            # only the first `set -e` would pass `set -e` ... `set +e` ... loop,
            # where errexit is demonstrably off for the loop.
            def last_before(pattern: "re.Pattern[str]", limit: int = loop.start()) -> int:
                return max(
                    (m.start() for m in pattern.finditer(cmd) if m.start() < limit),
                    default=-1,
                )

            enabled_at = last_before(self._ERREXIT)
            disabled_at = last_before(self._NO_ERREXIT)
            if enabled_at > disabled_at:
                continue

            why = (
                "re-disables errexit with 'set +e' before"
                if disabled_at > enabled_at >= 0
                else "does not enable errexit before"
            )
            line = cmd.count("\n", 0, loop.start()) + 1
            self.errors.append(
                f"Phase '{where}', command #{idx} (line {line}): a multi-line command "
                f"{why} a '{loop.group(1)}' loop, so the command's exit status is only "
                "the last iteration's and a failure in any earlier one is silently "
                "skipped (issue #1310). Add 'set -e' before the loop."
            )

    def _validate_env(self):
        """Validate env section if present"""
        if "env" not in self.buildspec:
            return

        env = self.buildspec["env"]
        if not isinstance(env, dict):
            self.errors.append("'env' must be a dictionary")
            return

        # Validate env subsections
        valid_env_sections = [
            "variables",
            "parameter-store",
            "secrets-manager",
            "exported-variables",
            "git-credential-helper",
        ]

        for section in env.keys():
            if section not in valid_env_sections:
                self.warnings.append(f"Unknown env section: '{section}'")

    def _validate_artifacts(self):
        """Validate artifacts section if present"""
        if "artifacts" not in self.buildspec:
            return

        artifacts = self.buildspec["artifacts"]
        if not isinstance(artifacts, dict):
            self.errors.append("'artifacts' must be a dictionary")
            return

        # Check for required fields in artifacts
        if "files" not in artifacts:
            self.warnings.append("'artifacts' section has no 'files' specified")

    def print_results(self):
        """Print validation results"""
        print(f"\nValidating: {self.filepath}")
        print("=" * 70)

        if self.errors:
            print(f"\n❌ ERRORS ({len(self.errors)}):")
            for error in self.errors:
                print(f"  - {error}")

        if self.warnings:
            print(f"\n⚠️  WARNINGS ({len(self.warnings)}):")
            for warning in self.warnings:
                print(f"  - {warning}")

        if not self.errors and not self.warnings:
            print("✅ Valid buildspec file")
        elif not self.errors:
            print("\n✅ Valid buildspec file (with warnings)")
        else:
            print("\n❌ Invalid buildspec file")

        # Print summary
        if self.buildspec.get("phases"):
            print("\nSummary:")
            print(f"  Version: {self.buildspec.get('version', 'N/A')}")
            print(f"  Phases: {', '.join(self.buildspec['phases'].keys())}")
            for phase, content in self.buildspec["phases"].items():
                if isinstance(content, dict) and "commands" in content:
                    cmd_count = len(content["commands"])
                    print(f"    - {phase}: {cmd_count} commands")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 validate_buildspec.py <buildspec-file>")
        print("       make validate-buildspec   (every buildspec in the tree)")
        sys.exit(1)

    # Expand glob patterns
    files = []
    for pattern in sys.argv[1:]:
        expanded = glob.glob(pattern, recursive=True)
        if expanded:
            files.extend(expanded)
        else:
            # Not a glob pattern, treat as regular file
            files.append(pattern)

    if not files:
        print("❌ No buildspec files found")
        sys.exit(1)

    all_valid = True
    validators = []

    for filepath in files:
        validator = BuildspecValidator(filepath)
        is_valid = validator.validate()
        validator.print_results()
        validators.append(validator)

        if not is_valid:
            all_valid = False

    # Print overall summary
    if len(validators) > 1:
        print("\n" + "=" * 70)
        print("OVERALL SUMMARY")
        print("=" * 70)
        valid_count = sum(1 for v in validators if not v.errors)
        print(f"Total files: {len(validators)}")
        print(f"Valid: {valid_count}")
        print(f"Invalid: {len(validators) - valid_count}")

    sys.exit(0 if all_valid else 1)


if __name__ == "__main__":
    main()