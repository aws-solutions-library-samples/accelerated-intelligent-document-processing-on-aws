#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Sync the canonical log redactor into every Lambda that vendors a copy of it.
#
# These committed copies are required because SAM packages each function from
# its own CodeUri directory only — it cannot reach a sibling Lambda directory,
# and the functions listed below deliberately carry no Lambda layer, so they
# cannot import idp_common at runtime either. The module is stdlib-only
# (copy, re, typing), so a copy costs a few KB where a layer would cost tens
# of MB of Pillow/pypdfium2/requests.
#
# Run this whenever lib/idp_common_pkg/idp_common/utils/log_sanitizer.py
# changes. scripts/tests/test_resolver_log_sanitizer.py fails if they drift.
#
# The destinations are DERIVED from the handler sources rather than listed here.
# This script used to carry a hand-written list of nine resolver directory names,
# and a second copy of the same fact then had to be asserted by the test suite to
# stop the two drifting. Once the scan that finds unsanitized event logging was
# widened from the resolver tree to src/lambda as well, that list would have had
# to grow to thirty entries — a hand-maintained inventory of a fact that is
# already written unambiguously in the code, namely which handlers say
# "from log_sanitizer import ...". So the list is gone: a directory is a target
# because something in it imports the vendored module. Adding a target now means
# writing the import and re-running this script, with nothing to keep in step.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
canonical="$repo_root/lib/idp_common_pkg/idp_common/utils/log_sanitizer.py"

# The Lambda source trees scanned for importers. Kept as an explicit list because
# it is the one thing that cannot be derived from the imports themselves, and
# scripts/tests/test_resolver_log_sanitizer.py asserts that these are exactly the
# roots that guard scans — a root added to one and not the other would leave a
# whole tree unsynced or unchecked.
roots=(
  "nested/api-resolvers/src/lambda"
  "src/lambda"
)

if [[ ! -f "$canonical" ]]; then
  echo "ERROR: canonical module not found: $canonical" >&2
  exit 1
fi

# A top-level import of the sibling module, anchored to the start of the line so
# that the canonical module's own `Usage::` docstring — which quotes the
# `idp_common.utils.log_sanitizer` path, not this one — cannot match, and neither
# can a commented-out import.
import_re='^[[:space:]]*(from log_sanitizer import|import log_sanitizer([[:space:]]|$))'

targets=()
for root in "${roots[@]}"; do
  if [[ ! -d "$repo_root/$root" ]]; then
    echo "ERROR: no such Lambda source root: $repo_root/$root" >&2
    exit 1
  fi
  while IFS= read -r file; do
    targets+=("$(dirname "$file")")
  done < <(grep -rlE "$import_re" --include='*.py' "$repo_root/$root" | sort -u)
done

# One handler directory can hold several importing files (index.py plus its
# tests), so collapse to unique directories.
mapfile -t targets < <(printf '%s\n' "${targets[@]}" | sort -u)

if [[ ${#targets[@]} -eq 0 ]]; then
  echo "ERROR: no handler imports the vendored log_sanitizer module; refusing to" \
       "report success, since that is far more likely to mean this script's" \
       "detection broke than that every copy became unnecessary." >&2
  exit 1
fi

for target in "${targets[@]}"; do
  cp "$canonical" "$target/log_sanitizer.py"
  echo "  synced ${target#"$repo_root/"}/log_sanitizer.py"
done

echo "Synced log_sanitizer.py into ${#targets[@]} Lambda directories"
