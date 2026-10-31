// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The class list is headed "Document Types", which is what the docs call it.
 *
 * The heading and the empty-state alert were composed from a plural noun
 * (`${'Documents'} Types`), so the builder rendered "Documents Types" and, on the
 * Policy Schema tab, "No Rules Types".
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { ComponentProps } from 'react';

import SchemaBuilder from '../SchemaBuilder';
import { X_AWS_IDP_DOCUMENT_TYPE } from '../../../constants/schemaConstants';

type BuilderSchema = NonNullable<ComponentProps<typeof SchemaBuilder>['initialSchema']>;

const oneClass = (isDocumentType: boolean): BuilderSchema =>
  [
    {
      id: 'class-payslip',
      name: 'Payslip',
      [X_AWS_IDP_DOCUMENT_TYPE]: isDocumentType,
      attributes: { type: 'object', properties: { PayDate: { type: 'string', description: '' } }, required: [] },
    },
  ] as unknown as BuilderSchema;

describe('Schema Builder labels', () => {
  it('heads the class list "Document Types"', () => {
    render(<SchemaBuilder initialSchema={oneClass(true)} />);

    expect(screen.getByText('Document Types')).toBeInTheDocument();
    expect(screen.queryByText(/Documents Types/)).not.toBeInTheDocument();
  });

  it('names the empty state "No Document Types"', () => {
    render(<SchemaBuilder initialSchema={oneClass(false)} />);

    expect(screen.getByText('No Document Types')).toBeInTheDocument();
  });

  it('names the policy empty state "No Rule Types"', () => {
    render(<SchemaBuilder initialSchema={oneClass(false)} isRuleSchema />);

    expect(screen.getByText('No Rule Types')).toBeInTheDocument();
  });
});
