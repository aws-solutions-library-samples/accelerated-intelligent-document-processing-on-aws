# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pin the property that GitHub's gates measure head-merged-into-base.

``actions/checkout``'s default ``ref`` on a ``pull_request`` event is the **merge
ref** — ``refs/pull/<n>/merge``, the result of merging the pull request's head into
its base — and no workflow in this repository passes a ``ref:``, so every gate
already runs against that merge result rather than against either side. From a real
run's log::

    git fetch ... +b2d83fda3...:refs/remotes/pull/1424/merge
    git checkout --progress --force refs/remotes/pull/1424/merge
    HEAD is now at b2d83fda3 Merge f0c79c2565a... into 32415670df...

That default is doing real work. `#1407
<https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1407>`_
is about a defect class where two pull requests are individually green, do not
conflict, and are nonetheless untested in combination — one adds a surface whose
scope is **lexical rather than referential** (an ``autouse`` fixture, a
``conftest.py``, a pytest configuration key), so its subject set is "whatever is in
this directory at run time" and it grows under it without any diff touching it. The
counter-check that issue asks for — run the affected tree on the merge result, not
on either side — is what the GitHub default already performs, for free, on every
pull request.

**What is unprotected is the default itself.** One line on a checkout step::

    ref: ${{ github.event.pull_request.head.sha }}

is a reasonable-looking thing to add — it is the usual fix for making a ``git diff``
against the base behave — and it silently converts every gate in that workflow from
measuring the merge result to measuring the pull request head. Nothing would fail.
What would stop happening is invisible by exactly the argument #1407 makes: there is
no conflict to see, no failing gate, and no diff overlap. This module is the thing
that fails instead.

**The property asserted is presence, not value.** A checkout step in a
``pull_request``-triggered workflow must not name a ``ref`` at all. Deciding whether
a given expression *evaluates to* the merge ref is evaluation rather than reading,
which this suite's sibling module declines to attempt for CI conditions and declines
to attempt here for the same reason; and the question does not arise, because the
default already **is** the merge ref, so a value that is correct is redundant and a
value that is not is the defect.

**Every ``pull_request``-triggered workflow is in scope, gate or not.** The two gate
workflows are derived rather than listed (see :func:`gate_workflows`) and are what
makes the pass non-vacuous, but the assertion is not narrowed to them. Two
``pull_request`` workflows here are path-filtered and are not gates — the docs build
and the dependency-manifest generator — and both still produce a mark a reviewer
reads as being about this pull request's code. If either measured the head instead,
that mark would be about a tree nobody is merging, in the same way and for the same
reason. Narrowing to gates would also add a classification step between the scan and
the assertion, and a classification that silently matched nothing is the way a guard
like this stops guarding anything; the trigger is the simplest derivable property and
is the one the GitHub default attaches to.

**One workflow may name a ref, and the reason is written beside it** —
:data:`REVIEWS_THE_CONTRIBUTION`. The advisory AI review reviews the *contribution*
rather than the merge result, and its scheduled sweep covers fork pull requests for
which no merge ref is reachable, so a head checkout there is correct. Today it
expresses that inside ``scripts/sdlc/ai_mr_review.py`` (``fetch_head`` fetches
``refs/pull/<n>/head``) rather than at the checkout step, so the entry shields
nothing in the tree as it stands. It is covered by a test instead of by the tree:
:func:`test_the_review_workflow_may_check_out_the_pull_request_head` drives the real
scanner over a copy of that workflow carrying a head ``ref:``, and the same test
shows a finding appearing once the entry is taken away — so an entry that stopped
being honoured, or that was widened to everything, fails here.

**Three things this module cannot see**, stated so a green run is not over-read:

* a checkout performed by a ``run:`` step (``git checkout <sha>``) rather than by the
  action. Reading shell to decide which tree it leaves behind is beyond what a
  text scan can claim.
* a checkout inside a composite action, or inside a reusable workflow a job
  delegates to with a job-level ``uses:``. Neither exists here, and
  :func:`test_every_checkout_in_this_repository_is_visible_to_this_scan` fails if one
  appears, so the boundary stays empty rather than being assumed empty.
* a third-party action that clones the repository under some other name. Only
  ``actions/checkout`` is recognised, by name.

GitLab is a separate question this module does not answer. Merged-results pipelines
are a GitLab **project setting** rather than a key in ``.gitlab-ci.yml``, so whether
its merge-request pipelines measure a merge result is not readable from this tree at
all; #1407's comment records that as an open residual.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
COMPOSITE_ACTIONS_DIR = REPO_ROOT / ".github" / "actions"
BRANCH_PROTECTION_SCRIPT = REPO_ROOT / "scripts" / "sdlc" / "check_branch_protection.py"

#: The branch whose pull requests the gate classification is derived for. Eligibility
#: is a property of the workflow *and* the branch: a ``pull_request: branches:``
#: filter decides whether a workflow runs on a pull request targeting a given base,
#: and this repository's pull requests target ``develop``.
GATE_BRANCH = "develop"

#: Workflows whose ``actions/checkout`` step may name a ``ref``, mapped to why. The
#: value is the reason, and it is reproduced in the failure message so it is read at
#: the point where it matters rather than here.
#:
#: Membership is one workflow's worth of reason, keyed by file name. See this module's
#: docstring for what covers the entry in place of an in-tree finding.
REVIEWS_THE_CONTRIBUTION = {
    "ai-pr-review.yml": (
        "the advisory AI review reviews the contribution rather than the merge "
        "result, and its scheduled sweep reviews fork pull requests for which no "
        "merge ref is reachable, so a head checkout is the correct tree there. It "
        "approves nothing and blocks nothing, so its mark is not read as 'the code "
        "is fine'."
    ),
}


@dataclass(frozen=True)
class PinnedRef:
    """One ``actions/checkout`` step that names a ``ref``."""

    workflow: str
    job: str
    step: str
    ref: str

    def __str__(self) -> str:  # pragma: no cover - failure-message rendering
        return (
            f"{self.workflow} job {self.job!r} step {self.step!r} sets "
            f"ref: {self.ref!r}"
        )


@dataclass(frozen=True)
class Scan:
    """What one pass over a workflows directory saw."""

    findings: tuple[PinnedRef, ...]
    pull_request_workflows: tuple[str, ...]
    gate_workflows: tuple[str, ...]
    #: ``{workflow name: checkout steps found in it}``, per workflow rather than as
    #: one total. A single total passes a ``>= 1`` floor while every gate workflow in
    #: it contributes nothing, which is the vacuous pass this module is guarding
    #: against one level down.
    checkouts_by_workflow: dict[str, int]

    @property
    def checkout_steps(self) -> int:
        return sum(self.checkouts_by_workflow.values())

    @property
    def gate_checkout_steps(self) -> int:
        return sum(
            count
            for name, count in self.checkouts_by_workflow.items()
            if name in self.gate_workflows
        )


def _load_branch_protection():
    """Load ``check_branch_protection.py`` by path (``scripts/sdlc`` is not importable).

    Registered in :data:`sys.modules` **before** execution: the script defines
    ``@dataclass`` types and ``dataclasses`` resolves ``cls.__module__`` through
    ``sys.modules``, so an unregistered module fails with an obscure
    ``AttributeError`` from inside the standard library. Same loader shape as
    ``scripts/tests/test_check_branch_protection.py``.
    """
    spec = importlib.util.spec_from_file_location(
        "_merge_ref_branch_protection", BRANCH_PROTECTION_SCRIPT
    )
    assert spec and spec.loader, f"cannot load {BRANCH_PROTECTION_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def workflow_paths(workflows_dir: Path) -> list[Path]:
    """Every workflow file, discovered at run time.

    ``*.y*ml`` rather than ``*.yml`` because GitHub Actions reads both extensions, so
    a workflow added as ``.yaml`` would otherwise sit outside a universe this module
    calls closed — the same reason ``scripts/tests/test_ci_gate_parity.py`` gives for
    the same glob.
    """
    return sorted(workflows_dir.glob("*.y*ml"))


def _triggers(data: dict) -> dict:
    """A workflow's ``on:`` block as a mapping of trigger name to its filters.

    ``on`` is a YAML 1.1 boolean, so ``yaml.safe_load`` yields the key ``True``.
    Reading only ``"on"`` makes every workflow look trigger-less, which would empty
    the universe below and pass forever.
    """
    raw = data.get(True, data.get("on"))
    if isinstance(raw, str):
        return {raw: {}}
    if isinstance(raw, list):
        return {str(item): {} for item in raw}
    if isinstance(raw, dict):
        return {str(key): (value or {}) for key, value in raw.items()}
    return {}


def _is_checkout(step: dict) -> bool:
    """Does this step run ``actions/checkout``?

    Matched on the action path with any ``@ref`` removed and case folded, because
    GitHub resolves ``uses:`` case-insensitively and the version here is a pinned
    commit SHA rather than a tag. A fork of the action under another name is not
    recognised; that boundary is in the module docstring.
    """
    uses = str(step.get("uses", "")).split("@", 1)[0].strip().strip("/").lower()
    return uses.endswith("actions/checkout")


def _named_ref(step: dict) -> str | None:
    """The ``ref`` this checkout step passes, or None.

    Keys are compared lower-cased and stripped. GitHub passes a ``with:`` entry to an
    action as ``INPUT_<KEY>`` upper-cased, so ``Ref:`` and ``ref :`` reach
    ``actions/checkout`` as the same input a plain ``ref:`` does — a case- or
    whitespace-sensitive comparison here would read as this module examining the step
    and finding nothing.
    """
    params = step.get("with")
    if not isinstance(params, dict):
        return None
    for key, value in params.items():
        if str(key).strip().lower() == "ref":
            return str(value)
    return None


def gate_workflows(workflows_dir: Path, branch: str = GATE_BRANCH) -> set[str]:
    """Workflows whose green mark is read as "the gates passed", derived.

    Derived rather than listed, for the reason
    ``scripts/sdlc/check_branch_protection.py`` gives for parsing the workflow files
    in the first place: a hardcoded inventory drifts the moment a workflow or a job is
    renamed. That script already computes what is needed — for each job it reads the
    gate commands the job's steps run (resolved against the real ``make`` targets, so
    prose like ``apt-get install make curl`` is not mistaken for a gate) and whether
    the job's context reports on every pull request targeting ``branch``. A workflow
    is a gate workflow here when it has at least one job that is **both**.

    Both halves are needed. Without the gate-command half, the path-filtered docs
    build would qualify. Without the eligibility half, the advisory AI review would
    qualify — its ``run:`` step invokes a ``scripts/sdlc/*.py`` path, which is
    gate-shaped to a text scan — and this classification would then start calling the
    one workflow that legitimately checks out a head a gate workflow. That the
    assertion permits it by name anyway is what keeps this from being load-bearing in
    the wrong direction.

    This set is **not** the scope of the assertion, which covers every
    ``pull_request``-triggered workflow; it is what makes the passing case
    non-vacuous, so a parsing change that stopped finding gate workflows fails rather
    than passing quietly.
    """
    module = _load_branch_protection()
    return {
        context.workflow
        for context in module.discover_check_contexts(workflows_dir, branch)
        if context.required_eligible and context.gates
    }


def scan(
    workflows_dir: Path | None = None,
    reviews_the_contribution: dict[str, str] | None = None,
    branch: str = GATE_BRANCH,
) -> Scan:
    """Find every ``pull_request`` workflow checkout step that names a ``ref``.

    ``workflows_dir`` and ``reviews_the_contribution`` are arguments so the tests can
    drive this exact function over a synthetic checkout and with the allowance
    withdrawn, rather than asserting that the scan works by reading the code that
    implements it. ``workflows_dir`` defaults to ``None`` and resolves
    :data:`WORKFLOWS_DIR` inside the body rather than taking it as a default
    *expression*, which Python would bind once at definition time and leave
    unaffected by a ``monkeypatch.setattr`` — that is how the real assertion comes to
    be exercised against a synthetic directory below.
    """
    workflows_dir = workflows_dir or WORKFLOWS_DIR
    permitted = (
        REVIEWS_THE_CONTRIBUTION
        if reviews_the_contribution is None
        else reviews_the_contribution
    )
    gates = gate_workflows(workflows_dir, branch)

    findings: list[PinnedRef] = []
    pull_request_workflows: list[str] = []
    checkouts: dict[str, int] = {}

    for path in workflow_paths(workflows_dir):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        if "pull_request" not in _triggers(data):
            continue
        pull_request_workflows.append(path.name)
        checkouts.setdefault(path.name, 0)
        jobs = data.get("jobs") or {}
        if not isinstance(jobs, dict):
            continue
        for job_id, job in jobs.items():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                if not isinstance(step, dict) or not _is_checkout(step):
                    continue
                checkouts[path.name] += 1
                ref = _named_ref(step)
                if ref is None or path.name in permitted:
                    continue
                findings.append(
                    PinnedRef(
                        workflow=path.name,
                        job=str(job_id),
                        step=str(step.get("name") or step.get("uses") or "<unnamed>"),
                        ref=ref,
                    )
                )

    return Scan(
        findings=tuple(findings),
        pull_request_workflows=tuple(pull_request_workflows),
        gate_workflows=tuple(sorted(gates)),
        checkouts_by_workflow=checkouts,
    )


def _synthetic_workflows(tmp_path: Path) -> Path:
    """A copy of the real workflows directory, for a test to mutate.

    The whole directory, not one file: the gate classification is derived across all
    of them, so a single-file copy would classify against a universe of one and prove
    less than it looks like.
    """
    target = tmp_path / "workflows"
    shutil.copytree(WORKFLOWS_DIR, target)
    return target


def _pin_first_checkout(path: Path, ref: str, *, key: str = "ref") -> str:
    """Add ``<key>: <ref>`` to the first ``actions/checkout`` step in ``path``.

    Edits the text rather than round-tripping the YAML, because ``yaml.safe_dump``
    would rewrite the file wholesale and lose the comments the other assertions in
    this suite read. Returns the job id the mutated step belongs to, so the caller
    asserts against a job name it derived rather than one it hardcoded.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    target_job = next(
        job_id
        for job_id, job in (data.get("jobs") or {}).items()
        for step in (job.get("steps") or [])
        if isinstance(step, dict) and _is_checkout(step)
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("- ") and not stripped.startswith("uses:"):
            continue
        if "actions/checkout" not in stripped:
            continue
        indent = " " * (len(line) - len(line.lstrip()))
        # `uses:` sits one level inside the step when the step starts `- name:`, and
        # at the step's own indent when it starts `- uses:`. Either way `with:` goes
        # at the same indent as `uses:`.
        if stripped.startswith("- "):
            indent += "  "
        insert = [f"{indent}with:", f"{indent}  {key}: {ref}"]
        if index + 1 < len(lines) and lines[index + 1].strip() == "with:":
            insert = [f"{indent}  {key}: {ref}"]
            lines[index + 2 : index + 2] = insert
        else:
            lines[index + 1 : index + 1] = insert
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(target_job)
    raise AssertionError(f"no actions/checkout step found in {path}")


def _measured(result: Scan) -> str:
    """What the scan examined, for whichever assertion message reports it."""
    return (
        f"examined {len(result.pull_request_workflows)} pull_request-triggered "
        f"workflow(s) {list(result.pull_request_workflows)}, of which "
        f"{len(result.gate_workflows)} run gates {list(result.gate_workflows)}, "
        f"and {result.checkout_steps} actions/checkout step(s) "
        f"({result.gate_checkout_steps} of them in a gate workflow)"
    )


def _failure_message(result: Scan) -> str:
    """The message the real assertion raises, so the red case can read it.

    Factored out rather than written twice: a red-case test asserting against its own
    copy of the wording would pass while the live message named neither the workflow
    nor the job, which is the whole of what makes a finding actionable.
    """
    return (
        "a pull_request-triggered workflow pins its checkout to a ref, so its jobs "
        "no longer measure head-merged-into-base:\n"
        + "\n".join(f"  - {finding}" for finding in result.findings)
        + "\n\nactions/checkout's default ref on a pull_request event IS the merge "
        "ref, so these jobs now measure one side of the merge instead of the "
        "result. That silently removes the counter-check #1407 is about: two "
        "individually-green pull requests that do not conflict are no longer "
        "tested in combination, and nothing else in this repository would notice. "
        "If a step needs the base branch, fetch it in a `run:` step and diff "
        "against it rather than changing what is checked out.\n"
        f"({_measured(result)}.)\nThe one workflow that may name a ref, and why: "
        + "; ".join(f"{name} — {why}" for name, why in REVIEWS_THE_CONTRIBUTION.items())
    )


@pytest.mark.unit
def test_no_pull_request_workflow_pins_the_checkout_to_a_ref() -> None:
    """Every gate runs on head-merged-into-base, because nothing names a ref.

    See this module's docstring for why that property matters and what it is
    protecting. The remedy for a failure here is almost never to register the
    workflow: it is to get whatever needed a ref some other way — fetch the base ref
    in a ``run:`` step and diff against it, which costs one line and leaves the
    checked-out tree alone.
    """
    result = scan()

    # Non-vacuity first: a scan that found nothing passes the assertion below for
    # free, and a parsing change that silently matched nothing would make it pass
    # forever. That is the failure mode this guard is least able to notice about
    # itself, so the counts are asserted rather than merely reported.
    assert result.pull_request_workflows, (
        "no pull_request-triggered workflow was discovered under "
        f"{WORKFLOWS_DIR}, so this assertion checked nothing. Either the "
        "directory moved or the `on:`/`True` key handling in _triggers() has "
        "stopped reading the trigger block."
    )
    assert result.gate_workflows, (
        f"no gate workflow was derived ({_measured(result)}). The gate "
        "classification comes from scripts/sdlc/check_branch_protection.py; if it "
        "now returns nothing, this module's passing case says nothing about the "
        "workflows whose green mark is read as 'the gates passed'."
    )
    unexamined = [
        name
        for name in result.gate_workflows
        if not result.checkouts_by_workflow.get(name)
    ]
    assert not unexamined, (
        f"these gate workflows yielded no actions/checkout step: {unexamined} "
        f"({_measured(result)}). Asserted per workflow rather than as a total, "
        "because one total satisfies a floor while the workflows that matter "
        "contribute nothing to it — which is the vacuous pass this module exists "
        "to prevent, one level down."
    )
    assert result.gate_checkout_steps >= 1, (
        f"no actions/checkout step was found inside a gate workflow "
        f"({_measured(result)}). A scan that examines no checkout step cannot "
        "report a ref on one."
    )

    assert not result.findings, _failure_message(result)


@pytest.mark.unit
def test_the_scan_fires_when_a_gate_workflow_pins_the_pull_request_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The red case, constructed: one plausible line on a gate workflow's checkout.

    This is the mutation the guard exists for — ``ref: ${{
    github.event.pull_request.head.sha }}`` on a checkout step, which is what someone
    adds to make a ``git diff`` against the base behave. Asserting that the real
    scanner reports it, and names the workflow and the job, is the only evidence that
    a passing run on the real tree means anything.

    The last part runs
    :func:`test_no_pull_request_workflow_pins_the_checkout_to_a_ref` itself against
    the mutated copy, rather than stopping at :func:`scan`'s return value. Measured by
    mutation sampling: with the real assertion inverted or deleted, a red case that
    only reads ``scan()`` stays green, so the guard's own assertion was the one line
    in the module nothing covered.
    """
    synthetic = _synthetic_workflows(tmp_path)
    gates = sorted(gate_workflows(synthetic))
    assert gates, "the gate classification found nothing in the synthetic copy"
    victim = synthetic / gates[0]
    job = _pin_first_checkout(victim, "${{ github.event.pull_request.head.sha }}")

    result = scan(synthetic)

    assert [(f.workflow, f.job) for f in result.findings] == [(victim.name, job)], (
        f"expected exactly one finding, naming {victim.name} job {job!r}; got "
        f"{[str(f) for f in result.findings]}"
    )
    assert "head.sha" in result.findings[0].ref

    # The message a developer actually sees has to name the workflow and the job, or
    # the finding is unactionable.
    message = _failure_message(result)
    assert victim.name in message and job in message, message

    monkeypatch.setattr(sys.modules[__name__], "WORKFLOWS_DIR", synthetic)
    with pytest.raises(AssertionError) as raised:
        test_no_pull_request_workflow_pins_the_checkout_to_a_ref()
    assert victim.name in str(raised.value) and job in str(raised.value), (
        "the live assertion did not refuse a gate workflow pinned to the pull "
        f"request head: {raised.value}"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "label,key,ref",
    [
        # GitHub passes a `with:` key to an action as `INPUT_<KEY>` upper-cased, so
        # `actions/checkout` receives `Ref:` as its `ref` input and checks out that
        # tree. A case-sensitive comparison here would examine the step, find no
        # `ref`, and report the workflow as clean.
        ("capitalised", "Ref", "${{ github.sha }}"),
        ("upper-cased", "REF", "${{ github.sha }}"),
        # Whitespace comes in two shapes and only one of them needs quoting. A YAML
        # PLAIN key written `ref :` already parses to `ref`, so a padded plain key
        # cannot be constructed; a QUOTED one can, and is reported, because the key
        # comparison strips. Whether GitHub honours `"ref "` as the action's `ref`
        # input is not decided here and does not need to be: reporting it is the safe
        # direction, since the worst case is refusing a key nobody would write.
        ("quoted padded key", '"ref "', "${{ github.sha }}"),
        # The value is never compared against anything, which is why presence rather
        # than value is the property asserted — a scan matching the literal head
        # expression would miss a padded or differently-spelled one.
        ("padded value", "ref", '"  ${{ github.event.pull_request.head.sha }}  "'),
        # The plainest shape, kept in the table so the three are read together.
        ("plain", "ref", "${{ github.event.pull_request.head.sha }}"),
    ],
)
def test_the_ref_input_is_matched_however_it_is_spelled(
    tmp_path: Path, label: str, key: str, ref: str
) -> None:
    """Every spelling that reaches ``actions/checkout`` as its ``ref`` is reported.

    Each of these is a way the guard could be made to look like it is checking while
    matching nothing, which is the shape of pass this module is least able to notice
    about itself.
    """
    synthetic = _synthetic_workflows(tmp_path)
    victim = synthetic / sorted(gate_workflows(synthetic))[0]
    _pin_first_checkout(victim, ref, key=key)
    result = scan(synthetic)
    assert len(result.findings) == 1, (
        f"the {label} spelling ({key}: {ref}) was not reported: "
        f"{[str(f) for f in result.findings]}"
    )


@pytest.mark.unit
def test_the_review_workflow_may_check_out_the_pull_request_head(
    tmp_path: Path,
) -> None:
    """The advisory AI review's head checkout is correct and is not reported.

    Shown directly rather than inferred from the real tree passing: the workflow does
    not name a ref today — ``scripts/sdlc/ai_mr_review.py`` fetches
    ``refs/pull/<n>/head`` itself — so the real tree would pass with the allowance
    deleted, and a guard that breaks the review workflow the first time it expresses
    that at the checkout step would be a regression this guard caused.

    The second half is what keeps the entry honest. With
    :data:`REVIEWS_THE_CONTRIBUTION` withdrawn the same workflow IS reported, so the
    entry is doing the work rather than the mutation being inert — and an entry
    widened to every workflow fails
    :func:`test_the_scan_fires_when_a_gate_workflow_pins_the_pull_request_head`
    instead.
    """
    reviewer = next(iter(REVIEWS_THE_CONTRIBUTION))
    synthetic = _synthetic_workflows(tmp_path)
    assert (synthetic / reviewer).is_file(), (
        f"{reviewer} is registered as reviewing the contribution but is not a "
        "workflow in this repository, so the entry describes nothing"
    )
    job = _pin_first_checkout(
        synthetic / reviewer,
        "refs/pull/${{ github.event.pull_request.number }}/head",
    )

    # Findings are filtered to the review workflow rather than compared against an
    # empty tuple: the synthetic copy carries every other workflow too, so an
    # unrelated finding would otherwise fail this test for a reason that has nothing
    # to do with what it is measuring.
    permitted = [f for f in scan(synthetic).findings if f.workflow == reviewer]
    assert not permitted, (
        f"{reviewer} checks out the pull request head on purpose and must not be "
        f"reported: {[str(f) for f in permitted]}"
    )
    assert reviewer in scan(synthetic).pull_request_workflows, (
        f"{reviewer} was not even examined, so the clean result above says nothing "
        "about whether the allowance is what permitted it"
    )

    withdrawn = scan(synthetic, reviews_the_contribution={})
    assert [
        (f.workflow, f.job) for f in withdrawn.findings if f.workflow == reviewer
    ] == [(reviewer, job)], (
        "with the allowance withdrawn the review workflow's head checkout should be "
        f"reported; got {[str(f) for f in withdrawn.findings]}. The allowance is "
        "therefore not what made the case above pass, and this test proves nothing."
    )


@pytest.mark.unit
def test_every_checkout_in_this_repository_is_visible_to_this_scan() -> None:
    """Reading workflow files alone sees every checkout — measured, not assumed.

    Two ways a checkout could sit where :func:`scan` never looks: inside a composite
    action a step ``uses:``, or inside a reusable workflow a job delegates to with a
    job-level ``uses:``. Neither exists here, so the boundary in the module docstring
    is currently empty; this fails the day one appears, which is when the scan has to
    learn to follow it rather than when somebody happens to re-read the docstring.
    """
    composite = sorted(COMPOSITE_ACTIONS_DIR.rglob("action.y*ml"))
    assert composite, (
        f"no composite action found under {COMPOSITE_ACTIONS_DIR} — discovery has "
        "stopped reaching them, so the assertion below is vacuous"
    )
    with_checkout = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in composite
        if any(
            _is_checkout(step)
            for step in (
                (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("runs", {})
                or {}
            ).get("steps")
            or []
            if isinstance(step, dict)
        )
    ]
    assert not with_checkout, (
        f"these composite actions run actions/checkout: {with_checkout}. A checkout "
        "there is invisible to scan(), which reads workflow files only, so the "
        "merge-ref property can now be lost in a file this module does not open. "
        "Teach _is_checkout()'s caller to follow a step's `uses: ./...` into the "
        "action it names."
    )

    delegating = []
    for path in workflow_paths(WORKFLOWS_DIR):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "pull_request" not in _triggers(data):
            continue
        for job_id, job in (data.get("jobs") or {}).items():
            if isinstance(job, dict) and "uses" in job:
                delegating.append(f"{path.name}:{job_id} -> {job['uses']}")
    assert not delegating, (
        f"these pull_request jobs delegate to a reusable workflow: {delegating}. The "
        "checkout then lives in the called workflow, which scan() does not open, so "
        "its jobs are unexamined while reporting as clean."
    )
