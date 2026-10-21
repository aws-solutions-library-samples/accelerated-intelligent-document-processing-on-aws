// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The list's Review Status links a queued document to its queue item, for the roles
 * that can open one, so reviewing starts the same way from the list as from the
 * document page's Review button.
 */

import React from 'react';
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import { COLUMN_DEFINITIONS_MAIN, type MappedDocument } from '../documents-table-config';

const statusCell = (canWorkReviewQueues: boolean, hitlStatus: string) => {
  const column = COLUMN_DEFINITIONS_MAIN([], 'PRODUCTION', canWorkReviewQueues).find((c) => c.id === 'hitlStatus');
  const item = { objectKey: 'tenant/one/lending.pdf', hitlStatus, hitlTriggered: true } as MappedDocument;
  render(<>{column?.cell(item)}</>);
};

describe('Review Status column', () => {
  it('links In Review to the queue item for a role that can open it', () => {
    statusCell(true, 'in_review');
    expect(screen.getByRole('link', { name: /In Review/ })).toHaveAttribute(
      'href',
      `#/review-queues/items/${encodeURIComponent('tenant/one/lending.pdf')}`,
    );
  });

  it('shows plain status to a role without queue access', () => {
    statusCell(false, 'in_review');
    expect(screen.getByText('In Review')).toBeInTheDocument();
    expect(screen.queryByRole('link')).toBeNull();
  });
});
