#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# One-shot: run the security tests and curate a public-safe snapshot into
# security/test-results/<version>/. This is the single entry point behind
# `make security-results` and the "run security tests and update results"
# request (see .claude/skills/curate-security-results/SKILL.md).
#
# What runs depends on whether a live stack is available:
#   * Always (no AWS): SRT SAST/deps scan + RBAC static authorization scan.
#   * With STACK_NAME set: also the live RBAC dynamic tests and the ZAP DAST
#     scan against that stack (temporary Cognito users are created + torn down).
# A test that is skipped or fails is recorded as a visible "not run" stub in the
# snapshot rather than aborting the whole run.
#
# Env / args (all optional except as noted):
#   STACK_NAME   deployed stack for the live tests (omit to snapshot offline tests only)
#   REGION       stack region (default: profile/AWS_DEFAULT_REGION)
#   VERSION      snapshot label (default: repo VERSION file)
#   DATE         snapshot date YYYY-MM-DD (default: today)
#   PYTHON       python interpreter (default: python3)
#   SKIP_SRT=1   skip the (slow, ~5-15 min) SRT scan and curate from whatever
#                .srt/issues.json / committed register is present
#
# Usage:
#   scripts/security/run_security_tests.sh                       # offline tests only
#   STACK_NAME=IDP1 REGION=us-west-2 scripts/security/run_security_tests.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-python3}"
VERSION="${VERSION:-$(cat VERSION 2>/dev/null || echo unknown)}"
DATE="${DATE:-$(date +%F)}"
SCRATCH="$REPO_ROOT/scratch"
RBAC_STATIC_OUT="$SCRATCH/rbac-static.txt"
ZAP_DIR="$SCRATCH/zap-reports"

echo "════════════════════════════════════════════════════════════════"
echo " Security tests → curated snapshot"
echo "   version: $VERSION   date: $DATE"
echo "   stack:   ${STACK_NAME:-<none — offline tests only>}${REGION:+ ($REGION)}"
echo "════════════════════════════════════════════════════════════════"

mkdir -p "$SCRATCH" "$ZAP_DIR"

# 1) SRT SAST / dependency scan (slow; writes .srt/issues.json). Best-effort:
#    the curator falls back to the committed register if this is skipped/fails.
if [ "${SKIP_SRT:-}" = "1" ]; then
  echo "── [1/4] SRT scan — SKIPPED (SKIP_SRT=1)"
else
  echo "── [1/4] SRT scan (SAST + deps, ~5-15 min)…"
  make srt-scan || echo "   ⚠ SRT scan failed/absent — curator will use the committed register"
fi

# 2) RBAC static authorization scan (offline, CI-safe). Capture stdout so the
#    curator can enumerate the S1–S5 checks.
echo "── [2/4] RBAC static scan…"
$PYTHON scripts/sdlc/scan_api_rbac.py 2>&1 | tee "$RBAC_STATIC_OUT" || true

CURATE_ARGS=(--date "$DATE" --version "$VERSION" --rbac-static "$RBAC_STATIC_OUT")

if [ -n "${STACK_NAME:-}" ]; then
  # 3) RBAC dynamic (live) — creates + tears down temporary Cognito users.
  echo "── [3/4] RBAC dynamic tests vs $STACK_NAME…"
  make api-test STACK_NAME="$STACK_NAME" ${REGION:+REGION="$REGION"} \
    REPORT_DIR="$SCRATCH/api-test-results" \
    || echo "   ⚠ RBAC dynamic failed — snapshot will stub it"

  # 4) ZAP DAST (live). Tee the scan stdout into the report dir: the ZAP JSON
  #    carries findings only, so the per-rule PASS/WARN/IGNORE enumeration
  #    exists ONLY in stdout.
  echo "── [4/4] ZAP DAST scan vs $STACK_NAME…"
  make stacktest-zap STACK_NAME="$STACK_NAME" ${REGION:+REGION="$REGION"} \
    REPORT_DIR="$ZAP_DIR" 2>&1 | tee "$ZAP_DIR/zap-scan-stdout.txt" \
    || echo "   ⚠ ZAP scan failed — snapshot will stub it"
else
  echo "── [3/4] RBAC dynamic — SKIPPED (no STACK_NAME)"
  echo "── [4/4] ZAP DAST      — SKIPPED (no STACK_NAME)"
  echo "   (the curator reuses the newest reports in scratch/ if a prior live"
  echo "    run exists; otherwise these two are recorded as 'not run' stubs)"
fi

echo "── Curating snapshot → security/test-results/$VERSION/"
$PYTHON scripts/security/curate_results.py "${CURATE_ARGS[@]}"

echo "════════════════════════════════════════════════════════════════"
echo " Done. Review security/test-results/$VERSION/ before committing."
echo "════════════════════════════════════════════════════════════════"
