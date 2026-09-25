# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Discover and run every Python test suite in the repo, one root at a time.

Why not a single ``pytest`` from the repo root? Several packages ship their own
``tests/conftest.py``; pytest imports them all as the module ``tests.conftest``
and aborts with ``ImportPathMismatchError`` / duplicate-plugin errors. Each
package/Lambda also has its own mini-environment (relative imports, per-dir
conftest, ``sys.modules`` shims). So each test *root* must run as a SEPARATE
pytest invocation.

The maintenance hazard with a hand-written list of roots (the old ``make test``)
is that a brand-new test directory is silently never run. This script removes
that hazard: it DISCOVERS every directory containing ``test_*.py`` and checks it
against two explicit registries — ``RUN_ROOTS`` (run in the gate) and
``QUARANTINE`` (known-excluded, each with a reason). A directory in NEITHER list
is a hard error, so adding tests in a new location forces a conscious decision
here.

It also decides the verdict on the **standing-failure baseline** declared in
``.claude/skills/full-test-battery.md``, because this is the process that has the
results. A failure nobody declared is red, as before; a declared failure that did
not occur is now red too, so a row cannot outlive its cause and become a waiver
over a passing test. Each root's results are written as JUnit XML under
``test-reports/``, alongside a ``run_all_tests.json`` summary, for a reader to
inspect — the verdict never reads them back, so there is no stale-artifact path
through it. See ``scripts/standing_failures.py``.

Usage:
    python scripts/run_all_tests.py            # run the gate (unit-level suites)
    python scripts/run_all_tests.py --list     # print the plan, run nothing
    python scripts/run_all_tests.py --integration   # run only integration suites
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from first_party_paths import pinned_environment  # noqa: E402
from standing_failures import (  # noqa: E402
    BaselineError,
    compare,
    declared_failures,
    describe,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Where each root's JUnit XML and the run summary land. Gitignored: it is a
# record of one machine's run, not a repository fact.
REPORT_DIR = REPO_ROOT / "test-reports"

# Directories that are NOT source test roots (build output, deps, vendored copies).
PRUNE_DIR_MARKERS = (
    "/.venv/",
    "/node_modules/",
    "/.aws-sam/",
    "/build/lib/",
    "/site-packages/",
    "/.git/",
    "/.pytest_cache/",
    # scratch/ is gitignored (local benchmarks, cloned tools, throwaway work);
    # never part of the gate. CI never sees it, so prune it locally too.
    "/scratch/",
    # Two gitignored, locally-staged copies of lib/idp_common_pkg that the
    # idp-data-generator feature's build drops next to its Lambda sources. They
    # hold library code only (no tests/ dir), so every test_*.py they contain is
    # a duplicate of one in lib/idp_common_pkg. CI never sees them; a developer
    # machine that has built that feature does. They cannot be matched by a
    # shared substring -- `/idp-data-generator/` would also prune
    # feature-platform/idp-data-generator/feature-api/tests, which is a real
    # registered suite -- so each copy root is named.
    "/idp-data-generator/idp_common_pkg/",
    "/idp-data-generator/bootstrap-processor/idp_common_pkg/",
    # Agent worktrees: `git worktree` checkouts of this same repo, created under
    # .claude/worktrees/ when work is delegated to a subagent. Every test file in
    # the repo therefore appears once per live worktree, so without this the guard
    # reports the entire suite as "unregistered test roots" and the gate fails for
    # a reason that has nothing to do with the code under test. They are also
    # gitignored, so CI never sees them.
    "/.claude/worktrees/",
)

# Generated files that are never source tests. `make srt-scan` nbconverts every
# notebook to "<nb>-converted.py" (gitignored); notebooks named test_*.ipynb
# therefore leave behind test_*-converted.py artifacts that would otherwise make
# notebooks/ look like an unregistered test root.
PRUNE_FILE_SUFFIXES = ("-converted.py",)

# --- Registry 1: roots RUN in the fast (non-integration) gate -----------------
# Each entry is a path relative to the repo root. They are run as independent
# `pytest -m "not integration" <root>` invocations. Verified green headless.
RUN_ROOTS = [
    "lib/idp_common_pkg/tests",
    "lib/idp_cli_pkg/tests",
    "lib/idp_sdk/tests",
    "lib/idp_feature_sdk/tests",
    "feature-platform/main-stack-extensions/tests",
    # Seller entitlement service (Marketplace signing/entitlement). Already run
    # explicitly by `make test-packages-cicd`, so it is verified green headless;
    # registering it here keeps `make test` from hard-erroring on an
    # unclassified directory, which blocked the whole gate.
    "feature-platform/seller-entitlement-service/tests",
    # ConfBench Test Set extension. test_planner.py self-skips unless
    # huggingface_hub + pyarrow are installed (ingest/planner.py imports both at
    # module scope); the other three modules run unconditionally. Install
    # feature-platform/confbench-testset/tests/requirements.txt to run all of it.
    "feature-platform/confbench-testset/tests",
    "feature-platform/feature-template/feature-api/tests",
    "feature-platform/idp-data-generator/feature-api/tests",
    "feature-platform/pii-anonymizer/feature-api/tests",
    "feature-platform/pii-anonymizer/hook/tests",
    "feature-platform/pii-anonymizer/ui-deployer/tests",
    "feature-platform/feature-template/ui-deployer/tests",
    "feature-platform/sample-feature/feature-api/tests",
    "feature-platform/sample-feature/ui-deployer/tests",
    "feature-platform/sample-health-insurance-review/feature-api/tests",
    "feature-platform/sample-health-insurance-review/hook/tests",
    "feature-platform/sample-health-insurance-review/ui-deployer/tests",
    # Structural assertions on patterns/unified/statemachine/workflow.asl.json —
    # invariants that live in the state machine (retry policies, failure routing)
    # where no Python test can see them. Pure JSON parsing, no AWS clients.
    "patterns/unified/tests",
    # Benchmark harness analysis/orchestration code. It decides what a release
    # report *claims* and which config a run actually executes, so a bug here
    # becomes a wrong published number rather than a visible failure — which is
    # exactly what happened at v0.6.5. Pure dict/YAML logic, no AWS.
    "benchmarks/tests",
    "nested/multi-doc-discovery/docker_build_lambda/tests",
    # S3 Vectors custom resource. The directory root, not its `tests` subdirectory:
    # `test_handler.py` sits beside `handler.py` and the nested suite is reached by
    # nesting under this entry. `conftest.py` here stubs `cfnresponse` and supplies
    # a region and placeholder credentials, which is what makes the handler
    # importable outside Lambda.
    "nested/bedrockkb/src/s3_vectors_manager",
    # Configuration Profile revision operations: group gate + profile-level scope.
    "nested/api-resolvers/src/lambda/configuration_resolver",
    "nested/api-resolvers/src/lambda/get_file_contents_resolver",
    # listFinetuningJobs is an ANY operation that runs a sparse filtered scan of
    # the whole TrackingTable, so its page/time bound is the only thing between an
    # authenticated caller and a full-history read.
    "nested/api-resolvers/src/lambda/finetuning_jobs_resolver",
    # Chat-session ownership: the refusal must reach the caller as an
    # authorization denial rather than being laundered into a 500 by the
    # handler's catch-all.
    # The discovery upload path's bucket/key constraint: `bucket` and `prefix` are
    # request arguments and this function's role holds write on the discovery bucket.
    "nested/api-resolvers/src/lambda/discovery_upload_resolver",
    "nested/api-resolvers/src/lambda/get_agent_chat_messages_resolver",
    "nested/api-resolvers/src/lambda/get_sample_document_resolver",
    "nested/api-resolvers/src/lambda/get_stepfunction_execution_resolver",
    "nested/api-resolvers/src/lambda/list_agent_chat_sessions_resolver/tests",
    # Guards the vendored config_scope copies against drifting from the canonical
    # idp_common module — a scope matcher that differs per call site is a
    # privilege-escalation bug — plus the fail-closed scope lookup and the
    # getDocumentCount filtering that makes its `scope_filtered` declaration true.
    "nested/api-resolvers/src/lambda/list_documents_gsi_resolver",
    # Sibling of the above: the same fail-closed scope-lookup contract on the
    # date-range list, and the reviewer-owner matching that an absent email claim
    # would otherwise widen.
    "nested/api-resolvers/src/lambda/list_documents_range_resolver",
    # syncBdaIdp mutates the BDA project linked to a Configuration Profile, so a
    # caller whose scope cannot be resolved must be refused in-band.
    "nested/api-resolvers/src/lambda/sync_bda_idp_resolver",
    "nested/api-resolvers/src/lambda/send_chat_document_message_resolver/tests",
    # Configuration-revision pinning on a test run.
    "nested/api-resolvers/src/lambda/test_runner",
    "nested/api-resolvers/src/lambda/test_set_resolver",
    # Reprocess resolver: output-data deletion on reprocess (nested-stack
    # namespace handling). Arrived with #719 unregistered, which made `make test`
    # fail on develop and — the reason this guard exists — meant its 5 tests were
    # never actually run. Verified green headless.
    "nested/api-resolvers/src/lambda/reprocess_document_resolver",
    "nested/api-resolvers/src/lambda/upload_resolver",
    "nested/bedrockkb/src/start_ingestion_job_custom_resource",
    "samples/lambda-hook-inference/GENAIIDP-cohere-parse-hook",
    "samples/lambda-hook-inference/GENAIIDP-mistral-ocr-hook",
    # Registered rather than quarantined: it was excluded on the stated ground
    # that "test_local.py is a manual local-run script; collects zero pytest
    # tests", which is false -- it collects 6 and all 6 pass. The real
    # obstruction is a module-scope `from index import lambda_handler`, which
    # collides with the identically-named modules in its sibling hook
    # directories, so it fails only when collected alongside them. This runner
    # invokes each root in its own subprocess, so it runs clean here, and six
    # green tests that were excluded from every gate are now gated.
    "samples/lambda-hook-inference/GENAIIDP-w2-copy-consistency",
    "src/lambda/api_handler",
    "src/lambda/batch_pre_processor",
    "src/lambda/bda_ocr_project/tests",
    "src/lambda/calculate_capacity",
    "src/lambda/chat_stream_processor/tests",
    "src/lambda/chat_with_document_processor/tests",
    "src/lambda/circuit_breaker_manager",
    "src/lambda/complete_section_review",
    "src/lambda/external_idp_group_mapping",
    "src/lambda/finetuning_deployment_handler",
    "src/lambda/finetuning_job_creator/tests",
    "src/lambda/job_tracker",
    "src/lambda/queue_processor",
    "src/lambda/queue_sender",
    "src/lambda/save_reporting_data",
    "src/lambda/test_file_copier",
    "src/lambda/user_management",
    "src/lambda/version_check_resolver",
    "src/lambda/workflow_tracker",
    "config_library",
    # SDLC CodeBuild harness unit tests (deployment-variant probe framework).
    # Run the sdlc/tests subdir specifically — the parent `scripts` root stays
    # quarantined because a bare `pytest scripts` mis-collects test_api_rbac.py.
    "scripts/sdlc/tests",
    # General scripts/ unit tests: the check_data_plane_tags and run-registry
    # tests, and the repo-script gates (the Python arn:aws: partition checker).
    # Run the tests/ subdir specifically for the same "mis-collect from scripts
    # root" reason as scripts/sdlc/tests above. Listed ONCE — it was registered
    # twice, under two comments, when the second set of gates was added, which
    # ran the whole 1,000-test suite twice per `make test` (~3.5 min each) and
    # printed the root twice in the failed-roots summary.
    "scripts/tests",
    # Dependency-vulnerability gate (dep_audit.py) unit tests. Registered as a
    # subdir for the same reason as scripts/sdlc/tests above.
    "scripts/security/tests",
    # SRT gate helpers (ci_paths.py) plus the guard that keeps gitignored
    # build-artifact paths out of the committed scripts/srt/issues.json baseline.
    # Same registration reason as the three above.
    "scripts/srt/tests",
]

# --- Registry 2: roots explicitly EXCLUDED, each with a reason ----------------
# These are known-not-runnable in the shared gate. Kept here (not silently
# dropped) so the "unclassified dir" check stays meaningful and the reason is
# discoverable. Revisit periodically.
QUARANTINE = {
    "scripts": (
        "Not a test suite — scripts/test_api_rbac.py is the live RBAC harness "
        "(run via `make api-test`); pytest mis-collects its test_email() helper."
    ),
    "src/lambda/ocr_benchmark_deployer": (
        "Requires huggingface_hub, which is not a test dependency."
    ),
    "samples/lambda-hook-inference/GENAIIDP-chandra-ocr-hook": (
        "test_local.py is a manual local-run script; collects zero pytest tests."
    ),
    # Vendored/internal helper trees that contain test_*.py but are not suites.
    "lib/idp_sdk/idp_sdk/_core": (
        "Source tree, not a test root (contains helper modules named test_*)."
    ),
    # Operator-run agent scripts. Every one drives real Bedrock, Athena or
    # DynamoDB against a deployed stack, so they are run by hand, never in a gate,
    # and `norecursedirs` in lib/idp_common_pkg/pytest.ini keeps pytest from
    # collecting them. Registered here rather than pruned above so that the
    # exclusion carries the registry's ratchets: the directory appears in
    # `--list`, it has to be named in docs/testing.md, and -- because nesting
    # under a QUARANTINE entry deliberately does not inherit the exclusion -- a
    # NEW subdirectory of manual_tests/ fails this guard instead of being
    # silently accepted, which a substring prune marker would have allowed.
    "lib/idp_common_pkg/manual_tests/agents": (
        "Operator-run scripts that call real Bedrock/Athena against a deployed "
        "stack; run by hand, excluded from pytest collection by "
        "lib/idp_common_pkg/pytest.ini's norecursedirs."
    ),
}


def discover_test_roots() -> set[str]:
    """Return the set of repo-relative dirs that directly contain a test_*.py."""
    roots: set[str] = set()
    for path in REPO_ROOT.rglob("test_*.py"):
        posix = "/" + path.as_posix().replace(REPO_ROOT.as_posix() + "/", "")
        if any(marker in posix for marker in PRUNE_DIR_MARKERS):
            continue
        if path.name.endswith(PRUNE_FILE_SUFFIXES):
            continue
        rel_dir = path.parent.relative_to(REPO_ROOT).as_posix()
        roots.add(rel_dir)
    return roots


def classify(discovered: set[str]) -> tuple[list[str], list[str]]:
    """Split discovered roots against the registries; error on any unknown.

    A discovered dir counts as "known" if it equals a registered RUN or
    QUARANTINE entry, or is nested under a **RUN** entry -- several roots
    register a parent ``tests`` dir that owns nested subdirs, and those nested
    dirs genuinely do run.

    Nesting under a QUARANTINE entry deliberately does NOT count. It used to,
    and that quietly inverted the guard's purpose: quarantining ``scripts``
    accepted every future test directory anywhere beneath it, so a new suite
    under ``scripts/`` satisfied this check while being run by nothing. A probe
    confirmed it -- ``scripts/probe_area/test_probe.py`` containing
    ``assert False`` left the whole suite green and never appeared in
    ``--list``. An exclusion should cover what somebody decided to exclude, not
    everything that later appears underneath it, so each excluded directory is
    now named on its own.
    """
    run_prefixes = [r.rstrip("/") for r in RUN_ROOTS]
    exact = {r.rstrip("/") for r in (*RUN_ROOTS, *QUARANTINE)}

    def is_known(d: str) -> bool:
        if d in exact:
            return True
        return any(d.startswith(k + "/") for k in run_prefixes)

    unknown = sorted(d for d in discovered if not is_known(d))
    if unknown:
        lines = "\n".join(f"  - {d}" for d in unknown)
        raise SystemExit(
            "ERROR: found test directories not registered in "
            f"scripts/run_all_tests.py:\n{lines}\n\n"
            "Add each to RUN_ROOTS (if it should run in the gate) or to "
            "QUARANTINE with a reason. This guard exists so new tests are never "
            "silently skipped."
        )
    # Only run registered roots that still exist on disk.
    run = [r for r in RUN_ROOTS if (REPO_ROOT / r).exists()]
    quarantined = sorted(QUARANTINE)
    return run, quarantined


# Parallelize each root across cores with pytest-xdist. `auto` = one worker per
# CPU; override with PYTEST_WORKERS (e.g. "4", or "0"/"1" to disable — handy if a
# suite has cross-test state that misbehaves under xdist). Falls back to serial
# automatically if pytest-xdist isn't installed.
_PYTEST_WORKERS = os.environ.get("PYTEST_WORKERS", "auto")


def _xdist_available() -> bool:
    try:
        import xdist  # noqa: F401

        return True
    except ImportError:
        return False


def _junit_path(root: str) -> Path:
    """One XML per root, named after the root so the file says what it covers."""
    return REPORT_DIR / f"{root.strip('/').replace('/', '_')}.xml"


def _display_path(path: Path) -> str:
    """Repo-relative if it is inside the tree, absolute otherwise."""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _report_bases(root: str) -> list[Path]:
    """Directories a JUnit ``file`` attribute could be relative to, deepest first.

    pytest writes paths relative to the *rootdir* it computed for that
    invocation, which is an ancestor-or-self of the root we passed (several
    packages here carry their own ``pytest.ini``, so it is rarely the repo root).
    The XML does not record which, so resolve by trying each candidate and taking
    the one that names a file that exists.
    """
    bases: list[Path] = []
    base = (REPO_ROOT / root).resolve()
    while True:
        bases.append(base)
        if base == REPO_ROOT or REPO_ROOT not in base.parents:
            break
        base = base.parent
    return bases


def _node_id(file_attr: str, classname: str, name: str, root: str) -> str:
    """Rebuild a repo-relative pytest node id from one JUnit ``testcase``.

    ``classname`` is the dotted module path plus any enclosing classes, so the
    classes are whatever it holds beyond the module. If it does not start with
    the module (an unusual importmode, or a collection error whose testcase
    carries no module at all) fall back to ``path::name``, which still names the
    file and so still reports something a reader can act on.
    """
    resolved = None
    for base in _report_bases(root):
        candidate = base / file_attr
        if candidate.is_file():
            resolved = candidate.relative_to(REPO_ROOT).as_posix()
            break
    path = resolved or file_attr
    module = file_attr[:-3].replace("/", ".") if file_attr.endswith(".py") else ""
    parts = [path]
    if module and classname.startswith(module + "."):
        parts += classname[len(module) + 1 :].split(".")
    parts.append(name)
    return "::".join(parts)


def failing_node_ids(xml_path: Path, root: str) -> list[str]:
    """The node ids of every failing or erroring test in one root's JUnit XML.

    A missing or unparseable file yields nothing, which the caller treats as an
    *unexplained* failure for a root that exited non-zero rather than as a pass —
    "no findings" and "could not read the findings" must not look alike.
    """
    if not xml_path.is_file():
        return []
    try:
        tree = ET.parse(xml_path)  # noqa: S314 - pytest's own output, not input
    except ET.ParseError:
        return []
    found: list[str] = []
    for case in tree.iter("testcase"):
        if case.find("failure") is None and case.find("error") is None:
            continue
        found.append(
            _node_id(
                case.get("file", ""),
                case.get("classname", ""),
                case.get("name", ""),
                root,
            )
        )
    return found


def _write_summary(payload: dict[str, object]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "run_all_tests.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _head_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def run_gate(roots: list[str], integration: bool) -> int:
    marker = "integration" if integration else "not integration"
    python = os.environ.get("PYTHON") or sys.executable
    # Build the -n flag once. Skip it when disabled or xdist is missing so the
    # gate still runs (serially) in a minimal environment.
    parallel = []
    if _PYTEST_WORKERS not in ("0", "1", "") and _xdist_available():
        parallel = ["-n", _PYTEST_WORKERS]
    elif _PYTEST_WORKERS not in ("0", "1", "") and not _xdist_available():
        print("⚠️ pytest-xdist not installed — running serially", flush=True)

    # Pin this checkout's own first-party packages onto every child's PYTHONPATH.
    # Without it `import idp_common` follows the editable-install pointer in the
    # interpreter's site-packages, which on a host sharing one interpreter between
    # checkouts names whichever tree last ran an install — so the gate reports on
    # that tree, and reports green while doing it, because the package it imported
    # is a real revision of this one (#1094). Built once: the value is absolute and
    # identical for every root.
    child_env = pinned_environment(REPO_ROOT)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    unexplained: list[str] = []
    observed: set[str] = set()
    per_root: list[dict[str, object]] = []
    for root in roots:
        print(f"\n=== pytest -m '{marker}' {root} ===", flush=True)
        xml_path = _junit_path(root)
        xml_path.unlink(missing_ok=True)
        result = subprocess.run(
            [
                python,
                "-m",
                "pytest",
                "-m",
                marker,
                *parallel,
                "-q",
                "-p",
                "no:cacheprovider",
                # xunit1 is what carries the `file` attribute on each testcase;
                # xunit2 drops it, leaving only a dotted classname that cannot be
                # turned back into a path when two suites share a module name.
                "-o",
                "junit_family=xunit1",
                f"--junitxml={xml_path}",
                root,
            ],
            cwd=REPO_ROOT,
            env=child_env,
        )
        root_failures = failing_node_ids(xml_path, root)
        observed.update(root_failures)
        # Exit code 5 == "no tests collected for this marker", which is fine.
        failed = result.returncode not in (0, 5)
        if failed:
            failures.append(root)
            if not root_failures:
                unexplained.append(root)
        per_root.append(
            {
                "root": root,
                "exitCode": result.returncode,
                "failed": failed,
                "failing": sorted(root_failures),
                "junit": _display_path(xml_path),
            }
        )

    print("\n" + "=" * 70)
    if failures:
        print(f"{len(failures)} test root(s) reported failures:")
        for f in failures:
            print(f"  - {f}")

    if integration:
        # The declared baseline describes the non-integration battery, so an
        # integration run has nothing to compare against: every declared row
        # would read as "did not fail here" for the trivial reason that it was
        # never run. Report and exit on the roots themselves.
        _write_summary(
            {
                "schemaVersion": 1,
                "generated": datetime.now(timezone.utc).isoformat(),
                "commit": _head_commit(),
                "marker": marker,
                "roots": per_root,
                "baselineCompared": False,
                "verdict": "red" if failures else "green",
            }
        )
        if failures:
            return 1
        print(f"✅ All {len(roots)} test roots passed.")
        return 0

    try:
        declared = declared_failures()
    except BaselineError as exc:
        print(f"\n❌ the standing-failure baseline cannot be read: {exc}")
        _write_summary(
            {
                "schemaVersion": 1,
                "generated": datetime.now(timezone.utc).isoformat(),
                "commit": _head_commit(),
                "marker": marker,
                "roots": per_root,
                "baselineError": str(exc),
            }
        )
        return 1

    verdict = compare(observed, {row.node_id for row in declared})
    print()
    print(describe(verdict))
    for root in unexplained:
        print(
            f"❌ {root} exited non-zero but its JUnit XML names no failing test — "
            "a crash, an internal pytest error, or a failure before anything could "
            "be collected. Read the output above; this is the one kind of failure "
            "the baseline cannot cover, because it keys on test node ids and there "
            "is none. (A module that fails to *import* does produce an entry, under "
            "a synthetic name, so that case is declarable like any other.)"
        )

    _write_summary(
        {
            "schemaVersion": 1,
            "generated": datetime.now(timezone.utc).isoformat(),
            "commit": _head_commit(),
            "marker": marker,
            "roots": per_root,
            "observed": list(verdict.observed),
            "declared": list(verdict.declared),
            "unexpected": list(verdict.unexpected),
            "resolved": list(verdict.resolved),
            "unexplainedRoots": unexplained,
            "baselineCompared": True,
            "verdict": "green" if verdict.agrees and not unexplained else "red",
        }
    )

    if not verdict.agrees or unexplained:
        return 1
    if verdict.declared:
        print(
            f"✅ {len(roots)} test roots ran; the only failures are the "
            f"{len(verdict.declared)} declared standing failure(s)."
        )
    else:
        print(f"✅ All {len(roots)} test roots passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list", action="store_true", help="print the plan and exit (run nothing)"
    )
    parser.add_argument(
        "--integration",
        action="store_true",
        help="run integration-marked tests instead of the default gate",
    )
    args = parser.parse_args()

    discovered = discover_test_roots()
    run, quarantined = classify(discovered)

    if args.list:
        print(f"RUN ({len(run)} roots):")
        for r in run:
            print(f"  + {r}")
        print(f"\nQUARANTINE ({len(quarantined)} roots):")
        for q in quarantined:
            print(f"  - {q}: {QUARANTINE[q]}")
        return 0

    return run_gate(run, integration=args.integration)


if __name__ == "__main__":
    raise SystemExit(main())
