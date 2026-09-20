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

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

import exemption_discovery
import gate_premises

pytestmark = pytest.mark.unit

REPO_ROOT = gate_premises.REPO_ROOT
REGISTRY_PATH = Path(__file__).resolve().parent / "gate_exemptions.json"

KINDS = {"exemption", "enforced-universe", "prune", "fixture", "capability"}
RATCHETS = {
    "non-vacuity",
    "count-pinned",
    "universe-closure",
    "staleness",
    "prune-meta-test",
    "none",
}

#: Entries with no ratchet at all, today. A ratchet that may SHRINK but never grow,
#: the same shape ``PASS_ROLE_WILDCARD_ALLOWED`` uses. Pinning it is what stops the
#: honest "declare the gap" escape hatch from becoming the default: declaring a gap is
#: allowed, and quietly adding a 30th is not.
MAX_UNRATCHETED = 35

#: Entries whose premise is computable but whose gate does not yet call the predicate.
#: Same ratchet direction, same reason: this state must not become a comfortable place
#: to leave things.
MAX_PENDING_WIRING = 1


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
        f"scripts/tests/gate_exemptions.json:\n  "
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

    assert entry.get("kind") in KINDS, f"{key}: kind {entry.get('kind')!r} not in {KINDS}"
    assert entry.get("turnsOff", "").strip(), (
        f"{key}: 'turnsOff' is empty. What stops being asserted for a member is the "
        "one thing a reviewer needs and the one thing the constant's name never says."
    )

    premise = entry.get("premise")
    known = set(gate_premises.PREDICATES) | {gate_premises.JUDGEMENT}
    assert premise in known, (
        f"{key}: premise {premise!r} is neither a predicate in gate_premises.PREDICATES "
        f"({sorted(gate_premises.PREDICATES)}) nor {gate_premises.JUDGEMENT!r}"
    )
    if premise == gate_premises.JUDGEMENT:
        assert entry.get("reason", "").strip(), (
            f"{key}: premise is JUDGEMENT, so a reason is required. JUDGEMENT records "
            "that there is nothing to compute; it does not record that nobody looked."
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
    premise = entry["premise"]
    if premise == gate_premises.JUDGEMENT:
        return
    if entry.get("premiseWiring") == "pending":
        assert entry.get("reason", "").strip(), (
            f"{key}: premiseWiring is 'pending', so a reason must say why the predicate "
            "is named but not yet evaluated."
        )
        return

    gate_path = REPO_ROOT / key.split("::", 1)[0]
    source = gate_path.read_text(encoding="utf-8")
    assert premise in source and "gate_premises" in source, (
        f"{key} names premise {premise!r}, but {gate_path.relative_to(REPO_ROOT)} does "
        f"not call gate_premises.{premise}. A premise the owning gate never evaluates "
        "is the original defect with a registry entry on top of it — either wire it up "
        "per member, record JUDGEMENT with the reason, or mark premiseWiring 'pending' "
        "and say why."
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

    So drive the real discovery against a one-file checkout created under exactly that
    path. A test that can only fail on a maintainer's machine is the defect it is
    trying to prevent.
    """
    with tempfile.TemporaryDirectory() as tmp:
        checkout = Path(tmp) / ".claude" / "worktrees" / "agent-probe"
        (checkout / "scripts").mkdir(parents=True)
        probe = checkout / "scripts" / "probe.py"
        probe.write_text(
            "# Paths this gate deliberately excludes, each with the reason.\n"
            "PROBE_EXEMPT = {'a': 'because'}\n",
            encoding="utf-8",
        )
        for args in (
            ["init", "-q"],
            ["-c", "user.email=t@example.invalid", "-c", "user.name=t", "add", "-A"],
        ):
            subprocess.run(["git", *args], cwd=checkout, check=True, capture_output=True)

        found = gate_premises.tracked_files("scripts/*.py", root=checkout)
        assert "scripts/probe.py" in found, (
            "discovery found no files in a checkout under .claude/worktrees/, so from "
            f"inside an agent worktree this gate sees nothing. Found: {found}"
        )
