# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The shard invocation's time budget: constants that are only correct together.

A sharded extraction runs one shard per Lambda invocation, capped at 900 seconds —
Lambda's maximum, so the budget cannot be widened. Several independent settings
spend it, and each is only meaningful relative to the others, so they are defined
here rather than at the call sites that use them (#1014). The inequality they all
serve::

    BOTOCORE_TOTAL_MAX_ATTEMPTS * (AGENT_READ_TIMEOUT_SECONDS
                                   + CONFIDENCE_READ_TIMEOUT_SECONDS)
        + AGENT_MAX_TOTAL_BACKOFF_SECONDS
        + <room for the work itself>
    <= LAMBDA_MAX_TIMEOUT_SECONDS

A shard invocation can stall on **two** different Bedrock clients, and the worst
case is one stall on each plus the whole backoff allowance. Every term is needed:

* ``AGENT_READ_TIMEOUT_SECONDS`` — the STREAMED agentic call. Strands'
  ``BedrockModel`` streams by default and nothing here disables it, so this bounds
  a socket read BETWEEN events (time to first event, then each inter-event gap)
  rather than total generation time. A healthy long generation emits deltas
  continuously and never approaches it; what trips it is three minutes with no
  traffic at all, which is a stall by definition.
* ``CONFIDENCE_READ_TIMEOUT_SECONDS`` — the NON-streamed ``converse`` in
  ``bedrock/client.py``, which bounds the whole response rather than a gap and so
  is legitimately larger. It runs inside the same shard invocation whenever
  confidence is in ``separate`` mode: ``ExtractionService._build_assess_runner``
  hands ``extract_one_shard`` a closure over ``AssessmentService.assess_results``.
  (In ``integrated`` mode the extraction agent emits confidence inline, so only the
  streamed term applies — but the budget has to hold for both.)
* ``BOTOCORE_TOTAL_MAX_ATTEMPTS`` — botocore retries a read timeout ITSELF
  (``ReadTimeoutError`` subclasses ``HTTPClientError``, which botocore's
  ``TransientRetryableChecker`` lists as transient), so a client's own attempt
  count multiplies its read timeout INSIDE ONE await, where no application-level
  ladder and no deadline check can observe it. Every Bedrock client passes this
  constant, which is 1: retries belong to the deadline-aware ladder in
  ``utils.bedrock_utils``, not to a second, blind one underneath it.

  ⚠️ It is spelled ``total_max_attempts``, NOT ``max_attempts``, and the difference
  is off-by-one in the dangerous direction. In *client config* botocore's
  ``max_attempts`` means max **retries** and is normalised to
  ``total_max_attempts = max_attempts + 1``
  (``botocore.args.ClientArgsCreator._compute_retry_max_attempts``, which says so in
  its own comment). So ``max_attempts=1`` permits **two** attempts — one doubling of
  the read timeout — and ``max_attempts=7`` permits **eight**, not seven.
  ``total_max_attempts`` passes through verbatim and takes precedence over
  ``max_attempts``, so it is the only spelling that means what it says. The constant
  is named after that key on purpose.
* ``AGENT_MAX_TOTAL_BACKOFF_SECONDS`` — time the retry ladder may spend ASLEEP. It
  is the cheapest term to shrink, since sleeping makes no progress, and long waits
  belong to the state machine (whose execution budget is 21,600 s) rather than
  inside a 900 s invocation. It is sized as the largest value that keeps the
  inequality true with room for one complete call of the slowest kind.

At the original read timeout of 600 s the two-term version of this sum was exactly
900 and left nothing: a shard died on the wall clock, Step Functions read the
resulting ``Sandbox.Timedout`` as DETERMINISTIC (one attempt, by design — #917), so
the transient blip a retry would have cleared became the one failure not retried,
and ``ExtractionShardMap`` — which tolerates no shard failures — discarded the
sibling shards that had already succeeded along with it.

**Why this module imports nothing.** ``extraction.runtime`` takes its ``read_timeout``
defaults from here, and a default argument is evaluated when the module is imported,
so it cannot be deferred to first use. ``runtime`` is deliberately import-light at
module top (no strands, no PIL, no boto3) so the planning and persistence logic is
cheap to import and testable without the agentic stack — and importing anything from
``idp_common.utils`` would defeat that AND require an AWS region at import time,
because ``utils.settings_helper`` builds an SSM client while it executes. A leaf
module with no imports of its own is free to import from anywhere.
``tests/unit/test_import_surface_region_free.py`` holds that property.

⚠️ ONE EXPOSURE IS NOT BOUNDED BY THESE CONSTANTS. ``bedrock/client.py``'s
``_invoke_with_retry`` has its own application-level ladder — ``max_retries``
attempts, backing off ``initial_backoff`` doubling to ``max_backoff`` — which does
NOT consult ``get_lambda_deadline_epoch`` and so can overrun an invocation by itself
regardless of the arithmetic above. Making that ladder deadline-aware changes
behaviour for every non-agentic path (classification, simple extraction,
summarization, assessment), so it is deliberately out of scope; its nominal worst
case is pinned by ``tests/unit/extraction/test_shard_timeout_budget.py`` so it fails
if either of its numbers moves.
"""

from __future__ import annotations

LAMBDA_MAX_TIMEOUT_SECONDS = 900.0
AGENT_READ_TIMEOUT_SECONDS = 180.0
CONFIDENCE_READ_TIMEOUT_SECONDS = 300.0
AGENT_MAX_BACKOFF_SECONDS = 60.0
AGENT_MAX_TOTAL_BACKOFF_SECONDS = 90.0
BOTOCORE_TOTAL_MAX_ATTEMPTS = 1
