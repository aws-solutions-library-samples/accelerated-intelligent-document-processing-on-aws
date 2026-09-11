#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Discover CloudFormation templates (default) or Step Functions definitions
# (`asl`) by CONTENT, one repo-relative path per line, sorted.
#
# WHY CONTENT, NOT FILENAME. `make check-arn-partitions` used to iterate a
# hardcoded glob list (template.yaml, patterns/*/template.yaml, ...) and so never
# looked at nested/, samples/, notebooks/, scripts/ or iam-roles/. The blind spot
# hid six hardcoded `arn:aws:states:::` service-integration ARNs in two state
# machines that Step Functions rejects outright in GovCloud. Anything declaring
# `AWSTemplateFormatVersion` is a template; anything with a top-level `"StartAt"`
# is a state machine. A new one cannot be added without being covered.
#
# WHY GIT. `git ls-files --cached --others --exclude-standard` yields exactly the
# files that are, or could be, committed: .gitignore is honoured (build output,
# .aws-sam/, node_modules/, worktrees under scratch/) and an untracked new
# template is checked before it is `git add`ed. Outside a checkout it falls back
# to `find` with the same prune list `make cfn-lint` shipped with.
#
# Shared by `make cfn-lint` and `make check-arn-partitions` so the two gates
# cannot drift apart again. Tested by scripts/tests/test_discover_templates.py.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

kind="${1:-cfn}"
case "$kind" in
  cfn) patterns=('*.yaml' '*.yml'); marker='^AWSTemplateFormatVersion' ;;
  asl) patterns=('*.json');         marker='"StartAt"[[:space:]]*:' ;;
  *) echo "usage: $0 [cfn|asl]" >&2; exit 2 ;;
esac

list_candidates() {
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git ls-files -z --cached --others --exclude-standard -- "${patterns[@]}"
  else
    local names=()
    for p in "${patterns[@]}"; do
      [ ${#names[@]} -gt 0 ] && names+=(-o)
      names+=(-name "$p")
    done
    find . \
      \( -name node_modules -o -name .aws-sam -o -name .venv -o -name .git \
         -o -name build -o -name dist -o -name __pycache__ \) -prune -o \
      -type f \( "${names[@]}" \) -print0
  fi
}

# `grep -l` exits 1 when nothing matches; that is an empty result, not an error.
list_candidates \
  | xargs -0 -r grep -lE "$marker" -- 2>/dev/null \
  | sed 's|^\./||' \
  | LC_ALL=C sort || true
