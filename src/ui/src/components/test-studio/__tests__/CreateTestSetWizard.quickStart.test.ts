// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The configuration prerequisite's hint names Quick Start only to a user who is
 * offered it. Quick Start is for Admins, and an Author can open this wizard, so a
 * hint naming it for everyone would send an Author to a button they do not have.
 *
 * Asserted at source level, as the sibling wizard tests are: rendering the wizard
 * needs the GraphQL client, settings, role hooks and a file input.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const WIZARD = readFileSync(join(__dirname, '..', 'CreateTestSetWizard.tsx'), 'utf-8');

const defaultMessage = (key: string): string => {
  const start = WIZARD.indexOf(`  ${key}: {`);
  const body = WIZARD.slice(start, WIZARD.indexOf('  },', start));
  return /defaultMessage:\s*'([^']*)'/.exec(body)?.[1] ?? '';
};

describe('the configuration hint and Quick Start', () => {
  it('chooses the Quick Start wording only when Quick Start is offered', () => {
    expect(WIZARD).toMatch(/const quickStartAvailable = useQuickStartAvailable\(\);/);
    expect(WIZARD).toMatch(/quickStartAvailable \? messages\.noConfigurationWithQuickStart : messages\.noConfiguration\b/);
  });

  it('names Quick Start in one message only', () => {
    expect(defaultMessage('noConfigurationWithQuickStart')).toMatch(/open Quick Start from the side navigation/);
    expect(defaultMessage('noConfiguration')).not.toMatch(/Quick Start/);
    expect(defaultMessage('noConfiguration')).toMatch(/<link>Discovery<\/link>/);
  });
});
