#!/usr/bin/env python3
"""
AWS CodeBuild buildspec.yml validator

This script validates AWS CodeBuild buildspec files for:
- Valid YAML syntax
- Required fields (version, phases)
- Correct structure and data types
- Common mistakes and best practices

Dependencies:
    PyYAML (install with: pip install pyyaml)

Usage:
    python3 scripts/sdlc/validate_buildspec.py <path-to-buildspec.yml>
    python3 scripts/sdlc/validate_buildspec.py patterns/*/buildspec.yml

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

    # A loop opening a statement, i.e. at the start of a line or straight after a
    # `;`/`&&`/`||`/`then`/`do`. `for` inside a string or a comment does not count.
    _LOOP = re.compile(
        r"(?:^|;|&&|\|\||\bthen\b|\bdo\b)\s*(for|while|until)\s", re.MULTILINE
    )
    # `set -e`, `set -eu`, `set -euo pipefail`, `set -o errexit`. A bare `-o
    # pipefail` is NOT errexit, so `-e` must appear as its own short flag or the
    # long option must be spelled out.
    _ERREXIT = re.compile(
        r"^\s*set\s+(?:-[a-zA-Z]*e[a-zA-Z]*\b|-o\s+errexit\b)", re.MULTILINE
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
        remedy is one line and the failure mode it prevents is a silent one. What
        it does not and cannot cover is a buildspec held inline in a
        CloudFormation ``BuildSpec`` property rather than in a file, since this
        validator is given files.
        """
        commands = phase_content.get("commands")
        if not isinstance(commands, list):
            return

        for idx, cmd in enumerate(commands, 1):
            if not isinstance(cmd, str) or "\n" not in cmd:
                # A single-line command is judged by CodeBuild on its own exit
                # status, so the phase already aborts on it.
                continue
            loop = self._LOOP.search(cmd)
            if not loop:
                continue
            errexit = self._ERREXIT.search(cmd)
            if errexit and errexit.start() < loop.start():
                continue
            self.errors.append(
                f"Phase '{phase_name}', command #{idx}: a multi-line command with a "
                f"'{loop.group(1)}' loop does not enable errexit, so the command's exit "
                "status is only the last iteration's and a failure in any earlier one is "
                "silently skipped (issue #1310). Add 'set -e' before the loop."
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
        print("       python3 validate_buildspec.py patterns/*/buildspec.yml")
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