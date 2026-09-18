# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for scripts/check_threat_model_currency.py, plus the threat
model's discoverability.

The gate exists because the threat model reached six releases behind while
still describing the retired AppSync API layer, and nothing failed. So the
first thing to pin is that the repository passes the gate *today* — a gate
that is red on the branch it ships in teaches everyone to ignore it.

The rest pins the arithmetic, because it is not ordinary version arithmetic:
distance is counted in *releases listed in CHANGELOG.md*, and the current
``VERSION`` is a ``.devN`` pre-release that is not in that list yet.

``TestDiscoverability`` covers the other half of the same problem. The model was
not only stale, it was unlinked: nothing in the documentation site, the docs index
or the top-level README pointed at it, so a reader had no way to learn it existed.
Keeping it current is pointless if nobody can find it, and an unlinked page decays
back to unlinked silently.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "check_threat_model_currency.py"
THREAT_MODEL_README = REPO_ROOT / "security" / "threat-modeling" / "README.md"
DOC = REPO_ROOT / "docs" / "threat-model.md"
SIDEBAR = REPO_ROOT / "docs-site" / "astro.config.mjs"
DOCS_INDEX = REPO_ROOT / "docs" / "README.md"
ROOT_README = REPO_ROOT / "README.md"
SECURITY_INDEX = REPO_ROOT / "security" / "README.md"


def _load_gate():
    """Load the gate module by path (``scripts/`` is not on sys.path)."""
    spec = importlib.util.spec_from_file_location("currency_gate", GATE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate():
    return _load_gate()


# A stand-in for CHANGELOG.md's ordering: oldest first, as released_versions
# returns it.
RELEASES = ["0.6.5", "0.6.6", "0.6.7", "0.6.8"]
CURRENT = "0.6.9"


@pytest.mark.unit
class TestNormalize:
    """Three spellings of the same release reach this gate: the ``VERSION``
    file's ``0.6.9.dev3``, the threat model's historical ``v0.6.5.dev1``, and a
    plain ``0.6.8``. All three must reduce to one release identity or the
    lookup against CHANGELOG.md misses.
    """

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("0.6.9", "0.6.9"),
            ("0.6.9.dev3", "0.6.9"),
            ("v0.6.9", "0.6.9"),
            ("v0.6.5.dev1", "0.6.5"),
            ("  0.6.8\n", "0.6.8"),
        ],
    )
    def test_reduces_to_release_identity(self, gate, raw, expected):
        assert gate.normalize(raw) == expected

    @pytest.mark.parametrize("raw", ["", "0.6", "unreleased", "TBD"])
    def test_rejects_unusable_values(self, gate, raw):
        with pytest.raises(ValueError):
            gate.normalize(raw)


@pytest.mark.unit
class TestReleasedVersions:
    def test_returns_releases_oldest_first(self, gate):
        text = "## [Unreleased]\n\n## [0.6.8] - x\n\n## [0.6.7]\n\n## [0.6.6]\n"
        assert gate.released_versions(text) == ["0.6.6", "0.6.7", "0.6.8"]

    def test_unreleased_heading_is_not_a_release(self, gate):
        assert gate.released_versions("## [Unreleased]\n") == []

    def test_reads_the_real_changelog(self, gate):
        releases = gate.released_versions(
            (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        )
        assert len(releases) > 5, "CHANGELOG.md release headings stopped parsing"
        assert releases == sorted(
            releases, key=lambda v: [int(p) for p in v.split(".")]
        ), "released_versions must return oldest-first order"


@pytest.mark.unit
class TestReleasesBehind:
    """The distance calculation. ``CURRENT`` is the in-development version and
    so is deliberately absent from ``RELEASES``; it sits one place past 0.6.8.
    """

    def test_reviewed_against_current_is_zero(self, gate):
        assert gate.releases_behind(CURRENT, CURRENT, RELEASES) == 0

    def test_reviewed_against_newest_release_is_one(self, gate):
        # The allowed steady state: reviewed against the release that shipped,
        # while VERSION has already been bumped for the next cycle.
        assert gate.releases_behind("0.6.8", CURRENT, RELEASES) == 1

    def test_one_release_further_back_is_two(self, gate):
        assert gate.releases_behind("0.6.7", CURRENT, RELEASES) == 2

    def test_six_releases_back_is_reported_as_such(self, gate):
        releases = ["0.6.3", *RELEASES]
        assert gate.releases_behind("0.6.3", CURRENT, releases) == 5

    def test_a_version_newer_than_current_is_not_stale(self, gate):
        # Clamped rather than negative: a model reviewed ahead of VERSION is
        # unusual but it is not a currency problem.
        assert gate.releases_behind("0.6.8", "0.6.7", RELEASES) == 0

    def test_unknown_older_version_is_an_error_not_a_pass(self, gate):
        # A typo'd or invented version that predates the newest release heading
        # must not silently read as current. It is reported as its own condition
        # — a CHANGELOG gap — rather than as staleness, because the remedy
        # differs: add the heading, or fix the row.
        with pytest.raises(gate.ReviewedVersionNotReleased) as excinfo:
            gate.releases_behind("0.6.4", CURRENT, RELEASES)
        message = str(excinfo.value)
        assert "no '## [0.6.4]' heading" in message
        assert "release heading to CHANGELOG.md" in message

    def test_version_bumped_before_the_release_heading_lands(self, gate):
        """Ordinary release-commit ordering, previously a hard failure.

        The release cycle bumps ``VERSION`` to ``0.6.10.dev1`` in one commit and
        adds the ``## [0.6.9]`` heading in another. Between the two, the reviewed
        version (0.6.9) is in neither the CHANGELOG nor ``VERSION``. That used to
        raise, printing a message about CHANGELOG parsing when nothing was wrong
        with the threat model at all. It is now measured on a timeline spanning
        both endpoints: one release behind, which is inside the threshold.
        """
        behind = gate.releases_behind("0.6.9", "0.6.10", RELEASES)
        assert behind == 1
        assert behind <= gate.MAX_RELEASES_BEHIND

    def test_skipped_release_never_gets_a_heading(self, gate):
        """The second ordering: ``VERSION`` jumps 0.6.9.dev3 -> 0.7.0.dev1 and
        ``## [0.6.9]`` is never added, so the reviewed release has no heading
        permanently rather than temporarily. Same reasoning — a missing heading is
        a release-notes gap, and failing the currency gate for it would name the
        wrong remedy.
        """
        behind = gate.releases_behind("0.6.9", "0.7.0", RELEASES)
        assert behind == 1
        assert behind <= gate.MAX_RELEASES_BEHIND


@pytest.mark.unit
class TestThreshold:
    def test_threshold_is_one_release(self, gate):
        """Pinned deliberately. One release is the whole design of the gate:
        zero red-lines develop the moment VERSION is bumped, and two is how the
        model drifted six releases without anything objecting. Changing this
        number is a policy change and should not pass silently.
        """
        assert gate.MAX_RELEASES_BEHIND == 1


@pytest.mark.unit
class TestFieldParsing:
    def test_reads_the_row(self, gate):
        text = (
            "| Field | Value |\n"
            "| **Version** | 3.2 |\n"
            "| **Last reviewed against version** | 0.6.9 |\n"
        )
        assert gate.read_reviewed_version(text) == "0.6.9"

    def test_missing_row_is_an_error(self, gate):
        with pytest.raises(ValueError, match="Last reviewed against version"):
            gate.read_reviewed_version("| **Version** | 3.2 |\n")

    def test_the_real_threat_model_carries_the_field(self, gate):
        """The gate's input must exist in the corpus, not just in the gate."""
        assert gate.read_reviewed_version(
            THREAT_MODEL_README.read_text(encoding="utf-8")
        )


@pytest.mark.unit
class TestEndToEnd:
    def test_the_repository_passes_today(self):
        """The threat model was refreshed in the PR that added this gate, so a
        clean checkout must pass. If this fails, the model is overdue for a
        re-review — the failure text says what to do.
        """
        result = subprocess.run(
            [sys.executable, str(GATE)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_failure_message_names_the_remedy_not_just_the_field(self, gate):
        """A gate cleared by editing one number is worse than no gate: the model
        then asserts a currency it does not have. The message has to say so.
        """
        message = gate._failure_message("0.6.3", "0.6.9", 6, ["0.6.4", "0.6.9"])
        assert "Do NOT clear this by editing the version field alone" in message
        assert "system-overview.md" in message
        assert "build_threat_model.py" in message
        assert "make check-threat-model-currency" in message

    def test_per_document_staleness_is_advisory_only(self, gate):
        """The corpus deliberately does not claim uniform freshness: most
        documents were carried forward at the release they were last verified
        against, and bumping them wholesale would assert reviews that did not
        happen. So per-document distance is *reported* and must never fail the
        build — the exit code depends solely on the README's field.
        """
        releases = gate.released_versions(
            (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        )
        current = gate.normalize((REPO_ROOT / "VERSION").read_text(encoding="utf-8"))
        stale = gate.stale_documents(current, releases)
        assert stale, "expected carried-forward documents to be reported"
        assert all(behind > gate.MAX_RELEASES_BEHIND for _, _, behind in stale)

        result = subprocess.run(
            [sys.executable, str(GATE)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "advisory only" in result.stdout


@pytest.mark.unit
class TestDiscoverability:
    """The model was unlinked as well as stale. Each assertion below names one
    surface a reader might arrive from; losing any of them puts the corpus back
    where it was, discoverable only by browsing the repository tree.
    """

    def test_the_page_exists_with_frontmatter_and_licence(self):
        text = DOC.read_text(encoding="utf-8")
        assert text.startswith('---\ntitle: "Threat Model"\n---'), (
            "docs/*.md needs YAML frontmatter with a title; the docs site keys off it"
        )
        assert "SPDX-License-Identifier: MIT-0" in text.split("# Threat Model")[0]

    def test_the_page_is_in_the_docs_site_sidebar(self):
        assert '{ label: "Threat Model", slug: "threat-model" }' in SIDEBAR.read_text(
            encoding="utf-8"
        ), "add docs/threat-model.md to the Planning & Security sidebar group"

    def test_the_page_is_in_the_docs_index(self):
        assert "(./threat-model.md)" in DOCS_INDEX.read_text(encoding="utf-8"), (
            "add docs/threat-model.md to the docs/README.md index"
        )

    def test_the_root_readme_has_a_security_section_linking_the_model(self):
        text = ROOT_README.read_text(encoding="utf-8")
        assert "\n## Security\n" in text, (
            "the top-level README needs a Security section; it is where most "
            "readers of a public repository start"
        )
        assert "./security/threat-modeling/README.md" in text
        assert "(#security)" in text, "add the Security section to the README's TOC"

    def test_the_security_index_points_at_the_published_page(self):
        assert "../docs/threat-model.md" in SECURITY_INDEX.read_text(encoding="utf-8"), (
            "security/README.md should link the published orientation page"
        )

    def test_relative_links_on_the_page_resolve(self):
        text = DOC.read_text(encoding="utf-8")
        missing = sorted(
            target
            for target in set(re.findall(r"\]\((\./[^)#]+)", text))
            if not (DOC.parent / target).exists()
        )
        assert not missing, f"relative doc links do not resolve: {missing}"

    def test_the_page_cites_the_gate_target(self):
        assert "make check-threat-model-currency" in DOC.read_text(encoding="utf-8"), (
            "the page should tell a reader how the model is kept current"
        )
