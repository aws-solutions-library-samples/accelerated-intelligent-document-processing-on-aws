// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The `routing` section of a configuration profile, as the Routing Rules tab edits it, and
// the catalogue of what a condition can test. routing-inputs.json is generated from
// ROUTING_INPUTS in idp_common/config/routing_validation.py by
// scripts/generate_run_step_catalog.py (never edited by hand). The server validates a
// saved section again; nothing here decides what is valid.
import catalog from './routing-inputs.json';

/** The one choice an outcome offers in the editor: a review queue, or no review. */
export type OutcomeKind = 'queue' | 'no_review';

export interface RoutingCondition {
  input: string;
  path: string;
  aggregate: string;
  test: string;
}

/** One thing that happens to a document a routing rule matches. */
export interface RoutingAction {
  /** `review_queue` is the only type the server accepts. */
  type: string;
  /** The review queue id, for a `review_queue` action. */
  queue: string;
}

export interface RoutingRule {
  id: string;
  label: string;
  enabled: boolean;
  conditions: RoutingCondition[];
  /** What happens to a matching document, in order; none means it is not reviewed. */
  actions: RoutingAction[];
  /** What the reviewer reads; `{placeholders}` take the document's values. */
  reason: string;
}

/** What happens to a document no routing rule matches. */
export interface RoutingDefaultOutcome {
  actions: RoutingAction[];
  reason: string;
}

export interface RoutingTable {
  id: string;
  stage: string;
  hit_policy: string;
  engine: string;
  rules: RoutingRule[];
  default_outcome: RoutingDefaultOutcome;
}

export interface RoutingConfig {
  tables: RoutingTable[];
  max_consecutive_placements: number;
}

/** What an input's path names: a field of a class, a policy rule id, a policy type, or a class. */
export type PathKind = '' | 'field' | 'policy_rule' | 'policy_type' | 'class';

export interface RoutingInput {
  name: string;
  /** string, number, boolean, or field (the type of the field the path names). */
  type: string;
  needs_path: boolean;
  is_list: boolean;
  path_kind: PathKind;
  path_optional: boolean;
  /** The values a guided control offers; empty when the value is free. */
  choices: string[];
}

/** Every routing input, in the order the rule editor offers them. */
export const ROUTING_INPUTS: readonly RoutingInput[] = catalog.inputs.map((entry) => ({
  name: entry.name,
  type: entry.type,
  needs_path: entry.needs_path,
  is_list: entry.is_list,
  path_kind: entry.path_kind as PathKind,
  path_optional: entry.path_optional,
  choices: entry.choices,
}));

export const AGGREGATES: readonly string[] = catalog.aggregates;
export const DEFAULT_AGGREGATE = 'any';
export const MAX_RULES: number = catalog.limits.max_rules;
export const MAX_CONDITIONS: number = catalog.limits.max_conditions;
/** The most bytes a reason holds, and the most placeholders. */
export const MAX_REASON_BYTES: number = catalog.limits.max_reason_bytes;
export const MAX_REASON_PLACEHOLDERS: number = catalog.limits.max_reason_placeholders;
export const RULE_ID_PATTERN = new RegExp(catalog.id_pattern);

export const DEFAULT_QUEUE_ID = 'default';
export const ACTION_REVIEW_QUEUE = 'review_queue';
/** The reason placeholder that reads the profile's confidence threshold. */
export const CONFIDENCE_THRESHOLD = 'confidence_threshold';
export const BUILT_IN_PREFIX = 'builtin-';
export const MIN_PLACEMENTS = 1;
export const MAX_PLACEMENTS = 10;

export const routingInput = (name: string): RoutingInput | undefined => ROUTING_INPUTS.find((input) => input.name === name);

export const isBuiltIn = (rule: Pick<RoutingRule, 'id'>): boolean => rule.id.startsWith(BUILT_IN_PREFIX);

/** The queue of the first `review_queue` action; empty when there is none (no review). */
export const queueOf = (actions: readonly RoutingAction[]): string =>
  actions.find((action) => action.type === ACTION_REVIEW_QUEUE)?.queue ?? '';

/** The actions that send a document to a queue; none for an empty queue (no review). */
export const withQueue = (queue: string): RoutingAction[] => (queue ? [{ type: ACTION_REVIEW_QUEUE, queue }] : []);

/** True for an input that reads a policy rule result. */
export const policyInput = (name: string): boolean => {
  const kind = routingInput(name)?.path_kind;
  return kind === 'policy_rule' || kind === 'policy_type';
};

/**
 * The placeholders of a reason, without their braces, in the order written. `{{` and
 * `}}` are literal braces. The server checks the grammar; this only finds what is there.
 */
export const reasonPlaceholders = (reason: string): string[] =>
  [...reason.matchAll(/\{\{|\}\}|\{([^{}]*)\}/g)].flatMap((match) => (match[1] === undefined ? [] : [match[1].trim()]));

/** The routing input a placeholder reads; none for a condition's value and the confidence threshold. */
export const placeholderInput = (placeholder: string): string | undefined =>
  placeholder === CONFIDENCE_THRESHOLD || /^condition_[1-9][0-9]?\.value$/.test(placeholder) ? undefined : placeholder.split(':')[0].trim();

/** True when a condition of the routing rule, or a placeholder of its reason, reads a policy rule result. */
export const readsPolicyResult = (rule: Pick<RoutingRule, 'conditions' | 'reason'>): boolean =>
  rule.conditions.some((condition) => policyInput(condition.input)) ||
  reasonPlaceholders(rule.reason).some((placeholder) => policyInput(placeholderInput(placeholder) ?? ''));

const asRecord = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};

// Every key of an action is kept, and its type is kept as read, so an action this editor
// does not know is saved as it was and the server says what is wrong with it.
const asActions = (value: unknown): RoutingAction[] =>
  (Array.isArray(value) ? value : []).map((entry) => {
    const action = asRecord(entry);
    return {
      ...action,
      type: typeof action.type === 'string' ? action.type : ACTION_REVIEW_QUEUE,
      queue: typeof action.queue === 'string' ? action.queue : '',
    };
  });

const asDefaultOutcome = (value: unknown): RoutingDefaultOutcome => {
  const outcome = asRecord(value);
  return { actions: asActions(outcome.actions), reason: typeof outcome.reason === 'string' ? outcome.reason : '' };
};

const asRule = (value: unknown): RoutingRule => {
  const rule = asRecord(value);
  return {
    id: typeof rule.id === 'string' ? rule.id : '',
    label: typeof rule.label === 'string' ? rule.label : '',
    enabled: rule.enabled !== false,
    conditions: (Array.isArray(rule.conditions) ? rule.conditions : []).map((entry) => {
      const condition = asRecord(entry);
      return {
        input: typeof condition.input === 'string' ? condition.input : '',
        path: typeof condition.path === 'string' ? condition.path : '',
        aggregate: typeof condition.aggregate === 'string' && condition.aggregate ? condition.aggregate : DEFAULT_AGGREGATE,
        test: typeof condition.test === 'string' ? condition.test : '',
      };
    }),
    actions: asActions(rule.actions),
    reason: typeof rule.reason === 'string' ? rule.reason : '',
  };
};

/**
 * The routing section of a configuration as the tab's value. A profile holds exactly one
 * table; a configuration with none (or with something that is not a section) reads as
 * one empty table, which is what the tab then saves.
 */
export const readRouting = (value: unknown): RoutingConfig => {
  const section = asRecord(value);
  const table = asRecord(Array.isArray(section.tables) ? section.tables[0] : undefined);
  const placements = Number(section.max_consecutive_placements);
  return {
    tables: [
      {
        id: typeof table.id === 'string' && table.id ? table.id : 'review-routing',
        stage: typeof table.stage === 'string' && table.stage ? table.stage : 'after_policy_rule_validation',
        hit_policy: typeof table.hit_policy === 'string' && table.hit_policy ? table.hit_policy : 'FIRST',
        engine: typeof table.engine === 'string' && table.engine ? table.engine : 'builtin',
        rules: (Array.isArray(table.rules) ? table.rules : []).map(asRule),
        default_outcome: asDefaultOutcome(table.default_outcome),
      },
    ],
    max_consecutive_placements: Number.isInteger(placements) && placements >= MIN_PLACEMENTS ? placements : 3,
  };
};

/** The one table of a routing section. */
export const tableOf = (routing: RoutingConfig): RoutingTable => routing.tables[0];

/** The section with its table's routing rules replaced. */
export const withRules = (routing: RoutingConfig, rules: RoutingRule[]): RoutingConfig => ({
  ...routing,
  tables: [{ ...tableOf(routing), rules }],
});

/** The built-in routing rules of the system defaults that a table does not have, in their order. */
export const missingBuiltInRules = (routing: RoutingConfig, defaults: unknown): RoutingRule[] => {
  const present = new Set(tableOf(routing).rules.map((rule) => rule.id));
  return tableOf(readRouting(defaults)).rules.filter((rule) => isBuiltIn(rule) && !present.has(rule.id));
};

/**
 * What the server refused about the routing table, out of the error a failed save
 * reports: its message for a routing table starts by naming the table. `null` for any
 * other error, and for none.
 */
export const routingRefusal = (saveError: string | null | undefined): string | null => {
  const message = (saveError ?? '').replace(/^Error:\s*/, '');
  return message.startsWith("Routing table '") ? message : null;
};

/** The id of the rule a refusal names, when it names one. */
export const refusedRuleId = (refusal: string | null | undefined): string | undefined =>
  /^Routing table '[^']*', rule '([^']+)'/.exec(refusal ?? '')?.[1];
