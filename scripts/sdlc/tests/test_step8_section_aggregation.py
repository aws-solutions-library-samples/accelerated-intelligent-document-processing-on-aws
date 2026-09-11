# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 8 must count rows across ALL sections, and say which thing broke (#750).

The step used to read one section:

    find … -path '*/sections/*/result.json' -type f | head -1

So when the #653 boundary rules split `samples/Nuveen.pdf` mid-table, the 193
rows sitting in `sections/1` were reported as::

    ❌ FundInformation count mismatch: expected 532, got 193

Extraction had lost nothing — every section was complete and the document
reached COMPLETED in 174s. The generated failure analysis then spent its whole
budget on "verify agentic pagination over the full 532-item table" and concluded
"root cause not captured", because the assertion had named the wrong subsystem.

`head -1` was also not ordered: `find` returns directory order, so which section
got measured was incidental.

These tests pin the two properties that stop that recurring — the total is over
every section, and a mis-split is reported as a mis-split.
"""

import json

import pytest


def _section(section_id, rows, pages, list_field="FundInformation"):
    return (
        f"/tmp/r/Nuveen.pdf/sections/{section_id}/result.json",  # nosec B108
        {
            "document_class": {"type": "Estimated2024AnnualTaxableDistributions"},
            "split_document": {"page_indices": pages},
            "inference_result": {
                list_field: [{"FundName": f"F{i}"} for i in range(rows)]
            },
        },
    )


@pytest.mark.unit
class TestSummarizeListSections:
    def test_totals_across_every_section_not_just_the_first(self, cbd):
        """The exact shape of the #750 CI failure: 193 + 339 == 532."""
        sections = [
            _section(1, 193, list(range(0, 6))),
            _section(2, 339, list(range(6, 16))),
        ]
        total, report = cbd.summarize_list_sections(sections, "FundInformation")
        assert total == 532, (
            "reading one section makes a boundary mis-split look like lost rows"
        )
        assert len(report) == 2

    def test_report_names_the_pages_each_section_covers(self, cbd):
        """`page_indices` are 0-based; a human reads 1-based page numbers."""
        total, report = cbd.summarize_list_sections(
            [_section(1, 193, list(range(0, 6)))], "FundInformation"
        )
        assert total == 193
        assert report == ["section 1: pages 1-6, 193 rows"]

    def test_a_section_with_no_page_indices_is_reported_not_crashed(self, cbd):
        """A stub result.json (empty attributes) carries no page_indices."""
        path, payload = _section(1, 0, [])
        payload["split_document"] = {}
        total, report = cbd.summarize_list_sections(
            [(path, payload)], "FundInformation"
        )
        assert total == 0
        assert report == ["section 1: pages unknown, 0 rows"]

    def test_a_null_list_field_counts_as_zero(self, cbd):
        """`FundInformation: null` is not the same as a missing key, and neither
        may raise — the step has to reach its own assertion to report anything."""
        path, payload = _section(1, 0, [0])
        payload["inference_result"]["FundInformation"] = None
        total, _ = cbd.summarize_list_sections([(path, payload)], "FundInformation")
        assert total == 0

    def test_sections_are_ordered_by_numeric_id_when_the_step_sorts_them(self, cbd):
        """Section 10 must not sort before section 2 in the printed report.

        The step sorts the paths before loading; this pins the key it sorts on,
        because a lexicographic sort would print a misleading page sequence.
        """
        paths = [
            f"/tmp/r/Nuveen.pdf/sections/{i}/result.json"  # nosec B108
            for i in (10, 2, 1)
        ]
        ordered = sorted(
            paths,
            key=lambda p: (
                int(p.rsplit("/", 2)[-2]) if p.rsplit("/", 2)[-2].isdigit() else 0
            ),
        )
        assert [p.rsplit("/", 2)[-2] for p in ordered] == ["1", "2", "10"]


@pytest.mark.unit
class TestStep8FailureMessages:
    """A mis-split and lost rows must not produce the same error string."""

    def test_the_step_distinguishes_over_split_from_lost_rows(
        self, cbd, tmp_path, monkeypatch
    ):
        """Two complete sections totalling 532 is still a FAILURE — but it must
        be reported as a boundary defect, not as missing fund items."""
        section_dir = tmp_path / "Nuveen.pdf" / "sections"
        for sid, rows, pages in (
            (1, 193, list(range(0, 6))),
            (2, 339, list(range(6, 16))),
        ):
            d = section_dir / str(sid)
            d.mkdir(parents=True)
            _, payload = _section(sid, rows, pages)
            (d / "result.json").write_text(json.dumps(payload))

        class _Result:
            returncode = 0

            def __init__(self, stdout=""):
                self.stdout = stdout

        calls = []

        def fake_run(cmd, *a, **kw):
            calls.append(cmd)
            if "run-inference" in cmd:
                return _Result("Batch ID: b1\n")
            if cmd.startswith("find "):
                found = sorted(str(p) for p in section_dir.rglob("result.json"))
                return _Result("\n".join(found) + "\n")
            return _Result("")

        monkeypatch.setattr(cbd, "run_command", fake_run)
        outcome = cbd.test_step8_agentic_extraction("stack")

        assert outcome["success"] is False
        assert "over-split" in outcome["error"]
        assert "2 sections" in outcome["error"]
        # The old, misleading wording must not be what a boundary defect reports.
        assert "expected 532 fund items, got" not in outcome["error"]
        assert not any(cmd.strip().endswith("head -1") for cmd in calls), (
            "the step must read every section, not the first one find happens to return"
        )

    def test_one_complete_section_passes(self, cbd, tmp_path, monkeypatch):
        """The fixed path: 1 section, all 532 rows."""
        d = tmp_path / "Nuveen.pdf" / "sections" / "1"
        d.mkdir(parents=True)
        _, payload = _section(1, 532, list(range(0, 16)))
        (d / "result.json").write_text(json.dumps(payload))

        class _Result:
            returncode = 0

            def __init__(self, stdout=""):
                self.stdout = stdout

        def fake_run(cmd, *a, **kw):
            if "run-inference" in cmd:
                return _Result("Batch ID: b1\n")
            if cmd.startswith("find "):
                return _Result(str(d / "result.json") + "\n")
            return _Result("")

        monkeypatch.setattr(cbd, "run_command", fake_run)
        assert cbd.test_step8_agentic_extraction("stack") == {"success": True}

    def test_lost_rows_in_a_single_section_still_report_as_lost_rows(
        self, cbd, tmp_path, monkeypatch
    ):
        """The genuine extraction-truncation case keeps its own message."""
        d = tmp_path / "Nuveen.pdf" / "sections" / "1"
        d.mkdir(parents=True)
        _, payload = _section(1, 400, list(range(0, 16)))
        (d / "result.json").write_text(json.dumps(payload))

        class _Result:
            returncode = 0

            def __init__(self, stdout=""):
                self.stdout = stdout

        def fake_run(cmd, *a, **kw):
            if "run-inference" in cmd:
                return _Result("Batch ID: b1\n")
            if cmd.startswith("find "):
                return _Result(str(d / "result.json") + "\n")
            return _Result("")

        monkeypatch.setattr(cbd, "run_command", fake_run)
        outcome = cbd.test_step8_agentic_extraction("stack")
        assert outcome["success"] is False
        assert "expected 532 fund items, got 400" in outcome["error"]
        assert "over-split" not in outcome["error"]
