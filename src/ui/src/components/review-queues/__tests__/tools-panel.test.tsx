// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Review Management info panel says how a document reaches a production queue now:
 * while Review Routing is on and the Default queue is enabled, a routing rule names the
 * queue, and the page warns when routing rules are not evaluated; the Default queue takes
 * what the built-in routing rules hold and documents whose queue is missing or disabled.
 * It says "routing rule", never a bare "rule", and "Review Routing" for the switch. It
 * links to the routing page of the docs.
 */

import React from 'react';
import { render } from '@testing-library/react';
import { IntlProvider } from 'react-intl';
import { describe, expect, it } from 'vitest';

import { ReviewManagementToolsPanel } from '../tools-panel';

const panel = (): HTMLElement =>
  render(
    <IntlProvider locale="en">
      <ReviewManagementToolsPanel />
    </IntlProvider>,
  ).container;

describe('the Review Management info panel', () => {
  it('says routing rules send a document to the queue a routing rule names, while Review Routing and the Default queue are on', () => {
    const text = panel().textContent ?? '';

    expect(text).toContain(
      "While Review Routing is on in a document's configuration profile and the Default queue is enabled, the routing rules of that profile decide whether the document is reviewed and send it to the queue a routing rule names.",
    );
    expect(text).toContain('A warning on this page says when routing rules are not evaluated, and why.');
    expect(text).toContain(
      'The Default queue receives the documents the built-in routing rules hold (failed extractions, confidence alerts, missing required fields) and documents whose queue is missing or disabled.',
    );
    expect(text).not.toContain('HITL');
  });

  it('names the switch Review Routing and every rule a routing rule', () => {
    const text = panel().textContent ?? '';

    expect(text).not.toMatch(/human review is enabled/i);
    // A "rule" is always a "routing rule" here: no bare one is left.
    expect(text.match(/\brules?\b/g)?.length).toBe(text.match(/\brouting rules?\b/gi)?.length);
  });

  it('names the Create queue button and says what deleting a queue does', () => {
    const container = panel();

    expect([...container.querySelectorAll('b')].map((b) => b.textContent)).toContain('Create queue');
    expect(container.textContent).toContain(
      'Deleting a queue moves its documents to the Default queue, and lists the routing rules that name it first.',
    );
  });

  it('links to the review routing page of the docs', () => {
    const links = [...panel().querySelectorAll('a')];
    const routing = links.find((a) => a.textContent === 'Review routing');

    expect(routing?.getAttribute('href')).toMatch(/review-routing\/$/);
    expect(links.map((a) => a.textContent)).toEqual(['Review queues', 'Review routing', 'Human review', 'Test Studio']);
  });
});
