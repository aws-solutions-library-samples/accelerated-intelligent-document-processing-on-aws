# WS-05 forced tool use — it works, and it changes nothing measurable

**Run:** 2026-09-03 20:09–20:36Z, stack `IDP1` v0.6.7.dev8 (quiesced; verified
`UPDATE_COMPLETE` at start and re-checked before every launch), extraction model
`us.anthropic.claude-sonnet-5`, 3 repeats. **Costs** are estimates from
`config_library/pricing.yaml`, rates as of 2026-09-02.

This is the first valid measurement of this feature. Two earlier attempts measured
defective code — see *Why the earlier numbers were void* below.

## Result

`bank_statement` (`valuenoise_100`, `longdesc_100` — 12 runs):

| arm | n | failures | completeness recall | cell accuracy | honored rate | cost |
|---|---|---|---|---|---|---|
| `force-off` | 6 | 0 | 1.0 (every run) | 1.0 | — | $0.259 |
| `force-on` | 6 | 0 | **1.0 (every run)** | 1.0 | **1.0** | $0.226 |

`kv_form` (6 runs): both arms `typed_accuracy` 1.0, 0 failures; cost $0.0434 (off) vs
$0.0519 (on).

**Forcing is honored on every single run and makes no measurable difference to
accuracy or completeness.** That is the null hypothesis `forced_tool.py`'s own module
docstring set out, now measured rather than assumed.

The honored rate is what makes this readable. Without it, "forcing had no effect" and
"forcing silently fell back to the prompt" are the same observation. It is 1.0 in all
nine `force-on` runs, so the arm genuinely engaged.

**Cost is not resolvable.** It moves in opposite directions on the two classes (−13% on
`bank_statement`, +20% on `kv_form`) at n=3–6. Nothing to claim.

## Guidance

**Leave `extraction.forced_tool.enabled: false`.** It is not broken and not harmful — it
simply buys nothing on this corpus. What it does buy in principle is unchanged: a
malformed-JSON parse failure becomes structurally impossible for the fields the schema
declares. If your corpus produces parse failures, that may be worth having; measure it
there, and watch `metadata.forced_tool.honored`.

## Why the earlier numbers were void

Both prior attempts measured bugs, and both bugs are now fixed (PR #744):

1. **`$id` rejected outright.** IDP class schemas set `$id` to the document-class name,
   so `"Bank Statement"` is not an RFC 3986 URI-reference and Bedrock's tool-schema
   meta-validation rejected the request. Every section failed. Verified afterwards that
   this is **not** model-dependent — `sonnet-5` and `sonnet-4-6` both reject it.
2. **A wrapper key silently discarded everything.** Sonnet 5 answered with
   `{"fields": {...the whole extraction...}}`; `fields` is not a declared property, so
   off-schema-key handling dropped it and with it a 100-row list. The section reported
   `parsing_succeeded: true`, `honored: true`, no error, and COMPLETED — recall 0.0 on
   three of three runs. Cued, most likely, by the tool being named
   `emit_extracted_fields` and asking for "the fields".

This run is the live confirmation of fix 2: the same cell, same model, same documents
went from **recall 0.167 → 1.0**.

⚠️ **Not a verdict on tool use generally.** Advanced (agentic) extraction has always used
tool-based structured output and measured completeness recall 1.0 across all 12 runs of
the `restatement` A/B on this stack.

## Reproducing

```bash
python3 benchmarks/harness/make_configs.py --suite forcing --class bank_statement \
    --set extraction_model=sonnet5
AWS_PROFILE=default python3 benchmarks/harness/run_matrix.py --stack <STACK> \
    --suite forcing --class bank_statement --set extraction_model=sonnet5 --max-inflight 20
# kv_form is a different document class:
#   ... --suite forcing --class kv_form --set extraction_model=sonnet5
```
