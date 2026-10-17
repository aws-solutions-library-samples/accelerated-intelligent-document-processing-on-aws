// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The My Work section: Inbox, Tasks, Queues (the queues that list the caller as an
 * Annotator) and Favorites, in that order.
 */

import { describe, expect, it } from 'vitest';
import type { SideNavigationProps } from '@cloudscape-design/components';

import { FAVORITES_PATH, MY_QUEUES_PATH, MY_TASKS_PATH } from '../../routes/constants';
import { MY_WORK_SECTION, buildMyWorkNavSection } from './review-queues-nav-items';

const inbox: SideNavigationProps.Link = { type: 'link', text: 'Inbox', href: '#/inbox' };

describe('buildMyWorkNavSection', () => {
  it('lists Inbox, Tasks, Queues and Favorites', () => {
    const section = buildMyWorkNavSection(inbox, 0);
    expect(section.text).toBe(MY_WORK_SECTION);
    const links = section.items as SideNavigationProps.Link[];
    expect(links.map((l) => l.text)).toEqual(['Inbox', 'Tasks', 'Queues', 'Favorites']);
    expect(links.map((l) => l.href)).toEqual(['#/inbox', `#${MY_TASKS_PATH}`, `#${MY_QUEUES_PATH}`, `#${FAVORITES_PATH}`]);
  });

  it('keeps the Tasks badge on Tasks only', () => {
    const links = buildMyWorkNavSection(inbox, 3).items as SideNavigationProps.Link[];
    expect(links[1].info).toBeTruthy();
    expect(links[2].info).toBeUndefined();
  });
});
