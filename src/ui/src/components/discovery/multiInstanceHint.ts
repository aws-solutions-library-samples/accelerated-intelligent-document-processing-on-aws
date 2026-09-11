// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// #765: Discovery returns a *suggestion* when the analyzed sample appeared to hold
// several records of the discovered class. The user decides; applying it sets
// x-aws-idp-multi-instance on the class through the ordinary configuration update.

import { X_AWS_IDP_INSTANCE_ARRAY, X_AWS_IDP_MULTI_INSTANCE } from '../../constants/schemaConstants';

export interface MultiInstanceHint {
  instance_count: number;
  class_name: string;
  already_multi_instance?: boolean;
  message?: string;
}

export function parseMultiInstanceHint(raw?: string | null): MultiInstanceHint | null {
  if (!raw) return null;
  try {
    const parsed = typeof raw === 'string' ? JSON.parse(raw) : raw;
    if (!parsed || typeof parsed !== 'object') return null;
    const count = Number((parsed as Record<string, unknown>).instance_count);
    const className = (parsed as Record<string, unknown>).class_name;
    if (!Number.isFinite(count) || count < 2 || typeof className !== 'string' || !className) return null;
    return parsed as MultiInstanceHint;
  } catch {
    return null;
  }
}

export interface ApplyResult {
  classes: Record<string, unknown>[];
  found: boolean;
  changed: boolean;
}

const classId = (c: Record<string, unknown>): string | undefined =>
  (c.$id as string | undefined) ?? (c['x-aws-idp-document-type'] as string | undefined);

// Return a new classes list with `Documents per section: Several` enabled on the
// named class (mirrors SchemaInspector's Synthesize mode: the flag on, any
// instance-array designation cleared). Never touches any other class.
export function applyMultiInstance(classes: unknown[], className: string): ApplyResult {
  let found = false;
  let changed = false;
  const next = (classes as Record<string, unknown>[]).map((c) => {
    if (!c || typeof c !== 'object' || classId(c) !== className) return c;
    found = true;
    if (c[X_AWS_IDP_MULTI_INSTANCE] === true && c[X_AWS_IDP_INSTANCE_ARRAY] === undefined) return c;
    changed = true;
    const copy: Record<string, unknown> = { ...c, [X_AWS_IDP_MULTI_INSTANCE]: true };
    delete copy[X_AWS_IDP_INSTANCE_ARRAY];
    return copy;
  });
  return { classes: next, found, changed };
}
