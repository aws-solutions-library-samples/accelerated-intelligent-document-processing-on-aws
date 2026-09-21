#!/usr/bin/env python3
"""Shared benchmark utilities: pricing, DDB metering, S3, ground-truth matching.

Resolver-free — reads S3 + DynamoDB directly so it works on any stack version.
All AWS access uses the 'default' profile (the deployment account).
"""

import json
import os
import re

import boto3
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PRICING_PATH = os.path.join(REPO, "config_library", "pricing.yaml")
REGION = os.environ.get("IDP_REGION", "us-west-2")

_session = None


def session():
    global _session
    if _session is None:
        _session = boto3.Session(
            profile_name=os.environ.get("AWS_PROFILE", "default"), region_name=REGION
        )
    return _session


_clients = {}


def client(name):
    """One cached client per service.

    ``Session.client()`` parses the service model on every call, and the S3 client
    was being rebuilt once per ``get_json`` — tens of thousands of times on a
    release-wide calibration pass over stored runs. Everything here is sequential;
    the cache is a latency fix and nothing more.
    """
    if name not in _clients:
        _clients[name] = session().client(name)
    return _clients[name]


def s3():
    return client("s3")


def ddb():
    return client("dynamodb")


# ----------------------------------------------------------------------------- pricing
def load_pricing():
    raw = yaml.safe_load(open(PRICING_PATH))
    table = {}
    for entry in raw["pricing"]:
        units = {}
        for u in entry.get("units") or []:
            try:
                units[u["name"]] = float(u["price"])
            except (TypeError, ValueError):
                pass
        table[entry["name"]] = units
    return table


PRICING = load_pricing()


def price_metering(metering):
    """metering: {'Phase/service/api': {unit: count}}. Price by LONGEST pricing-key
    suffix of the metering key. Returns (total, {matched_key: cost}).

    Both the model key and the unit name are matched EXACTLY — never by substring.
    This is the reference form of the rule; production
    (idp_common/reporting/save_reporting_data.py::_get_unit_cost) implements the
    same one, and did NOT until GitHub issue #926: its substring fallback bound
    'cacheReadInputTokens' to a row's 'inputTokens' price and overcharged cache
    reads by up to 10x. Keep the two in step — a benchmark cost that disagrees
    with the reported cost for the same metering map is a bug in one of them.
    """
    total = 0.0
    by = {}
    for meter_key, units in (metering or {}).items():
        if not isinstance(units, dict):
            continue
        parts = meter_key.split("/")
        pu = matched = None
        for start in range(len(parts)):
            cand = "/".join(parts[start:])
            if cand in PRICING:
                pu, matched = PRICING[cand], cand
                break
        if not pu:
            continue
        for unit, count in units.items():
            if unit in pu and isinstance(count, (int, float)):
                c = count * pu[unit]
                total += c
                by[matched] = by.get(matched, 0.0) + c
    return total, by


# ----------------------------------------------------------------------------- DDB
def ddb_to_py(v):
    if "M" in v:
        return {k: ddb_to_py(x) for k, x in v["M"].items()}
    if "N" in v:
        return float(v["N"])
    if "S" in v:
        return v["S"]
    if "L" in v:
        return [ddb_to_py(x) for x in v["L"]]
    if "BOOL" in v:
        return v["BOOL"]
    return None


def doc_metering(tracking, run_id, doc_name):
    """Metering map from the doc# tracking row. Handles Map or JSON-string."""
    pk = f"doc#{run_id}/{doc_name}"
    try:
        r = ddb().get_item(
            TableName=tracking,
            Key={"PK": {"S": pk}, "SK": {"S": "none"}},
            ProjectionExpression="Metering",
        )
        item = r.get("Item")
        if not item or "Metering" not in item:
            return {}
        m = ddb_to_py(item["Metering"])
        if isinstance(m, str):
            m = json.loads(m)
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def doc_row(
    tracking,
    run_id,
    doc_name,
    attrs="ObjectStatus,EvaluationStatus,WorkflowStartTime,CompletionTime,PageCount,WorkflowStatus",
):
    pk = f"doc#{run_id}/{doc_name}"
    r = ddb().get_item(
        TableName=tracking,
        Key={"PK": {"S": pk}, "SK": {"S": "none"}},
        ProjectionExpression=attrs,
    )
    it = r.get("Item", {})
    return {k: ddb_to_py(v) for k, v in it.items()}


def _empty_poll():
    return {"total": 0, "obj_done": 0, "eval_done": 0, "failed": 0, "statuses": {}}


def poll_runs(tracking, run_ids):
    """Per-doc status counts for MANY runs in ONE table pass. {run_id: dict}.

    A document's key is ``doc#<run_id>/<doc_name>``, and ``PK`` is the partition
    key — so ``begins_with`` is unavailable (DynamoDB allows it on the sort key
    only) and resolving a run means scanning. That is survivable once per poll
    iteration and is not survivable once per RUN per iteration, which is what
    calling ``poll_run`` in a loop did: a 171-run suite against a stack whose
    tracking table holds months of documents issued 171 full scans per cycle and
    spent longer inside one drain iteration than the runs themselves took (#1016).
    The same call on the launch path is why throughput sat at 12-20 concurrent
    executions against a stack cap of 100 — the harness was scanning, not
    launching. Cost per iteration now scales with the table, not with the table
    times the run count.

    Runs are matched by prefix in memory rather than by a ``contains()`` filter:
    a filter is applied server-side AFTER the read, so it saves transfer but not
    the scan, and one filter cannot select several run ids at once.
    """
    wanted = [r for r in run_ids if r]
    out = {r: _empty_poll() for r in wanted}
    if not wanted:
        return out
    prefixes = [(f"doc#{r}/", r) for r in wanted]

    kw = {
        "TableName": tracking,
        "ProjectionExpression": "PK, ObjectStatus, EvaluationStatus",
    }
    while True:
        r = ddb().scan(**kw)
        for it in r.get("Items", []):
            pk = it.get("PK", {}).get("S", "")
            if not pk.startswith("doc#"):
                continue
            for prefix, rid in prefixes:
                if pk.startswith(prefix):
                    acc = out[rid]
                    acc["total"] += 1
                    o = it.get("ObjectStatus", {}).get("S", "")
                    acc["statuses"][o] = acc["statuses"].get(o, 0) + 1
                    if o == "COMPLETED":
                        acc["obj_done"] += 1
                    if o in ("FAILED", "ERROR"):
                        acc["failed"] += 1
                    if it.get("EvaluationStatus", {}).get("S", "") == "COMPLETED":
                        acc["eval_done"] += 1
                    break
        if "LastEvaluatedKey" not in r:
            break
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    return out


def poll_run(tracking, run_id):
    """Per-doc status counts for one run. Prefer :func:`poll_runs` in a loop over
    several runs — see the cost note there."""
    return poll_runs(tracking, [run_id])[run_id]


# ----------------------------------------------------------------------------- S3
def list_doc_prefixes(bucket, run_id):
    docs = []
    for p in (
        s3()
        .get_paginator("list_objects_v2")
        .paginate(Bucket=bucket, Prefix=f"{run_id}/", Delimiter="/")
    ):
        for cp in p.get("CommonPrefixes", []):
            docs.append(cp["Prefix"])
    return docs


def get_json(bucket, key):
    try:
        return json.loads(s3().get_object(Bucket=bucket, Key=key)["Body"].read())
    except Exception:
        return None


def iter_section_results(bucket, doc_prefix):
    for pg in (
        s3()
        .get_paginator("list_objects_v2")
        .paginate(Bucket=bucket, Prefix=doc_prefix + "sections/")
    ):
        for o in pg.get("Contents", []):
            if o["Key"].endswith("result.json"):
                sec = get_json(bucket, o["Key"])
                if sec:
                    yield sec


# ----------------------------------------------------------------------------- GT matching
SEQ = re.compile(r"SEQ(\d{5})")


def walk_confidence(explainability_info):
    """``{field path: confidence}`` for one section's ``explainability_info``.

    This used to append the bare scalar to a list and throw the path away, which
    put a ceiling on everything the harness could say about confidence: a score
    with no path cannot be joined to the cell it describes, so the only available
    statistics were distributional (``mean_confidence``, ``pct_conf_below_0.9``)
    and calibration against ground truth was unmeasurable on the one corpus that
    has exact per-cell truth (GitHub #935).

    The traversal is NOT implemented here. ``flatten_confidences`` is the rule the
    product itself keys stored confidence curves by, and its sibling
    ``flatten_values`` keys an ``inference_result`` identically — so a path from
    one indexes straight into the other, which is the entire join. A private copy
    of the walk in the harness would drift from the one the shipped curve uses,
    and then a harness calibration number and a stored curve would disagree for
    reasons that have nothing to do with the data.

    Requires ``idp_common`` on ``PYTHONPATH`` (pinned in
    benchmarks/matrices/METHODOLOGY.md). ``confidence_curve`` and ``curve_store``
    are deliberately standard-library-only, so this import does not pull Stickler.
    """
    from idp_common.evaluation import flatten_confidences

    return flatten_confidences(explainability_info)


def confidence_values(explainability_info):
    """Just the confidence scores, for the distribution-only statistics.

    Paths are unique by construction, so this is the same multiset the old
    list-appending walk produced and ``mean_confidence`` / ``pct_conf_below_0.9``
    / ``n_conf_leaves`` are unchanged — verified against stored v0.6.9 sections
    across scalar, table, multi-section and multi-instance shapes. The committed
    baselines stay comparable.
    """
    return list(walk_confidence(explainability_info).values())


def find_list(node, key_lc=("transactions",)):
    """Return the first list value whose key matches (case-insensitive)."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k.lower() in key_lc and isinstance(v, list):
                return v
            r = find_list(v, key_lc)
            if r is not None:
                return r
    elif isinstance(node, list):
        for i in node:
            r = find_list(i, key_lc)
            if r is not None:
                return r
    return None
