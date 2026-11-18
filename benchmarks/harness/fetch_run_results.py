"""Fetch the Test Studio result of every run a manifest lists, if not already on disk.

The collector reads `<workdir>/results/<run ID>-result.json`, which the runner writes as
each run finishes. A lost workdir (or a run adopted from the stack) leaves those files
missing; this re-fetches them with `idp-cli test-result`, which re-reads a finished
run's stored result without re-running anything.

    AWS_PROFILE=... python3 benchmarks/harness/fetch_run_results.py \\
        benchmarks/results/guidance/manifests/SC-13.json --checkout ~/wt/develop-1117
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
from pathlib import Path

import run_guidance_ablation as runner


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("manifests", nargs="+", type=Path)
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--workdir", default=str(runner.default_workdir()))
    parser.add_argument("--concurrency", type=int, default=6)
    args = parser.parse_args()
    results = Path(args.workdir) / "results"
    results.mkdir(parents=True, exist_ok=True)

    jobs: list[tuple[argparse.Namespace, str]] = []
    for mpath in args.manifests:
        man = json.loads(mpath.read_text(encoding="utf-8"))
        ns = argparse.Namespace(
            checkout=args.checkout, region=man["region"], stack=man["stack"]
        )
        for values in man["runs"].values():
            for ids in values.values():
                jobs += [
                    (ns, rid)
                    for rid in ids
                    if not (results / f"{rid}-result.json").exists()
                ]
    print(f"{len(jobs)} results to fetch", flush=True)

    def fetch(job: tuple[argparse.Namespace, str]) -> tuple[str, bool, str]:
        ns, rid = job
        out = runner.cli(
            ns,
            "test-result",
            "--stack-name",
            ns.stack,
            "--region",
            ns.region,
            "--test-run-id",
            rid,
            "--output-dir",
            str(results),
        )
        return rid, (results / f"{rid}-result.json").exists(), out[-400:]

    missing = 0
    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for rid, ok, tail in pool.map(fetch, jobs):
            if not ok:
                missing += 1
                print(f"  ! {rid}: no result\n{tail}", flush=True)
    print(f"done; {missing} still missing", flush=True)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
