#!/usr/bin/env python3
"""Score ONE benchmark run on all seven dimensions vs ground truth.

Synthetic docs -> exact completeness + field/cell accuracy from <id>.truth.json.
Reference docs -> stack evaluation weighted_overall_score + parse-failure rate.

Usage:
  AWS_PROFILE=default python3 analyze.py --bucket <out> --tracking <tbl> \
      --run <runId> --doc <docName> [--truth <truth.json>] [--label L]
Prints a JSON score object.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib  # noqa: E402


def _wall(row):
    st, ct = row.get("WorkflowStartTime"), row.get("CompletionTime")
    if not st or not ct:
        return None
    from datetime import datetime

    def _parse(s):
        return datetime.fromisoformat(str(s).replace("Z", ""))

    try:
        return (_parse(ct) - _parse(st)).total_seconds()
    except Exception:
        return None


def typed_match(expected, got):
    """Does ``got`` match a SCHEMA-TYPED expectation, in both type and value?

    This is deliberately stricter than ``scalar_accuracy``'s string compare, and
    the difference is the whole point of the metric. ``fields`` in a truth file
    records the text the document RENDERS (``"$685.50"``); ``fields_typed``
    records what a correctly-typed extraction must produce (``685.5``). Comparing
    a typed field by ``str()`` scores the *rendered* form as correct, so a
    pipeline that correctly returns the number looks WRONG — which is exactly
    backwards, and is how a value-normalization feature would be measured as a
    regression.

    So: a string in a ``number`` field is a MISS, because that is the failure
    under test. Numbers compare by value (``1234`` == ``1234.0``) since JSON does
    not distinguish them; booleans must be real booleans, not ``"Yes"``/``"true"``.
    """
    if isinstance(expected, bool):
        # Checked before the numeric branch: bool is a subclass of int.
        return isinstance(got, bool) and got == expected
    if isinstance(expected, (int, float)):
        if isinstance(got, bool) or not isinstance(got, (int, float)):
            return False
        return float(got) == float(expected)
    if expected is None:
        return got is None
    return isinstance(got, str) and got.strip() == str(expected).strip()


def _seq_of(row):
    """The SEQnnnnn tag embedded in any string cell of an extracted row."""
    if not isinstance(row, dict):
        return None
    for v in row.values():
        if isinstance(v, str):
            m = lib.SEQ.search(v)
            if m:
                return int(m.group(1))
    return None


def score_cells(sections, rows_typed, list_key):
    """Per-CELL accuracy over list rows, matched to truth by SEQ tag.

    Completeness (``completeness_recall``) answers "did every row come back";
    this answers "did every cell come back with the right typed value". They are
    independent: a run can recover 100% of rows and still return every ``Amount``
    as the string ``"$1,234.00"``. Without this metric, per-row value handling is
    invisible to the whole benchmark — which is why an earlier enforcement A/B
    measured 81 value repairs and a delta of exactly zero.

    Returns ``(hits, total, rows_matched)``. ``total`` counts only cells the
    truth declares AND whose row was recovered, so this metric is about VALUE
    fidelity and does not double-count the truncation that recall already reports.
    """
    if not rows_typed:
        return None, None, None
    by_seq = {}
    for sec in sections:
        ir = sec.get("inference_result") or {}
        if not isinstance(ir, dict):
            continue
        for key, val in ir.items():
            if list_key and key.lower() != str(list_key).lower():
                continue
            if not isinstance(val, list):
                continue
            for row in val:
                seq = _seq_of(row)
                if seq is not None:
                    by_seq.setdefault(seq, row)
    hits = total = 0
    for seq_tag, cells in rows_typed.items():
        seq = int(str(seq_tag)[3:]) if str(seq_tag).startswith("SEQ") else int(seq_tag)
        row = by_seq.get(seq)
        if row is None:
            continue  # not recovered at all -> recall's business, not ours
        for cell, exp in (cells or {}).items():
            total += 1
            got = next((v for k, v in row.items() if k.lower() == cell.lower()), None)
            if typed_match(exp, got):
                hits += 1
    return hits, total, len(by_seq)


def score_synthetic(bucket, doc_prefix, truth):
    """Exact completeness + accuracy from SEQ tags and known field values."""
    seqs, confs = [], []
    scalar_hits = scalar_tot = 0
    typed_hits = typed_tot = 0
    fields = truth.get("fields") or {}
    fields_typed = truth.get("fields_typed") or {}
    got_fields = {}
    sections = list(lib.iter_section_results(bucket, doc_prefix))
    for sec in sections:
        ir = sec.get("inference_result", {}) or {}
        blob = json.dumps(ir)
        seqs += [int(m) for m in lib.SEQ.findall(blob)]
        lib.walk_confidence(sec.get("explainability_info"), confs)
        # capture scalar fields (top-level, case-insensitive)
        if isinstance(ir, dict):
            for k, v in ir.items():
                got_fields.setdefault(k.lower(), v)
    truth_ids = set(int(s[3:]) for s in truth.get("seq_ids", []))
    extracted = set(seqs)
    n_truth = len(truth_ids)
    recall = len(extracted & truth_ids) / n_truth if n_truth else None
    prefix = 0
    while prefix in extracted:
        prefix += 1
    # scalar field accuracy (exact, normalized). Compares against the RENDERED
    # text, so it is unchanged for every existing truth file — the committed
    # baseline stays comparable.
    for label, exp in fields.items():
        scalar_tot += 1
        got = got_fields.get(label.lower())
        if got is not None and str(got).strip() == str(exp).strip():
            scalar_hits += 1
    # Typed accuracy: a SEPARATE metric, not a redefinition of the one above.
    # Only populated for truth files that declare `fields_typed`.
    for label, exp in fields_typed.items():
        typed_tot += 1
        if typed_match(exp, got_fields.get(label.lower())):
            typed_hits += 1
    cell_hits, cell_tot, _rows_matched = score_cells(
        sections, truth.get("rows_typed"), truth.get("list_key")
    )
    cell_accuracy = (
        round(cell_hits / cell_tot, 4) if cell_hits is not None and cell_tot else None
    )
    return {
        "rows_truth": n_truth,
        "rows_extracted": len(extracted),
        "completeness_recall": round(recall, 4) if recall is not None else None,
        "truncation_prefix": prefix if n_truth else None,
        "dups": len(seqs) - len(extracted),
        "n_gaps": len(truth_ids - extracted),
        "scalar_accuracy": round(scalar_hits / scalar_tot, 4) if scalar_tot else None,
        "typed_accuracy": round(typed_hits / typed_tot, 4) if typed_tot else None,
        "typed_fields": typed_tot or None,
        "cell_accuracy": cell_accuracy,
        "cells_compared": cell_tot or None,
        "mean_confidence": round(sum(confs) / len(confs), 4) if confs else None,
        "pct_conf_below_0.9": round(
            100 * sum(1 for c in confs if c < 0.9) / len(confs), 1
        )
        if confs
        else None,
        "n_conf_leaves": len(confs),
    }


def score_reference(bucket, doc_prefix):
    """Weighted accuracy + parse failures + calibration from the stack eval."""
    ev = lib.get_json(bucket, doc_prefix + "evaluation/results.json")
    acc = pf = None
    sep = None
    if ev:
        acc = ev.get("overall_metrics", {}).get("weighted_overall_score")
        pf = 0
        corr_conf, wrong_conf = [], []
        for sec in ev.get("section_results") or ev.get("sections") or []:
            for a in sec.get("attributes") or []:
                if "fail" in str(a.get("failure_type") or "").lower():
                    pf += 1
                c = a.get("confidence")
                if isinstance(c, (int, float)):
                    (corr_conf if a.get("matched") else wrong_conf).append(c)
        if corr_conf and wrong_conf:
            sep = round(
                sum(corr_conf) / len(corr_conf) - sum(wrong_conf) / len(wrong_conf), 4
            )
    confs = []
    for sec in lib.iter_section_results(bucket, doc_prefix):
        lib.walk_confidence(sec.get("explainability_info"), confs)
    return {
        "weighted_accuracy": acc,
        "parse_failures": pf,
        "calibration_separation": sep,
        "mean_confidence": round(sum(confs) / len(confs), 4) if confs else None,
        "pct_conf_below_0.9": round(
            100 * sum(1 for c in confs if c < 0.9) / len(confs), 1
        )
        if confs
        else None,
        "n_conf_leaves": len(confs),
    }


def score_doc(bucket, tracking, run_id, doc_name, truth=None):
    doc_prefix = f"{run_id}/{doc_name}/"
    row = lib.doc_row(tracking, run_id, run_id and doc_name)
    status = row.get("ObjectStatus", "?")
    metering = lib.doc_metering(tracking, run_id, doc_name)
    cost, by = lib.price_metering(metering)
    by_phase = {}
    for k, units in (metering or {}).items():
        phase = k.split("/")[0]
        c, _ = lib.price_metering({k: units})
        by_phase[phase] = round(by_phase.get(phase, 0.0) + c, 5)
    tok = {}
    for k, units in (metering or {}).items():
        if isinstance(units, dict):
            for u in (
                "inputTokens",
                "outputTokens",
                "cacheReadInputTokens",
                "cacheWriteInputTokens",
            ):
                if u in units:
                    tok[u] = tok.get(u, 0) + int(units[u])
    out = {
        "doc": doc_name,
        "status": status,
        "success": status == "COMPLETED",
        "page_count": row.get("PageCount"),
        "wall_s": _wall(row),
        "cost": round(cost, 4),
        "cost_by_phase": by_phase,
        "tokens": tok,
    }
    if truth:
        out.update(score_synthetic(bucket, doc_prefix, truth))
    else:
        out.update(score_reference(bucket, doc_prefix))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--tracking", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--doc", required=True)
    ap.add_argument("--truth", default=None)
    ap.add_argument("--label", default=None)
    a = ap.parse_args()
    truth = json.load(open(a.truth)) if a.truth else None
    res = score_doc(a.bucket, a.tracking, a.run, a.doc, truth)
    if a.label:
        res["label"] = a.label
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
