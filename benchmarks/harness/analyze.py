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


def score_synthetic(bucket, doc_prefix, truth):
    """Exact completeness + accuracy from SEQ tags and known field values."""
    seqs, confs = [], []
    scalar_hits = scalar_tot = 0
    fields = truth.get("fields") or {}
    got_fields = {}
    for sec in lib.iter_section_results(bucket, doc_prefix):
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
    # scalar field accuracy (exact, normalized)
    for label, exp in fields.items():
        scalar_tot += 1
        got = got_fields.get(label.lower())
        if got is not None and str(got).strip() == str(exp).strip():
            scalar_hits += 1
    return {
        "rows_truth": n_truth,
        "rows_extracted": len(extracted),
        "completeness_recall": round(recall, 4) if recall is not None else None,
        "truncation_prefix": prefix if n_truth else None,
        "dups": len(seqs) - len(extracted),
        "n_gaps": len(truth_ids - extracted),
        "scalar_accuracy": round(scalar_hits / scalar_tot, 4) if scalar_tot else None,
        "mean_confidence": round(sum(confs) / len(confs), 4) if confs else None,
        "pct_conf_below_0.9": round(
            100 * sum(1 for c in confs if c < 0.9) / len(confs), 1
        )
        if confs
        else None,
        "n_conf_leaves": len(confs),
    }


def score_classification(ev):
    """CLASSIFICATION accuracy + confidence calibration from the stack eval.

    The evaluation report's `doc_split_metrics.page_details` carries, per page,
    the ground-truth class, the predicted class, whether it was `correct`, and
    (since GitHub #673) the classifier's own `predicted_confidence`. That last
    pairing is the only thing that says whether a reported classification
    confidence means anything:

      class_calibration_separation = mean(conf | correct) - mean(conf | wrong)

    Near 0 (or negative) means the model is equally confident when it is right
    and when it is wrong — the score carries no information and must not drive
    escalation, no matter how plausible the individual numbers look. This mirrors
    `calibration_separation`, which does the same for extracted FIELDS.

    Returns Nones when classification was not scored (the default) or when the
    run has no ground-truth classes, so an unscored run reports honestly instead
    of scoring 0.
    """
    out = {
        "class_accuracy": None,
        "class_calibration_separation": None,
        "class_mean_confidence": None,
        "n_class_scored_pages": 0,
    }
    if not ev:
        return out
    ds = ev.get("doc_split_metrics") or {}
    acc = ds.get("page_level_accuracy")
    if isinstance(acc, (int, float)):
        out["class_accuracy"] = round(acc, 4)
    right, wrong = [], []
    for row in ds.get("page_details") or []:
        c = row.get("predicted_confidence")
        if isinstance(c, (int, float)):
            (right if row.get("correct") else wrong).append(c)
    scored = right + wrong
    out["n_class_scored_pages"] = len(scored)
    if scored:
        out["class_mean_confidence"] = round(sum(scored) / len(scored), 4)
    # Needs both populations: separation is undefined when every page was right
    # (a perfect run says nothing about calibration) or every page was wrong.
    if right and wrong:
        out["class_calibration_separation"] = round(
            sum(right) / len(right) - sum(wrong) / len(wrong), 4
        )
    return out


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
        **score_classification(ev),
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
