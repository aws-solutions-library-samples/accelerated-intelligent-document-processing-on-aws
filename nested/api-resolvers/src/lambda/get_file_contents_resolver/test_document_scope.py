# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Per-document authorization on the Input and Output buckets.

``test_key_scope.py`` covers the two buckets partitioned by a **per-user** axis.
This file covers the two document buckets, where the axis is the **document**:
``Document.ConfigVersion`` on the tracking table against the caller's
``allowedConfigVersions`` on the UsersTable.

Why this suite exists at all is worth stating, because the shape it guards against
already shipped once. ``key_scope.scope_subject`` returns a scope subject only for
the Configuration and Test Set buckets, so ``key_scope.assert_key_in_scope`` is a
**no-op on every Input- and Output-bucket key** — which means that moving the
document read path behind this resolver (#1033) bought the group floor and the
bucket allow-list and, on its own, nothing per-document. A reviewer reading only
the call graph would have seen a scope check being called on every key and
concluded the cross-document read was closed. It was not. So the assertions here
are about the check *refusing*, driven through the resolver's own handler against
real moto-backed DynamoDB and S3, rather than about it being called.

Three things each have a test that can fail, and the mutation that fails each is
named in its docstring:

* a **scoped** caller is refused another document's page image, output and source
  bytes — the check that did not exist before;
* every way the check can fail to reach an answer **denies** rather than reading as
  unrestricted: an unidentifiable document, an unstamped document, an unresolvable
  caller, a missing table, a DynamoDB error;
* an **unscoped** caller is deliberately *not* refused, because scoping is opt-in
  per user and ``resolve_allowed_config_versions`` answers "unrestricted" for them.
  That is asserted positively rather than left implicit: it is the honest bound on
  what this change closes, and a future edit that made it a refusal would break
  every ordinary deployment.
"""

from __future__ import annotations

import importlib
import pathlib

import boto3
import pytest
from moto import mock_aws

INPUT_BUCKET = "idp-input-bucket"
OUTPUT_BUCKET = "idp-output-bucket"
CONFIGURATION_BUCKET = "idp-configuration-bucket"
TRACKING_TABLE = "idp-tracking-table"
USERS_TABLE = "idp-users-table"

# Two documents, each stamped with a different configuration profile. `MINE` is the
# one the scoped caller may see.
MINE = "inbox/mine.pdf"
MINE_PROFILE = "lending"
THEIRS = "inbox/theirs.pdf"
THEIRS_PROFILE = "claims"
# A third with no ConfigVersion at all, which a scoped caller must not be able to
# prove is in their scope.
UNSTAMPED = "inbox/unstamped.pdf"

SCOPED_EMAIL = "scoped@example.invalid"
UNSCOPED_EMAIL = "unscoped@example.invalid"
NO_ROW_EMAIL = "nobody@example.invalid"


def _page_key(object_key: str, page: int = 1) -> str:
    return f"{object_key}/pages/{page}/image.jpg"


def _section_key(object_key: str, section: int = 1) -> str:
    return f"{object_key}/sections/{section}/result.json"


def _event(field, s3_uris, *, email, groups=("Viewer",), identity=True):
    """A dispatcher-shaped event for one of the three fields.

    ``s3_uris`` may be a single URI or a list; a list selects the batch field's
    argument shape. The identity is the shape ``http_api_dispatcher`` builds from
    the authorizer's verified claims — nothing in a request body reaches it.
    """
    if isinstance(s3_uris, str):
        arguments = {"s3Uri": s3_uris}
    else:
        arguments = {"files": [{"s3Uri": uri} for uri in s3_uris]}
    event = {"info": {"fieldName": field}, "arguments": arguments}
    if identity:
        event["identity"] = {"claims": {"email": email, "cognito:groups": list(groups)}}
    return event


@pytest.fixture
def resolver(monkeypatch):
    """The resolver module, reloaded against moto with both tables populated."""
    monkeypatch.setenv("INPUT_BUCKET", INPUT_BUCKET)
    monkeypatch.setenv("OUTPUT_BUCKET", OUTPUT_BUCKET)
    monkeypatch.setenv("CONFIGURATION_BUCKET", CONFIGURATION_BUCKET)
    monkeypatch.setenv("TRACKING_TABLE_NAME", TRACKING_TABLE)
    monkeypatch.setenv("USERS_TABLE_NAME", USERS_TABLE)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        for bucket in (INPUT_BUCKET, OUTPUT_BUCKET, CONFIGURATION_BUCKET):
            s3.create_bucket(Bucket=bucket)
        for object_key in (MINE, THEIRS, UNSTAMPED):
            s3.put_object(Bucket=INPUT_BUCKET, Key=object_key, Body=b"%PDF-1.4")
            s3.put_object(
                Bucket=OUTPUT_BUCKET,
                Key=_page_key(object_key),
                Body=b"\xff\xd8\xff",
                ContentType="image/jpeg",
            )
            s3.put_object(
                Bucket=OUTPUT_BUCKET,
                Key=_section_key(object_key),
                Body=b'{"inference_result": {"EmployeeName": "x"}}',
                ContentType="application/json",
            )

        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        tracking = ddb.create_table(
            TableName=TRACKING_TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        for object_key, profile in (
            (MINE, MINE_PROFILE),
            (THEIRS, THEIRS_PROFILE),
        ):
            tracking.put_item(
                Item={
                    "PK": f"doc#{object_key}",
                    "SK": "none",
                    "ObjectKey": object_key,
                    "ConfigVersion": profile,
                }
            )
        tracking.put_item(
            Item={"PK": f"doc#{UNSTAMPED}", "SK": "none", "ObjectKey": UNSTAMPED}
        )

        users = ddb.create_table(
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
        users.put_item(
            Item={
                "PK": "USER#scoped",
                "SK": "USER#scoped",
                "email": SCOPED_EMAIL,
                "allowedConfigVersions": [MINE_PROFILE],
            }
        )
        users.put_item(
            Item={
                "PK": "USER#unscoped",
                "SK": "USER#unscoped",
                "email": UNSCOPED_EMAIL,
            }
        )

        import index

        importlib.reload(index)
        yield index, s3, tracking, users


# --------------------------------------------------------------------------- #
# the check this change exists to add
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.parametrize(
    "field,locator",
    [
        ("getFilePresignedUrl", lambda: f"s3://{OUTPUT_BUCKET}/{_page_key(THEIRS)}"),
        ("getFilePresignedUrl", lambda: f"s3://{OUTPUT_BUCKET}/{_section_key(THEIRS)}"),
        ("getFilePresignedUrl", lambda: f"s3://{INPUT_BUCKET}/{THEIRS}"),
        ("getFileContents", lambda: f"s3://{OUTPUT_BUCKET}/{_section_key(THEIRS)}"),
        ("getFilePresignedUrls", lambda: [f"s3://{OUTPUT_BUCKET}/{_page_key(THEIRS)}"]),
    ],
)
def test_a_caller_in_a_group_is_refused_another_documents_bytes(
    resolver, field, locator
):
    """The cross-document read, on all three fields and both buckets.

    This is the assertion that did not exist before #1033: the caller holds a real
    group (``Viewer``), passes the dispatcher's floor and the bucket allow-list, and
    is refused because the document the key belongs to was processed under a profile
    outside their ``allowedConfigVersions``.

    **Mutation that fails it:** delete the ``scope.assert_allowed(bucket, key)`` line
    from ``index._parse_and_validate_one``, or make
    ``document_scope.assert_document_in_scope`` return early. Both restore the
    pre-#1033 behaviour and both turn every parameter of this test red.
    """
    index, *_ = resolver
    with pytest.raises(PermissionError, match="^Unauthorized:"):
        index.handler(_event(field, locator(), email=SCOPED_EMAIL), None)


@pytest.mark.unit
@pytest.mark.parametrize(
    "field,locator",
    [
        ("getFilePresignedUrl", lambda: f"s3://{OUTPUT_BUCKET}/{_page_key(MINE)}"),
        ("getFilePresignedUrl", lambda: f"s3://{INPUT_BUCKET}/{MINE}"),
        ("getFileContents", lambda: f"s3://{OUTPUT_BUCKET}/{_section_key(MINE)}"),
        ("getFilePresignedUrls", lambda: [f"s3://{OUTPUT_BUCKET}/{_page_key(MINE)}"]),
    ],
)
def test_a_scoped_caller_still_reads_their_own_documents(resolver, field, locator):
    """The other direction, which a refuse-everything implementation would fail.

    Without this, ``assert_document_in_scope`` raising unconditionally would satisfy
    every refusal assertion above while breaking the product.
    """
    index, *_ = resolver
    result = index.handler(_event(field, locator(), email=SCOPED_EMAIL), None)
    assert result


@pytest.mark.unit
def test_one_out_of_scope_key_refuses_the_whole_batch(resolver):
    """A batch is not an authorization oracle, key by key.

    The brief for the batch form was explicit about this: if a refusal were reported
    per entry, one call would tell the caller which of N documents are in their
    scope. So a single out-of-scope key must fail the **request**, and no partial
    result may be returned.

    **Mutation that fails it:** wrap the per-entry
    ``_parse_and_validate_one`` call in ``_handle_presigned_urls`` in a
    ``try/except PermissionError`` that records an error entry and continues.
    """
    index, *_ = resolver
    uris = [
        f"s3://{OUTPUT_BUCKET}/{_page_key(MINE, 1)}",
        f"s3://{OUTPUT_BUCKET}/{_page_key(THEIRS, 1)}",
    ]
    with pytest.raises(PermissionError, match="^Unauthorized:"):
        index.handler(_event("getFilePresignedUrls", uris, email=SCOPED_EMAIL), None)


@pytest.mark.unit
def test_every_refusal_carries_the_same_text_as_the_per_user_axes(resolver):
    """A refusal must not say which control answered, or whether anything exists.

    The per-document refusal, the per-profile one and the per-test-set one are the
    same string, so a caller cannot distinguish "that document is not yours" from
    "that document does not exist" from "that profile is not yours". The two
    literals are compared directly as well, because a reworded copy is the way this
    property is lost.
    """
    import document_scope
    import key_scope

    assert document_scope.REFUSAL == key_scope._REFUSAL

    index, *_ = resolver
    for uri in (
        f"s3://{OUTPUT_BUCKET}/{_page_key(THEIRS)}",
        f"s3://{OUTPUT_BUCKET}/{_page_key('inbox/never-existed.pdf')}",
        f"s3://{INPUT_BUCKET}/{THEIRS}",
    ):
        with pytest.raises(PermissionError) as raised:
            index.handler(_event("getFilePresignedUrl", uri, email=SCOPED_EMAIL), None)
        assert str(raised.value) == document_scope.REFUSAL
        for leak in ("theirs", "claims", "lending", "never-existed", OUTPUT_BUCKET):
            assert leak not in str(raised.value)


# --------------------------------------------------------------------------- #
# which control refused: the label, and the uniformity that keeps it safe
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.parametrize(
    "why,locator",
    [
        (
            "out of the caller's scope",
            lambda: f"s3://{OUTPUT_BUCKET}/{_page_key(THEIRS)}",
        ),
        (
            "the owning document is unstamped",
            lambda: f"s3://{OUTPUT_BUCKET}/{_page_key(UNSTAMPED)}",
        ),
        (
            "no tracked document owns the key",
            lambda: f"s3://{OUTPUT_BUCKET}/no-such-doc.pdf/pages/1/image.jpg",
        ),
        ("the source object, not an artifact", lambda: f"s3://{INPUT_BUCKET}/{THEIRS}"),
    ],
)
def test_a_per_document_refusal_declares_itself_as_a_per_object_control(
    resolver, why, locator
):
    """The refusal has to say *which* control answered, and it says it by class.

    A 403 ``errorType: "Unauthorized"`` is the response shape the field-level group
    floor uses too, so the dispatcher distinguishes them with
    ``errors[0].authzControl``, set from the resolver's exception class **name** (the
    object does not cross the ``lambda:Invoke`` boundary). ``ResourceAccessDenied``
    is the declaration; anything else authorization-shaped is labelled ``resolver``,
    documented as "a resolver refused and did not say which control", whose
    population is every resolver's own *group* check.

    Leaving it undeclared is not a silent cost. The live RBAC harness reads an
    unattributed refusal strictly (``refusal_is_resource_level`` answers False for
    everything but an explicit ``resource``), so the per-document cell in
    ``test_api_rbac.run_document_bytes_scope_suite`` goes to a hard failure on a
    deployment whose authorization is correct — the shape that published
    ``RBAC dynamic — FAIL`` for a sound release once already (#1308, #1478).

    **Mutation that fails it:** change any of the three ``raise`` statements in
    ``document_scope.assert_document_in_scope`` back to ``PermissionError``.
    """
    index, *_ = resolver
    with pytest.raises(PermissionError) as raised:
        index.handler(
            _event("getFilePresignedUrl", locator(), email=SCOPED_EMAIL), None
        )
    assert type(raised.value).__name__ == "ResourceAccessDenied", (
        f"the refusal for {why!r} is a decision about the object the caller named, "
        f"but arrives as {type(raised.value).__name__}, which the dispatcher labels "
        "'resolver' — read downstream as the strict verdict"
    )


def _raised_class_names(function):
    """Every class name raised directly inside one top-level function, with repeats.

    A **list** rather than a set, because the count is what the assertion below
    turns on, and ``.attr`` as well as ``.id`` so a qualified
    ``s3_targets.ResourceAccessDenied(...)`` is read as the class it names rather
    than as nothing. ``AsyncFunctionDef`` is accepted for the same reason: a
    reader converting this module to ``async def`` should get the real verdict,
    not "the function is missing".
    """
    import ast

    import document_scope

    source = pathlib.Path(document_scope.__file__).read_text()
    for node in ast.parse(source).body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function
        ):
            names = []
            for sub in ast.walk(node):
                if isinstance(sub, ast.Raise) and sub.exc is not None:
                    callee = sub.exc.func if isinstance(sub.exc, ast.Call) else sub.exc
                    name = getattr(callee, "id", None) or getattr(callee, "attr", None)
                    if name:
                        names.append(name)
            return names
    raise AssertionError(f"document_scope has no top-level {function}")


#: Every key shape that would make :func:`document_key_candidates` return an empty
#: list. All of them are refused as malformed before it gets that far — see the test
#: below, which is why that refusal branch is unreachable rather than untested.
_KEYS_THAT_NAME_NO_OBJECT = ("", "/", "//", ".", "..", "a/", "/a", "a//b")


@pytest.mark.unit
@pytest.mark.parametrize("key", _KEYS_THAT_NAME_NO_OBJECT)
def test_a_key_naming_no_object_is_refused_as_malformed_before_the_scope_check(key):
    """Why the empty-candidate refusal is unreachable, measured rather than assumed.

    ``assert_document_in_scope`` raises on an empty candidate list, and no caller can
    produce one: ``document_key_candidates`` calls
    ``key_scope.assert_canonical_key`` first, which rejects every key shape that
    would yield one with a ``ValueError`` — a 400, not a 403. So the branch is
    defence in depth behind a guard, and **this ordering is the real property**: the
    branch stays because an unreachable fail-closed refusal costs nothing and a
    reordering that exposed it should find a refusal there rather than a fall-through.

    Asserted here rather than left implicit because it is the one thing the
    handler-driven tests above cannot show, and because the count pinned in
    ``test_the_label_does_not_distinguish_one_per_document_refusal_from_another``
    counts that raise — a reader checking the three per-object refusals against live
    coverage would otherwise find one of them has none and reasonably conclude the
    test is loose.
    """
    import document_scope

    with pytest.raises(ValueError, match="canonical form"):
        document_scope.document_key_candidates(
            INPUT_BUCKET,
            key,
            input_bucket=INPUT_BUCKET,
            output_bucket=OUTPUT_BUCKET,
        )


@pytest.mark.unit
def test_the_label_does_not_distinguish_one_per_document_refusal_from_another():
    """Uniformity across the per-object branches, which is the load-bearing part.

    Converting only the scope mismatch would separate "this document exists and is
    outside your scope" from "no such document" in ``authzControl`` — an existence
    oracle at the API, over documents entirely outside the caller's scope, and
    exactly what the single shared refusal string exists to prevent. The status, the
    message and the class are therefore all three identical across every branch of
    ``assert_document_in_scope``.

    ⚠️ **The COUNT is pinned, not just the set**, and that is the whole strength of
    this test. A set is satisfied by routing one branch through a helper that raises
    the unattributed class while the other two keep raising directly: the set is
    unchanged, every behavioural test still passes, and the oracle is back. A count
    fails that mutation. It also means a branch added to this function fails here
    until it is accounted for, which is the intended cost — the alternative is a
    source-shaped assertion that silently stops covering what it describes.

    The ``resolver`` branches leak nothing by contrast and are asserted the other
    way below: none of them varies with the key, so they answer the same for every
    object a given caller names.
    """
    raised = _raised_class_names("assert_document_in_scope")

    # RuntimeError is the defensive `resolver is None` branch: a caller-side
    # programming error that must surface as a 500, not an authorization answer.
    assert sorted(raised) == ["ResourceAccessDenied"] * 3 + ["RuntimeError"], (
        "every refusal in assert_document_in_scope must raise the same class, and "
        "all three of them must raise it HERE rather than through a helper that "
        f"could raise a different one; raised {sorted(raised)}"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "why,prepare",
    [
        (
            "an unresolvable scope lookup is a fact about the deployment",
            lambda index, mp: (
                mp.setattr(index, "_user_scope_cache", {}),
                mp.setenv("USERS_TABLE_NAME", "no-such-users-table"),
            ),
        ),
        (
            "an unwired tracking table is a template fault",
            lambda index, mp: mp.setattr(index, "TRACKING_TABLE_NAME", ""),
        ),
    ],
)
def test_a_fail_closed_refusal_stays_unattributed(resolver, monkeypatch, why, prepare):
    """The other side of the line, asserted so the conversion cannot creep.

    Neither of these is a decision about the object the caller named — one is about
    the principal's scope being unreadable, the other about a bucket of wiring the
    template did not set — so ``resolver`` is the true label and the strict reading
    it gets is what keeps a deployment fault in the row an operator looks at first.
    Relabelling one cannot create a false pass, but it would file a real fault under
    "a per-object control working as intended".

    Both are indistinguishable from the per-object refusals in status and message,
    which is why the label can differ safely: they do not vary with the key.
    """
    index, *_ = resolver
    prepare(index, monkeypatch)
    with pytest.raises(PermissionError) as raised:
        index.handler(
            _event(
                "getFilePresignedUrl",
                f"s3://{OUTPUT_BUCKET}/{_page_key(MINE)}",
                email=SCOPED_EMAIL,
            ),
            None,
        )
    assert type(raised.value).__name__ != "ResourceAccessDenied", (
        f"{why}, so it must stay unattributed; it now arrives as "
        f"{type(raised.value).__name__}, which the dispatcher labels 'resource'"
    )


# --------------------------------------------------------------------------- #
# fail closed, every way the answer can be unavailable
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_a_key_no_tracked_document_owns_is_refused(resolver):
    """Not "unscoped" — refused.

    A key under no document this deployment tracks cannot be proven to be in the
    caller's scope. Reading it as unpartitioned would exempt precisely the keys this
    check exists to bound, since an attacker supplies the key.

    **Mutation that fails it:** make ``assert_document_in_scope`` ``return`` instead
    of raising when ``resolver.owning_document`` answers ``None``.
    """
    index, *_ = resolver
    with pytest.raises(PermissionError, match="^Unauthorized:"):
        index.handler(
            _event(
                "getFilePresignedUrl",
                f"s3://{OUTPUT_BUCKET}/no-such-doc.pdf/pages/1/image.jpg",
                email=SCOPED_EMAIL,
            ),
            None,
        )


@pytest.mark.unit
def test_an_unstamped_document_is_refused_to_a_scoped_caller(resolver):
    """``scope_allows`` denies a ``None`` name against a set scope.

    The same rule the chat processor and both document-list resolvers apply: a
    document carrying no ``ConfigVersion`` cannot be shown to be inside a
    restriction, so it is outside it.
    """
    index, *_ = resolver
    with pytest.raises(PermissionError, match="^Unauthorized:"):
        index.handler(
            _event(
                "getFilePresignedUrl",
                f"s3://{OUTPUT_BUCKET}/{_page_key(UNSTAMPED)}",
                email=SCOPED_EMAIL,
            ),
            None,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "identity",
    [
        pytest.param(..., id="absent"),
        pytest.param(None, id="null"),
        pytest.param({}, id="no-claims-key"),
        pytest.param({"claims": None}, id="null-claims"),
        pytest.param({"claims": "scoped@example.invalid"}, id="claims-not-a-mapping"),
        pytest.param({"claims": []}, id="claims-is-a-list"),
    ],
)
def test_an_event_with_no_verified_identity_is_refused(resolver, identity):
    """AUTH.T07's shape: an absent or unusable identity is not a trusted caller.

    The only invoker of this function is ``http_api_dispatcher``, which always builds
    the identity from the authorizer's verified claims. Reading an absent one as
    trusted would make every check here removable by dropping a key from the event.

    ⚠️ **Every shape is driven, because there are two refusals and one test reached
    only the first.** ``resolve_caller_scope`` refuses an identity that is not a
    mapping and, separately, a mapping whose ``claims`` is not one — and an event with
    no ``identity`` key at all only ever reaches the first. A mutation deleting the
    second ``raise`` survived the suite until this was parametrised, which is the
    whole value of having run one: the branch was written, reviewed and uncovered.
    """
    index, *_ = resolver
    event = {
        "info": {"fieldName": "getFilePresignedUrl"},
        "arguments": {"s3Uri": f"s3://{OUTPUT_BUCKET}/{_page_key(MINE)}"},
    }
    if identity is not ...:
        event["identity"] = identity
    with pytest.raises(PermissionError, match="^Unauthorized:"):
        index.handler(event, None)


@pytest.mark.unit
def test_an_unresolvable_scope_lookup_denies(resolver, monkeypatch):
    """A failed UsersTable read is a refusal, never an unrestricted caller.

    Exercised by pointing ``USERS_TABLE_NAME`` at a table that does not exist, so
    the DynamoDB call really raises rather than being stubbed. ⚠️ Driven with a
    caller holding **no** unscoped group: an ``Admin`` is unrestricted before the
    lookup happens, so an Admin here would pass whatever the table did and the
    assertion would measure nothing.
    """
    index, *_ = resolver
    monkeypatch.setattr(index, "_user_scope_cache", {})
    monkeypatch.setenv("USERS_TABLE_NAME", "no-such-users-table")
    with pytest.raises(PermissionError, match="^Unauthorized:"):
        index.handler(
            _event(
                "getFilePresignedUrl",
                f"s3://{OUTPUT_BUCKET}/{_page_key(MINE)}",
                email=SCOPED_EMAIL,
            ),
            None,
        )


@pytest.mark.unit
def test_an_unwired_tracking_table_denies(resolver, monkeypatch):
    """No table name means the document cannot be identified, so the read is refused.

    The alternative — reading an unset environment variable as "nothing to check" —
    would make the per-document check removable by deleting one line from
    ``nested/api-resolvers/template.yaml``, with every unit test still green.
    """
    index, *_ = resolver
    monkeypatch.setattr(index, "TRACKING_TABLE_NAME", "")
    with pytest.raises(PermissionError, match="^Unauthorized:"):
        index.handler(
            _event(
                "getFilePresignedUrl",
                f"s3://{OUTPUT_BUCKET}/{_page_key(MINE)}",
                email=SCOPED_EMAIL,
            ),
            None,
        )


# --------------------------------------------------------------------------- #
# the bound: an unscoped caller is deliberately NOT narrowed
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.parametrize("email", [UNSCOPED_EMAIL, NO_ROW_EMAIL])
def test_an_unscoped_caller_reads_any_document_by_design(resolver, email):
    """The honest bound on what #1033 closes, asserted rather than implied.

    ``resolve_allowed_config_versions`` returns ``None`` — unrestricted — both for a
    row that sets no ``allowedConfigVersions`` and for a caller no row matches at
    all. Scoping is opt-in per user and must stay that way: denying on an empty page
    would lock every ordinary user out of the UI. So for these callers the
    per-document check is a no-op and the bound on document reads is the group floor
    — the same reach ``listDocuments`` already gives them.

    This is pinned positively so that the limit is visible in the test output rather
    than only in prose, and so that a change making it a refusal is a deliberate
    product decision rather than a side effect.
    """
    index, *_ = resolver
    result = index.handler(
        _event(
            "getFilePresignedUrl",
            f"s3://{OUTPUT_BUCKET}/{_page_key(THEIRS)}",
            email=email,
        ),
        None,
    )
    assert result["presignedUrl"].startswith("https://")


@pytest.mark.unit
def test_an_admin_is_unrestricted_without_a_lookup(resolver, monkeypatch):
    """Mirrors ``key_scope.CONFIG_UNSCOPED_GROUPS`` and the configuration resolver.

    Asserted with the UsersTable pointed at nothing, so a lookup would raise: an
    Admin must not depend on one. The three enforcements of this rule have to agree,
    because a caller one path serves and another refuses is the divergence
    ``config_scope`` exists to prevent.
    """
    index, *_ = resolver
    monkeypatch.setattr(index, "_user_scope_cache", {})
    monkeypatch.setenv("USERS_TABLE_NAME", "no-such-users-table")
    result = index.handler(
        _event(
            "getFilePresignedUrl",
            f"s3://{OUTPUT_BUCKET}/{_page_key(THEIRS)}",
            email=SCOPED_EMAIL,
            groups=("Admin",),
        ),
        None,
    )
    assert result["presignedUrl"].startswith("https://")


@pytest.mark.unit
@pytest.mark.parametrize(
    "groups,identity,expect_refusal",
    [
        pytest.param(("Admin",), True, False, id="admin-is-unscoped"),
        pytest.param(("Viewer",), False, True, id="no-identity-refuses"),
    ],
)
def test_a_decision_needing_no_lookup_builds_no_dynamodb_client(
    resolver, monkeypatch, groups, identity, expect_refusal
):
    """The resource is built on demand, and three branches never demand it.

    An ``Admin`` is unrestricted and an event with no verified identity is refused,
    both before any table is read — so neither may construct a DynamoDB client. That
    is not only a wasted client per request: ``boto3.resource("dynamodb")`` **raises**
    where no region is configured, so an eagerly-evaluated argument turned an Admin
    request this module waves through into a 500. It was invisible locally and showed
    up only under the hermetic test wrapper, which strips every region variable — so
    it is pinned here by making the factory raise, which is the same failure in a
    form that does not depend on the environment.
    """
    index, *_ = resolver

    def _refuse():
        raise AssertionError(
            "a decision that needs no UsersTable read must not build a DynamoDB "
            "client; pass the factory, not a resource"
        )

    monkeypatch.setattr(index, "_dynamodb_resource", _refuse)
    event = {
        "info": {"fieldName": "getFilePresignedUrl"},
        "arguments": {"s3Uri": f"s3://{OUTPUT_BUCKET}/{_page_key(THEIRS)}"},
    }
    if identity:
        event["identity"] = {
            "claims": {"email": SCOPED_EMAIL, "cognito:groups": list(groups)}
        }
    if expect_refusal:
        with pytest.raises(PermissionError, match="^Unauthorized:"):
            index.handler(event, None)
    else:
        assert index.handler(event, None)["presignedUrl"].startswith("https://")


# --------------------------------------------------------------------------- #
# which document owns a key
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_candidates_are_strictly_longest_first():
    """The ordering is the whole rule: the longest existing prefix is the owner.

    ``idp_common.document_versions`` calls out deployments where uploads nest — ``foo``
    and ``foo/bar.pdf`` both documents. ``foo``'s own page is ``foo/pages/1/image.jpg``
    and ``foo/bar.pdf``'s is ``foo/bar.pdf/pages/1/image.jpg``, so anything that puts a
    shorter ancestor first authorizes the second key against the wrong document.
    """
    import document_scope

    def candidates(key):
        return document_scope.document_key_candidates(
            "out", key, input_bucket="in", output_bucket="out"
        )

    assert candidates("foo/bar.pdf/pages/1/image.jpg") == [
        "foo/bar.pdf/pages/1/image.jpg",
        "foo/bar.pdf/pages/1",
        "foo/bar.pdf/pages",
        "foo/bar.pdf",
        "foo",
    ]
    for key in (
        "foo/bar.pdf/pages/1/image.jpg",
        "a/pages/b.pdf/foo/x.json",
        "a/b/c/d/e.pdf/sections/3/result.json",
    ):
        found = candidates(key)
        # `is not None` separately, and first: `None` means "not a document bucket",
        # so a regression that stopped recognising the Output bucket would raise a
        # TypeError here instead of failing with the bucket named. basedpyright
        # objects to iterating an optional for the same reason.
        assert found is not None, key
        assert len(found) == len(set(found)), found
        lengths = [c.count("/") for c in found]
        assert lengths == sorted(lengths, reverse=True), (key, found)


@pytest.mark.unit
def test_no_key_shape_authorizes_against_a_shorter_document(resolver):
    """The regression this ordering exists to prevent, driven end to end.

    An earlier version proposed the ancestors whose *next* segment named a known
    artifact store first, so that a document's N page keys shared one candidate. For
    every artifact store it knew about that was correct as well as faster. For one it
    did not, it put a **shorter** ancestor ahead of the true owner: for
    ``a/pages/b.pdf/foo/x.json`` it proposed ``a``, because ``a`` is followed by
    ``pages``, before ``a/pages/b.pdf`` — so with documents at both, a caller scoped to
    ``a``'s profile was authorized to read ``a/pages/b.pdf``'s artifact. The list being
    incomplete had been documented as costing latency and nothing else.

    Both segment shapes are driven, the registered-looking one and the arbitrary one,
    because the defect was visible only in the second and the first passed throughout.
    """
    index, s3, tracking, _users = resolver
    s3.put_object(
        Bucket=OUTPUT_BUCKET,
        Key="a/summary/summary.json",
        Body=b"{}",
        ContentType="application/json",
    )
    for object_key, profile in (("a", MINE_PROFILE), ("a/pages/b.pdf", THEIRS_PROFILE)):
        tracking.put_item(
            Item={
                "PK": f"doc#{object_key}",
                "SK": "none",
                "ObjectKey": object_key,
                "ConfigVersion": profile,
            }
        )
    for tail in ("sections/1/result.json", "foo/x.json", "anything/at/all.bin"):
        with pytest.raises(PermissionError, match="^Unauthorized:"):
            index.handler(
                _event(
                    "getFilePresignedUrl",
                    f"s3://{OUTPUT_BUCKET}/a/pages/b.pdf/{tail}",
                    email=SCOPED_EMAIL,
                ),
                None,
            )
    # ...and the shallower document's OWN artifacts are still served, so the fix is
    # not "refuse everything under a/". The object is created above because the
    # handler HEADs it after authorizing, and a 404 there would be indistinguishable
    # from the authorization having failed.
    assert index.handler(
        _event(
            "getFilePresignedUrl",
            f"s3://{OUTPUT_BUCKET}/a/summary/summary.json",
            email=SCOPED_EMAIL,
        ),
        None,
    )


@pytest.mark.unit
def test_the_input_bucket_key_is_the_document_key():
    """No walk for the Input bucket: the uploaded key IS the tracking row's key."""
    import document_scope

    assert document_scope.document_key_candidates(
        "in", "foo/bar.pdf", input_bucket="in", output_bucket="out"
    ) == ["foo/bar.pdf"]


@pytest.mark.unit
def test_a_bucket_no_document_partitions_is_not_document_scoped():
    """``None``, not an empty list: the module has nothing to say about the key."""
    import document_scope

    assert (
        document_scope.document_key_candidates(
            "cfg",
            "config_revisions/x/000001.json.gz",
            input_bucket="in",
            output_bucket="out",
        )
        is None
    )


@pytest.mark.unit
def test_an_empty_bucket_name_matches_nothing():
    """An unset ``INPUT_BUCKET`` must not make every bucket the input bucket.

    ``bucket == os.environ.get(...)`` would do exactly that for a request naming a
    bucket the allow-list somehow admitted with no name configured, which would make
    this check apply to the wrong keys and skip the right ones.
    """
    import document_scope

    assert not document_scope.is_document_bucket("", input_bucket="", output_bucket="")
    assert not document_scope.is_document_bucket("anything", input_bucket="")
    assert document_scope.document_key_candidates("anything", "k") is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    [
        "/inbox/mine.pdf/pages/1/image.jpg",
        "inbox//mine.pdf/pages/1/image.jpg",
        "./inbox/mine.pdf/pages/1/image.jpg",
        "inbox/../theirs.pdf/pages/1/image.jpg",
        "inbox/mine.pdf/pages/1/image.jpg/",
    ],
)
def test_a_non_canonical_key_is_refused_as_malformed(resolver, key):
    """A 400, not a refusal, and the same rule ``key_scope`` applies.

    These spellings name the same document to a human and derive a *different*
    candidate list to a ``/``-splitting walk, so a normalising intermediary between
    the signed URL and S3 could shift which document was authorized. The rule is
    ``key_scope.assert_canonical_key`` — the function itself, not a second copy of
    it.
    """
    index, *_ = resolver
    with pytest.raises(ValueError, match="Invalid S3 URI"):
        index.handler(
            _event(
                "getFilePresignedUrl",
                f"s3://{OUTPUT_BUCKET}/{key}",
                email=SCOPED_EMAIL,
            ),
            None,
        )


# --------------------------------------------------------------------------- #
# bounded work
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_a_batch_over_one_document_costs_a_handful_of_round_trips(resolver):
    """The property that makes a strictly-ordered candidate walk affordable.

    269 page keys of one document produce ~540 distinct candidates — each key's own
    name and its ``…/pages/<n>`` parent differ per page, and everything above that is
    shared. Resolved one key at a time that is 540 round trips; resolved 100 per
    ``BatchGetItem`` it is six. Measured by counting the resolver's own round trips,
    not by timing, and asserted against the budget so a regression in either shows up
    as a number rather than as a slow test.

    ⚠️ Counting *candidates* instead of *round trips* is what made the earlier version
    of this assertion mislead: it asserted one lookup per document, which was true of
    an ordering that was unsound, and it would have accepted any number of round trips.
    """
    import document_scope

    index, _s3, tracking, _users = resolver
    scope = index._DocumentScope(_event("getFilePresignedUrls", [], email=SCOPED_EMAIL))
    pages = [(OUTPUT_BUCKET, _page_key(MINE, page)) for page in range(1, 270)]
    scope.prime(pages)
    for bucket, key in pages:
        scope.assert_allowed(bucket, key)

    candidates = scope._resolver.lookups
    batches = scope._resolver.batches
    assert candidates > 400, (
        f"only {candidates} candidates were resolved for 269 page keys, which is too "
        "few for a strictly-ordered walk — the ordering may have been shortcut again"
    )
    assert batches <= 1 + candidates // 100, (
        f"{batches} round trips for {candidates} candidates; priming is not batching "
        "them"
    )
    assert batches < document_scope.MAX_DOCUMENT_LOOKUP_BATCHES


@pytest.mark.unit
def test_priming_is_an_optimisation_and_not_a_requirement(resolver):
    """The same answers without it, so a caller that forgets is correct and slower.

    This is why ``prime`` may stay an optimisation: ``owning_document`` resolves any
    candidate it has not been given. A design where skipping the prime step skipped
    the check would be the worst available shape here.
    """
    index, *_ = resolver
    unprimed = index._DocumentScope(
        _event("getFilePresignedUrl", "", email=SCOPED_EMAIL)
    )
    unprimed.assert_allowed(OUTPUT_BUCKET, _page_key(MINE))
    with pytest.raises(PermissionError, match="^Unauthorized:"):
        unprimed.assert_allowed(OUTPUT_BUCKET, _page_key(THEIRS))


@pytest.mark.unit
def test_the_round_trip_budget_is_a_plausible_number():
    """The constant itself, bounded, so a mutation of it is a failure and not a hang.

    A budget large enough to be no budget defeats the amplification bound this check
    exists for; one smaller than a handful would refuse an ordinary document view,
    which needs six. Both ends are asserted rather than only the behaviour, because
    the behavioural tests derive their fixture sizes from the constant — so a mutation
    setting it to a billion would build a billion-element list rather than failing. A
    test that cannot fail without exhausting the host is not a test.
    """
    import document_scope

    assert 8 <= document_scope.MAX_DOCUMENT_LOOKUP_BATCHES <= 256, (
        f"MAX_DOCUMENT_LOOKUP_BATCHES is "
        f"{document_scope.MAX_DOCUMENT_LOOKUP_BATCHES}. Each round trip resolves up "
        "to 100 candidates: too high and one API call becomes an unbounded number of "
        "DynamoDB reads, too low and a legitimate multi-page view is refused."
    )
    assert document_scope._CANDIDATES_PER_BATCH == 100, (
        "DynamoDB's BatchGetItem limit is 100 keys per table per call; a larger value "
        "is rejected by the service and a smaller one silently multiplies round trips"
    )


@pytest.mark.unit
def test_the_round_trip_budget_refuses_rather_than_amplifying(resolver):
    """One API call must not become thousands of DynamoDB round trips.

    Driven with keys deep enough that each one contributes many candidates, which is
    the shape that amplifies — a batch over ONE document shares nearly all of its
    candidates and is six round trips however many pages it names.
    """
    import document_scope

    index, *_ = resolver
    budget = document_scope.MAX_DOCUMENT_LOOKUP_BATCHES
    # Each key has 40 segments and shares no ancestor with any other, so it
    # contributes ~40 candidates; `budget + 2` batches' worth of them is comfortably
    # over the limit. Derived from the constants rather than written as a literal, so
    # raising either does not silently turn this into a test of nothing.
    per_key = 40
    keys = (budget * document_scope._CANDIDATES_PER_BATCH) // per_key + per_key
    uris = [
        "s3://"
        + OUTPUT_BUCKET
        + "/"
        + "/".join(f"s{n}x{i}" for i in range(per_key - 1))
        + f"/d{n}.pdf"
        for n in range(keys)
    ]
    with pytest.raises(PermissionError, match="^Unauthorized:"):
        index.handler(_event("getFilePresignedUrls", uris, email=SCOPED_EMAIL), None)


@pytest.mark.unit
def test_the_budget_refusal_is_not_an_existence_oracle(resolver):
    """The status class must not depend on which candidates exist.

    How many round trips a request costs is a function of **which documents exist**,
    because the walk stops at the first row it finds. So a 400 "too broad" beside a
    403 "refused" hands a caller one bit about whether an arbitrary object key is a
    tracked document — for documents entirely outside their scope, which is the
    distinction the single refusal message is worded to avoid, and it needs no timing
    analysis because the two answers are different status classes.

    Both arms are driven: the same padded request with a probe key that exists and one
    that does not must produce the same exception type and the same message.

    ⚠️ The exception **class** is part of that, not just the status and the string.
    The class name crosses the invoke boundary and the dispatcher turns it into
    ``errors[0].authzControl``, so a budget overrun raising the unattributed
    ``PermissionError`` while an ordinary scope denial declares itself would put the
    bit this test exists to deny back into the response — in a different field.
    """
    import document_scope

    index, _s3, tracking, _users = resolver
    budget = document_scope.MAX_DOCUMENT_LOOKUP_BATCHES
    per_key = 40
    filler = [
        "s3://"
        + OUTPUT_BUCKET
        + "/"
        + "/".join(f"f{n}y{i}" for i in range(per_key - 1))
        + f"/d{n}.pdf"
        for n in range((budget * document_scope._CANDIDATES_PER_BATCH) // per_key + 2)
    ]
    outcomes = set()
    for probe in (
        f"s3://{OUTPUT_BUCKET}/{_page_key(THEIRS)}",
        f"s3://{OUTPUT_BUCKET}/{_page_key('inbox/ghost.pdf')}",
    ):
        with pytest.raises(Exception) as raised:  # noqa: B017 - the TYPE is the assertion
            index.handler(
                _event("getFilePresignedUrls", filler + [probe], email=SCOPED_EMAIL),
                None,
            )
        outcomes.add((type(raised.value).__name__, str(raised.value)))
    assert len(outcomes) == 1, (
        "the two probes produced different outcomes, so the refusal reports whether "
        f"the probed key is a tracked document: {outcomes}"
    )
    ((name, message),) = outcomes
    assert name == "ResourceAccessDenied"
    assert message == document_scope.REFUSAL


@pytest.mark.unit
@pytest.mark.parametrize("field", ["getFileContents", "getFilePresignedUrl"])
def test_a_single_object_field_reports_the_budget_as_a_refusal_too(resolver, field):
    """Not as a 500, which is what it did while only the batch field converted it.

    The dispatcher maps anything that is not ``ValueError``/``PermissionError`` to a
    500 ``InternalError`` **with the exception text in the body**, so a malformed
    caller-supplied key arrived as a server fault that also leaked the internal
    constant. One deep key is enough to exceed the round-trip budget on its own.
    """
    import document_scope

    index, *_ = resolver
    per_key = document_scope.MAX_DOCUMENT_LOOKUP_BATCHES * (
        document_scope._CANDIDATES_PER_BATCH + 1
    )
    deep = "/".join(f"s{i}" for i in range(per_key))
    with pytest.raises(PermissionError, match="^Unauthorized:"):
        index.handler(
            _event(field, f"s3://{OUTPUT_BUCKET}/{deep}", email=SCOPED_EMAIL), None
        )


@pytest.mark.unit
def test_an_unresolvable_batch_denies_after_a_bounded_backed_off_retry():
    """Three claims about the retry, each of which was wrong when written.

    * It **denies** rather than reading an unresolved candidate as "no such
      document and therefore refused for scope reasons" — the refusal is the same
      string either way, but the log line has to be able to say which, so the
      exhausted path raises explicitly.
    * Every attempt **spends budget**. Counting one per ``_fetch`` instead made the
      documented bound four times optimistic: four attempts per batch against a
      64-batch budget is 256 calls, while the reasoning about the dispatcher's 20 s
      rested on 64.
    * It **backs off**. Four immediate retries against a throttled partition are
      four throttled calls, so the first version retried in under 10 microseconds in
      total and would almost certainly have failed anyway — a mitigation in shape
      only.

    Driven against a stub that always reports everything unprocessed, so the
    exhausted path is the one measured, with ``time.sleep`` captured rather than
    waited on.
    """
    import document_scope

    class _AlwaysUnprocessed:
        def __init__(self):
            self.calls = 0

        def batch_get_item(self, RequestItems):  # noqa: N803 - boto3's own spelling
            self.calls += 1
            return {"Responses": {}, "UnprocessedKeys": dict(RequestItems)}

    stub = _AlwaysUnprocessed()
    slept = []
    original_sleep = document_scope.time.sleep
    document_scope.time.sleep = slept.append
    try:
        resolver = document_scope.DocumentResolver(stub, "tracking")
        with pytest.raises(PermissionError, match="^Unauthorized:"):
            resolver.owning_document(["a/b.pdf", "a"])
    finally:
        document_scope.time.sleep = original_sleep

    attempts = document_scope._UNPROCESSED_RETRIES + 1
    assert stub.calls == attempts, stub.calls
    assert resolver.batches == attempts, (
        f"{attempts} calls were made but {resolver.batches} counted against the "
        "budget, so the documented round-trip bound is optimistic by that factor"
    )
    assert slept == [
        document_scope._UNPROCESSED_BACKOFF_SECONDS * (2**n)
        for n in range(attempts - 1)
    ], slept
    assert sum(slept) < 1.0, (
        f"the retry waits {sum(slept)}s per batch, which at the full round-trip "
        "budget would exceed the dispatcher's 20 s abandonment"
    )


@pytest.mark.unit
def test_a_batch_that_recovers_on_retry_returns_the_answer():
    """The other direction: a transient throttle must not become a refusal.

    Without this, raising unconditionally on any ``UnprocessedKeys`` would satisfy
    the test above while turning every throttle into an authorization failure — which
    is the thing the retry exists to prevent.
    """
    import document_scope

    class _ThrottledOnce:
        def __init__(self):
            self.calls = 0

        def batch_get_item(self, RequestItems):  # noqa: N803 - boto3's own spelling
            self.calls += 1
            if self.calls == 1:
                return {"Responses": {}, "UnprocessedKeys": dict(RequestItems)}
            return {
                "Responses": {
                    "tracking": [{"PK": "doc#a/b.pdf", "ConfigVersion": MINE_PROFILE}]
                },
                "UnprocessedKeys": {},
            }

    slept = []
    original_sleep = document_scope.time.sleep
    document_scope.time.sleep = slept.append
    try:
        stub = _ThrottledOnce()
        resolver = document_scope.DocumentResolver(stub, "tracking")
        assert resolver.owning_document(["a/b.pdf", "a"]) == (
            "a/b.pdf",
            MINE_PROFILE,
        )
    finally:
        document_scope.time.sleep = original_sleep
    assert stub.calls == 2
    assert slept == [document_scope._UNPROCESSED_BACKOFF_SECONDS]


@pytest.mark.unit
def test_the_read_is_strongly_consistent_and_projects_what_the_check_reads():
    """Both are claims on the wire, so both are read off the request.

    ``ConsistentRead`` because an eventually-consistent read that has not caught up
    reports no row and no row **denies** — a document written moments ago would be
    refused to its own uploader. The projection because ``_config_version`` reads two
    attribute spellings and the join needs ``PK``; projecting one of the three turns a
    legacy-spelled document into a refusal for a scoped caller.
    """
    import document_scope

    captured = []

    class _Recording:
        def batch_get_item(self, RequestItems):  # noqa: N803 - boto3's own spelling
            captured.append(RequestItems)
            return {"Responses": {}, "UnprocessedKeys": {}}

    document_scope.DocumentResolver(_Recording(), "tracking").owning_document(["a"])
    assert captured, "no request was issued"
    table = captured[0]["tracking"]
    assert table["ConsistentRead"] is True
    projected = {part.strip() for part in table["ProjectionExpression"].split(",")}
    assert projected == {"PK", *document_scope._CONFIG_VERSION_ATTRIBUTES}, projected


@pytest.mark.unit
def test_a_document_carrying_only_the_legacy_attribute_spelling_is_read(resolver):
    """``ConfigurationVersion`` is the older name and rows still carry it.

    Asserted end to end in both directions, because treating a legacy-spelled
    document as unstamped would refuse a scoped caller their own document — and the
    projection above is what makes it reachable at all.
    """
    index, _s3, tracking, _users = resolver
    for object_key, profile in (
        ("legacy/mine.pdf", MINE_PROFILE),
        ("legacy/theirs.pdf", THEIRS_PROFILE),
    ):
        tracking.put_item(
            Item={
                "PK": f"doc#{object_key}",
                "SK": "none",
                "ObjectKey": object_key,
                "ConfigurationVersion": profile,
            }
        )
        _s3.put_object(
            Bucket=OUTPUT_BUCKET,
            Key=_page_key(object_key),
            Body=b"\xff\xd8\xff",
            ContentType="image/jpeg",
        )
    assert index.handler(
        _event(
            "getFilePresignedUrl",
            f"s3://{OUTPUT_BUCKET}/{_page_key('legacy/mine.pdf')}",
            email=SCOPED_EMAIL,
        ),
        None,
    )
    with pytest.raises(PermissionError, match="^Unauthorized:"):
        index.handler(
            _event(
                "getFilePresignedUrl",
                f"s3://{OUTPUT_BUCKET}/{_page_key('legacy/theirs.pdf')}",
                email=SCOPED_EMAIL,
            ),
            None,
        )


@pytest.mark.unit
def test_the_memo_is_per_request_not_per_container(resolver):
    """A container-lifetime memo would serve a stale ``ConfigVersion``.

    A reprocess can change which profile a document carries; on a restriction, a
    stale answer fails open. Asserted by changing the row between two requests and
    confirming the second sees the change.
    """
    index, _s3, tracking, _users = resolver
    uri = f"s3://{OUTPUT_BUCKET}/{_page_key(MINE)}"
    assert index.handler(_event("getFilePresignedUrl", uri, email=SCOPED_EMAIL), None)

    tracking.put_item(
        Item={
            "PK": f"doc#{MINE}",
            "SK": "none",
            "ObjectKey": MINE,
            "ConfigVersion": THEIRS_PROFILE,
        }
    )
    with pytest.raises(PermissionError, match="^Unauthorized:"):
        index.handler(_event("getFilePresignedUrl", uri, email=SCOPED_EMAIL), None)
