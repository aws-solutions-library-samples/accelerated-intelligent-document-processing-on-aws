#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Fail when the documentation says a retired AWS service is still in use.

When AppSync was replaced by an API Gateway REST API plus a dispatcher Lambda,
the templates were updated but roughly two dozen documents and code comments went
on asserting that AppSync was present and serving traffic. Nothing detected that,
because nothing was looking: removing a service is a template change, and the
prose describing it lives somewhere else entirely. A reader following those
documents looked for a GraphQL endpoint, a subscription, or an ``AppSyncVisibility``
parameter that no deployment has had for several releases.

This gate closes that loop. It reads ``scripts/sdlc/retired_services.json`` -- an
explicit registry of services this solution no longer uses -- and fails if a
scanned document mentions one outside the allowlist.

The allowlist is the substance of the check, not an escape hatch. A retired
service's name legitimately survives in two forms, and a blind find-and-replace
would destroy both:

* **Historical prose** that says what the architecture *used to* be, or names a
  retained artifact such as the GraphQL schema kept as a typed contract for UI
  codegen and dispatcher input validation.
* **Vestigial identifiers** -- a directory or logical id that keeps the old name
  for backward compatibility, or an IAM grant retained so an in-place upgrade from
  a pre-migration stack can still delete its own leftover resources.

Each allowlist entry therefore carries a ``bucket`` and a written
``justification``, following the precedent set by
``scripts/security/dep_audit_allowlist.json`` and ``scripts/srt/issues.json``.
Entries are ``(path, linePattern)`` pairs so that a file may be partly allowlisted:
``docs/rbac.md`` may keep its note about the retained schema directives while a
newly added claim that AppSync serves requests still fails.

Run directly (``make check-retired-services``) for a pass/fail gate;
``scripts/sdlc/tests/test_retired_services_not_documented.py`` wraps the same
functions and adds the guard-the-guard assertions.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = Path(__file__).resolve().parent / "retired_services.json"


@dataclass(frozen=True)
class Finding:
    """One line of documentation that names a retired service."""

    path: str
    lineno: int
    line: str
    service: str

    def render(self) -> str:
        return f"  {self.path}:{self.lineno}: {self.line.strip()}"


def load_registry(path: Path | None = None) -> dict:
    """Read and structurally validate the registry.

    Validation is deliberately strict: a typo that silently emptied
    ``scannedPaths`` or dropped ``retiredServices`` would turn this gate into a
    no-op that still reports success, which is worse than not having it.
    """
    registry_path = path or REGISTRY_PATH
    data = json.loads(registry_path.read_text(encoding="utf-8"))

    if not data.get("retiredServices"):
        raise ValueError(f"{registry_path.name}: 'retiredServices' is missing or empty")
    if not data.get("scannedPaths"):
        raise ValueError(f"{registry_path.name}: 'scannedPaths' is missing or empty")

    for service in data["retiredServices"]:
        for field in ("name", "pattern", "reason", "replacement"):
            if not service.get(field):
                raise ValueError(
                    f"{registry_path.name}: retired service "
                    f"{service.get('name', '<unnamed>')!r} is missing {field!r}"
                )

    for marker in data.get("historicalMarkers", []):
        if not marker.get("pattern") or not marker.get("justification"):
            raise ValueError(
                f"{registry_path.name}: every historicalMarkers entry needs both "
                f"'pattern' and 'justification'"
            )

    for entry in data.get("allowlist", []):
        for field in ("path", "linePattern", "bucket", "justification"):
            if not entry.get(field):
                raise ValueError(
                    f"{registry_path.name}: allowlist entry "
                    f"{entry.get('path', '<unnamed>')!r} is missing {field!r}"
                )
        if entry["bucket"] not in {"a", "b", "c"}:
            raise ValueError(
                f"{registry_path.name}: allowlist entry {entry['path']!r} has "
                f"bucket {entry['bucket']!r}; expected 'a', 'b' or 'c'"
            )

    for excluded in data.get("excludedPaths", []):
        if not excluded.get("glob") or not excluded.get("justification"):
            raise ValueError(
                f"{registry_path.name}: every excludedPaths entry needs both "
                f"'glob' and 'justification'"
            )

    return data


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    """Translate a POSIX-style path glob to a regex, honouring ``**``.

    ``Path.full_match`` would do this, but it is Python 3.13+ and this repo
    targets 3.12; ``fnmatch`` does not distinguish ``*`` from ``**``, which
    matters because ``workshop/**`` must match a nested file while ``*.md`` must
    match only files at the root.
    """
    out: list[str] = []
    index = 0
    while index < len(glob):
        char = glob[index]
        if glob.startswith("**/", index):
            # Zero or more leading directories.
            out.append("(?:[^/]+/)*")
            index += 3
        elif glob.startswith("**", index):
            out.append(".*")
            index += 2
        elif char == "*":
            out.append("[^/]*")
            index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(char))
            index += 1
    return re.compile("".join(out) + r"\Z")


def _is_excluded(rel: str, registry: dict) -> bool:
    return any(
        _glob_to_regex(excluded["glob"]).match(rel)
        for excluded in registry.get("excludedPaths", [])
    )


def scanned_files(registry: dict, root: Path = REPO_ROOT) -> list[Path]:
    """Every documentation file the gate enforces, deduplicated and sorted."""
    found: set[Path] = set()
    for pattern in registry["scannedPaths"]:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            if _is_excluded(path.relative_to(root).as_posix(), registry):
                continue
            found.add(path)
    return sorted(found)


def _allowlist_matchers(registry: dict) -> list[tuple[dict, re.Pattern[str]]]:
    return [
        (entry, re.compile(entry["linePattern"], re.IGNORECASE))
        for entry in registry.get("allowlist", [])
    ]


def marker_matchers(registry: dict) -> list[tuple[dict, re.Pattern[str]]]:
    return [
        (marker, re.compile(marker["pattern"], re.IGNORECASE))
        for marker in registry.get("historicalMarkers", [])
    ]


#: How many neighbouring lines a historical marker may reach.
#:
#: Documentation prose wraps at roughly 80 columns, so the qualifier that makes a
#: sentence historical ("...which has since been removed") routinely lands on the
#: line after the service name. Judging each line in isolation reported those
#: continuations as stale and would have pushed a dozen fragments of correct
#: sentences into the allowlist. One line either side covers the wrap without
#: reaching into an unrelated paragraph.
MARKER_WINDOW = 1


def reads_as_historical(
    lines: list[str], index: int, registry: dict, window: int = MARKER_WINDOW
) -> bool:
    """True when this line or an immediate neighbour marks the mention historical."""
    start = max(0, index - window)
    context = "\n".join(lines[start : index + window + 1])
    return any(matcher.search(context) for _, matcher in marker_matchers(registry))


def find_violations(
    registry: dict, root: Path = REPO_ROOT
) -> tuple[list[Finding], set[int]]:
    """Scan for un-allowlisted mentions of a retired service.

    Returns the findings plus the indices of the allowlist entries that matched
    something, so a caller can report allowlist entries that no longer apply.
    """
    services = [
        (service, re.compile(service["pattern"], re.IGNORECASE))
        for service in registry["retiredServices"]
    ]
    allowlist = _allowlist_matchers(registry)
    markers = marker_matchers(registry)

    findings: list[Finding] = []
    used: set[int] = set()

    for path in scanned_files(registry, root):
        rel = path.relative_to(root).as_posix()
        applicable = [
            (index, matcher)
            for index, (entry, matcher) in enumerate(allowlist)
            if entry["path"] == rel
        ]
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # pragma: no cover - documentation is UTF-8
            continue

        lines = text.splitlines()
        for offset, line in enumerate(lines):
            for service, service_matcher in services:
                if not service_matcher.search(line):
                    continue
                start = max(0, offset - MARKER_WINDOW)
                context = "\n".join(lines[start : offset + MARKER_WINDOW + 1])
                allowed = any(matcher.search(context) for _, matcher in markers)
                for index, matcher in applicable:
                    if matcher.search(line):
                        used.add(index)
                        allowed = True
                if not allowed:
                    findings.append(Finding(rel, offset + 1, line, service["name"]))

    return findings, used


def stale_allowlist_entries(registry: dict, used: set[int]) -> list[dict]:
    """Allowlist entries that matched nothing, so the exemption is now dead.

    An exemption nobody needs is worse than none: it is a standing licence to
    reintroduce the claim it was written to excuse.
    """
    return [
        entry
        for index, entry in enumerate(registry.get("allowlist", []))
        if index not in used
    ]


def unexpected_resources(registry: dict, root: Path = REPO_ROOT) -> list[str]:
    """Templates declaring a resource type the registry says no longer exists.

    If someone reintroduces the service, this gate's premise is void and it must
    say so loudly rather than keep policing prose that has become correct again.
    """
    wanted = {
        resource_type: service["name"]
        for service in registry["retiredServices"]
        for resource_type in service.get("absentResourceTypes", [])
    }
    if not wanted:
        return []

    hits: list[str] = []
    for pattern in ("*.yaml", "*.yml", "*.json"):
        for path in root.rglob(pattern):
            rel = path.relative_to(root).as_posix()
            if "/node_modules/" in rel or rel.startswith(
                (".aws-sam/", ".git/", "node_modules/", "workshop/")
            ):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            # Identify templates by CONTENT, the way `make cfn-lint` does, so a
            # new template cannot dodge this check by being named something
            # unexpected -- and so this registry, which necessarily *names* the
            # retired resource types, is not mistaken for a template.
            if "AWSTemplateFormatVersion" not in text:
                continue
            for resource_type, service_name in wanted.items():
                if resource_type in text:
                    hits.append(f"{rel}: declares {resource_type} ({service_name})")
    return sorted(set(hits))


def _report(registry: dict, findings: list[Finding], stale: list[dict]) -> None:
    by_service: dict[str, list[Finding]] = {}
    for finding in findings:
        by_service.setdefault(finding.service, []).append(finding)

    details = {service["name"]: service for service in registry["retiredServices"]}

    for name, service_findings in sorted(by_service.items()):
        service = details[name]
        print(
            f"\n{name} was removed"
            + (f" in {service['removedIn']}" if service.get("removedIn") else "")
            + f", but {len(service_findings)} documentation line(s) still present it "
            f"as part of the architecture:\n",
            file=sys.stderr,
        )
        for finding in service_findings:
            print(finding.render(), file=sys.stderr)
        print(f"\n  Why it was removed: {service['reason']}", file=sys.stderr)
        print(f"  What replaced it:   {service['replacement']}", file=sys.stderr)
        if service.get("documentation"):
            print(f"  Reference:          {service['documentation']}", file=sys.stderr)

    if stale:
        print(
            "\nAllowlist entries in scripts/sdlc/retired_services.json that no "
            "longer match anything. The text they excused is gone, so delete the "
            "entry -- leaving it standing re-permits the claim:\n",
            file=sys.stderr,
        )
        for entry in stale:
            print(
                f"  {entry['path']} (linePattern {entry['linePattern']!r})",
                file=sys.stderr,
            )

    if findings:
        print(
            "\nFix each line to describe the current architecture. If a line is "
            "correct because it is explicitly historical, or because it names an "
            "identifier or IAM grant deliberately kept for backward "
            "compatibility, add it to the 'allowlist' in "
            "scripts/sdlc/retired_services.json with its bucket and a written "
            "justification.",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--registry",
        type=Path,
        default=REGISTRY_PATH,
        help="path to the retired-services registry (default: %(default)s)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="repository root to scan (default: the checkout containing this file)",
    )
    args = parser.parse_args(argv)

    registry = load_registry(args.registry)
    root = args.root.resolve()

    reintroduced = unexpected_resources(registry, root)
    if reintroduced:
        print(
            "A service the registry lists as retired is declared by a template "
            "again, so scripts/sdlc/retired_services.json is now wrong:\n",
            file=sys.stderr,
        )
        for hit in reintroduced:
            print(f"  {hit}", file=sys.stderr)
        print(
            "\nEither remove the resource or remove the service from the "
            "registry -- the documentation it polices is no longer stale.",
            file=sys.stderr,
        )
        return 1

    files = scanned_files(registry, root)
    findings, used = find_violations(registry, root)
    stale = stale_allowlist_entries(registry, used)

    if findings or stale:
        _report(registry, findings, stale)
        return 1

    services = ", ".join(service["name"] for service in registry["retiredServices"])
    print(
        f"No documentation presents a retired service as current "
        f"({len(files)} files scanned; retired: {services})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
