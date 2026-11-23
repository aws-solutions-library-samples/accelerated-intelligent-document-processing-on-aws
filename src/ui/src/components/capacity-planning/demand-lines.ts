// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Inputs for the capacity planner's Evaluation and Escalation demand lines (#1953).
 *
 * Each line is charged to the model that serves it, so the planner is told the
 * judge model only while evaluation is enabled and the escalation model only while
 * validation failures escalate. The evaluation line's request count when no
 * evaluation metering exists yet is the number of LLM comparisons a document of the
 * class implies, counted here from the class schema.
 */

type Json = Record<string, unknown>;

const EVALUATION_METHOD = 'x-aws-idp-evaluation-method';
const LLM_IN_LIST = 'x-aws-idp-evaluation-allow-llm-in-list';

/** The YAML-tolerant reading of a boolean the evaluation mapper uses (`_coerce_bool`). */
function coerceBool(value: unknown): boolean {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'string') return !['', 'false', 'no', 'off', '0', 'n'].includes(value.trim().toLowerCase());
  return Boolean(value);
}

function asObject(value: unknown): Json | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Json) : null;
}

function resolve(node: Json, root: Json): Json {
  const ref = node.$ref;
  if (typeof ref !== 'string' || !ref.startsWith('#/$defs/')) return node;
  const target = asObject(asObject(root.$defs)?.[ref.slice('#/$defs/'.length)]);
  if (!target) return node;
  const { $ref: _ignored, ...rest } = node;
  return { ...target, ...rest };
}

function isObjectSchema(node: Json): boolean {
  return node.type === 'object' || asObject(node.properties) !== null;
}

function count(node: Json, root: Json, inListItems: boolean, depth: number): number {
  if (depth > 32) return 0; // a $ref cycle; real schemas are nowhere near this deep
  const schema = resolve(node, root);
  if (isObjectSchema(schema)) {
    // An object-level LLM method makes no judge call of its own: its fields are
    // compared through their own methods.
    return Object.values(asObject(schema.properties) ?? {}).reduce<number>((sum, child) => {
      const childSchema = asObject(child);
      return sum + (childSchema ? count(childSchema, root, inListItems, depth + 1) : 0);
    }, 0);
  }
  if (schema.type === 'array') {
    const items = asObject(schema.items);
    const itemSchema = items ? resolve(items, root) : null;
    if (itemSchema && isObjectSchema(itemSchema)) {
      // A method on a structured list is ignored; its item fields carry their own.
      return count(itemSchema, root, true, depth + 1);
    }
    // A list of plain values compared by the judge costs one call per pair of items;
    // one is the floor, for a single item on each side.
    return schema[EVALUATION_METHOD] === 'LLM' ? 1 : 0;
  }
  if (schema[EVALUATION_METHOD] !== 'LLM') return 0;
  // Inside a structured list the LLM method is downgraded to a deterministic
  // comparator unless the field opts in; an opted-in field costs one call per pair
  // of rows, so one is again the floor.
  if (inListItems && !coerceBool(schema[LLM_IN_LIST])) return 0;
  return 1;
}

/**
 * Judge requests one document of this class makes when every LLM-compared value
 * differs from its baseline (identical values are settled without a call): one per
 * LLM-compared field, and at least one per list field that is LLM-compared, which
 * grows with the square of the list's length.
 */
export function countLlmComparisons(classSchema: unknown): number {
  const root = asObject(classSchema);
  return root ? count(root, root, false, 0) : 0;
}

interface DemandConfiguration {
  extraction?: {
    model?: string;
    validation?: { enabled?: boolean; fail_action?: string; escalation_model?: string | null };
  };
  evaluation?: { enabled?: boolean; llm_method?: { model?: string } };
}

/** The evaluation judge model, or undefined while evaluation is disabled. */
export function evaluationModelOf(configuration: DemandConfiguration | null | undefined): string | undefined {
  const evaluation = configuration?.evaluation;
  if (!evaluation || evaluation.enabled === false) return undefined;
  return evaluation.llm_method?.model || undefined;
}

/**
 * The model that re-extracts after a validation failure, or undefined unless
 * validation is on and `fail_action` is `escalate`. With no escalation model set,
 * escalation retries on the extraction model, so that is the model charged.
 */
export function escalationModelOf(configuration: DemandConfiguration | null | undefined): string | undefined {
  const extraction = configuration?.extraction;
  const validation = extraction?.validation;
  if (!validation || validation.enabled === false || validation.fail_action !== 'escalate') return undefined;
  return validation.escalation_model || extraction?.model || undefined;
}
