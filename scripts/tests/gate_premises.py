# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Machine-checkable premises for gate exemptions, derived from the source of truth.

A gate exemption is a promise about the world: *this member is exempt because P*.
The repeated defect this module exists to stop is a promise nobody ever evaluated.
Four exemption lists in this repository stated a premise that was false for at
least one of their members, and in every case the false member was the one the
gate most needed to see:

* an X-Ray exemption said seven templates were independently deployed, and one of
  them was a nested stack of the parent receiving 26 parameters;
* a redaction exemption said a Lambda tree was "built and versioned separately",
  and the publisher builds it in the same run, unconditionally;
* a ``LogLevel`` exclusion said an installer manifest overrode the template
  default, and one of the excluded directories has no manifest;
* an ARN-partition exemption said four templates named a commercial-only
  principal, and one of them contains no ARN at all.

Each of those premises is a *fact about this tree*, computable from a file that is
already the authority for it. Each was also already computed, once, inside the one
gate that happened to need it — so the ingredient existed and the assembly did not.
This module is the assembly. A gate that wants to exempt a member names a predicate
here; the registry meta-test evaluates it per member and fails on the member the
premise does not hold for.

**Per member, never per set.** Every one of the four defects is the same bug: one
justification attached to a set, where the justification is a property of individual
members. Reading the reason in aggregate ("does this hold broadly?") passes all four.
So every predicate here takes ONE member and returns a verdict for it, and callers
are expected to map it over the list rather than ask whether it holds generally.

**Discovery goes through git.** Every helper that enumerates files uses
``git ls-files`` rather than walking the filesystem. Two gates in the last release
cycle reported findings against build output and against another branch's worktree
under ``.claude/`` — results CI can never reproduce, and in one case 157 false
failures burying one real defect. ``git ls-files`` resolves paths relative to the
checkout it is run in, so a checkout that itself sits under a pruned directory still
sees its own files (and only its own).

Related: ``scripts/tests/repo_files.py`` on the in-flight tracing branch introduces
an equivalent tracked-file helper for the three repo-walking gates. When both land,
:func:`tracked_files` should delegate to it rather than keep a second implementation
of "what does git track" — see the PR that added this module.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
import sys
from collections.abc import Collection
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The parent template whose ``AWS::CloudFormation::Stack`` resources define the
#: "nested stack of the parent" relation. This is the deployable root: a parameter
#: declared here reaches every template named below it.
PARENT_TEMPLATE = "template.yaml"

#: Where the publisher lives. Its component map is the authority on what is built
#: in a publish run, and nothing else in the tree reads it.
_PUBLISH_PACKAGE = "lib/idp_sdk"


# --------------------------------------------------------------------------- #
# File discovery
# --------------------------------------------------------------------------- #


#: Repo-relative prefixes that can hold a whole copy of this tree: local verification
#: work and agent worktrees. Pruned in ADDITION to ``--exclude-standard``, not instead
#: of it.
#:
#: Asking git is not sufficient on its own, and the difference is easy to miss.
#: ``git ls-files --others`` lists files under ``scratch/`` and ``.claude/`` whenever
#: those are not gitignored — measured, not assumed. In *this* repository they are
#: (``.gitignore`` lines 33 and 114), so ``--exclude-standard`` happens to cover them
#: and dropping this set would change nothing today. That is exactly why it is here:
#: a helper that is correct only because of two lines in a sibling file is a latent
#: bug, and this class of gate has already produced 157 false failures from worktrees
#: once. A `git worktree` checkout is additionally protected by git listing a nested
#: repository as a bare directory rather than its contents, but a plain copy is not.
LOCAL_WORK_PREFIXES = ("scratch/", ".claude/")


def tracked_files(
    *globs: str,
    root: Path | None = None,
    include_untracked: bool = False,
    prune_local_work: bool = True,
) -> tuple[str, ...]:
    """Repo-relative POSIX paths that git reports, sorted.

    ``globs`` are git pathspecs (``"*.py"``, ``"scripts/**"``); with none given,
    every file is returned. Gitignored build output and local work are excluded by
    the same mechanism CI uses, which is the point — a gate that walks the
    filesystem finds ``.aws-sam/`` copies and sibling worktrees and reports
    findings against files that ship nowhere.

    ``include_untracked`` adds files that exist but are not yet committed (still
    honouring ``.gitignore``), matching ``scripts/discover_templates.sh``. Turn it on
    for a gate that must see a file the author has not committed yet — otherwise the
    gate's verdict changes at ``git add`` time, which this module's own registry
    discovered on itself.

    ``prune_local_work`` additionally drops :data:`LOCAL_WORK_PREFIXES`, matched
    against the **repo-relative** path so that where the checkout sits cannot exclude
    it. See that constant for why ``--exclude-standard`` alone is not enough.
    """
    root = root or REPO_ROOT
    args = ["git", "-C", str(root), "ls-files", "-z", "--cached", "--exclude-standard"]
    if include_untracked:
        args.append("--others")
    if globs:
        args.append("--")
        args.extend(globs)
    out = subprocess.run(args, check=True, capture_output=True, text=True).stdout
    found = (p for p in out.split("\0") if p)
    if prune_local_work:
        found = (
            p for p in found if not any(p.startswith(x) for x in LOCAL_WORK_PREFIXES)
        )
    return tuple(sorted(found))


def is_tracked(rel_path: str, root: Path | None = None) -> bool:
    """Whether git tracks exactly this path (not a prefix of it)."""
    return rel_path in tracked_files(rel_path, root=root)


# --------------------------------------------------------------------------- #
# CloudFormation parsing
# --------------------------------------------------------------------------- #


class CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags (``!Ref``, ``!Sub``).

    The same loader four gates each define privately. ``!Ref Foo`` becomes
    ``{"Fn::Ref": "Foo"}`` so a comparison against the long form works either way.
    """


def _tag_to_python(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{tag_suffix}": loader.construct_sequence(node, deep=True)}
    return {f"Fn::{tag_suffix}": loader.construct_mapping(node, deep=True)}


CfnLoader.add_multi_constructor("!", _tag_to_python)


def load_template(rel_path: str, root: Path | None = None) -> dict:
    """Parse a CloudFormation template, tolerating short-form tags."""
    path = (root or REPO_ROOT) / rel_path
    return yaml.load(path.read_text(encoding="utf-8"), Loader=CfnLoader) or {}


def source_template_for(template_url: object) -> str | None:
    """``./nested/x/.aws-sam/packaged.yaml`` -> ``nested/x/template.yaml``.

    A parent's ``TemplateURL`` points at a *build artifact*, which is absent on a
    clean checkout and stale on a dirty one. The relation a gate means is between
    source templates, so resolve back to the source the artifact is built from.
    """
    if not isinstance(template_url, str):
        return None
    match = re.match(r"^\./(.+?)/\.aws-sam/packaged\.ya?ml$", template_url.strip())
    if not match:
        return None
    for candidate in (
        f"{match.group(1)}/template.yaml",
        f"{match.group(1)}/template.yml",
    ):
        if (REPO_ROOT / candidate).is_file():
            return candidate
    return None


@functools.lru_cache(maxsize=1)
def nested_stack_sources(parent: str = PARENT_TEMPLATE) -> dict[str, str]:
    """``{logical id: source template}`` for every nested stack of ``parent``.

    This is the relation three gates each compute privately and a fourth got
    wrong. If a template appears as a value here then the parent deploys it, the
    parent's parameters reach it, and no exemption may claim otherwise.
    """
    resources = load_template(parent).get("Resources") or {}
    found: dict[str, str] = {}
    for logical_id, body in resources.items():
        if (
            not isinstance(body, dict)
            or body.get("Type") != "AWS::CloudFormation::Stack"
        ):
            continue
        source = source_template_for((body.get("Properties") or {}).get("TemplateURL"))
        if source:
            found[logical_id] = source
    return found


# --------------------------------------------------------------------------- #
# Publisher component map
# --------------------------------------------------------------------------- #


@functools.lru_cache(maxsize=1)
def built_components() -> dict[str, tuple[str, ...]]:
    """The publisher's component -> build-inputs map, read from the publisher.

    ``IDPPublisher.get_component_dependencies`` is the authority on what a single
    ``publish.py`` run builds. It reads nothing and touches no AWS, so it is called
    unbound; if that ever stops being true this raises rather than guessing, which
    is the correct failure for a premise check.
    """
    sdk = str(REPO_ROOT / _PUBLISH_PACKAGE)
    if sdk not in sys.path:
        sys.path.insert(0, sdk)
    # noqa justification: the private module IS the source of truth here. The
    # public IDPClient does not expose the publisher's build sources, and the
    # whole point of this predicate is to read the authority rather than restate
    # it -- two exemption lists cited "built separately" and nothing in the tree
    # read this map. Importing IDPClient instead would mean hand-copying the
    # component list, which is the defect being fixed.
    from idp_sdk._core.publish import IDPPublisher  # noqa: TID251

    raw = IDPPublisher.get_component_dependencies(None)  # pyright: ignore[reportArgumentType]
    return {
        component: tuple(dep.removeprefix("./") for dep in deps)
        for component, deps in raw.items()
    }


@functools.lru_cache(maxsize=1)
def bundled_feature_dirs() -> tuple[str, ...]:
    """The OSS feature directories a publish run builds, from ``extensions-oss.yaml``.

    These are built by ``build_and_upload_sample_features`` -> ``_bundled_feature_dirs``
    -> ``build_and_package_template(force_rebuild=True)``, and **none of them appears in
    the component-dependency map**. That map is the publisher's *smart-rebuild checksum*
    map, authoritative for the components the rebuild loop iterates and not for
    everything a publish run builds.

    Missing this cost the predicate below its correctness for five of the twelve members
    of the only list that used it, and the gate turned that into a written permission to
    claim independence. Reading two sources is not belt-and-braces here: it is what
    "does one publish run build this" actually means.
    """
    sdk = str(REPO_ROOT / _PUBLISH_PACKAGE)
    if sdk not in sys.path:
        sys.path.insert(0, sdk)
    # noqa justification: the private module IS the source of truth here. The
    # public IDPClient does not expose the publisher's build sources, and the
    # whole point of this predicate is to read the authority rather than restate
    # it -- two exemption lists cited "built separately" and nothing in the tree
    # read this map. Importing IDPClient instead would mean hand-copying the
    # component list, which is the defect being fixed.
    from idp_sdk._core.publish import IDPPublisher  # noqa: TID251

    class _Shim:
        """Only what ``_bundled_feature_dirs`` touches; it reads a file and nothing else."""

        _OSS_EXTENSIONS_FILE = IDPPublisher._OSS_EXTENSIONS_FILE
        _DEFAULT_BUNDLED_FEATURE_DIRS = IDPPublisher._DEFAULT_BUNDLED_FEATURE_DIRS

        @staticmethod
        def log_verbose(*_args, **_kwargs):
            return None

        @staticmethod
        def log_error(*_args, **_kwargs):
            return None

    # The method resolves its path relative to the process CWD.
    previous = Path.cwd()
    try:
        os.chdir(REPO_ROOT)
        return tuple(IDPPublisher._bundled_feature_dirs(_Shim()))
    finally:
        os.chdir(previous)


@functools.lru_cache(maxsize=1)
def build_input_paths() -> frozenset[str]:
    """Every path a single publish run builds, from both of its sources.

    The union matters. Either source alone gives the wrong answer for members the other
    covers, and "built separately" is a claim about the whole publish run.

    There was briefly a third source here: a hand-written ``EXPLICIT_BUILD_DIRS`` tuple
    mirroring the one ``force_rebuild=True`` call site that passes a literal directory,
    under a comment claiming a test pinned it against the publisher. No such test
    existed, and writing it showed the tuple was redundant -- its single member is
    already a key in the component map. So it is gone, and
    ``scripts/tests/test_gate_premises.py`` asserts the property it was standing in for:
    every literal directory a forced-rebuild call site names is covered by one of these
    two derived sources, so a genuinely new one fails instead of narrowing the predicate
    in silence.
    """
    paths = set(built_components())
    for deps in built_components().values():
        paths.update(deps)
    paths.update(bundled_feature_dirs())
    return frozenset(paths)


# --------------------------------------------------------------------------- #
# The premise predicates
# --------------------------------------------------------------------------- #
#
# Each takes ONE exemption member and returns (holds, explanation). The
# explanation is returned in both directions so a failure message can say what
# was measured rather than only that it disagreed.


Verdict = tuple[bool, str]


def _normalise(member: str) -> str:
    return member.rstrip("/")


def not_a_nested_stack_of_parent(member: str) -> Verdict:
    """The parent does not deploy this, so a parent parameter cannot reach it.

    True when the member is neither a nested stack's source template nor a path
    inside one's directory. This is the premise that was false for
    ``feature-platform/main-stack-extensions`` in three separate lists at once.
    """
    target = _normalise(member)
    for logical_id, source in sorted(nested_stack_sources().items()):
        stack_dir = str(Path(source).parent)
        if (
            target == source
            or target == stack_dir
            or target.startswith(stack_dir + "/")
        ):
            return (
                False,
                f"{member} belongs to {source}, which {PARENT_TEMPLATE} deploys as "
                f"nested stack {logical_id} — the parent's parameters reach it",
            )
    return (
        True,
        f"no AWS::CloudFormation::Stack in {PARENT_TEMPLATE} deploys {member}",
    )


def built_separately_from_main_stack(member: str) -> Verdict:
    """The publisher does not build this in a main-stack publish run.

    True when no component root or build input in the publisher's component map
    covers the member. Nothing else in this repository reads that map, which is why
    two exemption lists could cite "built separately" and neither be checked.
    """
    target = _normalise(member)
    for known in sorted(build_input_paths()):
        if target == known or target.startswith(known.rstrip("/") + "/"):
            owners = sorted(
                component
                for component, deps in built_components().items()
                if known == component or known in deps
            )
            return (
                False,
                f"{member} is covered by build input {known!r} of publisher "
                f"component(s) {owners}, so one publish run builds it alongside "
                "the main stack",
            )
    return (True, f"no publisher component names {member} as a build input")


def file_absent_or_untracked(member: str) -> Verdict:
    """Nothing in the repository occupies this path, so the gate has nothing to see.

    The honest premise for an exemption covering a path that only exists after a
    build. It is deliberately narrow: an exemption whose path *does* exist has to
    justify itself some other way.
    """
    if is_tracked(_normalise(member)):
        return (False, f"git tracks {member}, so the gate would see it")
    return (True, f"git does not track {member}")


def vcs_ignored_build_output(member: str) -> Verdict:
    """An ignore rule covers this path and git tracks nothing under it.

    The premise for an exemption covering a tree that only exists after a local
    build: a gate that walks the filesystem reaches it, a gate that asks git never
    does, and nothing shipped from this repository lives there. Two things are
    measured, and the exemption is only sound with both:

    * **An ignore rule covers the path**, reported with the file and line that
      states it. That is the durable half — while the rule stands, a file under
      this path cannot become tracked without the rule being edited, so the
      exclusion cannot silently grow to hide real code.
    * **Git tracks no file under it**, which is what makes the exclusion cost
      nothing today. This is a prefix question, not a question about one path, and
      that is why :func:`file_absent_or_untracked` does not answer it: that
      predicate asks whether git tracks *exactly* the named path, so for any
      directory it returns True whatever the directory contains.

    ⚠️ The ignore query is made with a **trailing slash**. An ignore pattern
    written ``build/`` matches directories only, and ``git check-ignore`` cannot
    tell that a path is a directory when the path is not on disk — so probing the
    bare path answers "not ignored" on a clean checkout and "ignored" on a machine
    that has run the build. A premise whose verdict depends on whether you have
    built locally is the shape of gate this module exists to stop.
    """
    target = _normalise(member)
    tracked = tracked_files(target, prune_local_work=False)
    if tracked:
        return (
            False,
            f"git tracks {len(tracked)} file(s) under {member} (e.g. {tracked[0]}), "
            "so this exclusion hides code that ships from this repository",
        )
    probed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-v", "--no-index", target + "/"],
        capture_output=True,
        text=True,
    )
    if probed.returncode != 0:
        return (
            False,
            f"no ignore rule covers {member}, so a tracked file can appear under it "
            "without any edit to an ignore file and this exclusion would hide it",
        )
    rule = probed.stdout.split("\t", 1)[0].strip()
    return (
        True,
        f"{rule} ignores {member} and git tracks no file under it",
    )


def vcs_ignored_generated_filename(member: str) -> Verdict:
    """An ignore rule covers every file this *filename glob* can match, and none is tracked.

    The premise for excluding a **generated artifact named by its filename** rather
    than by the directory it lands in — a tool that writes ``<something>-converted.py``
    beside the notebook it converted, and removes it when it finishes. Such a file has
    no fixed home, so :func:`vcs_ignored_build_output` cannot answer for it: that
    predicate probes one path with a trailing slash because it is asking about a
    directory, and a filename glob is not one.

    Three things are measured, and the exclusion is only sound with all three:

    * **An ignore rule covers the shape**, reported with the file and line that
      states it. While that rule stands, a file of this shape cannot become tracked
      without the rule being edited, so the exclusion cannot silently grow to cover
      code that ships.
    * **Git tracks nothing matching it**, which is what makes the exclusion cost
      nothing. Unlike the directory case this is asked of the glob itself, so a
      committed ``foo-converted.py`` fails here rather than being absorbed.
    * **The final component is a wildcard over filenames, not a bare directory
      name.** ``notebooks`` and ``**/build`` exclude a tree and would keep excluding
      it as the tree grew; ``**/*-converted.py`` cannot widen beyond the suffix
      without the pattern being rewritten. A predicate that accepted either spelling
      would let the narrow claim be made about the broad one.

    The probe path is derived from the glob rather than from any file on disk,
    because the whole point of this class of artifact is that it is usually absent —
    a premise whose verdict depended on whether a scan happened to be running would
    be the shape of gate this module exists to stop.
    """
    pattern = member.strip()
    final = pattern.rsplit("/", 1)[-1]
    if "*" not in final:
        return (
            False,
            f"{member} names no wildcard in its final component, so it excludes a "
            "tree rather than a generated filename shape and can widen as that tree "
            "grows without the pattern being edited",
        )
    tracked = tracked_files(pattern, prune_local_work=False)
    if tracked:
        return (
            False,
            f"git tracks {len(tracked)} file(s) matching {member} (e.g. {tracked[0]}), "
            "so this exclusion hides code that ships from this repository",
        )
    probe = pattern.replace("**/", "").replace("*", "_gate_premise_probe_")
    probed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-v", "--no-index", probe],
        capture_output=True,
        text=True,
    )
    if probed.returncode != 0:
        return (
            False,
            f"no ignore rule covers {probe} (derived from {member}), so a file of "
            "this shape can be committed without any edit to an ignore file and this "
            "exclusion would hide it",
        )
    rule = probed.stdout.split("\t", 1)[0].strip()
    return (
        True,
        f"{rule} ignores {probe} and git tracks no file matching {member}",
    )


def installer_manifest_pins_parameter(
    member: str, parameter: str, allowed: Collection[object] | None = None
) -> Verdict:
    """A feature's ``feature.yaml`` sets ``defaultParameters.<parameter>``.

    Named for exactly what it proves and no more: that a manifest value exists for
    an installer to pass. It deliberately does NOT claim the broader "the template
    default is never reached", because that depends on the install path — the
    console launch-URL flow folds ``defaultParameters`` in, and
    ``idp-feature-cli deploy`` passes the parameter only when told to. A predicate
    that claimed the broader thing would be the same kind of unchecked promise this
    module exists to stop.

    Where it does not hold — the directory has no manifest, or the manifest is
    silent about this parameter — the template's own ``Default`` is what a
    deployment gets, and an exclusion resting on the manifest is resting on nothing.
    """
    manifest = f"{_normalise(member)}/feature.yaml"
    if not is_tracked(manifest):
        return (False, f"{manifest} does not exist, so no manifest value is passed")
    loaded = yaml.safe_load((REPO_ROOT / manifest).read_text(encoding="utf-8")) or {}
    pinned = (loaded.get("defaultParameters") or {}).get(parameter)
    if pinned is None:
        return (
            False,
            f"{manifest} declares no defaultParameters.{parameter}, so the "
            "template default is what reaches CloudFormation",
        )
    # Existence is not safety. Without `allowed`, this predicate answered True for
    # `LogLevel: DEBUG` -- one of the two values the calling gate's own failure message
    # names as unsafe -- because it only asked whether a value was pinned at all. An
    # exemption resting on "the installer supplies the value" is only sound if the value
    # it supplies is one the gate would have accepted.
    #
    # `allowed` is a SET rather than a single expected value because the caller's
    # tolerance is not always the caller's ideal: the LogLevel gate accepts the INFO
    # these manifests pin today as a recorded residual, while refusing DEBUG. That makes
    # this a ratchet -- the pinned value may improve and may not regress -- which is the
    # honest shape when the current state is known and accepted rather than desired.
    if allowed is not None and pinned not in allowed:
        return (
            False,
            f"{manifest} pins defaultParameters.{parameter}={pinned!r}, which is not "
            f"one of {sorted(map(str, allowed))}: the installer does supply a value, "
            "and it is not one the gate would have accepted",
        )
    return (True, f"{manifest} pins defaultParameters.{parameter}={pinned!r}")


def matching_lines(
    rel_path: str, needle: str, *, exclude: tuple[str, ...] = ()
) -> tuple[tuple[int, str], ...]:
    """``(line number, text)`` for each line containing ``needle`` and no ``exclude``.

    The primitive behind non-vacuity: an exemption that hides zero lines has no
    expressible reason, and one that hides more lines than were audited has grown
    past its justification.
    """
    # A path git reports but that is not on disk (staged deletion, a broken symlink)
    # must make the gate FAIL rather than raise: an uncaught OSError aborts the whole
    # suite with a traceback instead of naming the problem, and the two tracked-file
    # helpers in this tree differ on exactly this point.
    try:
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise AssertionError(
            f"{rel_path} is reported by git but cannot be read ({exc}). A staged "
            "deletion or a broken link leaves the gate unable to measure this file; "
            "commit the deletion or restore the file."
        ) from exc
    hits = []
    for number, line in enumerate(text.splitlines(), start=1):
        if needle not in line:
            continue
        if line.lstrip().startswith("#"):
            continue
        if any(skip in line for skip in exclude):
            continue
        hits.append((number, line))
    return tuple(hits)


def collects_zero_tests(member: str) -> Verdict:
    """``pytest --collect-only`` finds no test in this directory.

    The premise behind "not a test suite" / "collects zero pytest tests". It is worth
    computing because the identical sentence has already been wrong once here: the
    same claim was made about ``samples/lambda-hook-inference/GENAIIDP-w2-copy-consistency``
    and that directory collects six tests, all passing, so six gated tests were
    excluded from every gate on the strength of a reason nobody ran.

    A collection **error** is reported as *not holding*, deliberately. Zero tests
    collected because an import failed is a different premise -- "requires a
    dependency this environment does not have" -- and conflating the two would let a
    dependency problem masquerade as "there is nothing here", which is the more
    dangerous direction: the tests exist and nobody runs them.
    """
    target = REPO_ROOT / _normalise(member)
    if not target.is_dir():
        return (False, f"{member} is not a directory")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            str(target),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    out = result.stdout + result.stderr
    # pytest's exit code is the reliable signal, not the summary text: a collection
    # error ALSO prints "no tests collected", so substring matching reports an
    # unimportable suite as an empty one -- the exact conflation the docstring above
    # says must not happen. 5 = nothing collected, 2 = interrupted by a collection
    # error, 0 = tests were collected.
    if result.returncode == 2:
        return (
            False,
            f"collection ERRORED in {member} rather than finding nothing, so the "
            "premise to record is the unavailable dependency; the tests may well "
            "exist and simply never run",
        )
    if result.returncode == 5:
        return (True, f"pytest collects no test in {member}")
    match = re.search(r"^(\d+) tests? collected", out, re.M)
    collected = match.group(1) if match else "some"
    return (False, f"{member} collects {collected} test(s), which nothing runs")


#: Predicates a registry entry may name, by the exact string it names them with.
#: ``JUDGEMENT`` is deliberately absent: it is not a predicate, it is the recorded
#: admission that there is no predicate, and the registry treats it separately.
#
#: ``installer_manifest_pins_parameter`` takes the parameter name as a second
#: argument, so a caller binds it rather than calling it bare. It is listed here
#: because the registry names predicates by string and must be able to resolve it.
PREDICATES = {
    "not_a_nested_stack_of_parent": not_a_nested_stack_of_parent,
    "built_separately_from_main_stack": built_separately_from_main_stack,
    "file_absent_or_untracked": file_absent_or_untracked,
    "vcs_ignored_build_output": vcs_ignored_build_output,
    "vcs_ignored_generated_filename": vcs_ignored_generated_filename,
    "installer_manifest_pins_parameter": installer_manifest_pins_parameter,
    "collects_zero_tests": collects_zero_tests,
}

#: The marker an entry uses instead of a predicate when its premise genuinely
#: cannot be computed here. It is not an escape from scrutiny — the registry still
#: requires a reason, and the non-vacuity and count ratchets still apply.
JUDGEMENT = "JUDGEMENT"
