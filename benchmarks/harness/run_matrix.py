#!/usr/bin/env python3
"""Orchestrate a benchmark suite against a deployed stack.

Steps: resolve stack resources -> register synthetic docs as test sets ->
upload config variants -> launch (cell x doc) runs -> poll to completion ->
write results/<run>/runmap.json for scoring by aggregate.py.

Usage:
  AWS_PROFILE=default python3 run_matrix.py --stack IDPBattery0708 --suite core \
     [--class bank_statement] [--estimate] [--max-inflight 6]

Safety: never mutates Config#default; uploads Config#bench-* versions only.
Idempotent test-set registration. --estimate prints projected cost/time and exits.
"""

# ruff: noqa: E402  (local sibling import requires the sys.path bootstrap first)
import argparse
import datetime
import json
import os
import subprocess
import sys
import time

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_configs import _resolve_value as _resolve_axis_value
from make_configs import set_path as _set_path

import lib

BENCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(BENCH)
CFG_MATRIX = os.path.join(BENCH, "matrices", "config_matrix.yaml")
DOC_MATRIX = os.path.join(BENCH, "matrices", "doc_matrix.yaml")
CONFIGS = os.path.join(BENCH, "corpus", "configs")
DOCS = os.path.join(BENCH, "corpus", "docs")
RESULTS = os.path.join(BENCH, "results")


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)  # nosec B602 - operator-owned local harness


def stack_fingerprint(stack):
    """(status, description, last_updated) for ``stack`` — the run's code identity.

    Cheap enough to call before every launch: one DescribeStacks call per ~minute.
    """
    import boto3

    cfn = boto3.client(
        "cloudformation", region_name=os.environ.get("AWS_REGION", "us-west-2")
    )
    st = cfn.describe_stacks(StackName=stack)["Stacks"][0]
    return (
        st["StackStatus"],
        st.get("Description", ""),
        str(st.get("LastUpdatedTime", "")),
    )


def assert_stack_quiesced(stack):
    """Refuse to start a run against a stack that is mid-update.

    A grid launched into an active CloudFormation update spans more than one build:
    Lambdas are replaced under it and the results are not attributable to any single
    version. This happened — the v0.6.7 corefast grid had 22 of 171 runs launched
    during an update someone else started, which cost the whole run its standing as
    a release gate (benchmarks/results/v0.6.7/corefast/FINDINGS.md).
    """
    status, desc, updated = stack_fingerprint(stack)
    if "IN_PROGRESS" in status:
        sys.exit(
            f"stack '{stack}' is {status} — refusing to start.\n"
            f"A run launched into an active update spans multiple builds and cannot "
            f"be attributed to a version. Wait for UPDATE_COMPLETE and re-run."
        )
    print(f"  stack {stack}: {status} | {desc.strip()[-40:]} | updated {updated}")
    return (status, desc, updated)


def assert_stack_unchanged(stack, expected, launched):
    """Abort mid-run if the stack moved underneath us.

    Checked per launch rather than once, because the damage is proportional to how
    long it goes unnoticed: the corefast grid ran for four hours across three builds
    and nothing reported it until the results were scored.
    """
    try:
        current = stack_fingerprint(stack)
    except Exception as exc:  # a transient API error must not kill a paid run
        print(f"  note: could not verify stack fingerprint ({exc}); continuing")
        return
    if current != expected:
        sys.exit(
            f"\nABORTING after {launched} launch(es): stack '{stack}' CHANGED mid-run.\n"
            f"  was: {expected}\n  now: {current}\n"
            f"Runs already launched span two builds, so this grid is no longer a "
            f"controlled comparison. Discard it (the runmap is written) and re-run "
            f"against a quiesced stack."
        )


def resolve_stack(stack):
    """Find testset bucket, output bucket, tracking table, config table."""
    s3c = lib.s3()
    buckets = [b["Name"] for b in s3c.list_buckets()["Buckets"]]
    pfx = stack.lower()

    def find(sub):
        for b in buckets:
            if b.startswith(pfx) and sub in b:
                return b
        return None

    ddbc = lib.ddb()
    tables = ddbc.list_tables()["TableNames"]

    def findt(sub):
        # Prefer the exact "<stack>-<sub>-<suffix>" table; skip look-alikes such as
        # BootstrapTrackingTable / DiscoveryTrackingTable / ChatDocument*Table.
        exact = f"{stack}-{sub}-"
        for t in tables:
            if t.startswith(exact):
                return t
        for t in tables:  # fallback: contains, excluding known decoys
            if (
                t.startswith(stack)
                and sub in t
                and not any(x in t for x in ("Bootstrap", "Discovery", "Chat"))
            ):
                return t
        return None

    return {
        "testset_bucket": find("testsetbucket"),
        "output_bucket": find("outputbucket"),
        "tracking_table": findt("TrackingTable"),
        "config_table": findt("ConfigurationTable"),
    }


def register_testset(stack, res, doc_id, pdf_path):
    """Upload PDF + put a testset# metadata row (idempotent)."""
    tsb = res["testset_bucket"]
    key = f"bench-{doc_id}/input/{os.path.basename(pdf_path)}"
    sh(
        f'AWS_PROFILE=default aws s3 cp "{pdf_path}" s3://{tsb}/{key} --region {lib.REGION}'
    )
    now = datetime.datetime.utcnow().isoformat() + "Z"
    item = {
        "PK": {"S": f"testset#bench-{doc_id}"},
        "SK": {"S": "metadata"},
        "ItemType": {"S": "testset"},
        "id": {"S": f"bench-{doc_id}"},
        "name": {"S": f"bench-{doc_id}"},
        "filePattern": {"S": f"bench-{doc_id}/input/"},
        "fileCount": {"N": "1"},
        "status": {"S": "READY"},
        "createdAt": {"S": now},
        "InitialEventTime": {"S": now},
    }
    lib.ddb().put_item(TableName=res["tracking_table"], Item=item)


def upload_config(stack, version, path, res=None, native=False):
    """Upload a config version. Default path uses idp-cli (which migrates the
    config to v0.6 storage). `native=True` writes the config VERBATIM to the
    ConfigurationTable (compat/native_upload), bypassing the forced v0.5->v0.6
    migration — REQUIRED for v0.5.16 stacks, whose assessment step reads a
    top-level `assessment` block that the migration would drop."""
    if native:
        import yaml as _yaml

        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "compat"))
        import native_upload

        try:
            native_upload.upload(
                res["config_table"],
                version,
                _yaml.safe_load(open(path)),
                region=lib.REGION,
                profile=os.environ.get("AWS_PROFILE", "default"),
            )
            return True
        except Exception as e:
            print(f"    native upload error: {e}")
            return False
    r = sh(
        f"PYTHONPATH={REPO}/lib/idp_common_pkg AWS_PROFILE=default idp-cli config-upload "
        f'--stack-name {stack} --config-file "{path}" --config-profile {version} '
        f'--version-description "benchmark {version}" --region {lib.REGION}'
    )
    return "uploaded successfully" in (r.stdout + r.stderr)


_LAMBDA_FNS = None


def _find_fn(stack, needle):
    """Locate a Lambda by (stack-prefix + substring), scanning ALL functions so
    it finds runners nested under APPSYNCSTACK (v0.5.x) or APIRESOLVERSTACK
    (v0.6). Version-agnostic: idp-cli only discovers main-stack resources and
    fails on v0.5.x where TestRunnerFunction lives in a nested stack."""
    global _LAMBDA_FNS
    if _LAMBDA_FNS is None:
        lam = lib.session().client("lambda", region_name=lib.REGION)
        _LAMBDA_FNS = []
        for page in lam.get_paginator("list_functions").paginate():
            _LAMBDA_FNS += [f["FunctionName"] for f in page["Functions"]]
    for fn in _LAMBDA_FNS:
        if stack in fn and needle in fn:
            return fn
    return None


def launch(stack, testset_id, version, context):
    """Invoke the TestRunner Lambda directly and return its testRunId (which is
    the S3/DDB run-id prefix the scorer keys on)."""
    lam = lib.session().client("lambda", region_name=lib.REGION)
    runner = _find_fn(stack, "TestRunnerFunction")
    resolver = _find_fn(stack, "TestSetResolverFunction")
    if resolver:  # register/refresh test sets (non-fatal)
        try:
            lam.invoke(
                FunctionName=resolver,
                Payload=json.dumps(
                    {"info": {"fieldName": "getTestSets"}, "arguments": {}}
                ),
            )
        except Exception:
            pass
    if not runner:
        return None
    payload = {
        "arguments": {
            "input": {
                "testSetId": testset_id,
                "configVersion": version,
                "numberOfFiles": 1,
                "context": context,
            }
        }
    }
    try:
        resp = lam.invoke(FunctionName=runner, Payload=json.dumps(payload))
        result = json.loads(resp["Payload"].read())
    except Exception:
        return None
    if "errorMessage" in result:
        return None
    return result.get("testRunId")


def load_plan(suite, klass, overrides=()):
    matrix = yaml.safe_load(open(CFG_MATRIX))
    docm = yaml.safe_load(open(DOC_MATRIX))
    suite_spec = matrix["suites"][suite]
    # cells: read the index written by make_configs.py
    # Must match make_configs' namespacing, or a --set run silently reads the
    # index of a DIFFERENT variant and every result is attributed to the wrong
    # configuration.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from make_configs import override_slug

    slug = override_slug(list(overrides))
    idx_path = os.path.join(CONFIGS, f"_index_{suite}_{klass}{slug}.yaml")
    if not os.path.exists(idx_path):
        setflags = "".join(f" --set {o}" for o in overrides)
        sys.exit(
            f"Run make_configs.py --suite {suite} --class {klass}{setflags} first "
            f"({idx_path} missing)"
        )
    cells = yaml.safe_load(open(idx_path))["cells"]
    # docs: may be an explicit list, a named group, or "*"
    if "docs" not in suite_spec:
        sys.exit(
            f"suite '{suite}' declares no `docs:` — a suite with cells but no "
            f"documents cannot run. Add a docs list or group in config_matrix.yaml."
        )
    dg = suite_spec["docs"]
    groups = docm["groups"]
    if isinstance(dg, list):
        doc_ids = dg
    elif dg in groups:
        doc_ids = (
            groups[dg] if groups[dg] != "*" else [d["id"] for d in docm["synthetic"]]
        )
    else:
        doc_ids = [dg]
    named_docs = list(doc_ids)
    doc_ids, skipped = _docs_for_class(doc_ids, docm, klass)
    # A reference doc's "class" is its config, so the class filter above removes it
    # too — but "run them with --class <their class>" is advice that cannot work for
    # one: make_configs has no config for a reference corpus, so load_plan would
    # exit on the missing index. Those are reported by their real cause in main()
    # (#766); this note is only for documents another --class really can run.
    elsewhere = [d for d in skipped if d not in reference_ids(docm)]
    if elsewhere:
        print(
            f"note: {len(elsewhere)} doc(s) in suite '{suite}' belong to another "
            f"document class and are NOT run here: {elsewhere}\n"
            f"      run them with --class <their class> (configs are per class)."
        )
    if not doc_ids:
        sys.exit(
            f"suite '{suite}' has no docs for class '{klass}' — every doc it names "
            f"belongs to a different class. Check --class."
        )
    return cells, doc_ids, int(suite_spec.get("repeats", 1)), named_docs


def reference_ids(docm=None):
    """Ids of the reference corpora — real labeled test SETS living on the stack.

    They have no PDF under ``corpus/docs`` and this harness launches one local PDF
    per run, so it cannot run them at all (#766).
    """
    if docm is None:
        docm = yaml.safe_load(open(DOC_MATRIX))
    return {d["id"] for d in docm.get("reference", [])}


def plan_coverage(named_docs, doc_ids, refs):
    """Split a suite's named documents into measured / unlaunchable / other-class.

    Kept as a pure function so the split is testable without a stack — the first
    attempt at this fix computed the unlaunchable set *after* ``_docs_for_class``
    had already removed reference docs by class, so it was always empty and the
    warning could never fire. Only a behavioral test catches that.

    ``named_docs`` is the suite's list before any filtering, which is what makes
    the reference corpora visible here: they are unlaunchable whether the class
    filter removed them (the usual path) or they survived it.
    """
    unlaunchable = [d for d in named_docs if d in refs]
    runnable = [d for d in doc_ids if d not in unlaunchable]
    other_class = [d for d in named_docs if d not in runnable and d not in unlaunchable]
    return runnable, unlaunchable, other_class


def _docs_for_class(doc_ids, docm, klass):
    """Split ``doc_ids`` into (runnable under ``klass``, belonging elsewhere).

    A suite may legitimately name documents of several classes — the enforcement
    suite runs a transaction-list doc AND a flat form, because the feature it
    measures behaves differently on each. But configs are built per class, so a
    document scored under another class's schema produces a meaningless number
    that looks like a real one. ``run_matrix`` used to run every named doc under
    whatever ``--class`` was passed, despite a comment claiming otherwise; this is
    that comment, implemented.

    A synthetic doc's class is its generator (``gen``); a reference doc names its
    config explicitly. Anything unrecognized is left in rather than dropped —
    silently skipping a document would be its own kind of wrong answer.
    """
    by_id = {d["id"]: d for d in docm.get("synthetic", [])}
    for d in docm.get("reference", []):
        by_id[d["id"]] = d
    keep, other = [], []
    for doc_id in doc_ids:
        spec = by_id.get(doc_id)
        if spec is None:
            keep.append(doc_id)  # unknown: let the existing missing-PDF error fire
            continue
        doc_class = spec.get("gen") or spec.get("config")
        if doc_class is None or doc_class == klass:
            keep.append(doc_id)
        else:
            other.append(doc_id)
    return keep, other


def _dig(d, dotted):
    """Fetch a dotted path out of a nested dict, or None."""
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def verify_config_axes(cells):
    """Abort unless each config FILE holds the axes its index claims.

    The index (`_index_<suite>_<class>.yaml`) records the resolved axis values
    for every cell; the file it points at is what actually gets uploaded and
    run. If the two disagree, every number the run produces is attributed to the
    wrong configuration — and the report looks perfectly normal.

    This is not hypothetical. Suites share cell names, so an un-namespaced config
    filename let `make_configs.py --suite B` overwrite suite A's files while
    leaving A's index untouched. A later `--suite A` run then advertised
    `extraction_model: sonnet5` in its index while uploading a file pinned to
    sonnet-4-6, and the resulting "before vs after" comparison silently spanned
    two different models. Filenames are namespaced per suite now; this check is
    the belt to that braces, and covers any other way the two can drift
    (hand-edited file, partial rebuild, stale index).
    """
    matrix = yaml.safe_load(open(CFG_MATRIX))
    axes = matrix.get("axes", {})
    problems = []
    for c in cells:
        path, resolved = c.get("path"), c.get("resolved") or {}
        if not path or not os.path.exists(path):
            problems.append(f"{c.get('cell')}: config file missing ({path})")
            continue
        cfg = yaml.safe_load(open(path)) or {}
        for axis, value in resolved.items():
            expected = (axes.get(axis) or {}).get(value)
            if not isinstance(expected, dict):
                continue  # axis not expressed as config paths; nothing to check
            for dotted, want in expected.items():
                # Compare against what make_configs.set_path would actually WRITE,
                # not the raw axis value: some knobs are reshaped on the way in
                # (ocr.features becomes [{name: X}, ...]). Reusing the generator's
                # own function means this check can never disagree with it over a
                # shape transform — only over a real value difference.
                # `@file:` axis values (the frozen pre-#653 prompt) name a file
                # whose CONTENTS get written, so the index's literal
                # "@file:foo.txt" will never equal what is on disk. Resolve it the
                # same way make_configs does, or this check reports a mismatch on
                # a config it generated correctly — which is exactly what it did
                # the first time boundaryctl ran.
                probe: dict = {}
                _set_path(probe, dotted, _resolve_axis_value(want))
                want_written = _dig(probe, dotted)
                got = _dig(cfg, dotted)
                if str(got) != str(want_written):
                    problems.append(
                        f"{c.get('cell')}: index says {axis}={value} "
                        f"(so {dotted}={want_written!r}) but "
                        f"{os.path.basename(path)} has {dotted}={got!r}"
                    )
    if problems:
        sys.exit(
            "Config integrity check FAILED — the index and the config files on "
            "disk disagree, so this run would attribute its results to the wrong "
            "configuration:\n  "
            + "\n  ".join(problems)
            + "\n\nRe-run make_configs.py for this suite (with the same --set "
            "overrides) and try again."
        )
    print(f"  config integrity: {len(cells)} cell(s) match their index")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stack", required=True)
    ap.add_argument("--suite", default="core")
    ap.add_argument("--class", dest="klass", default="bank_statement")
    ap.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="AXIS=VALUE",
        help=(
            "The same --set overrides used to build the configs. Required to "
            "locate the right index: make_configs namespaces its output by "
            "overrides, so omitting them here reads a different variant's plan."
        ),
    )
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument(
        "--native-upload",
        action="store_true",
        help="Write configs verbatim to the ConfigurationTable (bypass idp-cli's "
        "v0.5->v0.6 migration). REQUIRED for v0.5.16 stacks.",
    )
    ap.add_argument("--max-inflight", type=int, default=6)
    ap.add_argument("--poll-interval", type=int, default=30)
    ap.add_argument("--timeout-min", type=int, default=60)
    ap.add_argument(
        "--repeats",
        type=int,
        default=None,
        help="Override the suite's repeats (N runs per cell×doc). "
        "Use >=5 for reliable cost-variance comparison of agentic cells.",
    )
    a = ap.parse_args()

    res = resolve_stack(a.stack)
    print("stack resources:", json.dumps(res, indent=2))
    for k, v in res.items():
        if not v:
            sys.exit(f"could not resolve {k} for stack {a.stack}")

    cells, doc_ids, repeats, named_docs = load_plan(a.suite, a.klass, a.overrides)
    if a.repeats is not None:
        repeats = a.repeats

    # This harness launches one local PDF per run, so a reference corpus — a test
    # SET on the stack, with no PDF under corpus/docs — cannot be launched at all.
    # The launch loop used to `continue` past them under the comment "reference
    # docs handled separately"; nothing handles them separately, there is no other
    # launch path (#766). A suite naming `core_docs` therefore measured 7 of its 9
    # documents, dropping the only two corpora with real pages and human-verified
    # labels, and nothing in the run's own output said which 7 it had been.
    #
    # Split on `named_docs` (pre-class-filter) rather than on a missing PDF: the
    # class filter removes reference docs first, because a reference doc's "class"
    # is its config. Checking for the PDF here would always come up empty and the
    # warning would never fire.
    doc_ids, unlaunchable, other_class = plan_coverage(
        named_docs, doc_ids, reference_ids()
    )
    if unlaunchable:
        print(
            f"\n!! {len(unlaunchable)} of the {len(named_docs)} document(s) named by "
            f"suite '{a.suite}' CANNOT be launched by this harness and are NOT "
            f"measured: {unlaunchable}\n"
            "   They are reference corpora — test SETS on the stack, not PDFs under "
            f"{os.path.relpath(DOCS, REPO)} — and run_matrix has no launch path for "
            "them (#766).\n"
            "   Run them through Test Studio (see benchmarks/harness/"
            "detection_ab_teststudio.py) and treat this run as synthetic-only.\n"
        )

    pairs = [(c, d, r) for c in cells for d in doc_ids for r in range(repeats)]
    print(
        f"plan: {len(cells)} cells x {len(doc_ids)} docs x {repeats} = {len(pairs)} runs"
        + (f"  ({len(unlaunchable)} doc(s) not launchable)" if unlaunchable else "")
    )

    if a.estimate:
        print("(estimate) doc ids:", doc_ids)
        if unlaunchable:
            print("(estimate) NOT launchable, not measured:", unlaunchable)
        print("(estimate) cell ids:", [c["cell"] for c in cells])
        print(
            "Run without --estimate to execute. Large docs/advanced cells dominate cost/time."
        )
        return

    # Every remaining doc is a synthetic one, so its PDF must exist. Checked here,
    # once, rather than per (cell, doc, repeat) in the launch loop — where a
    # missing PDF used to be skipped silently. `--estimate` deliberately returns
    # above this: "what would this suite cost" is a fair question to ask before
    # spending 20 minutes generating the corpus.
    missing = [d for d in doc_ids if not os.path.exists(os.path.join(DOCS, d + ".pdf"))]
    if missing:
        sys.exit(
            f"no PDF for {missing} under {os.path.relpath(DOCS, REPO)}.\n"
            f"Run gen_corpus.py (a partial corpus from --only/--series needs the "
            f"docs this suite names), or check the doc id for a typo."
        )

    run_stamp = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(RESULTS, f"run-{run_stamp}")
    os.makedirs(outdir, exist_ok=True)

    # 1. register test sets (unique docs)
    for d in set(doc_ids):
        register_testset(a.stack, res, d, os.path.join(DOCS, d + ".pdf"))
        print(f"  registered bench-{d}")
    # 2. upload configs (unique versions), but only after proving each file on
    #    disk really holds the axes its index advertises.
    verify_config_axes(cells)

    # The stack must not move underneath the grid. Checked here and again
    # before every launch — see assert_stack_unchanged.
    stack_expected = assert_stack_quiesced(a.stack)
    for c in {cc["version"]: cc for cc in cells}.values():
        ok = upload_config(
            a.stack, c["version"], c["path"], res=res, native=a.native_upload
        )
        print(f"  config {c['version']}: {'ok' if ok else 'FAIL'}")

    # 3. launch with an in-flight cap; poll
    runmap = []
    inflight = []

    def poll_done(rid):
        st = lib.poll_run(res["tracking_table"], rid)
        return st["obj_done"] + st["failed"] >= 1  # 1 doc/run

    for c, d, rep in pairs:
        # No missing-PDF check here: it is done once, above, before anything is
        # registered or uploaded. It used to live here as a bare `continue`, which
        # is how a suite silently measured 7 of its 9 documents (#766).
        pdf = os.path.join(DOCS, d + ".pdf")
        # PRUNE finished runs instead of re-polling them. This list used to grow
        # for the whole suite and every slot check polled ALL of it, so the cost of
        # deciding whether to launch was O(runs launched so far) DynamoDB queries —
        # quadratic over a suite. Observed: a 12-run suite averaged 2.5 min/run
        # while a 30-run suite degraded to 8 min/run, and raising --max-inflight
        # barely helped because the CHECK was the bottleneck, not the concurrency.
        # A 171-run grid would have polled ~14,000 times to launch its last run.
        inflight = [x for x in inflight if not poll_done(x)]
        while len(inflight) >= a.max_inflight:
            time.sleep(a.poll_interval)
            inflight = [x for x in inflight if not poll_done(x)]
        assert_stack_unchanged(a.stack, stack_expected, len(runmap))
        ctx = f"bench-{c['cell']}-{d}-r{rep}"
        rid = launch(a.stack, f"bench-{d}", c["version"], ctx)
        rec = {
            "cell": c["cell"],
            "resolved": c["resolved"],
            "doc": d,
            "repeat": rep,
            "run_id": rid,
            "doc_name": os.path.basename(pdf),
            "truth": os.path.join(DOCS, d + ".pdf.truth.json"),
        }
        runmap.append(rec)
        if rid:
            inflight.append(rid)
        print(f"  launched {ctx} -> {rid}")
        json.dump(
            {
                "stack": a.stack,
                "suite": a.suite,
                "class": a.klass,
                "resources": res,
                # What the suite ASKED for vs. what this run can measure. Without
                # these a runmap cannot be told apart from one that covered the
                # whole suite, which is how a 7-of-9 grid got read as complete
                # (#766). Scoring reads `runs`; these are for whoever reads the
                # result later. Absent on a runmap written before this existed, or
                # by another launcher — absent is "unknown", not "nothing skipped".
                # `docs_other_class` is separate because a suite legitimately names
                # documents of several classes and runs them under their own
                # --class; `docs_unlaunchable` is work that CANNOT be run here.
                "docs_named": named_docs,
                "docs_run": doc_ids,
                "docs_unlaunchable": unlaunchable,
                "docs_other_class": other_class,
                "runs": runmap,
            },
            open(os.path.join(outdir, "runmap.json"), "w"),
            indent=2,
        )

    # 4. drain
    print("draining...")
    deadline = time.time() + a.timeout_min * 60
    while time.time() < deadline:
        pending = [
            r["run_id"] for r in runmap if r["run_id"] and not poll_done(r["run_id"])
        ]
        if not pending:
            break
        print(f"  {len(pending)} runs pending...")
        time.sleep(a.poll_interval)
    print(f"done. runmap -> {outdir}/runmap.json")


if __name__ == "__main__":
    main()
