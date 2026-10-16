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
import sys
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

#: CodeBuild's property naming the buildspec a container image build runs.
#:
#: Reading it is what puts a container-built Lambda's source tree in the packaged set
#: at all. A `PackageType: Image` function declares no `CodeUri`: its template carries
#: an `ImageUri` naming an ECR tag, and no template key anywhere in this repository
#: names the directory such an image is built from — `DockerContext` appears in none
#: of them, because SAM does not drive these builds. The build does name them, in the
#: buildspec the CodeBuild project points at, which exports one `FUNCTION_<name>`
#: variable per source tree before looping over them.
#:
#: Before this, `patterns/unified/template.yaml` declared 16 image-built functions and
#: one `CodeUri` function, so 15 of the 16 source trees under `patterns/unified/src/`
#: were invisible here and a `test_*.py` beside one of those handlers satisfied both
#: halves of :func:`is_test_only_path` — the premise that makes the test-shape
#: heuristic safe, absent for the trees it was most needed on (#1389).
#:
#: The buildspec is resolved from this property rather than found by globbing, which is
#: what `patterns/unified/tests/test_expected_images_match_the_buildspec.py` does and
#: for the same reason: the sibling files `buildspec-bda.yml` and
#: `buildspec-pipeline.yml` carry `export FUNCTION_` lines of their own, so a glob
#: reads a different and wrong set, and retargeting the project moves this reader
#: with it.
_BUILDSPEC_KEY = "BuildSpec"

_BUILDSPEC_RE = re.compile(
    rf"^\s*{_BUILDSPEC_KEY}:\s*(?P<value>[^\s#]+)\s*$",
    re.MULTILINE,
)

#: `BuildSpec` scalars that name no file on disk: a block-scalar introducer (the value
#: is an inline buildspec document, which names no source directory of its own), a
#: short-form intrinsic, a flow collection, or a remote location.
_UNRESOLVABLE_VALUE_PREFIXES = ("|", ">", "&", "*", "!", "{", "[", "s3://", "arn:")

#: The shell assignment a container buildspec uses to name one image's source tree.
#:
#: Deliberately unanchored. These lines are YAML **list items** — `- export
#: FUNCTION_ocr="patterns/unified/src/ocr_function"`, with the `-` before `export` —
#: so a pattern anchored to the start of a line, or to leading whitespace followed by
#: `export`, matches none of them and reports zero, which looks exactly like the key
#: being absent. It is applied to the command strings of a parsed buildspec rather
#: than to raw lines, so a commented-out export is not read as a live one.
_FUNCTION_EXPORT_RE = re.compile(
    r'export\s+FUNCTION_[A-Za-z0-9_]+="(?P<path>[^"]+)"',
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


def _resolve_candidates(value, base):
    """The two places a build-relative path can mean, repository root first.

    A CodeBuild project's source is a zip whose root is not always this
    repository's root. `patterns/unified/template.yaml` names
    `patterns/unified/buildspec.yml`, repository-relative, because its zip holds the
    whole tree; `feature-platform/idp-data-generator/template.yaml` names a bare
    `buildspec.yml` sitting beside it, because its zip root is that directory. Both
    spellings are live in this tree, so both are tried and whichever exists wins.
    """
    candidates = []
    for candidate in (_repo_relative("", value), _repo_relative(base, value)):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


class ScanDependencyError(RuntimeError):
    """A distribution this module's classification cannot run without is absent.

    Deliberately **not** one of this module's "cannot tell" answers, and the
    distinction is the whole point. Every other unknown here — git unavailable, a
    buildspec that is not tracked, a document that does not parse — is a fact about
    the *tree*, and answering it by gating every finding is correct: something in
    the repository really cannot be established. A missing parser is a fact about
    the *environment*, and answering it the same way reported ten Bandit
    identifier-name findings in test files as gating HIGH on every pull request for
    a day, because the demotion that classifies them was switched off by a
    `pip install` nobody had run (#1428). Both states are a findings table and a
    non-zero exit, so a setup fault and a security regression were the same output.

    Raised rather than returned so there is no code path on which the degraded
    answer can be read as a finding: a caller that has not checked gets a named
    failure instead of a smaller packaged set.
    """


#: The distribution :func:`_buildspec_commands` needs, and the command that installs
#: it. Spelled as a distribution name rather than an import name because that is what
#: the reader has to type, and both CI jobs name it on their own `pip install` line —
#: `scripts/tests/test_ci_gate_parity.py` reads this constant and asserts they still
#: do, so dropping it from one platform cannot pass as parity.
YAML_DISTRIBUTION = "PyYAML"

#: Every distribution the classification requires. A tuple so a caller can report all
#: of them in one message rather than one per re-run.
REQUIRED_DISTRIBUTIONS = (YAML_DISTRIBUTION,)

_INSTALL_COMMANDS = {YAML_DISTRIBUTION: "pip install PyYAML"}


def _yaml_module():
    """PyYAML, or None if it is not installed.

    Imported lazily, and the reason is no longer that a missing distribution is
    tolerable: :func:`_buildspec_commands` refuses outright when this answers None.
    It is that the refusal has to be a sentence rather than a traceback, and that
    the parts of this module which need no parser must keep working without one.
    `scripts/srt/fix.py` imports it for :func:`partition_by_ci_visibility` alone, and
    a module-level `import yaml` would make that import fail too — naming the module
    `yaml`, which is not the string anybody installs.
    """
    try:
        import yaml
    except ImportError:
        return None
    return yaml


def missing_distributions():
    """The names in :data:`REQUIRED_DISTRIBUTIONS` this interpreter cannot import.

    Measured by attempting the import rather than by reading installed metadata,
    because the question is whether *this* interpreter can parse a buildspec. `make
    srt-scan` runs the interpreter on PATH, not SRT's managed venv, so a package
    installed for the scanners says nothing about the one doing the classification.
    """
    missing = []
    if _yaml_module() is None:
        missing.append(YAML_DISTRIBUTION)
    return tuple(missing)


def dependency_failure_message(missing):
    """The refusal to print for `missing`, naming each distribution and its install.

    One message with the command in it, because the alternative measured in #1428 is
    a warning line in banner output that a reader skims past while ten HIGH findings
    below it look like the actual result.
    """
    names = ", ".join(missing)
    commands = "\n".join(
        f"      {_INSTALL_COMMANDS.get(name, f'pip install {name}')}"
        for name in missing
    )
    return (
        f"{names} is not importable on this interpreter ({sys.executable}), so the "
        "SRT gate\n"
        "   cannot establish which directories a deployment artifact is built from, "
        "and the\n"
        "   demotion of identifier-name findings in test code is therefore "
        "unavailable.\n"
        "\n"
        "   Install it and re-run:\n"
        "\n"
        f"{commands}\n"
        "\n"
        "   This is a setup fault, not a scan result. Scanning anyway would report "
        "every\n"
        "   B105/B106 finding in test code as a gating HIGH — indistinguishable from "
        "a real\n"
        "   security regression, which is what it did on every pull request for a "
        "day (#1428)."
    )


def _buildspec_commands(text):
    """Every command string in every phase of a buildspec, or None if unreadable.

    A buildspec carries no CloudFormation intrinsics, so `yaml.safe_load` is the
    right parser and no custom loader is involved — the same reasoning
    `patterns/unified/tests/test_expected_images_match_the_buildspec.py` states for
    reading the same file.

    Reading the loaded document rather than the raw lines is what the parser buys,
    in both directions. A commented-out `export FUNCTION_` line is not read as live
    (so a directory nothing ships does not enter the packaged set, where it would
    keep every test file under it gating), and a buildspec that is malformed is
    *known* to be malformed — a line-oriented read of a truncated or corrupted file
    returns a quietly smaller set of exports, which is the direction that demotes a
    test file the artifact really does carry (#1389) and reports nothing.

    None means the document is unreadable, which keeps every finding gating. A
    missing parser is not that answer: it raises, because the tree would be fine and
    the environment is not.
    """
    yaml_module = _yaml_module()
    if yaml_module is None:
        raise ScanDependencyError(dependency_failure_message((YAML_DISTRIBUTION,)))
    try:
        document = yaml_module.safe_load(text)
    except yaml_module.YAMLError:
        return None
    if not isinstance(document, dict):
        return None

    phases = document.get("phases")
    if not isinstance(phases, dict):
        return []

    commands = []
    for phase in phases.values():
        if not isinstance(phase, dict):
            continue
        for command in phase.get("commands") or []:
            if isinstance(command, str):
                commands.append(command)
    return commands


def _buildspec_refs(text):
    """The `BuildSpec` values in a template that name a file rather than a document.

    An inline buildspec — the `BuildSpec: |` form, which three templates here use —
    is a document, not a path. It names no source directory of its own, so there is
    nothing for this reader to resolve and its absence from the result is not a
    failure to establish anything.
    """
    values = []
    for match in _BUILDSPEC_RE.finditer(text):
        value = match.group("value").strip().strip("'\"")
        if not value or value.startswith(_UNRESOLVABLE_VALUE_PREFIXES):
            continue
        if "${" in value:
            continue
        values.append(value)
    return values


def _function_source_dirs(root, buildspec_rel, text):
    """Source trees a buildspec's `export FUNCTION_*` lines name, or None if unread.

    None means the buildspec could not be parsed, which is "cannot establish the
    packaged set" and never "nothing ships". An empty set is a different answer and a
    legitimate one: a build whose context is the zip root, such as the data
    generator's, names its inputs in a Dockerfile rather than in a variable here.

    Raises :class:`ScanDependencyError` when the parser itself is absent, which is
    neither of those answers — see :func:`_buildspec_commands`.
    """
    commands = _buildspec_commands(text)
    if commands is None:
        return None

    base = Path(buildspec_rel).parent.as_posix()
    found = set()
    for command in commands:
        for match in _FUNCTION_EXPORT_RE.finditer(command):
            value = match.group("path")
            # A path assembled from another shell variable names nothing this
            # reader can resolve, so there is nothing to add.
            if "$" in value:
                continue
            for candidate in _resolve_candidates(value, base):
                if (root / candidate).is_dir():
                    found.add(candidate)
                    break
    return found


def _image_build_source_dirs(project_root, tracked, template_rel, text):
    """Source trees the image builds this template declares are built from.

    None if a `BuildSpec` names a file that cannot be found or parsed. That is the
    honest answer rather than an empty one — the directories a container image ships
    would then be unknown — and :func:`packaged_source_dirs` propagates it as its own
    None, so every finding keeps gating.
    """
    root = Path(project_root)
    base = Path(template_rel).parent.as_posix()
    found = set()

    for value in _buildspec_refs(text):
        buildspec_rel = next(
            (
                candidate
                for candidate in _resolve_candidates(value, base)
                if candidate in tracked and (root / candidate).is_file()
            ),
            None,
        )
        if buildspec_rel is None:
            print(
                f"⚠️  Warning: {template_rel} points its container build at "
                f"'{value}', which is not a tracked file in this checkout, so the "
                "source trees its images are built from are unknown; treating every "
                "finding as shipped."
            )
            return None

        try:
            buildspec_text = (root / buildspec_rel).read_text(
                encoding="utf-8", errors="ignore"
            )
        except OSError as e:
            print(
                f"⚠️  Warning: could not read {buildspec_rel} ({e}), so the source "
                "trees its images are built from are unknown; treating every finding "
                "as shipped."
            )
            return None

        dirs = _function_source_dirs(root, buildspec_rel, buildspec_text)
        if dirs is None:
            print(
                f"⚠️  Warning: could not parse {buildspec_rel} as YAML, so the source "
                "trees its images are built from are unknown; treating every finding "
                "as shipped."
            )
            return None
        found |= dirs

    return found


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

    Two kinds of source are read, because the two packaging types name their trees in
    different places. A `Zip` function names its directory in the template, under
    :data:`_PACKAGED_SOURCE_KEYS`. An `Image` function names an ECR tag and nothing
    else, so its tree is read from the buildspec its CodeBuild project points at —
    see :data:`_BUILDSPEC_KEY`. A container build copies its context whole, exactly
    as `sam build` copies a `CodeUri` tree, so the two sources answer one question.

    Returns None when the answer cannot be established — git unavailable, no template
    found, or a build pointing at a buildspec this checkout cannot read. Callers must
    read that as "cannot tell" and keep gating, never as "nothing ships".

    Raises :class:`ScanDependencyError` when a required distribution is not installed.
    That is a different kind of answer from None and must not be collapsed into it:
    None says this repository cannot be read, the exception says this machine cannot
    read any repository. `scripts/srt/run.py` refuses before the scan for exactly
    that reason; the raise is what makes the refusal unskippable for a caller that
    does not.
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

        image_dirs = _image_build_source_dirs(root, tracked, rel, text)
        if image_dirs is None:
            return None
        found |= image_dirs

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
