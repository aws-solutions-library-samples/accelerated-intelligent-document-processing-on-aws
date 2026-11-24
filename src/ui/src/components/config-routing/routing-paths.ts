// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// What a routing condition's path can name, read from the profile being edited: the
// fields of its document classes, the ids of its policy rules, its policy types, and
// its document classes. The server checks a saved path against the same profile.
import { X_AWS_IDP_DOCUMENT_TYPE, X_AWS_IDP_POLICY_TYPE } from '../../constants/schemaConstants';
import { getConfigClassOptions } from '../common/config-class-options';
import type { PathKind } from './routing-types';

/** The policy type key that older Policy Schemas use. */
const LEGACY_POLICY_TYPE = 'x-aws-idp-rule-type';

const isRecord = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);

const scalarFieldPaths = (schema: unknown, prefix: string, depth: number): string[] => {
  if (!isRecord(schema) || !isRecord(schema.properties) || depth > 4) return [];
  return Object.entries(schema.properties).flatMap(([name, field]) => {
    const path = prefix ? `${prefix}.${name}` : name;
    if (!isRecord(field)) return [];
    // A list needs a row index to read a field in it, which is written by hand.
    if (field.type === 'array') return [];
    if (field.type === 'object' || isRecord(field.properties)) return scalarFieldPaths(field, path, depth + 1);
    return [path];
  });
};

export interface ClassFieldPaths {
  className: string;
  /** `Class:field.path` for each scalar field of the class. */
  paths: string[];
}

/** The scalar fields of each document class, as `Class:field` paths. */
export const fieldPathsByClass = (classes: unknown): ClassFieldPaths[] =>
  (Array.isArray(classes) ? classes : []).flatMap((schema) => {
    if (!isRecord(schema)) return [];
    const className = schema[X_AWS_IDP_DOCUMENT_TYPE];
    if (typeof className !== 'string' || !className) return [];
    return [{ className, paths: scalarFieldPaths(schema, '', 0).map((path) => `${className}:${path}`) }];
  });

/**
 * The type of the field a `Class:field.path` names: its JSON Schema type, or `date` for
 * a string that holds a date. Undefined when the profile has no such scalar field (a
 * path written by hand, a row of a list), so the caller knows nothing about its values.
 */
export const fieldType = (classes: unknown, path: string): string | undefined => {
  const separator = path.indexOf(':');
  if (separator < 0) return undefined;
  const className = path.slice(0, separator);
  const schema = (Array.isArray(classes) ? classes : []).find(
    (candidate) => isRecord(candidate) && candidate[X_AWS_IDP_DOCUMENT_TYPE] === className,
  );
  let field: unknown = schema;
  for (const name of path.slice(separator + 1).split('.')) {
    field = isRecord(field) && isRecord(field.properties) ? field.properties[name] : undefined;
  }
  if (!isRecord(field)) return undefined;
  const types: unknown[] = Array.isArray(field.type) ? field.type : [field.type];
  const type = types.find((candidate) => typeof candidate === 'string' && candidate !== 'null');
  if (typeof type !== 'string') return undefined;
  return type === 'string' && (field.format === 'date' || field.format === 'date-time') ? 'date' : type;
};

/** The names of the document classes of the profile. An entry that is not a class schema has none. */
export const classNames = (classes: unknown): string[] =>
  getConfigClassOptions({ classes: Array.isArray(classes) ? classes.filter(isRecord) : [] }).map((option) => option.value);

const policyTypeOf = (policyClass: unknown): string | undefined => {
  if (!isRecord(policyClass)) return undefined;
  const policyType = policyClass[X_AWS_IDP_POLICY_TYPE] ?? policyClass[LEGACY_POLICY_TYPE];
  return typeof policyType === 'string' && policyType ? policyType : undefined;
};

/** Every policy type of the profile. */
export const policyTypes = (policyClasses: unknown): string[] =>
  (Array.isArray(policyClasses) ? policyClasses : []).flatMap((policyClass) => {
    const policyType = policyTypeOf(policyClass);
    return policyType ? [policyType] : [];
  });

/** Every policy rule id of the profile: `<policy type>.<rule key>`. */
export const policyRuleIds = (policyClasses: unknown): string[] =>
  (Array.isArray(policyClasses) ? policyClasses : []).flatMap((policyClass) => {
    const policyType = policyTypeOf(policyClass);
    const rules = isRecord(policyClass) ? policyClass.rule_properties : undefined;
    return policyType && isRecord(rules) ? Object.keys(rules).map((key) => `${policyType}.${key}`) : [];
  });

/** The paths a kind of input can name in this profile, flat. */
export const pathsFor = (kind: PathKind, classes: unknown, policyClasses: unknown): string[] => {
  if (kind === 'field') return fieldPathsByClass(classes).flatMap((entry) => entry.paths);
  if (kind === 'policy_rule') return policyRuleIds(policyClasses);
  if (kind === 'policy_type') return policyTypes(policyClasses);
  if (kind === 'class') return classNames(classes);
  return [];
};
