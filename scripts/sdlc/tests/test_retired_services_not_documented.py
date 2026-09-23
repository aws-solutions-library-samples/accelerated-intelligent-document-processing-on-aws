# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Documentation must not present a retired service as part of the architecture.

AWS AppSync was removed and replaced by an API Gateway REST API with a dispatcher
Lambda, but roughly two dozen documents went on describing the GraphQL endpoint,
its subscriptions and its ``AppSyncVisibility`` parameter as things a deployment
still has (GitHub #929). Nothing caught it, because removing a service is a
template change and the prose describing it lives elsewhere.

``scripts/sdlc/check_retired_services.py`` is the gate; this module wraps it and
adds the assertions that keep the gate honest. Three of them matter more than the
pass/fail check itself:

* :func:`test_the_scanned_surface_is_not_trivial` guards the guard. A typo in
  ``scannedPaths`` that resolved to nothing would leave a gate that always
  reports success.
* :func:`test_every_allowlist_entry_still_matches_something` fails on a dead
  exemption. An exemption nobody needs is a standing licence to reintroduce the
  claim it was written to excuse.
* :func:`test_markers_do_not_exempt_a_real_stale_claim` pins the
  ``historicalMarkers`` list against over-broadening, using the actual pre-fix
  text from three files as the fixture. Those markers are what keep the allowlist
  from needing one entry per sentence of correct historical prose, but a marker
  like ``API Gateway`` or ``GraphQL`` would match the stale lines too and
  silently turn the gate off. This test is why widening a marker is a reviewable
  act rather than an invisible one.

The security threat-modeling corpus came under the gate in #995, which added four
marker forms for the ways that corpus states the service is gone. Widening a
blocking gate's vocabulary is the risky half of that change, so each new form has
its own near-miss fixture in :data:`STALE_LINES_NEAR_NEW_MARKERS`: a line that
contains the new marker's own keyword and still asserts the service is present.
:func:`test_the_threat_model_corpus_is_scanned` pins the tree into the scanned set
so the exclusion cannot quietly come back, and
:func:`test_every_exclusion_still_matches_something` closes the gap that let the
previous exclusion's "revisit once #960 lands" note outlive its trigger unnoticed.
"""

from __future__ import annotations

import copy
import json
import pathlib
import re

import pytest

# conftest.py puts scripts/sdlc on sys.path; `scripts` is not a package.
from check_retired_services import (  # noqa: E402
    find_violations,
    load_registry,
    main,
    marker_matchers,
    reads_as_historical,
    scanned_files,
    stale_allowlist_entries,
    stale_alternatives,
    stale_exclusions,
    top_level_alternatives,
    unexpected_resources,
)


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def registry() -> dict:
    return load_registry()


#: Real pre-fix lines, copied verbatim from the tree before #929 was fixed. Each
#: asserts AppSync is present; none contains any retirement, pastness or
#: vestigiality language. A marker that exempted one of these would be wrong.
STALE_LINES_AT_HEAD = [
    "- **WAF Integration**: Web Application Firewall protection for the "
    "AppSync GraphQL API.",
    "3. **API Layer**: AppSync GraphQL API connects the UI to backend services",
    "- AppSync GraphQL API for UI-backend communication",
    "        AppSync[(AppSync API<br/>feature-platform resolvers)]",
    "### AppSync (`idp_common.appsync`)",
    "Document state persistence through the AppSync GraphQL API.",
    "  - `APPSYNC_API_URL`: AppSync endpoint for streaming",
    "- **Real-Time Streaming**: Streams responses as they're generated via AppSync",
]


#: Near-misses for the marker forms #995 added. Each contains the new marker's own
#: keyword -- "removed", "gone", "former", "replaced" -- in a construction that
#: still asserts AppSync is present, so a marker written as a bare keyword search
#: rather than as an assertion of absence would exempt it. These are the fixtures
#: that make widening the vocabulary safe rather than merely convenient.
STALE_LINES_NEAR_NEW_MARKERS = [
    # "is/are ... removed" must assert that AppSync is removed, not that AppSync
    # removed something else.
    "- AppSync removed the need for a custom API layer, and still fronts every "
    "UI query today.",
    # "gone" about something else in the sentence.
    "- The ALB is gone, so the AppSync GraphQL API is now reached directly by the "
    "browser.",
    # "former" qualifying a different noun.
    "- The former ALB hostname is replaced by the AppSync GraphQL endpoint.",
    # "replaced" where AppSync is the replacement rather than the replaced.
    "- Polling is replaced by AppSync subscriptions for real-time status.",
    # The revision-history forms, inverted: an edit note that leaves AppSync in.
    "| 4.0 | 2026-09-18 | Removed the A2I flow; the AppSync GraphQL API is "
    "unchanged and still serves the UI. |",
]


@pytest.mark.unit
def test_the_scanned_surface_is_not_trivial(registry: dict) -> None:
    """Guard the guard: an empty or near-empty sweep proves nothing.

    ``scannedPaths`` is a list of globs, so a directory rename or a stray typo
    could quietly reduce it to a handful of files while the gate kept reporting
    success. Pin both the order of magnitude and a few files that must be in it.
    """
    files = scanned_files(registry, _repo_root())
    relative = {p.relative_to(_repo_root()).as_posix() for p in files}

    assert len(files) > 100, (
        f"only {len(files)} documentation files are scanned; scannedPaths in "
        f"scripts/sdlc/retired_services.json has probably stopped matching"
    )
    for required in (
        "CLAUDE.md",
        "docs/architecture.md",
        "docs/rbac.md",
        "docs/migration-appsync-to-rest.md",
        "lib/idp_common_pkg/idp_common/agents/README.md",
    ):
        assert required in relative, (required, len(relative))

    # The exclusions must actually exclude, or the gate would police release
    # records and the externally-referenced workshop material.
    assert not [rel for rel in relative if rel.startswith("workshop/")]
    assert "CHANGELOG.md" not in relative


@pytest.mark.unit
def test_no_documentation_presents_a_retired_service_as_current(
    registry: dict,
) -> None:
    """The gate itself: every mention is either fixed or triaged."""
    findings, _ = find_violations(registry, _repo_root())
    assert not findings, "\n".join(f.render() for f in findings)


@pytest.mark.unit
def test_every_allowlist_entry_still_matches_something(registry: dict) -> None:
    """A dead exemption re-permits the claim it was written to excuse."""
    _, use = find_violations(registry, _repo_root())
    stale = stale_allowlist_entries(registry, use)
    assert not stale, [(entry["path"], entry["linePattern"]) for entry in stale]


@pytest.mark.unit
def test_every_individual_alternative_still_shields_something(registry: dict) -> None:
    """Per ALTERNATIVE, not per entry — the granularity a dead fragment hides at.

    An entry counted as live as soon as any part of its compiled pattern matched,
    so a fragment pinned to a sentence that was later rewrapped stopped matching in
    silence. The entry stayed green on its siblings, the line it named quietly
    stopped being excused, and the failure surfaced as unexcused findings in a
    document nobody had edited deliberately — three sessions each had to establish
    it was not theirs (#1097).

    The remedy when this fails is to re-pin the fragment to the wording it was
    written for, or to delete it. Never edit the prose to suit the pattern.
    """
    _, use = find_violations(registry, _repo_root())
    stale = stale_alternatives(registry, use)
    assert not stale, "\n".join(fragment.render() for fragment in stale)


@pytest.mark.unit
def test_a_dead_fragment_is_reported_even_when_a_sibling_is_live(
    registry: dict, tmp_path: pathlib.Path
) -> None:
    """The #1097 reproduction, as a measurement rather than a description.

    Two alternatives, one matching the document and one pinned to text that is not
    in it. Per-entry staleness sees a live entry and says nothing; per-alternative
    staleness names the fragment that stopped working.
    """
    scoped = copy.deepcopy(registry)
    scoped["scannedPaths"] = ["docs/*.md"]
    scoped["excludedPaths"] = []
    scoped["allowlist"] = [
        {
            "path": "docs/note.md",
            "linePattern": "the AppSync era ended|a sentence that was rewrapped away",
            "bucket": "b",
            "justification": "fixture",
        }
    ]

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text(
        "In this architecture the AppSync era ended at v0.6.\n", encoding="utf-8"
    )

    findings, use = find_violations(scoped, tmp_path)
    assert not findings, [f.render() for f in findings]
    # The entry as a whole still looks alive, which is exactly the problem.
    assert not stale_allowlist_entries(scoped, use)

    dead = [f for f in stale_alternatives(scoped, use) if f.surface == "allowlist"]
    assert [f.alternative for f in dead] == ["a sentence that was rewrapped away"]
    assert "docs/note.md" in dead[0].render()


@pytest.mark.unit
def test_a_marker_alternative_is_accounted_for_per_alternative(
    registry: dict, tmp_path: pathlib.Path
) -> None:
    """Markers had no staleness check of any granularity before #1097.

    They are the heavier alternation surface — five patterns, tree-wide reach — and
    nothing recorded which of their fragments had done any work, so the registry
    entry covering this file claimed a staleness ratchet that reached only the
    allowlist.
    """
    scoped = copy.deepcopy(registry)
    scoped["scannedPaths"] = ["docs/*.md"]
    scoped["excludedPaths"] = []
    scoped["allowlist"] = []
    scoped["historicalMarkers"] = [
        {"pattern": "has since been removed|never written anywhere", "justification": "f"}
    ]

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text(
        "The AppSync GraphQL API has since been removed.\n", encoding="utf-8"
    )

    findings, use = find_violations(scoped, tmp_path)
    assert not findings, [f.render() for f in findings]
    assert use.markers == {(0, 0): 1}

    dead = stale_alternatives(scoped, use)
    assert [f.alternative for f in dead] == ["never written anywhere"]
    assert dead[0].surface == "historicalMarkers"
    assert "historicalMarkers[0]" in dead[0].render()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("a|b|c", ["a", "b", "c"]),
        # A pipe inside a group is one claim phrased three ways, not three claims.
        ("(has|had|have) been removed|gone", ["(has|had|have) been removed", "gone"]),
        ("((a|b)|c)|d", ["((a|b)|c)", "d"]),
        # An escaped pipe is a literal: the Mermaid edge label in this registry's
        # detector would otherwise split into three.
        (r"\|\s*GraphQL Subscription\s*\||x", [r"\|\s*GraphQL Subscription\s*\|", "x"]),
        # Inside a character class, | and ( are literal.
        ("[a|b]c|d", ["[a|b]c", "d"]),
        ("[]|]x|y", ["[]|]x", "y"]),
        ("", [""]),
    ],
)
def test_top_level_alternatives_splits_only_the_outermost_level(
    pattern: str, expected: list[str]
) -> None:
    assert top_level_alternatives(pattern) == expected


@pytest.mark.unit
def test_the_split_is_a_faithful_decomposition_of_every_real_pattern(
    registry: dict,
) -> None:
    """Rejoining reproduces the pattern, and the parts match what the whole matches.

    Both halves matter. If the split were lossy the accounting would be counting
    something other than the gate's own verdict, and a fragment could be reported
    dead because the splitter mangled it — which would teach the next reader to
    distrust the report and delete the pattern instead of re-pinning it.
    """
    corpus = [
        "The AWS AppSync GraphQL API has since been removed.",
        "3. **API Layer**: AppSync GraphQL API connects the UI to backend services",
        "`APIRESOLVERSTACK` was historically named `nested/appsync/`.",
        "| UI | GraphQL Subscription | AppSync |",
        "no current template creates an AppSync resource",
    ]
    patterns = [entry["linePattern"] for entry in registry["allowlist"]]
    patterns += [marker["pattern"] for marker in registry["historicalMarkers"]]
    patterns += [service["pattern"] for service in registry["retiredServices"]]

    for pattern in patterns:
        parts = top_level_alternatives(pattern)
        assert "|".join(parts) == pattern, pattern
        whole = re.compile(pattern, re.IGNORECASE)
        compiled = [re.compile(part, re.IGNORECASE) for part in parts]
        for line in corpus:
            assert bool(whole.search(line)) == any(
                part.search(line) for part in compiled
            ), (pattern, line)


@pytest.mark.unit
def test_the_detector_is_not_subject_to_alternative_staleness(registry: dict) -> None:
    """A detector that finds nothing is a clean tree, not a dead exemption.

    ``retiredServices[].pattern`` points the other way from the two exemption
    surfaces: its arms exist to catch a reintroduction, so requiring each to match
    something today would force the deletion of exactly the arms that would catch
    one. Four of its five alternatives match nothing in the scanned corpus, and
    that is the correct state.
    """
    _, use = find_violations(registry, _repo_root())
    surfaces = {fragment.surface for fragment in stale_alternatives(registry, use)}
    assert "retiredServices" not in surfaces
    assert surfaces <= {"allowlist", "historicalMarkers"}


@pytest.mark.unit
def test_no_template_declares_a_retired_resource_type(registry: dict) -> None:
    """If the service comes back, the registry -- not the prose -- is wrong."""
    assert not unexpected_resources(registry, _repo_root())


@pytest.mark.unit
def test_naming_a_retired_resource_type_in_a_comment_is_not_a_reintroduction(
    registry: dict, tmp_path: pathlib.Path
) -> None:
    """The reintroduction check must read declarations, not occurrences.

    It used to substring-search the whole template, so a comment explaining why
    the legacy ``appsync:*`` grant is retained counted as evidence that AppSync had
    come back -- and the only reason the repository was not already red was that
    the surviving comment happens to write the wildcard form ``AWS::AppSync::*``.
    A reader who tidied that comment into a concrete type would have failed the
    build with a message pointing at the wrong file.
    """
    (tmp_path / "t.yaml").write_text(
        "AWSTemplateFormatVersion: '2010-09-09'\n"
        "# Retained so an in-place upgrade can delete the\n"
        "# AWS::AppSync::GraphQLApi a pre-0.6.0 stack created.\n"
        "Resources:\n"
        "  Bucket:\n"
        "    Type: AWS::S3::Bucket\n",
        encoding="utf-8",
    )
    assert not unexpected_resources(registry, tmp_path)


@pytest.mark.unit
@pytest.mark.parametrize(
    "declaration",
    [
        pytest.param("    Type: AWS::AppSync::GraphQLApi\n", id="yaml"),
        pytest.param('      "Type": "AWS::AppSync::GraphQLApi",\n', id="json-block"),
    ],
)
def test_a_real_declaration_is_still_reported(
    registry: dict, tmp_path: pathlib.Path, declaration: str
) -> None:
    """The other direction: anchoring on ``Type:`` must not blind the check.

    Tightening a check is only safe if the thing it was built to catch is still
    caught, in every syntax the repository actually uses. Both YAML block form and
    the JSON spelling of the same declaration must be reported.
    """
    (tmp_path / "t.yaml").write_text(
        "AWSTemplateFormatVersion: '2010-09-09'\nResources:\n  Api:\n" + declaration,
        encoding="utf-8",
    )
    hits = unexpected_resources(registry, tmp_path)
    assert hits == ["t.yaml: declares AWS::AppSync::GraphQLApi (AWS AppSync)"], hits


@pytest.mark.unit
def test_a_reintroduced_resource_does_not_suppress_the_prose_scan(
    registry: dict, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One false positive here used to hide every documentation finding.

    ``main`` reported reintroduced resources and returned immediately, so while the
    comment-matching defect above was live, the entire prose scan was unreachable
    and the gate's real output was one misleading paragraph. Both scans now run and
    the exit code reflects either.
    """
    scoped = copy.deepcopy(registry)
    scoped["scannedPaths"] = ["docs/*.md"]
    scoped["excludedPaths"] = []
    scoped["allowlist"] = []

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "probe.md").write_text(
        "3. **API Layer**: AppSync GraphQL API connects the UI to backend services\n",
        encoding="utf-8",
    )
    (tmp_path / "t.yaml").write_text(
        "AWSTemplateFormatVersion: '2010-09-09'\n"
        "Resources:\n  Api:\n    Type: AWS::AppSync::GraphQLApi\n",
        encoding="utf-8",
    )
    registry_path = tmp_path / "retired_services.json"
    registry_path.write_text(json.dumps(scoped), encoding="utf-8")

    exit_code = main(["--registry", str(registry_path), "--root", str(tmp_path)])
    err = capsys.readouterr().err

    assert exit_code == 1
    assert "AWS::AppSync::GraphQLApi" in err, err
    assert "docs/probe.md:1" in err, err


@pytest.mark.unit
def test_retired_parameters_are_declared_by_no_template(registry: dict) -> None:
    """``AppSyncVisibility``/``UsePrivateAppSync`` were renamed, not kept.

    They became ``ApiGatewayVisibility`` and ``UsePrivateApi``. A document telling
    an operator to set the old name is not merely stale, it is unusable, so the
    registry records the old names and this asserts no template still offers them.
    """
    wanted = [
        name
        for service in registry["retiredServices"]
        for name in service.get("retiredParameters", [])
    ]
    assert wanted, "the registry should record the renamed parameters"

    offenders: list[str] = []
    root = _repo_root()
    for pattern in ("*.yaml", "*.yml"):
        for path in root.rglob(pattern):
            rel = path.relative_to(root).as_posix()
            if (
                rel.startswith((".aws-sam/", "workshop/"))
                or "/node_modules/" in rel
                # Nested SAM build output, e.g. feature-platform/*/.aws-sam/build/.
                or "/.aws-sam/" in rel
            ):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if "AWSTemplateFormatVersion" not in text:
                continue
            for name in wanted:
                if re.search(rf"^\s*{name}\s*:", text, re.MULTILINE):
                    offenders.append(f"{rel}: declares {name}")
    assert not offenders, sorted(offenders)


#: A real historical sentence, written as a bullet because that is the shape it
#: takes in the architecture lists where this mattered most. Judged on its own it
#: is correctly exempt; the point of the neighbour cases below is that it must not
#: lend that exemption to the line above or the line below it.
HISTORICAL_NEIGHBOUR = "- AWS AppSync has since been removed from the solution."


def _scoped_to_one_file(registry: dict, rel: str) -> dict:
    """A copy of the registry that scans exactly ``rel`` and allowlists nothing."""
    scoped = copy.deepcopy(registry)
    scoped["scannedPaths"] = [rel]
    scoped["excludedPaths"] = []
    scoped["allowlist"] = []
    return scoped


def _findings_for(scoped: dict, root: pathlib.Path, rel: str, body: str) -> list:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    findings, _ = find_violations(scoped, root)
    return findings


@pytest.mark.unit
@pytest.mark.parametrize("line", STALE_LINES_NEAR_NEW_MARKERS)
def test_new_marker_forms_do_not_exempt_a_near_miss(
    registry: dict, tmp_path: pathlib.Path, line: str
) -> None:
    """The four marker forms #995 added must assert absence, not contain a keyword.

    Each fixture contains one of the new markers' own keywords in a construction
    that still presents AppSync as current -- "AppSync removed the need for...",
    "The ALB is gone, so the AppSync GraphQL API...", "The former ALB hostname is
    replaced by the AppSync GraphQL endpoint", "Polling is replaced by AppSync
    subscriptions". A marker implemented as a keyword search would pass all four
    and the vocabulary widening would have quietly cost the gate its teeth.

    This is separate from :func:`test_markers_do_not_exempt_a_real_stale_claim`
    rather than folded into it because those fixtures are real pre-fix lines and
    these are constructed adversarially. Keeping them apart means a failure names
    which risk materialised: a marker that matches ordinary architecture prose, or
    a marker that matches its own keyword in the wrong grammatical role.
    """
    scoped = _scoped_to_one_file(registry, "docs/probe.md")
    findings = _findings_for(scoped, tmp_path, "docs/probe.md", line + "\n")

    assert [f.lineno for f in findings] == [1], (
        f"the near-miss line {line!r} was NOT reported, so a historicalMarkers "
        f"pattern is matching its keyword rather than an assertion of absence "
        f"(findings: {[f.render() for f in findings]}). A marker must be true only "
        f"of a sentence that says the service is gone -- check whether it needs to "
        f"be anchored to the service name, to a copula, or to punctuation."
    )


@pytest.mark.unit
def test_the_threat_model_corpus_is_scanned(registry: dict) -> None:
    """The living threat-model documents are inside the gate, not exempt from it.

    They were excluded while they were point-in-time review artifacts pinned to the
    release each assessed. #960 gave the corpus its own currency gate, which made it
    a living description of the current architecture, and a living document has no
    business being exempt from the gate that stops documentation presenting a removed
    service as current. Pinning the files here is what stops the exclusion returning
    as a quick fix the next time the gate reports one of them.

    The genuinely dated artifacts under the same tree stay excluded, and are checked
    in the other direction: editing them would falsify the record of what was
    reviewed at that version.
    """
    relative = {
        p.relative_to(_repo_root()).as_posix() for p in scanned_files(registry, _repo_root())
    }

    for required in (
        "security/threat-modeling/README.md",
        "security/threat-modeling/architecture/system-overview.md",
        "security/threat-modeling/architecture/data-flows.md",
        "security/threat-modeling/feature-threats/rbac-authentication.md",
        "security/threat-modeling/threat-analysis/stride-analysis.md",
    ):
        assert required in relative, (
            f"{required} is not scanned by the retired-service gate. It is part of "
            f"the living threat model, which has a currency gate of its own "
            f"(make check-threat-model-currency), so it must not be excluded here. "
            f"If the gate reported a line in it, fix the line or allowlist that "
            f"line -- do not re-exclude the tree."
        )

    # The dated snapshots stay out, for the opposite reason.
    assert not [rel for rel in relative if rel.startswith("security/test-results/")]
    assert not [rel for rel in relative if "security-review-v" in rel]


@pytest.mark.unit
def test_every_exclusion_still_matches_something(registry: dict) -> None:
    """A dead exclusion glob exempts nothing, then exempts whatever moves in.

    ``stale_allowlist_entries`` has always failed on an allowlist entry that matched
    nothing. Exclusions had no equivalent, and the omission had already cost
    something: the ``security/threat-modeling/**`` exclusion this change removes
    carried a justification ending "REVISIT ONCE PR #960 LANDS" for most of a
    release cycle. #960 landed and nothing asked, because an exclusion's effect is
    an absence of findings and so cannot fail by matching too little.

    This does not check that a *reason* is still true -- no test can, which is why
    the justification text is what a reviewer reads. It checks the mechanical half:
    a renamed or deleted directory leaves a glob that silently grants its exemption
    to whatever next occupies the path.
    """
    assert not stale_exclusions(registry, _repo_root()), (
        "excludedPaths glob(s) match nothing: "
        f"{[e['glob'] for e in stale_exclusions(registry, _repo_root())]}. Delete "
        "the entry, fix the glob if the path was renamed, or -- if the path only "
        "exists after a build and the exclusion is deliberately defensive -- set "
        '"mayBeAbsent": true on it and say so in the justification.'
    )


@pytest.mark.unit
def test_may_be_absent_is_not_a_blanket_escape(registry: dict) -> None:
    """``mayBeAbsent`` is for gitignored build output, and nothing else.

    The flag exists so three defensive exclusions (node_modules, .aws-sam, the
    vendored idp_common_pkg copies) do not fail on a clean checkout. It would be an
    easy way to silence the check above for a genuinely dead exclusion, so the set
    of entries carrying it is pinned: adding a fourth has to be a visible edit here
    with a reason, not a field appended to the registry.
    """
    flagged = {
        e["glob"] for e in registry["excludedPaths"] if e.get("mayBeAbsent")
    }
    assert flagged == {
        "**/node_modules/**",
        "**/.aws-sam/**",
        "feature-platform/**/idp_common_pkg/**",
    }, (
        f"the set of excludedPaths entries marked mayBeAbsent changed: {sorted(flagged)}. "
        "That flag exempts an exclusion from the staleness check, so it belongs only "
        "on paths that are gitignored build output or vendored copies. If a new one "
        "genuinely qualifies, add it here with the reason it can be absent."
    )


@pytest.mark.unit
@pytest.mark.parametrize("line", STALE_LINES_AT_HEAD)
def test_markers_do_not_exempt_a_real_stale_claim(
    registry: dict, tmp_path: pathlib.Path, line: str
) -> None:
    """Pin ``historicalMarkers`` against over-broadening, in all three directions.

    The markers exist so correct historical prose does not need an allowlist entry
    per sentence. The risk they carry is the mirror image: a marker broad enough to
    match ordinary architecture prose would exempt genuinely stale lines and gut
    the gate without changing a single line of Python.

    The earlier version of this test judged each fixture as a one-line document,
    which could not see the defect that actually existed. The gate used to exempt a
    line when a marker appeared anywhere within one line of it, symmetrically and
    unconditionally, so every genuine historical sentence in the corpus silently
    exempted its two neighbours -- and consecutive bullets, one historical and one
    stale, is exactly how these documents are written. A one-element fixture list
    can never exercise a neighbour rule. Each fixture is therefore checked three
    ways: alone, with a real historical bullet immediately above it, and with the
    same bullet immediately below it. The claim must be reported in all three.

    This drives ``find_violations`` rather than ``reads_as_historical`` so that the
    rule under test is the one the gate actually applies.
    """
    scoped = _scoped_to_one_file(registry, "docs/probe.md")

    cases = (
        ("alone", [line], 1),
        ("marker on the line above", [HISTORICAL_NEIGHBOUR, line], 2),
        ("marker on the line below", [line, HISTORICAL_NEIGHBOUR], 1),
    )
    for label, body, expected in cases:
        findings = _findings_for(
            scoped, tmp_path, "docs/probe.md", "\n".join(body) + "\n"
        )
        assert [f.lineno for f in findings] == [expected], (
            f"with the {label}, the stale claim {line!r} was not reported "
            f"(findings: {[f.render() for f in findings]}). Either a "
            f"historicalMarkers pattern is broad enough to match ordinary "
            f"architecture prose, or the marker's reach has been widened beyond "
            f"the sentence it appears in."
        )


@pytest.mark.unit
def test_a_marker_elsewhere_on_the_same_line_does_not_exempt(
    registry: dict, tmp_path: pathlib.Path
) -> None:
    """One line can hold two sentences, and only one of them may be historical.

    A table row or a dense bullet often states the history and then makes a
    separate present-tense claim. Exempting the whole line because a marker
    appeared somewhere on it would let the second claim ride along behind the
    first, so the marker governs its own sentence only.
    """
    scoped = _scoped_to_one_file(registry, "docs/probe.md")
    findings = _findings_for(
        scoped,
        tmp_path,
        "docs/probe.md",
        "The AppSync data source has since been removed. AppSync GraphQL still\n"
        "serves the UI.\n",
    )
    assert [f.lineno for f in findings] == [1], [f.render() for f in findings]


#: Sentences that read as historical and must stay clean. The first is the shape
#: PR #960 adds to ``docs/threat-model.md``: an AppSync mention whose retirement
#: verb sits on the same wrapped line, in a sentence that begins two lines
#: earlier. The second is the same sentence with 'replaced' instead of 'removed',
#: which is how that sentence was phrased at one point in #960's review; the
#: markers cover both verbs so the merged tree passes either way. The third
#: carries Markdown emphasis on the verb, as docs/deployment-private-network.md
#: does.
HISTORICAL_SENTENCES = [
    "Two or more is how this model reached roughly six releases behind the\n"
    "architecture it described, including a period when it still documented an\n"
    "AWS AppSync GraphQL API that had been removed several releases earlier in\n"
    "favour of API Gateway.\n",
    "Two or more is how this model reached roughly six releases behind the\n"
    "architecture it described, including a period when it still documented an\n"
    "AWS AppSync GraphQL API that had been replaced several releases earlier by\n"
    "API Gateway.\n",
    "The AppSync resources were **removed** in v0.6 and nothing recreates them.\n",
]


@pytest.mark.unit
@pytest.mark.parametrize("body", HISTORICAL_SENTENCES)
def test_a_historical_sentence_survives_the_line_wrap(
    registry: dict, tmp_path: pathlib.Path, body: str
) -> None:
    """The complement of the marker tests: tightening must not red-line the truth.

    Prose here wraps at roughly 80 columns, so the mention and the verb that makes
    it historical frequently land on different physical lines of the same
    sentence. Sixteen correct sentences in the current corpus are written that
    way. A rule that judged each physical line alone would report all sixteen and
    push accurate documentation into the allowlist, which is why the gate
    reconstructs the sentence across a Markdown block before deciding.
    """
    scoped = _scoped_to_one_file(registry, "docs/probe.md")
    findings = _findings_for(scoped, tmp_path, "docs/probe.md", body)
    assert not findings, [f.render() for f in findings]


def _first_matching_line(path: pathlib.Path, line_pattern: str) -> str | None:
    matcher = re.compile(line_pattern, re.IGNORECASE)
    for line in path.read_text(encoding="utf-8").splitlines():
        if matcher.search(line):
            return line
    return None


@pytest.mark.unit
def test_only_declared_entries_are_whole_file(registry: dict) -> None:
    """A whole-file amnesty must say so, and must be the exception.

    ``docs/migration-appsync-to-rest.md`` is the historical record of the
    migration, so every line in it is legitimately about AppSync and its entry
    covers the file with ``linePattern: ".*"``. That is the only such entry, and it
    carries ``wholeFile: true`` so the intent is machine-readable rather than
    inferred from a suspiciously permissive regex. Any other entry that reached
    for a file-wide pattern would be exempting lines nobody reviewed.
    """
    declared = [e["path"] for e in registry["allowlist"] if e.get("wholeFile")]
    assert declared == ["docs/migration-appsync-to-rest.md"], declared

    for entry in registry["allowlist"]:
        if entry.get("wholeFile"):
            continue
        pattern = entry["linePattern"]
        assert pattern not in {".*", ".+", ""}, (entry["path"], pattern)
        # A bare service name matches every line the gate could ever flag in the
        # file, present tense or not. docs/well-architected.md was written that
        # way and PR #951 would have silently transferred the exemption to three
        # new lines. Require something more literal than the service's name.
        for service in registry["retiredServices"]:
            assert pattern.strip().lower() != service["name"].split()[-1].lower(), (
                entry["path"],
                pattern,
            )


@pytest.mark.unit
def test_a_stale_claim_in_an_allowlisted_file_is_still_caught(
    registry: dict, tmp_path: pathlib.Path
) -> None:
    """Allowlisting is per line pattern, not per file (except where declared).

    Driven from the registry rather than from one named file, so a newly added
    entry is covered the day it is added. For every entry that is not declared
    whole-file, this reconstructs a document containing the real line that entry
    exempts -- read out of the actual file, so the pattern is checked against text
    that exists -- plus a stale claim elsewhere in the same file. The exempted line
    must be credited as used and the stale claim must still be reported. Without
    this, narrowing an entry's pattern and widening it back to the whole file would
    look identical from the gate's output.
    """
    root = _repo_root()
    checked = 0

    for entry in registry["allowlist"]:
        if entry.get("wholeFile"):
            continue
        rel = entry["path"]
        real = _first_matching_line(root / rel, entry["linePattern"])
        assert real is not None, (
            f"allowlist entry {rel} / {entry['linePattern']!r} matches no line in "
            f"the file it exempts; either the text moved or the pattern is wrong"
        )

        scoped = copy.deepcopy(registry)
        scoped["scannedPaths"] = [rel]
        scoped["excludedPaths"] = []
        scoped["allowlist"] = [entry]

        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"{real}\n"
            "\n"
            "Documents are queued through SQS before the state machine starts.\n"
            "\n"
            "3. **API Layer**: AppSync GraphQL API connects the UI to backend "
            "services\n",
            encoding="utf-8",
        )

        findings, use = find_violations(scoped, tmp_path)
        assert [f.lineno for f in findings] == [5], (
            rel,
            entry["linePattern"],
            [f.render() for f in findings],
        )
        assert use.live_allowlist_entries() == {0}, (
            rel,
            entry["linePattern"],
            use.allowlist,
        )
        checked += 1

    assert checked >= 8, f"only {checked} narrow allowlist entries were exercised"


@pytest.mark.unit
def test_correct_historical_prose_is_not_reported(
    registry: dict, tmp_path: pathlib.Path
) -> None:
    """The complement of the previous test, and the reason markers exist.

    Prose wraps at roughly 80 columns, so the qualifier that makes a sentence
    historical routinely lands on the following line. Judging each line alone
    reported those continuations as stale.
    """
    scoped = copy.deepcopy(registry)
    scoped["scannedPaths"] = ["docs/*.md"]
    scoped["excludedPaths"] = []
    scoped["allowlist"] = []

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text(
        "`APIRESOLVERSTACK` was historically named `nested/appsync/`, from when the\n"
        "Web UI talked to AWS AppSync. AppSync has since been removed.\n",
        encoding="utf-8",
    )

    findings, _ = find_violations(scoped, tmp_path)
    assert not findings, [f.render() for f in findings]


@pytest.mark.unit
@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: d.update(retiredServices=[]), id="no-services"),
        pytest.param(lambda d: d.update(scannedPaths=[]), id="no-scanned-paths"),
        pytest.param(
            lambda d: d["retiredServices"][0].pop("replacement"), id="no-replacement"
        ),
        pytest.param(
            lambda d: d["historicalMarkers"][0].pop("justification"),
            id="unjustified-marker",
        ),
        pytest.param(
            lambda d: d["allowlist"][0].pop("justification"),
            id="unjustified-allowlist-entry",
        ),
        pytest.param(
            lambda d: d["allowlist"][0].update(bucket="d"), id="invalid-bucket"
        ),
        pytest.param(
            lambda d: d["excludedPaths"][0].pop("justification"),
            id="unjustified-exclusion",
        ),
        pytest.param(
            lambda d: d["allowlist"][0].update(justification="   \n\t "),
            id="whitespace-only-justification",
        ),
        pytest.param(
            lambda d: d["historicalMarkers"][0].update(justification=" "),
            id="whitespace-only-marker-justification",
        ),
        pytest.param(
            lambda d: d["excludedPaths"][0].update(justification="\t"),
            id="whitespace-only-exclusion-justification",
        ),
        pytest.param(
            lambda d: d["allowlist"][0].update(expires="when #937 merges"),
            id="legacy-unenforced-expires",
        ),
    ],
)
def test_registry_refuses_a_structure_that_would_neuter_the_gate(
    registry: dict, tmp_path: pathlib.Path, mutate
) -> None:
    """A registry that silently disabled the gate is worse than no gate.

    Each mutation below is a plausible edit -- an emptied list, a dropped
    justification, a bucket typo -- that would either stop the sweep or let an
    exemption in without a written reason. ``load_registry`` must reject all of
    them rather than carry on reporting success.
    """
    broken = copy.deepcopy(registry)
    mutate(broken)
    path = tmp_path / "retired_services.json"
    path.write_text(json.dumps(broken), encoding="utf-8")

    with pytest.raises(ValueError):
        load_registry(path)


@pytest.mark.unit
def test_every_marker_is_justified_and_narrow(registry: dict) -> None:
    """Each marker must compile and must carry a written reason.

    Compiling them here rather than only at gate time means a malformed regex
    fails a test run instead of a CI lint job on someone else's branch.
    """
    matchers = marker_matchers(registry)
    assert matchers, "the markers are load-bearing; an empty list is a mistake"
    for marker, matcher in matchers:
        assert marker["justification"].strip()
        assert matcher.pattern


@pytest.mark.unit
def test_a_marker_governs_its_own_sentence_only(registry: dict) -> None:
    """Directly pin the helper the gate calls, at the column level.

    ``find_violations`` passes each mention's column into
    ``reads_as_historical``, so the same physical line can be historical at one
    column and stale at another. Asserting that here, rather than only through the
    gate, means a regression in the sentence scoping is reported as a scoping
    failure instead of as a mysterious change in the repo-wide count.
    """
    lines = [
        "The AppSync data source has since been removed. AppSync GraphQL still",
        "serves the UI.",
    ]
    historical = lines[0].index("AppSync")
    stale = lines[0].index("AppSync", historical + 1)

    assert reads_as_historical(lines, 0, registry, historical)
    assert not reads_as_historical(lines, 0, registry, stale)


@pytest.mark.unit
def test_an_expiring_allowlist_entry_is_enforced_offline(
    registry: dict, tmp_path: pathlib.Path
) -> None:
    """``expiresAfterVersion`` must be read, not decorated.

    The field started life as ``expires: "when #937 merges"`` -- a free-text
    condition nothing evaluated, so the exemption would have outlived its reason
    unless a human happened to reread the registry. Enforcement is deliberately
    offline: the gate never calls the GitHub API, it compares the deadline against
    the repository's ``VERSION`` file, so it behaves identically on a developer
    laptop, in both CI systems and in an air-gapped clone.

    The expiring entry is SYNTHESIZED here rather than read out of the live
    registry. This test originally did ``next(e for e in registry["allowlist"] if
    e.get("expiresAfterVersion"))``, which coupled a claim about the loader to the
    repository happening to be parking a known-wrong claim at that moment. Bucket
    (a) is a parking space and the healthy state is an EMPTY one: when #937's fix
    landed and the last bucket (a) entry was deleted, that ``next()`` raised
    ``StopIteration`` and the suite failed for the one reason it should not --
    the registry getting better. ``test_every_bucket_a_entry_carries_an_expiry``
    still guards the registry's shape and passes vacuously, correctly, when there
    is nothing parked.
    """
    deadline = "1.2.3"
    synthetic = dict(registry)
    synthetic["allowlist"] = [
        *registry["allowlist"],
        {
            "path": "docs/synthetic-expiry-fixture.md",
            "linePattern": "a claim this fixture parks",
            "bucket": "a",
            "justification": (
                "Synthetic fixture for test_an_expiring_allowlist_entry_is_"
                "enforced_offline. Never written to the real registry."
            ),
            "expiresAfterVersion": deadline,
        },
    ]

    path = tmp_path / "retired_services.json"
    path.write_text(json.dumps(synthetic), encoding="utf-8")

    load_registry(path, version=deadline)
    load_registry(path, version=f"{deadline}.dev7")

    major, minor, patch = (int(part) for part in deadline.split("."))
    with pytest.raises(ValueError, match="expired after version"):
        load_registry(path, version=f"{major}.{minor}.{patch + 1}.dev1")


@pytest.mark.unit
def test_every_bucket_a_entry_carries_an_expiry(registry: dict) -> None:
    """Bucket (a) is a parking space, and a parking space needs a meter.

    Buckets (b) and (c) -- historical prose and vestigial identifiers -- are
    permanent by nature and correctly have no deadline. Bucket (a) means the claim
    is genuinely wrong and is only tolerated because another change owns the fix,
    so it must expire on a stated release or it becomes the thing this gate exists
    to prevent.
    """
    undated = [
        e["path"]
        for e in registry["allowlist"]
        if e["bucket"] == "a" and not e.get("expiresAfterVersion")
    ]
    assert not undated, undated
