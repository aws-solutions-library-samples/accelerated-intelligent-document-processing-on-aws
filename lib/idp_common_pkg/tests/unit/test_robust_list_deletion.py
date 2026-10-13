# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``create_document_resolver``'s own copy of the list-entry cleanup.

This module is a second implementation of the same shard cascade that lives in
``idp_common.delete_documents``, and it is **live**: ``create_document_resolver``
calls it when a document key is re-uploaded, to remove the previous list row so the
Document List does not show the document twice. It is duplicated rather than imported
because ``CreateDocumentResolverFunction`` declares no ``Layers`` and so has no
``idp_common`` — which is also why fixing the shared copy of #1366 did nothing for
this one.

Its failure mode here is quieter than the delete resolver's and still user visible: a
re-uploaded document whose previous list row sat under a different ``QueuedTime`` than
the one its record carries. Strategy 1 aims at the exact key and misses; strategies 2
and 3 are the ones that look for the row wherever it is, and both went through a
shard query whose ``FilterExpression`` named the sort key — which DynamoDB refuses in
a ``Query`` — so they raised, were caught, returned ``[]``, and the old row stayed.
The document then appears twice.

⚠️ **Both call sites matter and only one of them is obvious.** The initial query and
the query inside the ``while 'LastEvaluatedKey' in response`` loop carried the same
expression as two separate literals. Repairing the first alone leaves the paginated
path broken, and that is the harder half to notice: it needs a shard partition large
enough to page, so it appears on a long-lived stack and not on a fresh one.

This file had no tests at all before, which is the other half of how the defect
survived here.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from idp_common.delete_documents import (
    list_entry_names_document as shared_predicate,
)

pytestmark = pytest.mark.unit

KEY = "batch-1/invoice.pdf"
QUEUED = "2025-09-10T12:03:27.256164+00:00"
#: Hour 12 falls in shard 03 — six four-hour shards per day.
SHARD_PK = "list#2025-09-10#s#03"

PROJECTION = "PK, SK, ObjectKey"


def _load():
    path = os.path.join(
        os.path.dirname(__file__),
        "../../../../nested/api-resolvers/src/lambda/create_document_resolver/"
        "robust_list_deletion.py",
    )
    spec = importlib.util.spec_from_file_location("robust_list_deletion", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["robust_list_deletion"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rld():
    return _load()


def _row(marker: str, pk: str = SHARD_PK, object_key: str | None = None) -> dict:
    row = {"PK": pk, "SK": f"ts#{marker}#id#{object_key or KEY}"}
    if object_key is not None:
        row["ObjectKey"] = object_key
    return row


def _table() -> MagicMock:
    table = MagicMock()
    table.query.return_value = {"Items": []}
    table.delete_item.return_value = {}
    return table


class TestTheShardQuery:
    def test_it_names_no_key_attribute_in_a_filter(self, rld):
        """The repair. A ``FilterExpression`` here is refused by DynamoDB outright."""
        table = _table()
        rld.query_shard_for_object_key(table, SHARD_PK, KEY)
        kwargs = table.query.call_args.kwargs
        assert "FilterExpression" not in kwargs
        assert kwargs["ProjectionExpression"] == PROJECTION
        assert "KeyConditionExpression" in kwargs

    def test_both_shapes_of_list_entry_are_selected(self, rld):
        by_sort_key = _row("a")
        by_attribute = {
            "PK": SHARD_PK,
            "SK": "ts#b#id#something-else",
            "ObjectKey": KEY,
        }
        other = _row("c", object_key="other/doc.pdf")
        table = _table()
        table.query.return_value = {"Items": [by_sort_key, by_attribute, other]}

        found = rld.query_shard_for_object_key(table, SHARD_PK, KEY)

        assert found == [by_sort_key, by_attribute], (
            "both shapes must be found and another document's row must not be"
        )

    def test_the_paginated_call_is_repaired_too(self, rld):
        """⚠️ The second call site, which the first fix misses.

        The pagination query was a separate literal carrying the same rejected
        expression, so a shard large enough to page kept failing after the initial
        call was repaired. Asserted on **every** call's arguments rather than the
        first, which is what makes this distinguishable from the single-page case.
        """
        first, second = _row("a"), _row("b")
        # ⚠️ A non-matching row on the SECOND page, which is what makes this test see
        # the predicate rather than only the pagination. With every row on both pages
        # matching, a paginated branch that appends unconditionally produces an
        # identical result — measured: that exact mutation survived this suite until
        # this row was added.
        stranger = _row("z", object_key="someone/else.pdf")
        table = _table()
        table.query.side_effect = [
            {"Items": [first], "LastEvaluatedKey": {"PK": SHARD_PK, "SK": "cursor"}},
            {"Items": [second, stranger]},
        ]

        found = rld.query_shard_for_object_key(table, SHARD_PK, KEY)

        assert found == [first, second], (
            "the second page was not collected, or its rows were not filtered"
        )
        assert table.query.call_count == 2
        for index, call in enumerate(table.query.call_args_list):
            assert "FilterExpression" not in call.kwargs, (
                f"call {index} still carries a FilterExpression"
            )
            assert call.kwargs["ProjectionExpression"] == PROJECTION
        assert table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {
            "PK": SHARD_PK,
            "SK": "cursor",
        }

    def test_a_bare_mock_response_does_not_paginate_for_ever(self, rld):
        """The double, not DynamoDB, is what makes this loop dangerous.

        ``MagicMock().get("LastEvaluatedKey")`` auto-creates a **truthy** value, so a
        loop written with ``.get()`` never terminates against a bare mock and the
        collected list grows without bound. This loop reads membership, which answers
        ``False``. Asserted with a hard call cap so a regression fails fast instead of
        exhausting the host.
        """
        table = MagicMock()
        calls = {"n": 0}

        def query(**_kwargs):
            calls["n"] += 1
            assert calls["n"] < 50, "the pagination loop did not terminate"
            return MagicMock()

        table.query.side_effect = query
        rld.query_shard_for_object_key(table, SHARD_PK, KEY)
        assert calls["n"] == 1


class TestTheCascade:
    def test_strategy_two_finds_and_deletes_the_row(self, rld):
        table = _table()
        # Strategy 1 aims at the exact computed key and misses; strategy 2 finds it.
        table.delete_item.side_effect = [{}, {"Attributes": {"x": 1}}]
        table.query.return_value = {"Items": [_row("elsewhere")]}

        assert (
            rld.delete_list_entries_robust(table, KEY, {"QueuedTime": QUEUED}) is True
        )

    def test_strategy_three_searches_the_adjacent_shards(self, rld):
        """The case the cascade exists for: the row landed one shard over.

        This is the path that was wholly unreachable while the query raised, and the
        one a re-upload under a drifted timestamp depends on.
        """
        table = _table()
        table.delete_item.side_effect = [{}, {"Attributes": {"x": 1}}]
        table.query.side_effect = [
            {"Items": []},  # computed shard: nothing
            {"Items": [_row("n", pk="list#2025-09-10#s#02")]},  # neighbour: hit
            {"Items": []},
        ]

        assert (
            rld.delete_list_entries_robust(table, KEY, {"QueuedTime": QUEUED}) is True
        )
        assert table.query.call_count >= 2, "the adjacent shards were never searched"

    def test_initialeventtime_is_used_when_queuedtime_is_absent(self, rld):
        """The fallback timestamp, which is the only route for some records.

        Both halves of ``QueuedTime or InitialEventTime`` are live: a record written
        before the queued time was recorded carries only the latter, and with the
        fallback gone its list row is unreachable — no shard is computed, no strategy
        runs, and the cascade returns ``False`` as though there were simply nothing to
        delete. The shared implementation has this assertion and this copy did not,
        which is the same asymmetry the adjacent-shard padding had.
        """
        table = _table()
        table.delete_item.side_effect = [{}, {"Attributes": {"x": 1}}]
        table.query.return_value = {"Items": [_row("fallback")]}

        assert (
            rld.delete_list_entries_robust(table, KEY, {"InitialEventTime": QUEUED})
            is True
        )
        assert table.query.called, "no shard was queried, so no timestamp was resolved"
        assert table.query.call_args.kwargs["KeyConditionExpression"] is not None

    def test_no_metadata_means_no_strategy_runs(self, rld):
        table = _table()
        assert rld.delete_list_entries_robust(table, KEY, None) is False
        table.delete_item.assert_not_called()


class TestTheAdjacentShards:
    """Strategy 3's shard identifiers, which have to be the padded two-digit form.

    A neighbour emitted as ``list#…#s#1`` rather than ``#s#01`` names a partition key
    that cannot exist, so strategy 3 queries nothing, finds nothing, and looks exactly
    like a neighbour search that ran and came back empty. The equivalent assertion
    exists for ``idp_common.delete_documents`` and did not for this copy — measured: a
    mutation dropping the ``02d`` format spec survived this suite until these cases
    were added, in the module that is *only* reached through strategy 3.
    """

    @pytest.mark.parametrize(
        "shard, expected",
        [
            ("00", ["01"]),
            ("01", ["00", "02"]),
            ("03", ["02", "04"]),
            ("05", ["04"]),
        ],
    )
    def test_the_identifiers_are_zero_padded_and_clamped(self, rld, shard, expected):
        assert rld.get_adjacent_shards("2025-09-10", shard) == [
            f"list#2025-09-10#s#{s}" for s in expected
        ]

    def test_a_non_numeric_shard_yields_no_neighbours(self, rld):
        assert rld.get_adjacent_shards("2025-09-10", "xx") == []

    @pytest.mark.parametrize("shard", ["00", "01", "02", "03", "04", "05"])
    def test_it_agrees_with_the_shared_implementation(self, rld, shard):
        from idp_common.delete_documents import _get_adjacent_shards

        assert rld.get_adjacent_shards("2025-09-10", shard) == _get_adjacent_shards(
            "2025-09-10", shard
        )


class TestParityWithTheSharedImplementation:
    """The duplication is deliberate; the two diverging is not.

    ``robust_list_deletion`` cannot import ``idp_common`` — its Lambda has no layer —
    so the predicate exists twice. A test is the only thing that can hold them in
    step, and the selection is the part where a divergence silently changes which rows
    a cleanup touches.
    """

    #: ``(entry, expected)`` — the **right answer**, not merely a shared one.
    #:
    #: ⚠️ This matrix used to carry entries alone and assert only that the two copies
    #: agreed. The prefix-sibling collision (``…/invoice.pdf.bak`` while deleting
    #: ``…/invoice.pdf``) was already sitting in it, both copies answered ``True``, and
    #: the suite was green: agreement is not correctness, and a parity-only test over
    #: two copies of one mistake cannot see it. The expected value is now authored per
    #: case and both copies are compared against *it*, so the parity property survives
    #: as a consequence rather than as the whole assertion.
    CASES = [
        ({"ObjectKey": KEY}, True),
        ({"SK": f"ts#{QUEUED}#id#{KEY}"}, True),
        ({"SK": f"ts#{QUEUED}#id#{KEY}", "ObjectKey": KEY}, True),
        ({"ObjectKey": "other.pdf", "SK": f"ts#{QUEUED}#id#other.pdf"}, False),
        # A right extension of the key. An ordinary filename, a different document, and
        # its list row must not be selected for deletion.
        ({"ObjectKey": f"{KEY}.bak", "SK": f"ts#{QUEUED}#id#{KEY}.bak"}, False),
        ({"SK": f"ts#{QUEUED}#id#{KEY} (1).pdf"}, False),
        ({}, False),
        ({"SK": None}, False),
        ({"SK": 7}, False),
        ({"ObjectKey": None}, False),
        # A left extension, which a substring test already answered correctly.
        ({"SK": f"ts#{QUEUED}#id#prefix-{KEY}"}, False),
        # A deeper path sharing the basename.
        ({"SK": f"ts#{QUEUED}#id#backup/{KEY}"}, False),
        # ⚠️ A document whose OWN key contains the delimiter and ends with the target.
        # `#` is legal in an S3 key, so this arrives by upload, and anchoring at the end
        # of the sort key matched it. Splitting on the first `#id#` does not.
        ({"SK": f"ts#{QUEUED}#id#a#id#{KEY}"}, False),
        ({"SK": f"ts#{QUEUED}#id#a#id#b", "ObjectKey": "a#id#b"}, False),
    ]

    #: A key that legitimately contains the delimiter must still match ITSELF — the
    #: narrowing must not strand a document whose name happens to carry `#id#`.
    DELIMITER_KEY = "odd#id#name.pdf"

    def test_the_case_matrix_is_not_empty(self):
        """A parametrisation over an empty list collects as a skip, not a failure."""
        assert len(self.CASES) >= 14

    def test_a_key_containing_the_delimiter_still_matches_itself(self, rld):
        """The other direction of the narrowing, which is the one it could break.

        Rejecting `…#id#a#id#<target>` must not also reject the row belonging to a
        document actually named `a#id#<something>`. Splitting on the FIRST delimiter
        returns the whole remainder, so such a key round-trips.
        """
        row = {"SK": f"ts#{QUEUED}#id#{self.DELIMITER_KEY}"}
        assert rld.list_entry_names_document(row, self.DELIMITER_KEY) is True
        assert shared_predicate(row, self.DELIMITER_KEY) is True

    @pytest.mark.parametrize("entry, expected", CASES, ids=range(len(CASES)))
    def test_both_predicates_give_the_right_answer(self, rld, entry, expected):
        assert rld.list_entry_names_document(entry, KEY) is expected, (
            f"the vendored predicate is wrong on {entry!r}; it decides which rows a "
            "cleanup deletes"
        )
        assert shared_predicate(entry, KEY) is expected, (
            f"idp_common's predicate is wrong on {entry!r}"
        )

    def test_the_matrix_exercises_both_answers(self, rld):
        """Otherwise the matrix could be asserting one constant answer.

        Both halves: that the authored expectations are not all one value, and that the
        predicate itself produces both over them — the first alone is a tautology over
        this file's own literals and would pass with the predicate never called.
        """
        assert {expected for _entry, expected in self.CASES} == {True, False}
        assert {
            rld.list_entry_names_document(entry, KEY) for entry, _expected in self.CASES
        } == {True, False}


def test_the_vendored_copy_still_has_no_idp_common_layer():
    """Why the duplication is still necessary, asserted rather than remembered.

    If ``CreateDocumentResolverFunction`` gains the ``idp_common`` layer, the right
    change is to delete this module and import the shared one — and the parity tests
    above would keep passing over two copies nobody needs. This fails at that point
    and says so.
    """
    import yaml

    repo = Path(__file__).resolve()
    while not (repo / "nested" / "api-resolvers").is_dir():
        repo = repo.parent

    class _Loader(yaml.SafeLoader):
        pass

    def _tag(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return {f"Fn::{suffix}": loader.construct_scalar(node)}
        if isinstance(node, yaml.SequenceNode):
            return {f"Fn::{suffix}": loader.construct_sequence(node, deep=True)}
        return {f"Fn::{suffix}": loader.construct_mapping(node, deep=True)}

    _Loader.add_multi_constructor("!", _tag)
    template = yaml.load(
        (repo / "nested" / "api-resolvers" / "template.yaml").read_text(),
        Loader=_Loader,
    )
    properties = template["Resources"]["CreateDocumentResolverFunction"]["Properties"]
    assert "Layers" not in properties, (
        "CreateDocumentResolverFunction now has a layer: if it is the idp_common "
        "base layer, delete robust_list_deletion.py and import "
        "idp_common.delete_documents instead of keeping two copies in step"
    )
