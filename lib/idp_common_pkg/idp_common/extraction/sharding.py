# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Token-budgeted page sharding for concurrent agentic extraction.

When ``extraction.agentic.max_concurrent_batches > 1`` the extraction service
splits a section's pages across several agents that run concurrently. The
original implementation handed every agent the *whole* document and merely told
it (in text) to "ignore the other pages" — so each agent still loaded the full
context and could overflow the model's context window (the failure mode that
prompted this module). This planner instead computes **page ranges sized to a
token budget**, so each shard's input actually fits.

Design (see PR discussion):
- **Size-bounded, not count-bounded.** Pages are grouped so each shard's
  estimated input tokens stay under ``token_budget``. ``max_concurrent_batches``
  is an *upper bound on parallelism*, not the number of shards: a very large
  section produces as many shards as needed to fit, run at most
  ``max_concurrent_batches`` at a time.
- **Page-aligned splits never cut a table row.** A table spans pages but each
  *row* lives on one page, so splitting between pages keeps rows intact; the
  merge re-concatenates list fields in order.
- **Opportunistic table-boundary preference (forward hook).** ``plan_shards``
  accepts ``table_boundary_pages`` and, when given, prefers to close a shard
  just before such a page (within budget — never grown past it). This parameter
  is implemented and tested but the service does not yet feed it real OCR
  boundaries (it passes ``None``); page-aligned splitting is already row-safe
  on its own, so this is a refinement to wire up later, not a correctness need.

This module is pure (no PIL/Strands/boto3) so it is cheap to import and unit-test.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Rough chars-per-token estimate, matching idp_common.utils.check_token_limit.
# Deliberately conservative (English prose ~4 chars/token; dense tabular/numeric
# text can be lower, so this *under*-counts tokens slightly — we compensate with
# a conservative default budget).
_CHARS_PER_TOKEN = 4.0

# Default per-shard input-token budget. Chosen low enough that a large document
# reliably shards into several agents *by default* (no per-config tuning). The
# previous default (40K) was high enough that even a ~25-page dense table fit a
# single shard, so sharding silently did NOT engage and one agent had to emit a
# giant table in one Bedrock call -> read timeout. 8000 is proven in scale tests
# to shard a 25-page doc into ~5 shards and complete in both modes. Models with
# larger windows can raise this via ``extraction.agentic.shard_token_budget``.
DEFAULT_SHARD_TOKEN_BUDGET = 8_000

# Default per-shard page ceiling. Even when OCR text is unusually compact and a
# big page range fits under the token budget, cap each shard at this many pages
# so large documents ALWAYS shard on page count. This makes "tokens AND pages
# both bound a shard" — guaranteeing parallelism (and bounded per-agent work)
# regardless of text density. 0 disables the page ceiling (token budget only).
DEFAULT_MAX_PAGES_PER_SHARD = 5


@dataclass
class Shard:
    """A contiguous page range assigned to one extraction agent.

    ``start`` and ``end`` are 0-based indices into the section's
    ``sorted_page_ids`` (``end`` exclusive), matching Python slice semantics.
    """

    index: int
    start: int
    end: int  # exclusive

    @property
    def page_count(self) -> int:
        return self.end - self.start


def estimate_tokens(text: str) -> int:
    """Estimate token count for a text blob (chars/4 heuristic)."""
    return int(len(text) / _CHARS_PER_TOKEN)


def plan_shards(
    page_texts: list[str],
    *,
    token_budget: int = DEFAULT_SHARD_TOKEN_BUDGET,
    max_shards: int | None = None,
    max_pages_per_shard: int | None = DEFAULT_MAX_PAGES_PER_SHARD,
    table_boundary_pages: frozenset[int] | None = None,
) -> list[Shard]:
    """Group pages into token-budgeted contiguous shards.

    Args:
        page_texts: OCR text per page, in section page order.
        token_budget: Target maximum estimated input tokens of page text per
            shard. A single page that alone exceeds the budget still becomes its
            own shard (we never split mid-page).
        max_shards: Optional hard cap on the number of shards. When the first
            pass would produce more, its ranges are DISCARDED and the pages are
            repacked into exactly ``max_shards`` **token-balanced** groups (the
            caller's parallelism cap doubles as a shard cap so we don't fan out
            unboundedly). ``None`` means no cap. Note what this implies: on that
            path ``max_pages_per_shard`` no longer holds, and because the repack
            balances estimated tokens rather than page counts, one text-heavy page
            can occupy a shard alone while sparse pages crowd into another — so
            neither the page cap nor ``ceil(pages / max_shards)`` bounds the
            largest shard's page count. Callers that need that number must ask
            for the plan (see ``ExtractionService._agentic_images_per_request``,
            which sizes Bedrock's many-image cap from it).
        max_pages_per_shard: Page-count ceiling per shard. A shard is closed
            once it holds this many pages even if its text is still under the
            token budget — so a document with unusually compact pages still
            shards on page count. ``None`` or ``<= 0`` disables the page ceiling
            (token budget alone bounds shards). Applied alongside the token
            budget: a shard closes when EITHER bound is hit.
        table_boundary_pages: 0-based indices of pages that *start* a new table
            (from OCR analysis). When provided, the packer prefers to close a
            shard just before such a page rather than mid-table, as long as the
            shard already holds at least one page.

    Returns:
        Ordered list of ``Shard``. Always covers every page exactly once with
        no gaps or overlaps. Returns a single shard for an empty/one-page input.
    """
    n = len(page_texts)
    if n <= 1:
        return (
            [Shard(index=0, start=0, end=max(n, 1) if n else 1)]
            if n
            else [Shard(index=0, start=0, end=0)]
        )

    boundaries = table_boundary_pages or frozenset()
    page_cap = max_pages_per_shard if (max_pages_per_shard or 0) > 0 else None

    # First pass: greedily pack pages until adding the next page would exceed
    # the token budget OR the shard already holds ``max_pages_per_shard`` pages
    # (but always keep at least one page per shard).
    ranges: list[tuple[int, int]] = []
    start = 0
    running = 0
    for i in range(n):
        page_tokens = estimate_tokens(page_texts[i])
        is_first_in_shard = i == start
        pages_in_shard = i - start
        would_exceed = running + page_tokens > token_budget and not is_first_in_shard
        hit_page_cap = page_cap is not None and pages_in_shard >= page_cap
        prefer_break_here = i in boundaries and not is_first_in_shard and running > 0
        if would_exceed or hit_page_cap or prefer_break_here:
            ranges.append((start, i))
            start = i
            running = page_tokens
        else:
            running += page_tokens
    ranges.append((start, n))

    # Second pass: enforce max_shards. This does NOT merge the first pass's
    # ranges — _rebalance_to_cap throws them away and repacks from page 0 by
    # token weight, so max_pages_per_shard does not survive it. Nor is it rare:
    # at the shipped defaults (max_concurrent_batches 10, max_pages_per_shard 5)
    # it fires on every section over 50 pages.
    if max_shards is not None and max_shards >= 1 and len(ranges) > max_shards:
        ranges = _rebalance_to_cap(page_texts, n, max_shards)

    return [Shard(index=idx, start=s, end=e) for idx, (s, e) in enumerate(ranges)]


def _rebalance_to_cap(
    page_texts: list[str], n: int, max_shards: int
) -> list[tuple[int, int]]:
    """Redistribute n pages into exactly ``max_shards`` token-balanced ranges.

    Greedy: target ~equal tokens per shard. Guarantees no empty shards when
    ``max_shards <= n`` (each shard gets at least one page).
    """
    max_shards = min(max_shards, n)
    total = sum(estimate_tokens(t) for t in page_texts)
    target = total / max_shards if max_shards else total

    ranges: list[tuple[int, int]] = []
    start = 0
    running = 0.0
    for i in range(n):
        running += estimate_tokens(page_texts[i])
        shards_done = len(ranges)
        pages_left = n - (i + 1)
        shards_left_after = max_shards - shards_done - 1
        # Close this shard if we've hit the per-shard target AND leaving enough
        # pages for the remaining shards to each get one, OR we must close now
        # so every remaining shard still gets a page.
        must_close = pages_left == shards_left_after
        hit_target = running >= target and shards_left_after > 0
        if (hit_target or must_close) and shards_done < max_shards - 1:
            ranges.append((start, i + 1))
            start = i + 1
            running = 0.0
    ranges.append((start, n))
    return ranges
