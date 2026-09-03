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

Usage:
    python scripts/run_all_tests.py            # run the gate (unit-level suites)
    python scripts/run_all_tests.py --list     # print the plan, run nothing
    python scripts/run_all_tests.py --integration   # run only integration suites
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

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
    # idp_common ships fixture-style helper "tests" that are not a suite.
    "/idp_common/agents/testing/",
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
    # Configuration Profile revision operations: group gate + profile-level scope.
    "nested/api-resolvers/src/lambda/configuration_resolver",
    "nested/api-resolvers/src/lambda/get_file_contents_resolver",
    "nested/api-resolvers/src/lambda/get_sample_document_resolver",
    "nested/api-resolvers/src/lambda/get_stepfunction_execution_resolver",
    "nested/api-resolvers/src/lambda/list_agent_chat_sessions_resolver/tests",
    # Guards the vendored config_scope copies against drifting from the canonical
    # idp_common module — a scope matcher that differs per call site is a
    # privilege-escalation bug.
    "nested/api-resolvers/src/lambda/list_documents_gsi_resolver",
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
    "samples/lambda-hook-inference/GENAIIDP-mistral-ocr-hook",
    "src/lambda/api_handler",
    "src/lambda/batch_pre_processor",
    "src/lambda/bda_ocr_project/tests",
    "src/lambda/calculate_capacity",
    "src/lambda/chat_stream_processor/tests",
    "src/lambda/chat_with_document_processor/tests",
    "src/lambda/circuit_breaker_manager",
    "src/lambda/complete_section_review",
    "src/lambda/external_idp_group_mapping",
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
    # General scripts/ unit tests (e.g. check_data_plane_tags, run-registry
    # tests). Run the tests/ subdir specifically for the same "mis-collect
    # from scripts root" reason as scripts/sdlc/tests above.
    "scripts/tests",
    # Dependency-vulnerability gate (dep_audit.py) unit tests. Registered as a
    # subdir for the same reason as scripts/sdlc/tests above.
    "scripts/security/tests",
    # Repo-script gates (the Python arn:aws: partition checker). Same reason as
    # the two above: the parent `scripts` root stays quarantined, so a new test
    # dir under it is invisible to this gate unless registered here.
    "scripts/tests",
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
    "nested/bedrockkb/src/s3_vectors_manager": (
        "Requires the Lambda-runtime-only 'cfnresponse' module."
    ),
    "samples/lambda-hook-inference/GENAIIDP-chandra-ocr-hook": (
        "test_local.py is a manual local-run script; collects zero pytest tests."
    ),
    "samples/lambda-hook-inference/GENAIIDP-w2-copy-consistency": (
        "test_local.py is a manual local-run script; collects zero pytest tests."
    ),
    # Vendored/internal helper trees that contain test_*.py but are not suites.
    "lib/idp_sdk/idp_sdk/_core": (
        "Source tree, not a test root (contains helper modules named test_*)."
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

    A discovered dir counts as "known" if it equals, or is nested under, a
    registered RUN or QUARANTINE entry (some roots register a parent ``tests``
    dir that owns nested subdirs).
    """
    known_prefixes = [r.rstrip("/") for r in (*RUN_ROOTS, *QUARANTINE)]

    def is_known(d: str) -> bool:
        return any(d == k or d.startswith(k + "/") for k in known_prefixes)

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
    failures: list[str] = []
    for root in roots:
        print(f"\n=== pytest -m '{marker}' {root} ===", flush=True)
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
                root,
            ],
            cwd=REPO_ROOT,
        )
        # Exit code 5 == "no tests collected for this marker", which is fine.
        if result.returncode not in (0, 5):
            failures.append(root)
    print("\n" + "=" * 70)
    if failures:
        print(f"❌ {len(failures)} test root(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
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
