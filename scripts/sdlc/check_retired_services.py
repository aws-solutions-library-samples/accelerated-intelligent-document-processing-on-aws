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
from typing import NamedTuple

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


def _release_number(version: str) -> tuple[int, ...]:
    """The leading dotted-integer part of a version string.

    ``VERSION`` carries a development suffix between releases (``0.6.9.dev3``),
    and a pre-release build of 0.6.9 must not be treated as being past 0.6.9.
    Taking only the leading numeric components gives ``(0, 6, 9)`` for both
    ``0.6.9`` and ``0.6.9.dev3``, so an entry pinned to 0.6.9 survives the whole
    0.6.9 development cycle and dies when the number is bumped.
    """
    parts: list[int] = []
    for piece in version.strip().split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    if not parts:
        raise ValueError(f"cannot read a release number from version {version!r}")
    return tuple(parts)


def current_version(root: Path = REPO_ROOT) -> str:
    """The repository's declared version, from the ``VERSION`` file."""
    return (root / "VERSION").read_text(encoding="utf-8").strip()


def _require_text(
    registry_name: str, kind: str, label: str, entry: dict, field: str
) -> None:
    """Demand a non-blank string in ``entry[field]``.

    ``if not entry.get(field)`` catches a missing or empty field but accepts
    ``" "``, so an allowlist entry could carry a justification made entirely of
    whitespace and pass validation. The justification is the only thing a
    reviewer has to argue with, so a blank one is the same defect as no
    justification at all and is rejected the same way.
    """
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{registry_name}: {kind} {label!r} has a missing, empty or "
            f"whitespace-only {field!r}"
        )


def load_registry(path: Path | None = None, version: str | None = None) -> dict:
    """Read and structurally validate the registry.

    Validation is deliberately strict: a typo that silently emptied
    ``scannedPaths`` or dropped ``retiredServices`` would turn this gate into a
    no-op that still reports success, which is worse than not having it.

    ``version`` defaults to the repository's ``VERSION`` file and exists so the
    tests can drive ``expiresAfterVersion`` in both directions without editing
    that file.
    """
    registry_path = path or REGISTRY_PATH
    data = json.loads(registry_path.read_text(encoding="utf-8"))

    if not data.get("retiredServices"):
        raise ValueError(f"{registry_path.name}: 'retiredServices' is missing or empty")
    if not data.get("scannedPaths"):
        raise ValueError(f"{registry_path.name}: 'scannedPaths' is missing or empty")

    for service in data["retiredServices"]:
        for field in ("name", "pattern", "reason", "replacement"):
            _require_text(
                registry_path.name,
                "retired service",
                service.get("name", "<unnamed>"),
                service,
                field,
            )

    for marker in data.get("historicalMarkers", []):
        for field in ("pattern", "justification"):
            _require_text(
                registry_path.name,
                "historicalMarkers entry",
                marker.get("pattern", "<unnamed>"),
                marker,
                field,
            )

    release = _release_number(version if version is not None else current_version())

    for entry in data.get("allowlist", []):
        for field in ("path", "linePattern", "bucket", "justification"):
            _require_text(
                registry_path.name,
                "allowlist entry",
                entry.get("path", "<unnamed>"),
                entry,
                field,
            )
        if "expires" in entry:
            # The original form of this field was a free-text condition
            # ('when #937 merges') that nothing read, so it expired only if a
            # human happened to notice it. Reject the key outright rather than
            # leave two spellings, one of which is decoration.
            raise ValueError(
                f"{registry_path.name}: allowlist entry {entry['path']!r} uses "
                f"'expires', which nothing enforces. Use 'expiresAfterVersion' "
                f"with a release number, which load_registry checks against the "
                f"VERSION file."
            )
        deadline = entry.get("expiresAfterVersion")
        if deadline is not None:
            _require_text(
                registry_path.name,
                "allowlist entry",
                entry["path"],
                entry,
                "expiresAfterVersion",
            )
            if release > _release_number(deadline):
                raise ValueError(
                    f"{registry_path.name}: allowlist entry {entry['path']!r} "
                    f"expired after version {deadline} and this tree is "
                    f"{'.'.join(str(part) for part in release)}. Either the "
                    f"underlying claim was fixed and the entry should be "
                    f"deleted, or it was not and the deadline must be moved "
                    f"deliberately, with the reason recorded in the "
                    f"justification."
                )
        if entry["bucket"] not in {"a", "b", "c"}:
            raise ValueError(
                f"{registry_path.name}: allowlist entry {entry['path']!r} has "
                f"bucket {entry['bucket']!r}; expected 'a', 'b' or 'c'"
            )

    for excluded in data.get("excludedPaths", []):
        for field in ("glob", "justification"):
            _require_text(
                registry_path.name,
                "excludedPaths entry",
                excluded.get("glob", "<unnamed>"),
                excluded,
                field,
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


#: Where one sentence ends and the next begins, for the purpose of deciding which
#: text a historical marker governs.
#:
#: The lookbehind requires terminal punctuation and the lookahead requires the next
#: character to open a new sentence, which keeps the constructs that pepper this
#: repo's prose intact: ``0.6.0`` and ``idp_common.appsync`` have no whitespace
#: after the dot, and a trailing ``.`` inside a code span is not followed by a
#: capital. Splitting slightly too eagerly is the safe direction -- it can only
#: narrow the text a marker reaches, so it can only produce a finding a human then
#: reads, never a silent exemption.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`*_#\[(-])")


def sentence_around(text: str, column: int = 0) -> str:
    """The one sentence within ``text`` that contains ``column``.

    Markers are judged against this. See :func:`reads_as_historical`.
    """
    start, end = 0, len(text)
    for match in _SENTENCE_BREAK.finditer(text):
        if match.start() <= column:
            start = match.end()
        else:
            end = match.start()
            break
    return text[start:end]


#: Lines that stand alone: nothing continues into them and nothing continues out
#: of them. A heading, a table row and a thematic break are complete by
#: construction, so gluing one to its neighbour would let a "what changed"
#: paragraph exempt the stale heading above it -- one of the two adjacencies that
#: made the old line window unsafe.
_CLOSED_LINE = re.compile(
    r"""^[ \t]*(?:
        \#{1,6}[ \t]                           # ATX heading
      | \|                                     # table row or table rule
      | [-*_][ \t]*[-*_][ \t]*[-*_][-*_ \t]*$  # thematic break
    )""",
    re.VERBOSE,
)
_FENCE = re.compile(r"^[ \t]*(?:```|~~~)")
_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]")
_QUOTE = re.compile(r"^[ \t]*>[ \t]?")


class _Scope(NamedTuple):
    """The reconstructed prose a line participates in."""

    #: The line's whole block, with wrapped lines rejoined by single spaces.
    text: str
    #: Where this line's own content starts inside :attr:`text`.
    offset: int
    #: How many leading characters of the raw line were dropped (indentation, and
    #: a blockquote's ``>`` marker), so a column in the raw line can be mapped in.
    dropped: int


def _continues(current: str, opener: str, previous: str) -> bool:
    """Whether ``current`` is a wrapped continuation of the block ``opener``."""
    if not current.strip():
        return False
    if _CLOSED_LINE.match(current) or _FENCE.match(current):
        return False
    if previous.endswith("  "):  # Markdown hard line break: a deliberate stop.
        return False
    if opener == "quote":
        return bool(_QUOTE.match(current))
    if _QUOTE.match(current) or _LIST_ITEM.match(current):
        # A new bullet, or a quote opening under a paragraph, is a new statement.
        return False
    if opener == "list":
        # Only an *indented* line continues a bullet. An unindented line below a
        # bullet is a new paragraph, and treating it as a continuation is exactly
        # how a false claim would inherit the next line's "no longer".
        return current[:1].isspace()
    return True


def sentence_scopes(lines: list[str]) -> list[_Scope]:
    """Group ``lines`` into blocks so a wrapped sentence can be read as one.

    This is the part that has to be right. A historical marker must govern the
    *sentence* containing the mention -- no more and no less. Prose in this repo
    wraps at roughly 80 columns, so "...which has since been removed" genuinely
    does land on the following line, and 16 correct historical sentences in the
    current corpus are split that way. Refusing to rejoin them would report all 16
    and push accurate documentation into the allowlist.

    Rejoining is therefore necessary, but it must not become the old line window
    by another route. A block ends at a blank line, a heading, a table row, a
    thematic break, a fence, a new bullet, and an unindented line under a bullet.
    Within the rejoined block, :func:`sentence_around` then confines the marker to
    one sentence, so a historical note in the *next* sentence -- whether it wrapped
    onto another line or not -- exempts nothing.
    """
    scopes: list[_Scope] = []
    for line in lines:
        scopes.append(_Scope(line.strip(), 0, len(line) - len(line.lstrip())))

    index = 0
    in_fence = False
    while index < len(lines):
        line = lines[index]
        if _FENCE.match(line):
            in_fence = not in_fence
            index += 1
            continue
        if in_fence or not line.strip() or _CLOSED_LINE.match(line):
            index += 1
            continue

        if _QUOTE.match(line):
            opener = "quote"
        elif _LIST_ITEM.match(line):
            opener = "list"
        else:
            opener = "paragraph"

        members = [index]
        cursor = index + 1
        while cursor < len(lines) and _continues(
            lines[cursor], opener, lines[cursor - 1]
        ):
            members.append(cursor)
            cursor += 1

        pieces: list[str] = []
        starts: list[int] = []
        drops: list[int] = []
        position = 0
        for member in members:
            raw = lines[member]
            quote = _QUOTE.match(raw) if opener == "quote" else None
            body = raw[quote.end() :] if quote else raw
            content = body.strip()
            drops.append(len(raw) - len(body) + (len(body) - len(body.lstrip())))
            starts.append(position)
            pieces.append(content)
            position += len(content) + 1

        text = " ".join(pieces)
        for member, start, dropped in zip(members, starts, drops):
            scopes[member] = _Scope(text, start, dropped)
        index = cursor

    return scopes


def reads_as_historical(
    lines: list[str],
    index: int,
    registry: dict,
    column: int = 0,
    scopes: list[_Scope] | None = None,
) -> bool:
    """True when the *same sentence* as the mention marks it historical.

    A historical marker governs one sentence. Wrapped lines are rejoined first, so
    the sentence may span a line break -- but it stops at the sentence boundary, so
    a historical note in the *neighbouring* sentence exempts nothing.

    This used to consult a +/-1 *line* window instead, on the reasoning that prose
    wraps at roughly 80 columns so "...which has since been removed" often lands on
    the following line. That reasoning was sound; the implementation was far wider
    than the reason required. Because the window was symmetric and unconditional,
    every genuine historical sentence in the corpus silently exempted the whole line
    above and the whole line below it, so a false present-tense claim merely had to
    sit next to a correct historical note to pass the gate. Two of this repo's
    commonest document shapes put exactly that adjacency in front of it:
    consecutive bullets in an architecture list, and a "what changed" paragraph
    directly under a heading that still described the old design.

    Scoping to the sentence keeps the wrap tolerance the window was actually for
    and drops the reach it was not. ``scopes`` may be passed to reuse one
    reconstruction across every mention in a file; it is otherwise derived here so
    that the gate and its tests cannot exercise different rules.
    """
    if not 0 <= index < len(lines):
        return False
    scope = (scopes or sentence_scopes(lines))[index]
    position = scope.offset + max(0, column - scope.dropped)
    sentence = sentence_around(scope.text, position)
    return any(matcher.search(sentence) for _, matcher in marker_matchers(registry))


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
        scopes = sentence_scopes(lines)
        for offset, line in enumerate(lines):
            for service, service_matcher in services:
                mentions = list(service_matcher.finditer(line))
                if not mentions:
                    continue

                # An allowlist entry exempts the whole line, because its
                # linePattern is written against the line's literal text.
                exempted_by_allowlist = False
                for index, matcher in applicable:
                    if matcher.search(line):
                        used.add(index)
                        exempted_by_allowlist = True
                if exempted_by_allowlist:
                    continue

                # Every mention on the line must sit in a sentence that marks it
                # historical. Checking only the first would let a stale claim ride
                # along behind a correct historical clause earlier on the line.
                if all(
                    reads_as_historical(
                        lines, offset, registry, mention.start(), scopes
                    )
                    for mention in mentions
                ):
                    continue

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


def _declaration_matcher(resource_type: str) -> re.Pattern[str]:
    """Match an actual ``Type:`` declaration of ``resource_type``, not a mention.

    This check used to substring-search the whole file, comments included, which
    made merely *naming* a retired type in a YAML comment report the service as
    reintroduced. That is not a hypothetical: the comment explaining why the
    legacy ``appsync:*`` grant is retained in
    ``iam-roles/cloudformation-management/IDP-Cloudformation-Service-Role.yaml``
    tripped it, and the gate's advice ("the registry is now wrong") pointed the
    reader at the wrong file entirely. Anchoring on ``Type:`` is also simply what
    the check means -- a resource exists because a template declares it -- so it
    survives comment styles nobody anticipated.

    Both YAML (``Type: AWS::AppSync::GraphQLApi``) and JSON
    (``"Type": "AWS::AppSync::GraphQLApi"``) block form are covered. YAML flow
    form (``{Type: AWS::AppSync::GraphQLApi}``) is not; no template in this repo
    declares resources that way, and ``make cfn-lint`` covers template validity
    independently.
    """
    return re.compile(
        rf"""^[ \t]*"?Type"?[ \t]*:[ \t]*["']?{re.escape(resource_type)}\b""",
        re.MULTILINE,
    )


def unexpected_resources(registry: dict, root: Path = REPO_ROOT) -> list[str]:
    """Templates declaring a resource type the registry says no longer exists.

    If someone reintroduces the service, this gate's premise is void and it must
    say so loudly rather than keep policing prose that has become correct again.
    """
    wanted = {
        _declaration_matcher(resource_type): (resource_type, service["name"])
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
            for matcher, (resource_type, service_name) in wanted.items():
                if matcher.search(text):
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

    # The reintroduction check no longer short-circuits the prose scan. It used to
    # return here, so a single false positive in it -- and before the ``Type:``
    # anchoring below it was easy to provoke with a comment -- hid every real
    # documentation finding in the run, and the reader had to fix the phantom and
    # re-run before learning what else was wrong. Both halves now always run and
    # the exit status is the union.
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

    files = scanned_files(registry, root)
    findings, used = find_violations(registry, root)
    stale = stale_allowlist_entries(registry, used)

    if findings or stale:
        _report(registry, findings, stale)
    if findings or stale or reintroduced:
        return 1

    services = ", ".join(service["name"] for service in registry["retiredServices"])
    print(
        f"No documentation presents a retired service as current "
        f"({len(files)} files scanned; retired: {services})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
