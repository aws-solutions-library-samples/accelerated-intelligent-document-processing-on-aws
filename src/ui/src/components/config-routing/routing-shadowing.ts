// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Rules that can never match. The table is first-hit: a document takes the outcome of the
// first enabled rule whose conditions all hold. So a rule is unreachable when an earlier
// enabled rule asks for a subset of what it asks for: whenever the later rule's conditions
// hold, the earlier one's do too, and it wins. Conditions are compared as written (input,
// path, aggregate and test text), which finds the plain cases and never a false one.
import type { RoutingCondition, RoutingRule } from './routing-types';

export interface ShadowedRule {
  /** The rule that can never match. */
  ruleId: string;
  /** The earlier rule that always matches first. */
  shadowedBy: string;
}

const conditionKey = (condition: RoutingCondition): string =>
  JSON.stringify([condition.input, condition.path.trim(), condition.aggregate, condition.test.trim()]);

/**
 * Each rule that an earlier enabled rule shadows, with the first such rule. A disabled
 * rule shadows nothing and is not reported: it does not run. A rule with no conditions
 * matches every document, so it shadows every rule after it.
 */
export const shadowedRules = (rules: readonly RoutingRule[]): ShadowedRule[] => {
  const shadowed: ShadowedRule[] = [];
  const earlier: { id: string; keys: string[] }[] = [];
  rules.forEach((rule) => {
    if (!rule.enabled) return;
    const keys = new Set(rule.conditions.map(conditionKey));
    const winner = earlier.find((candidate) => candidate.keys.every((key) => keys.has(key)));
    if (winner) shadowed.push({ ruleId: rule.id, shadowedBy: winner.id });
    earlier.push({ id: rule.id, keys: [...keys] });
  });
  return shadowed;
};
