// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Whether a document's configuration processes it with Bedrock Data Automation.
 *
 * Read from the document's OWN configuration profile, never from the stack's
 * `IDPPattern` setting: every stack deploys the one unified pattern, so that
 * setting names no processing mode and reads the same for a BDA document as for a
 * pipeline one. `use_bda` is the flag the workflow branches on.
 *
 * Why this decides what the editor offers: a BDA reprocess keeps the document's
 * existing sections when it has any, so a structural or text edit — reclassify,
 * split or merge sections, regroup pages, change a page's text — clears a section's
 * values and nothing regenerates them. Field-value edits are unaffected and stay
 * available.
 *
 * The flag can arrive as a string after a YAML or DynamoDB round-trip, where
 * `"false"` is truthy, so it is read with Pydantic's spellings (the backend's
 * `flag_is_true`). An unrecognised value, or no configuration yet, reads as
 * pipeline mode.
 */
const TRUE_SPELLINGS = ['true', 'yes', 'on', '1', 't', 'y'];

export const isBdaModeConfig = (config: Record<string, unknown> | null | undefined): boolean => {
  const raw = config?.use_bda;
  if (typeof raw === 'boolean') return raw;
  if (typeof raw === 'number') return raw !== 0;
  if (typeof raw === 'string') return TRUE_SPELLINGS.includes(raw.trim().toLowerCase());
  return false;
};
