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

import json
import pathlib
import subprocess
import sys
import urllib.error

import pytest

SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import dep_audit  # noqa: E402


def subprocess_result(code, out, err):
    """A CompletedProcess, so the generator-failure path can be driven without
    actually running `generate-dep-manifest.sh` (which resolves every dependency in
    the repository and takes minutes)."""
    return subprocess.CompletedProcess(["bash", "x"], code, out, err)


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


class TestAshConfig:
    """Guards on `.ash/.ash.yaml`.

    ASH is not in CI, so nothing else exercises this file. The dangerous mistake
    it invites is a `path`-only suppression (no `rule_id`) on a source file or
    CloudFormation template: that silently suppresses EVERY ASH finding on that
    path, turning a triage entry into a blind spot. These tests make that
    mistake fail loudly instead.
    """

    DATA_SUFFIXES = (".drawio", ".json", ".ipynb", ".md")

    @staticmethod
    def _config():
        import pathlib

        import yaml

        p = pathlib.Path(__file__).resolve().parents[3] / ".ash" / ".ash.yaml"
        assert p.exists(), f"missing {p}"
        return yaml.safe_load(p.read_text(encoding="utf-8"))

    def test_parses_and_every_suppression_is_justified(self):
        sups = self._config()["global_settings"]["suppressions"]
        assert sups
        for s in sups:
            assert s.get("path"), f"suppression without a path: {s}"
            reason = s.get("reason", "")
            assert len(reason) > 40, f"{s.get('path')}: reason too thin: {reason!r}"

    def test_path_only_suppressions_are_data_files_only(self):
        """A rule_id-less entry suppresses everything on the path, so it must not
        point at code or a template."""
        for s in self._config()["global_settings"]["suppressions"]:
            if s.get("rule_id"):
                continue
            path = s["path"]
            assert path.endswith(self.DATA_SUFFIXES) or path.endswith("/**"), (
                f"path-only suppression on a non-data path: {path!r}. Use an "
                "inline pragma, or scope the entry with a rule_id."
            )

    def test_dependency_suppressions_match_the_gating_allowlist(self):
        """The ASH entries mirror dep_audit_allowlist.json. If a dependency is
        upgraded and dropped there, this catches the stale ASH twin."""
        gating = {e["id"] for e in dep_audit.load_allowlist().values()}
        ash_ids = {
            s["rule_id"]
            for s in self._config()["global_settings"]["suppressions"]
            if s.get("rule_id", "").startswith(("GHSA-", "CVE-"))
        }
        stale = ash_ids - gating
        assert not stale, (
            f"advisories suppressed for ASH but no longer in "
            f"dep_audit_allowlist.json: {sorted(stale)}"
        )


class TestToolPreflight:
    """The gate must not silently under-cover when a tool is missing.

    A missing `jq` failed the real CI job (the Node manifest generator shells out
    to it). Two things go wrong then: the error is a bare "jq: command not found"
    buried in captured output, and `node-packages.txt` still EXISTS but is empty,
    because the shell redirect creates it before jq runs. An empty manifest audits
    clean, so this has to be caught, not shrugged off.
    """

    def test_required_tools_are_declared_with_reasons(self):
        assert "jq" in dep_audit.REQUIRED_TOOLS
        assert "uv" in dep_audit.REQUIRED_TOOLS
        for tool, why in dep_audit.REQUIRED_TOOLS.items():
            assert len(why) > 20, f"{tool}: explain why it is needed"

    def test_missing_tools_reports_absent_tool(self, monkeypatch):
        monkeypatch.setattr(
            dep_audit.shutil, "which", lambda t: None if t == "jq" else "/usr/bin/" + t
        )
        assert [t for t, _ in dep_audit.missing_tools()] == ["jq"]

    def test_missing_tools_empty_when_all_present(self, monkeypatch):
        monkeypatch.setattr(dep_audit.shutil, "which", lambda t: "/usr/bin/" + t)
        assert dep_audit.missing_tools() == []

    def test_generation_is_refused_when_a_tool_is_missing(self, monkeypatch, capsys):
        """Exit 2, and never invoke the generator."""
        monkeypatch.setattr(
            dep_audit.shutil, "which", lambda t: None if t == "jq" else "/usr/bin/" + t
        )

        def _boom(*a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("generator must not run when a tool is missing")

        monkeypatch.setattr(dep_audit.subprocess, "run", _boom)
        assert dep_audit.main([]) == 2
        out = capsys.readouterr().out
        assert "jq" in out
        assert "NOT treating this as a pass" in out


class TestEmptyManifestIsNotAPass:
    """An empty manifest is the gate's worst failure mode: it reports clean."""

    @staticmethod
    def _write(tmp_path, py_text, node_text, monkeypatch):
        py = tmp_path / "python-packages.txt"
        node = tmp_path / "node-packages.txt"
        py.write_text(py_text, encoding="utf-8")
        node.write_text(node_text, encoding="utf-8")
        monkeypatch.setattr(dep_audit, "PYTHON_MANIFEST", py)
        monkeypatch.setattr(dep_audit, "NODE_MANIFEST", node)

        def _no_network(*a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("must bail out before querying OSV")

        monkeypatch.setattr(dep_audit, "query_osv", _no_network)

    def test_empty_node_manifest_exits_2(self, tmp_path, monkeypatch, capsys):
        """The exact shape the jq failure produced: file exists, but empty."""
        self._write(tmp_path, "pillow==12.3.0\n", "", monkeypatch)
        assert dep_audit.main(["--no-generate"]) == 2
        assert "Node manifest(s) contain no pinned packages" in capsys.readouterr().out

    def test_empty_python_manifest_exits_2(self, tmp_path, monkeypatch, capsys):
        self._write(tmp_path, "", "nanoid@3.3.18\n", monkeypatch)
        assert dep_audit.main(["--no-generate"]) == 2
        assert (
            "Python manifest(s) contain no pinned packages" in capsys.readouterr().out
        )

    def test_both_populated_proceeds_to_the_audit(self, tmp_path, monkeypatch):
        """Sanity check the guard does not block the normal path."""
        self._write(tmp_path, "pillow==12.3.0\n", "nanoid@3.3.18\n", monkeypatch)
        # query_osv raises AssertionError only if we get PAST the guard, which is
        # what we want to prove here.
        with pytest.raises(AssertionError, match="must bail out"):
            dep_audit.main(["--no-generate"])


def _manifests(tmp_path, monkeypatch, py="pillow==12.3.0\n", node="nanoid@3.3.18\n"):
    """Point the gate at throwaway manifests, both populated."""
    p, n = tmp_path / "py.txt", tmp_path / "node.txt"
    p.write_text(py, encoding="utf-8")
    n.write_text(node, encoding="utf-8")
    monkeypatch.setattr(dep_audit, "PYTHON_MANIFEST", p)
    monkeypatch.setattr(dep_audit, "NODE_MANIFEST", n)
    return p, n


def _osv(monkeypatch, hits, details, allowlist=None):
    """Replace both OSV calls, so no test in this file touches the network.

    A live call would make the suite's result depend on the advisory database on
    the day it ran, which is the opposite of what a gate's own tests should assert.
    """
    monkeypatch.setattr(dep_audit, "query_osv", lambda pkgs: hits)
    monkeypatch.setattr(
        dep_audit, "_http_get_json", lambda url: details[url.rsplit("/", 1)[-1]]
    )
    monkeypatch.setattr(dep_audit, "load_allowlist", lambda: allowlist or {})


def _advisory(severity="HIGH", summary="a hole", fixed="9.9.9", name="pillow"):
    return {
        "id": "GHSA-test",
        "database_specific": {"severity": severity} if severity else {},
        "summary": summary,
        "affected": [
            {
                "package": {"name": name},
                "ranges": [{"events": [{"introduced": "0"}, {"fixed": fixed}]}],
            }
        ],
    }


@pytest.mark.unit
class TestTheGateDecision:
    """What exit code the audit produces, which is the only thing CI reads.

    Everything below is about the distinction the module docstring insists on: an
    audit that did not run must not look like an audit that passed. Exit 1 means
    "ran, found something"; exit 2 means "did not run"; exit 0 means "ran, clean".
    Collapsing 2 into 0 is the failure mode with no symptom.
    """

    def test_a_high_finding_exits_1_and_names_the_package_and_the_fix(
        self, tmp_path, monkeypatch, capsys
    ):
        _manifests(tmp_path, monkeypatch)
        _osv(
            monkeypatch,
            {"PyPI|pillow|12.3.0": ["GHSA-aaaa"]},
            {"GHSA-aaaa": _advisory("HIGH", fixed="12.4.0")},
        )
        assert dep_audit.main(["--no-generate"]) == 1
        out = capsys.readouterr().out
        assert "GATING" in out and "pillow@12.3.0" in out
        assert "12.4.0" in out, "the remedy has to be in the output to be actionable"

    def test_no_findings_exits_0(self, tmp_path, monkeypatch, capsys):
        _manifests(tmp_path, monkeypatch)
        _osv(monkeypatch, {}, {})
        assert dep_audit.main(["--no-generate"]) == 0
        assert "No dependency vulnerabilities" in capsys.readouterr().out

    def test_a_finding_below_the_threshold_is_reported_but_does_not_gate(
        self, tmp_path, monkeypatch, capsys
    ):
        """Informational, not invisible. A MODERATE advisory still has to be readable
        in the log, or nobody discovers it until it is upgraded to HIGH upstream."""
        _manifests(tmp_path, monkeypatch)
        _osv(
            monkeypatch,
            {"PyPI|pillow|12.3.0": ["GHSA-mod"]},
            {"GHSA-mod": _advisory("MODERATE")},
        )
        assert dep_audit.main(["--no-generate"]) == 0
        out = capsys.readouterr().out
        assert "BELOW THRESHOLD" in out and "GHSA-mod" in out

    def test_the_severity_flag_moves_the_threshold(self, tmp_path, monkeypatch, capsys):
        """The same advisory, gating or not depending only on --severity.

        Asserting one direction would pass against a hardcoded threshold; both
        directions on identical input is what pins the flag to the decision.
        """
        for severity, expected in (("HIGH", 0), ("MODERATE", 1)):
            _manifests(tmp_path, monkeypatch)
            _osv(
                monkeypatch,
                {"PyPI|pillow|12.3.0": ["GHSA-mod"]},
                {"GHSA-mod": _advisory("MODERATE")},
            )
            assert (
                dep_audit.main(["--no-generate", "--severity", severity]) == expected
            ), severity
            capsys.readouterr()

    def test_a_cvss_only_advisory_is_surfaced_but_does_not_gate(
        self, tmp_path, monkeypatch, capsys
    ):
        """The documented known limit, asserted so the docstring stays true.

        A record with a CVSS vector and no qualitative label ranks below the gate. That
        is a deliberate choice, not an oversight, and it is only defensible while the
        finding is still printed -- so both halves are checked.
        """
        _manifests(tmp_path, monkeypatch)
        advisory = {
            "id": "PYSEC-x",
            "severity": [{"score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
            "summary": "cvss only",
            "affected": [],
        }
        _osv(monkeypatch, {"PyPI|pillow|12.3.0": ["PYSEC-x"]}, {"PYSEC-x": advisory})
        assert dep_audit.main(["--no-generate"]) == 0
        out = capsys.readouterr().out
        assert "UNKNOWN-CVSS" in out and "PYSEC-x" in out

    def test_an_advisory_with_no_published_fix_says_so_rather_than_printing_nothing(
        self, tmp_path, monkeypatch, capsys
    ):
        """A blank remedy reads as "no information"; the reason matters here.

        Some advisories have no `fixed` event because the fix shipped outside the
        registry, which means every version matches and there is nothing to bump to.
        """
        _manifests(tmp_path, monkeypatch)
        advisory = {
            "id": "GHSA-nofix",
            "database_specific": {"severity": "HIGH"},
            "summary": "abandoned package",
            "affected": [
                {
                    "package": {"name": "pillow"},
                    "ranges": [{"events": [{"introduced": "0"}]}],
                    "database_specific": {
                        "last_known_affected_version_range": "<= 12.3.0"
                    },
                }
            ],
        }
        _osv(
            monkeypatch,
            {"PyPI|pillow|12.3.0": ["GHSA-nofix"]},
            {"GHSA-nofix": advisory},
        )
        assert dep_audit.main(["--no-generate"]) == 1
        out = capsys.readouterr().out
        assert "no fix published" in out
        assert "last known affected range" in out and "<= 12.3.0" in out


@pytest.mark.unit
class TestAllowlistSuppressionEndToEnd:
    """The allowlist is the only way a HIGH finding can stop gating, so its effect on
    the exit code is asserted here rather than only at the lookup level."""

    def test_an_allowlisted_high_finding_does_not_gate_but_is_still_printed(
        self, tmp_path, monkeypatch, capsys
    ):
        """Suppressed is not hidden. A triaged finding stays visible with its reason,
        because the reason is what a later reader has to re-evaluate."""
        _manifests(tmp_path, monkeypatch)
        _osv(
            monkeypatch,
            {"PyPI|pillow|12.3.0": ["GHSA-aaaa"]},
            {"GHSA-aaaa": _advisory("HIGH")},
            allowlist={
                "GHSA-aaaa": {"id": "GHSA-aaaa", "reason": "not reachable here"}
            },
        )
        assert dep_audit.main(["--no-generate"]) == 0
        out = capsys.readouterr().out
        assert "ALLOWLISTED" in out and "not reachable here" in out

    def test_an_entry_scoped_to_another_package_does_not_suppress_this_one(
        self, tmp_path, monkeypatch, capsys
    ):
        """Scoping has to bite at the gate, not just in the lookup helper.

        An entry written for one package silently covering every package is how an
        allowlist stops being a triage record and becomes a blanket off-switch.
        """
        _manifests(tmp_path, monkeypatch)
        _osv(
            monkeypatch,
            {"PyPI|pillow|12.3.0": ["GHSA-aaaa"]},
            {"GHSA-aaaa": _advisory("HIGH")},
            allowlist={
                "GHSA-aaaa|somethingelse": {"id": "GHSA-aaaa", "reason": "other pkg"}
            },
        )
        assert dep_audit.main(["--no-generate"]) == 1
        assert "GATING" in capsys.readouterr().out

    def test_an_allowlist_entry_with_no_reason_is_reported_as_such(
        self, tmp_path, monkeypatch, capsys
    ):
        """It still suppresses -- refusing would break the gate on a data error -- but
        it says the justification is missing, which is what makes it fixable."""
        _manifests(tmp_path, monkeypatch)
        _osv(
            monkeypatch,
            {"PyPI|pillow|12.3.0": ["GHSA-aaaa"]},
            {"GHSA-aaaa": _advisory("HIGH")},
            allowlist={"GHSA-aaaa": {"id": "GHSA-aaaa"}},
        )
        assert dep_audit.main(["--no-generate"]) == 0
        assert "(no reason given)" in capsys.readouterr().out

    def test_a_missing_allowlist_file_is_an_empty_allowlist_not_a_crash(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(dep_audit, "ALLOWLIST", tmp_path / "absent.json")
        assert dep_audit.load_allowlist() == {}


@pytest.mark.unit
class TestAnAuditThatDidNotRunIsNotAPass:
    """Every route to exit 2. Each one is a case where the answer is unknown."""

    def test_an_absent_manifest_exits_2(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(dep_audit, "PYTHON_MANIFEST", tmp_path / "nope.txt")
        monkeypatch.setattr(dep_audit, "NODE_MANIFEST", tmp_path / "also-nope.txt")
        assert dep_audit.main(["--no-generate"]) == 2
        assert "manifest(s) not found" in capsys.readouterr().err

    def test_a_failing_manifest_generator_exits_2_and_shows_both_streams(
        self, tmp_path, monkeypatch, capsys
    ):
        """Its stdout AND stderr, both on stdout in order.

        Interleaving a captured stderr onto the real stderr scrambles the ordering in a
        CI log, which once buried the actual cause above the output it belonged to.
        """
        _manifests(tmp_path, monkeypatch)
        monkeypatch.setattr(dep_audit, "missing_tools", lambda: [])
        monkeypatch.setattr(
            dep_audit.subprocess,
            "run",
            lambda *a, **k: subprocess_result(3, "the stdout", "the stderr"),
        )
        assert dep_audit.main([]) == 2
        out = capsys.readouterr().out
        assert "the stdout" in out and "the stderr" in out
        assert out.index("the stdout") < out.index("the stderr")
        assert "exit 3" in out

    def test_osv_being_unreachable_exits_2_rather_than_reporting_clean(
        self, tmp_path, monkeypatch, capsys
    ):
        """The most important single assertion in this file.

        A network failure that returned 0 would be indistinguishable from a clean
        audit, and every subsequent green build would mean nothing.
        """
        _manifests(tmp_path, monkeypatch)

        def boom(_pkgs):
            raise RuntimeError("OSV request failed after 3 attempts: timed out")

        monkeypatch.setattr(dep_audit, "query_osv", boom)
        assert dep_audit.main(["--no-generate"]) == 2
        err = capsys.readouterr().err
        assert "did not complete" in err and "false pass" in err

    def test_a_failure_fetching_severities_also_exits_2(
        self, tmp_path, monkeypatch, capsys
    ):
        """The second OSV call, not just the first.

        Package matching succeeding while severity lookup fails leaves every finding
        unclassified, so the threshold comparison would be meaningless -- but the
        earlier call having succeeded makes this the easier half to leave unguarded.
        """
        _manifests(tmp_path, monkeypatch)
        monkeypatch.setattr(
            dep_audit, "query_osv", lambda p: {"PyPI|pillow|12.3.0": ["GHSA-aaaa"]}
        )

        def boom(_url):
            raise RuntimeError("OSV request failed after 3 attempts: 503")

        monkeypatch.setattr(dep_audit, "_http_get_json", boom)
        assert dep_audit.main(["--no-generate"]) == 2
        assert "did not complete" in capsys.readouterr().err


@pytest.mark.unit
class TestHttpRetries:
    """The helpers retry, then raise -- they must not return a partial answer."""

    def test_a_get_retries_then_raises_runtime_error(self, monkeypatch):
        calls = {"n": 0}

        def fail(*a, **k):
            calls["n"] += 1
            raise urllib.error.URLError("nope")

        monkeypatch.setattr(dep_audit.urllib.request, "urlopen", fail)
        monkeypatch.setattr(dep_audit.time, "sleep", lambda s: None)
        with pytest.raises(RuntimeError, match="failed after 2 attempts"):
            dep_audit._http_get_json("https://example.invalid/x", retries=2)
        assert calls["n"] == 2

    def test_a_post_retries_then_raises_runtime_error(self, monkeypatch):
        calls = {"n": 0}

        def fail(*a, **k):
            calls["n"] += 1
            raise TimeoutError

        monkeypatch.setattr(dep_audit.urllib.request, "urlopen", fail)
        monkeypatch.setattr(dep_audit.time, "sleep", lambda s: None)
        with pytest.raises(RuntimeError, match="failed after 3 attempts"):
            dep_audit._http_post_json("https://example.invalid/x", {"queries": []})
        assert calls["n"] == 3

    def test_a_transient_failure_followed_by_success_returns_the_answer(
        self, monkeypatch
    ):
        """Retrying is only worth having if a later attempt is actually used."""
        state = {"n": 0}

        class _Resp:
            def read(self):
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def sometimes(*a, **k):
            state["n"] += 1
            if state["n"] == 1:
                raise urllib.error.URLError("first one fails")
            return _Resp()

        monkeypatch.setattr(dep_audit.urllib.request, "urlopen", sometimes)
        monkeypatch.setattr(dep_audit.time, "sleep", lambda s: None)
        assert dep_audit._http_get_json("https://example.invalid/x") == {"ok": True}


@pytest.mark.unit
class TestBatching:
    """OSV's batch endpoint has limits, so packages are chunked."""

    def test_more_packages_than_the_batch_size_are_sent_in_several_requests(
        self, monkeypatch, capsys
    ):
        """A single oversized request is rejected by OSV, and the failure is a
        RuntimeError that exits 2 -- so the whole gate depends on this chunking."""
        monkeypatch.setattr(dep_audit, "BATCH_SIZE", 2)
        sent = []

        def fake_post(url, payload, retries=3):
            sent.append(len(payload["queries"]))
            return {"results": [{} for _ in payload["queries"]]}

        monkeypatch.setattr(dep_audit, "_http_post_json", fake_post)
        pkgs = [("PyPI", f"p{i}", "1.0") for i in range(5)]
        dep_audit.query_osv(pkgs)
        capsys.readouterr()
        assert sent == [2, 2, 1]

    def test_every_packages_hits_are_keyed_by_ecosystem_name_and_version(
        self, monkeypatch, capsys
    ):
        """The key is what the allowlist's package scoping and the report both use.

        `zip` pairs each result with its query by position, so a chunking change that
        broke the ordering would attribute one package's advisories to another.
        """
        monkeypatch.setattr(dep_audit, "BATCH_SIZE", 2)

        def fake_post(url, payload, retries=3):
            return {
                "results": [
                    {"vulns": [{"id": f"GHSA-{q['package']['name']}"}]}
                    for q in payload["queries"]
                ]
            }

        monkeypatch.setattr(dep_audit, "_http_post_json", fake_post)
        pkgs = [("PyPI", "alpha", "1.0"), ("npm", "beta", "2.0")]
        hits = dep_audit.query_osv(pkgs)
        capsys.readouterr()
        assert hits == {
            "PyPI|alpha|1.0": ["GHSA-alpha"],
            "npm|beta|2.0": ["GHSA-beta"],
        }

    def test_a_package_with_no_advisories_is_absent_rather_than_an_empty_list(
        self, monkeypatch, capsys
    ):
        """Downstream iterates `hits.items()`, so an empty entry would print a package
        heading with no findings under it."""

        monkeypatch.setattr(
            dep_audit, "_http_post_json", lambda u, p, retries=3: {"results": [None]}
        )
        hits = dep_audit.query_osv([("PyPI", "clean", "1.0")])
        capsys.readouterr()
        assert hits == {}


@pytest.mark.unit
class TestTheJsonReport:
    def test_the_json_report_carries_all_three_buckets_and_the_threshold(
        self, tmp_path, monkeypatch, capsys
    ):
        """The machine-readable output has to agree with the printed one.

        A report that omitted `allowlisted` would make a suppressed finding look
        absent to anything consuming the JSON rather than the log.
        """
        _manifests(tmp_path, monkeypatch)
        _osv(
            monkeypatch,
            {"PyPI|pillow|12.3.0": ["GHSA-high", "GHSA-mod", "GHSA-ok"]},
            {
                "GHSA-high": _advisory("HIGH"),
                "GHSA-mod": _advisory("MODERATE"),
                "GHSA-ok": _advisory("HIGH"),
            },
            allowlist={"GHSA-ok": {"id": "GHSA-ok", "reason": "triaged"}},
        )
        out = tmp_path / "report.json"
        assert dep_audit.main(["--no-generate", "--json", str(out)]) == 1
        capsys.readouterr()
        report = json.loads(out.read_text())
        assert report["severity_threshold"] == "HIGH"
        assert [f["id"] for f in report["gating"]] == ["GHSA-high"]
        assert [f["id"] for f in report["allowlisted"]] == ["GHSA-ok"]
        assert [f["id"] for f in report["below_threshold"]] == ["GHSA-mod"]
        assert report["packages_audited"] == 2
