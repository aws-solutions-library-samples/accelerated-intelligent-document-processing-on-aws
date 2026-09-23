# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Standalone demonstration: sharded extraction runs as plain Python (no SFN).

This is the library-integrity proof for the ExtractionRuntime refactor. It shows
that the SAME sharding primitives the production Step Functions Distributed Map
uses (``extract_one_shard`` + ``merge_shard_results``, scheduled by
``InProcessRuntime``) run end-to-end in a single Python process — a notebook, a
CLI, or one Lambda — with NO Step Functions dependency and NO behaviour
divergence from production.

Two modes:

1. Offline (default) — uses a deterministic fake shard_runner so the scheduling,
   idempotent persistence and merge can be verified with no AWS calls. This is
   what CI / a laptop runs.

2. Live (set RUN_LIVE=1 with AWS creds + a config that has agentic.enabled and
   max_concurrent_batches>1) — calls ``ExtractionService.process_document_section``
   exactly as the section Lambda does; the in-process runtime shards the section
   and runs real Bedrock agents. This is the production code path, just hosted in
   a plain process instead of Step Functions.

Run: ``python notebooks/misc/standalone_sharded_extraction_demo.py``
"""

import asyncio
import os

from pydantic import BaseModel

from idp_common.config.models import IDPConfig
from idp_common.extraction.runtime import (
    InProcessRuntime,
    S3ShardPersistence,
    select_runtime,
)


class Holding(BaseModel):
    symbol: str | None = None
    quantity: str | None = None


class BrokerageStatement(BaseModel):
    account_number: str | None = None
    statement_period: str | None = None
    holdings: list[Holding] | None = None


def _payload(start, end, total):
    return {
        "content": [{"text": f"pages {start}-{end}"}],
        "page_start": start,
        "page_end": end,
        "total_pages": total,
    }


def _fake_runner_factory(per_shard):
    async def runner(*, shard_index, total_shards, payload, **kwargs):
        data = per_shard[payload["page_start"]]
        return BrokerageStatement(**data), {"metering": {f"s{payload['page_start']}": {"t": 1}}}

    return runner


def demo_offline():
    print("=== Offline standalone sharded extraction (no SFN, no AWS) ===")
    config = IDPConfig()
    config.extraction.agentic.enabled = True
    config.extraction.agentic.max_concurrent_batches = 3

    # The runtime selected for a standalone process is InProcessRuntime.
    runtime = select_runtime(config, config.extraction.agentic.max_concurrent_batches)
    assert isinstance(runtime, InProcessRuntime), runtime
    print(f"select_runtime() -> {runtime.name} (standalone default)")

    # Three page-budgeted shards; account/period live on page 0.
    payloads = [_payload(0, 3, 9), _payload(3, 6, 9), _payload(6, 9, 9)]
    per_shard = {
        0: {
            "account_number": "1234-5678",
            "statement_period": "Jan 2025",
            "holdings": [{"symbol": "AAA", "quantity": "10"}],
        },
        3: {"holdings": [{"symbol": "BBB", "quantity": "20"}]},
        6: {"holdings": [{"symbol": "CCC", "quantity": "30"}]},
    }
    runner = _fake_runner_factory(per_shard)

    merged, response = asyncio.run(
        runtime.run(
            shard_payloads=payloads,
            model_id="us.anthropic.claude-sonnet-4-6:1m",
            data_format=BrokerageStatement,
            config=config,
            section_id="brokerage_0_9",
            shard_runner=runner,
        )
    )
    print(f"merged account_number = {merged.account_number}")
    print(f"merged statement_period = {merged.statement_period}")
    print(f"merged holdings ({len(merged.holdings)}): "
          f"{[h.symbol for h in merged.holdings]}")
    assert [h.symbol for h in merged.holdings] == ["AAA", "BBB", "CCC"]
    assert merged.account_number == "1234-5678"
    print("OK: page-ordered concatenation + first-non-null scalars.\n")

    # Idempotent skip-completed (proves SFN-retry / asyncio-reentry resume).
    class _MemPersist:
        def __init__(self):
            self.store = {}

        def load(self, sid, s, e):
            return self.store.get((sid, s, e))

        def save(self, sid, s, e, result):
            self.store[(sid, s, e)] = result

    persist = _MemPersist()
    persist.save(
        "brokerage_0_9", 0, 3,
        {"extracted_fields": {"holdings": [{"symbol": "PRE", "quantity": "1"}]},
         "metering": {}},
    )
    ran = []

    async def logging_runner(*, payload, **kw):
        ran.append(payload["page_start"])
        return await runner(payload=payload, **kw)

    merged2, _ = asyncio.run(
        InProcessRuntime(3).run(
            shard_payloads=payloads,
            model_id="m",
            data_format=BrokerageStatement,
            config=config,
            section_id="brokerage_0_9",
            persistence=persist,
            shard_runner=logging_runner,
        )
    )
    print(f"shards that actually ran (page0 was pre-persisted): {sorted(ran)}")
    assert 0 not in ran, "page-0 shard should have been skipped (loaded from S3)"
    assert merged2.holdings[0].symbol == "PRE"
    print("OK: completed shard loaded from persistence, only incomplete shards ran.\n")
    print("merge_shard_results / extract_one_shard / InProcessRuntime are the "
          "SAME primitives the SFN Distributed Map runs — single source of truth.")


def demo_live():
    """Live path: process a real document section through process_document_section.

    Requires AWS creds + S3 OCR output for a document and a config with
    agentic.enabled + max_concurrent_batches>1. Left as a documented template; the
    in-process runtime shards and runs real Bedrock agents with no SFN involved.
    """
    from idp_common import get_config
    from idp_common.extraction import ExtractionService
    from idp_common.models import Document

    config = get_config(as_model=True, version=os.environ["CONFIG_VERSION"])
    document = Document.load_document(
        {"s3_key": os.environ["DOCUMENT_S3_KEY"]}, os.environ["WORKING_BUCKET"], None
    )
    service = ExtractionService(config=config)
    # Optional: persist shards so a re-run resumes only incomplete ones.
    service._shard_persistence = S3ShardPersistence(
        bucket=os.environ["WORKING_BUCKET"], execution_arn="standalone-demo"
    )
    out = service.process_document_section(
        document=document, section_id=os.environ["SECTION_ID"]
    )
    print("Live standalone sharded extraction complete:", out.sections[0].extraction_result_uri)


if __name__ == "__main__":
    if os.environ.get("RUN_LIVE") == "1":
        demo_live()
    else:
        demo_offline()
