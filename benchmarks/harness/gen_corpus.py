#!/usr/bin/env python3
"""Build the synthetic benchmark corpus from doc_matrix.yaml.

Writes PDFs + <id>.truth.json into benchmarks/corpus/docs/ and a manifest.
Deterministic: same matrix -> same bytes.

The manifest it writes is tracked, so every path recorded in it is **relative to the
repository root** and is resolved at read time against the manifest's own location.
This used to record whatever absolute path the build produced on the machine that ran
it, which left the committed file naming a sibling checkout (#1393); see
``corpus_manifest.py`` for why the working case was the dangerous one.

Usage: python3 benchmarks/harness/gen_corpus.py [--only <id,id>] [--series scaling]
"""

import argparse
import importlib.util
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import corpus_manifest  # noqa: E402 - local sibling import needs the bootstrap above

BENCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GENDIR = os.path.join(BENCH, "corpus", "generators")
DOCS = os.path.join(BENCH, "corpus", "docs")
MATRIX = os.path.join(BENCH, "matrices", "doc_matrix.yaml")


def load_gen(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(GENDIR, name + ".py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="comma-separated doc ids")
    ap.add_argument("--series", default=None, help="only docs with this series tag")
    args = ap.parse_args()
    os.makedirs(DOCS, exist_ok=True)
    matrix = yaml.safe_load(open(MATRIX))
    only = set(args.only.split(",")) if args.only else None
    gens = {}
    manifest = []
    for spec in matrix.get("synthetic", []):
        if only and spec["id"] not in only:
            continue
        if args.series and spec.get("series") != args.series:
            continue
        gen_name = spec["gen"]
        if gen_name not in gens:
            gens[gen_name] = load_gen(gen_name)
        out = os.path.join(DOCS, spec["id"] + ".pdf")
        params = {
            k: v for k, v in spec.items() if k not in ("id", "gen", "tag", "series")
        }
        info = gens[gen_name].build(out=out, **params)
        rec = {
            "id": spec["id"],
            "gen": gen_name,
            "pdf": out,
            "truth": out + ".truth.json",
            **info,
            "tag": spec.get("tag"),
            "series": spec.get("series"),
        }
        # In place, so the reviewed key order of the committed manifest survives a
        # rebuild. ``out`` arrives absolute from the generator via ``**info``, which is
        # why this relativizes the whole record rather than the two keys set above.
        corpus_manifest.relativize_record(rec)
        manifest.append(rec)
        print(
            f"  {spec['id']:16s} gen={gen_name} pages={info.get('pages')} rows={info.get('rows', '-')}"
        )
    man_path = corpus_manifest.MANIFEST_PATH
    # merge with existing manifest (don't clobber other-series entries). Read through
    # the validator: the records carried over are re-dumped verbatim, so a stale
    # absolute path in one of them would survive a partial rebuild (`--only`,
    # `--series`) and be committed again. allow_empty, because this writer is the thing
    # that populates a manifest that has none.
    existing = {}
    if os.path.exists(man_path):
        for r in corpus_manifest.load_raw(man_path, allow_empty=True):
            existing[r["id"]] = r
    for r in manifest:
        existing[r["id"]] = r
    yaml.safe_dump(
        {"docs": list(existing.values())}, open(man_path, "w"), sort_keys=False
    )
    print(f"corpus manifest -> {man_path} ({len(existing)} docs)")


if __name__ == "__main__":
    main()
