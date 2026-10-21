# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The batch presign cap, read from both sides of the API.

``getFilePresignedUrls`` takes a caller-supplied list, so the resolver caps its length
(``MAX_BATCH_OBJECTS``) and **refuses** a longer one rather than truncating it — a
truncated list returns fewer URLs than asked for and the browser renders blank pages
with no error. The web UI therefore chunks its own requests, at
``PRESIGN_BATCH_SIZE``.

**Two pairs of numbers in two languages**, and in both pairs a disagreement is
invisible to every suite on either side of the boundary:

* the **cap**: a UI chunk larger than the server cap means every page-image load in
  the product is refused. The resolver's suite proves the cap is enforced; the UI's
  proves the chunking happens; neither reads the other's source.
* the **lifetime**: the batch field mints URLs a document view uses over minutes —
  the first page's image is fetched at once, the rest when the user navigates — so
  the UI re-signs when its map is older than a refresh threshold. A threshold at or
  above the server's expiry means a URL can be judged fresh and expire before it is
  fetched, which is the exact breakage the re-sign was added to fix, reappearing as
  a race instead of a cliff.

**Both values are read out of the files that declare them**, not restated here. A test
that restated either would be a double encoding a belief about the other layer instead
of measuring it, which is the defect class this repository names most often. The
assertions are therefore on the *relation* between the two, and on each file still
declaring the constant the relation is about — a regex that stops matching reads as a
missing constant and fails, rather than skipping.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

RESOLVER_INDEX = (
    REPO_ROOT / "nested/api-resolvers/src/lambda/get_file_contents_resolver/index.py"
)
UI_PRESIGN_BATCH = REPO_ROOT / "src/ui/src/api/presign-batch.ts"

#: ``MAX_BATCH_OBJECTS = 1000`` at module scope in the resolver.
_SERVER_CAP_RE = re.compile(r"^MAX_BATCH_OBJECTS\s*=\s*(\d+)\s*$", re.M)
#: ``_PRESIGNED_URL_EXPIRY_SECONDS = 300`` at module scope in the resolver.
_SERVER_EXPIRY_RE = re.compile(r"^_PRESIGNED_URL_EXPIRY_SECONDS\s*=\s*(\d+)\s*$", re.M)
#: ``export const PRESIGN_REFRESH_AFTER_MS = 240_000;`` in the UI module. The
#: underscore separator is JavaScript's numeric literal separator and is stripped
#: before the comparison — reading ``240`` out of ``240_000`` would make this gate
#: pass on a threshold a thousand times too small, which is the failing-quiet
#: direction.
_UI_REFRESH_RE = re.compile(
    r"^export const PRESIGN_REFRESH_AFTER_MS\s*=\s*([\d_]+);\s*$", re.M
)
#: ``export const PRESIGN_BATCH_SIZE = 250;`` in the UI module.
_UI_CHUNK_RE = re.compile(r"^export const PRESIGN_BATCH_SIZE\s*=\s*(\d+);\s*$", re.M)


def _server_cap() -> int:
    match = _SERVER_CAP_RE.search(RESOLVER_INDEX.read_text())
    assert match, (
        f"{RESOLVER_INDEX.relative_to(REPO_ROOT)} no longer declares "
        "MAX_BATCH_OBJECTS as a module-scope integer literal, so this gate cannot "
        "read the server's cap. Either it moved — point this at it — or the batch "
        "field stopped being capped, which is the thing this file exists to notice."
    )
    return int(match.group(1))


def _ui_chunk() -> int:
    match = _UI_CHUNK_RE.search(UI_PRESIGN_BATCH.read_text())
    assert match, (
        f"{UI_PRESIGN_BATCH.relative_to(REPO_ROOT)} no longer declares "
        "`export const PRESIGN_BATCH_SIZE = <n>;` on one line, so this gate cannot "
        "read the UI's chunk size. Keep the one-line form — the comment beside it "
        "says so — or update this pattern deliberately."
    )
    return int(match.group(1))


def _server_expiry_seconds() -> int:
    match = _SERVER_EXPIRY_RE.search(RESOLVER_INDEX.read_text())
    assert match, (
        f"{RESOLVER_INDEX.relative_to(REPO_ROOT)} no longer declares "
        "_PRESIGNED_URL_EXPIRY_SECONDS as a module-scope integer literal, so this "
        "gate cannot read how long a presigned URL lives — and the UI's refresh "
        "threshold is only meaningful relative to it."
    )
    return int(match.group(1))


def _ui_refresh_seconds() -> float:
    match = _UI_REFRESH_RE.search(UI_PRESIGN_BATCH.read_text())
    assert match, (
        f"{UI_PRESIGN_BATCH.relative_to(REPO_ROOT)} no longer declares "
        "`export const PRESIGN_REFRESH_AFTER_MS = <n>;` on one line, so this gate "
        "cannot read when the UI re-signs. Keep the one-line form — the comment "
        "beside it says so."
    )
    return int(match.group(1).replace("_", "")) / 1000.0


def test_both_files_declare_their_constant():
    """Non-vacuity. Every assertion below is read from these two files."""
    assert RESOLVER_INDEX.is_file(), RESOLVER_INDEX
    assert UI_PRESIGN_BATCH.is_file(), UI_PRESIGN_BATCH
    assert _server_cap() > 0
    assert _ui_chunk() > 0
    assert _server_expiry_seconds() > 0
    assert _ui_refresh_seconds() > 0


def test_the_ui_never_sends_more_than_the_resolver_accepts():
    """The relation that matters, and the only one that can break the product.

    A UI chunk larger than the server cap means every batched page-image request is
    refused with a 400 — so every document view in the product loads no images, and
    the two suites that cover these modules both stay green, because each measures its
    own side of the boundary.
    """
    server, ui = _server_cap(), _ui_chunk()
    assert ui <= server, (
        f"the web UI chunks presign requests at {ui} objects but the resolver refuses "
        f"a list longer than {server}, so every batched page-image request would be "
        "answered 400 and no document view would load its images. Lower "
        "PRESIGN_BATCH_SIZE in src/ui/src/api/presign-batch.ts, or raise "
        "MAX_BATCH_OBJECTS in the resolver — and if you raise it, re-check the "
        "response-size assertion in "
        "get_file_contents_resolver/test_batch_presign.py, which is what bounds it "
        "against the dispatcher's 6 MiB invoke response."
    )


def test_the_ui_chunk_leaves_headroom_rather_than_sitting_on_the_cap():
    """Equal values are legal and fragile, so the margin is asserted separately.

    At ``ui == server`` any future off-by-one on either side — a chunk built with
    ``<=`` instead of ``<``, a cap compared with ``>=`` instead of ``>`` — turns into
    a total failure of the page-image path rather than into a slightly smaller batch.
    The margin costs one extra round trip on a document with more pages than the
    chunk, which no document in ``samples/`` has (the largest is 27 pages).

    This is advisory in the sense that it is not a correctness requirement — which is
    why it is a separate test with its own name, so a deliberate decision to sit on
    the cap fails one assertion and not the one above it.
    """
    server, ui = _server_cap(), _ui_chunk()
    assert ui <= server // 2, (
        f"PRESIGN_BATCH_SIZE ({ui}) is more than half MAX_BATCH_OBJECTS ({server}). "
        "Keep a margin: at the cap, an off-by-one in either the chunker or the cap "
        "check breaks every page-image load instead of making one batch smaller."
    )


def test_the_ui_re_signs_before_a_url_can_expire():
    """The lifetime pair, and the reason it needs a margin rather than equality.

    The batch field mints a document view's worth of URLs at once, and most of them
    are fetched later — when the user navigates to that page. So the UI re-signs when
    its map is older than ``PRESIGN_REFRESH_AFTER_MS``. If that threshold reached the
    server's expiry, a URL could be judged fresh and expire between the check and the
    fetch: the same broken image the re-sign exists to prevent, reappearing as a race
    instead of as a cliff at five minutes.

    A margin rather than a strict inequality, because "fresh" has to survive the
    round trip that follows it — the GraphQL call, the browser's own image request,
    and a slow connection. One sixth of the lifetime is the floor asserted here;
    at the shipped values that is 60 s of 300.
    """
    expiry = _server_expiry_seconds()
    refresh = _ui_refresh_seconds()
    assert refresh < expiry, (
        f"the UI re-signs after {refresh}s but a presigned URL lives {expiry}s, so a "
        "URL it judges fresh may already be dead. Lower PRESIGN_REFRESH_AFTER_MS in "
        "src/ui/src/api/presign-batch.ts, or raise _PRESIGNED_URL_EXPIRY_SECONDS in "
        "the resolver — and note that raising it lengthens a bearer capability's life "
        "for every consumer of the single-object field too, which is a security cost "
        "paid for a UX problem."
    )
    margin = expiry - refresh
    assert margin >= expiry / 6, (
        f"the refresh threshold leaves only {margin}s of a {expiry}s lifetime. That "
        "has to cover the re-sign round trip and the browser's own image fetch after "
        "it, on a slow connection — a margin this thin turns the five-minute cliff "
        "into an intermittent one, which is harder to recognise rather than better."
    )


def test_the_ui_comment_points_at_the_resolver_constant():
    """So the next reader of either file can find the other.

    The relation above is enforced here and nowhere else, so the only thing that tells
    somebody editing one number that the other exists is the comment. Checked as a
    literal mention of the resolver's constant name, which is the cheapest thing that
    cannot be true by accident.
    """
    text = UI_PRESIGN_BATCH.read_text()
    assert "MAX_BATCH_OBJECTS" in text, (
        f"{UI_PRESIGN_BATCH.relative_to(REPO_ROOT)} does not mention "
        "MAX_BATCH_OBJECTS, so nothing in the file says the number has a server-side "
        "counterpart or where it lives."
    )
    assert "get_file_contents_resolver" in text, (
        "the comment should name the resolver module holding the cap, not only the "
        "constant"
    )
    assert "_PRESIGNED_URL_EXPIRY_SECONDS" in text, (
        f"{UI_PRESIGN_BATCH.relative_to(REPO_ROOT)} does not mention "
        "_PRESIGNED_URL_EXPIRY_SECONDS, so nothing in the file says the refresh "
        "threshold is relative to a server-side lifetime or where that lives."
    )
