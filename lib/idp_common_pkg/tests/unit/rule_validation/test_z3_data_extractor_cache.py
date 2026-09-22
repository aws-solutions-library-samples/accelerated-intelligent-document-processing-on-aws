# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for `DataExtractor`'s path memoization and its document scope.

The readings this class produces are the values bound into the Z3 solver, so a
reading served for the wrong document is not a crash or a missing field: it is a
compliance verdict computed against another document's data, reported with normal
confidence. That is why the scope of the memo is asserted here directly rather than
inferred from the values a happy-path extraction returns.

The memo lives inside one `extract_values` call. What that buys is one property,
asserted below in the form that would have failed before it: reusing one extractor
across documents reads each document's own values, with no cleanup step in between
and whatever the allocator does with the addresses in the meantime. `clear_cache()`
is kept because callers exist, and is now a no-op — so the test for it asserts that
it is harmless and changes no reading, which is what it does.

⚠️ **Which of these tests can catch a regression, and which only sometimes can.**
Keying a memo on an address goes wrong only when CPython hands a later document the
address a freed one had
([#1115](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1115)),
and whether it does depends on what else was allocated in between — including
allocations made by the implementation under test, measured here as reuse happening
on one run of a pair of temporaries and not on the next. So the two tests that read
successive documents through one extractor are **opportunistic**: both did fail
against the address-keyed version, and neither is guaranteed to.

What holds every time, with no allocator involved, is
`test_a_document_mutated_between_calls_is_read_again` (an address that has not
changed while the content has) and `test_the_extractor_retains_nothing_after_a_call`
(no attribute of the extractor holds a reading at all, so there is nothing for a
later document to be served). Those are the regression guarantees; read the
opportunistic pair as illustrating the reported symptom rather than as the gate.

Nothing here touches AWS; the extractor is pure over a dict.
"""

from __future__ import annotations

import pytest

from idp_common.rule_validation.z3.data_extractor import DataExtractor
from idp_common.rule_validation.z3.models import Parameter, PathMapping, RuleJSON


def _rule(*, rule_id: str = "r1", paths: tuple[tuple[str, str], ...] = ()) -> RuleJSON:
    mappings = paths or (("coverage", "doc.coverage"),)
    names = [name for name, _ in mappings]
    return RuleJSON(
        rule_id=rule_id,
        version="1.0",
        description="d",
        natural_language_rule="nl",
        parameters=[Parameter(name=name, type="Real") for name in names],
        constraints=[f"(> {names[0]} 0)"],
        path_mappings=[
            PathMapping(parameter_name=name, data_path=path) for name, path in mappings
        ],
    )


@pytest.mark.unit
class TestExtractionMemoScope:
    """What the per-call memo does, and what it refuses to do across documents."""

    def test_the_same_path_twice_gives_the_same_value(self):
        extractor = DataExtractor()
        rule = _rule()
        data = {"doc": {"coverage": 1.0}}
        assert extractor.extract_values(rule, data) == extractor.extract_values(
            rule, data
        )

    def test_two_parameters_sharing_a_path_both_resolve(self):
        # The one redundancy the memo exists to remove: two declared parameters
        # reading the same data_path within a single call.
        extractor = DataExtractor()
        rule = _rule(
            paths=(("coverage", "doc.coverage"), ("limit", "doc.coverage")),
        )
        assert extractor.extract_values(rule, {"doc": {"coverage": 7.5}}) == {
            "coverage": 7.5,
            "limit": 7.5,
        }

    def test_two_temporary_documents_are_read_independently(self):
        # Opportunistic (see the module docstring). Both documents are temporaries,
        # so the first is unreachable the moment the first call returns and CPython
        # may place the second at the same address; when it did, the address-keyed
        # memo answered the second call with 1.0 (#1115).
        extractor = DataExtractor()
        rule = _rule()
        first = extractor.extract_values(rule, {"doc": {"coverage": 1.0}})
        second = extractor.extract_values(rule, {"doc": {"coverage": 2.0}})
        assert first == {"coverage": 1.0}
        assert second == {"coverage": 2.0}

    def test_many_documents_through_one_extractor_each_read_their_own_value(self):
        # Opportunistic in the same way, but over fifty temporaries rather than two,
        # which is the shape a warm Lambda reusing one extractor actually has. The
        # address-keyed version returned a spread of earlier documents' values here.
        extractor = DataExtractor()
        rule = _rule()
        readings = [
            extractor.extract_values(rule, {"doc": {"coverage": float(i)}})["coverage"]
            for i in range(50)
        ]
        assert readings == [float(i) for i in range(50)]

    def test_a_document_mutated_between_calls_is_read_again(self):
        # The same root cause seen from the other side: an address that has NOT
        # changed while the content has. A memo outliving the call served the stale
        # reading here too.
        extractor = DataExtractor()
        rule = _rule()
        document = {"doc": {"coverage": 1.0}}
        assert extractor.extract_values(rule, document) == {"coverage": 1.0}
        document["doc"]["coverage"] = 2.0
        assert extractor.extract_values(rule, document) == {"coverage": 2.0}

    def test_a_miss_is_memoized_without_being_confused_with_absence_of_a_memo(self):
        # `None` is a legitimate reading (an optional parameter's path missed), so
        # the memo has to be probed by membership rather than by truthiness.
        extractor = DataExtractor()
        cache: dict[str, object] = {}
        assert (
            extractor._extract_path({"doc": {}}, "doc.coverage", "r1", path_cache=cache)
            is None
        )
        assert cache == {"doc.coverage": None}
        # Second read comes from the memo, and is still None rather than re-walked
        # into something else.
        assert (
            extractor._extract_path({"doc": {}}, "doc.coverage", "r1", path_cache=cache)
            is None
        )

    def test_extract_path_without_a_memo_still_reads(self):
        # Direct callers (tests, notebooks) pass no memo; they get one scoped to the
        # single call, which is to say no memoization at all.
        assert (
            DataExtractor()._extract_path({"doc": {"coverage": 3.0}}, "doc.coverage")
            == 3.0
        )

    def test_the_extractor_retains_nothing_after_a_call(self):
        # Stated as a property rather than as an assertion about a named attribute:
        # no attribute of the extractor may hold the document or its values, which
        # is what makes one instance safe to reuse and keeps a warm Lambda from
        # accumulating documents.
        extractor = DataExtractor()
        marker = "coverage-marker-value"
        extractor.extract_values(
            _rule(paths=(("coverage", "doc.coverage"),)),
            {"doc": {"coverage": 1.0, "note": marker}},
        )
        retained = [
            name
            for name, value in vars(extractor).items()
            if marker in repr(value) or "1.0" in repr(value)
        ]
        assert retained == []


@pytest.mark.unit
class TestClearCache:
    """`clear_cache()` is retained for callers and does nothing."""

    def test_clearing_is_safe_and_changes_no_reading(self):
        extractor = DataExtractor()
        rule = _rule()
        before = extractor.extract_values(rule, {"doc": {"coverage": 1.0}})
        extractor.clear_cache()
        after = extractor.extract_values(rule, {"doc": {"coverage": 2.0}})
        assert before == {"coverage": 1.0}
        assert after == {"coverage": 2.0}

    def test_clearing_before_anything_has_been_read_is_safe(self):
        DataExtractor().clear_cache()

    def test_reads_across_documents_do_not_depend_on_clearing(self):
        # The pair that matters: the same two documents read with and without the
        # call in between produce the same answers. Correctness across documents is
        # a property of the extractor, not of remembering to call this.
        rule = _rule()
        with_clear = DataExtractor()
        first = with_clear.extract_values(rule, {"doc": {"coverage": 1.0}})
        with_clear.clear_cache()
        second = with_clear.extract_values(rule, {"doc": {"coverage": 2.0}})

        without_clear = DataExtractor()
        third = without_clear.extract_values(rule, {"doc": {"coverage": 1.0}})
        fourth = without_clear.extract_values(rule, {"doc": {"coverage": 2.0}})

        assert (first, second) == (third, fourth)
