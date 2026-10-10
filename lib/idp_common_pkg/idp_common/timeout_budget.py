# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The shard invocation's time budget: constants that are only correct together.

A sharded extraction runs one shard per Lambda invocation, capped at 900 seconds —
Lambda's maximum, so the budget cannot be widened. Several independent settings
spend it, and each is only meaningful relative to the others, so they are defined
here rather than at the call sites that use them (#1014).

⚠️ **The constants are sized for the work, and the LADDER is what is bounded.** The
first version of this module took the other route: it required

    BOTOCORE_TOTAL_MAX_ATTEMPTS * (AGENT_READ_TIMEOUT_SECONDS
                                   + CONFIDENCE_READ_TIMEOUT_SECONDS)
        + AGENT_MAX_TOTAL_BACKOFF_SECONDS
        + <room for the work itself>
    <= LAMBDA_MAX_TIMEOUT_SECONDS

and solved it by cutting the agentic read timeout from 600s to 180s. That number is
not free to choose: it is the longest gap a streamed generation may leave BETWEEN
events, and a real one exceeded it. The Nuveen agentic extraction (532 table rows,
17 page images, a ~52k-token cached prefix) reliably goes quiet for longer than 180s
at one point in its agent loop. Every attempt therefore ended in
``ReadTimeoutError``; the ladder resumed the same conversation, which stalled
identically; five attempts filled the 900s invocation, and it died on the wall clock
with ``Sandbox.Timedout`` — the one classification Step Functions does NOT retry
(``MaxAttempts: 1`` since #917) — so ``ExtractionShardMap`` discarded the sibling
shards too. That is the same loss #1014 set out to prevent, reached by shrinking the
timeout instead of by the stall it was aimed at (#1310). Measured: the identical
document and configuration passed at 600s and has failed every CI run since the cut.

So ``AGENT_READ_TIMEOUT_SECONDS`` is back at a value the work fits inside, and the
invariant that keeps the invocation safe lives in the retry ladder instead:
``utils.bedrock_utils._attempt_cannot_finish`` refuses to BEGIN an attempt the size
of the one that just failed when the remaining invocation cannot hold it, and raises
the underlying error. The handlers wrap that as ``TransientError``, which
``ExtractionStep``/``ShardExtractionStep`` retry eight times against a state-machine
budget of 21,600s. The bound is on elapsed time rather than on a sum of constants,
and it is self-calibrating: it needs no estimate of how long a request may take,
because it measures.

What the arithmetic still has to hold is weaker and is asserted in
``tests/unit/extraction/test_shard_timeout_budget.py``: ONE stall on the streamed
agentic client plus the whole backoff allowance must leave the invocation room to
return an error rather than be killed::

    AGENT_READ_TIMEOUT_SECONDS + AGENT_MAX_TOTAL_BACKOFF_SECONDS
        + <room to return>
    <= LAMBDA_MAX_TIMEOUT_SECONDS

A shard invocation can stall on **two** different Bedrock clients. Every term below
is still needed, because each is a real way to spend the invocation: what changed is
that their sum is no longer required to fit inside it.

* ``AGENT_READ_TIMEOUT_SECONDS`` — the STREAMED agentic call. Strands'
  ``BedrockModel`` streams by default and nothing here disables it, so this bounds
  a socket read BETWEEN events (time to first event, then each inter-event gap)
  rather than total generation time.

  ⚠️ **Do not reason about this number from "a healthy generation streams
  continuously".** That was the stated basis for 180s and it is wrong for an agent
  loop: a gap here is not only the model falling silent mid-sentence, it is also the
  whole turn between one tool result being submitted and the first event of the
  model's reply, on a request carrying a large cached prefix and a dozen-plus page
  images. Measured on ``samples/Nuveen.pdf``, that gap exceeds 180s reproducibly and
  fits inside 600s. Lower it only against a measurement of that gap, not against the
  arithmetic — see the warning at the top.
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
  inside a 900 s invocation. It is sized so that one agentic stall plus the whole
  allowance still leaves the invocation room to return.

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
AGENT_READ_TIMEOUT_SECONDS = 600.0
CONFIDENCE_READ_TIMEOUT_SECONDS = 300.0
AGENT_MAX_BACKOFF_SECONDS = 60.0
AGENT_MAX_TOTAL_BACKOFF_SECONDS = 90.0
BOTOCORE_TOTAL_MAX_ATTEMPTS = 1
