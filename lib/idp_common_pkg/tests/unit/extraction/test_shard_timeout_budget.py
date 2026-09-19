# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The shard invocation's time budget has to add up, and nothing checked that.

Three numbers draw on the same 900 seconds: how long ONE Bedrock request may
stall (``AGENT_READ_TIMEOUT_SECONDS``), how much total backoff the retry ladder
may spend (``AGENT_MAX_TOTAL_BACKOFF_SECONDS``), and the shard function's Lambda
``Timeout``. At a read timeout of 600 the first two summed to exactly 900 and
left nothing for the work itself, so a single transient ``Read timed out`` ran the
invocation into the wall clock. Step Functions then read the resulting
``Sandbox.Timedout`` as deterministic — one attempt, by design (#917) — so the one
failure a retry would have cleared was the one not retried, and
``ExtractionShardMap``, which tolerates no shard failures, discarded the sibling
shards that had already succeeded (#1014).

Each number was individually defensible and the relationship between them was
stated only in a comment. These tests assert the relationship, and that the
constants are actually the ones the extraction code and the deployed function
use — a correct constant nobody reads is not a fix.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from idp_common.utils.bedrock_utils import (
    AGENT_MAX_BACKOFF_SECONDS,
    AGENT_MAX_TOTAL_BACKOFF_SECONDS,
    AGENT_READ_TIMEOUT_SECONDS,
    LAMBDA_MAX_TIMEOUT_SECONDS,
)

REPO = Path(__file__).resolve().parents[5]
ASL = REPO / "patterns" / "unified" / "statemachine" / "workflow.asl.json"
TEMPLATE = REPO / "patterns" / "unified" / "template.yaml"

# Room that must remain for the actual extraction work after one stalled request
# and the whole backoff allowance. Two further attempts at the read timeout is the
# floor worth defending: one to replace the stalled call, one for a second blip.
_MIN_WORKING_MARGIN_SECONDS = 2 * AGENT_READ_TIMEOUT_SECONDS


@pytest.mark.unit
def test_one_stalled_request_plus_all_backoff_leaves_room_to_work():
    """The budget inequality itself. This is the assertion that was missing."""
    spent_worst_case = AGENT_READ_TIMEOUT_SECONDS + AGENT_MAX_TOTAL_BACKOFF_SECONDS
    remaining = LAMBDA_MAX_TIMEOUT_SECONDS - spent_worst_case
    assert remaining >= _MIN_WORKING_MARGIN_SECONDS, (
        f"a stalled Bedrock request ({AGENT_READ_TIMEOUT_SECONDS}s) plus the full "
        f"backoff allowance ({AGENT_MAX_TOTAL_BACKOFF_SECONDS}s) leaves only "
        f"{remaining}s of the {LAMBDA_MAX_TIMEOUT_SECONDS}s invocation for the work "
        f"itself, which is under the {_MIN_WORKING_MARGIN_SECONDS}s floor. The shard "
        "will die on the wall clock instead of retrying, and Step Functions treats a "
        "timeout as deterministic. See #1014."
    )


@pytest.mark.unit
def test_a_single_sleep_cannot_outlast_the_invocation():
    """``max_delay`` was once 1800 inside a 900s function. Keep that shut."""
    assert AGENT_MAX_BACKOFF_SECONDS < LAMBDA_MAX_TIMEOUT_SECONDS
    assert AGENT_MAX_BACKOFF_SECONDS <= AGENT_MAX_TOTAL_BACKOFF_SECONDS


@pytest.mark.unit
def test_the_extraction_code_actually_defaults_to_this_read_timeout():
    """A budget constant that the callers do not use would pass the maths and
    change nothing. Both extraction modules must take their default from it, and
    no literal 600 may survive as a read timeout."""
    from idp_common.extraction import agentic_idp, runtime

    for mod in (agentic_idp, runtime):
        src = Path(mod.__file__).read_text()
        assert "read_timeout: float = 600.0" not in src, (
            f"{mod.__name__} still hardcodes a 600s read timeout; the deployed "
            "function's Lambda timeout is 900s, so one stalled request takes the "
            "invocation. See #1014."
        )
        assert "read_timeout: float = AGENT_READ_TIMEOUT_SECONDS" in src, (
            f"{mod.__name__} does not take its read timeout from the shared budget "
            "constant, so the budget assertions above do not constrain it."
        )


@pytest.mark.unit
def test_the_deployed_shard_function_timeout_matches_the_assumed_ceiling():
    """The maths above assumes the function really is capped at 900s. If someone
    lowers the function's Timeout, the budget silently stops adding up."""
    text = TEMPLATE.read_text()
    idx = text.index("ShardRuntimeFunction:")
    block = text[idx : idx + 4000]
    m = re.search(r"^\s+Timeout:\s*(\d+)\s*$", block, re.MULTILINE)
    assert m, "could not read ShardRuntimeFunction's Timeout from the template"
    assert float(m.group(1)) <= LAMBDA_MAX_TIMEOUT_SECONDS, (
        "ShardRuntimeFunction's Timeout exceeds the ceiling the budget assumes"
    )
    assert float(m.group(1)) == LAMBDA_MAX_TIMEOUT_SECONDS, (
        f"ShardRuntimeFunction's Timeout is {m.group(1)}s but the budget constants "
        f"are sized for {LAMBDA_MAX_TIMEOUT_SECONDS}s. Re-derive the constants in "
        "utils/bedrock_utils.py against the real timeout."
    )


@pytest.mark.unit
def test_the_shard_map_can_retry_so_persisted_shards_are_not_thrown_away():
    """``ExtractionShardMap`` declares no ToleratedFailurePercentage, so Step
    Functions' default of 0 applies and one failed shard fails the Map. That is
    only survivable because the Map is retried and the shards that already
    succeeded reload from S3 rather than re-inferring. Without a Map-level
    retrier, that persistence can never be used."""
    text = ASL.read_text()
    block = text[
        text.index('"ExtractionShardMap"') : text.index('"ExtractionMergeStep"')
    ]
    assert "States.ExceedToleratedFailureThreshold" in block, (
        "ExtractionShardMap has no retrier for the error Step Functions raises when "
        "a shard fails under a zero failure tolerance, so a single transient shard "
        "failure discards the output of every shard that succeeded. See #1014."
    )
    assert '"ToleratedFailurePercentage"' not in block, (
        "A non-zero failure tolerance would let the document complete with shards "
        "missing, which is silent data loss - worse than the failure it replaces. "
        "Recover by retrying the Map, not by accepting a partial result."
    )
    # That the ASL still parses at all is asserted by
    # scripts/tests/test_asl_placeholder_substitution.py, which resolves the
    # CloudFormation ``${...}`` tokens first - this file is a template fragment,
    # not loadable JSON, so a json.loads() check here would be wrong as well as
    # redundant.
