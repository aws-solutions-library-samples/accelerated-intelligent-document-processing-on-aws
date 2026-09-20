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
import re
import subprocess
import sys
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


def tracked_files(
    *globs: str, root: Path | None = None, include_untracked: bool = False
) -> tuple[str, ...]:
    """Repo-relative POSIX paths that git reports, sorted.

    ``globs`` are git pathspecs (``"*.py"``, ``"scripts/**"``); with none given,
    every file is returned. Gitignored build output and local work are excluded by
    the same mechanism CI uses, which is the point — a gate that walks the
    filesystem finds ``.aws-sam/`` copies and sibling worktrees and reports
    findings against files that ship nowhere.

    ``include_untracked`` adds files that exist but are not yet committed (still
    honouring ``.gitignore``), matching ``scripts/discover_templates.sh``. Leave it
    off for a gate whose expectation is "what is in the repository"; turn it on for
    one that must see a file the author has not committed yet.
    """
    root = root or REPO_ROOT
    args = ["git", "-C", str(root), "ls-files", "-z", "--cached", "--exclude-standard"]
    if include_untracked:
        args.append("--others")
    if globs:
        args.append("--")
        args.extend(globs)
    out = subprocess.run(args, check=True, capture_output=True, text=True).stdout
    return tuple(sorted(p for p in out.split("\0") if p))


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
    from idp_sdk._core.publish import IDPPublisher

    raw = IDPPublisher.get_component_dependencies(None)  # pyright: ignore[reportArgumentType]
    return {
        component: tuple(dep.removeprefix("./") for dep in deps)
        for component, deps in raw.items()
    }


@functools.lru_cache(maxsize=1)
def build_input_paths() -> frozenset[str]:
    """Every path the publisher names, as a component root or as a build input."""
    paths = set(built_components())
    for deps in built_components().values():
        paths.update(deps)
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


def installer_manifest_pins_parameter(member: str, parameter: str) -> Verdict:
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
    return (True, f"{manifest} pins defaultParameters.{parameter}={pinned!r}")


def matching_lines(
    rel_path: str, needle: str, *, exclude: tuple[str, ...] = ()
) -> tuple[tuple[int, str], ...]:
    """``(line number, text)`` for each line containing ``needle`` and no ``exclude``.

    The primitive behind non-vacuity: an exemption that hides zero lines has no
    expressible reason, and one that hides more lines than were audited has grown
    past its justification.
    """
    text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
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
    "installer_manifest_pins_parameter": installer_manifest_pins_parameter,
}

#: The marker an entry uses instead of a predicate when its premise genuinely
#: cannot be computed here. It is not an escape from scrutiny — the registry still
#: requires a reason, and the non-vacuity and count ratchets still apply.
JUDGEMENT = "JUDGEMENT"
