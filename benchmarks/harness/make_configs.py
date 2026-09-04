#!/usr/bin/env python3
"""Expand config_matrix.yaml cells into full v0.6 IDPConfig variants.

For each requested cell, merges the cell's axis knobs (dotted paths) onto a base
managed config for the target document class, validates, strips `managed`, and
writes benchmarks/corpus/configs/<cell-id>__<class>.yaml.

Usage:
  python3 benchmarks/harness/make_configs.py --suite core [--class bank_statement]
"""

import argparse
import copy
import os
import sys

import yaml

BENCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(BENCH)
sys.path.insert(0, os.path.join(REPO, "lib", "idp_common_pkg"))
from idp_common.config.merge_utils import merge_config_with_defaults  # noqa: E402

CFG_MATRIX = os.path.join(BENCH, "matrices", "config_matrix.yaml")
OUT = os.path.join(BENCH, "corpus", "configs")

# Base managed config per document class (source of classes/attributes/schema).
BASE_CONFIG = {
    "bank_statement": os.path.join(
        REPO, "config_library", "unified", "bank-statement-sample", "config.yaml"
    ),
    # Emitted by the generator itself (gen_corpus.py must run first), so the class
    # schema and the ground truth are derived from one source and cannot drift.
    # It previously pointed at realkie-fcc-verified — an unrelated forms schema
    # that does not declare any of this generator's 25 fields, so anything scored
    # under it produced a meaningless number rather than an obvious error.
    "kv_form": os.path.join(BENCH, "corpus", "docs", "kv_form.pdf.classes.yaml"),
    "realkie": os.path.join(
        REPO, "config_library", "managed_config", "realkie-fcc-verified", "config.yaml"
    ),
    "ocr_bench": os.path.join(
        REPO, "config_library", "managed_config", "ocr-benchmark", "config.yaml"
    ),
}


def override_slug(overrides):
    """A short deterministic suffix identifying a set of ``--set`` overrides.

    Empty when there are none, so every path and version name is byte-identical
    to what this script produced before overrides were namespaced.

    Why this exists: the FILE was namespaced by suite but nothing was namespaced
    by ``--set``, and the uploaded config VERSION name was namespaced by neither.
    So two runs of one suite differing only in ``--set`` overwrote the same file,
    the same index, AND the same ``Config#<version>`` on the stack — the second
    silently relabelling the first. That is the same silent-cross-model-comparison
    failure the suite namespacing was added to fix (see the comment on `path`
    below), reached through a different door: `verify_config_axes` cannot catch it,
    because the file and the index are rewritten together and therefore agree.

    Args:
        overrides: the raw ``AXIS=VALUE`` strings from ``--set``.

    Returns:
        ``""`` or ``"__axis-value[-axis-value...]"``, sorted so it is stable.
    """
    if not overrides:
        return ""
    parts = []
    for ov in sorted(overrides):
        axis, _, value = ov.partition("=")
        parts.append(f"{axis}-{value}".replace("_", "-").replace(".", "-"))
    return "__" + "-".join(parts)


def set_path(cfg, dotted, value):
    """Set a dotted config path, creating dicts as needed. Special-cases the
    knobs whose real shape differs from a plain scalar."""
    # ocr.features expects a list of {name: X}
    if dotted == "ocr.features":
        cfg.setdefault("ocr", {})["features"] = [{"name": f} for f in value]
        return
    # A CLASS-level JSON-Schema extension, applied to every document class in the
    # config. `classes` is a list, which the generic dotted walk below cannot
    # address, and the benchmark configs are single-class by construction — so
    # "set it on every class" is both unambiguous and what the axis means.
    # Written as `classes.<extension>`, e.g.
    # `classes.x-aws-idp-multi-instance: true`.
    if dotted.startswith("classes.") and dotted.count(".") == 1:
        key = dotted.split(".", 1)[1]
        for doc_class in cfg.get("classes") or []:
            if isinstance(doc_class, dict):
                if value is None:
                    doc_class.pop(key, None)
                else:
                    doc_class[key] = value
        return
    parts = dotted.split(".")
    node = cfg
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value


def _norm(v):
    """YAML 1.1 coerces off/on/yes/no to bool; normalize keys/choices to str."""
    if isinstance(v, bool):
        return "on" if v else "off"
    return str(v)


def _resolve_value(value):
    """Expand an ``@file:<path>`` axis value to the file's contents.

    Some knobs are whole PROMPTS, not scalars — the pre-#653 classification
    task_prompt is one, and it is the control arm of the `boundaryctl` suite. A
    frozen prompt pasted into this matrix would be unreadable and would silently
    rot next to the live one, so the value names a file under
    ``benchmarks/matrices/prompts/`` instead. Relative to that directory, so a
    matrix entry cannot reach outside the repo.
    """
    if not (isinstance(value, str) and value.startswith("@file:")):
        return value
    name = value[len("@file:") :].strip()
    if os.path.sep in name or ".." in name:
        raise ValueError(f"@file: value must be a bare filename, got {name!r}")
    path = os.path.join(BENCH, "matrices", "prompts", name)
    if not os.path.exists(path):
        raise SystemExit(f"axis value {value!r} names a missing file: {path}")
    with open(path) as fh:
        return fh.read().strip()


def apply_axis(cfg, axes, axis_name, choice):
    # axis choice map may have bool keys (off/on) due to YAML; index tolerantly
    amap = {_norm(k): kv for k, kv in axes[axis_name].items()}
    knobs = amap[_norm(choice)]
    for dotted, value in knobs.items():
        set_path(cfg, dotted, _resolve_value(value))
    # derive agentic.enabled from extraction_mode
    if axis_name == "extraction_mode":
        cfg.setdefault("extraction", {}).setdefault("agentic", {})["enabled"] = (
            choice == "advanced"
        )


# v0.5.16 sourced its assessment/confidence prompt from a top-level `assessment`
# block in the stored config; v0.6 moved this under `extraction.confidence` and
# sources prompts from system defaults at runtime. To run the SAME config file on
# both a v0.5.16 and a v0.6 stack (apples-to-apples version A/B), we inject a
# self-contained top-level `assessment` block: v0.5.16 reads it, v0.6 ignores it
# (IDPConfig extra="ignore" drops it, keeping extraction.confidence authoritative).
_V0516_ASSESS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "compat", "v0516-base-assessment.yaml"
)


def inject_v0516_assessment(cfg, axes, resolved):
    """Add a top-level `assessment` block honored by v0.5.16 stacks.

    enabled = (assessment axis != off); model = the cell's confidence model so the
    separate-pass assessment on v0.5.16 uses the same model v0.6 uses for
    extraction.confidence. No-op on v0.6 (dropped by extra="ignore")."""
    base = yaml.safe_load(open(_V0516_ASSESS))["assessment"]
    a = copy.deepcopy(base)
    a["enabled"] = resolved.get("assessment", "separate") != "off"
    # confidence model axis -> the same value v0.6 puts in extraction.confidence.model
    cm_axis = resolved.get("confidence_model", "nova_lite")
    cm = axes["confidence_model"][cm_axis].get("extraction.confidence.model")
    if cm:
        a["model"] = cm
    # Mirror v0.6's confidence BATCH SIZE into v0.5.16's granular assessment so the
    # two versions issue comparable numbers of Bedrock calls (fair cost/latency
    # A/B). v0.5.16's base default is list_batch_size=1 (one Bedrock call PER list
    # row -> ~25x more calls than v0.6's default 25, crippling large-list cells).
    conf = cfg.get("extraction", {}).get("confidence", {})
    lbs = conf.get("list_batch_size")
    if lbs:
        a.setdefault("granular", {})["list_batch_size"] = str(lbs)
    cfg["assessment"] = a


def build_cell(base_path, axes, default_cell, cell):
    """cell: dict with id + any axis overrides. Missing axes take default_cell."""
    cfg = yaml.safe_load(open(base_path))
    cfg.pop("description", None)
    cfg.pop("managed", None)
    resolved = {k: _norm(v) for k, v in default_cell.items()}
    resolved.update({k: _norm(v) for k, v in cell.items() if k in axes})
    for axis_name, choice in resolved.items():
        apply_axis(cfg, axes, axis_name, choice)
    # Fully merge with system defaults so ALL step prompts are populated. v0.6
    # sources prompts from system defaults at runtime, but v0.5.16 uses a stored
    # CUSTOM config verbatim (no runtime merge) -> empty extraction/classification
    # prompts would crash Bedrock ("system[0].text length 0"). Merging makes the
    # config self-contained and runnable on BOTH versions from identical bytes.
    merged = merge_config_with_defaults(copy.deepcopy(cfg), validate=True)
    # Re-inject the top-level `assessment` block (merge drops it into
    # extraction.confidence): v0.5.16 reads assessment, v0.6 ignores it.
    inject_v0516_assessment(merged, axes, resolved)
    sanitize_for_v0516(merged)
    # Disable summarization on BOTH versions: it's an unscored late step, and its
    # default model (sonnet-5) is rejected by v0.5.16's bedrock client (sends
    # deprecated `temperature`), which would fail otherwise-successful docs.
    # Turning it off keeps the two versions identical and the pipeline focused on
    # the scored phases (OCR/classification/extraction/assessment).
    merged.setdefault("summarization", {})["enabled"] = False
    return merged, resolved


def sanitize_for_v0516(node):
    """v0.6 stores empty/0 `max_tokens` (and `shard_token_budget`) to mean
    "use model default"; v0.5.16's IDPConfig enforces gt=0 and REJECTS the whole
    config at load if any is 0/''. Recursively fill non-positive values with the
    v0.5.16 field defaults so the shared config passes both validators. These are
    steps' token caps — the fill matches each version's own default, so behavior
    on the exercised steps (OCR/classification/extraction/assessment) is unchanged."""
    # nosec B105 - the 40000 literal is an LLM token-budget default, not a
    # credential. Bandit's hardcoded-password heuristic fires only because the
    # dict key "shard_token_budget" contains the substring "token".
    DEFAULTS = {"max_tokens": 10000, "shard_token_budget": 40000}  # nosec B105
    if isinstance(node, dict):
        for k, v in node.items():
            if k in DEFAULTS:
                try:
                    bad = v in (None, "", 0) or int(v) <= 0
                except (TypeError, ValueError):
                    bad = True
                if bad:
                    node[k] = DEFAULTS[k]
            else:
                sanitize_for_v0516(v)
    elif isinstance(node, list):
        for v in node:
            sanitize_for_v0516(v)


def cells_for_suite(matrix, suite):
    """Return a list of cell dicts for a suite name.

    A suite that names cells EXPLICITLY may also reference `control_cells` —
    deliberately wrong or historical arms (e.g. the pre-#653 prompt) that exist
    only as somebody's control. The `core_cells` / `core_cells+sweeps` expansions
    deliberately do NOT include them, so a known-defective configuration cannot
    end up in the release regression grid just because a study needed it once.
    """
    spec = matrix["suites"][suite]["cells"]
    core = {c["id"]: c for c in matrix["core_cells"]}
    controls = {c["id"]: c for c in matrix.get("control_cells") or []}
    # Multi-instance cells (#715/#753) are their own registry for the same reason
    # control_cells is: they are only meaningful on a document holding several
    # records of one class in ONE section, so a suite saying `cells: "core_cells"`
    # must not pick them up and spend money measuring nothing.
    multi = {c["id"]: c for c in matrix.get("multi_instance_cells") or []}
    out = []
    if spec == "core_cells":
        out = list(core.values())
    elif spec == "multi_instance_cells":
        out = list(multi.values())
    elif spec == "core_cells+sweeps":
        out = list(core.values())
        # add one-axis sweeps as cells (default + varied axis)
        for axis, choices in matrix["sweeps"].items():
            for ch in choices:
                out.append({"id": f"sweep-{axis}-{_norm(ch)}", axis: _norm(ch)})
    elif isinstance(spec, list):
        # Fail on an unknown id rather than dropping it. Every A/B suite in this
        # matrix is a COMPARISON, so silently discarding an arm does not shrink
        # the run — it produces a one-armed "A/B" whose delta is undefined, and
        # nothing downstream can tell that from a suite that was declared with
        # one cell. A typo'd cell id is exactly how a control arm disappears.
        known = {**core, **controls, **multi}
        missing = [i for i in spec if i not in known]
        if missing:
            raise SystemExit(
                f"suite '{suite}' names cell(s) not defined in core_cells, "
                f"control_cells or multi_instance_cells: {missing}. Add them "
                f"there or fix the suite."
            )
        out = [known[i] for i in spec]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", default="core")
    ap.add_argument(
        "--class",
        dest="klass",
        default="bank_statement",
        help="document class whose base config to build onto",
    )
    ap.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="AXIS=VALUE",
        help=(
            "Override a default_cell axis for every cell in this suite "
            "(repeatable), e.g. --set extraction_model=sonnet5. Cells that name "
            "the axis explicitly still win. Use this for a SINGLE-release study "
            "that should reflect the product default; the committed default_cell "
            "holds extraction_model at the cross-version control (sonnet46) so "
            "the release A/B runs on a model every compared release can invoke."
        ),
    )
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    matrix = yaml.safe_load(open(CFG_MATRIX))
    axes = matrix["axes"]
    default_cell = dict(matrix["default_cell"])
    for ov in args.overrides:
        if "=" not in ov:
            ap.error(f"--set expects AXIS=VALUE, got {ov!r}")
        axis, value = ov.split("=", 1)
        if axis not in default_cell:
            ap.error(
                f"--set: unknown axis {axis!r} "
                f"(known: {', '.join(sorted(default_cell))})"
            )
        if axis in axes and value not in axes[axis]:
            ap.error(
                f"--set {axis}: unknown value {value!r} "
                f"(known: {', '.join(sorted(axes[axis]))})"
            )
        default_cell[axis] = value
        print(f"  [override] default_cell.{axis} = {value}")
    base_path = BASE_CONFIG[args.klass]
    slug = override_slug(args.overrides)
    cells = cells_for_suite(matrix, args.suite)
    written = []
    for cell in cells:
        cfg, resolved = build_cell(base_path, axes, default_cell, cell)
        # The slug keeps two --set variants of one suite from colliding, on disk
        # AND in the stack's config table.
        name = f"{cell['id']}__{args.klass}{slug}"
        # The FILE is namespaced by suite; the config VERSION name is not.
        #
        # Suites share cell names (`core_cells` is used by corefast, core,
        # coresynth, …), so an un-namespaced filename made two suites fight over
        # one file: building suite B overwrote suite A's configs while leaving
        # A's index untouched, so A's index advertised one set of axes and the
        # file on disk held another. That silently produced a benchmark
        # comparison across two DIFFERENT extraction models — see the
        # integrity check in run_matrix.py, which now refuses to launch on any
        # such mismatch.
        path = os.path.join(OUT, f"{name}__{args.suite}.yaml")
        yaml.safe_dump(cfg, open(path, "w"), sort_keys=False)
        written.append(
            {
                "cell": cell["id"],
                "class": args.klass,
                "version": name,
                "resolved": resolved,
                "path": path,
            }
        )
        print(f"  {name}: {resolved}")
    idx = os.path.join(OUT, f"_index_{args.suite}_{args.klass}{slug}.yaml")
    yaml.safe_dump(
        {
            "suite": args.suite,
            "class": args.klass,
            "overrides": list(args.overrides),
            "cells": written,
        },
        open(idx, "w"),
        sort_keys=False,
    )
    print(f"{len(written)} configs -> {OUT} (index {os.path.basename(idx)})")


if __name__ == "__main__":
    main()
