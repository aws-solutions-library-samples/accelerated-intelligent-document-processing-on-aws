// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Print builds the print window's HTML as a string, so the document name and the
 * report title it puts in `<title>` are written as text, not markup. A document name
 * is whatever the uploaded file was called.
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));

import MarkdownViewer from '../MarkdownViewer';

describe('MarkdownViewer print', () => {
  it('writes the document name and title into <title> as text', async () => {
    const written: string[] = [];
    const fakeWindow = {
      document: { open: vi.fn(), write: (html: string) => written.push(html), close: vi.fn() },
      print: vi.fn(),
      close: vi.fn(),
    };
    const open = vi.spyOn(window, 'open').mockReturnValue(fakeWindow as unknown as Window);

    render(
      <MarkdownViewer content="# Report" title='Summary "A"' documentName={'a</title><img src="https://remote.example/x.png">.pdf'} />,
    );
    await userEvent.click(screen.getByRole('button', { name: /print/i }));

    expect(open).toHaveBeenCalled();
    const html = written.join('');
    const doc = new DOMParser().parseFromString(html, 'text/html');
    expect(doc.title).toBe('Summary "A" - a</title><img src="https://remote.example/x.png">.pdf');
    expect(doc.head.querySelector('img')).toBeNull();
    expect(doc.querySelectorAll('img')).toHaveLength(0);
    open.mockRestore();
  });
});
