---
name: sync-pii-anonymizer
description: "Use when re-syncing the vendored copy of awslabs/pii-anonymizer under feature-platform/pii-anonymizer/hook/vendor/ after an upstream fix or feature."
---

# Skill: Re-sync the vendored pii-anonymizer library

**When to use:** Upstream [awslabs/pii-anonymizer](https://github.com/awslabs/pii-anonymizer)
has fixed a bug or added a feature you want in the PII Anonymization extension,
and you need to pull updated files into the vendored copy at
`feature-platform/pii-anonymizer/hook/vendor/pii_anonymizer/`.

This skill keeps the vendored **document** closure in sync WITHOUT dragging in
the parts we deliberately excluded (audio, handlers, infra, observability).

## Background

We vendor (copy) rather than submodule because upstream is an AWS Labs *sample*
with no PyPI release and no git tags — a submodule pinned to `main` is a moving
target, and the code must pass our lint + SRT security scan as owned code.

The vendored set, the excluded set, and the current pinned commit are documented
in `feature-platform/pii-anonymizer/hook/vendor/PROVENANCE.md`. **Read it first.**

## Procedure

1. **Clone upstream fresh** and capture the new HEAD SHA:
   ```bash
   rm -rf /tmp/pii-anonymizer-src
   git clone --depth 50 https://github.com/awslabs/pii-anonymizer.git /tmp/pii-anonymizer-src
   cd /tmp/pii-anonymizer-src && git rev-parse HEAD
   ```

2. **Diff what changed** since the pinned commit (from PROVENANCE.md) — scope the
   diff to the closure paths so you ignore churn in excluded modules:
   ```bash
   PINNED=<sha-from-PROVENANCE.md>
   git -C /tmp/pii-anonymizer-src diff --stat $PINNED..HEAD -- \
     src/core src/helpers src/processors src/redaction src/validation
   ```
   Review the diff. Pay attention to:
   - **New intra-project imports** in any closure file (`from core...`,
     `from helpers...`, `from validation...`, `from redaction...`,
     `from processors...`). A new import may pull in a module we don't yet
     vendor — the closure has grown. Add the new file(s) and re-check transitively.
   - Changes that reintroduce excluded deps (audio, xray, infra) — do NOT vendor those.
   - New external pip deps — update `hook/requirements.txt` / the layer.

3. **Re-copy the closure** (same file list as PROVENANCE.md). The helper script
   does exactly this and preserves the excluded-module exclusions:
   ```bash
   bash feature-platform/pii-anonymizer/hook/vendor/resync.sh /tmp/pii-anonymizer-src
   ```
   The script re-copies the documented closure, re-writes the empty
   `pii_anonymizer/__init__.py` marker, and re-copies LICENSE/NOTICE. It does
   NOT copy audio_processor, handlers/, infra/, observability, throttle_handler,
   log_scrubber, or core/redactor.

4. **Verify** the closure still imports cleanly and nothing excluded leaked in:
   ```bash
   cd feature-platform/pii-anonymizer/hook/vendor/pii_anonymizer
   grep -rnE "observability|throttle_handler|log_scrubber|audio_processor|from infra|import infra" . && echo "!! LEAK — investigate" || echo "no excluded refs"
   python3 - <<'PY'
   import sys, os; sys.path.insert(0, os.getcwd())
   for m in ["core.pii_detector","core.synthetic_pii_generator","helpers.model_router",
             "helpers.threaded_detector","processors.txt_processor","processors.pdf_text_processor",
             "processors.tabular_processor","processors.word_processor","processors.pdf_image_processor",
             "processors.image_processor","redaction.pdf_redactor","validation.model_schemas"]:
       __import__(m)
   print("ALL CLOSURE MODULES IMPORT OK")
   PY
   ```

5. **Update PROVENANCE.md**: bump the *Vendored commit*, *Commit date*, and
   *Vendored on* fields; note any files added to (or removed from) the closure;
   record any local modifications you had to re-apply.

6. **Quality gates**:
   ```bash
   make ruff-lint            # vendored code must pass our lint
   cd lib/idp_common_pkg && python -m pytest ../../feature-platform/pii-anonymizer/hook/tests -q
   make srt-scan             # security-scan the updated vendored code (see .claude/skills/srt-security-scan/SKILL.md)
   ```
   Triage any new SRT HIGH findings (mitigate with `# nosec` + rationale, a code
   fix, or a `scripts/srt/issues.json` suppression).

7. **Bump the feature version** in `feature.yaml` if the sync changes behavior,
   and add a CHANGELOG entry.

## Gotchas

- **Import root, not package root.** Submodules use absolute `from core...`
  imports; `pii_anonymizer/` goes on `sys.path`. Do NOT restore upstream's
  eager `src/__init__.py` re-exports — they pull in excluded modules.
- **Closure can grow.** A new upstream import inside a vendored file may
  reference a module we don't have yet. Step 2's import scan catches this;
  add the transitive file(s) and update PROVENANCE.md's file list.
- **Never vendor audio/ffmpeg.** Out of scope; it bloats the Lambda layer.
- **Config is a dict.** Upstream reads config via `config[...]`; the hook passes
  a plain dict built from the active config version's `preprocessing` block.
  A re-sync must not reintroduce a hard dependency on `config_loader` /
  CONFIG_BUCKET (it's a lazy fallback only — keep it that way).
