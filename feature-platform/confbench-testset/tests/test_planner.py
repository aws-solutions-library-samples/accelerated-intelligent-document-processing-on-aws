# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the ConfBench ingest planner and finalizer.

The planner's job is to download the parquet metadata exactly ONCE per ingest
and hand the workers a filtered row index. The finalizer's job is to report what
actually landed rather than what the counters claim.
"""

# The fakes mirror boto3's keyword-only signatures; most args are unused.
# pyright: reportUnusedParameter=false

import io
import json
from typing import Any, Dict, List

import pytest

# ingest/planner.py imports huggingface_hub and pyarrow at module scope (the
# HF_HOME setup has to happen before huggingface_hub loads), so importing it
# needs both present. They are heavy runtime pins listed in
# tests/requirements.txt, not general test dependencies, so skip the whole
# module rather than erroring when running in the shared gate without them.
pytest.importorskip(
    "huggingface_hub", reason="planner imports huggingface_hub at module scope"
)
pytest.importorskip("pyarrow", reason="planner imports pyarrow at module scope")


@pytest.fixture
def planner(monkeypatch):
    """The planner module with S3/DynamoDB/HuggingFace replaced by fakes."""
    import planner as planner_mod

    state: Dict[str, Any] = {
        "puts": {},
        "objects": {},
        "job_updates": [],
        "tracking_updates": [],
        "deletes": [],
        "parquet_rows": {},
    }

    class FakeS3:
        def put_object(self, Bucket, Key, Body, **kw):  # noqa: N803
            state["puts"][Key] = Body

        def get_object(self, Bucket, Key):  # noqa: N803
            if Key in state["objects"]:
                return {"Body": io.BytesIO(state["objects"][Key])}
            raise KeyError(Key)

        def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None, **kw):  # noqa: N803
            keys = [k for k in state["objects"] if k.startswith(Prefix)]
            extra = state.get("extra_objects", {}).get(Prefix, [])
            keys = keys + extra
            start = int(ContinuationToken or 0)
            page = keys[start : start + 1000]
            nxt = start + 1000
            truncated = nxt < len(keys)
            resp: Dict[str, Any] = {
                "KeyCount": len(page),
                "Contents": [{"Key": k} for k in page],
                "IsTruncated": truncated,
            }
            if truncated:
                resp["NextContinuationToken"] = str(nxt)
            return resp

        def delete_object(self, Bucket, Key):  # noqa: N803
            state["deletes"].append(Key)

    class FakeTable:
        def __init__(self, name):
            self._name = name

        def update_item(self, **kw):
            target = (
                state["job_updates"]
                if "job" in self._name
                else state["tracking_updates"]
            )
            target.append(kw)
            return {}

    class FakeDdb:
        def Table(self, name):  # noqa: N802
            return FakeTable(name)

    monkeypatch.setattr(planner_mod, "_s3", FakeS3())
    monkeypatch.setattr(planner_mod, "_ddb", FakeDdb())
    return planner_mod, state


def _fake_parquet(monkeypatch, planner_mod, rows: List[Dict[str, Any]]):
    """Stub hf_hub_download + pyarrow so no network or real file is needed."""
    monkeypatch.setattr(
        planner_mod,
        "hf_hub_download",
        lambda **kw: "/tmp/fake.parquet",  # nosec B108 - fake path returned by a monkeypatched download, never opened
    )

    class FakeTable:
        def to_pydict(self):
            return {
                "id": [r["id"] for r in rows],
                "noise_variant": [r["noise_variant"] for r in rows],
                "page_count": [r["page_count"] for r in rows],
                "json_response": [r["json_response"] for r in rows],
            }

    monkeypatch.setattr(planner_mod.pq, "read_table", lambda path: FakeTable())


def _rows(spec: List[tuple]) -> List[Dict[str, Any]]:
    return [
        {
            "id": doc_id,
            "noise_variant": variant,
            "page_count": 2,
            "json_response": {"Agency": "ACME"},
        }
        for doc_id, variant in spec
    ]


@pytest.mark.unit
class TestPlanner:
    def test_filters_rows_to_the_selected_variants(self, planner, monkeypatch):
        """Ingesting the clean tier must not write 32 GB of other variants into
        the row index the workers read."""
        planner_mod, state = planner
        _fake_parquet(
            monkeypatch,
            planner_mod,
            _rows(
                [
                    ("a__original.pdf", "original"),
                    ("b__custom15.pdf", "custom15"),
                    ("c__original.pdf", "original"),
                ]
            ),
        )
        result = planner_mod.plan_handler(
            {
                "jobId": "j1",
                "testSetId": "confbench-clean",
                "variants": ["original"],
            },
            None,
        )
        assert result["plannedFiles"] == 2
        written = json.loads(state["puts"]["_confbench_jobs/j1/rows.json"])
        assert {r["id"] for r in written} == {"a__original.pdf", "c__original.pdf"}

    def test_emits_one_shard_per_variant_at_offset_zero(self, planner, monkeypatch):
        planner_mod, _ = planner
        _fake_parquet(
            monkeypatch,
            planner_mod,
            _rows([("a__original.pdf", "original"), ("b__custom12.pdf", "custom12")]),
        )
        result = planner_mod.plan_handler(
            {
                "jobId": "j",
                "testSetId": "confbench-custom",
                "variants": ["original", "custom12"],
            },
            None,
        )
        shards = result["shards"]
        assert len(shards) == 2
        assert all(s["offset"] == 0 for s in shards)
        # Sorted for determinism, and each shard carries what the worker needs.
        assert [s["variant"] for s in shards] == ["custom12", "original"]
        for shard in shards:
            assert set(shard) == {"jobId", "testSetId", "variant", "offset"}

    def test_marks_the_job_running(self, planner, monkeypatch):
        planner_mod, state = planner
        _fake_parquet(
            monkeypatch, planner_mod, _rows([("a__original.pdf", "original")])
        )
        planner_mod.plan_handler(
            {"jobId": "j", "testSetId": "confbench-clean", "variants": ["original"]},
            None,
        )
        assert state["job_updates"]
        values = state["job_updates"][0]["ExpressionAttributeValues"]
        assert values[":s"] == "RUNNING"

    def test_registers_the_test_set_as_in_progress(self, planner, monkeypatch):
        """Test Studio should show the set arriving, not nothing at all."""
        planner_mod, state = planner
        _fake_parquet(
            monkeypatch, planner_mod, _rows([("a__original.pdf", "original")])
        )
        planner_mod.plan_handler(
            {"jobId": "j", "testSetId": "confbench-clean", "variants": ["original"]},
            None,
        )
        assert state["tracking_updates"]
        upd = state["tracking_updates"][0]
        assert upd["Key"] == {"PK": "testset#confbench-clean", "SK": "metadata"}
        assert upd["ExpressionAttributeValues"][":status"] == "IN_PROGRESS"
        assert upd["ExpressionAttributeValues"][":itemType"] == "testset"

    def test_warns_but_proceeds_when_row_count_differs_from_catalog(
        self, planner, monkeypatch, caplog
    ):
        """If upstream re-publishes, the parquet is authoritative — the job must
        still run, with the discrepancy recorded."""
        planner_mod, _ = planner
        # Catalog says original has 75 files; supply only 1.
        _fake_parquet(
            monkeypatch, planner_mod, _rows([("a__original.pdf", "original")])
        )
        with caplog.at_level("WARNING"):
            result = planner_mod.plan_handler(
                {
                    "jobId": "j",
                    "testSetId": "confbench-clean",
                    "variants": ["original"],
                },
                None,
            )
        assert result["plannedFiles"] == 1
        assert any("upstream" in r.message.lower() for r in caplog.records)

    def test_declares_its_config_version_on_the_test_set(self, planner, monkeypatch):
        """The row carries `configVersion` so Test Studio preselects this
        extension's Invoice preset. Name matching can't do it: the Feature
        Platform names presets `<featureId>-v<version>`, never the test set id.
        """
        planner_mod, state = planner
        monkeypatch.setattr(
            planner_mod, "CONFIG_VERSION_NAME", "confbench-testset-v0.1.0"
        )
        _fake_parquet(
            monkeypatch, planner_mod, _rows([("a__original.pdf", "original")])
        )
        planner_mod.plan_handler(
            {"jobId": "j", "testSetId": "confbench-clean", "variants": ["original"]},
            None,
        )
        upd = state["tracking_updates"][0]
        assert upd["ExpressionAttributeValues"][":configVersion"] == (
            "confbench-testset-v0.1.0"
        )
        # if_not_exists: an admin who repoints the set at their own tuned config
        # keeps that choice across re-ingests.
        assert (
            "#configVersion = if_not_exists(#configVersion, :configVersion)"
            in (upd["UpdateExpression"])
        )

    def test_omits_config_version_when_unset(self, planner, monkeypatch):
        """An older host without configVersion support must still register fine."""
        planner_mod, state = planner
        monkeypatch.setattr(planner_mod, "CONFIG_VERSION_NAME", "")
        _fake_parquet(
            monkeypatch, planner_mod, _rows([("a__original.pdf", "original")])
        )
        planner_mod.plan_handler(
            {"jobId": "j", "testSetId": "confbench-clean", "variants": ["original"]},
            None,
        )
        upd = state["tracking_updates"][0]
        assert ":configVersion" not in upd["ExpressionAttributeValues"]
        assert "configVersion" not in upd["UpdateExpression"]

    def test_preserves_admin_edited_metadata_on_re_ingest(self, planner, monkeypatch):
        """if_not_exists on the descriptive fields means re-ingesting updates
        status without clobbering a name or description an admin changed."""
        planner_mod, state = planner
        _fake_parquet(
            monkeypatch, planner_mod, _rows([("a__original.pdf", "original")])
        )
        planner_mod.plan_handler(
            {"jobId": "j", "testSetId": "confbench-clean", "variants": ["original"]},
            None,
        )
        expr = state["tracking_updates"][0]["UpdateExpression"]
        assert "if_not_exists(#name" in expr
        assert "if_not_exists(#description" in expr
        assert "if_not_exists(#createdAt" in expr


@pytest.mark.unit
class TestFinalizer:
    def test_counts_actual_objects_not_reported_counters(self, planner):
        """The bucket is the source of truth. A worker that died after uploading
        but before reporting would otherwise under-count."""
        planner_mod, state = planner
        for i in range(3):
            state["objects"][f"confbench-clean/input/doc{i}.pdf"] = b"x"
        result = planner_mod.finalize_handler(
            {
                "jobId": "j",
                "testSetId": "confbench-clean",
                # Counters deliberately disagree with reality.
                "results": [{"uploaded": 1, "skipped": 0, "failed": 0}],
            },
            None,
        )
        assert result["filesInBucket"] == 3
        assert result["status"] == "COMPLETED"

    def test_object_count_paginates(self, planner):
        planner_mod, state = planner
        state["extra_objects"] = {
            "confbench/input/": [f"confbench/input/doc{i}.pdf" for i in range(2700)]
        }
        result = planner_mod.finalize_handler(
            {"jobId": "j", "testSetId": "confbench", "results": []}, None
        )
        assert result["filesInBucket"] == 2700

    def test_empty_ingest_is_a_failure(self, planner):
        """Nothing landed — reporting COMPLETED would hide a total failure."""
        planner_mod, _ = planner
        result = planner_mod.finalize_handler(
            {"jobId": "j", "testSetId": "confbench-clean", "results": []}, None
        )
        assert result["status"] == "FAILED"

    def test_partial_success_is_completed_with_a_failure_count(self, planner):
        """Matches the accelerator's other deployers: degrade gracefully rather
        than discard a mostly-good test set."""
        planner_mod, state = planner
        state["objects"]["confbench-clean/input/a.pdf"] = b"x"
        result = planner_mod.finalize_handler(
            {
                "jobId": "j",
                "testSetId": "confbench-clean",
                "results": [{"uploaded": 1, "skipped": 0, "failed": 4}],
            },
            None,
        )
        assert result["status"] == "COMPLETED"
        assert result["failed"] == 4

    def test_consolidates_per_variant_failure_reports(self, planner):
        planner_mod, state = planner
        state["objects"]["confbench/input/a.pdf"] = b"x"
        state["objects"]["_confbench_jobs/j/failures/original.json"] = json.dumps(
            [{"id": "a__original.pdf", "error": "404"}]
        ).encode()
        state["objects"]["_confbench_jobs/j/failures/custom15.json"] = json.dumps(
            [{"id": "b__custom15.pdf", "error": "timeout"}]
        ).encode()
        planner_mod.finalize_handler(
            {"jobId": "j", "testSetId": "confbench", "results": []}, None
        )
        report = json.loads(state["puts"]["_confbench_jobs/j/report.json"])
        assert len(report["failures"]) == 2
        assert {f["id"] for f in report["failures"]} == {
            "a__original.pdf",
            "b__custom15.pdf",
        }

    def test_deletes_the_row_index(self, planner):
        """It is the largest artifact of the job and useless once done."""
        planner_mod, state = planner
        state["objects"]["confbench-clean/input/a.pdf"] = b"x"
        planner_mod.finalize_handler(
            {"jobId": "j", "testSetId": "confbench-clean", "results": []}, None
        )
        assert "_confbench_jobs/j/rows.json" in state["deletes"]

    def test_sums_counters_across_shards(self, planner):
        planner_mod, state = planner
        state["objects"]["confbench/input/a.pdf"] = b"x"
        result = planner_mod.finalize_handler(
            {
                "jobId": "j",
                "testSetId": "confbench",
                "results": [
                    {"uploaded": 10, "skipped": 1, "failed": 2},
                    {"uploaded": 5, "skipped": 0, "failed": 1},
                ],
            },
            None,
        )
        assert (result["uploaded"], result["skipped"], result["failed"]) == (15, 1, 3)

    def test_tolerates_non_dict_shard_results(self, planner):
        """A shard that returned null (skipped/killed) must not break finalize."""
        planner_mod, state = planner
        state["objects"]["confbench/input/a.pdf"] = b"x"
        result = planner_mod.finalize_handler(
            {
                "jobId": "j",
                "testSetId": "confbench",
                "results": [None, {"uploaded": 2, "skipped": 0, "failed": 0}],
            },
            None,
        )
        assert result["uploaded"] == 2


@pytest.mark.unit
class TestFailHandler:
    def test_marks_job_and_test_set_failed(self, planner):
        planner_mod, state = planner
        result = planner_mod.fail_handler(
            {
                "jobId": "j",
                "testSetId": "confbench-clean",
                "error": {"Error": "States.Timeout"},
            },
            None,
        )
        assert result["status"] == "FAILED"
        assert state["job_updates"][0]["ExpressionAttributeValues"][":s"] == "FAILED"
        assert (
            state["tracking_updates"][0]["ExpressionAttributeValues"][":status"]
            == "FAILED"
        )

    def test_bounds_the_error_string(self, planner):
        planner_mod, state = planner
        planner_mod.fail_handler(
            {"jobId": "j", "testSetId": "", "error": {"Cause": "x" * 5000}}, None
        )
        recorded = state["job_updates"][0]["ExpressionAttributeValues"][":e"]
        assert len(recorded) <= 500
