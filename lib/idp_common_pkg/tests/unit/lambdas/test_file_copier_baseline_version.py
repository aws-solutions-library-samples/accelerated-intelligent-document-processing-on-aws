# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which baseline a scored run reads.

A run records the test-set version it was measured against. That number used to mean
nothing you could return to: ``publish_test_set_version`` wrote a DynamoDB row and copied
no objects, while annotation wrote straight to ``{id}/baseline/`` — so the labels a run
had scored could change afterwards and "scored against v1" was unverifiable.

Annotation sessions now snapshot the state they move away from to
``{id}/versions/{n}/baseline/``. These tests pin the consuming half: a pinned run reads
that snapshot, and everything else keeps reading the live folder, which is what stops
existing sets and historical runs from changing behaviour.
"""

import importlib.util
import os
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "../../../../..")


@pytest.fixture
def copier():
    """Load the flat Lambda `index.py` by path, with boto3 stubbed at import."""
    with patch("boto3.client"), patch("boto3.resource"):
        spec = importlib.util.spec_from_file_location(
            "test_file_copier_index",
            os.path.join(_REPO_ROOT, "src/lambda/test_file_copier/index.py"),
        )
        if spec is None or spec.loader is None:
            raise ImportError("Could not load test_file_copier/index.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


@pytest.mark.unit
class TestResolveBaselineFolder:
    def test_a_pinned_run_reads_that_version_snapshot(self, copier):
        """The property the whole snapshot mechanism exists to serve.

        Without this the labels are preserved and nothing reads them, so the version
        number stays decoration.
        """
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {"KeyCount": 1}

        with patch.object(copier, "s3", s3):
            folder = copier._resolve_baseline_folder("bkt", "ts1", 3)

        assert folder == "versions/3/baseline"
        # Probed with MaxKeys=1: existence is the question, not the contents.
        _args, kwargs = s3.list_objects_v2.call_args
        assert kwargs["Prefix"] == "ts1/versions/3/baseline/"
        assert kwargs["MaxKeys"] == 1

    def test_an_unpinned_run_reads_the_live_baselines(self, copier):
        # Most runs. Nothing should be probed at all.
        s3 = MagicMock()

        with patch.object(copier, "s3", s3):
            assert copier._resolve_baseline_folder("bkt", "ts1", None) == "baseline"

        s3.list_objects_v2.assert_not_called()

    def test_a_version_with_no_snapshot_falls_back_to_the_live_baselines(self, copier):
        """The back-compatibility case, and the one that matters most on deploy.

        Every version published before snapshots existed has no such prefix. Failing or
        staging nothing here would silently score those runs against an empty baseline —
        which reads as a catastrophic accuracy drop rather than as a missing folder.
        """
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {"KeyCount": 0}

        with patch.object(copier, "s3", s3):
            assert copier._resolve_baseline_folder("bkt", "ts1", 2) == "baseline"

    def test_version_zero_is_treated_as_unpinned(self, copier):
        # `if not test_set_version` — 0 is not a version number this system issues
        # (publish reserves from 1), and probing versions/0/ would be meaningless.
        s3 = MagicMock()

        with patch.object(copier, "s3", s3):
            assert copier._resolve_baseline_folder("bkt", "ts1", 0) == "baseline"

        s3.list_objects_v2.assert_not_called()

    def test_a_string_version_is_coerced(self, copier):
        # It arrives via JSON on an SQS message, so the type is whatever was serialised.
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {"KeyCount": 1}

        with patch.object(copier, "s3", s3):
            assert (
                copier._resolve_baseline_folder("bkt", "ts1", "4")
                == "versions/4/baseline"
            )

    def test_the_folder_composes_into_the_prefix_the_lister_builds(self, copier):
        """`_list_test_set_files` builds `{id}/{folder}/`, which is why a folder with
        slashes in it works without touching that function."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {"KeyCount": 1}

        with patch.object(copier, "s3", s3):
            folder = copier._resolve_baseline_folder("bkt", "ts1", 3)

        assert f"ts1/{folder}/" == "ts1/versions/3/baseline/"
