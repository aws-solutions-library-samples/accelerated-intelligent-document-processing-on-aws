# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import pytest


@pytest.mark.unit
def test_path_extraction_logic():
    """Test the path extraction logic for testset bucket files"""
    # Simulate the path extraction logic from _copy_input_files_from_test_set_bucket
    file_key = "fcc_benchmark/input/fcc_benchmark/033f718b16cb597c065930410752c294.pdf"
    test_set_id = "fcc_demo_test_set"

    # Extract actual file path from test_set/input/file_path
    path_parts = file_key.split("/")
    if len(path_parts) >= 3 and path_parts[1] == "input":
        actual_file_path = "/".join(path_parts[2:])
        dest_key = f"{test_set_id}/input/{actual_file_path}"
    else:
        dest_key = f"{test_set_id}/input/{file_key}"

    expected = (
        "fcc_demo_test_set/input/fcc_benchmark/033f718b16cb597c065930410752c294.pdf"
    )
    assert dest_key == expected


@pytest.mark.unit
def test_baseline_path_extraction_logic():
    """Test the path extraction logic for baseline files from testset bucket"""
    # Simulate the path extraction logic from _copy_baseline_from_testset
    file_key = "fcc_benchmark/input/fcc_benchmark/033f718b16cb597c065930410752c294.pdf"
    test_set_id = "demo_test_set"

    # Extract test set name and file name from path (format: test_set_name/input/file_name)
    path_parts = file_key.split("/")
    if len(path_parts) >= 3 and path_parts[1] == "input":
        source_test_set_name = path_parts[0]
        file_name = "/".join(path_parts[2:])  # Get full path after 'input/'

        # Source baseline path in testset bucket
        source_baseline_prefix = f"{source_test_set_name}/baseline/{file_name}/"
        # Destination baseline path
        dest_baseline_prefix = f"{test_set_id}/baseline/{file_name}/"

    expected_source = (
        "fcc_benchmark/baseline/fcc_benchmark/033f718b16cb597c065930410752c294.pdf/"
    )
    expected_dest = (
        "demo_test_set/baseline/fcc_benchmark/033f718b16cb597c065930410752c294.pdf/"
    )

    assert source_baseline_prefix == expected_source
    assert dest_baseline_prefix == expected_dest


@pytest.mark.unit
def test_path_extraction_edge_cases():
    """Test edge cases for path extraction"""
    test_set_id = "test-set-1"

    # Test normal file without input path
    file_key = "simple_file.pdf"
    path_parts = file_key.split("/")
    if len(path_parts) >= 3 and path_parts[1] == "input":
        actual_file_path = "/".join(path_parts[2:])
        dest_key = f"{test_set_id}/input/{actual_file_path}"
    else:
        dest_key = f"{test_set_id}/input/{file_key}"

    assert dest_key == "test-set-1/input/simple_file.pdf"

    # Test malformed path
    file_key = "malformed/path.pdf"
    path_parts = file_key.split("/")
    if len(path_parts) >= 3 and path_parts[1] == "input":
        actual_file_path = "/".join(path_parts[2:])
        dest_key = f"{test_set_id}/input/{actual_file_path}"
    else:
        dest_key = f"{test_set_id}/input/{file_key}"

    assert dest_key == "test-set-1/input/malformed/path.pdf"


# --- Unlabeled test sets (the draft-labeling on-ramp) ----------------------
#
# The copier is invoked for every test run, including a draft-labeling run over
# a set that has no baseline yet. It used to raise "All baseline files failed to
# copy" whenever zero baseline files were copied, which made any unlabeled run
# fail before the pipeline ever started.


def _load_copier():
    """Import the copier lambda by path (it lives outside the package)."""
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve()
    for parent in root.parents:
        if (parent / "src" / "lambda" / "test_file_copier").is_dir():
            path = parent / "src" / "lambda" / "test_file_copier" / "index.py"
            break
    else:
        raise RuntimeError("Could not locate src/lambda/test_file_copier")

    spec = importlib.util.spec_from_file_location("test_file_copier_index", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["test_file_copier_index"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def copier_env(monkeypatch):
    monkeypatch.setenv("TEST_SET_BUCKET", "test-set-bucket")
    monkeypatch.setenv("INPUT_BUCKET", "input-bucket")
    monkeypatch.setenv("BASELINE_BUCKET", "baseline-bucket")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    return _load_copier()


def _copier_event():
    import json

    return {
        "Records": [
            {
                "body": json.dumps(
                    {
                        "testRunId": "ts1-run",
                        "testSetId": "ts1",
                        "trackingTable": "test-table",
                    }
                )
            }
        ]
    }


@pytest.mark.unit
def test_unlabeled_set_does_not_fail_the_run(copier_env, monkeypatch):
    """A set with no baseline is being labeled, not failing."""
    copier = copier_env
    statuses = []

    monkeypatch.setattr(
        copier,
        "_list_test_set_files",
        lambda bucket, tsid, folder: ["a.pdf"] if folder == "input" else [],
    )
    monkeypatch.setattr(copier, "_update_tracking_in_progress", lambda *a, **k: None)
    monkeypatch.setattr(
        copier,
        "_copy_files_to_bucket",
        lambda src, sp, dst, dp, files, cv=None, **kwargs: list(files),
    )
    monkeypatch.setattr(
        copier,
        "_update_test_run_status",
        lambda table, run, status, error=None, failed_count=None: statuses.append(
            (status, error)
        ),
    )

    copier.handler(_copier_event(), None)

    assert statuses == [], f"unlabeled run should not be marked failed: {statuses}"


@pytest.mark.unit
def test_failed_baseline_copies_still_fail_the_run(copier_env, monkeypatch):
    """When baselines exist but none copy, that is still a real failure."""
    copier = copier_env
    statuses = []

    monkeypatch.setattr(
        copier,
        "_list_test_set_files",
        lambda bucket, tsid, folder: (
            ["a.pdf"] if folder == "input" else ["a.pdf/sections/1/result.json"]
        ),
    )
    monkeypatch.setattr(copier, "_update_tracking_in_progress", lambda *a, **k: None)
    monkeypatch.setattr(
        copier,
        "_copy_files_to_bucket",
        # Input copies fine; every baseline copy fails.
        lambda src, sp, dst, dp, files, cv=None: (
            [] if "baseline" in sp else list(files)
        ),
    )
    monkeypatch.setattr(
        copier,
        "_update_test_run_status",
        lambda table, run, status, error=None, failed_count=None: statuses.append(
            (status, error)
        ),
    )

    copier.handler(_copier_event(), None)

    assert statuses and statuses[0][0] == "FAILED"
    assert "baseline" in statuses[0][1]


@pytest.mark.unit
def test_copy_files_to_bucket_handles_an_empty_file_list(copier_env):
    """An unlabeled set has no baseline files, so the copy list is empty.

    Regression: ThreadPoolExecutor(max_workers=0) raises "max_workers must be greater
    than 0", failing the whole labeling run. Calls the real function, since the
    handler-level tests mock _copy_files_to_bucket and skip the thread pool.
    """
    copier = copier_env
    assert (
        copier._copy_files_to_bucket(
            "src-bucket", "ts1/baseline/", "dst-bucket", "run1/", []
        )
        == []
    )


@pytest.mark.unit
def test_object_keys_restricts_to_the_named_documents(copier_env, monkeypatch):
    """objectKeys must select those exact documents, not the first N.

    numberOfFiles takes a prefix of the file list, so it cannot express "label
    these specific documents" — the case draft-labeling a subset needs.
    """
    import json

    copier = copier_env
    copied = {}

    monkeypatch.setattr(
        copier,
        "_list_test_set_files",
        lambda bucket, tsid, folder: (
            ["a.pdf", "b.pdf", "c.pdf"]
            if folder == "input"
            else [
                "a.pdf/sections/1/result.json",
                "c.pdf/sections/1/result.json",
            ]
        ),
    )
    monkeypatch.setattr(copier, "_update_tracking_in_progress", lambda *a, **k: None)

    def _copy(src, sp, dst, dp, files, cv=None, **kwargs):
        copied["baseline" if "baseline" in sp else "input"] = list(files)
        return list(files)

    monkeypatch.setattr(copier, "_copy_files_to_bucket", _copy)
    monkeypatch.setattr(copier, "_update_test_run_status", lambda *a, **k: None)

    event = {
        "Records": [
            {
                "body": json.dumps(
                    {
                        "testRunId": "ts1-run",
                        "testSetId": "ts1",
                        "trackingTable": "test-table",
                        "objectKeys": ["c.pdf"],
                    }
                )
            }
        ]
    }
    copier.handler(event, None)

    # 'c.pdf' — not 'a.pdf', which is what numberOfFiles=1 would have picked.
    assert copied["input"] == ["c.pdf"]
    # Baselines follow the same selection.
    assert copied["baseline"] == ["c.pdf/sections/1/result.json"]


@pytest.mark.unit
def test_object_keys_rejects_documents_not_in_the_test_set(copier_env, monkeypatch):
    """A typo'd document name must fail loudly, not silently label nothing."""
    import json

    copier = copier_env
    statuses = []

    monkeypatch.setattr(
        copier,
        "_list_test_set_files",
        lambda bucket, tsid, folder: ["a.pdf"] if folder == "input" else [],
    )
    monkeypatch.setattr(copier, "_update_tracking_in_progress", lambda *a, **k: None)
    monkeypatch.setattr(
        copier,
        "_copy_files_to_bucket",
        lambda *a, **k: list(a[4]) if len(a) > 4 else [],
    )
    monkeypatch.setattr(
        copier,
        "_update_test_run_status",
        lambda table, run, status, error=None, failed_count=None: statuses.append(
            (status, error)
        ),
    )

    event = {
        "Records": [
            {
                "body": json.dumps(
                    {
                        "testRunId": "ts1-run",
                        "testSetId": "ts1",
                        "trackingTable": "test-table",
                        "objectKeys": ["typo.pdf"],
                    }
                )
            }
        ]
    }
    copier.handler(event, None)

    assert statuses and statuses[0][0] == "FAILED"
    assert "typo.pdf" in statuses[0][1]
