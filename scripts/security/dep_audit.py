#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Dependency vulnerability gate (SCA) for CI.

WHY THIS EXISTS
---------------
The SRT scan that gates this repo (``make srt-scan``) runs checkov, semgrep,
bandit and syft. Syft builds an SBOM — an *inventory* — and does no
vulnerability matching, so SRT cannot fail a build for a known-vulnerable
dependency. Nothing else in CI did either, which meant a HIGH CVE in a pinned
package could merge unnoticed. This closes that gap.

HOW IT WORKS
------------
``scripts/generate-dep-manifest.sh`` already resolves every Python and Node
dependency in the repo — across ``uv.lock``, the ``package-lock.json`` files and
every ``requirements.txt`` — into two pinned manifests. That is exactly the
input an SCA pass needs, so this script reuses it rather than re-deriving the
dependency graph, then matches each ``name==version`` against the OSV database
(https://osv.dev), the same advisory source that backs grype and pip-audit.

Findings at or above the severity threshold fail the run unless allowlisted in
``scripts/security/dep_audit_allowlist.json`` — the same
triage-with-justification pattern ``scripts/srt/issues.json`` uses for SRT.

USAGE
-----
    python3 scripts/security/dep_audit.py                  # generate + audit
    python3 scripts/security/dep_audit.py --no-generate    # reuse manifests
    python3 scripts/security/dep_audit.py --severity CRITICAL
    python3 scripts/security/dep_audit.py --json report.json
    python3 scripts/security/dep_audit.py --no-unmapped    # skip the GitHub pass

Exits 1 when un-allowlisted findings remain at/above the threshold, or when an
allowlist entry matched no finding at all (a dead entry pre-approves its
advisory id the day it comes back, so it must be deleted), 0 otherwise,
and **2 whenever the audit could not actually be performed** — an audit that did
not run must never look like an audit that passed. Exit 2 covers: OSV unreachable,
a required tool missing from PATH (``REQUIRED_TOOLS``), the generator failing, and
either manifest coming back with no packages (an empty manifest audits clean,
which is this gate's most dangerous failure mode — see ``REQUIRED_TOOLS``).

KNOWN LIMITS
------------
- **Severity comes from GitHub's qualitative label.** A record carrying only a
  CVSS vector is reported as ``UNKNOWN-CVSS`` and ranks BELOW the gate, so it is
  surfaced but does not fail the build. In practice such records (e.g. ``PYSEC-*``)
  are aliases of a ``GHSA-*`` that does carry a label, and the labelled one gates —
  but a CVSS-only advisory with no GHSA twin would only be reported. Read the
  informational section, don't just trust the exit code.
- **No reachability analysis.** A finding means the version is affected, not that
  the vulnerable code path is used. That judgement belongs in the allowlist
  reason (see the ``bedrock-agentcore`` entry).
- **Version matching is OSV's, not ours.** Advisories with no ``fixed`` event and
  an open-ended range match every version; see ``last_known_affected()``.
- **Unmapped advisories are reported, never gated.** GitHub publishes many
  advisories as *unreviewed* with no affected package attached (a CVE mirrored
  before anyone has mapped it). OSV, ``npm audit`` and Dependabot all match by
  package and version, so none of them can report one; a commercial scanner that
  maps CVEs itself can. ``match_unmapped()`` closes most of that gap by reading
  the advisory summary — "mammoth (aka mammoth.js) before 1.12.2 ...",
  "http-cache-semantics through 4.2.0 ..." — but that is text matching, so its
  output is a review list, not a verdict. It reads the last ``UNMAPPED_DAYS`` of
  advisories only, so one that stays unmapped longer than that drops out of the
  report. It needs a GitHub token (the 60-request unauthenticated limit is far
  below what one run uses); without one the section says it was skipped.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv, runs the repo's own manifest script
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate-dep-manifest.sh"
MANIFEST_DIR = REPO_ROOT / "dist" / "manifests"
PYTHON_MANIFEST = MANIFEST_DIR / "python-packages.txt"
NODE_MANIFEST = MANIFEST_DIR / "node-packages.txt"
ALLOWLIST = Path(__file__).resolve().parent / "dep_audit_allowlist.json"

OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
GHSA_URL = "https://api.github.com/advisories"
OSV_VULN_URL = "https://api.osv.dev/v1/vulns/"

# OSV's batch endpoint returns ids only, so severities come from a second
# per-vulnerability lookup. Keep batches modest to stay well inside OSV limits.
BATCH_SIZE = 500
RANK = {"CRITICAL": 4, "HIGH": 3, "MODERATE": 2, "MEDIUM": 2, "LOW": 1, "": 0}

# GitHub publishes roughly 500 unreviewed advisories a day, so 60 days is about
# 330 paged requests. One day per worker keeps the pass under half a minute.
UNMAPPED_DAYS = 60
UNMAPPED_WORKERS = 8


def _http_post_json(
    url: str, payload: Dict[str, Any], retries: int = 3
) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(  # nosec B310 - fixed https OSV endpoint
                url, data=body, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"OSV request failed after {retries} attempts: {last}")


def _http_get_json(url: str, retries: int = 3) -> Dict[str, Any]:
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:  # nosec B310 - fixed https OSV endpoint
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"OSV request failed after {retries} attempts: {last}")


def parse_python_manifest(path: Path) -> List[Tuple[str, str, str]]:
    """Return (ecosystem, name, version) for each ``name==version`` line."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, _, version = line.partition("==")
        # Drop environment markers / extras if a line ever carries them.
        version = version.split(";")[0].strip()
        name = name.split("[")[0].strip()
        if name and version:
            out.append(("PyPI", name, version))
    return out


def parse_node_manifest(path: Path) -> List[Tuple[str, str, str]]:
    """Return (ecosystem, name, version) for each ``name@version`` line.

    Scoped packages (``@scope/pkg@1.2.3``) mean the separator is the LAST ``@``.
    """
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        idx = line.rfind("@")
        if idx <= 0:
            continue
        name, version = line[:idx], line[idx + 1 :]
        if name and version:
            out.append(("npm", name, version))
    return out


def query_osv(packages: List[Tuple[str, str, str]]) -> Dict[str, List[str]]:
    """Map ``ecosystem|name|version`` -> list of OSV vulnerability ids."""
    hits: Dict[str, List[str]] = {}
    for start in range(0, len(packages), BATCH_SIZE):
        chunk = packages[start : start + BATCH_SIZE]
        payload = {
            "queries": [
                {"package": {"name": name, "ecosystem": eco}, "version": version}
                for eco, name, version in chunk
            ]
        }
        result = _http_post_json(OSV_BATCH_URL, payload)
        for (eco, name, version), res in zip(chunk, result.get("results", [])):
            ids = [v["id"] for v in (res or {}).get("vulns", []) or []]
            if ids:
                hits[f"{eco}|{name}|{version}"] = ids
        print(
            f"  queried {min(start + BATCH_SIZE, len(packages))}/{len(packages)}"
            f" packages, {len(hits)} vulnerable so far",
            flush=True,
        )
    return hits


def severity_of(vuln: Dict[str, Any]) -> str:
    """Best-effort severity label for an OSV record."""
    sev = (vuln.get("database_specific") or {}).get("severity")
    if isinstance(sev, str) and sev:
        return sev.upper()
    # Fall back to a CVSS vector's own qualitative band where GitHub didn't
    # supply one. Anything unrecognised stays "" and is reported, not gated.
    for entry in vuln.get("severity") or []:
        score = entry.get("score", "")
        if score.startswith("CVSS"):
            return "UNKNOWN-CVSS"
    return ""


def fixed_versions(vuln: Dict[str, Any], name: str) -> List[str]:
    out = set()
    for affected in vuln.get("affected") or []:
        if (affected.get("package") or {}).get("name", "").lower() != name.lower():
            continue
        for rng in affected.get("ranges") or []:
            for event in rng.get("events") or []:
                if "fixed" in event:
                    out.add(event["fixed"])
    return sorted(out)


def last_known_affected(vuln: Dict[str, Any], name: str) -> str:
    """GitHub's ``last_known_affected_version_range``, when present.

    Some advisories carry no ``fixed`` event at all — typically because the fix
    shipped outside the package registry, so the range is open-ended
    (``introduced: 0`` and nothing else) and EVERY version matches. SheetJS is
    the canonical case: ``xlsx`` on npm is abandoned and the patched builds live
    on cdn.sheetjs.com, which OSV cannot express. Surfacing this field lets a
    reviewer see immediately that e.g. "< 0.20.2" does not include the pinned
    0.20.2, instead of chasing a fix that npm will never have.
    """
    for affected in vuln.get("affected") or []:
        if (affected.get("package") or {}).get("name", "").lower() != name.lower():
            continue
        rng = (affected.get("database_specific") or {}).get(
            "last_known_affected_version_range"
        )
        if rng:
            return str(rng)
    return ""


def load_allowlist() -> Dict[str, Dict[str, Any]]:
    if not ALLOWLIST.exists():
        return {}
    entries = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    out = {}
    for e in entries:
        # A key of "<id>" suppresses the advisory everywhere; "<id>|<package>"
        # scopes it to one package.
        out[e["id"] if not e.get("package") else f"{e['id']}|{e['package']}"] = e
    return out


def allowlist_key(
    allowlist: Dict[str, Dict[str, Any]], vid: str, name: str
) -> Optional[str]:
    """The allowlist key that suppresses ``vid`` on ``name``, if any."""
    for key in (f"{vid}|{name}", vid):
        if key in allowlist:
            return key
    return None


def is_allowlisted(
    allowlist: Dict[str, Dict[str, Any]], vid: str, name: str
) -> Optional[Dict[str, Any]]:
    key = allowlist_key(allowlist, vid, name)
    return allowlist[key] if key else None


def github_token() -> Optional[str]:
    """A token for the GitHub advisory API: the environment, else ``gh``."""
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    if shutil.which("gh"):
        proc = subprocess.run(  # nosec B603 B607 - fixed argv
            ["gh", "auth", "token"], capture_output=True, text=True
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return None


def _ghsa_page(url: str, token: str, retries: int = 3) -> Tuple[List[Any], str]:
    """One page of the advisory API, plus the URL of the next page ("" at the end)."""
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(  # nosec B310 - fixed https GitHub endpoint
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310
                link = resp.headers.get("Link", "") or ""
                nxt = re.search(r'<([^>]+)>;\s*rel="next"', link)
                return json.loads(resp.read().decode("utf-8")), (
                    nxt.group(1) if nxt else ""
                )
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(
        f"GitHub advisory request failed after {retries} attempts: {last}"
    )


def fetch_unreviewed(
    token: str, days: int = UNMAPPED_DAYS, today: Optional[dt.date] = None
) -> List[Dict[str, Any]]:
    """Every unreviewed GitHub advisory published in the last ``days`` days."""
    today = today or dt.datetime.now(dt.timezone.utc).date()

    def one_day(day: dt.date) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        url = f"{GHSA_URL}?type=unreviewed&published={day.isoformat()}&per_page=100"
        while url:
            page, url = _ghsa_page(url, token)
            out.extend(page)
        return out

    with ThreadPoolExecutor(UNMAPPED_WORKERS) as pool:
        days_list = [today - dt.timedelta(days=i) for i in range(days)]
        return [adv for chunk in pool.map(one_day, days_list) for adv in chunk]


# "<name> [(aka ...)] [package] [versions] [from <lo>] before|through|<= <hi>" at the
# start of an advisory summary. Anchoring on the start is what keeps generic package
# names ("build", "buffer", "requests") from matching the middle of unrelated prose:
# measured over 60 days of unreviewed advisories, an unanchored name match returned
# 183 hits, almost all noise, and this one returned only advisories about the package.
_UNMAPPED_HEAD = re.compile(
    r"^\s*(?:the\s+)?(?P<name>@?[\w.\-/]+?)(?:\.js)?"
    r"(?:\s*\([^)]{0,80}\))?"
    r"(?:\s+(?:npm\s+|python\s+)?(?:package|library|module))?"
    r"(?:\s*\([^)]{0,80}\))?"
    r"\s+(?:versions?\s+)?"
    r"(?:(?:from\s+)?v?(?P<lo>\d+(?:\.\d+)*)\s+)?"
    r"(?P<op>before|prior to|through|up to and including|up to|<=|<)\s+"
    r"v?(?P<hi>\d+(?:\.\d+)*)",
    re.IGNORECASE,
)


def _version_tuple(text: str) -> Optional[Tuple[int, ...]]:
    """``1.12.0`` -> (1, 12, 0); None for anything that is not dotted integers."""
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return None


def _compare(a: Tuple[int, ...], b: Tuple[int, ...]) -> int:
    width = max(len(a), len(b))
    a, b = a + (0,) * (width - len(a)), b + (0,) * (width - len(b))
    return (a > b) - (a < b)


def match_unmapped(
    advisories: List[Dict[str, Any]],
    packages: List[Tuple[str, str, str]],
    known_ids: Iterable[str] = (),
) -> Tuple[List[Dict[str, Any]], int]:
    """Advisories with no package mapping whose summary names a pinned package.

    Returns (findings, not_affected): a finding is an advisory whose stated range
    includes the pinned version, or whose range could not be read; ``not_affected``
    counts the ones that named a package but exclude the version we pin.
    """
    known = set(known_ids)
    by_name: Dict[str, List[Tuple[str, str, str]]] = {}
    for eco, name, version in packages:
        by_name.setdefault(name.lower(), []).append((eco, name, version))

    findings: List[Dict[str, Any]] = []
    not_affected = 0
    for adv in advisories:
        if adv.get("vulnerabilities"):
            continue  # mapped, so OSV already matches it by package and version
        if adv.get("ghsa_id") in known or adv.get("cve_id") in known:
            continue
        m = _UNMAPPED_HEAD.match(adv.get("summary") or "")
        if not m:
            continue
        for eco, name, version in by_name.get(m.group("name").lower(), []):
            ours, hi = _version_tuple(version), _version_tuple(m.group("hi"))
            lo = _version_tuple(m.group("lo")) if m.group("lo") else None
            if ours is not None and hi is not None:
                inclusive = m.group("op").lower() not in ("before", "prior to", "<")
                under = _compare(ours, hi) <= (0 if inclusive else -1)
                over = lo is None or _compare(ours, lo) >= 0
                if not (under and over):
                    not_affected += 1
                    continue
                stated = "in the stated range"
            else:
                stated = "range not machine-readable; check by hand"
            findings.append(
                {
                    "id": adv.get("ghsa_id", ""),
                    "cve": adv.get("cve_id") or "",
                    "ecosystem": eco,
                    "package": name,
                    "version": version,
                    "severity": (adv.get("severity") or "unknown").upper(),
                    "match": stated,
                    "summary": (adv.get("summary") or "").strip(),
                }
            )
    return findings, not_affected


# External tools `scripts/generate-dep-manifest.sh` shells out to. Checked up
# front because a missing one otherwise surfaces as a bare
# `line 194: jq: command not found` buried in that script's captured output —
# and, worse, the Node half of the manifest comes out EMPTY, which would look
# like "no Node vulnerabilities" if the generator's exit code were ignored.
REQUIRED_TOOLS = {
    "jq": "reads the committed package-lock.json files to build the Node manifest",
    "uv": "resolves the loose Python pins the lockfiles do not cover",
}


def missing_tools() -> List[Tuple[str, str]]:
    """Return [(tool, why_it_is_needed)] for each required tool not on PATH."""
    return [(t, why) for t, why in REQUIRED_TOOLS.items() if shutil.which(t) is None]


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--severity",
        default="HIGH",
        choices=["CRITICAL", "HIGH", "MODERATE", "LOW"],
        help="minimum severity that fails the run (default: HIGH)",
    )
    ap.add_argument(
        "--no-generate",
        action="store_true",
        help="reuse existing dist/manifests instead of regenerating them",
    )
    ap.add_argument("--json", metavar="PATH", help="write the full report as JSON")
    ap.add_argument(
        "--no-unmapped",
        action="store_true",
        help="skip the report of unreviewed GitHub advisories with no package mapping",
    )
    args = ap.parse_args(list(argv) if argv is not None else None)

    if not args.no_generate:
        absent = missing_tools()
        if absent:
            print(
                "ERROR: cannot generate the dependency manifests — required "
                "tool(s) missing from PATH:",
                flush=True,
            )
            for tool, why in absent:
                print(f"  - {tool}: {why}", flush=True)
            print(
                "\nInstall them and re-run (Debian/Ubuntu: `apt-get install -y "
                f"{' '.join(t for t, _ in absent)}`), or pass --no-generate to "
                "audit manifests generated elsewhere.\n"
                "NOT treating this as a pass: without these the manifest is "
                "incomplete, and an incomplete manifest audits clean.",
                flush=True,
            )
            return 2

        print("Generating dependency manifests...", flush=True)
        proc = subprocess.run(  # nosec B603 - fixed argv, repo-local script
            ["bash", str(GENERATE_SCRIPT)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            # Everything on stdout, in order, flushed: interleaving a captured
            # stderr onto the real stderr scrambles the ordering in a CI log and
            # buries the actual cause (learned from a jq-missing failure whose
            # error landed above the output it belonged to).
            print("--- generate-dep-manifest.sh stdout ---", flush=True)
            print(proc.stdout, flush=True)
            print("--- generate-dep-manifest.sh stderr ---", flush=True)
            print(proc.stderr, flush=True)
            print(
                f"ERROR: could not generate dependency manifests "
                f"(exit {proc.returncode}). See the output above.",
                flush=True,
            )
            return 2

    missing = [p for p in (PYTHON_MANIFEST, NODE_MANIFEST) if not p.exists()]
    if missing:
        print(
            "ERROR: manifest(s) not found: "
            + ", ".join(str(p) for p in missing)
            + "\n       run without --no-generate, or `make dep-manifest` first",
            file=sys.stderr,
        )
        return 2

    py_pkgs = parse_python_manifest(PYTHON_MANIFEST)
    node_pkgs = parse_node_manifest(NODE_MANIFEST)

    # An EMPTY manifest audits clean, which is the most dangerous way for this
    # gate to fail: it reports success while covering nothing. That is not
    # hypothetical — a missing `jq` left node-packages.txt empty (the file was
    # still created by the shell redirect, so an exists() check passed). The
    # generator also only warns, without failing, when a lockfile is absent. So
    # require BOTH ecosystems to be represented and treat a shortfall as a broken
    # audit rather than a passing one.
    empty = [
        name for name, pkgs in (("Python", py_pkgs), ("Node", node_pkgs)) if not pkgs
    ]
    if empty:
        print(
            f"ERROR: the {' and '.join(empty)} manifest(s) contain no pinned "
            "packages, so this audit would cover nothing and report clean.\n"
            f"       python-packages.txt: {len(py_pkgs)} packages\n"
            f"       node-packages.txt:   {len(node_pkgs)} packages\n"
            "       Usually a missing tool (see REQUIRED_TOOLS) or a lockfile the "
            "generator skipped — check its output above.",
            flush=True,
        )
        return 2

    packages = py_pkgs + node_pkgs
    print(
        f"Auditing {len(packages)} pinned packages against OSV "
        f"({len(py_pkgs)} Python, {len(node_pkgs)} Node)...",
        flush=True,
    )

    try:
        hits = query_osv(packages)
        vuln_ids = sorted({vid for ids in hits.values() for vid in ids})
        print(f"Fetching severity for {len(vuln_ids)} advisories...", flush=True)
        details = {vid: _http_get_json(OSV_VULN_URL + vid) for vid in vuln_ids}
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(
            "The audit did not complete, so its result is unknown — failing "
            "rather than reporting a false pass.",
            file=sys.stderr,
        )
        return 2

    allowlist = load_allowlist()
    threshold = RANK[args.severity]
    gating: List[Dict[str, Any]] = []
    suppressed: List[Dict[str, Any]] = []
    below: List[Dict[str, Any]] = []
    used_keys = set()

    for key, ids in sorted(hits.items()):
        eco, name, version = key.split("|", 2)
        for vid in ids:
            vuln = details[vid]
            sev = severity_of(vuln)
            finding = {
                "id": vid,
                "aliases": vuln.get("aliases", []),
                "ecosystem": eco,
                "package": name,
                "version": version,
                "severity": sev or "UNSPECIFIED",
                "fixed_in": fixed_versions(vuln, name),
                "last_known_affected": last_known_affected(vuln, name),
                "summary": (vuln.get("summary") or "").strip(),
            }
            matched = allowlist_key(allowlist, vid, name)
            entry = allowlist[matched] if matched else None
            if entry:
                used_keys.add(matched)
                finding["allowlist_reason"] = entry.get("reason", "(no reason given)")
                suppressed.append(finding)
            elif RANK.get(sev, 0) >= threshold:
                gating.append(finding)
            else:
                below.append(finding)

    def show(title: str, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        print(f"\n{title} ({len(rows)})")
        print("-" * 100)
        for f in rows:
            fixed = ", ".join(f["fixed_in"]) or "no fix published"
            print(
                f"  {f['severity']:<14} {f['ecosystem']}:{f['package']}@{f['version']}"
            )
            print(f"    {f['id']}  fixed in: {fixed}")
            if f["last_known_affected"]:
                print(
                    "    last known affected range: "
                    f"{f['last_known_affected']}  (advisory has no 'fixed' event)"
                )
            if f["summary"]:
                print(f"    {f['summary'][:150]}")
            if "allowlist_reason" in f:
                print(f"    allowlisted: {f['allowlist_reason']}")

    # An entry that suppressed nothing is gating too: its advisory was withdrawn,
    # or the package was upgraded or removed, and leaving it in place approves the
    # same id in advance if it is ever reissued or the old version comes back.
    stale = [
        {"key": key, **allowlist[key]}
        for key in sorted(allowlist)
        if key not in used_keys
    ]

    unmapped: Dict[str, Any] = {"status": "skipped", "findings": []}
    if args.no_unmapped:
        unmapped["reason"] = "--no-unmapped"
    else:
        token = github_token()
        if not token:
            unmapped["reason"] = "no GitHub token (set GITHUB_TOKEN or log in with gh)"
        else:
            known = {vid for ids in hits.values() for vid in ids}
            known |= {a for v in details.values() for a in v.get("aliases", [])}
            try:
                advisories = fetch_unreviewed(token)
            except RuntimeError as exc:
                unmapped["reason"] = str(exc)
            else:
                found, not_affected = match_unmapped(advisories, packages, known)
                unmapped = {
                    "status": "ran",
                    "days": UNMAPPED_DAYS,
                    "advisories_read": len(advisories),
                    "not_affected": not_affected,
                    "findings": found,
                }

    show(f"GATING — at or above {args.severity}", gating)
    if stale:
        print(f"\nGATING — allowlist entries that matched no finding ({len(stale)})")
        print("-" * 100)
        for e in stale:
            print(f"  {e['key']}")
        print(
            "    The advisory was withdrawn, or the package moved past it. Delete\n"
            f"    the entry from {ALLOWLIST.relative_to(REPO_ROOT)} and its twin in\n"
            "    .ash/.ash.yaml."
        )
    show("ALLOWLISTED (triaged, not gating)", suppressed)
    show(f"BELOW THRESHOLD (informational, under {args.severity})", below)

    if unmapped["status"] == "ran":
        rows = unmapped["findings"]
        print(
            f"\nUNMAPPED ADVISORIES — review, not gating ({len(rows)}; "
            f"{unmapped['advisories_read']} unreviewed advisories from the last "
            f"{UNMAPPED_DAYS} days read, {unmapped['not_affected']} more name a "
            "pinned package but exclude its version)"
        )
        print("-" * 100)
        for f in rows:
            print(
                f"  {f['severity']:<14} {f['ecosystem']}:{f['package']}@{f['version']}"
            )
            print(f"    {f['id']} {f['cve']}  {f['match']}")
            print(f"    {f['summary'][:150]}")
    else:
        print(f"\nUNMAPPED ADVISORIES — not checked: {unmapped['reason']}")

    report = {
        "packages_audited": len(packages),
        "severity_threshold": args.severity,
        "gating": gating,
        "allowlisted": suppressed,
        "below_threshold": below,
        "stale_allowlist": stale,
        "unmapped": unmapped,
    }
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nJSON report written to {args.json}")

    print(
        f"\nSummary: {len(packages)} packages audited | {len(gating)} gating | "
        f"{len(suppressed)} allowlisted | {len(below)} below {args.severity} | "
        f"{len(stale)} stale allowlist | {len(unmapped['findings'])} unmapped to review"
    )
    if stale and not gating:
        print(
            f"\n❌ {len(stale)} allowlist entr{'y' if len(stale) == 1 else 'ies'} "
            "matched no finding. Delete them (see above)."
        )
        return 1
    if gating:
        print(
            f"\n❌ {len(gating)} dependency finding(s) at/above {args.severity}.\n"
            "   Fix by bumping the pin (and regenerating the lockfile), or — if the\n"
            "   advisory is genuinely not reachable here — add an entry with a\n"
            f"   specific justification to {ALLOWLIST.relative_to(REPO_ROOT)}.",
        )
        return 1

    print("\n✅ No dependency vulnerabilities at or above the threshold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
