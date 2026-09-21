# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The published security snapshot must not print PASS over checks that did not run.

`security/test-results/<version>/rbac-dynamic.md` is an audit artifact: it is what a
reader is told the release's authorization tests established. It was generated
straight from the dynamic harness's `report.json`, and the harness counted an
inconclusive result as a pass — so the 0.6.9 snapshot carries 43 matrix cells
reading `500 ✅` under `Gate (hard failures): PASS ✅`.

Curation is the last place that can be caught, and it has to catch it even for a
report written by an older harness that has no `outcome` field, because a snapshot
may be curated from an archived run.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _load():
    path = Path(__file__).resolve().parents[1] / "security" / "curate_results.py"
    spec = importlib.util.spec_from_file_location("curate_results", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["curate_results"] = mod
    spec.loader.exec_module(mod)
    return mod


curate = _load()


def _report(tmp_path, results, totals=None):
    d = tmp_path / "stack-20260101T000000Z"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(
        json.dumps(
            {
                "region": "us-west-2",
                "git_sha": "abc1234",
                "totals": totals
                or {
                    "checks": len(results),
                    "passed": sum(1 for r in results if r["passed"]),
                    "hard_fail": 0,
                    "gap_warn": 0,
                },
            }
        )
    )
    (d / "report.json").write_text(json.dumps({"results": results}))
    return d


def _row(op, principal, status, passed, **extra):
    row = {
        "op": op,
        "principal": principal,
        "http_status": status,
        "passed": passed,
        "detail": "ok",
        "known_gap": None,
    }
    row.update(extra)
    return row


class TestTheGateIsQualified:
    def test_an_inconclusive_check_stops_an_unqualified_pass(self, tmp_path):
        d = _report(
            tmp_path,
            [
                _row("listDocuments", "Admin", 200, True),
                _row(
                    "addDocumentsToTestSet",
                    "Admin",
                    500,
                    False,
                    outcome="ERROR",
                    inconclusive=True,
                    detail="INCONCLUSIVE: server error 500",
                ),
            ],
        )

        body, meta = curate.curate_rbac_dynamic(d)

        assert meta["gate"] == "pass-with-reservations"
        assert meta["inconclusive"] == 1
        assert "PASS with reservations" in body
        assert "could not be run" in body

    def test_a_clean_run_is_still_an_unqualified_pass(self, tmp_path):
        """The control: "never says PASS" would also satisfy the test above."""
        d = _report(tmp_path, [_row("listDocuments", "Admin", 200, True)])

        body, meta = curate.curate_rbac_dynamic(d)

        assert meta["gate"] == "pass"
        assert meta["inconclusive"] == 0
        assert "Gate (hard failures):** PASS ✅" in body

    def test_a_hard_failure_still_outranks_a_reservation(self, tmp_path):
        d = _report(
            tmp_path,
            [
                _row("listDocuments", "Viewer", 200, False, detail="LEAK"),
                _row("x", "Admin", 0, False, outcome="ERROR", inconclusive=True),
            ],
            totals={"checks": 2, "passed": 0, "hard_fail": 1, "gap_warn": 0},
        )

        _, meta = curate.curate_rbac_dynamic(d)

        assert meta["gate"] == "fail"


class TestAnArchivedPreFixReportIsStillReadCorrectly:
    """The exact row shape the pre-fix harness wrote.

    Before outcomes existed, `classify()` returned `(True, f"{status}")`, so a 5xx
    cell was recorded as `{"passed": True, "detail": "500", "http_status": 500}` —
    no `outcome` key, no `inconclusive` flag, and the string "INCONCLUSIVE" nowhere
    in the harness. Recognising only the new fields would re-publish such a run as
    an unqualified pass, which is to say regenerate the page this corrects.
    """

    def _archived_500_row(self):
        return {
            "op": "addDocumentsToTestSet",
            "principal": "Admin",
            "http_status": 500,
            "passed": True,
            "detail": "500",
            "known_gap": None,
        }

    def test_the_real_pre_fix_row_shape_is_not_published_as_a_pass(self, tmp_path):
        d = _report(
            tmp_path,
            [self._archived_500_row()],
            totals={"checks": 1, "passed": 1, "hard_fail": 0, "gap_warn": 0},
        )

        body, meta = curate.curate_rbac_dynamic(d)

        assert meta["gate"] == "pass-with-reservations", (
            "an archived pre-fix report was re-published as an unqualified pass"
        )
        assert meta["inconclusive"] == 1
        assert "could not be run" in body
        assert "500 ✅" not in body, "regenerated the cell the fix exists to correct"

    def test_an_inconclusive_detail_is_also_honoured(self, tmp_path):
        """The newer shape, for a report from a harness mid-transition."""
        d = _report(
            tmp_path,
            [
                _row(
                    "addDocumentsToTestSet",
                    "Admin",
                    500,
                    False,
                    detail="INCONCLUSIVE: server error 500",
                )
            ],
        )

        body, meta = curate.curate_rbac_dynamic(d)

        assert meta["gate"] == "pass-with-reservations"
        assert "could not be run" in body

    def test_a_status_zero_row_is_inconclusive(self, tmp_path):
        """`call_body` returned 0 for a dead connection even before this change."""
        d = _report(
            tmp_path,
            [
                {
                    "op": "getAgentJobStatus",
                    "principal": "userB(reads A's job)",
                    "http_status": 0,
                    "passed": True,
                    "detail": "0",
                    "known_gap": None,
                }
            ],
            totals={"checks": 1, "passed": 1, "hard_fail": 0, "gap_warn": 0},
        )

        _, meta = curate.curate_rbac_dynamic(d)

        assert meta["inconclusive"] == 1

    def test_the_curators_rule_agrees_with_the_harnesss_definition(self):
        """One definition, two implementations that cannot share an import.

        `inconclusive()` in the harness is authoritative; this asserts the curator's
        row-level test agrees with it rather than restating it and drifting.
        """
        import importlib.util
        import sys as _sys

        path = Path(__file__).resolve().parents[1] / "test_api_rbac.py"
        spec = importlib.util.spec_from_file_location("_harness_for_curate", path)
        harness = importlib.util.module_from_spec(spec)
        _sys.modules["_harness_for_curate"] = harness
        spec.loader.exec_module(harness)

        for status in (0, 200, 400, 401, 403, 404, 409, 499, 500, 502, 503, 504, 599):
            expected = harness.inconclusive(status) is not None
            actual = curate._row_is_inconclusive({"http_status": status})
            assert actual is expected, (
                f"status {status}: harness says inconclusive={expected}, curator "
                f"says {actual} — the two definitions have drifted"
            )

    def test_a_non_numeric_status_is_not_mistaken_for_a_server_error(self):
        """`http_status` also carries "SKIP"/"ERR"/"n/a"."""
        assert curate._row_is_inconclusive({"http_status": "SKIP"}) is False
        assert curate._row_is_inconclusive({"http_status": "n/a"}) is False
        assert curate._row_is_inconclusive({"http_status": "ERR"}) is True


class TestTheMatrixCellsSayWhichIsWhich:
    def test_an_inconclusive_cell_is_not_rendered_with_a_tick(self, tmp_path):
        d = _report(
            tmp_path,
            [
                _row(
                    "addDocumentsToTestSet",
                    "Admin",
                    500,
                    False,
                    outcome="ERROR",
                    inconclusive=True,
                    detail="INCONCLUSIVE: server error 500",
                )
            ],
        )

        body, _ = curate.curate_rbac_dynamic(d)

        assert "500 ✅" not in body, "the 0.6.9 snapshot printed exactly this"
        assert "500 🛑" in body

    def test_a_skipped_cell_is_not_rendered_as_a_pass(self, tmp_path):
        d = _report(
            tmp_path,
            [_row("someOp", "Admin", "SKIP", False, outcome="SKIP")],
        )

        body, meta = curate.curate_rbac_dynamic(d)

        assert meta["skipped"] == 1
        assert "SKIP ✅" not in body


class TestTheManifestCarriesTheReservation:
    def test_the_manifest_gate_column_is_qualified(self, tmp_path, monkeypatch):
        # `_write` logs a repo-relative path, so capture instead of writing under
        # pytest's tmp dir (which is outside the repo).
        written = {}
        monkeypatch.setattr(
            curate, "_write", lambda path, text: written.__setitem__(path.name, text)
        )

        curate.write_manifest(
            tmp_path,
            "0.0.0-test",
            "2026-01-01",
            {
                "rbac_dynamic": {
                    "status": "run",
                    "gate": "pass-with-reservations",
                    "checks": 2,
                    "hard_fail": 0,
                    "inconclusive": 1,
                }
            },
        )

        manifest = written["MANIFEST.md"]

        assert "PASS with reservations" in manifest
        assert "1 not run" in manifest
