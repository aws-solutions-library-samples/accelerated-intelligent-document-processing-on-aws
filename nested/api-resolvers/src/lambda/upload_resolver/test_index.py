# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the sample-document operations in upload_resolver.

Covers listSampleDocuments (manifest read) and uploadSampleDocument
(server-side copy of a bundled sample from the ConfigurationBucket into the
InputBucket, including config-version metadata and batch expansion).
"""

import importlib
import json

import boto3
import pytest
from moto import mock_aws

CONFIG_BUCKET = "config-bucket"
INPUT_BUCKET = "input-bucket"
USERS_TABLE = "users-table"
CONFIG_TABLE = "configuration-table"

MANIFEST = {
    "schemaVersion": "1.0",
    "samples": [
        {
            "id": "bank-statement-multipage",
            "name": "Bank Statement (multi-page)",
            "description": "desc",
            "s3Key": "samples/bank-statement-multipage.pdf",
            "kind": "document",
            "fileCount": 1,
            "configId": "bank-statement-sample",
        },
        {
            "id": "w2",
            "name": "W-2 Forms",
            "description": "desc",
            "s3Key": "samples/w2/",
            "kind": "batch",
            "fileCount": 2,
            "configId": "fake-w2",
        },
    ],
}


def _event(field, arguments=None, groups=("Admin",), email="user@example.com"):
    return {
        "info": {"fieldName": field},
        "arguments": arguments or {},
        # `email` is the configuration-scope lookup key. The dispatcher's Cognito
        # authorizer always supplies it; an identity carrying neither it nor a `sub`
        # cannot be placed and so is DENIED, which is what
        # test_an_identity_that_cannot_be_scoped_is_refused covers.
        "identity": {"claims": {"cognito:groups": list(groups), "email": email}},
    }


@pytest.fixture
def resolver(monkeypatch):
    monkeypatch.setenv("CONFIGURATION_BUCKET", CONFIG_BUCKET)
    monkeypatch.setenv("INPUT_BUCKET", INPUT_BUCKET)
    # The configuration-scope check on the resolved profile runs against REAL
    # tables under moto rather than a stub. It is a security control, and a stubbed
    # scope lookup proves only that the stub was called.
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("USERS_TABLE_NAME", USERS_TABLE)
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", CONFIG_TABLE)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=USERS_TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "email", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "EmailIndex",
                    "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName=CONFIG_TABLE,
            KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "Configuration", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=CONFIG_BUCKET)
        s3.create_bucket(Bucket=INPUT_BUCKET)
        s3.put_object(
            Bucket=CONFIG_BUCKET,
            Key="config_library/samples-manifest.json",
            Body=json.dumps(MANIFEST).encode(),
        )
        s3.put_object(
            Bucket=CONFIG_BUCKET,
            Key="samples/bank-statement-multipage.pdf",
            Body=b"%PDF-1.4 statement",
        )
        s3.put_object(Bucket=CONFIG_BUCKET, Key="samples/w2/W2_0.pdf", Body=b"%PDF a")
        s3.put_object(Bucket=CONFIG_BUCKET, Key="samples/w2/W2_1.pdf", Body=b"%PDF b")

        # Import after the mock + env are in place so the module-level S3 client
        # is created against moto.
        import index

        importlib.reload(index)
        yield index, s3


@pytest.mark.unit
def test_list_sample_documents(resolver):
    index, _ = resolver
    result = index.handler(_event("listSampleDocuments", groups=("Viewer",)))
    assert result["success"] is True
    ids = {s["id"] for s in result["samples"]}
    assert ids == {"bank-statement-multipage", "w2"}


@pytest.mark.unit
def test_list_sample_documents_denies_unauthorized(resolver):
    index, _ = resolver
    with pytest.raises(PermissionError):
        index.handler(_event("listSampleDocuments", groups=("Reviewer",)))


@pytest.mark.unit
def test_upload_sample_document_copies_with_version_metadata(resolver):
    index, s3 = resolver
    result = index.handler(
        _event(
            "uploadSampleDocument",
            {"sampleId": "bank-statement-multipage", "prefix": "demo", "version": "bank-statement-sample"},
        )
    )
    assert result["success"] is True
    assert result["objectKeys"] == ["demo/bank-statement-multipage.pdf"]

    head = s3.head_object(Bucket=INPUT_BUCKET, Key="demo/bank-statement-multipage.pdf")
    assert head["Metadata"]["config-version"] == "bank-statement-sample"


@pytest.mark.unit
def test_upload_sample_document_batch_expands_all_files(resolver):
    index, s3 = resolver
    result = index.handler(
        _event("uploadSampleDocument", {"sampleId": "w2", "version": "fake-w2"})
    )
    assert result["success"] is True
    assert sorted(result["objectKeys"]) == ["W2_0.pdf", "W2_1.pdf"]
    listing = s3.list_objects_v2(Bucket=INPUT_BUCKET)
    assert {o["Key"] for o in listing["Contents"]} == {"W2_0.pdf", "W2_1.pdf"}


@pytest.mark.unit
def test_upload_sample_document_unknown_id(resolver):
    index, _ = resolver
    result = index.handler(_event("uploadSampleDocument", {"sampleId": "nope"}))
    assert result["success"] is False
    assert "Unknown sampleId" in result["error"]


@pytest.mark.unit
def test_upload_sample_document_denies_viewer(resolver):
    index, _ = resolver
    with pytest.raises(PermissionError):
        index.handler(
            _event("uploadSampleDocument", {"sampleId": "w2"}, groups=("Viewer",))
        )


# ---------------------------------------------------------------------------
# Which bucket and key an upload may target
#
# `bucket` and `prefix` are request arguments, and this function's role holds write
# on every bucket the deployment uses, so the request is bounded here or not at all.
# Two controls, from the shared `s3_targets` rule: the same bucket allow-list the read
# path in get_file_contents_resolver applies, plus a write-once key rule that write
# paths consult and the read path does not.
#
# `test_a_legitimate_upload_still_succeeds` is the one that has to hold: this is a
# live path the UI uses on every document upload, so a control that over-refuses
# breaks uploading rather than protecting it.
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestTheUploadTargetIsConstrained:
    def test_a_legitimate_upload_still_succeeds(self, resolver):
        index, _ = resolver

        result = index.handler(
            _event(
                "uploadDocument",
                {"fileName": "invoice.pdf", "prefix": "lending"},
            )
        )

        assert result["objectKey"] == "lending/invoice.pdf"
        assert result["presignedUrl"]

    def test_an_upload_with_no_prefix_still_succeeds(self, resolver):
        index, _ = resolver

        result = index.handler(_event("uploadDocument", {"fileName": "scan.pdf"}))

        assert result["objectKey"] == "scan.pdf"

    def test_a_bucket_outside_the_deployment_is_refused(self, resolver):
        index, _ = resolver

        with pytest.raises(PermissionError) as excinfo:
            index.handler(
                _event(
                    "uploadDocument",
                    {"fileName": "x.pdf", "bucket": "someone-elses-bucket"},
                )
            )

        assert str(excinfo.value).startswith("Unauthorized")
        assert "someone-elses-bucket" not in str(excinfo.value)

    def test_a_write_once_prefix_is_refused(self, resolver):
        """Some keys hold objects whose integrity comes from the key: written once,
        then read as the record of what happened."""
        index, _ = resolver

        with pytest.raises(PermissionError) as excinfo:
            index.handler(
                _event(
                    "uploadDocument",
                    {
                        "fileName": "000001.json.gz",
                        "prefix": "config_revisions/default",
                        "bucket": CONFIG_BUCKET,
                    },
                )
            )

        assert str(excinfo.value).startswith("Unauthorized")

    def test_the_other_write_once_store_is_refused_too(self, resolver):
        index, _ = resolver

        with pytest.raises(PermissionError):
            index.handler(
                _event(
                    "uploadDocument",
                    {
                        "fileName": "manifest.json",
                        "prefix": "mydoc.pdf/runs/20260101T000000Z-abc",
                    },
                )
            )

    def test_no_presigned_url_is_minted_for_a_refused_target(self, resolver):
        """The refusal has to precede the mint, or the capability already exists."""
        index, s3 = resolver
        minted = []
        original = index.s3_client.generate_presigned_post

        def _spy(**kwargs):
            minted.append(kwargs)
            return original(**kwargs)

        index.s3_client.generate_presigned_post = _spy
        try:
            with pytest.raises(PermissionError):
                index.handler(
                    _event(
                        "uploadDocument",
                        {"fileName": "x.pdf", "bucket": "someone-elses-bucket"},
                    )
                )
        finally:
            index.s3_client.generate_presigned_post = original

        assert minted == [], "a presigned URL was minted for a refused target"

    def test_a_sample_copy_into_a_write_once_prefix_is_refused(self, resolver):
        """The sample-copy path takes `prefix` from the request too.

        Uses the revision prefix rather than a run path: the run rule is scoped to the
        manifest object itself, and the copied file's name comes from the sample
        manifest, so a run path here would not land on the protected key. That the
        rule declines to refuse it is correct — see the note on WRITE_ONCE_KEY_RULES
        about why it is not widened to the whole `runs/` directory.
        """
        index, _ = resolver

        with pytest.raises(PermissionError):
            index.handler(
                _event(
                    "uploadSampleDocument",
                    {
                        "sampleId": "bank-statement-multipage",
                        "prefix": "config_revisions/default",
                    },
                )
            )

    def test_a_document_merely_under_a_runs_path_is_not_refused(self, resolver):
        """The run rule protects the manifest, not every key beneath `runs/`.

        A document key can legitimately contain `runs` as a path segment, and
        refusing those would break uploads for the sake of a key nothing writes.
        """
        index, _ = resolver

        result = index.handler(
            _event(
                "uploadDocument",
                {"fileName": "report.pdf", "prefix": "2026/runs/january"},
            )
        )

        assert result["objectKey"] == "2026/runs/january/report.pdf"

    def test_a_sample_copy_to_an_ordinary_prefix_still_succeeds(self, resolver):
        index, _ = resolver

        result = index.handler(
            _event(
                "uploadSampleDocument",
                {"sampleId": "bank-statement-multipage", "prefix": "inbox"},
            )
        )

        assert result["success"] is True
        assert result["objectKeys"] == ["inbox/bank-statement-multipage.pdf"]


@pytest.mark.unit
class TestTheAllowListFailsClosed:
    def test_an_unconfigured_allow_list_refuses_every_upload(self, monkeypatch):
        for name in (
            "INPUT_BUCKET",
            "OUTPUT_BUCKET",
            "CONFIGURATION_BUCKET",
            "EVALUATION_BASELINE_BUCKET",
            "REPORTING_BUCKET",
            "TEST_SET_BUCKET",
            "DISCOVERY_BUCKET",
            "WORKING_BUCKET",
        ):
            monkeypatch.delenv(name, raising=False)
        with mock_aws():
            import index

            importlib.reload(index)
            assert index.ALLOWED_BUCKETS == set()

            with pytest.raises(PermissionError, match="not configured"):
                index.handler(
                    _event("uploadDocument", {"fileName": "x.pdf", "bucket": "b"})
                )


@pytest.mark.unit
class TestTheDestinationsConfigurationScopeIsEnforced:
    """A scoped caller may not put a document into a profile outside their scope.

    This closes a gap that predates prefix mappings. `uploadDocument` checked the
    caller's Cognito GROUP and the target BUCKET, but never their
    `allowedConfigVersions` -- so a scoped Author could already name any profile in
    the deployment via the `version` argument, and a document's profile is the
    document-visibility partition for every scoped user.

    Prefix mappings add a second route to the same gap, where the DESTINATION
    chooses the profile with no metadata involved. Both are covered here, because
    the check is on the RESOLVED profile rather than on the requested one -- which
    is the only form that can cover both.
    """

    @staticmethod
    def _scope(index, *versions, email="user@example.com"):
        boto3.resource("dynamodb", region_name="us-east-1").Table(USERS_TABLE).put_item(
            Item={
                "PK": "USER#u1",
                "SK": "USER#u1",
                "email": email,
                "allowedConfigVersions": list(versions),
            }
        )
        # The lookup caches successful answers per container; a test that seeds a
        # row after a previous lookup would otherwise read the stale one.
        index._user_scope_cache.clear()

    @staticmethod
    def _activate(profile):
        """Make `profile` this deployment's active Configuration Profile.

        Must be called with the `resolver` fixture in scope, so the writes land in
        moto rather than in a real account.
        """
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(CONFIG_TABLE)
        table.put_item(Item={"Configuration": f"Config#{profile}"})
        table.put_item(
            Item={"Configuration": "Config#__active", "ActiveVersion": profile}
        )

    @staticmethod
    def _mapping(index, prefix, profile, **kwargs):
        """Write a mapping, and the profile head item it names.

        The head item is not decoration. This resolver injects `profile_exists`, so
        a mapping naming a profile that is not in the table resolves as *stale* and
        falls through rather than applying — which is the correct behaviour and
        makes a fixture that omits the profile test the opposite of what it says.
        """
        from idp_common.config.prefix_mappings import PrefixMappingStore

        table = boto3.resource("dynamodb", region_name="us-east-1").Table(CONFIG_TABLE)
        table.put_item(Item={"Configuration": f"Config#{profile}"})
        PrefixMappingStore(table).put(prefix, profile, **kwargs)

    def test_the_metadata_route_is_refused_out_of_scope(self, resolver):
        index, _ = resolver
        self._scope(index, "teamA")

        with pytest.raises(PermissionError) as excinfo:
            index.handler(
                _event(
                    "uploadDocument",
                    {"fileName": "x.pdf", "version": "finance-prod"},
                )
            )

        assert str(excinfo.value).startswith("Unauthorized")

    def test_the_prefix_route_is_refused_out_of_scope(self, resolver):
        """The route a mapping adds: no `version` argument at all."""
        index, _ = resolver
        self._scope(index, "teamA")
        self._mapping(index, "finance/", "finance-prod")

        with pytest.raises(PermissionError) as excinfo:
            index.handler(
                _event("uploadDocument", {"fileName": "x.pdf", "prefix": "finance"})
            )

        assert str(excinfo.value).startswith("Unauthorized")

    def test_a_refusal_does_not_name_the_profile_it_refused(self, resolver):
        """A 403 that reports which profile it refused is an enumeration oracle, and
        profile names are themselves access-controlled."""
        index, _ = resolver
        self._scope(index, "teamA")
        self._mapping(index, "finance/", "finance-prod")

        with pytest.raises(PermissionError) as excinfo:
            index.handler(
                _event("uploadDocument", {"fileName": "x.pdf", "prefix": "finance"})
            )

        assert "finance-prod" not in str(excinfo.value)
        assert "teamA" not in str(excinfo.value)

    def test_an_in_scope_destination_is_allowed(self, resolver):
        index, _ = resolver
        self._scope(index, "teamA-*")
        self._mapping(index, "teamA/", "teamA-prod")

        result = index.handler(
            _event("uploadDocument", {"fileName": "x.pdf", "prefix": "teamA"})
        )

        assert result["objectKey"] == "teamA/x.pdf"

    def test_an_unscoped_caller_is_unrestricted(self, resolver):
        """Scoping is opt-in per user; no row means no restriction, and this must
        stay true or the feature locks every ordinary user out of uploading."""
        index, _ = resolver
        self._mapping(index, "finance/", "finance-prod")

        result = index.handler(
            _event("uploadDocument", {"fileName": "x.pdf", "prefix": "finance"})
        )

        assert result["objectKey"] == "finance/x.pdf"

    def test_a_sample_copy_is_refused_out_of_scope_too(self, resolver):
        """uploadSampleDocument is the second caller-chosen-prefix writer into the
        Input bucket, so it needs the identical check."""
        index, _ = resolver
        self._scope(index, "teamA")
        self._mapping(index, "finance/", "finance-prod")

        with pytest.raises(PermissionError):
            index.handler(
                _event(
                    "uploadSampleDocument",
                    {"sampleId": "bank-statement-multipage", "prefix": "finance"},
                )
            )

    def test_an_identity_that_cannot_be_scoped_is_refused(self, resolver):
        """Fail closed: "cannot evaluate" is not "unrestricted"."""
        index, _ = resolver
        event = _event("uploadDocument", {"fileName": "x.pdf"})
        event["identity"]["claims"].pop("email")

        with pytest.raises(PermissionError):
            index.handler(event)

    def test_a_reject_mapping_refuses_before_minting_a_url(self, resolver):
        """The upload would fail at ingest, so refusing here is strictly better than
        handing out a URL for an upload that cannot succeed."""
        index, _ = resolver
        self._mapping(
            index, "regulated/", "regulated-profile", metadata_precedence="reject"
        )

        with pytest.raises(ValueError) as excinfo:
            index.handler(
                _event(
                    "uploadDocument",
                    {
                        "fileName": "x.pdf",
                        "prefix": "regulated",
                        "version": "something-else",
                    },
                )
            )

        assert "Refused" in str(excinfo.value)

    def test_a_reject_mapping_allows_an_agreeing_upload(self, resolver):
        index, _ = resolver
        self._mapping(
            index, "regulated/", "regulated-profile", metadata_precedence="reject"
        )

        result = index.handler(
            _event(
                "uploadDocument",
                {
                    "fileName": "x.pdf",
                    "prefix": "regulated",
                    "version": "regulated-profile",
                },
            )
        )

        assert result["objectKey"] == "regulated/x.pdf"

    def test_the_requested_version_is_still_what_gets_stamped(self, resolver):
        """Deliberate: the metadata records what was REQUESTED and the tracking row
        records what happened and why. Rewriting it here would make the two agree at
        ingest, so a mapping that overrode a user's selection would stop counting as
        a conflict -- losing the metric and the UI badge on exactly the case an
        operator wants to see."""
        index, _ = resolver
        self._mapping(index, "acme/", "mapped-profile")

        result = index.handler(
            _event(
                "uploadDocument",
                {"fileName": "x.pdf", "prefix": "acme", "version": "chosen"},
            )
        )

        presigned = json.loads(result["presignedUrl"])
        assert presigned["fields"]["x-amz-meta-config-version"] == "chosen"

    def test_a_leading_slash_cannot_bypass_a_reject_mapping(self, resolver):
        """S3 accepts '/regulated/x.pdf' as a key DISTINCT from 'regulated/x.pdf',
        and only the latter matches a mapping on 'regulated/'. Without
        canonicalization that is a one-character bypass."""
        index, _ = resolver
        self._mapping(
            index, "regulated/", "regulated-profile", metadata_precedence="reject"
        )

        with pytest.raises(ValueError):
            index.handler(
                _event(
                    "uploadDocument",
                    {
                        "fileName": "x.pdf",
                        "prefix": "/regulated",
                        "version": "something-else",
                    },
                )
            )

    def test_a_non_input_bucket_write_is_not_scope_checked(self, resolver):
        """Ground-truth baselines, page images and exports are not documents and no
        mapping governs them, so the check would be meaningless there -- and would
        break the Test Studio ground-truth editor for every scoped user."""
        index, _ = resolver
        self._scope(index, "teamA")

        result = index.handler(
            _event(
                "uploadDocument",
                {
                    "fileName": "result.json",
                    "prefix": "ts/labels",
                    "bucket": CONFIG_BUCKET,
                    "version": "finance-prod",
                },
            )
        )

        assert result["objectKey"] == "ts/labels/result.json"

    def test_an_unreadable_mapping_table_does_not_block_uploads(self, resolver):
        """Fails open on the ROUTING read while the SCOPE read fails closed. A
        mapping that cannot be read means "unmapped", which is the state the
        deployment was in before the feature; a scope that cannot be read means the
        caller cannot be placed."""
        index, _ = resolver

        class Boom:
            def Table(self, _name):
                raise RuntimeError("throttled")

        original = index._dynamodb
        try:
            index._prefix_mappings.__globals__["_dynamodb"] = Boom()
            assert index._prefix_mappings() == []
        finally:
            index._prefix_mappings.__globals__["_dynamodb"] = original

    def test_the_active_profile_route_is_scope_checked(self, resolver):
        """The widest upload shape there is: no `version`, no mapping match.

        This was allowed because the resolver was not given `active_profile`, so the
        resolved profile was None and a None profile skips the scope guard. The
        route the control exists for was the one it did not cover.
        """
        index, _ = resolver
        self._activate("finance-prod")
        self._scope(index, "teamA")

        with pytest.raises(PermissionError) as excinfo:
            index.handler(_event("uploadDocument", {"fileName": "x.pdf"}))

        assert str(excinfo.value).startswith("Unauthorized")
        assert "finance-prod" not in str(excinfo.value)

    def test_the_active_profile_route_allows_an_in_scope_caller(self, resolver):
        index, _ = resolver
        self._activate("teamA-prod")
        self._scope(index, "teamA-*")

        result = index.handler(_event("uploadDocument", {"fileName": "x.pdf"}))

        assert result["objectKey"] == "x.pdf"

    def test_a_pinned_mapping_agrees_with_an_unpinned_request(self, resolver):
        """The preview, the upload and ingest must give the same verdict.

        A `reject` mapping pinned to r7 against a request that names the profile but
        no revision: without `published_revision` injected here, the request's
        effective revision is None, "disagrees" with 7, and the upload is refused —
        a 400 on a legitimate upload that the preview said was fine and that ingest
        would have accepted.
        """
        index, _ = resolver
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(CONFIG_TABLE)
        table.put_item(
            Item={"Configuration": "Config#reg", "PublishedRevision": 7}
        )
        from idp_common.config.prefix_mappings import PrefixMappingStore

        PrefixMappingStore(table).put(
            "regulated/", "reg", config_revision=7, metadata_precedence="reject"
        )

        result = index.handler(
            _event(
                "uploadDocument",
                {"fileName": "x.pdf", "prefix": "regulated", "version": "reg"},
            )
        )

        assert result["objectKey"] == "regulated/x.pdf"
