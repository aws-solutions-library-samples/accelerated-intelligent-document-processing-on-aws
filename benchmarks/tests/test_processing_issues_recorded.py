# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Processing issues as a RECORDED benchmark figure (issue #1378).

What was missing
----------------
``grep -rn processing_issues benchmarks/harness/*.py`` returned nothing. Extraction,
classification and assessment all emit ``ProcessingIssue`` objects, and none of them
reached ``benchmarks/results/*/summary.json`` — so a run whose extraction reported
losing rows was recorded identically to a clean one. **A check that fires into a
field nobody reads is indistinguishable, in the results tree, from a check that does
not fire.** Issue #1361 was duly written as large documents losing rows "silently"
when ``extraction_rows_below_ocr_estimate`` had fired at every lossy count it
reported.

What these tests pin
--------------------
1. **A lossy-but-warned run is distinguishable from a clean one by reading
   ``summary.json`` alone** — through the real ``aggregate.write_summary`` and back
   out of ``json.load``. That is the property #1361's author could not establish and
   it is the point of the change.
2. **Severity survives.** A warning and an error do not collapse into one field.
3. **Every issue type the tree can emit is recorded**, derived from the tree rather
   than from a list written here, including the one code reachable only through a
   class constant (``extraction_rows_below_ocr_estimate`` — the code this issue is
   about, which literal-only discovery misses).
4. **A code no one has written yet is recorded with no harness change.** A recorder
   enumerating the issue types it knows would be the same defect it closes, one
   level up.
5. The keys reach ``CSV_COLS``: ``DictWriter(extrasaction="ignore")`` drops an
   unlisted row key in silence, which is how ``n_conf_leaves`` came to be computed
   by the scorer and readable from nothing.
6. A clean document records ``0``, never ``None`` — ``None`` is this harness's
   encoding for "could not be read" (``analyze.unread_sections_row``), and the
   ambiguity between "clean" and "not read" is the defect being closed.
7. Both scorers splice the recorder, so reference-corpus documents carry it too.
8. ``compare_cells`` reports the metric as uncomparable rather than unchanged when
   one side of a release pair predates it (``LATE_ADDED_METRICS``).

Note on where this runs: ``benchmarks/tests`` is in ``scripts/run_all_tests.py``'s
``RUN_ROOTS``, so ``make test`` covers it, and ``make test-packages-cicd`` runs it in
both CIs.
"""

from __future__ import annotations

import ast
import csv
import json
import os
from pathlib import Path

import pytest
from harness_import import harness_module

analyze = harness_module("analyze")
aggregate = harness_module("aggregate")
lib = harness_module("lib")

import idp_common  # noqa: E402
from idp_common.models import ProcessingIssue  # noqa: E402

IDP_COMMON = Path(idp_common.__file__).parent


# ---------------------------------------------------------------------------
# The authority: what this tree can actually emit, read off the tree.
# ---------------------------------------------------------------------------
#
# ``ProcessingIssue(...)`` call sites whose ``code`` is a value carried in from
# somewhere else, so they define no issue type of their own. Both are RE-EMITTERS of
# an issue some other site already built, and each entry is bounded to one function
# so the reason is a property of that member rather than of a directory:
#
# * ``dynamodb.service._document_from_item`` rehydrates issues off a stored DynamoDB
#   item (``iss.get("code", "")``) — its universe is whatever was written earlier.
# * ``document_failure._record`` copies a ``diagnosis`` built by its caller.
#
# Pinned rather than skipped: a THIRD unresolvable site means a service now emits
# codes this discovery cannot see, and the parametrised tests below would quietly
# stop covering them. The failure names the site.
ISSUE_CODE_PASS_THROUGH_SITES = {
    ("dynamodb/service.py", "_dynamodb_item_to_document"),
    ("document_failure.py", "record_section_failure"),
}

# Modules that DECLARE processing-issue codes or stages as module constants rather
# than spelling them at the construction site. `document_failure.py` is the one that
# matters: it declares `rule_validation_failed`, `rule_validation_not_consolidated`,
# `section_processing_failed` and both of the stage names, and its own
# `ProcessingIssue(...)` call is a pass-through taking a caller's diagnosis — so an
# AST walk of the construction sites alone finds 24 codes and 3 stages where the tree
# declares 28 and 5, and looks complete. `scripts/tests/test_failure_code_ui_parity.py`
# already treats these two modules as the authority for the same reason.
DECLARING_MODULES = ("document_failure.py", "extraction/failure.py")

# Stages whose issues this reader CANNOT see, and why — one entry, one stage's worth
# of reason. Both record onto the section through
# `document_failure.record_section_failure` from a handler in `patterns/unified/src/`,
# and both run AFTER assessment with nothing rewriting the section's `result.json`
# afterwards, so their issues reach the DynamoDB tracking row and never the S3 object
# `score_processing_issues` reads. The reasoning for not adding the tracking row as a
# second source is in that function's docstring.
#
# This is a declared boundary, not an exemption from scrutiny: the universe-closure
# test below partitions every `*_STAGE` constant in `document_failure.py` across the
# covered set and this one, so a THIRD such stage fails rather than being silently
# unread — which is the failure mode the whole issue is about.
STAGES_EXCLUDED_FROM_RESULT_JSON = {
    "rule_validation": (
        "patterns/unified/src/rule-validation-function and "
        "rule-validation-orchestration-function record onto the section after "
        "assessment; nothing rewrites result.json"
    ),
    "postprocessing": (
        "patterns/unified/src/processresults_function records onto the section "
        "after assessment; nothing rewrites result.json"
    ),
}

# The same boundary one level finer, and the half that actually costs something.
# These two are `assessment`-stage codes, so the stage list above does not reach
# them: `assessment/degradation.py` states in its own docstring that the degrade path
# "deliberately does not rewrite that file", and every one of
# `skip_section_no_confidence`'s call sites in `assessment/service.py` returns before
# the result.json write. They are ERROR severity on a document that COMPLETES, so
# unlike the failure-path codes above there is no `status` to fall back on — a
# section with no confidence scores at all reads as clean here.
#
# Derived-and-equality-checked rather than listed as a judgement: the closure test
# below compares this against the codes `degradation.py` declares AND computes the
# premise the boundary rests on (that module performs no S3 write), so a third code
# there fails, and the entry becoming wrong because degradation started writing
# result.json fails too.
CODES_EXCLUDED_FROM_RESULT_JSON = {
    "assessment_failed_confidence_unavailable",
    "assessment_skipped_confidence_unavailable",
}

#: Call-shaped source fragments that count as writing to S3, for the premise above.
S3_WRITE_MARKERS = ("write_content(", "put_object(", "upload_file(", "upload_fileobj(")


def _string_constants(tree):
    """``NAME = "literal"`` at module or class scope — and deliberately not deeper.

    Function LOCALS must not land here. ``code`` is a local in the classification
    site, reassigned per branch, so collecting locals makes ``code=code`` resolve to
    whichever branch the walk saw last: one real code, silently standing in for the
    three that site can emit. Resolving it from the mapping it indexes instead
    (:func:`_mappings_indexed_by`) gets all three.
    """
    out = {}
    bodies = [tree.body] + [
        node.body for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
    ]
    for body in bodies:
        for node in body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            if not isinstance(node.value, ast.Constant) or not isinstance(
                node.value.value, str
            ):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value.value
    return out


def _dict_literals(scope):
    """``NAME = {"k": "v", ...}`` assignments inside one function body."""
    out = {}
    for node in ast.walk(scope):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        keys = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
        values = [
            v.value
            for v in node.value.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        ]
        for target in node.targets:
            if isinstance(target, ast.Name):
                out[target.id] = (keys, values)
    return out


def _mappings_indexed_by(scope):
    """``{index_name: [mapping_name, ...]}`` for every ``mapping[index]`` in a scope.

    This is what resolves the classification site, where the code is a loop variable
    (``for code, _ in by_reason.items()``) and the only in-scope statement of its
    universe is the code-keyed mapping it indexes (``severities[code]``).
    """
    out = {}
    for node in ast.walk(scope):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and isinstance(node.slice, ast.Name)
        ):
            out.setdefault(node.slice.id, []).append(node.value.id)
    return out


def _resolve(node, constants, dicts, indexed_by):
    """Every string ``node`` can evaluate to, or ``None`` when it cannot be read.

    Five shapes, which is what the emitting sites actually use: a literal; a
    module- or class-level constant by name or by ``self.ATTR``; both arms of a
    conditional severity; a name bound by iterating a code-keyed mapping, resolved
    to that mapping's KEYS; and a ``mapping[code]`` subscript, resolved to its
    VALUES.
    """
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        if node.id in constants:
            return {constants[node.id]}
        keys = {
            key
            for mapping in indexed_by.get(node.id, ())
            if mapping in dicts
            for key in dicts[mapping][0]
        }
        return keys or None
    if isinstance(node, ast.Attribute):
        return {constants[node.attr]} if node.attr in constants else None
    if isinstance(node, ast.IfExp):
        left = _resolve(node.body, constants, dicts, indexed_by)
        right = _resolve(node.orelse, constants, dicts, indexed_by)
        return (left or set()) | (right or set()) if (left or right) else None
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        entry = dicts.get(node.value.id)
        return set(entry[1]) or None if entry else None
    return None


def processing_issue_universe():
    """Every ``stage``, ``severity`` and ``code`` a service here can emit.

    Read off the tree by AST rather than listed, for the same reason the recorder
    tallies rather than enumerates: a list written here goes stale the first time a
    service grows an issue type, and it goes stale in the direction that looks
    green. Returns the three sets plus the sites it could not read, which the test
    below pins.
    """
    codes, severities, stages, unresolved = set(), set(), set(), set()
    for path in sorted(IDP_COMMON.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = _string_constants(tree)
        scopes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for scope in scopes:
            dicts = _dict_literals(scope)
            indexed_by = _mappings_indexed_by(scope)
            for node in ast.walk(scope):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name != "ProcessingIssue":
                    continue
                kwargs = {k.arg: k.value for k in node.keywords}
                got = {
                    field: _resolve(kwargs.get(field), constants, dicts, indexed_by)
                    for field in ("stage", "severity", "code")
                }
                if got["code"] is None:
                    unresolved.add(
                        (
                            str(path.relative_to(IDP_COMMON)),
                            scope.name,
                        )
                    )
                    continue
                codes |= got["code"]
                severities |= got["severity"] or set()
                stages |= got["stage"] or set()
    # ...plus the codes and stages DECLARED as module constants in the modules whose
    # construction site is a pass-through. Without this the walk returns 24 codes and
    # 3 stages where the tree declares 28 and 5 — and the four it misses are the ones
    # whose issues this reader cannot see, so the omission hid the finding as well as
    # narrowing the coverage.
    #
    # Kept as a SEPARATE set from the walk's, because the difference is the finding:
    # a stage with a construction site inside a service is one whose issues travel
    # with that service's own writes, while a stage that exists only as a declared
    # constant handed to the `document_failure` pass-through is one this reader has
    # to reason about. The closure test below is that difference, as an equality.
    declared_stages: set[str] = set()
    for rel in DECLARING_MODULES:
        constants = _string_constants(ast.parse((IDP_COMMON / rel).read_text("utf-8")))
        codes |= {v for k, v in constants.items() if k.endswith("_CODE")}
        declared_stages |= {v for k, v in constants.items() if k.endswith("_STAGE")}
    return codes, severities, stages, declared_stages, unresolved


#: ``SITE_STAGES`` are the stages with a ``ProcessingIssue(...)`` construction site
#: inside a service, so their issues travel with that service's own writes.
#: ``DECLARED_STAGES`` are the stage names declared as constants and handed to the
#: ``document_failure`` pass-through. ``STAGES`` is every stage either way, and is what
#: the recorder must be agnostic to.
CODES, SEVERITIES, SITE_STAGES, DECLARED_STAGES, UNRESOLVED = (
    processing_issue_universe()
)
STAGES = SITE_STAGES | DECLARED_STAGES


def test_the_discovered_issue_universe_is_not_empty():
    """An empty derived set collects as a pytest SKIP, not a failure, so every
    parametrised test below would report green while covering nothing."""
    assert CODES, (
        "no ProcessingIssue codes discovered, so every parametrised test below "
        "collects as a SKIP with a green exit and covers nothing"
    )
    assert SEVERITIES, "no severities discovered"
    assert STAGES, "no stages discovered"
    # A floor, not an equality: a new issue type raises the count and costs nothing,
    # while a resolution rule that stopped reading one service's sites would drop it
    # and is the direction that hides.
    assert len(CODES) >= 28, sorted(CODES)
    assert len(STAGES) >= 5, sorted(STAGES)


def test_discovery_reaches_the_code_this_issue_is_about():
    """``extraction_rows_below_ocr_estimate`` is emitted as
    ``code=self.ROW_SHORTFALL_CODE``, so a discovery reading string literals only
    misses the one check #1361 was written about — while still returning sixteen
    other codes and looking complete."""
    assert "extraction_rows_below_ocr_estimate" in CODES
    assert "assessment_coverage_incomplete" in CODES
    assert "classification_failed" in CODES


@pytest.mark.parametrize("stage", sorted(SITE_STAGES))
def test_discovery_reaches_every_stage_with_a_construction_site(stage):
    """Parametrised over the stages DISCOVERED, not over a list here.

    The inventory was hand-written (`extraction`, `classification`, `assessment`) and
    that was the file's one authored universe: had it been derived it would have held
    `rule_validation` and `postprocessing` too and gone red, which is how a
    hand-written inventory hides exactly the thing it is meant to cover.

    The ``<stage>_`` code-naming convention holds for these three and not for the
    pass-through stages (``postprocessing``'s code is ``section_processing_failed``),
    which is itself the difference the closure below is about.
    """
    assert any(code.startswith(stage) for code in CODES), (stage, sorted(CODES))


@pytest.mark.parametrize("stage", sorted(STAGES))
def test_an_issue_from_any_stage_is_recorded(stage, tmp_path):
    """The axis a stage-keyed filter would exploit.

    There was no parametrisation over stages at all, so
    ``if entry.get("stage") == "ocr": continue`` — or any other stage rule — passed
    the suite. Codes and severities each had one; this is the third.
    """
    summary, _ = _written(
        tmp_path,
        [
            _row(
                "c",
                "d.pdf",
                [_section(_issue(f"{stage}_something_went_wrong", stage=stage))],
            )
        ],
    )
    (out,) = summary["rows"]
    assert out["processing_issues"] == 1
    assert out["processing_issues_by_stage"] == {stage: 1}


def test_the_codes_outside_result_json_are_exactly_the_degradation_pair():
    """Closure one level finer than the stage list, on the half that costs something.

    ``assessment_failed_confidence_unavailable`` and
    ``assessment_skipped_confidence_unavailable`` are ``assessment``-stage codes, so
    the stage boundary does not reach them, and they are **error** severity on a
    document that **completes** — there is no ``status`` to fall back on. Both are in
    the discovered 24 and both are recorded correctly by the parametrised test above
    *when injected*, which is precisely why this is needed: that green parametrisation
    says nothing about whether the pipeline can put them where the reader looks.

    Equality, so a third code declared in ``degradation.py`` fails here.
    """
    constants = _string_constants(
        ast.parse((IDP_COMMON / "assessment" / "degradation.py").read_text("utf-8"))
    )
    declared = {v for k, v in constants.items() if k.endswith("_CODE")}
    assert declared, "no *_CODE constants in assessment/degradation.py"
    assert declared == CODES_EXCLUDED_FROM_RESULT_JSON, sorted(
        declared ^ CODES_EXCLUDED_FROM_RESULT_JSON
    )
    # Non-vacuity: these have to be codes the discovery really finds, or the boundary
    # is describing something that does not exist.
    assert CODES_EXCLUDED_FROM_RESULT_JSON <= CODES


def test_the_premise_behind_that_boundary_is_computed_not_asserted():
    """The boundary holds *because* ``degradation.py`` writes no S3 object. That is
    computable, so it is computed: if the module ever gains a write, this entry is
    wrong in the direction that under-reports, and the failure says to re-check it
    rather than leaving a stale reason in place.

    Its own docstring says the same thing in prose — "this degrade path deliberately
    does not rewrite that file" — and prose is what goes stale.
    """
    source = (IDP_COMMON / "assessment" / "degradation.py").read_text("utf-8")
    writes = [m for m in S3_WRITE_MARKERS if m in source]
    assert not writes, (
        f"assessment/degradation.py now contains {writes}. If it writes the section's "
        "result.json, CODES_EXCLUDED_FROM_RESULT_JSON is wrong and the recorder's docstring "
        "and docs/benchmarking/index.md overstate the boundary."
    )
    # And the marker list is live rather than decorative: the same probe must find a
    # write in a module that definitely has one.
    assert any(
        m in (IDP_COMMON / "extraction" / "service.py").read_text("utf-8")
        for m in S3_WRITE_MARKERS
    ), (
        "S3_WRITE_MARKERS matches nothing even where a write exists, so the check above is vacuous"
    )


def test_every_skip_site_returns_before_the_result_json_write():
    """The other half of the same premise, for the skip path rather than the degrade
    path. ``skip_section_no_confidence``'s callers reach the result.json write only if
    one of them fails to return, and that is what would silently turn these codes from
    invisible into visible — or, worse, leave them half visible."""
    source = (IDP_COMMON / "assessment" / "service.py").read_text("utf-8")
    lines = source.splitlines()
    sites = [
        i
        for i, line in enumerate(lines)
        if "skip_section_no_confidence(" in line and "import" not in line
    ]
    assert len(sites) >= 5, f"expected the five known skip sites, found {len(sites)}"
    for i in sites:
        window = lines[i : i + 25]
        assert any(line.strip().startswith("return ") for line in window), (
            f"assessment/service.py:{i + 1} calls skip_section_no_confidence and the "
            "next 25 lines contain no return. If execution now continues to the "
            "result.json write, CODES_EXCLUDED_FROM_RESULT_JSON is wrong."
        )


def test_every_declared_stage_is_either_covered_or_a_named_boundary():
    """Universe closure over the stages, which is the half an AST walk of
    ``idp_common`` cannot give on its own.

    ``rule_validation`` and ``postprocessing`` issues are built by handlers in
    ``patterns/unified/src/`` through ``document_failure.record_section_failure``, a
    pass-through this discovery deliberately does not resolve — so they appear in
    neither ``CODES`` nor ``STAGES``, and nothing above would notice that this
    reader cannot see them. The stage NAMES are declared in ``document_failure.py``,
    so that is the authority to close against: every ``*_STAGE = "..."`` constant
    must be either a stage whose issues reach the section's ``result.json`` or a
    named entry in :data:`STAGES_EXCLUDED_FROM_RESULT_JSON`. A third such stage added later
    lands in neither and fails here.
    """
    assert STAGES, "no stages discovered at all"
    assert SITE_STAGES, "no stage has a construction site inside a service"
    assert DECLARED_STAGES, "the declaring-module route found nothing"
    # An EQUALITY with no authored inventory on either side: the left is every stage
    # whose only construction is the pass-through, the right is the boundary this
    # reader declares. A sixth stage lands in the left and not the right and fails
    # here; a boundary entry that stops being outside the sites fails here too.
    outside = STAGES - SITE_STAGES
    assert outside == set(STAGES_EXCLUDED_FROM_RESULT_JSON), (
        f"{sorted(outside ^ set(STAGES_EXCLUDED_FROM_RESULT_JSON))}: a processing-issue "
        "stage is either discovered emitting from inside a service, whose writes "
        "carry its issues into the section's result.json, or declared in "
        "STAGES_EXCLUDED_FROM_RESULT_JSON with the reason it is not. Neither is where this "
        "stage is, and a stage in neither is one this harness reports as clean."
    )


def test_the_only_unreadable_sites_are_the_known_pass_throughs():
    """A third unresolvable site means a service emits codes this discovery cannot
    see, and the coverage below would narrow with nothing going red."""
    assert UNRESOLVED == ISSUE_CODE_PASS_THROUGH_SITES, UNRESOLVED


# ---------------------------------------------------------------------------
# Fixtures built from the authority: real ProcessingIssue objects, serialised the
# way the extraction service serialises them into a section's result.json.
# ---------------------------------------------------------------------------


def _issue(code, severity="warning", stage=None, **kw):
    return ProcessingIssue(
        stage=stage or (code.split("_", 1)[0] if "_" in code else "extraction"),
        severity=severity,
        code=code,
        message=kw.pop("message", f"{code} fired on this section"),
        **kw,
    )


def _section(*issues, section_id="1"):
    """One section result.json as the harness reads it off S3.

    ``metadata.processing_issues`` is where the extraction service writes them
    (`extraction/service.py`) and where assessment merges into; the shape is
    ``ProcessingIssue.to_dict()``, so it is produced here by the real model.
    """
    section = {
        "document_class": {"type": "BankStatement"},
        "inference_result": {"Transactions": [{"Amount": "1.00"}]},
        "metadata": {"assessment_time_seconds": 1.0},
    }
    if issues:
        section["metadata"]["processing_issues"] = [i.to_dict() for i in issues]
    return section


def _row(cell, doc, sections, *, success=True):
    """One summary row, scored through the real recorder."""
    return {
        "cell": cell,
        "doc": doc,
        "sub_doc": None,
        "repeat": 0,
        "resolved": {},
        "run_id": f"run-{cell}",
        "status": "COMPLETED" if success else "FAILED",
        "success": success,
        "cost": 0.1,
        "completeness_recall": 1.0,
        **analyze.score_processing_issues(sections),
    }


def _written(tmp_path, rows):
    """Rows through the real writer, back out of the real reader."""
    out = tmp_path / "suite"
    aggregate.write_summary({}, rows, str(out))
    with open(out / "summary.json") as f:
        summary = json.load(f)
    with open(out / "summary.csv", newline="") as f:
        csv_rows = list(csv.DictReader(f))
    return summary, csv_rows


# ---------------------------------------------------------------------------
# The headline property.
# ---------------------------------------------------------------------------


def test_a_lossy_but_warned_run_is_distinguishable_from_a_clean_one(tmp_path):
    """The property #1361's author could not establish.

    Two runs identical in every figure the harness recorded before this change —
    same status, same cost, same recall — one of which warned that it lost rows.
    Reading ``summary.json`` alone has to tell them apart.
    """
    warned = _row(
        "lossy",
        "longdesc_800.pdf",
        [
            _section(
                _issue(
                    "extraction_rows_below_ocr_estimate",
                    severity="error",
                    message=(
                        "Extraction returned 141 rows where the OCR text suggests "
                        "about 814."
                    ),
                    details={"ratio": 0.173},
                )
            )
        ],
    )
    clean = _row("clean", "longdesc_800.pdf", [_section()])

    summary, csv_rows = _written(tmp_path, [warned, clean])
    by_cell = {r["cell"]: r for r in summary["rows"]}

    # Every pre-existing figure agrees, which is exactly the state the issue describes.
    for key in ("status", "success", "cost", "completeness_recall"):
        assert by_cell["lossy"][key] == by_cell["clean"][key]

    # And the two are now distinguishable on the count...
    assert by_cell["lossy"]["processing_issues"] == 1
    assert by_cell["clean"]["processing_issues"] == 0
    # ...on the code...
    assert by_cell["lossy"]["processing_issues_by_code"] == {
        "extraction_rows_below_ocr_estimate": 1
    }
    assert by_cell["clean"]["processing_issues_by_code"] == {}
    # ...and on the message a reader needs to reach the right conclusion, which is
    # what a count alone would not have given #1361's author.
    (detail,) = by_cell["lossy"]["processing_issue_detail"]
    assert "141 rows" in detail["message"]
    assert detail["severity"] == "error"
    assert by_cell["clean"]["processing_issue_detail"] == []

    # Distinguishable in the CSV too — the artifact most readers open.
    by_cell_csv = {r["cell"]: r for r in csv_rows}
    assert by_cell_csv["lossy"]["processing_issues"] == "1"
    assert by_cell_csv["clean"]["processing_issues"] == "0"
    assert (
        by_cell_csv["lossy"]["processing_issue_codes"]
        == "extraction_rows_below_ocr_estimate=1"
    )
    assert by_cell_csv["clean"]["processing_issue_codes"] == ""

    # And at cell level, which is the altitude a release A/B is read at.
    assert (
        summary["cell_stats"]["lossy"]["processing_issues"]["n_runs_with_issues"] == 1
    )
    assert (
        summary["cell_stats"]["clean"]["processing_issues"]["n_runs_with_issues"] == 0
    )


def test_severity_does_not_collapse_into_one_field(tmp_path):
    """The whole use of the figure is triage, so an error must stay readable as an
    error beside two warnings rather than becoming "three issues"."""
    row = _row(
        "mixed",
        "d.pdf",
        [
            _section(
                _issue("extraction_rows_below_ocr_estimate", severity="error"),
                _issue("extraction_incomplete", severity="warning"),
            ),
            _section(
                _issue("assessment_truncated", severity="warning"), section_id="2"
            ),
        ],
    )
    summary, csv_rows = _written(tmp_path, [row])
    (out,) = summary["rows"]
    assert out["processing_issues"] == 3
    assert out["processing_issues_by_severity"] == {"error": 1, "warning": 2}
    assert csv_rows[0]["processing_issue_severities"] == "error=1 warning=2"
    # And the stage axis survives too, so "which service warned" is readable.
    assert out["processing_issues_by_stage"] == {"extraction": 2, "assessment": 1}
    # EVERY issue, not the first. A detail list truncated to its head keeps every
    # tally above correct and loses the other two messages, and the message is the
    # reason the list exists. Measured: `issues[:1]` survived a suite that asserted
    # only the tallies.
    assert len(out["processing_issue_detail"]) == 3
    assert [d["code"] for d in out["processing_issue_detail"]] == [
        "extraction_rows_below_ocr_estimate",
        "extraction_incomplete",
        "assessment_truncated",
    ]


# ---------------------------------------------------------------------------
# Derived coverage: every issue type the tree can emit.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(CODES))
def test_every_issue_type_the_tree_can_emit_reaches_the_summary(code, tmp_path):
    """Parametrised over codes discovered in ``idp_common``, not over a list here.

    A recorder that enumerated the codes it knew would pass a test written the same
    way and silently stop covering the next one added — which is the defect this
    change closes, one level up.
    """
    summary, csv_rows = _written(
        tmp_path, [_row("c", "d.pdf", [_section(_issue(code))])]
    )
    (out,) = summary["rows"]
    assert out["processing_issues"] == 1
    assert out["processing_issues_by_code"] == {code: 1}
    assert out["processing_issue_detail"][0]["code"] == code
    assert csv_rows[0]["processing_issue_codes"] == f"{code}=1"


@pytest.mark.parametrize("severity", sorted(SEVERITIES))
def test_every_severity_the_tree_can_emit_reaches_the_summary(severity, tmp_path):
    summary, _ = _written(
        tmp_path,
        [_row("c", "d.pdf", [_section(_issue("extraction_incomplete", severity))])],
    )
    assert summary["rows"][0]["processing_issues_by_severity"] == {severity: 1}


# Codes no service emits, chosen so that no recognition rule a recorder might
# plausibly carry would let any of them through. Measured rather than guessed: a
# single invented code sharing the `extraction_` prefix left a recorder filtering on
# `startswith(("extraction_", "assessment_", "classification_"))` — which is the
# defect this change closes, one level up — passing the whole suite.
#
# * one with a real stage prefix, which an exact-code allowlist rejects;
# * one with no prefix any service here uses, which a prefix rule rejects;
# * one whose stage is not a pipeline stage at all.
INVENTED_CODES = [
    "extraction_rows_below_a_metric_invented_in_2027",
    "a_shortfall_no_service_here_reports_yet",
    "telepathy_unanimity_not_reached",
]


@pytest.mark.parametrize("invented_code", INVENTED_CODES)
def test_an_issue_type_nobody_has_written_yet_is_recorded_unchanged(
    invented_code, tmp_path
):
    """The forward-looking half, and the one that says the recorder is not a list.

    A code and a severity that appear nowhere in this repository must come back
    counted, tallied and readable, **beside** a code that does. If this ever needs a
    harness edit to pass, the recorder has become the control it replaced.
    """
    invented_severity = "catastrophe"
    assert invented_code not in CODES
    assert invented_severity not in SEVERITIES

    known = "extraction_incomplete"
    assert known in CODES
    summary, csv_rows = _written(
        tmp_path,
        [
            _row(
                "c",
                "d.pdf",
                [
                    _section(
                        _issue(invented_code, invented_severity, stage="telepathy"),
                        # Beside a real one, so a recorder that keeps only the codes
                        # it recognises cannot pass by recording this and dropping
                        # the other.
                        _issue(known, "warning", stage="extraction"),
                    )
                ],
            )
        ],
    )
    (out,) = summary["rows"]
    assert out["processing_issues"] == 2
    assert out["processing_issues_by_code"] == {invented_code: 1, known: 1}
    assert out["processing_issues_by_severity"] == {invented_severity: 1, "warning": 1}
    assert out["processing_issues_by_stage"] == {"telepathy": 1, "extraction": 1}
    assert invented_code in csv_rows[0]["processing_issue_codes"]
    assert (
        summary["cell_stats"]["c"]["processing_issues"]["by_code"][invented_code] == 1
    )


# ---------------------------------------------------------------------------
# Absence versus failure, and the model as the authority.
# ---------------------------------------------------------------------------


def test_every_section_of_a_long_packet_is_read():
    """A real packet has more sections than any fixture above, and a reader that
    stopped after the first few would keep every test green while losing the
    warnings on the tail. Measured: ``sections[:5]`` survived a suite whose largest
    document had five sections. 40 is well past any plausible head slice and past
    the section count of the largest document in this corpus.
    """
    n = 40
    sections = [
        _section(_issue("extraction_incomplete"), section_id=str(i)) for i in range(n)
    ]
    out = analyze.score_processing_issues(sections)
    assert out["processing_issues"] == n
    assert out["processing_issues_by_code"] == {"extraction_incomplete": n}
    assert len(out["processing_issue_detail"]) == n


def test_a_clean_document_records_zero_and_never_null():
    """``None`` is this harness's encoding for "could not be read"
    (``unread_sections_row``). Using it for "nothing to report" would reinstate
    exactly the ambiguity #1378 is about — a clean run and an unmeasured one
    reading the same."""
    out = analyze.score_processing_issues([_section(), _section(section_id="2")])
    assert out["processing_issues"] == 0
    assert out["processing_issues"] is not None
    assert out["processing_issues_by_severity"] == {}
    assert out["processing_issue_detail"] == []
    assert out["processing_issue_codes"] is None
    # A section with no `processing_issues` key is CLEAN, not malformed. Reporting a
    # malformed count here would put a parse complaint on every healthy run, which
    # is the fastest way to teach a reader to ignore the field.
    assert out["processing_issues_malformed"] is None


def test_a_document_whose_sections_did_not_read_records_no_issue_keys_at_all(
    monkeypatch,
):
    """Not ``0``. A zero would claim the document was read and warned about nothing,
    which is the one reading that must not be available for an unread document —
    and ``unread_sections_row`` exists to keep absent keys out of every average."""
    unread = lib.SectionRead([], unreadable=2, errors=("kms: denied",))
    monkeypatch.setattr(lib, "read_sections", lambda bucket, prefix: unread)
    monkeypatch.setattr(
        lib, "read_json", lambda bucket, key: lib.Reading.absent("not there")
    )
    for row in (
        analyze.score_synthetic("b", "p/", {"seq_ids": ["SEQ00001"]}),
        analyze.score_reference("b", "p/"),
    ):
        assert "processing_issues" not in row
        assert "processing_issue_detail" not in row
        assert row["sections_unreadable"] == 2


def test_a_read_document_with_issues_is_scored_by_the_real_scorers(monkeypatch):
    """End to end through ``score_synthetic`` and ``score_reference`` themselves,
    not only through the helper, with S3 stubbed at ``lib``."""
    sections = [_section(_issue("extraction_incomplete"))]
    monkeypatch.setattr(
        lib, "read_sections", lambda bucket, prefix: lib.SectionRead(sections)
    )
    monkeypatch.setattr(
        lib, "read_json", lambda bucket, key: lib.Reading.absent("no eval")
    )
    for row in (
        analyze.score_synthetic("b", "p/", {"seq_ids": [], "fields": {}}),
        analyze.score_reference("b", "p/"),
    ):
        assert row["processing_issues"] == 1
        assert row["processing_issues_by_code"] == {"extraction_incomplete": 1}


def test_a_section_whose_metadata_is_not_a_dict_does_not_stop_the_document():
    """Every section result.json in this corpus carries a ``metadata`` dict, so this
    shape is reachable only from malformed or very old output — which is precisely
    when a scoring pass must not die part-way through a 130-run grid. The sections
    around it are still read."""
    out = analyze.score_processing_issues(
        [
            {"inference_result": {}},
            {"metadata": None},
            {"metadata": "not a dict"},
            {"metadata": ["also not a dict"]},
            _section(_issue("extraction_incomplete")),
        ]
    )
    assert out["processing_issues"] == 1
    assert out["processing_issues_by_code"] == {"extraction_incomplete": 1}


def test_malformed_issue_entries_are_counted_not_dropped():
    """Output nobody can parse is a finding about the run, not an empty result.
    Dropping it silently is the shape of defect this whole change is about."""
    out = analyze.score_processing_issues(
        [
            {"metadata": {"processing_issues": ["not a dict", 7]}},
            {"metadata": {"processing_issues": "a string, not a list"}},
            {
                "metadata": {
                    "processing_issues": [_issue("extraction_sparse").to_dict()]
                }
            },
        ]
    )
    assert out["processing_issues"] == 1
    assert out["processing_issues_malformed"] == 3
    assert out["processing_issues_by_code"] == {"extraction_sparse": 1}
    # The reason names the shape that was wrong, not just that something was. A count
    # with no reason cannot be acted on, and the reason text is unasserted unless it
    # is read — measured: dropping the word "not" from each reason survived.
    why = out["processing_issues_malformed_why"]
    assert why == [
        "issue entry is str, not a dict",
        "issue entry is int, not a dict",
        "processing_issues is str, not a list",
    ]


def test_an_entry_with_no_code_is_malformed_not_an_issue_with_an_empty_name():
    """``{}`` in the list would otherwise become a real issue tallied under ``""`` on
    all three axes — a warned run with an unnameable cause, which is worse than
    either a clean reading or a malformed one.

    The test is STRUCTURAL: the identifying field is absent. It is not a check
    against any list of known codes, so an invented code still records — asserted
    here beside the empty one so the guard cannot be widened into a recognition rule
    without this failing.
    """
    out = analyze.score_processing_issues(
        [
            {
                "metadata": {
                    "processing_issues": [
                        {},
                        {"severity": "error", "message": "no code"},
                        {"code": "   ", "message": "blank code"},
                        _issue("a_code_no_service_emits_yet").to_dict(),
                    ]
                }
            }
        ]
    )
    assert out["processing_issues"] == 1
    assert out["processing_issues_by_code"] == {"a_code_no_service_emits_yet": 1}
    assert out["processing_issues_by_severity"] == {"warning": 1}
    assert out["processing_issues_malformed"] == 3
    assert out["processing_issues_malformed_why"] == ["issue entry carries no code"] * 3


def test_each_issue_is_normalised_through_the_shipped_model_not_a_lookalike():
    """The recorder round-trips every issue through
    ``ProcessingIssue.from_dict(...).to_dict()``, so the model decides what an issue
    IS — which is what keeps a 281 KB jsonschema ``root_cause`` out of a committed
    artifact (``ProcessingIssue.MAX_ROOT_CAUSE_BYTES``) without the harness carrying
    a size rule of its own, and what means a field added to the model is recorded
    without a harness change.
    """
    raw = {
        "stage": "extraction",
        "severity": "error",
        "code": "extraction_rows_below_ocr_estimate",
        "message": "lost rows",
        # Every OPTIONAL field populated, deliberately. `to_dict` omits an empty
        # one, so a fixture leaving `section_id` out compares equal on both sides
        # whether the recorder keeps the field or drops it — measured: popping
        # `section_id` before recording survived the suite, and it is the field
        # that says WHICH section warned.
        "section_id": "3",
        "root_cause": "x" * (ProcessingIssue.MAX_ROOT_CAUSE_BYTES * 4),
        "details": {"blob": "y" * (ProcessingIssue.MAX_DETAIL_STRING_BYTES * 4)},
    }
    out = analyze.score_processing_issues([{"metadata": {"processing_issues": [raw]}}])
    (recorded,) = out["processing_issue_detail"]
    assert recorded == ProcessingIssue.from_dict(raw).to_dict()
    # Named explicitly as well as compared, so the comparison cannot pass by both
    # sides omitting the same field.
    assert set(recorded) == {
        "stage",
        "severity",
        "code",
        "message",
        "section_id",
        "root_cause",
        "details",
    }
    assert recorded["section_id"] == "3"
    assert len(recorded["root_cause"]) < len(raw["root_cause"])
    assert len(recorded["details"]["blob"]) < len(raw["details"]["blob"])


def test_an_issue_entry_the_model_refuses_is_counted_not_raised():
    """``isinstance(entry, dict)`` is not enough to admit an entry to the model.

    ``ProcessingIssue.__post_init__`` byte-truncates ``root_cause``, so an entry
    carrying a non-string there raises ``AttributeError: 'dict' object has no
    attribute 'encode'`` — and before the guard existed that one entry killed the
    whole scoring pass, which is exactly what
    ``test_a_section_whose_metadata_is_not_a_dict_does_not_stop_the_document``
    says must not happen. It is ``root_cause`` specifically: the same wrong type
    in ``details``, ``message``, ``code`` or ``stage`` reads fine.
    """
    bad = {
        "stage": "extraction",
        "severity": "warning",
        "code": "extraction_incomplete",
        "message": "m",
        "root_cause": {"not": "a string"},
    }
    out = analyze.score_processing_issues(
        [
            {"metadata": {"processing_issues": [bad]}},
            _section(_issue("extraction_sparse")),
        ]
    )
    # The pass survives, the good issue is still recorded, and the bad one is
    # counted AND named rather than swallowed.
    assert out["processing_issues"] == 1
    assert out["processing_issues_by_code"] == {"extraction_sparse": 1}
    assert out["processing_issues_malformed"] == 1
    (why,) = out["processing_issues_malformed_why"]
    assert "extraction_incomplete" in why and "AttributeError" in why
    assert "would not read" in why
    # The code is quoted so it can be grepped for, and elided so a pathological one
    # cannot put the document's content in the reason.
    assert why.startswith("issue entry 'extraction_incomplete' would not read: ")


def test_a_document_whose_every_issue_is_unparseable_does_not_read_as_clean():
    """``0`` issues and ``0`` malformed is the clean reading. ``0`` issues with a
    malformed count is a document that warned about something nobody can read, and
    collapsing the two would be this issue's own defect reproduced inside its own
    fix. Measured: ``malformed if issues else None`` survived a suite whose
    malformed fixtures always carried one valid issue alongside."""
    out = analyze.score_processing_issues(
        [{"metadata": {"processing_issues": ["not a dict", 7]}}]
    )
    assert out["processing_issues"] == 0
    assert out["processing_issues_malformed"] == 2
    assert len(out["processing_issues_malformed_why"]) == 2


# ---------------------------------------------------------------------------
# The wiring: both scorers, the CSV, the cell roll-up, the release comparison.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "column",
    [
        "processing_issues",
        "processing_issues_malformed",
        "processing_issue_severities",
        "processing_issue_codes",
    ],
)
def test_every_flat_key_reaches_the_csv(column):
    """``DictWriter(extrasaction="ignore")`` drops an unlisted row key without a
    word, so a figure can be recorded in summary.json and invisible in the CSV."""
    assert column in aggregate.CSV_COLS


@pytest.mark.parametrize("scorer", ["score_synthetic", "score_reference"])
def test_both_scorers_splice_the_recorder(scorer):
    """Checked by AST rather than by a substring count: a count breaks on a reformat
    and cannot tell which function the call sits in. The reference path is the one an
    earlier metric was added to only the synthetic side of."""
    tree = ast.parse(Path(analyze.__file__).read_text(encoding="utf-8"))
    function = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == scorer
        ),
        None,
    )
    assert function is not None, f"{scorer} is gone from {analyze.__file__}"
    splices = [
        value
        for node in ast.walk(function)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values)
        if key is None
        and isinstance(value, ast.Call)
        and getattr(value.func, "id", None) == "score_processing_issues"
    ]
    assert splices, (
        f"{scorer} does not splice score_processing_issues(...) into a returned "
        "dict, so documents scored by that path record no processing issues — and a "
        "warned run would read as a clean one again."
    )


def test_the_cell_rollup_counts_the_runs_that_failed_too(tmp_path):
    """A run that failed is the one whose warnings triage wants first. Every other
    statistic in ``cell_stats`` is over successful runs only, so restricting this
    one the same way would reproduce the blind spot at cell level."""
    rows = [
        _row("c", "ok.pdf", [_section(_issue("extraction_incomplete"))]),
        _row(
            "c",
            "dead.pdf",
            [_section(_issue("extraction_rows_below_ocr_estimate", severity="error"))],
            success=False,
        ),
    ]
    summary, _ = _written(tmp_path, rows)
    block = summary["cell_stats"]["c"]["processing_issues"]
    assert block["n_runs"] == 2
    assert block["n_runs_with_issues"] == 2
    assert block["n_issues"] == 2
    assert block["by_severity"] == {"warning": 1, "error": 1}
    assert block["by_code"] == {
        "extraction_incomplete": 1,
        "extraction_rows_below_ocr_estimate": 1,
    }
    # All three axes, not two: a mutation renaming this key survived a suite that
    # asserted only the other two.
    assert block["by_stage"] == {"extraction": 2}


def test_a_cell_holding_a_row_that_was_never_scored_still_rolls_up(tmp_path):
    """``score_all`` emits ``{..., "status": "NOT_LAUNCHED"}`` rows carrying no issue
    keys at all, and ``--augment`` produces grids mixing scored and unscored rows.
    The roll-up has to read those as "nothing recorded" rather than raising, and it
    must not report the unscored run as clean: ``n_runs_scored`` is short of
    ``n_runs``, which is what says the counts cover fewer runs than the cell has.
    """
    not_launched = {
        "cell": "c",
        "doc": "never.pdf",
        "sub_doc": None,
        "repeat": 0,
        "resolved": {},
        "run_id": None,
        "status": "NOT_LAUNCHED",
        "success": False,
    }
    rows = [not_launched, _row("c", "ok.pdf", [_section(_issue("extraction_sparse"))])]
    summary, _ = _written(tmp_path, rows)
    block = summary["cell_stats"]["c"]["processing_issues"]
    assert block["n_runs"] == 2
    assert block["n_runs_scored"] == 1
    assert block["n_runs_with_issues"] == 1
    assert block["by_code"] == {"extraction_sparse": 1}


def test_scoring_prints_a_warning_naming_the_codes(tmp_path, capsys):
    """Whoever runs ``aggregate.py`` is the person about to copy these numbers
    somewhere. A completeness warning that only ever reaches a file is how a warned
    run gets described as a silent one."""
    aggregate.write_summary(
        {},
        [
            _row(
                "lossy",
                "d.pdf",
                [_section(_issue("extraction_rows_below_ocr_estimate", "error"))],
            )
        ],
        str(tmp_path / "s"),
    )
    printed = capsys.readouterr().out
    assert "processing issues" in printed
    assert "extraction_rows_below_ocr_estimate=1" in printed
    assert "error=1" in printed
    # The notice has to say the run is not clean and where to read the detail.
    # Asserting only the first and last fragments leaves the sentence between them
    # unread — measured: a mutation blanking that line survived the suite.
    assert "NOT clean" in printed
    assert "processing_issue_detail" in printed
    assert "1 issue(s) on 1 of 1 runs" in printed


def test_the_notice_prints_for_a_warning_with_no_error_anywhere(tmp_path, capsys):
    """The severity that matters most, and the one a notice keyed on ``error``
    would silently skip.

    ``extraction_rows_below_ocr_estimate`` fires at **warning** on the shipped
    default: ``extraction/service.py`` chooses ``"error" if shortfall_action ==
    "fail" else "warning"``, and ``system_defaults/base-extraction.yaml`` ships
    ``row_shortfall_action: warn``. So a grid run on the default configuration —
    which is the configuration #1361 was run on — carries no error-severity issue
    at all, and a notice that needed one would print nothing on exactly the case
    the notice exists for. Measured: keying the notice on
    ``by_severity["error"]`` instead of on the issue count left the suite green
    until this test existed.
    """
    aggregate.write_summary(
        {},
        [
            _row(
                "warned",
                "d.pdf",
                [_section(_issue("extraction_rows_below_ocr_estimate", "warning"))],
            )
        ],
        str(tmp_path / "s"),
    )
    printed = capsys.readouterr().out
    assert "processing issues" in printed
    assert "warning=1" in printed
    assert "error" not in printed
    assert "extraction_rows_below_ocr_estimate=1" in printed


def test_a_clean_grid_prints_no_warning(tmp_path, capsys):
    """Non-vacuity in the other direction: a notice printed on every run is one
    nobody reads."""
    aggregate.write_summary(
        {}, [_row("clean", "d.pdf", [_section()])], str(tmp_path / "s")
    )
    assert "processing issues" not in capsys.readouterr().out


def test_the_metric_is_registered_as_late_added_so_skipped_is_not_unchanged():
    """Every committed baseline predates this figure. Without the registration,
    ``compare_cells`` prints identically whether the comparison was made and found
    nothing or was never possible — the defect class this repository keeps
    rediscovering as a control that exists and is never consulted."""
    assert "processing_issues" in aggregate.LATE_ADDED_METRICS
    label = aggregate.LATE_ADDED_METRICS["processing_issues"]
    # The label is what `compare_cells` prints, so it has to say where to look.
    # Reading it out of the constant and comparing it to itself asserts nothing
    # about its text — measured: a mutation changing the issue number survived.
    assert "#1378" in label and "processing issues" in label
    cur = aggregate.cell_issues([_row("c", "d.pdf", [_section()])])
    assert aggregate._missing_metric_notes({"processing_issues": cur}, {}) == [
        (label, "baseline")
    ]
    assert aggregate._missing_metric_notes({}, {"processing_issues": cur}) == [
        (label, "current")
    ]
    # Absent from both sides says nothing about either grid and is not reported.
    assert aggregate._missing_metric_notes({}, {}) == []


def test_a_grid_that_measured_none_of_the_metric_is_not_reported_as_carrying_it():
    """The direction ``_missing_metric_notes`` was added to catch, and the one a
    present-key test cannot see.

    ``cell_issues`` returns a populated dict for every cell, so plain truthiness
    reads "collected" even when no row in the cell carries the figure — which is the
    state of a grid whose section objects would not read (its own docstring's
    KMS-pending-deletion case, 83 of 96 committed summaries for the sibling metric).
    The presence test is therefore keyed on ``n_runs_scored``. Measured: keying it on
    truthiness survived a suite that tested only key-present against key-absent.
    """
    label = aggregate.LATE_ADDED_METRICS["processing_issues"]
    # A cell whose three runs were all unread: the block exists, and nothing in it
    # was measured.
    unread_rows = [
        {
            "cell": "c",
            "doc": f"d{i}.pdf",
            "sub_doc": None,
            "repeat": 0,
            "resolved": {},
            "run_id": "r",
            "status": "COMPLETED",
            "success": True,
            "sections_unreadable": 2,
            "sections_unread": "kms: denied",
        }
        for i in range(3)
    ]
    unread = aggregate.cell_issues(unread_rows)
    assert unread["n_runs"] == 3 and unread["n_runs_scored"] == 0
    measured = aggregate.cell_issues([_row("c", "d.pdf", [_section()])])
    assert measured["n_runs_scored"] == 1

    # Unread current grid against a baseline that measured it: reported as a gap on
    # the CURRENT side, which is the half that used to print like a clean run.
    assert aggregate._missing_metric_notes(
        {"processing_issues": unread}, {"processing_issues": measured}
    ) == [(label, "current")]
    # ...and the mirror.
    assert aggregate._missing_metric_notes(
        {"processing_issues": measured}, {"processing_issues": unread}
    ) == [(label, "baseline")]
    # Both unread says nothing about either grid.
    assert (
        aggregate._missing_metric_notes(
            {"processing_issues": unread}, {"processing_issues": unread}
        )
        == []
    )


def test_the_cell_block_is_present_even_when_a_cell_recorded_nothing(tmp_path):
    """What makes the registration above work: a scored-and-clean cell must be
    distinguishable from a cell scored before the figure existed, so the block is
    always populated rather than emitted only when non-empty."""
    summary, _ = _written(
        tmp_path,
        [_row("clean", f"d{i}.pdf", [_section()]) for i in range(10)],
    )
    block = summary["cell_stats"]["clean"]["processing_issues"]
    assert block["n_runs"] == 10
    assert block["n_issues"] == 0
    assert bool(block) is True
    # Every one of the ten WAS scored and found clean. A cell reporting
    # `n_runs_scored: 0` against `n_runs: 10` is claiming the opposite — that
    # nothing here was measured — which is the reading `n_runs_scored` exists to
    # distinguish. Measured: testing a clean cell only at n=1 with an
    # issue-bearing row elsewhere let a truthiness test stand in for the type
    # test, because the two agree on every row that carries an issue.
    assert block["n_runs_scored"] == 10


def test_the_recorder_reads_the_same_place_the_audit_block_does():
    """Both read the section's ``result.json`` ``metadata``. Reading extraction's own
    list instead of that key would miss classification's and assessment's issues,
    which extraction preserves and assessment merges into."""
    source = Path(analyze.__file__).read_text(encoding="utf-8")
    assert 'md.get("processing_issues")' in source
    out = analyze.score_processing_issues(
        [
            {
                "metadata": {
                    "processing_issues": [
                        _issue("classification_failed", "error").to_dict(),
                        _issue("assessment_truncated").to_dict(),
                        _issue("extraction_incomplete").to_dict(),
                    ]
                }
            }
        ]
    )
    assert out["processing_issues_by_stage"] == {
        "classification": 1,
        "assessment": 1,
        "extraction": 1,
    }


def test_the_detail_payloads_are_written_one_line_per_issue(tmp_path):
    """Why both keys are in ``COMPACT_PAYLOAD_KEYS``, asserted as an EFFECT.

    The detail list carries issues verbatim and nothing bounds how many a document
    has, so expanded it is the artifact: a busy 130-run grid measures at 24.8 MB and
    409,533 lines, against an 8.8 MB largest-committed summary and the 130k-line
    figure that constant's own comment calls a diff nobody reads. Compact is one line
    per issue — which keeps every issue rather than capping the list, the property
    this whole change exists for.

    ``test_confidence_calibration.py`` pins the membership of the constant; this pins
    what membership does for these two keys, which is the half a membership test
    cannot see.
    """
    row = _row(
        "c",
        "d.pdf",
        [
            _section(
                _issue("extraction_incomplete"),
                _issue("assessment_truncated", "error"),
            )
        ],
    )
    out = tmp_path / "s"
    aggregate.write_summary({}, [row], str(out))
    text = (out / "summary.json").read_text()

    # Round-trips exactly: compaction must not be able to change a value.
    reread = json.loads(text)
    assert (
        reread["rows"][0]["processing_issue_detail"] == row["processing_issue_detail"]
    )
    assert len(reread["rows"][0]["processing_issue_detail"]) == 2

    lines = text.splitlines()
    detail = [ln for ln in lines if '"processing_issue_detail": [' in ln]
    assert detail, "processing_issue_detail is not in the artifact at all"
    # Each issue on ONE line: no line inside the list holds a bare field name.
    assert not [ln for ln in lines if ln.strip().startswith('"root_cause":')], (
        "an issue field is on its own line, so the detail payload is expanded — "
        "every issue in a grid then costs ~8 lines instead of 1"
    )
    assert any('"stage": "extraction"' in ln and '"code":' in ln for ln in lines)
    # And the cell block likewise.
    assert [
        ln
        for ln in lines
        if '"processing_issues": {' in ln and ln.rstrip().endswith("}")
    ]


def test_a_pathological_code_cannot_put_document_content_in_the_reason():
    """The elision bound in the malformed reason, asserted as an effect.

    The reason quotes the offending entry's ``code`` so it can be grepped for, and
    elides it, because ``code`` is model output and the reason rides into a
    committed artifact. Measured: widening the bound survived a suite that only ever
    gave it a short code.
    """
    bad = {
        "code": "x" * 500,
        "severity": "warning",
        "message": "m",
        "root_cause": {"not": "a string"},
    }
    out = analyze.score_processing_issues([{"metadata": {"processing_issues": [bad]}}])
    (why,) = out["processing_issues_malformed_why"]
    assert out["processing_issues_malformed"] == 1
    assert "x" * 60 in why, "the code is not quoted at all"
    assert "x" * 61 not in why, f"the code is not elided: reason is {len(why)} chars"


def test_render_tally_is_stable_and_blank_when_empty():
    assert analyze.render_tally({"b": 2, "a": 1}) == "a=1 b=2"
    assert analyze.render_tally({}) is None


def test_render_tally_does_not_truncate_a_tally_that_is_long():
    """A cell tally merges across every run in the cell, so it can hold every code in
    the tree at once — and the printed notice renders a *cell* tally, not a row's. A
    length cap there would corrupt the one line a reader of the console sees.
    Measured: ``render_tally(...)[:200]`` survived the suite until this existed.
    """
    tally = {code: 7 for code in sorted(CODES)}
    rendered = analyze.render_tally(tally)
    assert len(rendered) > 400, len(rendered)
    for code in CODES:
        assert f"{code}=7" in rendered, code
    # Every entry, and nothing but: the rendering is exactly the tally.
    assert dict(part.rsplit("=", 1) for part in rendered.split(" ")) == {
        code: "7" for code in CODES
    }


def test_the_fixtures_are_reachable_at_all():
    """Guard on this file rather than on the harness: every parametrised test above
    is driven off ``CODES``, and an empty parametrisation collects as a skip with a
    green exit."""
    assert os.path.isdir(IDP_COMMON)
    assert len(list(IDP_COMMON.rglob("*.py"))) > 50
