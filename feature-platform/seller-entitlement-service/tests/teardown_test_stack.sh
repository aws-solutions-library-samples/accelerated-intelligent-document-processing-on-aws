#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Tear down a TEST deployment of the Seller Entitlement Service, including the
# resources the template deliberately RETAINS.
#
# Why this script has to exist
# ---------------------------
# `TokenSigningKey` and `ActivationsTable` are `DeletionPolicy: Retain`, and that
# is correct for production: destroying the key invalidates every issued token and
# makes previously-issued ones unverifiable, and the roster is a record of who
# your customers are. `DeletionPolicy` takes a literal — it cannot be `Fn::If`'d —
# so the same policy applies to a throwaway test stack.
#
# Consequence: `delete-stack` alone leaves a KMS key and a DynamoDB table behind
# on EVERY test run. KMS keys cannot be deleted immediately (7–30 day pending
# window), so repeated runs accumulate keys, and each carries a monthly charge.
# This script deletes the stack and then explicitly cleans up what it retained.
#
# ONLY run this against a stack you know is a test deployment. It is destructive
# and it will schedule deletion of a signing key.
#
# Usage:
#   tests/teardown_test_stack.sh --stack-name idp-seller-entitlement-citest \
#       [--region us-east-1] [--pending-window-days 7] [--yes]

set -uo pipefail

STACK_NAME=""
REGION="us-east-1"
PENDING_DAYS=7
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --pending-window-days) PENDING_DAYS="$2"; shift 2 ;;
    --yes) ASSUME_YES=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
die()  { echo -e "${RED}✗ $*${NC}" >&2; exit 1; }

[[ -n "$STACK_NAME" ]] || die "--stack-name is required"

# Refuse to run against anything that doesn't look like a test stack. The whole
# point of Retain is that production keys are not casually destroyed, so this
# script must not be the thing that destroys one.
case "$STACK_NAME" in
  *test*|*citest*|*ci-*|*-tmp|*scratch*) ;;
  *) die "'$STACK_NAME' does not look like a test stack (expected 'test', 'citest',
  'ci-', '-tmp' or 'scratch' in the name).

  This script SCHEDULES DELETION OF A SIGNING KEY. Destroying a production key
  invalidates every token ever issued and makes previously-issued ones
  unverifiable. If you really mean to delete a production deployment, do it by
  hand and deliberately." ;;
esac

echo "Stack:  $STACK_NAME"
echo "Region: $REGION"

# Capture the retained physical ids BEFORE deleting the stack — afterwards the
# stack's resource list is gone and the orphans become hard to find.
KEY_ID="$(aws cloudformation describe-stack-resource \
  --stack-name "$STACK_NAME" --logical-resource-id TokenSigningKey \
  --region "$REGION" --query 'StackResourceDetail.PhysicalResourceId' \
  --output text 2>/dev/null)" || KEY_ID=""
TABLE_NAME="$(aws cloudformation describe-stack-resource \
  --stack-name "$STACK_NAME" --logical-resource-id ActivationsTable \
  --region "$REGION" --query 'StackResourceDetail.PhysicalResourceId' \
  --output text 2>/dev/null)" || TABLE_NAME=""

echo "Retained resources to clean up after stack deletion:"
echo "  KMS key:  ${KEY_ID:-<not found>}"
echo "  DDB table: ${TABLE_NAME:-<not found>}"

if [[ $ASSUME_YES -ne 1 ]]; then
  read -r -p "Proceed with deletion? [y/N] " reply
  [[ "$reply" == "y" || "$reply" == "Y" ]] || { echo "Aborted."; exit 1; }
fi

aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION" \
  || die "delete-stack failed"
echo "Waiting for stack deletion..."
aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" \
  --region "$REGION" || warn "wait returned non-zero; check the stack state"
ok "stack deleted"

if [[ -n "$TABLE_NAME" && "$TABLE_NAME" != "None" ]]; then
  if aws dynamodb delete-table --table-name "$TABLE_NAME" --region "$REGION" \
      >/dev/null 2>&1; then
    ok "deleted retained DynamoDB table $TABLE_NAME"
  else
    warn "could not delete table $TABLE_NAME (already gone?)"
  fi
fi

if [[ -n "$KEY_ID" && "$KEY_ID" != "None" ]]; then
  # KMS has no immediate delete. The minimum pending window is 7 days; until it
  # elapses the key still exists and still bills.
  if aws kms schedule-key-deletion --key-id "$KEY_ID" \
      --pending-window-in-days "$PENDING_DAYS" --region "$REGION" >/dev/null 2>&1; then
    ok "scheduled KMS key $KEY_ID for deletion in ${PENDING_DAYS} day(s)"
    warn "the key is NOT gone yet — KMS has no immediate delete. Repeated test
  runs will accumulate pending-deletion keys, each of which still bills until the
  window elapses."
  else
    warn "could not schedule deletion of key $KEY_ID (already pending?)"
  fi
fi

ok "teardown complete"
