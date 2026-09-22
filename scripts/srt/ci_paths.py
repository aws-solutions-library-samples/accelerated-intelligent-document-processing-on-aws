#!/usr/bin/env python3
"""Classify SRT finding paths by whether CI's checkout can actually see them.

CI runs the SRT gate (`srt_security_review` in .gitlab-ci.yml) on a clean
checkout of TRACKED files only — the job declares `needs: []` in `fast_checks`,
so no build stage ever precedes it and `.aws-sam/` does not exist there.

A local working tree is different: after a `python3 publish.py ...` run it
carries gitignored build output — `.aws-sam/packaged.yaml` for every nested
stack, `.aws-sam/idp-main.yaml`, vendored Lambda `layer/python/` trees,
`scratch/` — and `srt assess` has no `--exclude` option, so it happily scans
and flags all of it. Those findings CANNOT exist in CI.

Worse, SRT matches a finding to an existing suppression on the 4-tuple
`(path, resourceType, resourceName, check_id)`, so a suppression recorded
against `template.yaml` can never match the same resource re-flagged in
`.aws-sam/packaged.yaml`. The historical result was 30+ phantom HIGH findings
after any build, "fixed" by committing artifact-path entries into the baseline
`scripts/srt/issues.json` — which then re-detect as `reopened` on the next
scan, because `resolved` is not a sticky disposition in SRT.

This module lets run.py report those separately instead of gating on them, and
lets fix.py keep them out of the committed baseline in the first place.
"""

import re
import subprocess
from pathlib import Path

# SRT dispositions that still need attention, i.e. that gate the build.
#
# 'reopened' MUST be here. SRT assigns it when a finding it had recorded as
# resolved/suppressed is detected again, and counts it in its own "N issues need
# attention" line. 'resolved' is NOT a sticky disposition — only 'suppressed'
# is — so gating on 'open' alone let a re-detected HIGH through silently (seen on
# 0.6.5: LAMBDA-012 in nested/bedrockkb/template.yaml and the semgrep npm
# minimum-release-age finding on src/ui/.npmrc, both carrying 'resolved').
GATING_STATUSES = ("open", "reopened")


def is_gating_status(status):
    """True if this SRT status means the finding is undispositioned."""
    return (status or "").lower() in GATING_STATUSES


def tracked_files(project_root):
    """Return the set of git-tracked repo-relative paths, or None if unknown.

    None means we could not ask git (not a repo, git missing, command failed).
    Callers must treat None as "cannot classify" and fall back to gating on
    everything — never silently drop findings we failed to check.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"⚠️  Warning: could not list tracked files ({e}); "
              "treating every finding as CI-visible.")
        return None

    if result.returncode != 0:
        print("⚠️  Warning: 'git ls-files' failed "
              f"(exit {result.returncode}); treating every finding as CI-visible.")
        return None

    return {p for p in result.stdout.split("\0") if p}


def _normalize(path):
    """Normalize an SRT finding path for comparison against `git ls-files`."""
    if not path:
        return None
    # SRT emits repo-relative POSIX paths, but be defensive about "./" prefixes
    # and Windows-style separators so a mismatch can't be read as "untracked".
    return Path(str(path).replace("\\", "/")).as_posix().removeprefix("./")


def is_in_ci_checkout(path, tracked):
    """True if this finding's file exists in CI's checkout (i.e. is tracked).

    Fails closed: an unknown `tracked` set, or a finding with no path at all,
    counts as CI-visible so it still gates.
    """
    if tracked is None:
        return True

    normalized = _normalize(path)
    if normalized is None:
        # A finding with no path (repo-wide observation) always gates.
        return True

    return normalized in tracked


#: Bandit's two hardcoded-credential heuristics, which gate everywhere EXCEPT in
#: test-only files. Both fire on the *name* of an identifier: B105 on any
#: assignment or comparison whose name matches a password-shaped wordlist
#: (`pas+wo?r?d|pass(phrase)?|pwd|token|secrete?`) against a constant, B106 on any
#: such keyword argument. `pass_count`, `next_token` and `_COMPACT_TOKEN` all match.
#:
#: Bandit rates both LOW. They reach this gate as HIGH because the SRT binary
#: carries `priorityOverrides = {B105: "High", B106: "High"}` and applies it before
#: its severity mapping, so a dict key named `pass_count` in a test fixture arrives
#: at the same priority as a real credential. Three times in one day that took the
#: gate red on `develop`, blocking every open pull request, and each response was
#: another per-line suppression pragma (#1086).
#:
#: The scope decision lives here because **no Bandit rule scopes a check to a
#: path.** The promotion itself is compiled into the SRT binary with no
#: configuration surface, and on Bandit's side the config keys are one flat
#: namespace: `skips` applies to the whole run, `exclude_dirs` removes the file
#: from the scan entirely rather than one check, and neither B105 nor B106 takes
#: plugin configuration, so the wordlist cannot be narrowed either. (A `.bandit`
#: file *is* discovered at any depth by the invocation SRT already makes, and its
#: `configfile` key chains to the full YAML/TOML surface including `pyproject.toml`
#: — the limitation is the key set, not the reach.)
#:
#: The one mechanism that *can* distinguish paths is `baseline`, a frozen JSON
#: report of findings to subtract, and it is the wrong tool for a reason worth
#: writing down: baseline matching compares the finding's literal matched value, so
#: it can only ever cover findings that already existed when it was frozen. Add one
#: new name-shaped identifier to an already-baselined file and it arrives as HIGH.
#: A rule about paths is what this needs, and a baseline is a list of facts.
#:
#: What this gives up, stated plainly: a genuine credential pasted into a test
#: fixture is reported at Bandit's own LOW severity and does not block the build.
#: That is the cost of not blinding the check in shipped code, where the same name
#: shape still gates. `make srt-scan` prints every demoted finding under its own
#: heading rather than dropping it.
NAME_HEURISTIC_EXEMPT = ("B105", "B106")

#: Directory names whose contents are test code.
_TEST_ONLY_SEGMENTS = frozenset({"test", "tests", "manual_tests"})

#: Template keys naming a local directory that `sam build` copies **verbatim** into
#: the deployment artifact. Both behave the same way, so both are read.
_PACKAGED_SOURCE_KEYS = ("CodeUri", "ContentUri")

_PACKAGED_SOURCE_RE = re.compile(
    r"^\s*(?:" + "|".join(_PACKAGED_SOURCE_KEYS) + r"):\s*(?P<value>[^\s#]+)\s*$",
    re.MULTILINE,
)

#: File suffixes worth opening when looking for a template.
_TEMPLATE_SUFFIXES = (".yaml", ".yml", ".json", ".template")


def _repo_relative(base, value):
    """Resolve a template-relative path value to a repo-relative POSIX path."""
    parts = []
    for piece in f"{base}/{value}".split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(piece)
    return "/".join(parts) or None


def packaged_source_dirs(project_root):
    """Directories a deployment artifact is built from, or None if unknown.

    Discovered by **content** — every tracked file declaring
    `AWSTemplateFormatVersion` — so a new template is covered without being listed,
    the same rule `scripts/discover_templates.sh` uses for the cfn-lint and
    ARN-partition gates.

    This exists because a path's *shape* does not decide whether it ships.
    `sam build` copies a `CodeUri` directory verbatim; there is no ignore file in
    this tree and the publisher prunes nothing, so a `test_*.py` or a `tests/`
    directory sitting beside a handler is packaged with it and runs in a customer's
    account. Deciding on shape alone demoted 99 such files.

    Returns None when the answer cannot be established — git unavailable, or no
    template found. Callers must read that as "cannot tell" and keep gating, never
    as "nothing ships".
    """
    tracked = tracked_files(project_root)
    if tracked is None:
        return None

    root = Path(project_root)
    found = set()
    templates = 0
    for rel in sorted(tracked):
        if not rel.endswith(_TEMPLATE_SUFFIXES):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "AWSTemplateFormatVersion" not in text:
            continue
        templates += 1
        base = Path(rel).parent.as_posix()
        for match in _PACKAGED_SOURCE_RE.finditer(text):
            value = match.group("value").strip().strip("'\"")
            # An intrinsic function, a nested mapping or an S3 location names no
            # local directory, so there is nothing to protect.
            if value.startswith(("!", "{", "s3://")) or "${" in value:
                continue
            resolved = _repo_relative(base, value)
            if resolved and (root / resolved).is_dir():
                found.add(resolved)

    if not templates:
        print(
            "⚠️  Warning: no CloudFormation template found, so packaged Lambda "
            "source directories are unknown; treating every finding as shipped."
        )
        return None

    return frozenset(found)


def _in_packaged_tree(rel, packaged):
    return any(rel == d or rel.startswith(d.rstrip("/") + "/") for d in packaged)


def _has_test_shape(rel):
    """True if the path's shape says test code: a directory segment or a filename."""
    parts = rel.split("/")
    if any(part in _TEST_ONLY_SEGMENTS for part in parts[:-1]):
        return True
    name = parts[-1]
    return (
        name == "conftest.py"
        or (name.startswith("test_") and name.endswith(".py"))
        or name.endswith("_test.py")
    )


def is_test_only_path(path, packaged):
    """True if this repo-relative path is test code that does **not** ship.

    Two conditions, because either alone is wrong. The path must have a test shape
    — whole segments, never prefixes, since `src/lambda/test_file_copier/` and
    `.../test_runner/` are Lambda handler directories — and it must not sit inside
    a directory a deployment artifact is built from. The second is what makes the
    first safe: `src/lambda/queue_processor/test_reconcile_counter.py` and
    `src/lambda/test_file_copier/test_index.py` are test-shaped and are copied into
    the artifact beside the handler, so a hardcoded credential in either reaches a
    customer's account and must keep gating.

    Fails closed in every direction that matters: a finding with no path, a path
    that is not test-shaped, a path inside a packaged tree, and a `packaged` of
    None (meaning the packaged set could not be established) all keep gating.
    """
    if packaged is None:
        return False

    normalized = _normalize(path)
    if normalized is None:
        return False

    if _in_packaged_tree(normalized, packaged):
        return False

    return _has_test_shape(normalized)


def partition_by_name_heuristic_scope(issues, project_root):
    """Split issues into (gating, name_heuristic_in_test_code).

    See :data:`NAME_HEURISTIC_EXEMPT`. Every finding is in exactly one of the two
    lists, and a finding reaches the second only if its check is one of those two
    AND its path is test code that is not packaged into a deployment artifact — so
    nothing is dropped and nothing shipped is demoted.
    """
    packaged = packaged_source_dirs(project_root)
    gating = []
    name_scoped = []

    for issue in issues:
        check_id = (issue.get("check_id") or "").upper()
        if check_id in NAME_HEURISTIC_EXEMPT and is_test_only_path(
            issue.get("path"), packaged
        ):
            name_scoped.append(issue)
        else:
            gating.append(issue)

    return gating, name_scoped


def partition_by_ci_visibility(issues, project_root):
    """Split issues into (ci_visible, local_only) by path trackedness.

    `local_only` findings live in gitignored files — build artifacts, vendored
    third-party trees, scratch dirs — that CI never checks out.
    """
    tracked = tracked_files(project_root)
    ci_visible = []
    local_only = []

    for issue in issues:
        if is_in_ci_checkout(issue.get("path"), tracked):
            ci_visible.append(issue)
        else:
            local_only.append(issue)

    return ci_visible, local_only
