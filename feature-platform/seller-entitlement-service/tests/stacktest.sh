#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# End-to-end stack-test for the Seller Entitlement Service: deploy a throwaway
# stack, prove the deployed API actually behaves, tear it down.
#
# Why this exists
# ---------------
# The static suite (`tests/test_template_security.py`) asserts what the template
# *says*. It cannot assert that the template *deploys* — and that is where this
# service has actually broken. Two real defects were both deploy-time failures
# that every offline test passed straight through:
#
#   1. SAM defaults InvokeRole to CALLER_CREDENTIALS under an AWS_IAM authorizer,
#      which API Gateway refuses to deploy alongside a resource policy
#      ("Caller provided credentials not allowed when resource policy is set").
#   2. A REST stage with access logging is rejected unless the account-level
#      API Gateway CloudWatch role exists — which it does not in a fresh seller
#      account, i.e. exactly the account this service is meant for.
#
# Both would have been caught by one deploy. Hence this.
#
# No real Marketplace product is required
# ---------------------------------------
# Every assertion here is a *refusal*, and a refusal looks the same whether the
# product exists or not (that is deliberate — see the no-oracle design). So the
# test registers a synthetic `prod-…` id and passes --skip-ownership-check. That
# means this runs in any account with the necessary permissions, not only in a
# real seller account with a real listing.
#
# What it does NOT cover: the positive path. Issuing a verifiable token needs a
# genuinely subscribed buyer account, which cannot be created on demand. Run
# `dynamic_activation_test.py --entitled-profile …` by hand for that.
#
# Usage:
#   tests/stacktest.sh [--stack-name idp-seller-entitlement-citest]
#                      [--region us-east-1] [--no-teardown]

set -uo pipefail

STACK_NAME="idp-seller-entitlement-citest"
REGION="us-east-1"
NO_TEARDOWN=0
# Synthetic, and deliberately not a real product id: the point is to exercise the
# refusal paths, and a real id would make the run depend on live subscription
# state.
SYNTHETIC_PRODUCT="prod-citestsynthetic"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --no-teardown) NO_TEARDOWN=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
info() { echo -e "${CYAN}→${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
die()  { echo -e "${RED}✗ $*${NC}" >&2; exit 1; }

# teardown_test_stack.sh refuses names that don't look disposable, so fail early
# and clearly rather than after a deploy we then cannot clean up.
case "$STACK_NAME" in
  *test*|*citest*|*ci-*|*-tmp|*scratch*) ;;
  *) die "--stack-name '$STACK_NAME' does not look disposable. This test DELETES
  the stack it deploys; use a name containing 'test', 'citest', 'ci-', '-tmp' or
  'scratch' so the teardown script will accept it." ;;
esac

command -v sam >/dev/null 2>&1 || die "the AWS SAM CLI is required"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)" \
  || die "could not resolve AWS credentials"

# Resolve an interpreter that actually has boto3 (the dynamic probe signs its own
# requests with botocore). A bare `python3` is often the system one, which does
# not — so prefer an active virtualenv, then fall back.
PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  for candidate in "${VIRTUAL_ENV:-}/bin/python" python3 python; do
    [[ -n "$candidate" ]] || continue
    if command -v "$candidate" >/dev/null 2>&1 && \
       "$candidate" -c 'import boto3' >/dev/null 2>&1; then
      PYTHON="$candidate"; break
    fi
  done
fi
[[ -n "$PYTHON" ]] || die "no Python with boto3 found. Activate your virtualenv, or
  set PYTHON=/path/to/python. (\`make setup\` / \`make setup-venv\` installs it.)"

CLI=(idp-feature-cli)
command -v idp-feature-cli >/dev/null 2>&1 || CLI=("$PYTHON" -m idp_feature_sdk.cli)

echo "Account: $ACCOUNT"
echo "Region:  $REGION"
echo "Stack:   $STACK_NAME  (will be deleted)"
echo

teardown() {
  if [[ $NO_TEARDOWN -eq 1 ]]; then
    warn "--no-teardown: leaving $STACK_NAME up. Clean up with:
  tests/teardown_test_stack.sh --stack-name $STACK_NAME --region $REGION --yes"
    return
  fi
  echo
  info "tearing down $STACK_NAME (including retained key + table)"
  "$HERE/teardown_test_stack.sh" --stack-name "$STACK_NAME" --region "$REGION" --yes \
    || warn "teardown reported a problem — check for leftover resources"
}
trap teardown EXIT

REGISTRY="{\"$SYNTHETIC_PRODUCT\":{\"productCode\":\"citest\",\"allowFreeTier\":false}}"

# The deploy itself is assertion #1: it fails on either of the two defects above.
# It also reads the registry back off the deployed function and fails if it did
# not survive SAM's parameter-override parsing, which is assertion #2.
info "deploying (this is the regression test for the deploy-time defects)"
"${CLI[@]}" seller-service deploy \
  --product-registry "$REGISTRY" \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --skip-ownership-check \
  --yes || die "deploy failed — see the error above"
ok "deployed, and the product registry survived deployment"

ENDPOINT="$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='ActivationEndpoint'].OutputValue" \
  --output text 2>/dev/null)"
[[ -n "$ENDPOINT" && "$ENDPOINT" != "None" ]] || die "no ActivationEndpoint output"
info "endpoint: $ENDPOINT"

# Assertion #3: the live stage really enforces SigV4, refuses cleanly, and never
# 5xxes on hostile input.
info "running the live security probe"
"$PYTHON" "$HERE/dynamic_activation_test.py" \
  --endpoint "$ENDPOINT" --product-id "$SYNTHETIC_PRODUCT" \
  || die "live security probe failed"

echo
ok "seller-service stack-test passed"
