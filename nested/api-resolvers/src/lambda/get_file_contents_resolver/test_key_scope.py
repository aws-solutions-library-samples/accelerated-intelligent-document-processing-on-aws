# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The object-read path enforces both per-user scope axes on the KEY.

``test_index.py`` covers the bucket allow-list: which bucket a caller-supplied
``s3Uri`` may name. That is a control over *deployments*, not over *users*, and two of
the allow-listed buckets are partitioned per user:

* the Configuration bucket, by configuration profile —
  ``config_revisions/<profile>/<nnnnnn>.json.gz`` is one profile's recorded
  configuration, prompts and few-shot examples included, and which profiles a caller
  may see is their ``allowedConfigVersions``;
* the Test Set bucket, which is ``<test_set_id>/…`` throughout — source documents,
  ground-truth bodies and published baseline snapshots — and which test sets an
  Annotator may see is their ``allowedTestSets``.

A bucket-only check reads, for those two, as "a caller who may read some of this
bucket may read all of it", which is the negation of both axes for exactly the callers
the axes exist to restrict. These tests are the out-of-scope-key case.

Both resolver fields are exercised for every axis. ``getFilePresignedUrl`` is the one
that matters most: it mints a URL carrying its own authorization for the life of the
signature, so it is the operation least able to rely on a later check.

**The UsersTable here is real** (moto, with the ``EmailIndex`` GSI the grants name),
not a double. The two axes reach it by different code — ``config_scope`` through the
``SUB#``/``EmailIndex`` pair, ``testset_scope`` through its own ``EmailIndex`` query —
and a double would let either read a row shape the real table never produces. The
region is reinstated inside the fixture because the hermetic CI wrapper strips it and
``boto3.resource("dynamodb")`` needs one even under moto.
"""

from __future__ import annotations

import importlib

import boto3
import pytest
from moto import mock_aws

OUTPUT_BUCKET = "output-bucket"
CONFIG_BUCKET = "configuration-bucket"
TEST_SET_BUCKET = "test-set-bucket"
USERS_TABLE = "IDP-UsersTable"

# Two configuration profiles. The caller is scoped to the first.
MINE = "lending"
THEIRS = "claims"

# Two test sets. The Annotator is assigned to the first.
MY_SET = "ts-mine"
THEIR_SET = "ts-theirs"


def _revision_key(profile: str, revision: int = 1) -> str:
    """The same layout `ConfigRevisionStore.body_key` writes."""
    return f"config_revisions/{profile}/{revision:06d}.json.gz"


# The two shapes of test-set object a scoped Annotator must not reach across sets: the
# source document, and the ground truth it would be scored against.
def _test_set_keys(test_set_id: str) -> list:
    return [
        f"{test_set_id}/input/statement.pdf",
        f"{test_set_id}/baseline/statement.pdf/sections/1/result.json",
        f"{test_set_id}/versions/3/baseline/statement.pdf/sections/1/result.json",
    ]


BOTH_FIELDS = ["getFileContents", "getFilePresignedUrl"]


def _event(
    field, bucket, key, *, groups=("Author",), email="user@example.test", sub=""
):
    claims = {"cognito:groups": list(groups)}
    if email is not None:
        claims["email"] = email
    if sub:
        claims["sub"] = sub
    return {
        "info": {"fieldName": field},
        "arguments": {"s3Uri": f"s3://{bucket}/{key}"},
        "identity": {"claims": claims},
    }


@pytest.fixture
def resolver(monkeypatch):
    """The resolver, its two scope modules and a real UsersTable, all under moto."""
    monkeypatch.setenv("OUTPUT_BUCKET", OUTPUT_BUCKET)
    monkeypatch.setenv("CONFIGURATION_BUCKET", CONFIG_BUCKET)
    monkeypatch.setenv("TEST_SET_BUCKET", TEST_SET_BUCKET)
    monkeypatch.setenv("USERS_TABLE_NAME", USERS_TABLE)
    # Reinstated deliberately: `make/hermetic_aws.mk` unsets every region variable, and
    # the lazily-built DynamoDB resource needs one even when moto is answering.
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        for bucket in (OUTPUT_BUCKET, CONFIG_BUCKET, TEST_SET_BUCKET):
            s3.create_bucket(Bucket=bucket)

        # Both profiles' revision bodies exist, so a refusal is never the object
        # merely being absent — the distinction the non-disclosure tests rest on.
        for profile in (MINE, THEIRS):
            s3.put_object(
                Bucket=CONFIG_BUCKET,
                Key=_revision_key(profile),
                Body=b'{"classes": {}}',
                ContentType="application/json",
            )
        # Unpartitioned Configuration-bucket content, which no scope axis governs.
        s3.put_object(
            Bucket=CONFIG_BUCKET,
            Key="config_library/unified/default/config.yaml",
            Body=b"classes: {}\n",
            ContentType="text/plain",
        )
        s3.put_object(
            Bucket=CONFIG_BUCKET,
            Key="samples/lending_package.pdf",
            Body=b"%PDF-1.4 fake",
            ContentType="application/pdf",
        )
        for test_set_id in (MY_SET, THEIR_SET):
            for key in _test_set_keys(test_set_id):
                s3.put_object(
                    Bucket=TEST_SET_BUCKET,
                    Key=key,
                    Body=b'{"sections": []}',
                    ContentType="application/json",
                )
        s3.put_object(
            Bucket=OUTPUT_BUCKET,
            Key="doc/sections/1/result.json",
            Body=b'{"hello": "world"}',
            ContentType="application/json",
        )

        ddb = boto3.client("dynamodb", region_name="us-east-1")
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

        import index
        import key_scope
        import testset_scope

        # Reloaded inside the mock so the module-level S3 client, ALLOWED_BUCKETS and
        # the two partitioned-bucket names are all built against this environment.
        importlib.reload(key_scope)
        importlib.reload(index)
        # Both scope modules cache per container. A cached answer from a previous test
        # would make the next one pass or fail for the wrong reason, and `key_scope`'s
        # lazily-built resource would point at a torn-down mock.
        key_scope._dynamodb = None
        key_scope._user_scope_cache.clear()
        testset_scope.clear_scope_cache()

        yield (
            index,
            boto3.resource("dynamodb", region_name="us-east-1").Table(USERS_TABLE),
        )

        key_scope._dynamodb = None
        key_scope._user_scope_cache.clear()
        testset_scope.clear_scope_cache()


def _put_user(table, email, **attributes):
    """A UsersTable row for ``email``, in the real key shape `config_scope` builds."""
    item = {"PK": f"USER#{email}", "SK": f"USER#{email}", "email": email}
    item.update(attributes)
    table.put_item(Item=item)


# ---------------------------------------------------------------------------
# The configuration-profile axis
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConfigurationRevisionKeyScope:
    @pytest.mark.parametrize("field", BOTH_FIELDS)
    def test_a_scoped_author_cannot_read_another_profiles_revision(
        self, resolver, field, monkeypatch
    ):
        """The finding: one Author confined to `lending` reading `claims`' history.

        The body at that key is the whole configuration that revision recorded — every
        prompt and every few-shot example — so this is the content the scope exists to
        withhold, not an incidental byproduct of it.
        """
        index, table = resolver
        _put_user(table, "user@example.test", allowedConfigVersions=[MINE])

        with pytest.raises(PermissionError, match="^Unauthorized"):
            index.handler(_event(field, CONFIG_BUCKET, _revision_key(THEIRS)), None)

    @pytest.mark.parametrize("field", BOTH_FIELDS)
    def test_a_scoped_author_can_still_read_their_own_profiles_revision(
        self, resolver, field
    ):
        """The control that proves the refusal above is about scope, not the prefix."""
        index, table = resolver
        _put_user(table, "user@example.test", allowedConfigVersions=[MINE])

        result = index.handler(_event(field, CONFIG_BUCKET, _revision_key(MINE)), None)
        assert result["size"] == len(b'{"classes": {}}')

    @pytest.mark.parametrize("field", BOTH_FIELDS)
    def test_an_unscoped_caller_is_unrestricted(self, resolver, field):
        """An empty page means "no restriction", deliberately (SCOPE4).

        Scoping is opt-in per user and most users have no row at all. Denying on an
        empty page would lock every ordinary user out of the configuration UI, which
        is why this is the one branch where an absence means allow — and it is an
        *answer* from the table, not a failure to get one.
        """
        index, _ = resolver
        result = index.handler(
            _event(field, CONFIG_BUCKET, _revision_key(THEIRS)), None
        )
        assert result["size"] == len(b'{"classes": {}}')

    def test_a_glob_scope_entry_admits_the_profiles_it_matches(self, resolver):
        """Deployments encode profile lineage in the name, so globs are the rule."""
        index, table = resolver
        _put_user(table, "user@example.test", allowedConfigVersions=["lend*"])

        assert index.handler(
            _event("getFileContents", CONFIG_BUCKET, _revision_key(MINE)), None
        )
        with pytest.raises(PermissionError):
            index.handler(
                _event("getFileContents", CONFIG_BUCKET, _revision_key(THEIRS)), None
            )

    def test_an_admin_is_never_profile_scoped(self, resolver):
        """Mirrors the configuration resolver, which resolves no scope for an Admin.

        The two must agree: a profile the metadata path serves and the byte path
        refuses is one rule enforced two different ways, which is the divergence
        `config_scope` exists to prevent.
        """
        index, table = resolver
        _put_user(table, "admin@example.test", allowedConfigVersions=[MINE])

        assert index.handler(
            _event(
                "getFileContents",
                CONFIG_BUCKET,
                _revision_key(THEIRS),
                groups=("Admin",),
                email="admin@example.test",
            ),
            None,
        )

    @pytest.mark.parametrize(
        "key",
        [
            "config_library/unified/default/config.yaml",
            "samples/lending_package.pdf",
        ],
    )
    def test_unpartitioned_configuration_keys_are_not_scoped(self, resolver, key):
        """Only `config_revisions/` is partitioned by profile.

        The bundled config library and the sample documents are the same bytes for
        every caller. Refusing them to a scoped Author would break the profile
        *creation* flow for the very users most likely to be scoped, so the bucket
        allow-list remains the only control there — unchanged.
        """
        index, table = resolver
        _put_user(table, "user@example.test", allowedConfigVersions=[MINE])

        assert index.handler(_event("getFileContents", CONFIG_BUCKET, key), None)


@pytest.mark.unit
class TestConfigurationScopeFailsClosed:
    """ "Cannot evaluate" must never be read as "unrestricted" — AUTH.T07."""

    def test_a_users_table_failure_denies(self, resolver, monkeypatch):
        """A missing IAM grant or a throttle refuses; it does not lift the scope.

        The cost of denying is one failed request for one caller. The cost of the
        other direction is every profile's full configuration history served to a
        caller entitled to a subset, on a DynamoDB blip and with no trace.
        """
        index, table = resolver
        _put_user(table, "user@example.test", allowedConfigVersions=[MINE])
        import key_scope

        class _Boom:
            def Table(self, _name):
                raise Exception("AccessDeniedException: dynamodb:Query")

        monkeypatch.setattr(key_scope, "_dynamodb", _Boom())

        with pytest.raises(PermissionError, match="^Unauthorized"):
            index.handler(
                _event("getFileContents", CONFIG_BUCKET, _revision_key(MINE)), None
            )

    def test_an_unwired_users_table_denies(self, resolver, monkeypatch):
        """The parent template wires it unconditionally, so empty means drift."""
        index, _ = resolver
        monkeypatch.delenv("USERS_TABLE_NAME", raising=False)

        with pytest.raises(PermissionError, match="^Unauthorized"):
            index.handler(
                _event("getFileContents", CONFIG_BUCKET, _revision_key(MINE)), None
            )

    def test_a_caller_with_neither_email_nor_sub_denies(self, resolver):
        """No key to look the caller up by, so no answer about them is available.

        Nothing is substituted for the email claim: every other identifier a claims
        set may carry is not an email address for all callers, and an email-keyed
        index queried with one matches nothing — which reads as "unrestricted".
        """
        index, _ = resolver
        event = _event(
            "getFileContents", CONFIG_BUCKET, _revision_key(THEIRS), email=None
        )
        event["identity"]["claims"]["cognito:username"] = "someone"

        with pytest.raises(PermissionError, match="^Unauthorized"):
            index.handler(event, None)

    def test_a_missing_identity_denies_rather_than_being_trusted(self, resolver):
        """An absence is not a service-to-service marker on this path.

        Only `http_api_dispatcher` invokes this function, and it always builds the
        identity from the authorizer's verified claims. Reading an absent identity as
        trusted would make every check here removable by dropping a key.
        """
        index, _ = resolver
        event = _event("getFileContents", CONFIG_BUCKET, _revision_key(THEIRS))
        event["identity"] = None

        with pytest.raises(PermissionError, match="^Unauthorized"):
            index.handler(event, None)


# ---------------------------------------------------------------------------
# The test-set axis
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTestSetKeyScope:
    @pytest.mark.parametrize("field", BOTH_FIELDS)
    @pytest.mark.parametrize("key", _test_set_keys(THEIR_SET))
    def test_a_scoped_annotator_cannot_read_another_test_sets_objects(
        self, resolver, field, key
    ):
        """Source documents AND ground truth, across all three key shapes.

        Annotators are routinely external contractors onboarded to label one test set,
        so this axis is the one most likely to span an organizational boundary.
        """
        index, table = resolver
        _put_user(table, "annotator@example.test", allowedTestSets=[MY_SET])

        with pytest.raises(PermissionError, match="^Unauthorized"):
            index.handler(
                _event(
                    field,
                    TEST_SET_BUCKET,
                    key,
                    groups=("Annotator",),
                    email="annotator@example.test",
                ),
                None,
            )

    @pytest.mark.parametrize("key", _test_set_keys(MY_SET))
    def test_a_scoped_annotator_can_read_their_own_test_sets_objects(
        self, resolver, key
    ):
        index, table = resolver
        _put_user(table, "annotator@example.test", allowedTestSets=[MY_SET])

        assert index.handler(
            _event(
                "getFileContents",
                TEST_SET_BUCKET,
                key,
                groups=("Annotator",),
                email="annotator@example.test",
            ),
            None,
        )

    def test_an_annotator_with_no_scope_is_denied_rather_than_unrestricted(
        self, resolver
    ):
        """A half-created annotator fails closed — `assert_can_access_test_set`'s rule.

        Note this is the opposite default from the configuration axis, and both are
        deliberate: an Annotator's whole access is their assignment, whereas
        configuration scoping is an optional restriction on an otherwise-entitled user.
        """
        index, _ = resolver

        with pytest.raises(PermissionError, match="^Unauthorized"):
            index.handler(
                _event(
                    "getFileContents",
                    TEST_SET_BUCKET,
                    _test_set_keys(MY_SET)[0],
                    groups=("Annotator",),
                    email="annotator@example.test",
                ),
                None,
            )

    @pytest.mark.parametrize("group", ["Admin", "Author"])
    def test_admin_and_author_are_never_test_set_scoped(self, resolver, group):
        """They own the test sets; scoping them would break test-set management."""
        index, _ = resolver

        assert index.handler(
            _event(
                "getFileContents",
                TEST_SET_BUCKET,
                _test_set_keys(THEIR_SET)[0],
                groups=(group,),
            ),
            None,
        )

    @pytest.mark.parametrize("group", ["Viewer", "Reviewer"])
    def test_roles_with_no_test_set_route_are_refused(self, resolver, group):
        """Consistent with the metadata path rather than stricter than it.

        `getTestSetDocuments` is `[Admin, Author, Annotator]`, so a Viewer or Reviewer
        has no way to discover a Test-Set-bucket key in the first place; serving the
        bytes to them would be the only route they had. Production HITL review is a
        different axis and grants nothing here.
        """
        index, _ = resolver

        with pytest.raises(PermissionError, match="^Unauthorized"):
            index.handler(
                _event(
                    "getFileContents",
                    TEST_SET_BUCKET,
                    _test_set_keys(MY_SET)[0],
                    groups=(group,),
                ),
                None,
            )


# ---------------------------------------------------------------------------
# What a refusal reveals, and what it touches
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScopeRefusalsDiscloseNothing:
    """Closing a read must not open an oracle in its place.

    `test_index.py` already pins this for the bucket allow-list. A scope refusal needs
    it more, not less: the caller supplied the key, so a message that named the
    profile or test set it declined — or that distinguished "out of scope" from "not
    there" — would turn the refusal into a profile- and test-set-enumeration oracle
    for exactly the caller being restricted.
    """

    @pytest.mark.parametrize("field", BOTH_FIELDS)
    def test_a_config_refusal_names_neither_the_profile_nor_the_object(
        self, resolver, field
    ):
        index, table = resolver
        _put_user(table, "user@example.test", allowedConfigVersions=[MINE])

        with pytest.raises(PermissionError) as excinfo:
            index.handler(_event(field, CONFIG_BUCKET, _revision_key(THEIRS)), None)

        message = str(excinfo.value)
        assert message.startswith("Unauthorized")
        for secret in (THEIRS, MINE, CONFIG_BUCKET, "config_revisions", ".json.gz"):
            assert secret not in message, secret

    @pytest.mark.parametrize("field", BOTH_FIELDS)
    def test_a_test_set_refusal_names_neither_the_set_nor_the_object(
        self, resolver, field
    ):
        index, table = resolver
        _put_user(table, "annotator@example.test", allowedTestSets=[MY_SET])

        with pytest.raises(PermissionError) as excinfo:
            index.handler(
                _event(
                    field,
                    TEST_SET_BUCKET,
                    _test_set_keys(THEIR_SET)[0],
                    groups=("Annotator",),
                    email="annotator@example.test",
                ),
                None,
            )

        message = str(excinfo.value)
        assert message.startswith("Unauthorized")
        for secret in (THEIR_SET, MY_SET, TEST_SET_BUCKET, "statement.pdf"):
            assert secret not in message, secret

    def test_an_out_of_scope_key_that_exists_and_one_that_does_not_refuse_alike(
        self, resolver
    ):
        """Otherwise the refusal distinguishes them, which is the oracle."""
        index, table = resolver
        _put_user(table, "user@example.test", allowedConfigVersions=[MINE])

        messages = set()
        for key in (_revision_key(THEIRS), _revision_key("no-such-profile", 99)):
            with pytest.raises(PermissionError) as excinfo:
                index.handler(_event("getFileContents", CONFIG_BUCKET, key), None)
            messages.add(str(excinfo.value))

        assert len(messages) == 1, messages

    @pytest.mark.parametrize("field", BOTH_FIELDS)
    def test_an_out_of_scope_object_is_never_read(self, resolver, field, monkeypatch):
        """No S3 call at all, so there is no latency difference to time either."""
        index, table = resolver
        _put_user(table, "user@example.test", allowedConfigVersions=[MINE])

        for operation in ("head_object", "get_object", "generate_presigned_url"):
            monkeypatch.setattr(
                index.s3_client,
                operation,
                lambda **kw: pytest.fail("read an out-of-scope object"),
            )

        with pytest.raises(PermissionError):
            index.handler(_event(field, CONFIG_BUCKET, _revision_key(THEIRS)), None)


# ---------------------------------------------------------------------------
# What this change does NOT scope, stated as a test
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTheRemainingResidual:
    """The document buckets are still bucket-scoped only, and that is the open gap.

    UI.T06's document half is not closed here: the Output and Input buckets are not
    partitioned by any per-user axis this deployment records, so there is nothing for
    a key rule to consult. Closing it needs per-document authorization, which is
    issue #1033 and a change to the document-viewing data path.

    Pinned rather than merely written down, so the residual cannot be mistaken for
    something this path already handles — and so that if per-document scope does
    arrive, this test is what fails and names the place to enforce it.
    """

    @pytest.mark.parametrize("field", BOTH_FIELDS)
    def test_an_output_bucket_key_is_not_key_scoped(self, resolver, field):
        index, table = resolver
        _put_user(table, "user@example.test", allowedConfigVersions=[MINE])

        assert index.handler(
            _event(field, OUTPUT_BUCKET, "doc/sections/1/result.json"), None
        )

    def test_the_document_buckets_are_scope_bearing_for_no_key_shape(self, resolver):
        """Which buckets carry a scope question, derived from the code not restated.

        The three key shapes below are every shape this resolver is asked for. The
        Output and Input buckets answer `None` to all of them — that is the residual,
        stated as the absence it actually is. If a document axis is ever added, this
        is what fails, and it names the buckets that need it.
        """
        import key_scope

        shapes = (
            _revision_key(MINE),
            _test_set_keys(MY_SET)[0],
            "doc/sections/1/result.json",
        )
        scoped = {
            (bucket, key)
            for bucket in (OUTPUT_BUCKET, CONFIG_BUCKET, TEST_SET_BUCKET)
            for key in shapes
            if key_scope.scope_subject(
                bucket,
                key,
                configuration_bucket=CONFIG_BUCKET,
                test_set_bucket=TEST_SET_BUCKET,
            )
            is not None
        }

        assert not any(bucket == OUTPUT_BUCKET for bucket, _ in scoped), (
            "the Output bucket is not partitioned by any per-user axis, so no key in "
            "it carries a scope question — this is UI.T06's unclosed document half"
        )
        # The Configuration bucket is scope-bearing for exactly its revision prefix;
        # the Test Set bucket is scope-bearing for every key, since every key names a
        # test set in its first segment.
        assert scoped == {
            (CONFIG_BUCKET, _revision_key(MINE)),
            *((TEST_SET_BUCKET, key) for key in shapes),
        }, sorted(scoped)


@pytest.mark.unit
class TestScopeSubjectDerivation:
    """The key -> scope-subject mapping, directly.

    Worth testing apart from the handler because a derivation that returns `None` is
    indistinguishable at the handler from a caller who is in scope: both succeed.
    """

    def test_an_unset_bucket_name_matches_nothing(self):
        """Otherwise an unwired variable would make every bucket the config bucket."""
        import key_scope

        assert (
            key_scope.scope_subject(
                "", _revision_key(MINE), configuration_bucket="", test_set_bucket=""
            )
            is None
        )
        assert (
            key_scope.scope_subject(
                CONFIG_BUCKET,
                _revision_key(MINE),
                configuration_bucket="",
                test_set_bucket="",
            )
            is None
        )

    def test_the_profile_is_the_second_segment_and_the_test_set_the_first(self):
        import key_scope

        assert key_scope.scope_subject(
            CONFIG_BUCKET,
            _revision_key(THEIRS, 412),
            configuration_bucket=CONFIG_BUCKET,
            test_set_bucket=TEST_SET_BUCKET,
        ) == (key_scope.CONFIG_PROFILE, THEIRS)
        assert key_scope.scope_subject(
            TEST_SET_BUCKET,
            f"{THEIR_SET}/baseline/a.pdf/sections/1/result.json",
            configuration_bucket=CONFIG_BUCKET,
            test_set_bucket=TEST_SET_BUCKET,
        ) == (key_scope.TEST_SET, THEIR_SET)

    @pytest.mark.parametrize(
        "key",
        [
            # Anchored: the prefix must start the key, not appear in it. This is a
            # genuinely different, canonical prefix in the same bucket.
            "backup/config_revisions/claims/000001.json.gz",
            # A near-miss prefix is a different prefix — and a real one, since
            # `config_library/` and `samples/` live here too.
            "config_revisions_backup/claims/000001.json.gz",
        ],
    )
    def test_a_canonical_key_outside_the_revision_prefix_names_no_profile(self, key):
        import key_scope

        assert (
            key_scope.scope_subject(
                CONFIG_BUCKET,
                key,
                configuration_bucket=CONFIG_BUCKET,
                test_set_bucket=TEST_SET_BUCKET,
            )
            is None
        )

    def test_a_test_set_key_with_no_segment_is_a_bad_request_not_a_denial(self):
        """It names no test set and no object, so there is no scope question.

        `ValueError` (400) rather than `PermissionError` (403): the bucket stores
        nothing at its root, so saying the argument is malformed discloses nothing.
        """
        import key_scope

        with pytest.raises(ValueError):
            key_scope.scope_subject(
                TEST_SET_BUCKET,
                "statement.pdf",
                configuration_bucket=CONFIG_BUCKET,
                test_set_bucket=TEST_SET_BUCKET,
            )


# Keys that name the same profile or test set as a canonical key would, while failing
# to match the anchored prefixes — so "no subject, therefore unpartitioned" would let
# them through unchecked.
_NON_CANONICAL_CONFIG_KEYS = [
    # The empty segment `[^/]+` cannot match.
    "config_revisions//claims/000001.json.gz",
    # A leading slash defeats the `^` anchor.
    "/config_revisions/claims/000001.json.gz",
    "//config_revisions/claims/000001.json.gz",
    # Relative-path segments.
    "./config_revisions/claims/000001.json.gz",
    "config_revisions/./claims/000001.json.gz",
    "config_revisions/../config_revisions/claims/000001.json.gz",
    # The prefix with nothing after it — an empty trailing segment.
    "config_revisions/",
    # Case variants. S3 keys are case-sensitive so these name no revision body, but
    # they are not keys any writer here produces either.
    "CONFIG_REVISIONS/claims/000001.json.gz",
    "Config_Revisions/claims/000001.json.gz",
]

_NON_CANONICAL_TEST_SET_KEYS = [
    "/ts-theirs/input/statement.pdf",
    "//ts-theirs/input/statement.pdf",
    "./ts-theirs/input/statement.pdf",
    "ts-theirs//input/statement.pdf",
    "ts-theirs/../ts-theirs/input/statement.pdf",
]


@pytest.mark.unit
class TestNonCanonicalKeysAreRejectedNotTreatedAsUnpartitioned:
    """A key that misses the prefix must not thereby escape the check.

    Each spelling below names the same profile or test set a canonical key would, and
    each fails the anchored prefix match. Returning "not scope-bearing" for them makes
    the correctness of the whole check depend on S3 treating keys as opaque bytes
    *everywhere downstream* — including in whatever client consumes a presigned URL,
    which this deployment does not control. A normalising intermediary would convert one
    of these into a real bypass with no signal, because the resolver would have logged
    an ordinary 400.

    So they are refused here, before the subject is decided, and the tests assert the
    refusal rather than the 404 that happens to follow today.
    """

    @pytest.mark.parametrize("key", _NON_CANONICAL_CONFIG_KEYS)
    def test_a_non_canonical_configuration_key_is_refused(self, key):
        import key_scope

        with pytest.raises(ValueError, match="canonical|required"):
            key_scope.scope_subject(
                CONFIG_BUCKET,
                key,
                configuration_bucket=CONFIG_BUCKET,
                test_set_bucket=TEST_SET_BUCKET,
            )

    @pytest.mark.parametrize("key", _NON_CANONICAL_TEST_SET_KEYS)
    def test_a_non_canonical_test_set_key_is_refused(self, key):
        import key_scope

        with pytest.raises(ValueError, match="canonical|required"):
            key_scope.scope_subject(
                TEST_SET_BUCKET,
                key,
                configuration_bucket=CONFIG_BUCKET,
                test_set_bucket=TEST_SET_BUCKET,
            )

    @pytest.mark.parametrize(
        "profile_segment",
        [
            # A percent sequence a proxy might decode into a path separator. A profile
            # name can never contain `%`, so this is not a name the store wrote.
            "cl%2faims",
            "claims%2f..",
            # Glob metacharacters. `scope_allows` matches entries with `fnmatchcase`,
            # so a segment carrying them is not a profile name being compared.
            "*",
            "?laims",
            "[abc]",
            # A space, which `_SAFE_PROFILE_RE` also excludes.
            "my claims",
        ],
    )
    def test_a_profile_segment_outside_the_writers_character_class_is_refused(
        self, profile_segment
    ):
        import key_scope

        with pytest.raises(ValueError, match="canonical"):
            key_scope.scope_subject(
                CONFIG_BUCKET,
                f"config_revisions/{profile_segment}/000001.json.gz",
                configuration_bucket=CONFIG_BUCKET,
                test_set_bucket=TEST_SET_BUCKET,
            )

    def test_every_character_the_revision_store_can_write_is_still_accepted(self):
        """The check must not refuse a profile name the product can actually create.

        `ConfigRevisionStore._safe_profile` permits letters, digits, dot, dash and
        underscore; a semver-style preset name uses three of those.
        """
        import key_scope

        for profile in ("default", "lending-2", "usecase_A.v1", "sample-v0.1.6"):
            assert key_scope.scope_subject(
                CONFIG_BUCKET,
                f"config_revisions/{profile}/000007.json.gz",
                configuration_bucket=CONFIG_BUCKET,
                test_set_bucket=TEST_SET_BUCKET,
            ) == (key_scope.CONFIG_PROFILE, profile)

    def test_a_non_canonical_key_in_an_unpartitioned_bucket_is_still_served(self):
        """The canonical-form rule is scoped to the two partitioned buckets.

        Document keys are caller-visible strings from the tracking table and are not
        this function's business; refusing an odd one here would break reads that work
        today for no security benefit, since no scope axis governs that bucket.
        """
        import key_scope

        assert (
            key_scope.scope_subject(
                OUTPUT_BUCKET,
                "//doc/sections/1/result.json",
                configuration_bucket=CONFIG_BUCKET,
                test_set_bucket=TEST_SET_BUCKET,
            )
            is None
        )

    @pytest.mark.parametrize("field", BOTH_FIELDS)
    def test_the_handler_refuses_without_reading_s3(self, resolver, field, monkeypatch):
        """End to end on the shipped bytes: refused before any S3 call.

        The 400 these produced before this rule existed came *from S3* answering
        NoSuchKey, which is exactly the dependency being removed. Proving no S3 call
        happens is what makes the refusal this module's own property.
        """
        index, table = resolver
        _put_user(table, "user@example.test", allowedConfigVersions=[MINE])

        for operation in ("head_object", "get_object", "generate_presigned_url"):
            monkeypatch.setattr(
                index.s3_client,
                operation,
                lambda **kw: pytest.fail("read a non-canonical key"),
            )

        for key in _NON_CANONICAL_CONFIG_KEYS:
            with pytest.raises(ValueError):
                index.handler(_event(field, CONFIG_BUCKET, key), None)
