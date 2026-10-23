# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The batch presign field, ``getFilePresignedUrls``.

Its authorization is covered in ``test_document_scope.py``; this file covers the
shape of the operation itself: the argument validation, the **enforced** list cap,
the response, and the two properties that make the batch form different from N
calls to the single form rather than merely faster — no ``head_object``, and
authorization over every entry before the first URL is minted.

The cap is the part worth being careful about. The argument is a caller-supplied
list, so an uncapped one is a request amplifier: one API call that signs an
arbitrary number of URLs, walks an arbitrary number of tracking-table ancestors and
returns an arbitrary number of bytes. A cap that is documented and not enforced is
worth nothing, so the assertion here is on the refusal.
"""

from __future__ import annotations

import importlib
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from moto import mock_aws

OUTPUT_BUCKET = "output-bucket"
OTHER_BUCKET = "some-unrelated-bucket"
DOC = "doc.pdf"


def _event(files, groups=("Admin",)):
    return {
        "info": {"fieldName": "getFilePresignedUrls"},
        "arguments": {"files": files},
        "identity": {"claims": {"cognito:groups": list(groups)}},
    }


def _uri(key):
    return f"s3://{OUTPUT_BUCKET}/{key}"


def _page(page):
    return f"{DOC}/pages/{page}/image.jpg"


@pytest.fixture
def resolver(monkeypatch):
    monkeypatch.setenv("OUTPUT_BUCKET", OUTPUT_BUCKET)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=OUTPUT_BUCKET)
        s3.create_bucket(Bucket=OTHER_BUCKET)
        for page in (1, 2, 3):
            s3.put_object(
                Bucket=OUTPUT_BUCKET,
                Key=_page(page),
                Body=b"\xff\xd8\xff",
                ContentType="image/jpeg",
            )

        import index

        importlib.reload(index)
        yield index, s3


# --------------------------------------------------------------------------- #
# the response
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_one_url_per_requested_object_in_request_order(resolver):
    index, _ = resolver
    result = index.handler(_event([{"s3Uri": _uri(_page(n))} for n in (1, 2, 3)]), None)
    assert [entry["s3Uri"] for entry in result["results"]] == [
        _uri(_page(1)),
        _uri(_page(2)),
        _uri(_page(3)),
    ]
    for entry in result["results"]:
        assert entry["presignedUrl"].startswith("https://")
        assert entry["contentType"] == "image/jpeg"
        # No `size`: this field issues no head_object, so there is none to report,
        # and claiming one would be the kind of plausible wrong answer a consumer
        # would act on.
        assert "size" not in entry


@pytest.mark.unit
def test_the_echoed_uri_is_what_the_caller_sent(resolver):
    """So a client can join on it rather than on list index.

    The UI chunks its requests and merges the results into one map; joining on
    position across chunk boundaries is the kind of bug that shows the wrong page's
    image, which is worse than showing none.
    """
    index, _ = resolver
    sent = _uri(_page(2))
    result = index.handler(_event([{"s3Uri": sent}]), None)
    assert result["results"][0]["s3Uri"] == sent


@pytest.mark.unit
def test_an_image_key_is_signed_for_inline_display(resolver):
    index, _ = resolver
    result = index.handler(_event([{"s3Uri": _uri(_page(1))}]), None)
    query = {
        k.lower(): v
        for k, v in parse_qs(
            urlparse(result["results"][0]["presignedUrl"]).query
        ).items()
    }
    assert query["response-content-type"] == ["image/jpeg"]
    assert query["response-content-disposition"] == ["inline"]


@pytest.mark.unit
@pytest.mark.parametrize("key", [f"{DOC}/pages/1/evil.svg", f"{DOC}/pages/1/evil.html"])
def test_a_type_a_browser_would_execute_is_forced_to_download(resolver, key):
    """The same explicit ``attachment`` the single form sets.

    The bucket is a separate origin from the app, so this is defence in depth — but
    declining to say "inline" is not the same as saying "attachment": with no
    disposition the browser decides from the Content-Type alone and would render
    ``text/html``. A batch field that dropped this would be a quiet weakening of a
    control the single field has.
    """
    index, _ = resolver
    result = index.handler(_event([{"s3Uri": _uri(key)}]), None)
    query = {
        k.lower(): v
        for k, v in parse_qs(
            urlparse(result["results"][0]["presignedUrl"]).query
        ).items()
    }
    assert query["response-content-disposition"] == ["attachment"]


@pytest.mark.unit
def test_no_head_object_is_issued(resolver, monkeypatch):
    """The property the batch form exists for, asserted rather than described.

    Signing is local SigV4 and costs nothing per key over the network; a per-key
    ``head_object`` is the only per-key *network* cost in the operation, so N serial
    HEADs is what would have made a batch slow. Measured by making the call raise:
    if anything HEADs, this test fails with that error rather than passing.
    """
    index, _ = resolver

    def _refuse(*_args, **_kwargs):
        raise AssertionError("the batch field must not call head_object")

    monkeypatch.setattr(index.s3_client, "head_object", _refuse)
    result = index.handler(_event([{"s3Uri": _uri(_page(n))} for n in (1, 2, 3)]), None)
    assert len(result["results"]) == 3


@pytest.mark.unit
def test_a_missing_object_is_signed_rather_than_refused(resolver):
    """The consequence of skipping HEAD, stated as a test so it is a decision.

    The single form returns 400 "File not found" for an absent key because it HEADs
    first. This one cannot know, so it signs: the browser's fetch 404s and both
    consuming surfaces already handle that (a placeholder image, and a ``null``
    entry in the thumbnail map). Signing a URL for an object that is not there
    discloses nothing — the caller supplied the key and the fetch fails.
    """
    index, _ = resolver
    result = index.handler(
        _event([{"s3Uri": _uri(f"{DOC}/pages/999/image.jpg")}]), None
    )
    assert result["results"][0]["presignedUrl"].startswith("https://")


@pytest.mark.unit
def test_a_version_id_is_carried_per_entry(resolver):
    """A run manifest records a VersionId per key, so one per request is not enough."""
    index, _ = resolver
    result = index.handler(
        _event(
            [
                {"s3Uri": _uri(_page(1)), "versionId": "v-one"},
                {"s3Uri": _uri(_page(2))},
            ]
        ),
        None,
    )
    first, second = result["results"]
    assert parse_qs(urlparse(first["presignedUrl"]).query)["versionId"] == ["v-one"]
    assert "versionId" not in parse_qs(urlparse(second["presignedUrl"]).query)


# --------------------------------------------------------------------------- #
# the cap, enforced
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_the_cap_is_a_plausible_number(resolver):
    """The constant itself, bounded, so a mutation of it fails rather than hangs.

    Every test below derives its fixture size from ``MAX_BATCH_OBJECTS``, which is
    right — a literal would go stale — but it means a mutation raising the cap to a
    billion builds a billion-element list instead of failing. A test that cannot fail
    without exhausting the host is not a test, so the number is bounded here first.

    The upper bound is the response-size one: at ~1.9 KB per entry with a long session
    token, 2,000 entries is already over half the dispatcher's 6 MiB invoke response.
    The lower bound is the page count of a document anyone would open — the
    benchmarking pages describe 269-page documents, and a cap below that would make a
    full document view take more than one call for no reason.
    """
    index, _ = resolver
    assert 300 <= index.MAX_BATCH_OBJECTS <= 2000, (
        f"MAX_BATCH_OBJECTS is {index.MAX_BATCH_OBJECTS}. Too high and one call's "
        "response outgrows the dispatcher's 6 MiB synchronous invoke limit; too low "
        "and an ordinary document view needs several calls. If you move it, re-check "
        "test_the_cap_leaves_the_response_inside_the_dispatchers_limit and "
        "PRESIGN_BATCH_SIZE in src/ui/src/api/presign-batch.ts."
    )


@pytest.mark.unit
def test_a_list_longer_than_the_cap_is_refused(resolver):
    """Refused with a 400, and the cap is derived from the constant.

    **Mutation that fails it:** delete the ``len(files) > MAX_BATCH_OBJECTS`` branch
    from ``_batch_requests``.
    """
    index, _ = resolver
    files = [{"s3Uri": _uri(_page(1))}] * (index.MAX_BATCH_OBJECTS + 1)
    with pytest.raises(ValueError, match=r"more than the maximum"):
        index.handler(_event(files), None)


@pytest.mark.unit
def test_the_cap_is_not_a_truncation(resolver):
    """A list AT the cap returns a URL for every entry.

    Truncating instead of refusing is the quiet failure available here: the caller
    would get fewer URLs than it asked for, render blank pages, and see no error.
    Driven at exactly the cap so the boundary is the one asserted — an off-by-one in
    the comparison shows up here rather than in production.
    """
    index, _ = resolver
    files = [{"s3Uri": _uri(_page(1))}] * index.MAX_BATCH_OBJECTS
    result = index.handler(_event(files), None)
    assert len(result["results"]) == index.MAX_BATCH_OBJECTS


@pytest.mark.unit
def test_the_cap_leaves_the_response_inside_the_dispatchers_limit(resolver):
    """Measured, not assumed: a full batch must fit the 6 MiB invoke response.

    The cap is chosen against this number, so the number is checked rather than
    quoted. Serialised with the same ``json.dumps`` the dispatcher uses, so what is
    measured is the body that would actually be returned.
    """
    import json

    index, _ = resolver
    files = [
        {"s3Uri": _uri(f"{DOC}/pages/{n}/image.jpg")}
        for n in range(index.MAX_BATCH_OBJECTS)
    ]
    body = json.dumps(index.handler(_event(files), None))
    assert len(body) < 6 * 1024 * 1024, (
        f"a full batch serialises to {len(body)} bytes, which is over the "
        "dispatcher's 6 MiB synchronous invoke response limit. Lower "
        "MAX_BATCH_OBJECTS."
    )
    # Headroom, not just "fits": a real deployment's session token is longer than
    # moto's, and a URL grows byte for byte with it. Measured offline against
    # botocore, a 1,200-character token puts one entry at ~1.86 KB, so the cap is
    # chosen to leave room for that rather than to fit this fixture exactly.
    assert len(body) < 3 * 1024 * 1024


# --------------------------------------------------------------------------- #
# argument validation
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.parametrize(
    "files",
    [
        pytest.param([], id="empty"),
        pytest.param("not-a-list", id="scalar"),
        pytest.param(None, id="null"),
        pytest.param([None], id="null-entry"),
        pytest.param(["s3://bucket/key"], id="string-entry"),
        pytest.param([{}], id="entry-with-no-uri"),
        pytest.param([{"s3Uri": ""}], id="empty-uri"),
        pytest.param([{"s3Uri": 7}], id="non-string-uri"),
        pytest.param([{"s3Uri": "s3://b/k", "versionId": 7}], id="non-string-version"),
        pytest.param([{"s3Uri": "https://example.invalid/x"}], id="not-an-s3-uri"),
        pytest.param([{"s3Uri": "s3://bucket-only"}], id="no-key"),
    ],
)
def test_a_malformed_argument_is_a_bad_request(resolver, files):
    """``ValueError`` -> 400, never a 500 and never a partial success.

    The dispatcher's schema-derived validator checks that ``files`` is a list of
    objects and no deeper — ``validation._scalar_type_ok`` validates an input-object
    type shallowly by design — so every per-entry rule is this resolver's to apply,
    and an unvalidated entry would reach botocore and surface as an
    ``InternalError``.
    """
    index, _ = resolver
    with pytest.raises(ValueError):
        index.handler(_event(files), None)


@pytest.mark.unit
def test_the_bucket_allow_list_applies_to_every_entry(resolver):
    """Not only to the first one.

    A batch handler that validated once outside its loop and signed the rest would
    satisfy a static reference check, which is the specific caution recorded on this
    change. Driven with the out-of-allow-list bucket in LAST position so a
    first-entry-only check fails here.
    """
    index, _ = resolver
    with pytest.raises(PermissionError, match="^Unauthorized:"):
        index.handler(
            _event(
                [
                    {"s3Uri": _uri(_page(1))},
                    {"s3Uri": f"s3://{OTHER_BUCKET}/secret.json"},
                ]
            ),
            None,
        )


@pytest.mark.unit
def test_nothing_is_signed_when_any_entry_is_refused(resolver, monkeypatch):
    """Authorization runs over every entry before the first URL is minted.

    So there is no partial result to leak, and no way to read the number of URLs
    returned as a count of how many keys passed. Measured by failing the test if
    ``generate_presigned_url`` is reached at all on a request that must be refused.
    """
    index, _ = resolver

    def _refuse(*_args, **_kwargs):
        raise AssertionError("no URL may be minted for a request that is refused")

    monkeypatch.setattr(index.s3_client, "generate_presigned_url", _refuse)
    with pytest.raises(PermissionError, match="^Unauthorized:"):
        index.handler(
            _event(
                [
                    {"s3Uri": _uri(_page(1))},
                    {"s3Uri": f"s3://{OTHER_BUCKET}/secret.json"},
                ]
            ),
            None,
        )


@pytest.mark.unit
def test_the_field_name_selects_the_batch_handler_exactly(resolver):
    """``getFilePresignedUrl`` and ``getFilePresignedUrls`` differ by one character.

    A prefix or ``startswith`` dispatch would route the single field to the batch
    handler, which would read a ``files`` argument that is not there. Both are
    asserted in the same test so the pair cannot drift apart.
    """
    index, _ = resolver
    batch = index.handler(_event([{"s3Uri": _uri(_page(1))}]), None)
    assert "results" in batch

    single = index.handler(
        {
            "info": {"fieldName": "getFilePresignedUrl"},
            "arguments": {"s3Uri": _uri(_page(1))},
            "identity": {"claims": {"cognito:groups": ["Admin"]}},
        },
        None,
    )
    assert "presignedUrl" in single and "results" not in single
