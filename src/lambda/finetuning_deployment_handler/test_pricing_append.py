# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Concurrency tests for the pricing-row append (issue #1111).

When a fine-tuned model's deployment goes Active this Lambda adds a placeholder
pricing entry for its ARN. It did so by reading the whole ``DefaultPricing`` /
``CustomPricing`` row, appending one entry to the ``pricing`` list in Python and
writing the **whole row back** with ``put_item`` and no condition. Two things get
lost that way, and the second is much larger than the first: another deployment's
entry, and -- because a whole-item replacement rewrites every attribute, not just
the list -- an operator's entire pricing edit made in the UI over the same window.

These are two global singleton rows keyed only on ``Configuration``, so every
writer in the account contends on the same two keys.

The table is a **real** ``moto`` table, so the ``ConditionExpression`` is evaluated
by DynamoDB's own engine. A mock would accept the keyword and prove nothing about
the write it is supposed to refuse.

The interleaving is **forced, not raced**: ``_CompetingWriteTable`` commits the
other writer inside the ``get_item`` call, straight after the snapshot this
Lambda will write from. No thread, no sleep, no scheduler.
"""

import gzip
import importlib
import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional

import boto3
import pytest
from moto import mock_aws

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
TABLE_NAME = "test-configuration"
DEPLOYMENT_ARN = "arn:aws:bedrock:us-east-1:111111111111:custom-model-deployment/mine"
OTHER_ARN = "arn:aws:bedrock:us-east-1:111111111111:custom-model-deployment/theirs"


@pytest.fixture
def mod(monkeypatch):
    """Import the handler with the environment it reads at module scope."""
    if MODULE_DIR not in sys.path:
        sys.path.insert(0, MODULE_DIR)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("TRACKING_TABLE", "test-tracking")
    sys.modules.pop("index", None)
    return importlib.import_module("index")


def _entry(name: str) -> Dict[str, Any]:
    return {
        "name": name,
        "units": [
            {"name": "inputTokens", "price": "0.0"},
            {"name": "outputTokens", "price": "0.0"},
        ],
    }


def _compressed_row(config_key: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Seed a row in the compressed shape every current writer produces.

    Built here rather than by calling the module's own ``_compress_config_item``,
    so a defect in that function cannot make the fixture agree with it.
    """
    return {
        "Configuration": config_key,
        "UpdatedAt": "2026-01-01T00:00:00Z",
        "_config_storage": "compressed",
        "_compressed_config": gzip.compress(
            json.dumps(body, separators=(",", ":")).encode("utf-8")
        ),
    }


class _CompetingWriteTable:
    """A real table with one competing write forced between read and write."""

    def __init__(
        self,
        table: Any,
        competitor: Optional[Callable[[Any], None]],
        every_read: bool = False,
    ):
        self._table = table
        self._competitor = competitor
        self._every_read = every_read
        self.reads = 0
        self.writes = 0
        self.rejections = 0
        # The handler reaches its exception class through this attribute.
        self.meta = table.meta

    def get_item(self, **kwargs) -> Dict[str, Any]:
        response = self._table.get_item(**kwargs)
        self.reads += 1
        if self._competitor is not None and (self._every_read or self.reads == 1):
            self._competitor(self._table)
        return response

    def put_item(self, **kwargs):
        self.writes += 1
        try:
            return self._table.put_item(**kwargs)
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            self.rejections += 1
            raise


def _pricing_of(table: Any, config_key: str) -> List[Dict[str, Any]]:
    item = table.get_item(Key={"Configuration": config_key}).get("Item", {})
    raw = item.get("_compressed_config")
    if raw is None:
        return list(item.get("pricing", []))
    body = json.loads(gzip.decompress(bytes(raw)).decode("utf-8"))
    return list(body.get("pricing", []))


def _names(entries: List[Dict[str, Any]]) -> set:
    return {e.get("name") for e in entries}


class _Harness:
    def __init__(
        self,
        seed_rows: Dict[str, Dict[str, Any]],
        competitor: Optional[Callable[[Any], None]] = None,
        every_read: bool = False,
    ):
        self.resource = boto3.resource("dynamodb", region_name="us-east-1")
        self.resource.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "Configuration", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        self.real = self.resource.Table(TABLE_NAME)
        for item in seed_rows.values():
            self.real.put_item(Item=item)
        self.wrapper = _CompetingWriteTable(
            self.real, competitor, every_read=every_read
        )

    def pricing(self, config_key: str) -> List[Dict[str, Any]]:
        return _pricing_of(self.real, config_key)


def _add_other_deployments_entry(table: Any) -> None:
    """The competing fine-tuning deployment, modelled as already fixed.

    Modelling the competitor as correct is what isolates the write under test: if
    both writers were broken, a green result would not say which one behaved.
    """
    current = _pricing_of(table, "DefaultPricing")
    table.put_item(
        Item=_compressed_row(
            "DefaultPricing", {"pricing": current + [_entry(OTHER_ARN)]}
        )
    )


def _legacy_row(pricing: List[Dict[str, Any]]) -> Dict[str, Any]:
    """A row in the pre-compression shape, with `pricing` at the top level."""
    return {"Configuration": "DefaultPricing", "pricing": pricing}


def _operator_edits_pricing(table: Any) -> None:
    """The other real writer: a pricing edit saved from the UI.

    This one does not append -- it rewrites the row's own content, which is what
    the whole-item replacement used to discard in full.
    """
    table.put_item(
        Item=_compressed_row(
            "DefaultPricing",
            {"pricing": [_entry("bedrock/nova-lite")], "operatorEdit": True},
        )
    )


@pytest.mark.unit
class TestPricingAppendUnderOverlap:
    @mock_aws
    def test_an_overlapping_deployments_entry_is_not_overwritten(self, mod):
        harness = _Harness(
            {"DefaultPricing": _compressed_row("DefaultPricing", {"pricing": []})},
            competitor=_add_other_deployments_entry,
        )
        mod._add_entry_to_pricing_config(
            harness.wrapper, "DefaultPricing", _entry(DEPLOYMENT_ARN)
        )
        assert _names(harness.pricing("DefaultPricing")) == {OTHER_ARN, DEPLOYMENT_ARN}
        # The overlap really happened and really was refused, rather than the test
        # passing because nothing collided.
        assert harness.wrapper.rejections == 1
        assert harness.wrapper.reads == 2

    @mock_aws
    def test_an_operators_pricing_edit_survives(self, mod):
        # The larger loss: a whole-item replacement discards every attribute of
        # the row, not just the one list.
        harness = _Harness(
            {
                "DefaultPricing": _compressed_row(
                    "DefaultPricing", {"pricing": [_entry("bedrock/nova-pro")]}
                )
            },
            competitor=_operator_edits_pricing,
        )
        mod._add_entry_to_pricing_config(
            harness.wrapper, "DefaultPricing", _entry(DEPLOYMENT_ARN)
        )
        stored = _names(harness.pricing("DefaultPricing"))
        assert stored == {"bedrock/nova-lite", DEPLOYMENT_ARN}, (
            "the operator's edit was discarded by the deployment's write"
        )
        assert harness.wrapper.rejections == 1

    @mock_aws
    def test_an_uncontended_append_to_a_legacy_row_is_not_refused(self, mod):
        """The guard must describe stored content, not content built from it.

        Rows written before compression keep `pricing` at the top level, and on
        such a row the decompressing read returns the row object itself -- so a
        guard built from it after the list has been appended to names content that
        has never been stored, and the write is refused on every attempt with
        nothing competing at all. That is a way for the guard to be false by
        construction rather than a lost conflict, so it is asserted with **no**
        competitor: a contended test cannot see it, because the competitor's own
        write migrates the row to the compressed format and the second attempt then
        reads a fresh object and succeeds.
        """
        harness = _Harness(
            {"DefaultPricing": _legacy_row([_entry("bedrock/nova-pro")])}
        )
        mod._add_entry_to_pricing_config(
            harness.wrapper, "DefaultPricing", _entry(DEPLOYMENT_ARN)
        )
        assert _names(harness.pricing("DefaultPricing")) == {
            "bedrock/nova-pro",
            DEPLOYMENT_ARN,
        }
        assert harness.wrapper.rejections == 0
        assert harness.wrapper.writes == 1

    @mock_aws
    def test_a_legacy_row_is_guarded_against_another_legacy_writer(self, mod):
        # The contended version of the same branch. The competitor stays in the
        # legacy shape on purpose: one that wrote the compressed shape would move
        # the row off this branch after the first attempt, so the retry would be
        # measuring the compressed path.
        harness = _Harness(
            {"DefaultPricing": _legacy_row([_entry("bedrock/nova-pro")])},
            competitor=lambda table: table.put_item(
                Item=_legacy_row([_entry("bedrock/nova-lite")])
            ),
        )
        mod._add_entry_to_pricing_config(
            harness.wrapper, "DefaultPricing", _entry(DEPLOYMENT_ARN)
        )
        assert _names(harness.pricing("DefaultPricing")) == {
            "bedrock/nova-lite",
            DEPLOYMENT_ARN,
        }
        assert harness.wrapper.rejections == 1

    @mock_aws
    def test_the_competitor_adding_the_same_arn_converges_without_duplicating(
        self, mod
    ):
        def add_my_arn(table):
            table.put_item(
                Item=_compressed_row(
                    "DefaultPricing", {"pricing": [_entry(DEPLOYMENT_ARN)]}
                )
            )

        harness = _Harness(
            {"DefaultPricing": _compressed_row("DefaultPricing", {"pricing": []})},
            competitor=add_my_arn,
        )
        mod._add_entry_to_pricing_config(
            harness.wrapper, "DefaultPricing", _entry(DEPLOYMENT_ARN)
        )
        entries = harness.pricing("DefaultPricing")
        assert len(entries) == 1, "the retry duplicated an entry already present"
        assert _names(entries) == {DEPLOYMENT_ARN}

    @mock_aws
    def test_a_row_that_never_settles_raises_rather_than_reporting_success(self, mod):
        # Exhausting the budget must not return quietly: the caller's non-fatal
        # handler logs an error, and a silent return is indistinguishable from
        # having written the entry.
        #
        # The competitor writes *different* content on every read so the budget is
        # exhausted for the reason being tested. A competitor repeating one body
        # cannot be relied on to do that, but not because the condition would hold:
        # `gzip.compress` embeds an mtime, so re-storing an identical body in a
        # later second stores different bytes and the condition is false anyway.
        # That makes the guard strictly stronger than content equality, which is
        # safe -- it can refuse a no-op rewrite, never permit a real one -- and it
        # is why this test names distinct entries rather than leaning on which of
        # the two semantics is in force.
        moves = iter(range(1, 100))
        harness = _Harness(
            {"DefaultPricing": _compressed_row("DefaultPricing", {"pricing": []})},
            competitor=lambda table: table.put_item(
                Item=_compressed_row(
                    "DefaultPricing",
                    {"pricing": [_entry(f"bedrock/edit-{next(moves)}")]},
                )
            ),
            every_read=True,
        )
        with pytest.raises(RuntimeError, match="kept changing"):
            mod._add_entry_to_pricing_config(
                harness.wrapper, "DefaultPricing", _entry(DEPLOYMENT_ARN)
            )
        assert harness.wrapper.writes == mod._MAX_PRICING_WRITE_ATTEMPTS

    @mock_aws
    def test_a_missing_row_is_still_skipped_quietly(self, mod):
        # CustomPricing need not exist; that is not an error.
        harness = _Harness({})
        mod._add_entry_to_pricing_config(
            harness.wrapper, "CustomPricing", _entry(DEPLOYMENT_ARN)
        )
        assert harness.wrapper.writes == 0

    @mock_aws
    def test_an_uncontended_append_writes_once_and_keeps_the_existing_entries(
        self, mod
    ):
        harness = _Harness(
            {
                "DefaultPricing": _compressed_row(
                    "DefaultPricing",
                    {"pricing": [_entry("bedrock/nova-pro")], "keepMe": 1},
                )
            }
        )
        mod._add_entry_to_pricing_config(
            harness.wrapper, "DefaultPricing", _entry(DEPLOYMENT_ARN)
        )
        assert _names(harness.pricing("DefaultPricing")) == {
            "bedrock/nova-pro",
            DEPLOYMENT_ARN,
        }
        assert harness.wrapper.writes == 1
        assert harness.wrapper.rejections == 0
        raw = harness.real.get_item(Key={"Configuration": "DefaultPricing"})["Item"]
        body = json.loads(gzip.decompress(bytes(raw["_compressed_config"])).decode())
        assert body["keepMe"] == 1
