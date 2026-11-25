#!/usr/bin/env bash
# Re-copy the vendored pii-anonymizer DOCUMENT closure from a fresh upstream
# clone, preserving the deliberate exclusions (audio, handlers, infra,
# observability, throttle_handler, log_scrubber, core/redactor).
#
# Usage: bash resync.sh /path/to/pii-anonymizer-clone
# Driven by .claude/skills/sync-pii-anonymizer/SKILL.md — read that first.
# The authoritative file list lives in PROVENANCE.md (this script must match it).
set -euo pipefail

UPSTREAM="${1:?usage: resync.sh <upstream-clone-dir>}"
SRC="$UPSTREAM/src"
HERE="$(cd "$(dirname "$0")" && pwd)"
V="$HERE/pii_anonymizer"

[ -d "$SRC" ] || { echo "!! $SRC not found — is $UPSTREAM a pii-anonymizer clone?"; exit 1; }

echo "Re-copying document closure from $SRC ..."
mkdir -p "$V"/core "$V"/helpers "$V"/processors "$V"/redaction "$V"/validation

for pkg in core helpers processors redaction validation; do
  cp "$SRC/$pkg/__init__.py" "$V/$pkg/__init__.py" 2>/dev/null || touch "$V/$pkg/__init__.py"
done

cp "$SRC"/core/{pii_detector,synthetic_pii_generator,text_replacer,prompts,value_categorizer}.py "$V"/core/
cp "$SRC"/helpers/{model_config_helper,model_router,threaded_detector,text_chunker,token_tracker,pdf_processor,textract_helper,page_type_checker,font_config,config_loader}.py "$V"/helpers/
cp "$SRC"/helpers/pricing.yaml "$V"/helpers/ 2>/dev/null || echo "  (no pricing.yaml upstream — cost estimation disabled, harmless)"
cp "$SRC"/processors/{pdf_text_processor,txt_processor,tabular_processor,word_processor,pdf_image_processor,image_processor}.py "$V"/processors/
cp "$SRC"/redaction/pdf_redactor.py "$V"/redaction/
cp "$SRC"/validation/{model_schemas,pdf_validator,document_validator}.py "$V"/validation/
cp "$UPSTREAM"/LICENSE "$HERE"/ 2>/dev/null || true
cp "$UPSTREAM"/NOTICE  "$HERE"/ 2>/dev/null || true

# Replace the eager upstream package root with an empty marker (submodules use
# absolute `from core...` imports; pii_anonymizer/ goes on sys.path).
cat > "$V/__init__.py" <<'PY'
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Vendored subset of AWS Labs pii-anonymizer (see ../PROVENANCE.md).
# Intentionally empty: the vendored submodules import each other with absolute
# names rooted here, so this directory is placed on sys.path. Do NOT restore
# upstream's eager re-exports — they pull in un-vendored modules.
PY

echo "Done. Now: (1) scan for excluded-module leaks, (2) run the import smoke"
echo "test, (3) update PROVENANCE.md's commit SHA/date, (4) lint + tests + SRT."
echo "See .claude/skills/sync-pii-anonymizer/SKILL.md steps 4-7."
