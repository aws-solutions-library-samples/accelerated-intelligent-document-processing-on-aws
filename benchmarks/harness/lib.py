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


def s3():
    return session().client("s3")


def ddb():
    return session().client("dynamodb")


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


def poll_run(tracking, run_id):
    """Count per-doc statuses for a run via scan+contains. Returns dict."""
    obj = evl = tot = fail = 0
    st = {}
    kw = {
        "TableName": tracking,
        "FilterExpression": "contains(PK, :r)",
        "ExpressionAttributeValues": {":r": {"S": f"doc#{run_id}/"}},
        "ProjectionExpression": "ObjectStatus, EvaluationStatus",
    }
    while True:
        r = ddb().scan(**kw)
        for it in r.get("Items", []):
            tot += 1
            o = it.get("ObjectStatus", {}).get("S", "")
            st[o] = st.get(o, 0) + 1
            if o == "COMPLETED":
                obj += 1
            if o in ("FAILED", "ERROR"):
                fail += 1
            if it.get("EvaluationStatus", {}).get("S", "") == "COMPLETED":
                evl += 1
        if "LastEvaluatedKey" not in r:
            break
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    return {
        "total": tot,
        "obj_done": obj,
        "eval_done": evl,
        "failed": fail,
        "statuses": st,
    }


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


def walk_confidence(node, out):
    if isinstance(node, dict):
        if isinstance(node.get("confidence"), (int, float)):
            out.append(node["confidence"])
        for v in node.values():
            walk_confidence(v, out)
    elif isinstance(node, list):
        for v in node:
            walk_confidence(v, out)


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
