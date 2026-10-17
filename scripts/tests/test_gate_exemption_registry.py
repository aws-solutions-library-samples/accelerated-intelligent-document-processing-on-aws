# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every gate exemption is registered, and says either what it computes or that it cannot.

THE DEFECT THIS EXISTS FOR. Four exemption lists in this repository stated a premise
that was false for at least one of their members, and in each case the false member
was the one the gate most needed to see: a nested stack exempted as independently
deployed; a Lambda tree exempted as built separately, which the publisher builds in
the same run; a ``LogLevel`` exclusion resting on an installer manifest that one of
the excluded directories does not have; four templates exempted for naming a
commercial-only principal, one of which contains no ARN at all.

Every one is the same bug: **one justification attached to a set, where the
justification is a property of individual members.** Read in aggregate — "does this
reason hold broadly?" — all four pass, which is why all four survived review and, in
two cases, a later audit. Per member, all four fail.

So this module does three things, and none of them is "check the reasons", because no
test can do that:

1. **Membership is derived.** ``exemption_discovery`` finds every exemption surface in
   the tree and this fails in both directions — an unregistered surface, and a
   registered surface that has vanished. The registry is itself a list, and the whole
   subject here is that hand-maintained lists go stale, so only its *judgement* is
   hand-maintained.
2. **A computable premise must be computed.** An entry naming a predicate in
   ``gate_premises`` must belong to a gate that actually calls it, so a premise cannot
   be decorative. ``JUDGEMENT`` is available and is not a loophole: it requires a
   written reason, and the ratchets below still apply.
3. **Every entry declares its ratchet, or declares the gap.** Non-vacuity, count
   pinning, universe closure and staleness each make an exemption finite without
   knowing anything about its reason. Where an entry has none, it must say what is
   consequently unprotected — so the residuals of this mechanism are enumerable rather
   than invisible. An unmeasured gap is how this class started.

WHAT THIS DOES NOT DO. It does not evaluate members. Two of these constants change
after import — one grows five entries in a module-scope loop, one is injected into by
its own tests — so an introspected membership would differ from what any reader of the
file sees. Per-member evaluation belongs in the owning gate, which is the only place
that knows what a member means; this module checks that the owning gate has been made
to do it.
"""

from __future__ import annotations

import ast
import functools
import json
import subprocess
import tempfile
from pathlib import Path

import exemption_discovery
import gate_premises
import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = gate_premises.REPO_ROOT
REGISTRY_PATH = Path(__file__).resolve().parent / "gate_exemptions.json"

#: This module, repo-relative. It is the file an entry is most likely to name as its
#: ratchetEvidence, and the one META_TEST_PERMITTED_RATCHETS below is about.
_THIS_MODULE = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()

KINDS = {"exemption", "enforced-universe", "prune", "fixture", "capability"}
RATCHETS = {
    "non-vacuity",
    "count-pinned",
    "universe-closure",
    "staleness",
    "prune-meta-test",
    "none",
}

#: Entries with no ratchet at all, today. A budget that may SHRINK and never grow.
#:
#: Pinning it is what stops the honest "declare the gap" escape hatch from becoming the
#: default answer to a new exemption: declaring a gap is allowed, and quietly adding one
#: more is not — raising this number is a deliberate edit a reviewer sees.
#:
#: Note that this comment names no figure. The budget moves whenever a gap is honestly
#: declared, so a figure written here would have to be maintained in two places and would
#: go stale in the one comment that explains the incentive the whole mechanism rests on.
#: State the rule, let the assignment below carry the number.
#:
#: ⚠️ The budget can also move for a reason the rule is **not** aimed at: **correcting a
#: label**. An entry claiming a ratchet nothing implements reads as protection that is not
#: there, which is worse than a declared gap, so relabelling it `none` is a move this
#: mechanism is supposed to make attractive even though it raises the number — no
#: exemption is added and nothing becomes less protected. Raising the budget to absorb a
#: NEW exemption is the thing it exists to refuse. An increment of this kind has to carry
#: its own measurement at the pin: what the entry does not check, and evidence that the
#: gap is one the tree exhibits now rather than a theoretical one.
#:
#: The number comes down when a gap is closed, and `scripts/srt/issues.json` is the worked
#: example in both directions. It was relabelled from a staleness ratchet it did not have
#: to `none`, and it is now `non-vacuity`: a suppressed entry whose source is measured and
#: which a scan produces no finding for fails the gate. That check lives in the scan
#: because only the scanner can answer it, and its own residual is written out in that
#: entry's `ratchetGap` rather than being absorbed here.
#:
#: ⚠️ **The third direction, and the only one that moves this budget in bulk: widening
#: DISCOVERY.** Adding a fragment to ``exemption_discovery.NAME_VOCABULARY`` does not add
#: an exemption; it makes one that was already in the tree visible for the first time, and
#: each newly visible surface arrives needing a judgement it has never had. So the budget
#: rises, and nothing became less protected — the opposite. Reading such an increment as
#: the thing this rule refuses would make the correct response "do not widen discovery",
#: which is the defect the whole vocabulary exists for.
#:
#: The measurement for the increment that put this number here, because the argument alone
#: is not enough: the ``NON_``/``OPEN_``/``PERMIT`` fragments discovered **14** surfaces,
#: **12** of which have no ratchet in their owning gate today. Two already had one
#: (``NON_SELECTABLE_DEFAULTS`` a staleness check, ``PERMITTED_DIFFERENCES`` an exact-value
#: pin), and the most consequential of the twelve is
#: ``scan_api_rbac.py::FUNCTION_URL_OPEN_ROUTES`` — an authorization carve-out whose
#: registered sibling in the same file was discovered only because its name contains
#: ``ALLOW``. Each of the twelve names what is unprotected in its own ``ratchetGap``; that
#: is the state the tree was already in, now written down. What this budget still refuses
#: is a NEW exemption arriving with `ratchet: none` and no such reason beside it.
MAX_UNRATCHETED = 70

#: Entries whose premise is computable but whose gate does not yet call the predicate.
#: Same ratchet direction, same reason: this state must not become a comfortable place
#: to leave things.
MAX_PENDING_WIRING = 1


def _predicates_called_in(path: Path) -> set[str]:
    """Predicate names this file actually CALLS, by parsing it.

    A substring search was satisfied by a comment: adding
    ``Premise: not_a_nested_stack_of_parent, per gate_premises.`` to a docstring made an
    entry naming that predicate pass while nothing evaluated it. A registry that asserts
    a fact is checked while nothing checks it is a more convincing version of the defect,
    not a fix for it — so this looks for a call node.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            called.add(func.attr)
        elif isinstance(func, ast.Name):
            called.add(func.id)
    return called


def _registry() -> dict[str, dict]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))["exemptions"]


def test_every_discovered_exemption_is_registered() -> None:
    """A new exemption surface arrives as a failure asking for a decision.

    This is the half that cannot be skipped. An exemption added without a registry
    entry is exactly the shape that has failed here four times: it exists, it turns an
    assertion off, and nothing anywhere asks why.
    """
    discovered = exemption_discovery.discover_all()
    assert len(discovered) >= 60, (
        f"discovery found only {len(discovered)} exemption surfaces, which is far "
        "below the tree's known count -- a silently narrowed scan would make this "
        "whole module vacuous. Check exemption_discovery.PYTHON_PATHSPECS and "
        "TEXT_SOURCES."
    )

    registry = _registry()
    unregistered = sorted(set(discovered) - set(registry))
    assert not unregistered, (
        "these look like gate exemptions and are not in "
        "scripts/tests/gate_exemptions.json:\n  "
        + "\n  ".join(
            f"{key}  (line {discovered[key].line}, found via {discovered[key].via})"
            for key in unregistered
        )
        + "\n\nRegister each with what it turns off, and either the name of a predicate "
        "in gate_premises.py that the owning gate evaluates per member, or JUDGEMENT "
        "with the reason. If it is not an exemption at all, register it as kind "
        "'fixture', 'prune' or 'enforced-universe' -- the point is that the decision "
        "is recorded, not that everything found is a carve-out."
    )


def test_no_registered_exemption_has_vanished() -> None:
    """The other direction: a registry entry whose surface is gone.

    A stale entry is not harmless. It reads as a live, considered decision about a
    constant nobody can find, and it is the reason the next person believes the
    registry describes the tree.
    """
    discovered = exemption_discovery.discover_all()
    vanished = sorted(set(_registry()) - set(discovered))
    assert not vanished, (
        "these registry entries name no exemption surface in the tree — renamed, "
        f"deleted, or no longer matching discovery: {vanished}. Delete the entry, or "
        "if the constant still exists under a new spelling, re-key it."
    )


@pytest.mark.parametrize("key", sorted(_registry()))
def test_entry_is_well_formed(key: str) -> None:
    """Schema, per entry — including that JUDGEMENT carries a reason."""
    entry = _registry()[key]

    assert entry.get("kind") in KINDS, (
        f"{key}: kind {entry.get('kind')!r} not in {KINDS}"
    )
    assert entry.get("turnsOff", "").strip(), (
        f"{key}: 'turnsOff' is empty. What stops being asserted for a member is the "
        "one thing a reviewer needs and the one thing the constant's name never says."
    )

    premise = entry.get("premise")
    assert isinstance(premise, list) and premise, (
        f"{key}: 'premise' must be a non-empty LIST. A scalar cannot hold the two "
        "predicates some of these gates compute per member, and the entries whose gates "
        "did compute two were forced to record JUDGEMENT and understate themselves."
    )
    known = set(gate_premises.PREDICATES) | {gate_premises.JUDGEMENT}
    unknown = sorted(set(premise) - known)
    assert not unknown, (
        f"{key}: premise names {unknown}, which are neither predicates in "
        f"gate_premises.PREDICATES ({sorted(gate_premises.PREDICATES)}) nor "
        f"{gate_premises.JUDGEMENT!r}"
    )
    if gate_premises.JUDGEMENT in premise:
        assert entry.get("reason", "").strip(), (
            f"{key}: premise includes JUDGEMENT, so a reason is required. JUDGEMENT "
            "records that there is nothing to compute; it does not record that nobody "
            "looked."
        )

    assert entry.get("memberKind", "").strip(), (
        f"{key}: 'memberKind' is required — what does one member NAME? A path, a "
        "CloudFormation logical id, a model id, a rule id? It decides which predicates "
        "can possibly apply, so it cannot be left to inference."
    )

    ratchet = entry.get("ratchet")
    assert ratchet in RATCHETS, f"{key}: ratchet {ratchet!r} not in {RATCHETS}"
    if ratchet == "none":
        assert entry.get("ratchetGap", "").strip(), (
            f"{key}: ratchet is 'none', so 'ratchetGap' must say what is consequently "
            "unprotected. Declaring a gap is allowed; leaving it unstated is the habit "
            "this registry exists to break."
        )


@pytest.mark.parametrize("key", sorted(_registry()))
def test_a_computable_premise_is_actually_computed(key: str) -> None:
    """A named predicate must be called by the gate that claims it.

    Otherwise ``premise`` is decoration: the registry would assert that a fact is
    checked while nothing checks it, which is a more convincing version of the defect
    rather than a fix for it.

    This half is textual — it looks for the predicate's name and the module in the
    gate's source — because the gate is the caller and importing it to inspect its
    behaviour would mean re-implementing each gate's semantics here. The behavioural
    half lives in the gate, where the per-member parametrisation is.
    """
    entry = _registry()[key]
    named = [p for p in entry["premise"] if p != gate_premises.JUDGEMENT]
    if not named:
        return
    if entry.get("premiseWiring") == "pending":
        assert entry.get("reason", "").strip(), (
            f"{key}: premiseWiring is 'pending', so a reason must say why the predicate "
            "is named but not yet evaluated."
        )
        return

    gate_path = REPO_ROOT / key.split("::", 1)[0]
    called = _predicates_called_in(gate_path)
    missing = sorted(set(named) - called)
    assert not missing, (
        f"{key} names premise(s) {missing}, but {gate_path.relative_to(REPO_ROOT)} "
        f"contains no CALL to them (calls found: {sorted(called)}). A premise the owning "
        "gate never evaluates is the original defect with a registry entry on top of it "
        "— either wire it up per member, record JUDGEMENT with the reason, or mark "
        "premiseWiring 'pending' and say why."
    )


def test_pending_premise_wiring_does_not_grow() -> None:
    """'Computable but unchecked' is a state, and it may shrink but not grow.

    It is kept distinct from ``JUDGEMENT`` on purpose. Collapsing the two would let a
    premise that a predicate here already computes be recorded as though there were
    nothing to compute — which is precisely how a checkable premise stops being
    checked, and is the fault this whole registry is a response to.
    """
    pending = sorted(
        key
        for key, entry in _registry().items()
        if entry.get("premiseWiring") == "pending"
    )
    assert len(pending) <= MAX_PENDING_WIRING, (
        f"{len(pending)} entries name a computable premise their gate does not "
        f"evaluate, up from {MAX_PENDING_WIRING}: {pending}. Wire the predicate in per "
        "member — that is the whole point of it being computable."
    )


def test_cross_references_resolve_and_are_mutual() -> None:
    """Auditing one half of a paired exemption must force looking at the other.

    Two exemptions in this tree point at each other in prose — a Makefile variable and
    a Python path fragment, one comment saying it "mirrors" the other. Two independent
    surveys audited one half, quoted the comment naming the second, and never followed
    it. A one-directional pointer is how that happens, so a reference here must be
    mutual.
    """
    registry = _registry()
    problems = []
    for key, entry in registry.items():
        for other in entry.get("crossReferences", []):
            if other not in registry:
                problems.append(f"{key} -> {other} (no such entry)")
            elif key not in registry[other].get("crossReferences", []):
                problems.append(f"{key} -> {other} is not mutual")
    assert not problems, (
        "cross-references must resolve and point both ways, so that auditing either "
        f"half reaches the other: {problems}"
    )


def test_the_unratcheted_count_does_not_grow() -> None:
    """A ratchet on the escape hatch: gaps may be closed, not added.

    ``ratchetGap`` is deliberately permitted — a partial mechanism honestly scoped is
    worth more than one that over-reaches — but permitting it without bounding it would
    make "declare the gap" the cheapest possible response to a new exemption, and the
    registry would become a longer way of writing the same unchecked list.
    """
    unratcheted = sorted(
        key for key, entry in _registry().items() if entry["ratchet"] == "none"
    )
    assert len(unratcheted) <= MAX_UNRATCHETED, (
        f"{len(unratcheted)} exemptions now have no ratchet, up from "
        f"{MAX_UNRATCHETED}. Give the new one a ratchet — non-vacuity is usually two "
        "lines and catches a dead entry — or, if it genuinely cannot have one, lower "
        f"the bar deliberately and say why here. Current: {unratcheted}"
    )


def test_discovery_works_from_a_checkout_under_a_pruned_directory() -> None:
    """Behavioural: discovery must not depend on where the checkout sits.

    An agent worktree is a real checkout underneath ``.claude/``, and a gate that
    matched its prune set against absolute paths found every path inside one to contain
    the pruned name — so discovery returned nothing, and the gate either failed on
    every task or passed vacuously. That happened repeatedly, and it is the most
    repeated defect of the batch this module comes from.

    So drive the REAL collector against a one-file checkout created under exactly that
    path. A test that can only fail on a maintainer's machine is the defect it is
    trying to prevent.
    """
    with tempfile.TemporaryDirectory() as tmp:
        checkout = Path(tmp) / ".claude" / "worktrees" / "agent-probe"
        (checkout / "scripts").mkdir(parents=True)
        (checkout / "scripts" / "probe.py").write_text(
            "# Paths this gate deliberately excludes, each with the reason.\n"
            "PROBE_EXEMPT = {'a': 'because'}\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "init", "-q"], cwd=checkout, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "add", "-A"], cwd=checkout, check=True, capture_output=True
        )

        found = exemption_discovery.discover_all(root=checkout)
        assert "scripts/probe.py::PROBE_EXEMPT" in found, (
            "discovery found nothing in a checkout under .claude/worktrees/, so from "
            f"inside an agent worktree this gate sees no exemptions at all: {sorted(found)}"
        )


def test_discovery_sees_a_file_that_is_not_committed_yet() -> None:
    """The verdict must not change at ``git add`` time.

    This module's own constants were uncommitted when it was first run, so the scan did
    not see them; the moment they were committed it discovered five of its own, and the
    gate passed locally while failing in CI. A gate whose answer depends on whether you
    have committed yet reports a clean tree to the person writing the exemption and an
    unclean one to everybody else, which is worse than not having it.

    This drives the real collector rather than the tracked-file helper underneath it.
    The first version of this case called the helper directly and passed even with
    ``include_untracked`` removed from the collector — a test of the wrong layer, which
    is the same mistake as a gate whose reach is narrower than the class it describes.

    Gitignored files must still be excluded: that is what the git-based discovery is
    for, and it is asserted here alongside.
    """
    with tempfile.TemporaryDirectory() as tmp:
        checkout = Path(tmp)
        (checkout / "scripts").mkdir()
        (checkout / "scripts" / "uncommitted.py").write_text(
            "# Deliberately not covered, for a reason.\nNEW_EXEMPT = {'a'}\n",
            encoding="utf-8",
        )
        (checkout / "scripts" / "ignored.py").write_text(
            "# Deliberately not covered, for a reason.\nIGNORED_EXEMPT = {'b'}\n",
            encoding="utf-8",
        )
        (checkout / ".gitignore").write_text("scripts/ignored.py\n", encoding="utf-8")
        subprocess.run(
            ["git", "init", "-q"], cwd=checkout, check=True, capture_output=True
        )

        found = exemption_discovery.discover_all(root=checkout)
        assert "scripts/uncommitted.py::NEW_EXEMPT" in found, (
            "a file that exists but is not committed is invisible to discovery, so a "
            f"newly written gate escapes this registry until it is staged: {sorted(found)}"
        )
        assert "scripts/ignored.py::IGNORED_EXEMPT" not in found, (
            "a gitignored file is visible to discovery, which is how a gate comes to "
            f"report findings against build output: {sorted(found)}"
        )


#: For each predicate, wording in a reason that means its domain is in play. A reason
#: that talks about nested stacks while recording JUDGEMENT has to say why the
#: nested-stack predicate does not settle it.
#:
#: This is a lint on the REGISTRY, not a claim about the world -- which is what makes
#: it safe to use here. It cannot be wrong about whether a template is a nested stack,
#: because it never asks; it only refuses to let a reason invoke a subject a predicate
#: already covers without engaging with it. The repo's own rule is that a weak proxy
#: must not be called a check, and this is not standing in for a premise: it is the
#: thing that stops JUDGEMENT being used INSTEAD of a premise.
#:
#: **Two properties keep this from being decorative, and they are different properties.**
#: Eleven of the sixteen phrases it started with matched nothing in the registry, and
#: three of the five predicates it knew about had no live phrase at all -- so for those
#: three the check could not fire, and one predicate (``vcs_ignored_build_output``) was
#: not listed here at all, which is the gap that actually costs coverage.
#:
#: 1. **Coverage of the predicate set.** Every predicate in ``gate_premises.PREDICATES``
#:    must appear as a key here with at least one phrase, so a predicate added without
#:    wording fails instead of being quietly unreachable. That is the ratchet this needed;
#:    per-phrase staleness is not, for the reason below.
#: 2. **Every phrase demonstrably fires.** A phrase matching nothing *today* is not dead
#:    -- this vocabulary exists to recognise wording in entries **not yet written**, and
#:    requiring a live match would force it to describe only the entries that already
#:    exist, making deletion the correct response to a forward-looking phrase. What is
#:    checkable is whether a phrase can fire at all, so
#:    :func:`test_every_domain_phrase_can_actually_fire` runs a synthetic entry through
#:    the same matcher per phrase. Matching is substring against the reason LOWERCASED,
#:    so a phrase carrying an uppercase letter is inert; that is the way one of these
#:    dies, and it is the way this catches it.
#:
#: Contrast :data:`RATCHET_EVIDENCE_MARKERS` below, where a dead entry IS deleted. The
#: direction decides the rule: a phrase here widens what *demands* engagement, so an
#: unused one costs nothing; a marker there widens what *satisfies* a claim, so an unused
#: one is a loophole waiting to be reached for.
PREDICATE_DOMAIN_WORDING = {
    "not_a_nested_stack_of_parent": (
        "nested stack",
        "nested stacks",
        "parameters reach",
        "child stack",
        "independently deployed",
        "deployed independently",
        "parent template",
        "parent stack",
    ),
    "built_separately_from_main_stack": (
        "built separately",
        "built and versioned separately",
        "versioned separately",
        "same publish run",
        "publish run",
        "own build",
        "separate build",
        "release train",
        "own release",
        "publisher builds",
    ),
    "installer_manifest_pins_parameter": (
        "feature.yaml",
        "defaultparameters",
        "installer manifest",
        "manifest pins",
        "feature manifest",
    ),
    "file_absent_or_untracked": (
        "does not exist",
        "no longer exists",
        "only after a build",
        "untracked",
        "not tracked",
        "git does not track",
        "is absent",
    ),
    "collects_zero_tests": (
        "collects zero",
        "collects no",
        "zero pytest tests",
        "no tests",
        "not a test suite",
        "collects nothing",
    ),
    "vcs_ignored_build_output": (
        "ignore rule",
        "gitignored build",
        "build output at any depth",
        "only exists after a build",
    ),
    "vcs_ignored_generated_filename": (
        "generated artifact",
        "generated filename",
        "written into the tree while",
    ),
    "not_reachable_by_a_package_walk": (
        "not a package",
        "no __init__.py",
        "package walk",
        "not an importable package",
        "nothing imports it",
        "cannot be discovered",
    ),
}


def _implicated_predicates(entry: dict) -> list[str]:
    """Predicates whose subject ``entry``'s prose invokes without engaging with them.

    Factored out of the assertion below so the same matcher can be driven by a synthetic
    entry per phrase. A phrase-liveness test that re-implemented ``phrase in text`` would
    be testing itself, and would miss the two things that actually make one of these
    inert: the lowercasing of the text, and the set of fields read.
    """
    if gate_premises.JUDGEMENT not in entry["premise"]:
        return []
    text = f"{entry.get('reason', '')} {entry.get('turnsOff', '')}".lower()
    already = set(entry.get("predicateConsidered", {})) | set(entry["premise"])
    return sorted(
        predicate
        for predicate, wording in PREDICATE_DOMAIN_WORDING.items()
        if any(phrase in text for phrase in wording) and predicate not in already
    )


def test_every_predicate_has_domain_wording() -> None:
    """A predicate with no wording here is one this check can never demand.

    The gap measured on this vocabulary: ``vcs_ignored_build_output`` was a predicate
    two gates compute per member and it had no entry at all, so an entry recording
    JUDGEMENT over exactly its subject -- an ignore rule covering a path -- passed
    silently. Coverage of the predicate set is the property worth asserting, because
    it fails when a predicate is *added* without wording, which is how this arose.
    """
    missing = sorted(set(gate_premises.PREDICATES) - set(PREDICATE_DOMAIN_WORDING))
    assert not missing, (
        f"gate_premises.PREDICATES contains {missing}, which PREDICATE_DOMAIN_WORDING "
        "does not cover. Until it does, an entry can record JUDGEMENT over precisely "
        "that predicate's subject and nothing will ask why the predicate does not "
        "settle it. Add the wording a reason would use for it."
    )
    stray = sorted(set(PREDICATE_DOMAIN_WORDING) - set(gate_premises.PREDICATES))
    assert not stray, (
        f"PREDICATE_DOMAIN_WORDING has wording for {stray}, which are not predicates "
        "in gate_premises.PREDICATES. Wording for a predicate that does not exist "
        "demands engagement with nothing."
    )
    empty = sorted(p for p, w in PREDICATE_DOMAIN_WORDING.items() if not w)
    assert not empty, (
        f"PREDICATE_DOMAIN_WORDING lists {empty} with no phrases, which is the same as "
        "not listing them at all while reading as covered."
    )


@pytest.mark.parametrize(
    ("predicate", "phrase"),
    [
        (p, phrase)
        for p, wording in PREDICATE_DOMAIN_WORDING.items()
        for phrase in wording
    ],
)
def test_every_domain_phrase_can_actually_fire(predicate: str, phrase: str) -> None:
    """Each phrase, run through the real matcher on a synthetic entry.

    This is the half that makes the vocabulary more than a list. A phrase is allowed to
    match nothing in the registry today -- it is there for entries not yet written -- but
    it is not allowed to be incapable of matching, and the usual cause is invisible:
    the text is lowercased before the comparison, so a phrase with any uppercase letter
    in it can never fire. The phrase is written into the synthetic reason with its first
    letter capitalised, the way a person would open a sentence, which is what makes that
    case fail here.
    """
    sentence = f"{phrase[0].upper()}{phrase[1:]}, which is why this member is exempt."
    entry = {
        "premise": [gate_premises.JUDGEMENT],
        "reason": sentence,
        "turnsOff": "a gate, for one member",
    }
    implicated = _implicated_predicates(entry)
    assert predicate in implicated, (
        f"PREDICATE_DOMAIN_WORDING maps {predicate!r} to {phrase!r}, and a reason "
        f"reading {sentence!r} does not implicate it. Matching is substring against the "
        f"reason LOWERCASED, so a phrase carrying an uppercase letter can never fire. "
        f"Implicated instead: {implicated}"
    )


@pytest.mark.parametrize("key", sorted(_registry()))
def test_judgement_does_not_stand_in_for_an_available_predicate(key: str) -> None:
    """``JUDGEMENT`` must not be used where a predicate here already applies.

    This answers the obvious objection to the whole mechanism: if `JUDGEMENT` and
    `ratchetGap` are both permitted, what stops a future author reaching for them to
    avoid writing a predicate? Nothing, unless something asks. So this asks.

    If a reason's wording invokes the subject of an existing predicate -- nested
    stacks, the publisher's build, an installer manifest, test collection -- the entry
    must either name that predicate or list it in ``predicateConsidered`` with a
    sentence saying why it does not settle the question. Both are cheap; neither is
    automatic, and that is the point. The failure is not "your reason is wrong", it is
    "a predicate exists for this and you have not said why it does not apply".

    **This does not make JUDGEMENT safe in general, and it is not claimed to.** An
    author can still write a premise this vocabulary does not recognise, and no test
    can read a sentence. What it removes is the specific, cheap failure mode of
    restating in prose a fact the tree can compute -- which is exactly what all four
    original defects did.
    """
    implicated = _implicated_predicates(_registry()[key])
    assert not implicated, (
        f"{key} records JUDGEMENT, but its reason invokes the subject of "
        f"{implicated} -- predicate(s) that exist in gate_premises.py and are computed "
        "from this tree. Either name the predicate as the premise and evaluate it per "
        "member in the owning gate, or add it to 'predicateConsidered' with a sentence "
        "saying why it does not settle the question. A premise restated in prose that "
        "the tree can compute is the original defect, four times over."
    )


@pytest.mark.parametrize("key", sorted(_registry()))
def test_a_considered_predicate_is_named_and_explained(key: str) -> None:
    """``predicateConsidered`` must name real predicates and say why each was set aside."""
    entry = _registry()[key]
    for predicate, why in (entry.get("predicateConsidered") or {}).items():
        assert predicate in gate_premises.PREDICATES, (
            f"{key}: predicateConsidered names {predicate!r}, which is not in "
            f"gate_premises.PREDICATES ({sorted(gate_premises.PREDICATES)})"
        )
        assert why.strip(), (
            f"{key}: predicateConsidered[{predicate!r}] has no explanation. Setting a "
            "predicate aside silently is the same act as never looking for it."
        )


#: Evidence that a named ratchet is implemented, not merely labelled. Text a file that
#: implements that kind of ratchet necessarily contains.
#:
#: Same standing as PREDICATE_DOMAIN_WORDING: a lint on this registry, not a claim about
#: the world. It cannot tell a good staleness check from a bad one. What it stops is the
#: cheapest possible cheat -- writing "staleness" beside an exemption with no staleness
#: check anywhere, which kept the unratcheted count down and the suite green, making
#: mislabelling cheaper than declaring a gap and inverting the incentive MAX_UNRATCHETED
#: exists to create.
#:
#: ⚠️ **A marker that matches nothing is deleted here, and that is the opposite of the
#: rule for PREDICATE_DOMAIN_WORDING above.** The direction decides it. A phrase there
#: widens what *demands* engagement, so one matching nothing yet costs nothing and may be
#: forward-looking. A marker here widens what *satisfies* a claimed ratchet, so one
#: matching nothing in any evidence file cannot do anything except let a future entry
#: claim a ratchet on the strength of a word -- it is pre-approval, the same shape as a
#: vacuous exemption. Four were dead when this was measured ("no longer match",
#: "stale_allowlist", "still needed", "no more sites") and are gone;
#: :func:`test_every_ratchet_marker_is_live` keeps the list swept.
RATCHET_EVIDENCE_MARKERS = {
    "non-vacuity": (
        "hides nothing",
        "shields 0",
        "shields nothing",
        "vacuous",
        "matched nothing",
        "still_needed",
    ),
    "count-pinned": (
        "pinned",
        "expected count",
        "audited when",
        "expected number",
    ),
    "universe-closure": (
        "unaccounted",
        "categorised",
        "is_categorised",
        "neither",
        "unregistered",
        "unclassified",
        # "not in RUN_ROOTS" was here and is deleted. It appeared in exactly ONE place in
        # the tree -- this table -- so before _evidence_text excised the table from its own
        # file it read as live purely because an entry named this module as its evidence.
        # The phrase a run-roots closure actually uses is "unregistered", which is live.
        "missing wildcard",
        "inverse",
        "drift",
        "TREE_INDEPENDENCE",
        "MANIFEST_TOLERATED",
        "_ALL_KNOWN",
    ),
    "staleness": (
        "stale",
        "vanished",
        "no longer",
        "still exist",
        "still_needed",
        "dead",
    ),
    "prune-meta-test": ("scratch", ".claude", "PRUNE"),
}


#: The two ratchet kinds this module itself implements: non-vacuity (the dead-glob check
#: on the discovery apertures) and universe-closure (membership asserted in both
#: directions).
#:
#: ⚠️ **It exists because excising the marker table is not sufficient.** After the excision
#: this file still contains, in ordinary prose, at least one marker for all five labels --
#: "stale", "vanished" and "no longer" for staleness, "pinned" for count-pinned, "scratch"
#: and ".claude" for the prune meta-test -- so naming it as evidence still satisfied the
#: label check for a ratchet it does not implement. A marker vocabulary matched by
#: substring cannot distinguish prose from an implementation, so the restriction is made
#: per file instead, about the one file an entry can always reach for.
META_TEST_PERMITTED_RATCHETS = frozenset({"non-vacuity", "universe-closure"})


@functools.cache
def _evidence_text(rel: str) -> str:
    """A ratchetEvidence file's text, with the marker TABLE removed if it declares one.

    ``RATCHET_EVIDENCE_MARKERS`` is declared in this module, so the text of this module
    contained every marker for every label and naming it as evidence satisfied the label
    check unconditionally -- an evidence pointer that cannot fail, which is exactly the
    "ratchet nothing implements" this registry refuses. Excising the assignment's own
    source span is the narrow half of the fix: a marker in a failure message, a docstring
    or a check body still counts, which is where an implemented ratchet says what it does.
    """
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=rel)
    except SyntaxError:
        return text
    lines = text.splitlines(keepends=True)
    for node in tree.body:
        targets = (
            [node.target]
            if isinstance(node, ast.AnnAssign)
            else list(node.targets)
            if isinstance(node, ast.Assign)
            else []
        )
        if any(
            isinstance(t, ast.Name) and t.id == "RATCHET_EVIDENCE_MARKERS"
            for t in targets
        ):
            end = node.end_lineno or node.lineno
            del lines[node.lineno - 1 : end]
            break
    return "".join(lines)


def ratchet_evidence_candidates(ratchet: str, paths: list[str]) -> list[str]:
    """The named evidence files that may satisfy ``ratchet``, as a pure function.

    ⚠️ **Applied PER FILE, not to an entry whose only evidence is this module.** Written
    the second way first, it was bypassed by naming any second tracked file alongside: the
    marker search is ``any(...)`` across the named files on purpose -- several ratchets are
    split across a script and its test -- so the second file lifted the restriction while
    this file went on supplying the marker out of its own prose. Measured: a ``staleness``
    claim with evidence ``[this module, CHANGELOG.md]`` passed.

    Pure so the rule can be driven by synthetic input. A rule that needs a real run to
    test would mean rewriting the registry this suite reads, which is not a trade worth
    making for a list comprehension.
    """
    return [
        rel
        for rel in paths
        if rel != _THIS_MODULE or ratchet in META_TEST_PERMITTED_RATCHETS
    ]


@pytest.mark.parametrize("key", sorted(_registry()))
def test_a_named_ratchet_is_implemented_somewhere(key: str) -> None:
    """``ratchet`` must point at a file that implements it.

    The field was an unverified label. Registering a new exemption with
    ``"ratchet": "staleness"`` and no staleness test anywhere left the unratcheted count
    untouched and the suite green -- so the cheapest response to a new exemption was not
    "declare the gap" but "claim a ratchet you did not build", which is worse than the
    gap because it reads as protection.

    An equivalent check already existed for ``premise`` and not for ``ratchet``, the
    field carrying the majority of these entries.
    """
    entry = _registry()[key]
    ratchet = entry["ratchet"]
    if ratchet == "none":
        return

    evidence = entry.get("ratchetEvidence")
    assert evidence, (
        f"{key} claims ratchet {ratchet!r} but names no 'ratchetEvidence'. Point at the "
        "tracked file that implements it."
    )
    paths = [evidence] if isinstance(evidence, str) else evidence
    markers = RATCHET_EVIDENCE_MARKERS[ratchet]
    for rel in paths:
        assert gate_premises.is_tracked(rel), (
            f"{key}: ratchetEvidence names {rel!r}, which git does not track"
        )
    # At least ONE of the named files must show the implementation. Several ratchets
    # are split across a script and its test -- run_all_tests.py hard-errors on an
    # unclassified directory while its test file covers the registry's shape -- so
    # requiring every named file to carry a marker would force the author to drop the
    # honest other half from the list.
    candidates = ratchet_evidence_candidates(ratchet, paths)
    assert candidates, (
        f"{key} claims ratchet {ratchet!r} and names only this module as evidence, and "
        f"the registry meta-test implements {sorted(META_TEST_PERMITTED_RATCHETS)} and "
        "nothing else. The marker check cannot catch this: after the marker table is "
        "excised this file still carries at least one marker for every label in ordinary "
        f"prose, so the label would be satisfied by a word. Point at the file that "
        f"implements {ratchet!r}, or set ratchet to 'none' with a ratchetGap."
    )
    # Read through _evidence_text, which excises the marker TABLE from the file that
    # declares it, and over `candidates` rather than every named path.
    assert any(
        marker.lower() in _evidence_text(rel).lower()
        for rel in candidates
        for marker in markers
    ), (
        f"{key} claims ratchet {ratchet!r} implemented in {candidates}, but none of those "
        f"files contains any of {list(markers)} outside the marker table itself. Either "
        "the ratchet is not implemented there -- in which case say so and set ratchet to "
        "'none' with a ratchetGap -- or point at the file that does implement it."
    )


def test_every_ratchet_marker_is_live() -> None:
    """A marker no evidence file contains can only ever excuse a future claim.

    This is the non-vacuity ratchet on the ratchet-evidence vocabulary itself, and it is
    the right rule *here* for a reason that does not generalise to the wording list above:
    a marker widens what satisfies a claimed ratchet. One that matches nothing in any file
    any entry names cannot be doing its job today and cannot start; what it can do is let
    the next entry claim "staleness" because a file happens to contain the word. That is
    pre-approval of whatever next takes the label, which is exactly what the registry's
    non-vacuity rule exists to refuse.

    Scoped per ratchet kind, because a marker is only reachable through the files entries
    claiming *that* kind name. A marker live for `staleness` and dead for `non-vacuity` is
    dead where it is written.
    """
    registry = _registry()
    dead: list[str] = []
    for ratchet, markers in RATCHET_EVIDENCE_MARKERS.items():
        paths: set[str] = set()
        for entry in registry.values():
            if entry["ratchet"] != ratchet:
                continue
            evidence = entry.get("ratchetEvidence") or []
            paths.update([evidence] if isinstance(evidence, str) else evidence)
        texts = [
            _evidence_text(rel).lower()
            for rel in sorted(paths)
            if (REPO_ROOT / rel).exists()
        ]
        assert texts, (
            f"no entry claiming ratchet {ratchet!r} names a readable ratchetEvidence "
            "file, so every marker for it would read as dead and this check would "
            "delete a working vocabulary. Look at the registry, not at the list."
        )
        for marker in markers:
            if not any(marker.lower() in text for text in texts):
                dead.append(f"{ratchet}:{marker!r}")
    assert not dead, (
        f"these ratchet-evidence markers appear in none of the files entries claiming "
        f"that ratchet name: {dead}. Delete them. A marker here widens what SATISFIES a "
        "claimed ratchet, so one that matches nothing cannot help today and can only "
        "let a future entry claim a ratchet it did not build — the same pre-approval a "
        "vacuous exemption is. (This is deliberately the opposite rule to "
        "PREDICATE_DOMAIN_WORDING, where a phrase widens what DEMANDS engagement and "
        "matching nothing yet is fine.)"
    )


#: Predicates that can apply to any exemption whose members name repo paths.
_PATH_PREDICATES = frozenset(
    {
        "not_a_nested_stack_of_parent",
        "built_separately_from_main_stack",
        "file_absent_or_untracked",
    }
)


@pytest.mark.parametrize("key", sorted(_registry()))
def test_a_path_membered_exemption_engages_with_the_path_predicates(key: str) -> None:
    """An exemption over repo paths may not be pure ``JUDGEMENT``.

    This is the hole the wording check alone does not close, and it is worth being
    precise about why. ``PREDICATE_DOMAIN_WORDING`` catches a reason that *says* "nested
    stack"; it does not catch one that says "has its own build and release train, ships
    on an independent cadence, and the main stack does not deploy it" — the same claim in
    words the list does not contain. A reviewer demonstrated exactly that against an
    earlier version of this file, with both predicates returning False for the member.

    Prose cannot be made airtight, so the rule here is structural instead: if a member
    is a **path**, then whether the parent deploys it and whether one publish run builds
    it are computable facts about it, and the entry must engage with them — by naming a
    predicate (which the owning gate must then actually call), by ``premiseWiring:
    pending``, or by ``predicateConsidered`` with a sentence per predicate. What it may
    not do is say nothing.

    Exemptions whose members are logical ids, model ids, rule ids or make targets are
    exempt from this, because no predicate here applies to them — recorded in
    ``memberKind`` rather than guessed.
    """
    entry = _registry()[key]
    if entry["memberKind"] != "deployable path":
        return
    if entry.get("premiseWiring") == "pending":
        return

    engaged = set(entry["premise"]) | set(entry.get("predicateConsidered") or {})
    assert engaged & _PATH_PREDICATES, (
        f"{key} exempts repo PATHS and engages with none of {sorted(_PATH_PREDICATES)}. "
        "Whether the parent deploys a path, and whether one publish run builds it, are "
        "facts this tree computes — four exemption lists asserted one of them in prose "
        "and were wrong. Name the predicate and evaluate it per member in the owning "
        "gate, or list it in 'predicateConsidered' with a sentence saying why it does "
        "not settle the question."
    )


def test_no_discovery_pathspec_is_dead() -> None:
    """A glob in either discovery aperture that matches nothing contributes nothing.

    The aperture constants are registered rather than excluded, because a scanner that
    skips itself is the one hole nothing can see -- but until now nothing asked whether an
    entry on one of those lists could fire. A mistyped or outlived glob leaves the tuple
    looking like coverage and reading as zero files, which is the same quiet failure as a
    dead vocabulary fragment, and it is the one two of the aperture entries in the registry
    named as their declared gap. ``TEXT_SOURCES`` is covered as well as
    ``PYTHON_PATHSPECS`` because a bare ``Makefile`` there matched the repository root
    alone, which is the same defect one step further on: this is what would now catch a
    typo in the three widened globs.

    It is NOT the broader property ``PYTHON_PATHSPECS``'s remaining ``ratchetGap`` names:
    nothing here asserts the aperture covers every tree holding a gate.
    """
    # The TEXT_SOURCES half is scoped to the surfaces built from the name vocabulary --
    # derived by comparing each entry's pattern with `_text_pattern`, not listed -- which
    # is exactly the three globs widened alongside this. The other entries there name a
    # specific CONFIGURATION file, and one of those (`.pre-commit-config.yaml`) is simply
    # absent from this repository: a named config that is not present is an absence rather
    # than a typo, and failing on it would make this check report a condition nobody can
    # fix.
    vocabulary_patterns = {
        exemption_discovery._text_pattern("upper"),
        exemption_discovery._text_pattern("lower"),
    }
    specs = list(exemption_discovery.PYTHON_PATHSPECS) + [
        spec
        for spec, pattern in exemption_discovery.TEXT_SOURCES
        if pattern in vocabulary_patterns
    ]
    assert len(specs) > len(exemption_discovery.PYTHON_PATHSPECS), (
        "no TEXT_SOURCES entry is built from the name vocabulary any more, so the Make "
        "and shell globs are outside this check and a typo in one would be silent"
    )
    dead = [
        spec
        for spec in specs
        if not gate_premises.tracked_files(spec, include_untracked=True)
    ]
    assert not dead, (
        f"these discovery pathspecs match no file at all: {dead}. A dead glob makes the "
        "aperture look wider than it is; either fix the spelling or delete it, because a "
        "tree or a file shape that has moved is one discovery has silently stopped "
        "covering."
    )


def test_the_marker_table_does_not_satisfy_its_own_label_check() -> None:
    """``_evidence_text`` must remove the markers from the file that declares them.

    Without it, naming this module as an entry's ``ratchetEvidence`` satisfied the label
    check for ANY label, because every marker for every label is written out here.

    The set is DERIVED rather than listed: markers present in the raw text and absent
    after excision are exactly the ones the hole was shielding. Asserted non-empty first,
    because an empty derived set would make the rest of this case vacuous rather than
    failing.
    """
    raw = (REPO_ROOT / _THIS_MODULE).read_text(encoding="utf-8").lower()
    excised = _evidence_text(_THIS_MODULE).lower()
    table_only = sorted(
        {
            marker
            for markers in RATCHET_EVIDENCE_MARKERS.values()
            for marker in markers
            if marker.lower() in raw and marker.lower() not in excised
        }
    )
    assert table_only, (
        "no marker in RATCHET_EVIDENCE_MARKERS is present in this file's raw text and "
        "absent after excision, so either the excision has stopped working or every "
        "marker is now used in prose here -- in both cases this case proves nothing."
    )
    for marker in table_only:
        assert marker.lower() not in excised, (
            f"{marker!r} survives excision, so an entry naming {_THIS_MODULE} as "
            "ratchetEvidence is satisfied by the table rather than by an implementation."
        )
    # Only the labels this module IMPLEMENTS need to survive excision. Asserting it for
    # all five would pin the hole open: the reason naming this file satisfied a label it
    # does not implement is that the prose here carries a marker for every one of them.
    for ratchet in sorted(META_TEST_PERMITTED_RATCHETS):
        assert any(m.lower() in excised for m in RATCHET_EVIDENCE_MARKERS[ratchet]), (
            f"after excision this file contains no marker for {ratchet!r}, which it does "
            "implement, so an entry claiming it and naming this file would fail. The "
            "excision has removed more than the table."
        )


@pytest.mark.parametrize("ratchet", sorted(set(RATCHETS) - {"none"}))
def test_this_module_may_only_be_evidence_for_the_ratchets_it_implements(
    ratchet: str,
) -> None:
    """Per label, through the real candidate rule.

    Excising the marker table was not sufficient, and this is the measurement that says
    so: after the excision this file still carries at least one marker for every one of
    the five labels in ordinary prose, so an entry naming it as evidence was satisfied for
    a ratchet it does not implement.
    """
    candidates = ratchet_evidence_candidates(ratchet, [_THIS_MODULE])
    satisfied_by_a_marker = any(
        marker.lower() in _evidence_text(_THIS_MODULE).lower()
        for marker in RATCHET_EVIDENCE_MARKERS[ratchet]
    )
    assert satisfied_by_a_marker, (
        f"{ratchet!r} has no marker in this file after excision, so this case is not "
        "measuring the restriction -- the marker check alone would already refuse it."
    )
    if ratchet in META_TEST_PERMITTED_RATCHETS:
        assert candidates == [_THIS_MODULE], (
            f"{ratchet!r} is a ratchet this module implements and it is not a candidate, "
            "so every entry claiming it here would fail"
        )
        return
    assert not candidates, (
        f"{ratchet!r} is satisfied by a marker in this file AND permitted for it, so an "
        "entry can claim a ratchet this module does not implement."
    )


def test_a_second_evidence_file_does_not_lift_the_meta_test_restriction() -> None:
    """The restriction is per file, because per entry it was one edit away from nothing.

    The marker search is ``any(...)`` across the named files deliberately -- several
    ratchets are split between a script and its test, and requiring every named file to
    carry a marker would force the honest other half out of the list. Written as "an entry
    whose ONLY evidence is this module", the restriction was therefore bypassed by naming
    any second tracked file: the second file lifted it and this one went on supplying the
    marker out of its own prose. Measured: a ``staleness`` claim with evidence ``[this
    module, CHANGELOG.md]`` passed.
    """
    second = "CHANGELOG.md"
    assert gate_premises.is_tracked(second), f"{second} is not tracked"
    for ratchet in sorted(set(RATCHETS) - {"none"}):
        candidates = ratchet_evidence_candidates(ratchet, [_THIS_MODULE, second])
        if ratchet in META_TEST_PERMITTED_RATCHETS:
            assert _THIS_MODULE in candidates, (
                f"{ratchet!r} is a ratchet this module implements and it is not a "
                "candidate, so every entry claiming it here would fail"
            )
            continue
        assert candidates == [second], (
            f"this module survives as a candidate for {ratchet!r}, which it does not "
            "implement, so an entry can claim that ratchet by naming any second file and "
            f"letting the marker come out of this file's prose: {candidates}"
        )
