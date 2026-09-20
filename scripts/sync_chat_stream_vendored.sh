#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Sync the chat processor sources into the chat_stream_processor package's
# vendored/ dir. These committed copies are required because SAM's makefile
# builder runs against an isolated copy of the function's CodeUri only — it
# cannot reach sibling Lambda directories at build time.
#
# Run this whenever chat_with_document_processor/index.py or
# agent_chat_processor/index.py changes. A unit test
# (src/lambda/chat_stream_processor/tests/test_vendored_in_sync.py) fails if they
# drift.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pkg="$repo_root/src/lambda/chat_stream_processor/vendored"
mkdir -p "$pkg"

cp "$repo_root/src/lambda/chat_with_document_processor/index.py" \
   "$pkg/chat_with_document_processor.py"
cp "$repo_root/src/lambda/agent_chat_processor/index.py" \
   "$pkg/agent_chat_processor.py"

echo "Synced vendored chat processor modules into $pkg"
