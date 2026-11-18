"""Wait on Test Studio runs a runner started but lost track of, and add them to a manifest.

A runner that dies or loses its network leaves its runs going on the stack. This waits
on each one with the runner's own `wait_run` (which fetches the result and applies the
dataset's failure limit) and appends the clean ones to the ablation's manifest. Do not
run it while a runner for the same ablation is alive: both rewrite the manifest.

    AWS_PROFILE=... python3 benchmarks/harness/adopt_runs.py SC-14 \\
        --checkout ~/wt/deploy-2034 \\
        --run "docsplit_long|Holistic · Claude Haiku 5.5|DocSplit-Long-20261118-210825"
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys

import run_guidance_ablation as runner
import yaml


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ablation")
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--workdir", default=str(runner.default_workdir()))
    parser.add_argument(
        "--run", action="append", required=True, help="<dataset>|<value>|<run ID>"
    )
    args = parser.parse_args()
    reg = yaml.safe_load(runner.REGISTRY.read_text(encoding="utf-8"))
    mpath = runner.MANIFESTS / f"{args.ablation}.json"
    man = json.loads(mpath.read_text(encoding="utf-8"))
    ns = argparse.Namespace(
        checkout=args.checkout,
        region=man["region"],
        stack=man["stack"],
        workdir=args.workdir,
    )
    jobs = [tuple(r.split("|", 2)) for r in args.run]

    def wait(job: tuple[str, ...]) -> tuple[tuple[str, ...], str | None]:
        ds_key, value, run_id = job
        ds = {
            **reg["datasets"][ds_key],
            **{
                "max_failed": man.get("max_failed", {}).get(
                    ds_key, reg["datasets"][ds_key].get("max_failed", 0)
                )
            },
        }
        return job, runner.wait_run(ns, ds, run_id, f"adopt {ds_key} {value}")

    added = 0
    with cf.ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
        for (ds_key, value, run_id), ok in pool.map(wait, jobs):
            if not ok:
                continue
            man = json.loads(mpath.read_text(encoding="utf-8"))
            ids = man["runs"].setdefault(ds_key, {}).setdefault(value, [])
            if run_id not in ids:
                ids.append(run_id)
                man["cache_isolated"] = sorted(
                    set(man.get("cache_isolated") or []) | {run_id}
                )
                mpath.write_text(json.dumps(man, indent=2) + "\n", encoding="utf-8")
                added += 1
    print(f"adopted {added} of {len(jobs)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
