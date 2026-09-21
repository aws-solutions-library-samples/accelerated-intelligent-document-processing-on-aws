# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``ProcessingIssue``'s two free-size fields are bounded, and bounded carefully.

The limit being defended is DynamoDB's **400 KB item ceiling**. A section map holds
every issue on the section, and both `root_cause` and `details` reach six figures
from ordinary inputs:

* `root_cause` is overwhelmingly `f"{type(e).__name__}: {e}"` from a broad `except`,
  and a Pydantic `ValidationError` over a merged long list renders one entry per
  offending row echoing its `input_value`;
* `details` gets there through *authored* content — `_build_extraction_issues` keeps
  the first five jsonschema messages, and jsonschema embeds `repr(instance)` in each,
  so one `minItems` failure on a 900-row list is 57,592 characters (jsonschema
  4.25.1). `[:5]` bounds the count and not the size, and it is the `[:5]` that sets
  the scale: five such messages are **281 KB, 70% of the ceiling in one field**.

Past the ceiling the section write raises, the extraction failure path swallows that
deliberately so it cannot mask the original exception, and the section is left
**completely unmarked** — on exactly the large documents the issue existed to
explain.

Three properties are easy to get wrong and each has its own test below:

1. **Nothing may be logged on a READ.** `ProcessingIssue` is rebuilt by
   `Section.from_dict` on every Step Functions hand-off and by `get_document`, so a
   bound that re-fires on an already-bounded value warns once per read about work
   done once on write.
2. **Authored content keeps its tail.** The extraction and assessment sites end with
   the remedy and put a variable-length list in the middle, so the middle is what
   goes. The classification site is the exception — its page list is at the tail —
   and where it crosses the ceiling is pinned rather than assumed.
3. **The bound is on bytes.** A test whose fixture is over the ceiling in *both*
   units cannot see a size guard that switched to characters, so there is a case that
   straddles it: 2,000 CJK characters is 6,000 UTF-8 bytes.
"""

from __future__ import annotations

import json
import logging

import pytest

from idp_common.models import Document, ProcessingIssue, Section


def _issue(root_cause: str = "", details: dict | None = None) -> ProcessingIssue:
    return ProcessingIssue(
        stage="extraction",
        severity="error",
        code="extraction_failed",
        message="m",
        root_cause=root_cause,
        section_id="1",
        details=details or {},
    )


@pytest.mark.unit
def test_root_cause_is_bounded_in_bytes_not_characters():
    """A multi-byte string must be measured the way DynamoDB measures it."""
    cjk = "漢" * 40_000  # 120,000 UTF-8 bytes, 40,000 characters
    issue = _issue(root_cause=cjk)
    encoded = len(issue.root_cause.encode("utf-8"))
    assert encoded <= ProcessingIssue.MAX_ROOT_CAUSE_BYTES, (
        f"{encoded} bytes survived a {ProcessingIssue.MAX_ROOT_CAUSE_BYTES}-byte "
        "bound, so the bound is counting characters"
    )


@pytest.mark.unit
def test_a_value_under_the_ceiling_in_characters_and_over_it_in_bytes_is_bounded():
    """The case a 40,000-character fixture cannot discriminate.

    40,000 characters is past a 4,096 ceiling in *either* unit, so a bound that
    measured characters would still fire on it and the test above would still pass.
    The regression that actually escapes is a **partial** conversion — the size guard
    moved to characters while the slicing stays byte-based — and only a value that is
    under the ceiling in characters and over it in bytes can see that. 2,000 CJK
    characters is 6,000 UTF-8 bytes: under 4,096 counted one way, half again over it
    counted the other.
    """
    cjk = "漢" * 2_000
    assert len(cjk) < ProcessingIssue.MAX_ROOT_CAUSE_BYTES < len(cjk.encode("utf-8")), (
        "fixture precondition: this string must straddle the ceiling — under it in "
        f"characters ({len(cjk)}) and over it in bytes ({len(cjk.encode('utf-8'))})"
    )
    encoded = len(_issue(root_cause=cjk).root_cause.encode("utf-8"))
    assert encoded <= ProcessingIssue.MAX_ROOT_CAUSE_BYTES, (
        f"{encoded} bytes survived a {ProcessingIssue.MAX_ROOT_CAUSE_BYTES}-byte "
        "ceiling. The size guard is counting characters even though the elision "
        "slices bytes."
    )


@pytest.mark.unit
def test_the_result_never_overshoots_the_bound():
    """Appending the marker *outside* the budget is the easy mistake: the value then
    exceeds its own stated limit, and re-reading it elides again."""
    for size in (4_097, 10_000, 500_000):
        issue = _issue(root_cause="x" * size)
        assert (
            len(issue.root_cause.encode("utf-8"))
            <= ProcessingIssue.MAX_ROOT_CAUSE_BYTES
        ), f"a {size}-byte root_cause came back over the bound"


@pytest.mark.unit
def test_the_head_and_the_tail_both_survive():
    """Property 2. The middle goes, because the remedy is at the end."""
    head = "ExtractionOutputIncomplete: extracted 43 rows"
    tail = "Set extraction.row_shortfall_action to 'warn' to accept a partial list."
    issue = _issue(root_cause=f"{head} {'row content ' * 50_000}{tail}")
    assert issue.root_cause.startswith(head)
    assert issue.root_cause.endswith(tail)
    assert "elided" in issue.root_cause


def _classification_worst_case(n_pages: int) -> str:
    """The classification site's real template, at ``n_pages`` page ids.

    Mirrors ``classification/service.py``: up to ``_MAX_ISSUE_DETAILS`` (3) distinct
    reasons, each already capped at ``_MAX_ISSUE_DETAIL_CHARS`` (500), joined with
    ``"; "``, then an ``"; and N more"``, then the section's whole page list in
    parentheses. Reproduced rather than imported because the point is the *size* the
    template can reach, and a real call would need a whole classification run.
    """
    reasons = "; ".join(["d" * 500] * 3) + "; and 7 more"
    pages = ", ".join(str(n) for n in range(1, n_pages + 1))
    return f"{reasons} (pages {pages})"


@pytest.mark.unit
def test_authored_root_causes_at_realistic_sizes_are_not_clipped():
    """Property 2, at the sizes the tree actually produces.

    The extraction and assessment sites are comfortably inside the bound. The
    classification site is **not** comfortable — see the test below — so its case
    here is the size it reaches at a page count a single section plausibly holds.
    """
    page_ids = ", ".join(str(n) for n in range(1, 501))
    authored = [
        # assessment/service.py — page list then the remedy sentence
        f"Pages missing from the document: {page_ids}. Check the Classification "
        "step's section boundaries and the OCR step's page list.",
        # extraction/failure.py, four blocking column-width groups
        "ExtractionOutputIncomplete: Section 2 extraction is materially incomplete: "
        + "; ".join(
            f"Extracted 43 rows for group {n} but the section's OCR evidences "
            f"about 1200."
            for n in range(4)
        )
        + " Set extraction.row_shortfall_action to 'warn' to accept a partial list "
        "as success.",
        # classification/service.py, its real template at 500 page ids
        _classification_worst_case(500),
    ]
    for text in authored:
        assert _issue(root_cause=text).root_cause == text, (
            f"an authored root_cause of {len(text.encode('utf-8'))} bytes was "
            "elided; the bound is too tight for content the tree really writes"
        )


@pytest.mark.unit
def test_where_the_classification_site_crosses_the_bound_is_known_and_narrow():
    """The one authored site that can reach the ceiling, pinned at both ends.

    Its worst case is 3,915 bytes at 500 page ids — 96% of a 4,096-byte ceiling — so
    "no authored content is ever touched" is not true of this site, and a reader of
    the bound should know where the edge is rather than discovering it. It takes more
    than 530 page ids on a single section to cross, and crossing is acceptable
    because the same ids are also in ``details["page_ids"]`` and in the unbounded
    ``message``: what is lost is a duplicate, not the diagnosis.

    Both directions are asserted, because a bound whose crossing point drifts
    silently is the thing that makes the paragraph above stale.
    """
    just_inside = _classification_worst_case(530)
    assert _issue(root_cause=just_inside).root_cause == just_inside, (
        f"the classification template at 530 page ids "
        f"({len(just_inside.encode('utf-8'))} bytes) is now elided; the ceiling moved "
        "down or the template grew, and the documented edge is wrong"
    )

    over = _classification_worst_case(600)
    assert _issue(root_cause=over).root_cause != over, (
        f"the classification template at 600 page ids "
        f"({len(over.encode('utf-8'))} bytes) is no longer elided, so the ceiling has "
        "been raised. That weakens the bound for the exception-derived text it exists "
        "for; the page list is redundant here and was the acceptable thing to lose"
    )


@pytest.mark.unit
def test_nothing_is_logged_when_an_already_bounded_issue_is_re_read(caplog):
    """Property 1, through the REAL read path.

    `Section.from_dict` and `Document.from_dict` are the Step Functions state
    hand-offs. Three passes over one issue is an ordinary document's lifetime.
    """
    issue = _issue(root_cause="x" * 500_000)
    with caplog.at_level(logging.WARNING, logger="idp_common.models"):
        # The construction above legitimately logs once; the reads below must not.
        caplog.clear()
        stored = issue.to_dict()
        for _ in range(3):
            section = Section.from_dict(
                {
                    "section_id": "1",
                    "classification": "bank-statement",
                    "page_ids": ["1"],
                    "processing_issues": [stored],
                }
            )
            stored = section.processing_issues[0].to_dict()
        document = Document.from_dict(
            {
                "id": "d",
                "input_key": "d.pdf",
                "sections": [
                    {
                        "section_id": "1",
                        "classification": "bank-statement",
                        "page_ids": ["1"],
                        "processing_issues": [stored],
                    }
                ],
            }
        )
    assert caplog.records == [], (
        "a read re-elided an already-bounded issue and logged about it: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    # And the value is stable across those passes rather than shrinking each time.
    assert document.sections[0].processing_issues[0].root_cause == issue.root_cause


@pytest.mark.unit
def test_a_value_that_carries_the_marker_and_is_still_oversized_is_still_bounded(
    caplog,
):
    """Recognising the marker may suppress the LOG; it may not suppress the bound.

    Otherwise a substring is enough to get past a ceiling whose whole purpose is to
    keep the item writable — and the marker is reachable in text nobody elided here,
    because an exception can quote an already-elided value into its own message.
    """
    smuggled = f"head … [123 bytes elided] … {'q' * 300_000} tail"
    with caplog.at_level(logging.WARNING, logger="idp_common.models"):
        issue = _issue(root_cause=smuggled, details={"errors": [smuggled]})
    assert (
        len(issue.root_cause.encode("utf-8")) <= ProcessingIssue.MAX_ROOT_CAUSE_BYTES
    ), "a marked value was waved through the root_cause bound"
    assert (
        len(issue.details["errors"][0].encode("utf-8"))
        <= ProcessingIssue.MAX_DETAIL_STRING_BYTES
    ), "a marked value was waved through the details bound"
    # Bounded, and quietly: the marker is taken as evidence it has been reported.
    assert caplog.records == []


@pytest.mark.unit
def test_the_producing_write_does_log_once(caplog):
    """The counterpart to the test above: silence on read must not become silence
    everywhere, or an oversized value is bounded with no record that it happened."""
    with caplog.at_level(logging.WARNING, logger="idp_common.models"):
        _issue(root_cause="x" * 500_000)
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "root_cause" in message
    # It must not send the reader somewhere the unabridged text is not. The previous
    # wording said "the full text is in the logs above", which is false on a read.
    assert "logs above" not in message


@pytest.mark.unit
def test_the_sibling_assessment_path_is_covered_without_an_edit_of_its_own(caplog):
    """The bound is on the model, which is what makes this a class-level fix.

    `assessment/degradation.py` builds `root_cause` from its own broad `except` and
    is reached by the same oversized-input failure. Bounding only the extraction
    site would have fixed the instance and left the class open — this repository's
    recurring defect shape — so the coverage is asserted rather than assumed, and
    `degradation.py` carries no bounding code of its own.
    """
    from idp_common.assessment import degradation
    from idp_common.assessment.degradation import degrade_section_to_no_confidence

    document = Document(
        id="d",
        input_key="d.pdf",
        sections=[Section(section_id="1", classification="x", page_ids=["1"])],
    )
    remedy = "use a confidence model with a larger context window."
    issue = degrade_section_to_no_confidence(
        document, "1", ValueError(f"scored rows: {'z' * 200_000} {remedy}")
    )
    assert len(issue.root_cause.encode("utf-8")) <= ProcessingIssue.MAX_ROOT_CAUSE_BYTES
    assert issue.root_cause.startswith("ValueError: scored rows:")
    assert issue.root_cause.endswith(remedy)

    # And it really is covered by the model rather than by a local copy of the rule.
    from pathlib import Path

    source = Path(degradation.__file__).read_text(encoding="utf-8")
    for token in ("_elide_middle", "MAX_ROOT_CAUSE_BYTES", "[:2048]", "elided"):
        assert token not in source, (
            f"degradation.py now bounds root_cause itself ({token!r}). That is the "
            "per-site fix this arrangement exists to avoid; remove it and rely on "
            "the model."
        )


@pytest.mark.unit
def test_a_real_jsonschema_error_in_details_is_bounded():
    """The `details` case, built from the real library rather than a synthetic
    string, because the size comes from jsonschema embedding `repr(instance)` and a
    synthetic value would not demonstrate that."""
    jsonschema = pytest.importorskip("jsonschema")

    schema = {
        "type": "object",
        "properties": {
            "Transactions": {
                "type": "array",
                "minItems": 1200,
                "items": {"type": "object"},
            }
        },
    }
    instance = {
        "Transactions": [
            {"description": f"PAYMENT TO MERCHANT {n}", "amount": f"{n * 1.37:.2f}"}
            for n in range(900)
        ]
    }
    errors = [
        e.message for e in jsonschema.Draft7Validator(schema).iter_errors(instance)
    ]
    assert len(errors) == 1, "fixture precondition: one error"
    assert len(errors[0]) > 50_000, (
        "fixture precondition: jsonschema still embeds repr(instance), which is what "
        f"makes this large (got {len(errors[0])} characters)"
    )

    # The shape _build_extraction_issues writes: the count is bounded by [:5], the
    # size is not.
    issue = _issue(details={"errors": errors[:5], "error_count": 1})

    serialised = len(json.dumps(issue.details, default=str).encode("utf-8"))
    assert serialised < 8_192, (
        f"details serialised to {serialised} bytes; eight such fields on one section "
        "exceed DynamoDB's 400 KB item budget, and the write that fails is the one "
        "whose error is deliberately swallowed"
    )
    # The structure a consumer reads is intact — keys, types and list length.
    assert issue.details["error_count"] == 1
    assert isinstance(issue.details["errors"], list)
    assert len(issue.details["errors"]) == 1
    # jsonschema puts `repr(instance)` at the FRONT and the verdict at the back —
    # "[...900 rows...] is too short" — so the tail is what says what was wrong. A
    # tail-clipping bound would have kept 1 KB of transaction rows and dropped it.
    assert issue.details["errors"][0].endswith("is too short")
    assert "elided" in issue.details["errors"][0]


@pytest.mark.unit
def test_the_five_error_shape_the_slice_exists_for_is_bounded():
    """One oversized message is the easy case; `[:5]` is the one that sets the scale.

    `_build_extraction_issues` keeps `errors[:5]`, so the worst case for a single
    `details` field is five of them, not one — a document with five list-bearing
    properties all short of their `minItems`. Unbounded that is one field holding
    **70% of DynamoDB's 400 KB item ceiling on its own**, so two fields like it
    exceed the limit and the section write fails silently. A single-error fixture
    understates the need by a factor of five, which is why this case exists
    separately.
    """
    jsonschema = pytest.importorskip("jsonschema")

    schema = {
        "type": "object",
        "properties": {
            f"Table{i}": {
                "type": "array",
                "minItems": 1200,
                "items": {"type": "object"},
            }
            for i in range(5)
        },
    }
    rows = [
        {"description": f"PAYMENT TO MERCHANT {n}", "amount": f"{n * 1.37:.2f}"}
        for n in range(900)
    ]
    instance = {f"Table{i}": rows for i in range(5)}
    errors = [
        e.message for e in jsonschema.Draft7Validator(schema).iter_errors(instance)
    ]
    assert len(errors) == 5, f"fixture precondition: five errors, got {len(errors)}"

    unbounded = len(
        json.dumps({"errors": errors[:5], "error_count": 5}, default=str).encode(
            "utf-8"
        )
    )
    ceiling = 400 * 1024
    assert unbounded > ceiling // 2, (
        "fixture precondition: five jsonschema minItems messages over a 900-row list "
        f"must dominate the item budget to make the point ({unbounded} bytes vs a "
        f"{ceiling}-byte ceiling)"
    )

    issue = _issue(details={"errors": errors[:5], "error_count": 5})
    bounded = len(json.dumps(issue.details, default=str).encode("utf-8"))
    assert bounded < 8_192, (
        f"five bounded messages serialised to {bounded} bytes, from {unbounded} "
        "unbounded; the bound is not holding on the shape [:5] exists for"
    )
    # All five survive as five, and each still ends in its own verdict.
    assert len(issue.details["errors"]) == 5
    assert all(m.endswith("is too short") for m in issue.details["errors"])


@pytest.mark.unit
def test_details_structure_and_short_values_are_preserved_exactly():
    """Bounding must not reshape a payload that is already small — `details` is read
    by the processing report and the Visual Editor by key."""
    details = {
        "page_ids": ["1", "2", "3"],
        "item_property_count": 2,
        "ratio": 0.132,
        "nested": {"tuple_like": ["a", "b"], "flag": True, "nothing": None},
    }
    issue = _issue(details=details)
    assert issue.details == details


@pytest.mark.unit
def test_a_long_string_nested_deep_inside_details_is_still_bounded():
    """The recursion matters: the oversized leaf is inside a list inside a dict in
    the case that motivated this."""
    issue = _issue(details={"a": {"b": [{"c": "z" * 200_000}]}})
    leaf = issue.details["a"]["b"][0]["c"]
    assert len(leaf.encode("utf-8")) <= ProcessingIssue.MAX_DETAIL_STRING_BYTES
    assert "elided" in leaf
