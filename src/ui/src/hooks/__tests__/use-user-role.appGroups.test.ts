// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * `APP_GROUPS` must be every group the stack creates.
 *
 * It is no longer only a filter for the federated-refresh heuristic. `hasNoRole` is
 * computed from it and gates the **entire application**, so a group omitted here is
 * a lockout rather than a missing convenience flag — and the lockout is invisible
 * from the server's side, because the dispatcher's required-groups manifest is
 * generated from `template.yaml` and would have granted that group all eleven
 * `ANY_GROUP` operations. Its members would be told "your account has not been
 * granted access yet" while holding a role that works, which is strictly worse than
 * the fall-through to the Viewer navigation that preceded the flag.
 *
 * Adding a Cognito group is a supported extension (see "Adding New Roles" in
 * docs/rbac.md), so this reads the template rather than restating the five names: a
 * sixth group fails here, which is the whole point. The backend equivalent is
 * `generate_api_rbac_manifest.cognito_group_names`, and this matches its extraction
 * deliberately — one fact, read from one place, checked in both languages.
 */

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { APP_GROUPS } from '../use-user-role';

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../../../../..');

/** The literal `GroupName` of every `AWS::Cognito::UserPoolGroup` in the template. */
const templateGroupNames = (): string[] => {
  const text = readFileSync(resolve(repoRoot, 'template.yaml'), 'utf8');
  const names: string[] = [];
  const marker = /Type:\s*AWS::Cognito::UserPoolGroup\b/g;
  let match = marker.exec(text);
  while (match !== null) {
    // The first literal GroupName after the resource's Type line. A `!Ref` value
    // belongs to a group ATTACHMENT, not a group, and does not match.
    const after = text.slice(match.index);
    const group = /^\s*GroupName:\s*([A-Za-z]\w*)\s*$/m.exec(after);
    if (group) names.push(group[1]);
    match = marker.exec(text);
  }
  return names.sort();
};

describe('APP_GROUPS', () => {
  it('lists exactly the Cognito groups template.yaml creates', () => {
    const fromTemplate = templateGroupNames();
    // Floor check: a broken regex returning nothing would make the comparison pass
    // vacuously the moment APP_GROUPS were also emptied.
    expect(fromTemplate.length).toBeGreaterThanOrEqual(5);
    expect([...APP_GROUPS].sort()).toEqual(fromTemplate);
  });
});
