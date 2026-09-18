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
    marker_matchers,
    reads_as_historical,
    scanned_files,
    stale_allowlist_entries,
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
    _, used = find_violations(registry, _repo_root())
    stale = stale_allowlist_entries(registry, used)
    assert not stale, [(entry["path"], entry["linePattern"]) for entry in stale]


@pytest.mark.unit
def test_no_template_declares_a_retired_resource_type(registry: dict) -> None:
    """If the service comes back, the registry -- not the prose -- is wrong."""
    assert not unexpected_resources(registry, _repo_root())


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
            if rel.startswith((".aws-sam/", "workshop/")) or "/node_modules/" in rel:
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


@pytest.mark.unit
@pytest.mark.parametrize("line", STALE_LINES_AT_HEAD)
def test_markers_do_not_exempt_a_real_stale_claim(registry: dict, line: str) -> None:
    """Pin ``historicalMarkers`` against over-broadening.

    The markers exist so correct historical prose does not need an allowlist entry
    per sentence. The risk they carry is the mirror image: a marker broad enough to
    match ordinary architecture prose would exempt genuinely stale lines and gut
    the gate without changing a single line of Python. Each fixture here is a real
    pre-fix line, judged in isolation and with a neighbour either side.
    """
    assert not reads_as_historical([line], 0, registry), (
        f"a historicalMarkers pattern matches a genuinely stale line: {line!r}. "
        f"Narrow the marker -- it must assert absence, pastness or vestigiality, "
        f"not merely co-occur with correct text."
    )


@pytest.mark.unit
def test_a_stale_claim_in_an_allowlisted_file_is_still_caught(
    registry: dict, tmp_path: pathlib.Path
) -> None:
    """Allowlisting is per line pattern, not per file (except where stated).

    ``docs/architecture.md`` is allowlisted for exactly one line -- the note that
    ``APIRESOLVERSTACK`` was once ``nested/appsync/``. A newly added claim
    elsewhere in the same file must still fail, or the exemption would have quietly
    covered the whole document. Only two entries in the registry are deliberately
    whole-file, and both say so in their justification.
    """
    scoped = copy.deepcopy(registry)
    scoped["scannedPaths"] = ["docs/*.md"]
    scoped["excludedPaths"] = []

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "architecture.md").write_text(
        "`APIRESOLVERSTACK` was historically named `nested/appsync/`.\n"
        "Documents are queued through SQS before the state machine starts.\n"
        "3. **API Layer**: AppSync GraphQL API connects the UI to backend services\n",
        encoding="utf-8",
    )

    findings, used = find_violations(scoped, tmp_path)
    assert [f.lineno for f in findings] == [3], [f.render() for f in findings]
    assert used, "the narrow entry should have been credited as used"


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
