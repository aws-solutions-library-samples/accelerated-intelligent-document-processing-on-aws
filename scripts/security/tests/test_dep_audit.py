# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the dependency vulnerability gate (`scripts/security/dep_audit.py`).

This gate decides whether a build fails, so the parts that can silently
mis-classify a finding are the parts worth pinning:

- **manifest parsing** — a scoped npm name (`@scope/pkg@1.2.3`) splits on the
  LAST `@`, and the Python manifest carries non-pin lines (comments, loose
  `>=` floors from the "resolution failed" fallback section) that must be skipped
  rather than mangled into a bogus package name.
- **allowlist scoping** — a package-scoped entry must NOT suppress the same
  advisory on a different package. Getting this wrong hides real findings.
- **severity mapping** — an unrecognised label must not accidentally rank at or
  above the gate threshold.
- **`last_known_affected`** — must read only the entry for the package in hand.

No network: every test drives the pure functions directly.
"""

import pathlib
import sys

import pytest

SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import dep_audit  # noqa: E402


class TestParseNodeManifest:
    def test_scoped_names_split_on_last_at(self, tmp_path):
        """`@scope/pkg@1.2.3` must yield name `@scope/pkg`, not `` or `scope/pkg`."""
        p = tmp_path / "node-packages.txt"
        p.write_text("@scope/pkg@1.2.3\nplain@4.5.6\n@babel/core@7.29.7\n")
        assert dep_audit.parse_node_manifest(p) == [
            ("npm", "@scope/pkg", "1.2.3"),
            ("npm", "plain", "4.5.6"),
            ("npm", "@babel/core", "7.29.7"),
        ]

    def test_skips_comments_blanks_and_unversioned(self, tmp_path):
        p = tmp_path / "node-packages.txt"
        p.write_text("# a comment\n\nplain@1.0.0\nnoversion\n")
        assert dep_audit.parse_node_manifest(p) == [("npm", "plain", "1.0.0")]


class TestParsePythonManifest:
    def test_keeps_pins_and_strips_extras_and_markers(self, tmp_path):
        p = tmp_path / "python-packages.txt"
        p.write_text(
            "pillow==12.3.0\nfoo[extra]==2.0\nbar==3.0 ; python_version>='3.10'\n"
        )
        assert dep_audit.parse_python_manifest(p) == [
            ("PyPI", "pillow", "12.3.0"),
            ("PyPI", "foo", "2.0"),
            ("PyPI", "bar", "3.0"),
        ]

    def test_skips_non_pin_lines(self, tmp_path):
        """The generator appends a comment header plus loose floors when uv
        resolution fails; those lines are not auditable pins."""
        p = tmp_path / "python-packages.txt"
        p.write_text(
            "pillow==12.3.0\n"
            "# Additional packages (resolution failed — install these ...)\n"
            "loose>=1.0\n"
            "\n"
        )
        assert dep_audit.parse_python_manifest(p) == [("PyPI", "pillow", "12.3.0")]


class TestAllowlistScoping:
    ALLOWLIST = {
        "GHSA-GLOBAL": {"reason": "applies everywhere"},
        "GHSA-SCOPED|pkg-a": {"reason": "only this package"},
    }

    def test_bare_id_suppresses_any_package(self):
        entry = dep_audit.is_allowlisted(self.ALLOWLIST, "GHSA-GLOBAL", "whatever")
        assert entry is not None and entry["reason"] == "applies everywhere"

    def test_scoped_id_suppresses_its_own_package(self):
        entry = dep_audit.is_allowlisted(self.ALLOWLIST, "GHSA-SCOPED", "pkg-a")
        assert entry is not None and entry["reason"] == "only this package"

    def test_scoped_id_does_not_suppress_another_package(self):
        """The failure that matters: a scoped triage must not hide a real finding
        for the same advisory on a different dependency."""
        assert dep_audit.is_allowlisted(self.ALLOWLIST, "GHSA-SCOPED", "pkg-b") is None

    def test_unknown_id_is_not_suppressed(self):
        assert dep_audit.is_allowlisted(self.ALLOWLIST, "GHSA-NEW", "pkg-a") is None


class TestSeverity:
    def test_github_label_wins(self):
        assert (
            dep_audit.severity_of({"database_specific": {"severity": "high"}}) == "HIGH"
        )

    def test_cvss_only_record_is_labelled_not_dropped(self):
        assert (
            dep_audit.severity_of({"severity": [{"score": "CVSS:3.1/AV:N/AC:L"}]})
            == "UNKNOWN-CVSS"
        )

    def test_no_severity_information(self):
        assert dep_audit.severity_of({}) == ""

    @pytest.mark.parametrize("label", ["UNKNOWN-CVSS", "", "SOMETHING-ELSE"])
    def test_unrecognised_labels_rank_below_the_gate(self, label):
        """An unmapped label must never reach HIGH by accident — that would fail
        builds on advisories nobody has classified."""
        assert dep_audit.RANK.get(label, 0) < dep_audit.RANK["HIGH"]

    def test_critical_outranks_high(self):
        assert (
            dep_audit.RANK["CRITICAL"] > dep_audit.RANK["HIGH"] > dep_audit.RANK["LOW"]
        )


class TestAdvisoryRanges:
    def test_fixed_versions_only_from_matching_package(self):
        vuln = {
            "affected": [
                {
                    "package": {"name": "other"},
                    "ranges": [{"events": [{"fixed": "9.9"}]}],
                },
                {
                    "package": {"name": "pillow"},
                    "ranges": [{"events": [{"fixed": "12.3.0"}]}],
                },
            ]
        }
        assert dep_audit.fixed_versions(vuln, "pillow") == ["12.3.0"]

    def test_last_known_affected_only_from_matching_package(self):
        """SheetJS-style advisories carry no `fixed` event, so this field is the
        only signal that a pinned version is already past the affected range."""
        vuln = {
            "affected": [
                {
                    "package": {"name": "other"},
                    "database_specific": {"last_known_affected_version_range": "< 9"},
                },
                {
                    "package": {"name": "xlsx"},
                    "database_specific": {
                        "last_known_affected_version_range": "< 0.20.2"
                    },
                },
            ]
        }
        assert dep_audit.last_known_affected(vuln, "xlsx") == "< 0.20.2"
        assert dep_audit.last_known_affected(vuln, "absent") == ""

    def test_absent_range_metadata_is_empty_not_an_error(self):
        assert dep_audit.last_known_affected({"affected": []}, "pkg") == ""
        assert dep_audit.fixed_versions({}, "pkg") == []


class TestAllowlistFileIsValid:
    def test_committed_allowlist_parses_and_justifies_every_entry(self):
        """A triage entry with no reason is indistinguishable from sweeping a
        finding under the rug, so require one."""
        entries = dep_audit.load_allowlist()
        assert entries, "expected the committed allowlist to be non-empty"
        for key, entry in entries.items():
            assert entry.get("id"), f"{key}: missing id"
            reason = entry.get("reason", "")
            assert len(reason) > 40, f"{key}: reason too thin to audit: {reason!r}"
