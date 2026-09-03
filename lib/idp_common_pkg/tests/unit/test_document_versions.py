# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for idp_common.document_versions (run manifests over S3 versions)."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock

import pytest

from idp_common.document_versions import (
    build_run_id,
    delete_current_output_objects,
    delete_run_artifacts,
    list_current_output_versions,
    load_run_manifest,
    manifest_version_map,
    runs_prefix,
    snapshot_output_versions,
)


def _paginator_returning(pages):
    paginator = Mock()
    paginator.paginate.return_value = pages
    return paginator


@pytest.mark.unit
class TestRunId:
    def test_build_run_id_from_arn(self):
        run_id = build_run_id(
            "2025-07-07T14:15:30.123456+00:00",
            "arn:aws:states:us-east-1:123456789012:execution:sm:exec-abc",
        )
        assert run_id == "20250707T141530Z-exec-abc"

    def test_build_run_id_without_arn(self):
        assert build_run_id("2025-07-07T14:15:30Z", None) == "20250707T141530Z"

    def test_run_ids_sort_chronologically(self):
        older = build_run_id("2025-01-01T00:00:00Z", "arn::::::z-exec")
        newer = build_run_id("2025-07-07T14:15:30Z", "arn::::::a-exec")
        assert sorted([newer, older]) == [older, newer]

    def test_build_run_id_converts_negative_offset_to_utc(self):
        # 14:15:30-05:00 == 19:15:30Z; must normalize to UTC, not mangle the sign.
        run_id = build_run_id("2025-07-07T14:15:30-05:00", "arn::::::exec")
        assert run_id == "20250707T191530Z-exec"

    def test_build_run_id_strips_subseconds(self):
        assert build_run_id("2025-07-07T14:15:30.987654Z", None) == "20250707T141530Z"


@pytest.mark.unit
class TestListCurrentOutputVersions:
    def test_filters_latest_and_excludes_runs_prefix(self):
        s3 = Mock()
        now = datetime(2025, 7, 7, 14, 15, 30, tzinfo=timezone.utc)
        s3.get_paginator.return_value = _paginator_returning(
            [
                {
                    "Versions": [
                        {
                            "Key": "doc.pdf/sections/1/result.json",
                            "VersionId": "v-current",
                            "IsLatest": True,
                            "Size": 100,
                            "LastModified": now,
                            "ETag": '"abc"',
                        },
                        {
                            "Key": "doc.pdf/sections/1/result.json",
                            "VersionId": "v-old",
                            "IsLatest": False,
                            "Size": 90,
                            "LastModified": now,
                        },
                        {
                            "Key": "doc.pdf/runs/r1/manifest.json",
                            "VersionId": "v-manifest",
                            "IsLatest": True,
                            "Size": 10,
                            "LastModified": now,
                        },
                    ]
                }
            ]
        )
        files = list_current_output_versions(s3, "out-bucket", "doc.pdf")
        assert len(files) == 1
        assert files[0]["key"] == "doc.pdf/sections/1/result.json"
        assert files[0]["version_id"] == "v-current"
        s3.get_paginator.return_value.paginate.assert_called_once_with(
            Bucket="out-bucket", Prefix="doc.pdf/"
        )


@pytest.mark.unit
class TestSnapshot:
    def test_snapshot_writes_manifest(self):
        s3 = Mock()
        now = datetime(2025, 7, 7, 14, 15, 30, tzinfo=timezone.utc)
        s3.get_paginator.return_value = _paginator_returning(
            [
                {
                    "Versions": [
                        {
                            "Key": "doc.pdf/pages/1/rawText.json",
                            "VersionId": "v1",
                            "IsLatest": True,
                            "Size": 5,
                            "LastModified": now,
                        }
                    ]
                }
            ]
        )
        manifest, uri = snapshot_output_versions(
            s3,
            "out-bucket",
            "doc.pdf",
            run_id="20250707T141530Z-exec",
            metadata={"config_version": "v3"},
        )
        assert uri == "s3://out-bucket/doc.pdf/runs/20250707T141530Z-exec/manifest.json"
        assert manifest["file_count"] == 1
        assert manifest["config_version"] == "v3"
        put_kwargs = s3.put_object.call_args.kwargs
        assert put_kwargs["Key"] == "doc.pdf/runs/20250707T141530Z-exec/manifest.json"
        body = json.loads(put_kwargs["Body"].decode("utf-8"))
        assert body["files"][0]["version_id"] == "v1"

    def test_manifest_version_map(self):
        manifest = {"files": [{"key": "a", "version_id": "v1"}]}
        assert manifest_version_map(manifest) == {"a": "v1"}

    def test_runs_prefix(self):
        assert runs_prefix("batch/doc.pdf") == "batch/doc.pdf/runs/"


@pytest.mark.unit
class TestDeleteCurrentOutputObjects:
    """delete_current_output_objects: defeats OCR retry-safe recovery on
    re-upload (issue #719) and full reprocess. Must preserve the reserved
    runs/ prefix (version-history manifests)."""

    def test_deletes_current_objects_but_preserves_runs_prefix(self):
        s3 = MagicMock()
        s3.get_paginator.return_value = _paginator_returning(
            [
                {
                    "Contents": [
                        {"Key": "doc.pdf/pages/1/rawText.json"},
                        {"Key": "doc.pdf/pages/1/result.json"},
                        {"Key": "doc.pdf/sections/1/result.json"},
                        # runs/ prefix must be preserved
                        {"Key": "doc.pdf/runs/r1/manifest.json"},
                    ]
                }
            ]
        )
        deleted = delete_current_output_objects(s3, "out-bucket", "doc.pdf")
        assert deleted == 3

        call = s3.delete_objects.call_args.kwargs
        assert call["Bucket"] == "out-bucket"
        keys = [o["Key"] for o in call["Delete"]["Objects"]]
        assert "doc.pdf/pages/1/rawText.json" in keys
        assert "doc.pdf/pages/1/result.json" in keys
        assert "doc.pdf/sections/1/result.json" in keys
        # The version-history manifest must survive the purge.
        assert "doc.pdf/runs/r1/manifest.json" not in keys

    def test_empty_prefix_is_noop(self):
        """First-time upload: nothing to delete, and no delete_objects call."""
        s3 = MagicMock()
        s3.get_paginator.return_value = _paginator_returning([{"Contents": []}])
        deleted = delete_current_output_objects(s3, "out-bucket", "fresh.pdf")
        assert deleted == 0
        s3.delete_objects.assert_not_called()

    def test_batches_over_1000_keys(self):
        s3 = MagicMock()
        contents = [{"Key": f"doc.pdf/pages/{i}/rawText.json"} for i in range(2500)]
        s3.get_paginator.return_value = _paginator_returning([{"Contents": contents}])
        deleted = delete_current_output_objects(s3, "out-bucket", "doc.pdf")
        assert deleted == 2500
        # 2500 objects -> 3 batches of 1000/1000/500
        assert s3.delete_objects.call_count == 3
        sizes = [
            len(c.kwargs["Delete"]["Objects"]) for c in s3.delete_objects.call_args_list
        ]
        assert sizes == [1000, 1000, 500]

    def test_iterates_multi_page_pagination(self):
        """S3 list_objects_v2 returns pages of 1000; the helper must iterate all."""
        s3 = MagicMock()
        s3.get_paginator.return_value = _paginator_returning(
            [
                {"Contents": [{"Key": "doc.pdf/pages/1/rawText.json"}]},
                {"Contents": [{"Key": "doc.pdf/pages/2/rawText.json"}]},
                {"Contents": [{"Key": "doc.pdf/runs/r1/manifest.json"}]},  # preserved
                {"Contents": [{"Key": "doc.pdf/sections/1/result.json"}]},
            ]
        )
        deleted = delete_current_output_objects(s3, "out-bucket", "doc.pdf")
        # 3 non-runs objects across 4 pages
        assert deleted == 3
        # runs/ page yields no objects -> skipped; delete called for 3 pages
        assert s3.delete_objects.call_count == 3
        all_deleted_keys = [
            o["Key"]
            for c in s3.delete_objects.call_args_list
            for o in c.kwargs["Delete"]["Objects"]
        ]
        assert "doc.pdf/runs/r1/manifest.json" not in all_deleted_keys

    def test_uses_trailing_slash_prefix_to_avoid_sibling_key_collisions(self):
        """Prefix must be '<key>/' so 'doc.pdf' does not match 'doc.pdf.bak/*'."""
        s3 = MagicMock()
        s3.get_paginator.return_value = _paginator_returning([{"Contents": []}])
        delete_current_output_objects(s3, "out-bucket", "doc.pdf")
        s3.get_paginator.return_value.paginate.assert_called_once_with(
            Bucket="out-bucket", Prefix="doc.pdf/"
        )

    def test_subprefixes_scopes_scan_to_named_subprefixes_only(self):
        """subprefixes=("pages/",) scans only <key>/pages/, not <key>/. This is
        the queue_sender re-upload path — closes #719 (OCR's discovery reads
        only pages/) while keeping the blast radius off nested-document
        prefixes like foo/bar.pdf/*."""
        s3 = MagicMock()
        s3.get_paginator.return_value = _paginator_returning(
            [{"Contents": [{"Key": "foo/pages/1/rawText.json"}]}]
        )
        deleted = delete_current_output_objects(
            s3, "out-bucket", "foo", subprefixes=("pages/",)
        )
        assert deleted == 1
        s3.get_paginator.return_value.paginate.assert_called_once_with(
            Bucket="out-bucket", Prefix="foo/pages/"
        )

    def test_subprefixes_none_is_broad_purge(self):
        """subprefixes=None (default) preserves the reprocess-resolver
        semantics — purge everything under <key>/ except runs/."""
        s3 = MagicMock()
        s3.get_paginator.return_value = _paginator_returning([{"Contents": []}])
        delete_current_output_objects(s3, "out-bucket", "doc.pdf", subprefixes=None)
        s3.get_paginator.return_value.paginate.assert_called_once_with(
            Bucket="out-bucket", Prefix="doc.pdf/"
        )

    def test_subprefixes_empty_tuple_is_noop(self):
        """subprefixes=() means 'purge these zero subprefixes' — no scans,
        no deletes. Distinguishes an explicit empty from ``None`` (broad)."""
        s3 = MagicMock()
        s3.get_paginator.return_value = _paginator_returning([{"Contents": []}])
        deleted = delete_current_output_objects(
            s3, "out-bucket", "doc.pdf", subprefixes=()
        )
        assert deleted == 0
        s3.get_paginator.return_value.paginate.assert_not_called()
        s3.delete_objects.assert_not_called()

    def test_subprefixes_pages_does_not_touch_nested_doc_prefix(self):
        """Adversarial: uploads exist at both `foo` and `foo/bar.pdf`. The
        queue_sender purge of `foo` with subprefixes=("pages/",) must scan
        only `foo/pages/`, never `foo/`. A broad scan would return
        `foo/bar.pdf/runs/manifest.json` (the nested document's version
        history) and — because that key does NOT start with `foo/runs/` —
        the preserved-prefix filter would let it through to delete_objects,
        permanently orphaning the nested document's noncurrent versions."""
        s3 = MagicMock()

        # Broad scan on foo/ WOULD return the nested manifest (list_objects_v2
        # is prefix-based). But queue_sender uses subprefixes=("pages/",), so
        # only foo/pages/ is scanned — the nested manifest never appears.
        def paginate_side_effect(**kwargs):
            prefix = kwargs.get("Prefix", "")
            if prefix == "foo/pages/":
                return iter([{"Contents": [{"Key": "foo/pages/1/rawText.json"}]}])
            # If the code ever regresses to scanning foo/ broadly, this branch
            # would return the nested-doc manifest — the assertion below on
            # the delete_objects payload catches that regression.
            return iter(
                [
                    {
                        "Contents": [
                            {"Key": "foo/pages/1/rawText.json"},
                            {"Key": "foo/bar.pdf/runs/manifest.json"},
                            {"Key": "foo/bar.pdf/pages/1/result.json"},
                        ]
                    }
                ]
            )

        s3.get_paginator.return_value.paginate.side_effect = paginate_side_effect
        deleted = delete_current_output_objects(
            s3, "out-bucket", "foo", subprefixes=("pages/",)
        )
        assert deleted == 1
        # Critical: the nested document's manifest and pages must NEVER end up
        # in a delete_objects payload.
        for call in s3.delete_objects.call_args_list:
            keys = [o["Key"] for o in call.kwargs["Delete"]["Objects"]]
            assert "foo/bar.pdf/runs/manifest.json" not in keys
            assert "foo/bar.pdf/pages/1/result.json" not in keys

    def test_delete_objects_errors_raise_to_caller(self):
        """S3 delete_objects with Quiet=True still returns per-key failures
        in ``Errors``. A silent partial failure that reported success would
        re-open #719 — OCR's retry-safe recovery only needs ONE complete
        4-file page (rawText+result+textConfidence+image) to survive. Any
        error MUST raise so the caller's error path fires (queue_sender
        logs at ERROR for metric-filter alarming; reprocess resolver's
        try/except catches and surfaces to the operator)."""
        s3 = MagicMock()
        s3.get_paginator.return_value = _paginator_returning(
            [
                {
                    "Contents": [
                        {"Key": "doc.pdf/pages/1/rawText.json"},
                        {"Key": "doc.pdf/pages/1/result.json"},
                        {"Key": "doc.pdf/pages/1/textConfidence.json"},
                    ]
                }
            ]
        )
        s3.delete_objects.return_value = {
            "Errors": [
                {
                    "Key": "doc.pdf/pages/1/result.json",
                    "Code": "AccessDenied",
                    "Message": "KMS policy denies",
                }
            ]
        }
        with pytest.raises(RuntimeError, match="delete_objects reported"):
            delete_current_output_objects(s3, "out-bucket", "doc.pdf")

    def test_delete_objects_no_errors_returns_full_count(self):
        """Baseline: with no Errors in the response, the count equals the
        batch size — preserved for callers that log deleted-count."""
        s3 = MagicMock()
        s3.get_paginator.return_value = _paginator_returning(
            [
                {
                    "Contents": [
                        {"Key": "doc.pdf/pages/1/rawText.json"},
                        {"Key": "doc.pdf/pages/1/result.json"},
                    ]
                }
            ]
        )
        s3.delete_objects.return_value = {}
        deleted = delete_current_output_objects(s3, "out-bucket", "doc.pdf")
        assert deleted == 2

    def test_subprefix_without_trailing_slash_is_normalized(self):
        """A caller passing ``pages`` (no trailing /) would otherwise scan
        ``<key>/pages`` — which S3 treats as a prefix match, so
        ``<key>/pages_backup/*`` would be listed and deleted along with
        the intended ``<key>/pages/*``. The helper normalizes to
        ``<key>/pages/`` so this class of sibling-prefix collision is
        structurally impossible."""
        s3 = MagicMock()
        s3.get_paginator.return_value = _paginator_returning([{"Contents": []}])
        delete_current_output_objects(s3, "out-bucket", "foo", subprefixes=("pages",))
        s3.get_paginator.return_value.paginate.assert_called_once_with(
            Bucket="out-bucket", Prefix="foo/pages/"
        )

    def test_input_key_trailing_slash_normalized_no_double_slash(self):
        """``input_key`` ending with '/' (e.g. ``foo/``) would otherwise
        produce ``foo//`` or ``foo//pages/`` — a literal prefix that
        matches nothing in S3, silently reducing the purge to a no-op
        and leaving stale artefacts. The helper strips trailing slashes
        up front."""
        s3 = MagicMock()
        s3.get_paginator.return_value = _paginator_returning([{"Contents": []}])
        delete_current_output_objects(s3, "out-bucket", "foo/")
        s3.get_paginator.return_value.paginate.assert_called_once_with(
            Bucket="out-bucket", Prefix="foo/"
        )
        # And with a subprefix: expected ``foo/pages/``, NOT ``foo//pages/``.
        s3.reset_mock()
        s3.get_paginator.return_value = _paginator_returning([{"Contents": []}])
        delete_current_output_objects(s3, "out-bucket", "foo/", subprefixes=("pages/",))
        s3.get_paginator.return_value.paginate.assert_called_once_with(
            Bucket="out-bucket", Prefix="foo/pages/"
        )

    def test_partial_error_message_includes_success_count(self):
        """When the helper raises on partial delete errors, the caller
        cannot inspect ``deleted`` from the exception — so the message
        must include both counts so triage knows whether 0, 50, or 1950
        of a large purge succeeded before failing."""
        s3 = MagicMock()
        # Two batches: page 1 (2 keys, both succeed), page 2 (1 key, fails).
        s3.get_paginator.return_value = _paginator_returning(
            [
                {
                    "Contents": [
                        {"Key": "doc.pdf/pages/1/rawText.json"},
                        {"Key": "doc.pdf/pages/1/result.json"},
                    ]
                },
                {"Contents": [{"Key": "doc.pdf/pages/2/rawText.json"}]},
            ]
        )
        s3.delete_objects.side_effect = [
            {},  # first batch: no errors → 2 succeed
            {
                "Errors": [
                    {
                        "Key": "doc.pdf/pages/2/rawText.json",
                        "Code": "AccessDenied",
                    }
                ]
            },  # second: 1 failure
        ]
        with pytest.raises(RuntimeError, match="2 succeeded, 1 failed"):
            delete_current_output_objects(s3, "out-bucket", "doc.pdf")

    def test_subprefixes_generator_input_is_materialized(self):
        """A generator (not a Sequence, but easy to pass) was iterated
        twice — the isinstance validation loop and the ``strip('/')``
        comprehension. First pass exhausted it; second reduced to an
        empty scan and the purge silently no-oped. The helper now wraps
        in ``list()`` up front, so a generator caller works."""
        s3 = MagicMock()
        s3.get_paginator.return_value = _paginator_returning(
            [{"Contents": [{"Key": "doc.pdf/pages/1/rawText.json"}]}]
        )
        deleted = delete_current_output_objects(
            s3, "out-bucket", "doc.pdf", subprefixes=(sp for sp in ["pages/"])
        )
        assert deleted == 1
        s3.get_paginator.return_value.paginate.assert_called_once_with(
            Bucket="out-bucket", Prefix="doc.pdf/pages/"
        )

    def test_subprefix_none_entry_raises_value_error_not_attribute_error(self):
        """A ``None`` entry in ``subprefixes`` used to hit
        ``None.strip('/')`` → AttributeError, contradicting the
        docstring's advertised ``ValueError``. Rejected up front now."""
        s3 = MagicMock()
        with pytest.raises(ValueError, match="not a str"):
            delete_current_output_objects(
                s3, "out-bucket", "doc.pdf", subprefixes=(None,)
            )

    def test_subprefix_non_str_entry_raises_value_error(self):
        """Non-str entries (int, dict, etc.) get the same clean
        ValueError as ``None`` — the docstring's contract holds."""
        s3 = MagicMock()
        with pytest.raises(ValueError, match="not a str"):
            delete_current_output_objects(
                s3, "out-bucket", "doc.pdf", subprefixes=(42,)
            )

    def test_bare_str_subprefixes_raises_not_iterated_over_chars(self):
        """A caller who typo'd ``subprefixes="pages/"`` (missing the
        comma → bare str) would otherwise iterate over characters
        and produce absurd per-char scans. Reject up front."""
        s3 = MagicMock()
        with pytest.raises(ValueError, match="bare string"):
            delete_current_output_objects(
                s3, "out-bucket", "doc.pdf", subprefixes="pages/"
            )

    def test_partial_error_message_names_scoped_prefix_not_bare_key(self):
        """A narrow ``subprefixes=("pages/",)`` failure should report the
        scanned prefix (``<key>/pages/``), not the bare key — so triage
        knows the failure surface was narrow, not a full-reprocess purge."""
        s3 = MagicMock()
        s3.get_paginator.return_value = _paginator_returning(
            [{"Contents": [{"Key": "doc.pdf/pages/1/rawText.json"}]}]
        )
        s3.delete_objects.return_value = {
            "Errors": [
                {
                    "Key": "doc.pdf/pages/1/rawText.json",
                    "Code": "AccessDenied",
                }
            ]
        }
        with pytest.raises(RuntimeError, match="doc.pdf/pages/") as exc:
            delete_current_output_objects(
                s3, "out-bucket", "doc.pdf", subprefixes=("pages/",)
            )
        assert "doc.pdf/pages/" in str(exc.value)

    def test_subprefix_empty_string_raises_value_error(self):
        """An entry like ``""`` or ``"/"`` normalizes to empty, which would
        silently reduce to a no-op — indistinguishable from a fresh-key
        upload. Since the caller passed a non-empty list they clearly
        intend to scope-purge; raise so the miss is loud rather than
        letting stale ``pages/*`` survive."""
        s3 = MagicMock()
        for bad in ("", "/"):
            with pytest.raises(ValueError, match="empty entry"):
                delete_current_output_objects(
                    s3, "out-bucket", "doc.pdf", subprefixes=(bad,)
                )
        # Even one bad entry among valid ones must raise.
        with pytest.raises(ValueError, match="empty entry"):
            delete_current_output_objects(
                s3, "out-bucket", "doc.pdf", subprefixes=("pages/", "")
            )
        # Explicit empty tuple ``()`` remains an intentional no-op.
        s3.get_paginator.return_value = _paginator_returning([{"Contents": []}])
        deleted = delete_current_output_objects(
            s3, "out-bucket", "doc.pdf", subprefixes=()
        )
        assert deleted == 0

    def test_subprefix_leading_and_trailing_slashes_both_normalized(self):
        """``/pages/`` (leading + trailing) normalizes the same as ``pages``
        or ``pages/`` — a caller cannot accidentally scan a different
        prefix by passing surrounding slashes."""
        s3 = MagicMock()
        s3.get_paginator.return_value = _paginator_returning([{"Contents": []}])
        delete_current_output_objects(s3, "out-bucket", "foo", subprefixes=("/pages/",))
        s3.get_paginator.return_value.paginate.assert_called_once_with(
            Bucket="out-bucket", Prefix="foo/pages/"
        )


@pytest.mark.unit
class TestLoadAndDelete:
    def test_load_run_manifest_missing_returns_none(self):
        s3 = MagicMock()

        class NoSuchKey(Exception):
            pass

        s3.exceptions.NoSuchKey = NoSuchKey
        s3.get_object.side_effect = NoSuchKey()
        assert load_run_manifest(s3, "b", "doc.pdf", "r1") is None

    def test_delete_run_artifacts_deletes_pinned_versions_and_manifest(self):
        s3 = MagicMock()
        manifest = {
            "files": [
                {"key": "doc.pdf/sections/1/result.json", "version_id": "v1"},
                {"key": "doc.pdf/pages/1/rawText.json", "version_id": "v2"},
            ]
        }
        body = Mock()
        body.read.return_value = json.dumps(manifest).encode("utf-8")
        s3.get_object.return_value = {"Body": body}
        s3.get_paginator.return_value = _paginator_returning(
            [
                {
                    "Versions": [
                        {"Key": "doc.pdf/runs/r1/manifest.json", "VersionId": "mv1"}
                    ],
                    "DeleteMarkers": [],
                }
            ]
        )

        deleted = delete_run_artifacts(s3, "out-bucket", "doc.pdf", "r1")
        assert deleted == 2

        first_delete = s3.delete_objects.call_args_list[0].kwargs
        assert {"Key": "doc.pdf/sections/1/result.json", "VersionId": "v1"} in (
            first_delete["Delete"]["Objects"]
        )
        # Manifest's own versions removed too
        last_delete = s3.delete_objects.call_args_list[-1].kwargs
        assert {"Key": "doc.pdf/runs/r1/manifest.json", "VersionId": "mv1"} in (
            last_delete["Delete"]["Objects"]
        )

    def test_delete_run_artifacts_skips_versions_referenced_by_other_run(self):
        """A version shared with another retained run must NOT be deleted."""
        s3 = MagicMock()

        manifests = {
            # Deleting r1. It pins v1 (unique to r1) and shared-v (also in r2).
            "r1": {
                "files": [
                    {"key": "doc.pdf/sections/1/result.json", "version_id": "v1"},
                    {"key": "doc.pdf/pages/1/image.jpg", "version_id": "shared-v"},
                ]
            },
            "r2": {
                "files": [
                    {"key": "doc.pdf/sections/1/result.json", "version_id": "v2"},
                    {"key": "doc.pdf/pages/1/image.jpg", "version_id": "shared-v"},
                ]
            },
        }

        def get_object(Bucket, Key, **kw):
            # Key: doc.pdf/runs/<run_id>/manifest.json
            run_id = Key.split("/runs/")[1].split("/")[0]
            body = Mock()
            body.read.return_value = json.dumps(manifests[run_id]).encode("utf-8")
            return {"Body": body}

        s3.get_object.side_effect = get_object

        def get_paginator(op):
            if op == "list_objects_v2":
                # list_run_ids enumerates both run manifests
                return _paginator_returning(
                    [
                        {
                            "Contents": [
                                {"Key": "doc.pdf/runs/r1/manifest.json"},
                                {"Key": "doc.pdf/runs/r2/manifest.json"},
                            ]
                        }
                    ]
                )
            # list_object_versions: run-prefix cleanup for r1's manifest
            return _paginator_returning(
                [
                    {
                        "Versions": [
                            {"Key": "doc.pdf/runs/r1/manifest.json", "VersionId": "mv1"}
                        ],
                        "DeleteMarkers": [],
                    }
                ]
            )

        s3.get_paginator.side_effect = get_paginator

        deleted = delete_run_artifacts(s3, "out-bucket", "doc.pdf", "r1")

        # Only v1 (unique to r1) is deleted; shared-v is retained for r2.
        assert deleted == 1
        deleted_pairs = [
            (o["Key"], o["VersionId"])
            for call in s3.delete_objects.call_args_list
            for o in call.kwargs["Delete"]["Objects"]
        ]
        assert ("doc.pdf/sections/1/result.json", "v1") in deleted_pairs
        assert ("doc.pdf/pages/1/image.jpg", "shared-v") not in deleted_pairs
