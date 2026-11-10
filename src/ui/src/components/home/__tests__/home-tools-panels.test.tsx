// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Every Home page has an info panel (the info icon on the right), as Review
 * Management does: what the page is for, who uses it, and links to the docs.
 */

import React from 'react';
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import { DOCS_BASE_URL } from '../../../constants/github';
import { InboxToolsPanel, JobToolsPanel, JobsToolsPanel, MyQueuesToolsPanel, TasksToolsPanel } from '../home-tools-panels';

const CASES: [string, () => React.JSX.Element, string[]][] = [
  ['Inbox', InboxToolsPanel, ['web-ui/#inbox', 'configuration/#inbox-message-retention']],
  ['Tasks', TasksToolsPanel, ['review-queues/', 'human-review/']],
  ['Queues', MyQueuesToolsPanel, ['review-queues/']],
  ['Jobs', JobsToolsPanel, ['human-review/#skip-all-reviews-for-many-documents', 'configuration/#bulk-skip-all-reviews-limit']],
  ['Job', JobToolsPanel, ['human-review/#skip-all-reviews-for-many-documents']],
];

describe('Home info panels', () => {
  it.each(CASES)('%s says what the page is for and links to the docs', (title, Panel, paths) => {
    render(<Panel />);
    expect(screen.getByRole('heading', { level: 2, name: title })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /Learn more/ })).toBeInTheDocument();
    const hrefs = screen.getAllByRole('link').map((a) => a.getAttribute('href'));
    for (const path of paths) expect(hrefs).toContain(`${DOCS_BASE_URL}/${path}`);
    for (const link of screen.getAllByRole('link')) expect(link).toHaveAttribute('target', '_blank');
  });
});
