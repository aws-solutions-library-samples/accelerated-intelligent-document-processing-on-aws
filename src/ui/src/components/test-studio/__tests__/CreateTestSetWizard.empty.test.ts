// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * "Start empty" as a way to create a test set.
 *
 * A set can now be grown from its own page, so creation no longer has to bring
 * documents with it. The wizard offers the source, sends only the fields an empty
 * set has (name, description, class type), and does not show the review rows or
 * the configuration prerequisite that belong to sources which bring documents.
 *
 * Asserted at source level, as the sibling wizard test is: rendering the wizard
 * needs the GraphQL client, settings, role hooks and a file input.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { CREATE_SOURCES } from '../testSetOptions';

const HERE = join(__dirname, '..');
const WIZARD = readFileSync(join(HERE, 'CreateTestSetWizard.tsx'), 'utf-8');
const ROOT = join(HERE, '..', '..', '..', '..', '..');
const SCHEMA = readFileSync(join(ROOT, 'nested', 'api-resolvers', 'src', 'api', 'schema.graphql'), 'utf-8');
const OP = readFileSync(join(HERE, '..', '..', 'graphql', 'operations', 'mutations', 'CreateEmptyTestSet.graphql'), 'utf-8');

describe('the "Start empty" source', () => {
  it('is offered, and says where the documents come from later', () => {
    const empty = CREATE_SOURCES.find((s) => s.value === 'empty');
    expect(empty?.label).toBe('Start empty');
    expect(empty?.description).toMatch(/files in a bucket, a zip, or generated documents/);
  });

  it('creates through its own mutation, with nothing but the set’s own fields', () => {
    expect(WIZARD).toMatch(/if \(isEmpty\) await submitEmpty\(\);/);
    const submitEmpty = WIZARD.slice(WIZARD.indexOf('const submitEmpty = async'), WIZARD.indexOf('const handleSubmit = async'));
    expect(submitEmpty).toMatch(/query: createEmptyTestSet/);
    expect(submitEmpty).not.toMatch(/filePattern|bucketType|fileCount/);
    expect(OP).toMatch(/createEmptyTestSet\(name: \$name, description: \$description, documentClassType: \$documentClassType\)/);
    expect(SCHEMA).toMatch(/createEmptyTestSet\(name: String!, description: String, documentClassType: DocumentClassType\): TestSet/);
  });

  it('skips the configuration prerequisite and the bucket or zip review rows', () => {
    expect(WIZARD).toMatch(/source === 'upload-labeled' \|\| isEmpty \? null :/);
    expect(WIZARD).toMatch(/\.\.\.\(isEmpty\s*\?\s*\[\]/);
  });

  it('tells the user what to do next', () => {
    expect(WIZARD).toMatch(/with no documents yet\. Open it and use Add documents\./);
  });
});
