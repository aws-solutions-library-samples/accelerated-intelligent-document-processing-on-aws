# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""No pattern in the exemption-discovery vocabularies may be silently dead.

THE DEFECT THIS EXISTS FOR. ``exemption_discovery`` decides which carve-outs in this
repository have to be registered, and it decides it with two wording lists: a set of
fragments matched against a constant's NAME, and a set of phrases matched against the
comment above it. Both are quiet when they miss. Both have missed:

* ``EXCLUD`` does not match ``EXCLUSION``, so ``TYPECHECK_SCOPE_EXCLUSIONS`` -- a live
  carve-out from the typecheck coverage gate -- sat unregistered while every meta-test
  passed. One letter of aperture.
* ``NOT_A_PRESET``, ``NOT_AN_IDP_CONFIG`` and ``READ_ELSEWHERE`` were matched by
  nothing, by name or by prose, and became discoverable only when they were renamed.

The remedy is not "make the lists longer" on its own, because a longer list has the
same property: nothing anywhere asks whether an entry on it can fire. So the meta-
property this module adds is that **a dead pattern is a failure**, in the same way the
registry already fails a vacuous exemption.

WHAT "DEAD" MEANS HERE, AND WHY IT IS NOT "MATCHES SOMETHING IN THIS TREE". A fragment
in :data:`~exemption_discovery.NAME_VOCABULARY` widens *discovery*: adding one can only
make more carve-outs require registration, and deleting one can only make fewer. Its job
is therefore to recognise wording in constants **not yet written**, and requiring it to
match something today would force the vocabulary to describe only what already exists --
which is backwards, and would make the correct response to a forward-looking fragment be
to delete it. Ten of the fragments match nothing in this tree right now and all ten are
doing their job.

What is checkable, and is what actually went wrong, is whether a pattern *works*. Each
one is exercised against the REAL collector on a synthetic checkout, which catches every
way one of these has been or could be inert:

* a fragment whose case cannot match, since names are compared uppercased and comments
  lowercased;
* a fragment containing a regex metacharacter, which would corrupt the alternation
  :func:`~exemption_discovery._text_pattern` builds and take the Makefile and shell
  surfaces down with it -- silently, because a broken alternation still compiles and
  still matches *something*;
* a fragment eaten by the polarity guard in ``_matches_name``;
* a prose phrase that fires on nothing because the matcher lowercases the comment.

Driving the collector rather than re-implementing the match is the load-bearing choice.
A test that applied ``fragment in name`` itself would pass while the collector's own
container check, pathspec list, or prose ordering made the fragment unreachable -- the
same "test of the wrong layer" that let an earlier version of the uncommitted-file case
pass with ``include_untracked`` removed.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import exemption_discovery
import pytest
from exemption_discovery import Discovered

pytestmark = pytest.mark.unit

#: A constant name the NAME vocabulary must NOT match, used by the prose probes so that
#: a prose hit is attributable to the comment. Asserted rather than assumed, because a
#: future fragment could collide with it and turn every prose probe green for the wrong
#: reason.
_PROSE_PROBE_NAME = "THING_THE_GATE_READS"


def _discover(files: dict[str, str]) -> dict[str, Discovered]:
    """Run the real collector over a synthetic git checkout holding ``files``.

    ``git init`` and no commit: discovery includes untracked-but-not-ignored files on
    purpose, so nothing here needs staging, and relying on that is also a standing
    check that it stays true.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
        return exemption_discovery.discover_all(root=root)


def _probe_name(fragment: str) -> str:
    """A constant name built around ``fragment``, in this repo's naming style."""
    return f"PROBE_{fragment.strip('_')}_SET"


def _assert_attributable(fragment: str, name: str) -> None:
    """A hit on ``name`` must be attributable to ``fragment`` and to nothing else.

    Without this the probes are worth nothing, and the way they fail is quiet: a
    fragment that is a mis-cased or redundant restatement of another one -- ``"Exempt"``
    beside ``"EXEMPT"`` -- builds a probe name the *other* fragment matches, so the probe
    passes while the new fragment can never fire on any name in the tree. Measured: that
    exact addition left every case here green.
    """
    upper = name.upper()
    overlapping = [
        other
        for other in exemption_discovery.NAME_VOCABULARY
        if other != fragment and other in upper
    ]
    assert not overlapping, (
        f"the probe name {name} built for {fragment!r} is also matched by "
        f"{overlapping}, so a hit on it says nothing about {fragment!r}. Either the "
        f"fragment is a redundant restatement of one of those -- a mis-cased duplicate "
        f"is the usual cause, and is dead on arrival because names are compared "
        f"uppercased -- or it needs a probe name the others cannot reach."
    )


def _sentence(phrase: str) -> str:
    """``phrase`` as the opening of a comment, cased the way a person would write it.

    The first letter is upper-cased and the rest left alone, which is what makes this
    probe able to fail: a phrase stored with any uppercase letter cannot match a comment
    the collector has lowercased, and capitalising the whole phrase or lower-casing it
    would hide exactly that.
    """
    return f"{phrase[0].upper()}{phrase[1:]}, which is why they are here."


@pytest.mark.parametrize("fragment", exemption_discovery.NAME_VOCABULARY)
def test_a_name_fragment_finds_a_python_constant(fragment: str) -> None:
    """Every NAME fragment discovers a module-level Python constant carrying it."""
    name = _probe_name(fragment)
    _assert_attributable(fragment, name)
    found = _discover({"scripts/probe.py": f"{name} = {{'a'}}\n"})
    key = f"scripts/probe.py::{name}"
    assert key in found, (
        f"NAME_VOCABULARY carries {fragment!r}, and a constant named {name} is not "
        f"discovered at all. The fragment cannot fire on any name. Found: "
        f"{sorted(found)}"
    )
    assert found[key].via == "name", (
        f"{name} was discovered via {found[key].via!r} rather than by name, so "
        f"{fragment!r} is not what found it and its own reach is untested."
    )


@pytest.mark.parametrize("fragment", exemption_discovery.NAME_VOCABULARY)
def test_a_name_fragment_finds_a_make_variable_and_a_shell_variable(
    fragment: str,
) -> None:
    """Every NAME fragment works on the two non-Python surfaces built from it.

    ``TEXT_SOURCES`` derives its patterns from this same vocabulary by joining the
    fragments into one regex alternation, which is what keeps the Makefile and shell
    surfaces from drifting from the Python one -- and is also why a fragment containing
    a regex metacharacter is dangerous rather than merely useless: the alternation still
    compiles, still matches other fragments, and quietly stops matching this one.
    ``ARN_PARTITION_EXEMPT`` lives in the ``Makefile``, so this surface is not
    hypothetical.
    """
    name = _probe_name(fragment)
    _assert_attributable(fragment, name)
    found = _discover(
        {
            "Makefile": f"{name} = a b\n",
            "scripts/probe.sh": f"#!/bin/sh\n{name.lower()}=1\n",
        }
    )
    for rel in ("Makefile", "scripts/probe.sh"):
        expected = name if rel == "Makefile" else name.lower()
        assert f"{rel}::{expected}" in found, (
            f"NAME_VOCABULARY carries {fragment!r}, and {expected} in {rel} is not "
            f"discovered. Check exemption_discovery._text_pattern -- a fragment with a "
            f"regex metacharacter in it breaks this surface without breaking the "
            f"Python one. Found: {sorted(found)}"
        )


@pytest.mark.parametrize("phrase", exemption_discovery.EXEMPTION_PROSE)
def test_a_prose_phrase_finds_a_constant_whose_name_says_nothing(phrase: str) -> None:
    """Every prose phrase discovers a constant the NAME vocabulary cannot see.

    This is the half that is supposed to make naming irrelevant, so the probe's constant
    is deliberately named after what it holds rather than after what the gate does with
    it -- the shape the three constants in issue #1163 had.
    """
    assert not exemption_discovery._matches_name(_PROSE_PROBE_NAME), (
        f"the prose probe's constant name {_PROSE_PROBE_NAME!r} is now matched by "
        "NAME_VOCABULARY, so these probes would pass by name and prove nothing about "
        "the prose route. Rename the probe."
    )
    source = f"# {_sentence(phrase)}\n{_PROSE_PROBE_NAME} = {{'a'}}\n"
    found = _discover({"scripts/probe.py": source})
    key = f"scripts/probe.py::{_PROSE_PROBE_NAME}"
    assert key in found, (
        f"EXEMPTION_PROSE carries {phrase!r}, and a comment opening with it does not "
        f"make the constant below it discoverable. Matching is substring against the "
        f"comment LOWERCASED, so a phrase with an uppercase letter can never fire. "
        f"Comment probed: {source.splitlines()[0]!r}"
    )
    assert found[key].via == "prose", (
        f"the probe constant was discovered via {found[key].via!r} rather than by "
        f"prose, so {phrase!r} is not what found it."
    )


def test_the_polarity_guards_still_reject_what_they_are_for() -> None:
    """``DISALLOWED`` is not an allowlist, and a swallowed-failure fixture is not one.

    The guard in ``_matches_name`` is the one place this module deliberately narrows,
    and a narrowing with no test is how a vocabulary comes to match nothing: deleting
    the guard would go unnoticed, and so would widening it until it ate ``ALLOW``.
    """
    found = _discover(
        {
            "scripts/probe.py": (
                "DISALLOWED_ACTIONS = {'a'}\n"
                "SWALLOWED_FAILURE_SHAPES = {'b'}\n"
                "REAL_ALLOWLIST = {'c'}\n"
            )
        }
    )
    assert "scripts/probe.py::REAL_ALLOWLIST" in found, (
        "the polarity guard now rejects a genuine allowlist, so every ALLOW-named "
        f"carve-out in the tree has stopped being discovered: {sorted(found)}"
    )
    for rejected in ("DISALLOWED_ACTIONS", "SWALLOWED_FAILURE_SHAPES"):
        assert f"scripts/probe.py::{rejected}" not in found, (
            f"{rejected} is discovered as an exemption. A denylist and a fixture named "
            "for a swallowed failure are the two polarity inversions this vocabulary "
            "gets wrong by substring, and registering them trains people to "
            "rubber-stamp the registry."
        )


def test_both_routes_find_something_in_this_repository() -> None:
    """Anti-vacuity for the collector as a whole, per route.

    The registry meta-test asserts a floor on the TOTAL, which a route that has stopped
    working entirely can pass on the strength of the others -- and the prose route is
    the one that carries a third of this tree's surfaces while being the one nothing
    else exercises.
    """
    discovered = exemption_discovery.discover_all()
    by_route: dict[str, int] = {}
    for item in discovered.values():
        by_route[item.via] = by_route.get(item.via, 0) + 1
    for route in ("name", "prose", "text", "json"):
        assert by_route.get(route, 0) > 0, (
            f"no exemption surface in this repository was found via {route!r}: "
            f"{by_route}. That route has stopped working; the total floor in "
            "test_gate_exemption_registry.py cannot see this."
        )
