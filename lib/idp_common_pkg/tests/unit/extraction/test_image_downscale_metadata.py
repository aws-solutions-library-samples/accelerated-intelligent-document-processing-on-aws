# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#778: an oversize page image is fitted when the section's images are loaded,
the reduction is recorded for the section's metadata, and the record is reset
with the per-section context (not the per-invocation one, which runs AFTER the
images are loaded and would wipe it before _save_results reads it).

#994 extends the same load-time fit with Bedrock's many-image DIMENSION cap:
above 20 image blocks in one request every image is capped at 2,000 px per side.
Applying it here as well as in the Bedrock client keeps the reduction auditable
in section metadata and covers the agentic (Strands) path, which builds its own
requests and so bypasses ``BedrockClient.invoke_model``."""

import io
import random
from unittest.mock import patch

import pytest
from PIL import Image

from idp_common.extraction.service import ExtractionService
from idp_common.extraction.sharding import DEFAULT_SHARD_TOKEN_BUDGET, plan_shards
from idp_common.image import (
    BEDROCK_IMAGE_MAX_ENCODED_BYTES,
    BEDROCK_MANY_IMAGE_MAX_DIMENSION,
    base64_encoded_size,
)
from idp_common.models import Document, Page, Status

pytestmark = pytest.mark.unit


def _oversize_png() -> bytes:
    rng = random.Random(5)
    w = h = 1180  # ~4.2 MB raw PNG: under 5 MiB, over the 3.75 MiB raw budget
    img = Image.frombytes(
        "RGB", (w, h), bytes(rng.getrandbits(8) for _ in range(w * h * 3))
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _small_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (400, 500), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def service():
    return ExtractionService(
        region="us-west-2",
        config={
            "classes": [],
            "extraction": {"model": "us.anthropic.claude-sonnet-4-6"},
        },
    )


@pytest.fixture
def document():
    doc = Document(
        id="d",
        input_key="d.pdf",
        input_bucket="in",
        output_bucket="out",
        status=Status.EXTRACTING,
    )
    for pid in ("1", "2"):
        doc.pages[pid] = Page(
            page_id=pid,
            image_uri=f"s3://in/d.pdf/pages/{pid}/image.png",
            parsed_text_uri=f"s3://in/d.pdf/pages/{pid}/parsed.txt",
        )
    return doc


def test_oversize_page_is_fitted_and_recorded_per_page(service, document):
    big, small = _oversize_png(), _small_png()
    assert base64_encoded_size(len(big)) > BEDROCK_IMAGE_MAX_ENCODED_BYTES
    with patch("idp_common.image.prepare_image", side_effect=[big, small]):
        images = service._load_document_images(document, ["1", "2"])

    assert len(images) == 2
    assert base64_encoded_size(len(images[0])) <= BEDROCK_IMAGE_MAX_ENCODED_BYTES
    assert images[1] is small, "an in-budget page passes through byte-identical"

    pending = service._pending_image_fit_metadata
    assert pending is not None and len(pending) == 1
    entry = pending[0]
    assert entry["page_id"] == "1"
    assert entry["original_bytes"] == len(big)
    assert entry["final_encoded_bytes"] <= BEDROCK_IMAGE_MAX_ENCODED_BYTES
    assert entry["passes"] >= 1 and "encoded size" in entry["reason"]


def test_no_entry_when_every_page_fits(service, document):
    with patch("idp_common.image.prepare_image", return_value=_small_png()):
        service._load_document_images(document, ["1", "2"])
    assert service._pending_image_fit_metadata is None


def _wide_png(size=(1585, 2048)) -> bytes:
    """A flat page just over the 2,000 px many-image cap and well under the
    byte budget, so only the dimension rule can trigger a fit."""
    buf = io.BytesIO()
    Image.new("RGB", size, (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _doc_with_pages(count: int) -> Document:
    doc = Document(
        id="d",
        input_key="d.pdf",
        input_bucket="in",
        output_bucket="out",
        status=Status.EXTRACTING,
    )
    for i in range(1, count + 1):
        doc.pages[str(i)] = Page(
            page_id=str(i),
            image_uri=f"s3://in/d.pdf/pages/{i}/image.png",
            parsed_text_uri=f"s3://in/d.pdf/pages/{i}/parsed.txt",
        )
    return doc


def _page_dimensions(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as img:
        return img.size


def test_a_section_over_twenty_pages_is_clamped_to_the_many_image_cap(service):
    """21 pages means 21 image blocks in one request, which drops the per-image
    cap to 2,000 px — each page is individually legal, so only the count reveals
    it (#994)."""
    doc = _doc_with_pages(21)
    page_ids = [str(i) for i in range(1, 22)]
    with patch("idp_common.image.prepare_image", return_value=_wide_png()):
        images = service._load_document_images(doc, page_ids)

    assert len(images) == 21
    assert all(
        max(_page_dimensions(img)) <= BEDROCK_MANY_IMAGE_MAX_DIMENSION for img in images
    )
    pending = service._pending_image_fit_metadata
    assert pending is not None and len(pending) == 21
    assert "px exceeds 2000 px per side" in pending[0]["reason"]


def test_a_twenty_page_section_keeps_full_resolution(service):
    """At exactly 20 the stricter cap does not apply, so nothing is degraded."""
    doc = _doc_with_pages(20)
    page_ids = [str(i) for i in range(1, 21)]
    with patch("idp_common.image.prepare_image", return_value=_wide_png()):
        images = service._load_document_images(doc, page_ids)

    assert all(_page_dimensions(img) == (1585, 2048) for img in images)
    assert service._pending_image_fit_metadata is None


def test_missing_pages_do_not_count_toward_the_threshold(service):
    """``sorted_page_ids`` can name pages the document does not have; those are
    skipped, so they must not push the count over the threshold and clamp the
    pages that ARE sent."""
    doc = _doc_with_pages(5)
    page_ids = [str(i) for i in range(1, 30)]  # 24 of them are absent
    with patch("idp_common.image.prepare_image", return_value=_wide_png()):
        images = service._load_document_images(doc, page_ids)

    assert len(images) == 5
    assert all(_page_dimensions(img) == (1585, 2048) for img in images)


def _agentic_service(**agentic_overrides) -> ExtractionService:
    return ExtractionService(
        region="us-west-2",
        config={
            "classes": [],
            "extraction": {
                "model": "us.anthropic.claude-sonnet-4-6",
                "agentic": {"enabled": True, **agentic_overrides},
            },
        },
    )


def _load(
    service: ExtractionService, pages: int, page_texts: list[str] | None = None
) -> list[bytes]:
    doc = _doc_with_pages(pages)
    with patch("idp_common.image.prepare_image", return_value=_wide_png()):
        return service._load_document_images(
            doc, [str(i) for i in range(1, pages + 1)], page_texts=page_texts
        )


def _uniform_texts(pages: int) -> list[str]:
    """Equal OCR volume per page — enough text that the page ceiling, not the
    token budget, is what closes a shard."""
    return ["x" * 4000] * pages


def test_agentic_mode_halves_the_effective_page_threshold():
    """Strands re-sends the attached pages each turn and ``view_image`` adds a
    second copy of a page to the same request, so a single agent invocation
    carrying 11 pages can present 22 image blocks. With sharding off
    (``max_concurrent_batches: 1``) the whole section goes to one agent, so the
    threshold lands at 11 pages rather than 21. Clamping early costs ~15% of the
    image tokens; not clamping costs the whole request.

    Note this is NOT the shipped default — `base-extraction.yaml` and the UI
    schema both ship ``max_concurrent_batches: 10``. See
    ``test_the_shipped_default_clamps_only_on_very_long_sections``.
    """
    svc = _agentic_service(max_concurrent_batches=1)
    images = _load(svc, 11, _uniform_texts(11))
    assert all(
        max(_page_dimensions(img)) <= BEDROCK_MANY_IMAGE_MAX_DIMENSION for img in images
    )


def test_agentic_mode_counts_what_one_request_carries_not_the_whole_section():
    """The count that matters is per REQUEST. At ``max_concurrent_batches: 4`` a
    12-page section is sent as shards of at most 5 pages — 10 blocks after
    doubling, under the threshold. Counting the section instead would downscale
    all 12 pages for a limit no request comes close to."""
    svc = _agentic_service(max_concurrent_batches=4, max_pages_per_shard=5)
    assert svc._agentic_images_per_request(12, _uniform_texts(12)) == 5
    images = _load(svc, 12, _uniform_texts(12))
    assert len(images) == 12
    assert all(_page_dimensions(img) == (1585, 2048) for img in images)


def test_the_estimate_is_the_real_planner_not_arithmetic_on_the_page_cap():
    """``max_pages_per_shard`` bounds neither the estimate nor the outcome.
    ``plan_shards`` closes a shard at that many pages, but when that would produce
    more shards than ``max_concurrent_batches``, ``_rebalance_to_cap`` discards
    those ranges and repacks into exactly that many TOKEN-balanced groups, ignoring
    the page cap. So a 30-page section at ``max_concurrent_batches: 2`` really goes
    out as two 15-page requests — 30 blocks after doubling, over the threshold.

    Asserted against ``plan_shards`` itself rather than against a number in this
    test, because two successive review passes accepted a closed form that the
    planner does not honour: first ``min(pages, max_pages_per_shard)``, then
    ``ceil(pages / max_concurrent_batches)``."""
    svc = _agentic_service(max_concurrent_batches=2, max_pages_per_shard=5)
    texts = _uniform_texts(30)
    planned = plan_shards(
        texts,
        token_budget=DEFAULT_SHARD_TOKEN_BUDGET,
        max_shards=2,
        max_pages_per_shard=5,
    )
    assert max(s.page_count for s in planned) == 15
    assert svc._agentic_images_per_request(30, texts) == 15
    images = _load(svc, 30, texts)
    assert all(
        max(_page_dimensions(img)) <= BEDROCK_MANY_IMAGE_MAX_DIMENSION for img in images
    )


def test_a_skewed_page_text_distribution_is_still_bounded():
    """The rebalance balances estimated TOKENS, so one dense page can take a whole
    shard and leave the rest crowded into another. No arithmetic over page counts
    predicts that, which is why the planner is asked. The estimate must still be an
    upper bound on what the largest shard attaches."""
    svc = _agentic_service(max_concurrent_batches=10, max_pages_per_shard=5)
    texts = ["x" * 400_000] + ["x" * 200] * 59
    planned = plan_shards(
        texts,
        token_budget=DEFAULT_SHARD_TOKEN_BUDGET,
        max_shards=10,
        max_pages_per_shard=5,
    )
    largest = min(max(s.page_count for s in planned), 20)  # _cap_agent_images
    assert svc._agentic_images_per_request(60, texts) >= largest


def test_the_page_ceiling_being_disabled_does_not_lose_the_clamp():
    """``max_pages_per_shard: 0`` is supported and documented as "page cap off".
    With it off, a section whose text fits one budget becomes ONE shard holding
    every page, so the request attaches ``max_images_per_agent`` of them. An
    estimate derived from ``ceil(pages / max_concurrent_batches)`` said 3 here and
    dropped a clamp the pre-#994 code applied."""
    svc = _agentic_service(max_concurrent_batches=10, max_pages_per_shard=0)
    texts = ["x" * 50] * 30  # compact: the token budget alone never splits these
    assert svc._agentic_images_per_request(30, texts) == 20
    images = _load(svc, 30, texts)
    assert all(
        max(_page_dimensions(img)) <= BEDROCK_MANY_IMAGE_MAX_DIMENSION for img in images
    )


def test_the_bound_holds_where_the_token_budget_changes_the_plan():
    """The regime that broke the previous two attempts.

    ``plan_shards`` has two regimes and the token budget decides WHICH one runs:
    a smaller budget produces more first-pass ranges, and once that count exceeds
    ``max_concurrent_batches`` the ranges are discarded and ``_rebalance_to_cap``
    repacks by token weight, at which point ``max_pages_per_shard`` no longer
    holds at all. Measured on the shipped default with a 50-page section holding
    one dense page: an unbounded budget plans ten 5-page shards, Sonnet 4.6's own
    derived budget of 18,400 tokens plans ``[1, 41, 1, ...]``. An estimate taken
    from a single (large) budget said 5 and missed a clamp the request needs.

    So the bound is the WORSE of the two regimes, which needs no budget. This test
    lives in the divergence regime deliberately — every other test here uses page
    text light enough that the page cap closes the shards, where the budget cannot
    matter, which is exactly why they all passed while the estimate was unsound.
    """
    svc = _agentic_service(max_concurrent_batches=10, max_pages_per_shard=5)
    texts = ["x" * 80_000] + ["x" * 200] * 49

    under_huge_budget = plan_shards(
        texts, token_budget=1 << 40, max_shards=10, max_pages_per_shard=5
    )
    under_real_budget = plan_shards(
        texts, token_budget=18_400, max_shards=10, max_pages_per_shard=5
    )
    assert max(s.page_count for s in under_huge_budget) == 5
    assert max(s.page_count for s in under_real_budget) == 41, (
        "fixture no longer reaches the repack regime"
    )

    # The bound must cover the real plan, not the unbounded-budget one.
    est = svc._agentic_images_per_request(50, texts)
    assert est == 20  # 41 pages, truncated by max_images_per_agent
    images = _load(svc, 50, texts)
    assert all(
        max(_page_dimensions(img)) <= BEDROCK_MANY_IMAGE_MAX_DIMENSION for img in images
    )


def test_the_bound_covers_every_budget_the_service_can_derive():
    """Cross-check rather than a fixed expectation: for a spread of page-text
    distributions and every shard budget the sizing code can produce for a shipped
    model, the estimate must be at least what the largest shard would attach."""
    svc = _agentic_service(max_concurrent_batches=10, max_pages_per_shard=5)
    dists = {
        "uniform": lambda n: ["x" * 4000] * n,
        "compact": lambda n: ["x" * 50] * n,
        "one dense": lambda n: ["x" * 80_000] + ["x" * 200] * (n - 1),
        "two dense": lambda n: ["x" * 80_000, "x" * 80_000] + ["x" * 200] * (n - 2),
    }
    # 18,400 = Sonnet 4.6, 63,200 = Haiku 4.5, 171,000 = Nova Lite.
    budgets = (4_000, 8_000, 18_400, 63_200, 171_000)
    for name, make in dists.items():
        for pages in (12, 30, 50, 60, 101):
            texts = make(pages)
            est = svc._agentic_images_per_request(pages, texts)
            for budget in budgets:
                planned = plan_shards(
                    texts, token_budget=budget, max_shards=10, max_pages_per_shard=5
                )
                attached = min(max(s.page_count for s in planned), 20)
                assert est >= attached, (
                    f"{name} n={pages} budget={budget}: estimate {est} < "
                    f"attached {attached}"
                )


def test_without_page_texts_the_whole_section_is_assumed():
    """A caller that does not supply the texts cannot be sharded-for, so the
    conservative answer is the whole section (capped by max_images_per_agent)."""
    svc = _agentic_service(max_concurrent_batches=10, max_pages_per_shard=5)
    assert svc._agentic_images_per_request(30, None) == 20


def test_the_shipped_default_clamps_only_on_very_long_sections():
    """`base-extraction.yaml` and the UI schema ship ``max_concurrent_batches: 10``
    with ``max_pages_per_shard: 5``, so up to 50 pages the shards hold 5 and beyond
    that the rebalance gives about ``pages / 10``. The clamp therefore engages at
    about 101 pages, not 11 — the figure three earlier drafts of the docs printed
    as "the default"."""
    svc = _agentic_service(max_concurrent_batches=10, max_pages_per_shard=5)
    assert svc._agentic_images_per_request(50, _uniform_texts(50)) == 5
    assert svc._agentic_images_per_request(100, _uniform_texts(100)) == 10
    assert svc._agentic_images_per_request(101, _uniform_texts(101)) == 11
    images = _load(svc, 100, _uniform_texts(100))
    assert all(_page_dimensions(img) == (1585, 2048) for img in images)


def test_the_per_agent_image_cap_also_bounds_the_count():
    """``max_images_per_agent`` caps how many pages are attached to one
    invocation, so a section far above the threshold still only ever presents that
    many attached blocks. At a cap of 4 (8 after doubling) nothing is clamped."""
    svc = _agentic_service(max_concurrent_batches=1, max_images_per_agent=4)
    images = _load(svc, 40, _uniform_texts(40))
    assert len(images) == 40
    assert all(_page_dimensions(img) == (1585, 2048) for img in images)


def test_reset_context_clears_the_record_for_the_next_section(service, document):
    with patch("idp_common.image.prepare_image", return_value=_oversize_png()):
        service._load_document_images(document, ["1"])
    assert service._pending_image_fit_metadata
    service._reset_context()
    assert service._pending_image_fit_metadata is None
