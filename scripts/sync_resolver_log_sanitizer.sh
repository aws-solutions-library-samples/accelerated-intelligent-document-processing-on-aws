#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Sync the canonical log redactor into the api-resolver Lambdas that need it.
#
# These committed copies are required because SAM packages each function from
# its own CodeUri directory only — it cannot reach a sibling Lambda directory,
# and most of these resolvers deliberately carry no Lambda layer, so they
# cannot import idp_common at runtime either. The module is stdlib-only
# (copy, re, typing), so a copy costs a few KB where a layer would cost tens
# of MB of Pillow/pypdfium2/requests.
#
# Run this whenever lib/idp_common_pkg/idp_common/utils/log_sanitizer.py
# changes. scripts/tests/test_resolver_log_sanitizer.py fails if they drift.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
canonical="$repo_root/lib/idp_common_pkg/idp_common/utils/log_sanitizer.py"
resolvers="$repo_root/nested/api-resolvers/src/lambda"

# The resolvers that carry NO idp-common Lambda layer, and so cannot import the
# module from the library. Resolvers that do carry the layer (including
# get_stepfunction_execution_resolver and test_runner) import
# idp_common.utils.log_sanitizer directly and must NOT be listed here.
# scripts/tests/test_resolver_log_sanitizer.py derives the same split from
# nested/api-resolvers/template.yaml, so it fails if this list drifts.
targets=(
  agent_chat_resolver
  agent_request_handler
  create_document_resolver
  delete_agent_chat_session_resolver
  get_agent_chat_messages_resolver
  get_file_contents_resolver
  list_agent_chat_sessions_resolver
  list_documents_range_resolver
  upload_resolver
)

for target in "${targets[@]}"; do
  dest="$resolvers/$target"
  if [[ ! -d "$dest" ]]; then
    echo "ERROR: no such resolver directory: $dest" >&2
    exit 1
  fi
  cp "$canonical" "$dest/log_sanitizer.py"
done

echo "Synced log_sanitizer.py into ${#targets[@]} resolver directories under $resolvers"
