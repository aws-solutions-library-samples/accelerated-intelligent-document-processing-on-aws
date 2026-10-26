// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Home section: the Inbox for everyone, then Tasks and Queues (the queues that list
 * the caller as an Annotator) for an Annotator, in that order.
 */

import { describe, expect, it } from 'vitest';
import type { SideNavigationProps } from '@cloudscape-design/components';

import { MY_QUEUES_PATH, MY_TASKS_PATH } from '../../routes/constants';
import { HOME_SECTION, buildHomeNavSection } from './review-queues-nav-items';

const inbox: SideNavigationProps.Link = { type: 'link', text: 'Inbox', href: '#/inbox' };

describe('buildHomeNavSection', () => {
  it('lists Inbox, Tasks and Queues for an Annotator', () => {
    const section = buildHomeNavSection(inbox, { showWork: true, taskCount: 0 });
    expect(section.text).toBe(HOME_SECTION);
    expect(HOME_SECTION).toBe('Home');
    const links = section.items as SideNavigationProps.Link[];
    expect(links.map((l) => l.text)).toEqual(['Inbox', 'Tasks', 'Queues']);
    expect(links.map((l) => l.href)).toEqual(['#/inbox', `#${MY_TASKS_PATH}`, `#${MY_QUEUES_PATH}`]);
  });

  it('lists the Inbox only for everyone else', () => {
    const section = buildHomeNavSection(inbox, { showWork: false, taskCount: 3 });
    expect((section.items as SideNavigationProps.Link[]).map((l) => l.text)).toEqual(['Inbox']);
  });

  it('keeps the Tasks badge on Tasks only', () => {
    const links = buildHomeNavSection(inbox, { showWork: true, taskCount: 3 }).items as SideNavigationProps.Link[];
    expect(links[1].info).toBeTruthy();
    expect(links[2].info).toBeUndefined();
  });
});
