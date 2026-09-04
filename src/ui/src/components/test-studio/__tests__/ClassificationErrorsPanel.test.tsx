// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from '@testing-library/react';
import React from 'react';
import { describe, expect, it } from 'vitest';

import ClassificationErrorsPanel from '../ClassificationErrorsPanel';

describe('ClassificationErrorsPanel', () => {
  it('renders nothing when the run classified everything correctly', () => {
    // An empty table would read as "no data" — i.e. as though the check had not
    // run — rather than as "no problems found".
    const { container } = render(<ClassificationErrorsPanel classificationErrors={{ errors: [], total: 0 }} testSetId="ts1" />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing for a run aggregated before this shipped', () => {
    const { container } = render(<ClassificationErrorsPanel classificationErrors={{}} testSetId="ts1" />);
    expect(container).toBeEmptyDOMElement();
    const { container: nullContainer } = render(<ClassificationErrorsPanel classificationErrors={null} testSetId="ts1" />);
    expect(nullContainer).toBeEmptyDOMElement();
  });

  it('shows the expected and predicted class for a misclassified document', () => {
    render(
      <ClassificationErrorsPanel
        classificationErrors={{
          errors: [
            {
              doc_key: 'invoice-7.pdf',
              kind: 'class',
              expected_class: 'Invoice',
              predicted_class: 'Receipt',
              expected_pages: [0, 1],
              predicted_pages: [0, 1],
            },
          ],
          total: 1,
          documents_affected: 1,
        }}
        testSetId="ts1"
      />,
    );

    expect(screen.getByText('invoice-7.pdf')).toBeInTheDocument();
    expect(screen.getByText('Invoice')).toBeInTheDocument();
    expect(screen.getByText('Receipt')).toBeInTheDocument();
    expect(screen.getByText('Wrong class')).toBeInTheDocument();
  });

  it('links the document into the annotation queue where the class is corrected', () => {
    render(
      <ClassificationErrorsPanel
        classificationErrors={{
          errors: [{ doc_key: 'a b.pdf', kind: 'class', expected_class: 'W2', predicted_class: 'Invoice' }],
          total: 1,
        }}
        testSetId="ts1"
      />,
    );

    const link = screen.getByRole('link', { name: 'a b.pdf' });
    // Encoded, so a key with a space or '&' still resolves to the right document.
    expect(link).toHaveAttribute('href', expect.stringContaining('doc=a%20b.pdf'));
    // The SHAPE, not just the query. A substring assertion passed happily against
    // '##/test-studio/...', which HashRouter cannot route: everything after the
    // first '#' is the fragment, so it matched nothing and dropped the ?doc=.
    // This is the one route into the panel's whole purpose.
    const href = link.getAttribute('href') ?? '';
    expect(href.startsWith('#/'), `href must be a single-hash route, got ${href}`).toBe(true);
    expect(href).not.toMatch(/^##/);
    expect(href).toMatch(/^#\/test-studio\/sets\/ts1\/annotate\?doc=/);
  });

  it('renders the document as plain text when there is no test set to link to', () => {
    render(
      <ClassificationErrorsPanel
        classificationErrors={{ errors: [{ doc_key: 'x.pdf', kind: 'class', expected_class: 'A', predicted_class: 'B' }], total: 1 }}
        testSetId={null}
      />,
    );

    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.getByText('x.pdf')).toBeInTheDocument();
  });

  it('distinguishes the three kinds rather than calling them all errors', () => {
    render(
      <ClassificationErrorsPanel
        classificationErrors={{
          errors: [
            { doc_key: 'a.pdf', kind: 'class', expected_class: 'A', predicted_class: 'B' },
            { doc_key: 'b.pdf', kind: 'unmatched', expected_class: 'A', predicted_class: null },
            { doc_key: 'c.pdf', kind: 'order', expected_class: 'A', predicted_class: 'A' },
          ],
          total: 3,
        }}
        testSetId="ts1"
      />,
    );

    expect(screen.getByText('Wrong class')).toBeInTheDocument();
    expect(screen.getByText('No matching section')).toBeInTheDocument();
    expect(screen.getByText('Page order')).toBeInTheDocument();
  });

  it('says how many rows it is not showing when the list was capped', () => {
    // Silent truncation would read as "these are all the errors".
    render(
      <ClassificationErrorsPanel
        classificationErrors={{
          errors: [{ doc_key: 'a.pdf', kind: 'class', expected_class: 'A', predicted_class: 'B' }],
          total: 340,
          truncated: true,
        }}
        testSetId="ts1"
      />,
    );

    expect(screen.getByText(/Showing the first 1 of 340/)).toBeInTheDocument();
  });

  it('does not claim a wrong schema when only the page order differs', () => {
    render(
      <ClassificationErrorsPanel
        classificationErrors={{
          errors: [{ doc_key: 'c.pdf', kind: 'order', expected_class: 'A', predicted_class: 'A' }],
          total: 1,
        }}
        testSetId="ts1"
      />,
    );

    expect(screen.queryByText(/extracted under the wrong schema/)).not.toBeInTheDocument();
  });
});
