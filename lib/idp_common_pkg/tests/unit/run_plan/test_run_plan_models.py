# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The run plan model, and its encoding as a message attribute."""

import json
import os
import subprocess  # nosec B404 - fixed argv, no shell
import sys
from typing import cast

import pytest
from pydantic import ValidationError

from idp_common.run_plan.models import (
    MAX_RUN_PLAN_BYTES,
    RUN_PLAN_ATTRIBUTE,
    RunKind,
    RunPlan,
    RunPlanError,
    decode,
    encode,
)

pytestmark = pytest.mark.unit

#: Every row of the "which plan each sender builds" table.
_KIND_TABLE = [
    pytest.param(dict(kind="new_upload", start="beginning"), id="upload"),
    pytest.param(dict(kind="reprocess", start="beginning"), id="reprocess-all"),
    pytest.param(dict(kind="reprocess", start="ocr"), id="reprocess-ocr-alias"),
    pytest.param(dict(kind="reprocess", start="classification"), id="reprocess-step"),
    pytest.param(
        dict(kind="reprocess", start="summarization", skip=["evaluation"]),
        id="reprocess-with-a-skip",
    ),
    pytest.param(
        dict(kind="reprocess", start="policy_rule_validation", reviewed=True),
        id="reprocess-a-reviewed-document",
    ),
    pytest.param(
        dict(kind="saved_edits", start="extraction", sections=["2", "5"]),
        id="saved-edits-sections",
    ),
    pytest.param(
        dict(kind="saved_edits", start="classification"), id="saved-edits-class"
    ),
    pytest.param(
        dict(kind="saved_edits", start="policy_rule_validation"),
        id="saved-edits-values",
    ),
    pytest.param(dict(kind="legacy_review", start="beginning"), id="legacy-review"),
    pytest.param(
        dict(
            kind="review_completed",
            start="apply_review_changes",
            cycle_id="01J0000000000000000000",
            reviewed=True,
        ),
        id="review-completed",
    ),
    pytest.param(
        dict(
            kind="review_completed",
            start="extraction",
            sections=["2", "5"],
            cycle_id="01J0000000000000000000",
        ),
        id="review-completed-sections-extracted-first",
    ),
    pytest.param(
        dict(
            kind="review_completed",
            start="extraction",
            cycle_id="01J0000000000000000000",
        ),
        id="review-completed-every-section-extracted-first",
    ),
    pytest.param(
        dict(
            kind="review_rejected", start="rejected", cycle_id="01J0000000000000000000"
        ),
        id="review-rejected",
    ),
    pytest.param(
        dict(
            kind="review_skipped",
            start="policy_rule_validation",
            cycle_id="01J0000000000000000000",
        ),
        id="review-skipped",
    ),
    pytest.param(
        dict(kind="sent_back", start="extraction", cycle_id="01J0000000000000000000"),
        id="sent-back",
    ),
    pytest.param(dict(kind="restart_failed", start="assessment"), id="restart-failed"),
    pytest.param(
        dict(kind="restart_failed", start="extraction", sections=["3"]),
        id="restart-failed-sections",
    ),
]


class TestKinds:
    @pytest.mark.parametrize("fields", _KIND_TABLE)
    def test_kind_table(self, fields):
        plan = RunPlan(**fields)

        assert decode(encode(plan)) == plan
        assert plan.v == 1

    def test_every_kind_is_in_the_table(self):
        covered = {cast(dict, param.values[0])["kind"] for param in _KIND_TABLE}

        assert covered == {kind.value for kind in RunKind}

    def test_the_defaults(self):
        plan = RunPlan(kind=RunKind.REPROCESS, start="beginning")

        assert (plan.skip, plan.sections, plan.reviewed, plan.cycle_id) == (
            [],
            [],
            False,
            "",
        )


class TestInvalidCombinations:
    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            (dict(kind="new_upload", start="extraction"), "starts at 'beginning'"),
            (dict(kind="legacy_review", start="extraction"), "starts at 'beginning'"),
            (
                dict(kind="review_completed", start="beginning"),
                "starts at 'apply_review_changes'",
            ),
            (
                dict(kind="review_completed", start="policy_rule_validation"),
                "a review_completed run starts at 'apply_review_changes' or "
                "'extraction', not 'policy_rule_validation'",
            ),
            (
                dict(kind="review_completed", start="rejected"),
                "starts at 'apply_review_changes' or 'extraction'",
            ),
            (
                dict(kind="review_completed", start="assessment", sections=["1"]),
                "starts at 'apply_review_changes' or 'extraction'",
            ),
            (
                dict(kind="review_completed", start="extraction", reviewed=True),
                "does not count as reviewed",
            ),
            (
                dict(
                    kind="review_completed",
                    start="apply_review_changes",
                    sections=["1"],
                ),
                "sections can be named only",
            ),
            (dict(kind="review_rejected", start="beginning"), "starts at 'rejected'"),
            (
                dict(kind="review_skipped", start="beginning"),
                "starts at 'policy_rule_validation'",
            ),
            (
                dict(kind="reprocess", start="rejected"),
                "only a run that follows a review",
            ),
            (
                dict(kind="sent_back", start="apply_review_changes"),
                "only a run that follows a review",
            ),
            (dict(kind="reprocess", start="nope"), "not a start step"),
            (
                dict(kind="reprocess", start="beginning", skip=["extraction"]),
                "extraction cannot be skipped",
            ),
            (
                dict(kind="reprocess", start="beginning", skip=["nope"]),
                "nope cannot be skipped",
            ),
            (
                dict(kind="reprocess", start="beginning", sections=["1"]),
                "sections can be named only",
            ),
            (
                dict(kind="reprocess", start="beginning", cycle_id="c"),
                "does not follow a review cycle",
            ),
            (
                dict(kind="reprocess", start="extraction", reviewed=True),
                "does not count as reviewed",
            ),
            (
                dict(kind="reprocess", start="assessment", reviewed=True),
                "does not count as reviewed",
            ),
            (
                dict(kind="reprocess", start="extraction", sections=[""]),
                "a section id is 1 to 64",
            ),
            (
                dict(kind="reprocess", start="extraction", sections=["x" * 65]),
                "a section id is 1 to 64",
            ),
            (
                dict(
                    kind="reprocess",
                    start="extraction",
                    sections=[str(i) for i in range(501)],
                ),
                "at most 500 sections",
            ),
            (dict(kind="nope", start="beginning"), "kind"),
            (dict(kind="reprocess", start="beginning", unknown=1), "unknown"),
            (dict(kind="reprocess", start="beginning", v=2), "v"),
        ],
    )
    def test_they_are_refused(self, fields, message):
        with pytest.raises(ValidationError) as raised:
            RunPlan(**fields)

        assert message in str(raised.value)

    def test_a_completed_review_that_extracts_first_is_not_marked_reviewed(self):
        """The sections are extracted again, so the review does not stand until
        the corrections are applied to the new results."""
        plan = RunPlan(
            kind=RunKind.REVIEW_COMPLETED,
            start="extraction",
            sections=["3"],
            cycle_id="01J0000000000000000000",
        )

        assert (plan.start, plan.sections, plan.reviewed) == (
            "extraction",
            ["3"],
            False,
        )
        assert decode(encode(plan)) == plan

    @pytest.mark.parametrize(
        "kind", [kind for kind in RunKind if kind != RunKind.REVIEW_COMPLETED]
    )
    def test_only_a_completed_review_starts_at_apply_review_changes(self, kind):
        with pytest.raises(ValidationError):
            RunPlan(kind=kind, start="apply_review_changes")

    def test_resume_is_a_start_until_it_is_resolved(self):
        assert RunPlan(kind=RunKind.RESTART_FAILED, start="resume").start == "resume"


class TestSkip:
    def test_the_skip_list_is_sorted_and_unique(self):
        plan = RunPlan(
            kind=RunKind.REPROCESS,
            start="beginning",
            skip=["summarization", "assessment", "summarization"],
        )

        assert plan.skip == ["assessment", "summarization"]


class TestTheAttribute:
    def test_the_attribute_name(self):
        assert RUN_PLAN_ATTRIBUTE == "RunPlan"

    def test_attribute_is_deterministic(self):
        first = RunPlan(
            kind=RunKind.REPROCESS,
            start="summarization",
            skip=["evaluation", "assessment"],
        )
        # The same plan built with its fields in another order.
        second = RunPlan(
            skip=["assessment", "evaluation"],
            start="summarization",
            kind="reprocess",
        )

        assert encode(first) == encode(second)
        assert encode(first).encode("utf-8") == encode(first).encode("utf-8")
        assert encode(first) == (
            '{"cycle_id":"","kind":"reprocess","reviewed":false,"sections":[],'
            '"skip":["assessment","evaluation"],"start":"summarization","v":1}'
        )

    def test_the_plan_holds_no_timestamp_and_no_random_value(self):
        """Every field is caller-supplied, so a re-sent message carries equal bytes."""
        assert set(RunPlan.model_fields) == {
            "v",
            "kind",
            "start",
            "skip",
            "sections",
            "reviewed",
            "cycle_id",
        }

    def test_an_oversized_plan_cannot_be_encoded(self):
        plan = RunPlan(
            kind=RunKind.REPROCESS,
            start="extraction",
            sections=[f"{i:064d}" for i in range(500)],
        )

        with pytest.raises(RunPlanError) as raised:
            encode(plan)

        assert str(MAX_RUN_PLAN_BYTES) in str(raised.value)

    def test_a_plan_with_many_short_section_ids_fits(self):
        plan = RunPlan(
            kind=RunKind.REPROCESS,
            start="extraction",
            sections=[str(i) for i in range(500)],
        )

        assert len(encode(plan).encode("utf-8")) <= MAX_RUN_PLAN_BYTES

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "not json",
            "[]",
            "null",
            '{"kind":"reprocess"}',
            '{"kind":"reprocess","start":"beginning","v":2}',
            '{"kind":"reprocess","start":"beginning","surprise":true}',
            '{"kind":"review_completed","start":"beginning"}',
        ],
    )
    def test_decode_refuses_what_is_not_a_plan(self, value):
        with pytest.raises(RunPlanError):
            decode(value)

    def test_decode_refuses_a_value_that_is_not_text(self):
        with pytest.raises(RunPlanError):
            decode(None)  # type: ignore[arg-type]

    def test_a_run_plan_error_is_a_value_error(self):
        assert issubclass(RunPlanError, ValueError)

    def test_key_order_in_the_attribute_does_not_matter(self):
        text = encode(RunPlan(kind=RunKind.REPROCESS, start="beginning"))
        reordered = json.dumps(dict(reversed(list(json.loads(text).items()))))

        assert decode(reordered) == decode(text)


_IMPORT_PROBE = r"""
import json
import sys

before = set(sys.modules)
import idp_common.run_plan.models
import idp_common.run_plan.asl
import idp_common.run_plan.steps

added = set(sys.modules) - before
stdlib = set(sys.stdlib_module_names)
print(
    json.dumps(
        {
            "first_party": sorted(m for m in added if m.split(".")[0] == "idp_common"),
            "third_party": sorted(
                {
                    m.split(".")[0]
                    for m in added
                    if m.split(".")[0] not in stdlib
                    and m.split(".")[0] != "idp_common"
                    and not m.startswith("_")
                }
            ),
        }
    )
)
"""


def test_the_plan_modules_load_only_pydantic_and_the_standard_library():
    """The queue processor reads a plan without loading the review code."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    run = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-c", _IMPORT_PROBE],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    loaded = json.loads(run.stdout.strip().splitlines()[-1])

    assert loaded["first_party"] == [
        "idp_common",
        "idp_common.run_plan",
        "idp_common.run_plan.asl",
        "idp_common.run_plan.models",
        "idp_common.run_plan.steps",
    ]
    assert set(loaded["third_party"]) <= {
        "pydantic",
        "pydantic_core",
        "typing_extensions",
        "annotated_types",
        "typing_inspection",
    }, loaded["third_party"]
