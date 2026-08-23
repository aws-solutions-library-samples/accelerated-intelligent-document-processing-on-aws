// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Signature regions detected by OCR must be visible in the page viewer.
 *
 * A SIGNATURE detection has confidence and geometry but no text, so the
 * LINE-driven OCR list ignored it: the page could carry a low-confidence
 * signature detection that nothing in the UI ever showed. pageData.json now
 * carries a `signatures` array, and the OCR Lines pane lists it with its
 * confidence and a clickable bounding box.
 */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

const PAGE_DATA = {
  provider: 'textract',
  geometryAvailable: true,
  confidenceAvailable: true,
  signaturesAvailable: true,
  lines: [
    {
      text: 'Signature of taxpayer',
      confidence: 99.9,
      geometry: { boundingBox: { left: 0.07, top: 0.87, width: 0.11, height: 0.01 } },
    },
  ],
  signatures: [
    {
      id: 'sig-1',
      confidence: 11,
      geometry: { boundingBox: { left: 0.571, top: 0.878, width: 0.037, height: 0.022 } },
    },
  ],
};

// vi.mock factories are hoisted above module-level consts, so the mock fn has to
// live in a hoisted binding.
const { graphqlMock } = vi.hoisted(() => ({ graphqlMock: vi.fn() }));

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: graphqlMock }) }));
vi.mock('../../../graphql/generated', () => ({ getFileContents: 'getFileContents', uploadDocument: 'uploadDocument' }));
vi.mock('@monaco-editor/react', () => ({ Editor: () => <div data-testid="editor" /> }));
vi.mock('../../document-viewer/MarkdownViewer', () => ({ default: () => <div data-testid="markdown" /> }));
vi.mock('../../common/PageImageViewer', () => ({ default: () => <div data-testid="page-image" /> }));

import PageTextEditorModal from '../PageTextEditorModal';

const PAGES = [
  {
    Id: '2',
    ImageUri: 's3://b/doc/pages/2/image.jpg',
    TextUri: 's3://b/doc/pages/2/result.json',
    OcrPageDataUri: 's3://b/doc/pages/2/pageData.json',
  },
];

/** Serve result.json / pageData.json from the given pageData fixture. */
const serve = (pageData: Record<string, unknown>, text: string) => {
  graphqlMock.mockImplementation(({ variables }: { variables: { s3Uri: string } }) => {
    const content = variables.s3Uri.endsWith('pageData.json') ? JSON.stringify(pageData) : JSON.stringify({ text });
    return Promise.resolve({ data: { getFileContents: { content, isBinary: false } } });
  });
};

describe('PageTextEditorModal signature detections', () => {
  it('lists each detected signature region with its confidence', async () => {
    serve(PAGE_DATA, 'Signature of taxpayer\n[SIGNATURE]');
    render(<PageTextEditorModal visible pages={PAGES} initialPageId="2" />);

    await waitFor(() => expect(screen.getByText(/Signature detections \(1\)/)).toBeInTheDocument());
    expect(screen.getByText(/\[SIGNATURE\] region 1/)).toBeInTheDocument();
    // The low detection confidence is what tells a reviewer this is a faint mark.
    expect(screen.getByText('11')).toBeInTheDocument();
    // OCR lines still render alongside.
    expect(screen.getByText('Signature of taxpayer')).toBeInTheDocument();
  });

  it('omits the section when the page has no detections', async () => {
    serve({ ...PAGE_DATA, signaturesAvailable: false, signatures: [] }, 'Signature of taxpayer');
    render(<PageTextEditorModal visible pages={PAGES} initialPageId="2" />);

    await waitFor(() => expect(screen.getByText('Signature of taxpayer')).toBeInTheDocument());
    expect(screen.queryByText(/Signature detections/)).not.toBeInTheDocument();
  });
});
