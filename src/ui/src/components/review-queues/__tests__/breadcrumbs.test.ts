// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Review Studio breadcrumbs. What has to hold: Review Management sits under Review
 * Studio, both opening Review Management on the table the trail came from; a queue's
 * page is under Review Management for an Admin and under Home › Queues otherwise; the
 * edit page's trail follows where it was opened from, the document last; `from=` is
 * read only when it names an origin, and added only to an edit page's route.
 */

import { describe, expect, it } from 'vitest';

import { EDIT_ORIGINS, parseEditOrigin, withEditOrigin } from '../../../routes/constants';
import { editActiveHref, editTrail, queuePageTrail, reviewManagementTrail } from '../breadcrumbs';

const texts = (trail: { text: string }[]) => trail.map((c) => c.text);
const QUEUE = { crumb: { text: 'Default', href: '#/review-queues/queues/default' }, view: 'production' as const };
const SET = { text: 'Invoices', href: '#/test-studio/sets/ts-1' };

describe('Review Management', () => {
  it('is under Review Studio, both opening it on the table the trail came from', () => {
    expect(reviewManagementTrail()).toEqual([
      { text: 'Review Studio', href: '#/review-queues' },
      { text: 'Review Management', href: '#/review-queues' },
    ]);
    expect(reviewManagementTrail('test').map((c) => c.href)).toEqual(['#/review-queues?view=test', '#/review-queues?view=test']);
  });

  it('holds a queue’s page for an Admin; an Annotator’s is under Home › Queues', () => {
    expect(texts(queuePageTrail(true, QUEUE.crumb, 'production'))).toEqual(['Review Studio', 'Review Management', 'Default']);
    expect(texts(queuePageTrail(false, QUEUE.crumb, 'production'))).toEqual(['Home', 'Queues', 'Default']);
  });
});

describe('the edit page’s trail', () => {
  it.each([
    ['document', ['Document Processing', 'Documents', 'a.pdf', 'Edit']],
    ['review', ['Review Studio', 'Review Management', 'Default', 'a.pdf']],
    ['queues', ['Home', 'Queues', 'Default', 'a.pdf']],
    ['tasks', ['Home', 'Tasks', 'a.pdf']],
    ['inbox', ['Home', 'Inbox', 'a.pdf']],
    ['test-studio', ['Test Studio', 'Invoices', 'a.pdf']],
  ] as const)('from %s', (origin, expected) => {
    expect(texts(editTrail({ origin, objectKey: 'in/a.pdf', name: 'a.pdf', queue: QUEUE, testSet: SET }))).toEqual(expected);
  });

  it('links the document crumb to the document page when opened from it', () => {
    expect(editTrail({ origin: 'document', objectKey: 'in/a.pdf', name: 'a.pdf' })[2].href).toBe('#/documents/in%2Fa.pdf');
  });

  it('leaves the queue out until it is known', () => {
    expect(texts(editTrail({ origin: 'review', objectKey: 'a.pdf', name: 'a.pdf', queue: null }))).toEqual([
      'Review Studio',
      'Review Management',
      'a.pdf',
    ]);
  });

  it('highlights the navigation entry of every origin', () => {
    expect(EDIT_ORIGINS.map(editActiveHref)).toEqual([
      '#/documents',
      '#/review-queues',
      '#/my-work/queues',
      '#/my-work/tasks',
      '#/test-studio?tab=sets',
      '#/inbox',
    ]);
  });
});

describe('the origin in the URL', () => {
  it('is read only when it names an origin', () => {
    expect(parseEditOrigin('?section=2&from=review')).toBe('review');
    expect(parseEditOrigin('?from=elsewhere')).toBeUndefined();
    expect(parseEditOrigin('')).toBeUndefined();
  });

  it('is added to an edit page’s route only, keeping its query', () => {
    expect(withEditOrigin('/documents/in%2Fa.pdf/edit?field=Box1', 'inbox')).toBe('/documents/in%2Fa.pdf/edit?field=Box1&from=inbox');
    expect(withEditOrigin('/review-queues/test-sets/ts-1/items/a.pdf', 'inbox')).toBe(
      '/review-queues/test-sets/ts-1/items/a.pdf?from=inbox',
    );
    expect(withEditOrigin('/documents/in%2Fa.pdf', 'inbox')).toBe('/documents/in%2Fa.pdf');
    expect(withEditOrigin('/test-studio?tab=sets', 'inbox')).toBe('/test-studio?tab=sets');
  });
});
