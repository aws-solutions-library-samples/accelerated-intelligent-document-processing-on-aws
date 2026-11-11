# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Which configuration profile a discovery request may name, and how it may save.

Every discovery operation reads the named profile's settings and most of them write
the discovered schema back into it, so the rule is the one ``updateConfiguration``
and ``setActiveVersion`` apply: a caller restricted by ``allowedConfigVersions`` may
name only a profile their scope admits. Admins are never looked up.

Two save rules sit beside it:

* a discovery job must name the profile it saves into (no fallback to whichever
  profile is active);
* ``saveMode: replace`` is refused against the active profile, because it clears
  the class list documents are being classified against.

The lookup is replaced at the module boundary (``resolve_allowed_config_versions``),
so what is under test is this resolver's use of the answer; the lookup itself has
its own suite in ``idp_common``.
"""

import importlib
import sys
from pathlib import Path
from unittest import mock

import pytest

pytestmark = pytest.mark.unit

DISCOVERY_BUCKET = "discovery-bucket"

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _event(field, arguments, groups=("Author",)):
    return {
        "info": {"fieldName": field},
        "arguments": arguments,
        "identity": {
            "claims": {
                "cognito:groups": list(groups),
                "email": "author@example.com",
                "sub": "11111111-2222-3333-4444-555555555555",
            }
        },
    }


@pytest.fixture
def index(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("DISCOVERY_BUCKET", DISCOVERY_BUCKET)
    monkeypatch.setenv("USERS_TABLE_NAME", "users-table")
    monkeypatch.setenv(
        "MULTI_DOC_DISCOVERY_STATE_MACHINE_ARN",
        "arn:aws:states:us-east-1:123456789012:stateMachine:multi-doc",
    )
    monkeypatch.delenv("DISCOVERY_TRACKING_TABLE", raising=False)
    monkeypatch.delenv("DISCOVERY_QUEUE_URL", raising=False)
    import index as module

    importlib.reload(module)
    monkeypatch.setattr(module, "_active_version", lambda: "production")
    sfn = mock.MagicMock()
    sfn.start_execution.return_value = {"executionArn": "arn:x"}
    monkeypatch.setattr(module, "sfn_client", sfn)
    return module


@pytest.fixture
def scoped(index, monkeypatch):
    """A caller whose allowedConfigVersions is ``["lending-*"]``."""
    lookup = mock.MagicMock(return_value=["lending-*"])
    monkeypatch.setattr(index, "resolve_allowed_config_versions", lookup)
    return lookup


def _call(index, field, arguments, groups=("Author",)):
    return index.handler(_event(field, arguments, groups), None)


class TestAScopedCallerNamesOnlyAProfileInScope:
    def test_start_multi_doc_discovery_outside_scope_is_refused(self, index, scoped):
        with pytest.raises(index.ResourceAccessDenied):
            _call(
                index,
                "startMultiDocDiscovery",
                {"configVersion": "payroll", "s3Bucket": DISCOVERY_BUCKET},
            )
        index.sfn_client.start_execution.assert_not_called()

    def test_start_multi_doc_discovery_inside_scope_runs(self, index, scoped):
        result = _call(
            index,
            "startMultiDocDiscovery",
            {"configVersion": "lending-v2", "s3Bucket": DISCOVERY_BUCKET},
        )
        assert result["configVersion"] == "lending-v2"
        index.sfn_client.start_execution.assert_called_once()

    def test_zip_upload_outside_scope_mints_no_url(self, index, scoped, monkeypatch):
        presign = mock.MagicMock(return_value="https://presigned")
        monkeypatch.setattr(index.s3_client, "generate_presigned_url", presign)
        with pytest.raises(index.ResourceAccessDenied):
            _call(
                index,
                "uploadMultiDocDiscoveryZip",
                {"fileName": "docs.zip", "configVersion": "payroll"},
            )
        presign.assert_not_called()

    def test_zip_upload_inside_scope_mints_a_url(self, index, scoped, monkeypatch):
        presign = mock.MagicMock(return_value="https://presigned")
        monkeypatch.setattr(index.s3_client, "generate_presigned_url", presign)
        result = _call(
            index,
            "uploadMultiDocDiscoveryZip",
            {"fileName": "docs.zip", "configVersion": "lending-v2"},
        )
        assert result["presignedUrl"] == "https://presigned"

    def test_single_document_discovery_outside_scope_creates_nothing(
        self, index, scoped, monkeypatch
    ):
        presign = mock.MagicMock()
        jobs = mock.MagicMock()
        monkeypatch.setattr(index, "create_s3_signed_post_url", presign)
        monkeypatch.setattr(index, "create_discovery_job", jobs)
        with pytest.raises(index.ResourceAccessDenied):
            _call(
                index,
                "uploadDiscoveryDocument",
                {"fileName": "a.pdf", "bucket": DISCOVERY_BUCKET, "version": "payroll"},
            )
        presign.assert_not_called()
        jobs.assert_not_called()

    def test_rules_discovery_outside_scope_creates_nothing(
        self, index, scoped, monkeypatch
    ):
        jobs = mock.MagicMock()
        monkeypatch.setattr(index, "create_discovery_job", jobs)
        with pytest.raises(index.ResourceAccessDenied):
            _call(
                index,
                "uploadDiscoveryDocument",
                {
                    "fileName": "policy.pdf",
                    "bucket": DISCOVERY_BUCKET,
                    "version": "payroll",
                    "discoveryType": "rules",
                },
            )
        jobs.assert_not_called()

    def test_single_document_discovery_inside_scope_creates_a_job(
        self, index, scoped, monkeypatch
    ):
        jobs = mock.MagicMock()
        monkeypatch.setattr(index, "create_discovery_job", jobs)
        result = _call(
            index,
            "uploadDiscoveryDocument",
            {"fileName": "a.pdf", "bucket": DISCOVERY_BUCKET, "version": "lending-v2"},
        )
        assert result["objectKey"].startswith("document/")
        jobs.assert_called_once()
        assert jobs.call_args.args[3] == "lending-v2"

    def test_auto_detect_outside_scope_is_refused_before_any_read(
        self, index, scoped, monkeypatch
    ):
        bucket_check = mock.MagicMock()
        monkeypatch.setattr(index.s3_targets, "assert_bucket_allowed", bucket_check)
        with pytest.raises(index.ResourceAccessDenied):
            _call(
                index,
                "autoDetectSections",
                {
                    "documentKey": "k.pdf",
                    "bucket": DISCOVERY_BUCKET,
                    "version": "payroll",
                },
            )
        bucket_check.assert_not_called()

    def test_auto_detect_with_no_version_checks_the_active_profile(
        self, index, scoped, monkeypatch
    ):
        # The active profile here is "production", outside ["lending-*"]: with no
        # version the operation reads the active profile, so that is what is checked.
        bucket_check = mock.MagicMock()
        monkeypatch.setattr(index.s3_targets, "assert_bucket_allowed", bucket_check)
        with pytest.raises(index.ResourceAccessDenied):
            _call(
                index,
                "autoDetectSections",
                {"documentKey": "k.pdf", "bucket": DISCOVERY_BUCKET},
            )
        bucket_check.assert_not_called()

    def test_auto_detect_with_no_version_runs_when_the_active_profile_is_in_scope(
        self, index, scoped, monkeypatch
    ):
        # The other half of the test above: what is checked is the active profile's
        # name, not "a version was given". A missing name alone is refused for a
        # scoped caller, so without this a check on the bare argument would pass both.
        import types

        monkeypatch.setattr(index, "_active_version", lambda: "lending-v1")
        monkeypatch.setattr(index.s3_targets, "assert_bucket_allowed", mock.MagicMock())
        discovery = mock.MagicMock()
        discovery.return_value.auto_detect_sections.return_value = [{"start": 1}]
        monkeypatch.setitem(
            sys.modules,
            "idp_common.discovery.classes_discovery",
            types.SimpleNamespace(ClassesDiscovery=discovery),
        )
        result = _call(
            index,
            "autoDetectSections",
            {"documentKey": "k.pdf", "bucket": DISCOVERY_BUCKET},
        )
        assert result == '[{"start": 1}]'
        discovery.assert_called_once()

    def test_the_refusal_does_not_name_the_callers_scope(self, index, scoped):
        with pytest.raises(index.ResourceAccessDenied) as excinfo:
            _call(
                index,
                "startMultiDocDiscovery",
                {"configVersion": "payroll", "s3Bucket": DISCOVERY_BUCKET},
            )
        assert str(excinfo.value).startswith("Unauthorized")
        assert "lending" not in str(excinfo.value)


class TestWhoIsLookedUp:
    def test_an_admin_is_never_looked_up(self, index, monkeypatch):
        lookup = mock.MagicMock(return_value=["lending-*"])
        monkeypatch.setattr(index, "resolve_allowed_config_versions", lookup)
        result = _call(
            index,
            "startMultiDocDiscovery",
            {"configVersion": "payroll", "s3Bucket": DISCOVERY_BUCKET},
            groups=("Admin",),
        )
        assert result["configVersion"] == "payroll"
        lookup.assert_not_called()

    def test_an_unscoped_author_is_unrestricted(self, index, monkeypatch):
        monkeypatch.setattr(
            index, "resolve_allowed_config_versions", mock.MagicMock(return_value=None)
        )
        result = _call(
            index,
            "startMultiDocDiscovery",
            {"configVersion": "payroll", "s3Bucket": DISCOVERY_BUCKET},
        )
        assert result["configVersion"] == "payroll"

    def test_a_lookup_that_fails_refuses(self, index, monkeypatch):
        from idp_common.config_scope import ScopeLookupError

        monkeypatch.setattr(
            index,
            "resolve_allowed_config_versions",
            mock.MagicMock(side_effect=ScopeLookupError("no table")),
        )
        with pytest.raises(PermissionError) as excinfo:
            _call(
                index,
                "startMultiDocDiscovery",
                {"configVersion": "lending-v2", "s3Bucket": DISCOVERY_BUCKET},
            )
        assert "could not be verified" in str(excinfo.value)
        index.sfn_client.start_execution.assert_not_called()

    def test_the_lookup_receives_the_callers_identifiers(self, index, scoped):
        _call(
            index,
            "startMultiDocDiscovery",
            {"configVersion": "lending-v2", "s3Bucket": DISCOVERY_BUCKET},
        )
        args, kwargs = scoped.call_args
        assert args[0] == "author@example.com"
        assert kwargs["caller_sub"] == "11111111-2222-3333-4444-555555555555"
        assert kwargs["users_table_name"] == "users-table"


class TestADiscoveryJobNamesItsProfile:
    def test_a_job_with_no_version_is_refused_for_every_caller(
        self, index, monkeypatch
    ):
        jobs = mock.MagicMock()
        presign = mock.MagicMock()
        monkeypatch.setattr(index, "create_discovery_job", jobs)
        monkeypatch.setattr(index, "create_s3_signed_post_url", presign)
        for discovery_type in ("classes", "rules"):
            with pytest.raises(ValueError, match="version is required"):
                _call(
                    index,
                    "uploadDiscoveryDocument",
                    {
                        "fileName": "a.pdf",
                        "bucket": DISCOVERY_BUCKET,
                        "discoveryType": discovery_type,
                    },
                    groups=("Admin",),
                )
        jobs.assert_not_called()
        presign.assert_not_called()

    def test_a_presign_only_call_still_needs_no_version(self, index, monkeypatch):
        jobs = mock.MagicMock()
        monkeypatch.setattr(index, "create_discovery_job", jobs)
        result = _call(
            index,
            "uploadDiscoveryDocument",
            {"fileName": "a.pdf", "bucket": DISCOVERY_BUCKET, "skipJobCreation": True},
            groups=("Admin",),
        )
        assert result["objectKey"]
        jobs.assert_not_called()


class TestReplaceIsRefusedOnTheActiveProfile:
    def test_multi_doc_replace_on_the_active_profile_clears_nothing(
        self, index, monkeypatch
    ):
        clear = mock.MagicMock()
        monkeypatch.setattr(index, "_clear_version_schema", clear)
        with pytest.raises(ValueError, match="active configuration profile"):
            _call(
                index,
                "startMultiDocDiscovery",
                {
                    "configVersion": "production",
                    "s3Bucket": DISCOVERY_BUCKET,
                    "saveMode": "replace",
                },
                groups=("Admin",),
            )
        clear.assert_not_called()
        index.sfn_client.start_execution.assert_not_called()

    def test_single_document_replace_on_the_active_profile_clears_nothing(
        self, index, monkeypatch
    ):
        clear = mock.MagicMock()
        presign = mock.MagicMock()
        monkeypatch.setattr(index, "_clear_version_schema", clear)
        monkeypatch.setattr(index, "create_s3_signed_post_url", presign)
        with pytest.raises(ValueError, match="active configuration profile"):
            _call(
                index,
                "uploadDiscoveryDocument",
                {
                    "fileName": "a.pdf",
                    "bucket": DISCOVERY_BUCKET,
                    "version": "production",
                    "saveMode": "replace",
                },
                groups=("Admin",),
            )
        clear.assert_not_called()
        presign.assert_not_called()

    def test_replace_on_another_profile_still_clears_it(self, index, monkeypatch):
        clear = mock.MagicMock()
        monkeypatch.setattr(index, "_clear_version_schema", clear)
        _call(
            index,
            "startMultiDocDiscovery",
            {
                "configVersion": "staging",
                "s3Bucket": DISCOVERY_BUCKET,
                "saveMode": "replace",
            },
            groups=("Admin",),
        )
        clear.assert_called_once_with("staging", discovery_type="classes")

    def test_augment_on_the_active_profile_is_unaffected(self, index, monkeypatch):
        clear = mock.MagicMock()
        monkeypatch.setattr(index, "_clear_version_schema", clear)
        result = _call(
            index,
            "startMultiDocDiscovery",
            {"configVersion": "production", "s3Bucket": DISCOVERY_BUCKET},
            groups=("Admin",),
        )
        assert result["configVersion"] == "production"
        clear.assert_not_called()


INPUT_BUCKET = "input-bucket"


def _mapping(prefix, profile, precedence="mapping", enabled=True):
    return {
        "prefix": prefix,
        "configProfile": profile,
        "metadataPrecedence": precedence,
        "enabled": enabled,
    }


class TestMultiDocumentSourceScope:
    """The documents multi-document discovery READS are held to the caller's scope,
    not only the profile it saves into."""

    @pytest.fixture
    def mappings(self, index, monkeypatch):
        import idp_common.config.prefix_mappings as pm

        monkeypatch.setenv("SCOPED_SOURCE_INPUT_BUCKET", INPUT_BUCKET)
        monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "config-table")
        current = {"entries": []}

        class _Store:
            def __init__(self, table):
                pass

            def list(self):
                if isinstance(current["entries"], Exception):
                    raise current["entries"]
                return pm.sort_entries(current["entries"])

        monkeypatch.setattr(pm, "PrefixMappingStore", _Store)
        return current

    def _start(self, index, bucket, prefix, groups=("Author",)):
        return _call(
            index,
            "startMultiDocDiscovery",
            {"configVersion": "lending-v2", "s3Bucket": bucket, "s3Prefix": prefix},
            groups,
        )

    def test_a_prefix_mapped_into_scope_runs(self, index, scoped, mappings):
        mappings["entries"] = [_mapping("lending/", "lending-v1")]
        assert self._start(index, INPUT_BUCKET, "lending/2026/")["status"] == "QUEUED"

    @pytest.mark.parametrize(
        "entries,prefix",
        [
            ([], "lending/"),
            ([_mapping("payroll/", "payroll")], "payroll/"),
            ([_mapping("lending/", "lending-v1")], "lend"),
            ([_mapping("lending/", "lending-v1")], ""),
            ([_mapping("lending/", "lending-v1", precedence="metadata")], "lending/"),
            (
                [
                    _mapping("lending/", "lending-v1"),
                    _mapping("lending/payroll/", "payroll"),
                ],
                "lending/",
            ),
            ([_mapping("lending/a.pdf", "lending-v1")], "lending/a.pdf"),
        ],
        ids=[
            "unmapped",
            "mapped-out-of-scope",
            "wider-than-mapping",
            "whole-bucket",
            "metadata-precedence",
            "narrower-mapping-out-of-scope",
            "exact-kind-mapping",
        ],
    )
    def test_an_input_prefix_not_wholly_in_scope_is_refused(
        self, index, scoped, mappings, entries, prefix
    ):
        mappings["entries"] = entries
        with pytest.raises(index.ResourceAccessDenied):
            self._start(index, INPUT_BUCKET, prefix)
        index.sfn_client.start_execution.assert_not_called()

    def test_another_bucket_is_refused(self, index, scoped, mappings):
        with pytest.raises(index.ResourceAccessDenied):
            self._start(index, "test-set-bucket", "my-set/input/")
        index.sfn_client.start_execution.assert_not_called()

    def test_a_mapping_does_not_admit_the_same_prefix_in_another_bucket(
        self, index, scoped, mappings
    ):
        """Mappings route Input-bucket keys only, so they say nothing about the
        Test Set bucket even when the prefix spells the same."""
        mappings["entries"] = [_mapping("lending/", "lending-v1")]
        with pytest.raises(index.ResourceAccessDenied):
            self._start(index, "test-set-bucket", "lending/")

    def test_unreadable_mappings_refuse(self, index, scoped, mappings):
        mappings["entries"] = RuntimeError("throttled")
        with pytest.raises(index.ResourceAccessDenied):
            self._start(index, INPUT_BUCKET, "lending/")

    def test_the_discovery_bucket_is_allowed(self, index, scoped, mappings):
        assert self._start(index, DISCOVERY_BUCKET, "mine/")["status"] == "QUEUED"

    def test_an_admin_and_an_unscoped_author_are_unaffected(
        self, index, mappings, monkeypatch
    ):
        monkeypatch.setattr(
            index, "resolve_allowed_config_versions", mock.MagicMock(return_value=None)
        )
        assert self._start(index, "test-set-bucket", "x/")["status"] == "QUEUED"
        admin = self._start(index, "test-set-bucket", "x/", groups=("Admin",))
        assert admin["status"] == "QUEUED"
