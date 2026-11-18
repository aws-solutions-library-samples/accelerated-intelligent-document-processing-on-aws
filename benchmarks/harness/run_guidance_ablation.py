#!/usr/bin/env python3
"""Run one ablation of the benchmark guidance registry as Test Studio runs.

For each dataset of the ablation that has a test set on the stack, and each value of
the setting, this script:

1. downloads the dataset's preset profile from the stack (the profile named after the
   preset, e.g. `realkie-fcc-verified`),
2. applies the ablation's `baseline` overrides, if it names one, then the value's
   overrides from the registry's `set` list (dotted config paths), then the value's
   `transform`, a function in profile_transforms.py, if it has one,
3. uploads the result as an unmanaged profile `bench-<id>-<dataset>-<n>-r<run>`, one
   per repeat run, each carrying a run marker in its system prompts,
4. starts `runs` Test Studio runs of the dataset's test set under those profiles,
   limited to the dataset's `docs`, and waits for their evaluation.

A repeat run gets its own profile because runs of one value process the same documents
within the prompt cache's lifetime, so the second and third runs would read the cache
entries the first run wrote and look cheaper than a document processed once in
production. The marker is one line appended to the system prompts the configuration
already has, which moves the cached prefix: calls inside a run still share a cache,
runs no longer share one with each other. The manifest lists those runs under
`cache_isolated`, which is how collect_guidance_results.py knows it can average their
costs instead of pricing the coldest run alone.

It then writes a run manifest under benchmarks/results/guidance/manifests/ and calls
collect_guidance_results.py, which writes benchmarks/results/guidance/<ID>.json.
Re-running an ablation keeps the runs already in its manifest and adds only what is
missing, so an interrupted run can be resumed.

The CLI runs from --checkout, which must be the source of the code the stack runs, so
that the profiles round-trip every setting the stack knows about.

Usage:
    AWS_PROFILE=... python3 benchmarks/harness/run_guidance_ablation.py EX-03 \\
        --stack sr-testing --region us-east-1 --checkout ~/wt/fix-1785 \\
        --release 0.6.15.dev1 [--datasets realkie]
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
REGISTRY = REPO / "benchmarks" / "matrices" / "guidance_ablations.yaml"
MANIFESTS = REPO / "benchmarks" / "results" / "guidance" / "manifests"
COLLECT = REPO / "benchmarks" / "harness" / "collect_guidance_results.py"
LOCK = threading.Lock()


def default_workdir() -> Path:
    """Where run results and caches live: outside /tmp, which a reboot empties."""
    return Path(
        os.environ.get("GUIDANCE_WORKDIR", "~/bench-work/guidance-runs")
    ).expanduser()


def cli(args: argparse.Namespace, *cmd: str, timeout: int = 7200) -> str:
    root = Path(args.checkout).expanduser()
    env = dict(os.environ)
    # Every idp-cli call targets the stack's region. Some subcommands (test-result)
    # take it from AWS_REGION alone, so an ambient AWS_REGION would aim them at a
    # region where the stack does not exist.
    env["AWS_REGION"] = args.region
    env["AWS_DEFAULT_REGION"] = args.region
    env["PYTHONPATH"] = os.pathsep.join(
        str(p)
        for p in sorted((root / "lib").iterdir())
        if (p / "pyproject.toml").exists()
    )
    proc = subprocess.run(
        [sys.executable, "-m", "idp_cli.cli", *cmd],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )
    return proc.stdout + proc.stderr


_ENV_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def expand_env(value: Any) -> Any:
    """Replace `${NAME}` in a registry value with the environment variable NAME.

    Account-specific values (a decision model's Lambda ARN) stay out of the public
    registry this way. A reference to an unset variable stops the run.
    """
    if isinstance(value, str):

        def sub(m: re.Match[str]) -> str:
            if m.group(1) not in os.environ:
                raise SystemExit(
                    f"the registry needs ${{{m.group(1)}}}; set it in the environment"
                )
            return os.environ[m.group(1)]

        return _ENV_REF.sub(sub, value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


def set_path(cfg: dict[str, Any], dotted: str, value: Any) -> None:
    node = cfg
    keys = dotted.split(".")
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = expand_env(value)


def nodes(node: Any) -> Iterator[dict[str, Any]]:
    """Every mapping inside a configuration subtree, including the subtree itself."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from nodes(value)


def mark_prompts(cfg: dict[str, Any], run: int) -> int:
    """Append a run marker to the system prompts, and return how many it changed.

    A prompt cache entry is keyed on the prefix of the request, which starts at the
    system prompt, so one line there is enough to stop two runs sharing a cache. Only
    prompts the configuration already carries are touched: a prompt this script
    created would be a setting the benchmark did not intend to change.
    """
    marker = f"\n<!-- benchmark run {run} -->"
    targets: list[dict[str, Any]] = []
    extraction = cfg.get("extraction")
    if isinstance(extraction, dict):
        targets.append(extraction)
        if isinstance(extraction.get("confidence"), dict):
            targets.append(extraction["confidence"])
    targets += list(nodes(cfg.get("classification")))
    changed = 0
    for node in targets:
        text = node.get("system_prompt")
        if isinstance(text, str) and text.strip():
            node["system_prompt"] = text + marker
            changed += 1
    return changed


def make_profile(
    args: argparse.Namespace,
    ab: dict[str, Any],
    ds_key: str,
    ds: dict[str, Any],
    idx: int,
    run: int,
) -> tuple[str, int]:
    """Create (or update) one run's profile; return its name and revision."""
    work = Path(args.workdir) / ab["id"]
    work.mkdir(parents=True, exist_ok=True)
    base = work / f"base-{ds_key}.yaml"
    if not base.exists():
        out = cli(
            args,
            "config-download",
            "--stack-name",
            args.stack,
            "--config-profile",
            ds["preset"],
            "--output",
            str(base),
        )
        if not base.exists():
            raise SystemExit(f"could not download profile {ds['preset']}:\n{out}")
    cfg = yaml.safe_load(base.read_text(encoding="utf-8"))
    cfg["managed"] = False
    # The baseline first, so that a value's own overrides win over it.
    for dotted, value in (ab.get("_baseline_set") or {}).items():
        set_path(cfg, dotted, value)
    for dotted, value in ab["set"][idx].items():
        set_path(cfg, dotted, value)
    transform = (ab.get("transform") or [None] * len(ab["values"]))[idx]
    if transform:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import profile_transforms  # noqa: PLC0415

        getattr(profile_transforms, transform)(cfg)
    if not mark_prompts(cfg, run):
        raise SystemExit(
            f"{ds['preset']}: no system prompt to mark, so runs would share a prompt "
            "cache and repeat runs would look cheaper than they are"
        )
    name = f"bench-{ab['id'].lower()}-{ds_key.replace('_', '-')}-{idx}-r{run}"
    path = work / f"{name}.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    out = cli(
        args,
        "config-upload",
        "--stack-name",
        args.stack,
        "--config-file",
        str(path),
        "--config-profile",
        name,
        "--version-description",
        f"Benchmark {ab['id']}: {ab['values'][idx]} (run {run})",
    )
    m = re.search(r"Revision: r(\d+)", out)
    if not m:
        raise SystemExit(f"upload of {name} failed:\n{out[-2000:]}")
    return name, int(m.group(1))


def one_run(
    args: argparse.Namespace,
    ab: dict[str, Any],
    ds: dict[str, Any],
    profile: str,
    rev: int,
    label: str,
) -> str | None:
    out = start_run(args, ds, profile, rev, label)
    m = re.search(r"Test run started: (\S+)", out)
    if not m:
        print(f"  ! could not start {label}:\n{out[-1500:]}", flush=True)
        return None
    return wait_run(args, ds, m.group(1), label)


def start_run(
    args: argparse.Namespace,
    ds: dict[str, Any],
    profile: str,
    rev: int,
    label: str,
) -> str:
    """Start one Test Studio run, one start at a time across every runner process.

    Test Studio names a run after its test set and the second it starts, so two runs of
    one test set started in the same second collide, and CloudFormation throttles a
    burst of stack lookups. A file lock serialises the starts, and a start that fails
    for either reason is retried.
    """
    lock = Path(args.workdir) / ".start.lock"
    out = ""
    for attempt in range(4):
        with lock.open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                out = start_once(args, ds, profile, rev, label)
                time.sleep(2)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        if "Test run started:" in out:
            return out
        if "unique test run id" not in out and "Rate exceeded" not in out:
            return out
        time.sleep(10 * (attempt + 1))
    return out


def start_once(
    args: argparse.Namespace,
    ds: dict[str, Any],
    profile: str,
    rev: int,
    label: str,
) -> str:
    return cli(
        args,
        "process",
        "--stack-name",
        args.stack,
        "--test-set",
        ds["testset"],
        "--number-of-files",
        str(ds.get("docs", 20)),
        "--config-profile",
        profile,
        "--config-revision",
        str(rev),
        "--context",
        label,
        timeout=600,
    )


def wait_run(
    args: argparse.Namespace, ds: dict[str, Any], run_id: str, label: str
) -> str | None:
    results = Path(args.workdir) / "results"
    results.mkdir(parents=True, exist_ok=True)
    out = cli(
        args,
        "test-result",
        "--stack-name",
        args.stack,
        "--test-run-id",
        run_id,
        "--wait",
        "--timeout",
        "14400",
        "--output-dir",
        str(results),
    )
    if not (results / f"{run_id}-result.json").exists():
        print(f"  ! no result for {run_id} ({label}):\n{out[-1500:]}", flush=True)
        return None
    data = json.loads((results / f"{run_id}-result.json").read_text(encoding="utf-8"))
    max_failed = int(ds.get("max_failed", 0))
    failed = int(data.get("failedFiles") or 0)
    ok_status = data.get("status") == "COMPLETE" or (
        data.get("status") == "PARTIAL_COMPLETE" and failed <= max_failed
    )
    if not ok_status or failed > max_failed:
        print(
            f"  ! {run_id} ({label}) is {data.get('status')} with "
            f"{data.get('failedFiles')} failed files; not counted",
            flush=True,
        )
        return None
    print(f"  ✓ {label}: {run_id}", flush=True)
    return run_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ablation")
    parser.add_argument("--stack", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--measured", default="November 2026")
    parser.add_argument(
        "--workdir",
        default=str(default_workdir()),
    )
    parser.add_argument("--datasets", help="comma-separated dataset keys")
    parser.add_argument("--runs", type=int, help="override the registry's runs")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--only",
        help="comma-separated substrings; run only the values whose name holds one",
    )
    args = parser.parse_args()

    reg = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    ab = next((a for a in reg["ablations"] if a["id"] == args.ablation), None)
    if ab is None or "set" not in ab:
        raise SystemExit(f"{args.ablation}: not in the registry, or has no `set` list")
    if ab.get("baseline"):
        ab["_baseline_set"] = reg["baselines"][ab["baseline"]]["set"]
    keys = args.datasets.split(",") if args.datasets else ab["datasets"]
    keys = [k for k in keys if reg["datasets"][k].get("testset")]
    if not keys:
        raise SystemExit(f"{args.ablation}: no dataset with a test set on the stack")
    runs = args.runs or ab["runs"]

    MANIFESTS.mkdir(parents=True, exist_ok=True)
    mpath = MANIFESTS / f"{ab['id']}.json"
    manifest = (
        json.loads(mpath.read_text(encoding="utf-8"))
        if mpath.exists()
        else {"ablation": ab["id"], "verdict": "Measured; see the table.", "runs": {}}
    )
    # Merge, not replace: a run limited to some datasets (--datasets) must keep the
    # other datasets' limits, or collecting their runs later refuses them.
    manifest["max_failed"] = {
        **(manifest.get("max_failed") or {}),
        **{
            k: int(reg["datasets"][k].get("max_failed", 0))
            for k in keys
            if reg["datasets"][k].get("max_failed")
        },
    }
    manifest.update(
        stack=args.stack,
        region=args.region,
        release=args.release,
        measured=args.measured,
    )
    isolated: set[str] = set(manifest.get("cache_isolated") or [])

    jobs = []
    for ds_key in keys:
        ds = reg["datasets"][ds_key]
        for idx, value in enumerate(ab["values"]):
            if args.only and not any(o in value for o in args.only.split(",")):
                continue
            have = manifest["runs"].get(ds_key, {}).get(value, [])
            if len(have) >= runs:
                continue
            for n in range(len(have), runs):
                profile, rev = make_profile(args, ab, ds_key, ds, idx, n + 1)
                jobs.append((ds_key, ds, value, profile, rev, n + 1))
    print(f"{ab['id']}: {len(jobs)} runs to do", flush=True)

    def save() -> None:
        manifest["cache_isolated"] = sorted(isolated)
        mpath.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(
                one_run,
                args,
                ab,
                ds,
                profile,
                rev,
                f"{ab['id']} {ds_key} {value} run {n}",
            ): (ds_key, value)
            for ds_key, ds, value, profile, rev, n in jobs
        }
        for fut in cf.as_completed(futures):
            run_id = fut.result()
            if run_id:
                ds_key, value = futures[fut]
                with LOCK:
                    manifest["runs"].setdefault(ds_key, {}).setdefault(
                        value, []
                    ).append(run_id)
                    isolated.add(run_id)
                    save()
    save()

    proc = subprocess.run(
        [
            sys.executable,
            str(COLLECT),
            str(mpath),
            "--results-dir",
            str(Path(args.workdir) / "results"),
        ],
        check=False,
    )
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
