// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Missing required fields (#1527): required fields extraction left absent, counted
 * beside the confidence alerts rather than as alerts. What has to hold: the stored
 * document count wins over a sum of the (capped) section lists; with neither, the
 * count is unknown (null, shown as a dash) rather than a 0 the row cannot support;
 * the sections table lists the names; and the explainability-based alert count no
 * longer counts a field whose extracted value is absent.
 */

import React from 'react';
import { cleanup, render } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { afterEach, describe, expect, it } from 'vitest';

import {
  getDocumentMissingRequiredFieldCount,
  getSectionMissingRequiredFields,
  resolveMissingRequiredFieldCount,
} from '../missing-required-fields-utils';
import { getFieldsBelowThreshold, getSectionConfidenceAlerts, hasExtractedValue } from '../confidence-alerts-utils';
import mapDocumentsAttributes from '../map-document-attributes';
import { MissingRequiredFieldsCell } from '../../sections-panel/SectionsPanel';
import { MissingRequiredFieldsCount } from '../../document-panel/DocumentPanel';

const missing = (name: string) => ({ attributeName: name, confidence: 0, confidenceThreshold: 0.8 });
const mapOne = (item: Record<string, unknown>) => mapDocumentsAttributes([item as never])[0] as Record<string, unknown>;

afterEach(() => {
  cleanup();
});

describe('missing-required-field counts', () => {
  it('drops null entries from a section list', () => {
    expect(getSectionMissingRequiredFields({ MissingRequiredFields: [missing('A'), null] })).toEqual([missing('A')]);
    expect(getSectionMissingRequiredFields({})).toEqual([]);
  });

  it('sums the sections', () => {
    expect(
      getDocumentMissingRequiredFieldCount([
        { MissingRequiredFields: [missing('A')] },
        { MissingRequiredFields: [missing('B'), missing('C')] },
      ]),
    ).toBe(3);
  });

  it('prefers the stored count, which the capped lists can undercount', () => {
    expect(resolveMissingRequiredFieldCount(120, [{ MissingRequiredFields: [missing('A')] }])).toBe(120);
    expect(resolveMissingRequiredFieldCount(0, [{ MissingRequiredFields: [missing('A')] }])).toBe(0);
  });

  it('is unknown, not zero, when the row carries neither', () => {
    expect(resolveMissingRequiredFieldCount(undefined, undefined)).toBeNull();
    expect(mapOne({ ObjectKey: 'd.pdf' }).missingRequiredFieldCount).toBeNull();
  });

  it('maps the stored count, or the sections when none is stored', () => {
    expect(mapOne({ ObjectKey: 'd.pdf', MissingRequiredFieldCount: 4 }).missingRequiredFieldCount).toBe(4);
    expect(mapOne({ ObjectKey: 'd.pdf', Sections: [{ Id: '1', MissingRequiredFields: [missing('A')] }] }).missingRequiredFieldCount).toBe(
      1,
    );
  });
});

describe('display', () => {
  it('lists the section fields by name behind the count', () => {
    render(<MissingRequiredFieldsCell item={{ Id: '1', MissingRequiredFields: [missing('Doi'), missing('Rows[2].Amount')] } as never} />);
    const wrapper = createWrapper(document.body);
    expect(wrapper.findStatusIndicator()!.getElement().textContent).toBe('2');
    const popover = wrapper.findPopover()!;
    popover.findTrigger().click();
    const content = popover.findContent()!.getElement().textContent;
    expect(content).toContain('Doi');
    expect(content).toContain('Rows[2].Amount');
  });

  it('shows a green zero for a section with none', () => {
    render(<MissingRequiredFieldsCell item={{ Id: '1' } as never} />);
    expect(createWrapper(document.body).findStatusIndicator()!.getElement().textContent).toBe('0');
    expect(createWrapper(document.body).findPopover()).toBeNull();
  });

  it('shows a dash in Document Details when the count is unknown', () => {
    const { container } = render(<MissingRequiredFieldsCount count={null} sections={undefined} />);
    expect(container.textContent).toBe('-');
  });

  it('shows the document count in Document Details', () => {
    render(<MissingRequiredFieldsCount count={3} sections={[]} />);
    expect(createWrapper(document.body).findStatusIndicator()!.getElement().textContent).toBe('3');
  });
});

describe('an absent value is not a confidence alert', () => {
  it.each([[null], [''], ['  '], [[]], [{}], [{ a: null }]])('treats %j as absent', (value) => {
    expect(hasExtractedValue(value)).toBe(false);
  });

  it.each([['x'], [0], [false], [{ a: 'x' }]])('treats %j as present', (value) => {
    expect(hasExtractedValue(value)).toBe(true);
  });

  it('skips low scores on absent values, nested and per row, when the values are known', () => {
    const explainability = {
      Title: { confidence: 0.0 },
      Journal: { confidence: 0.1 },
      Address: { City: { confidence: 0.1 }, Zip: { confidence: 0.1 } },
      Rows: [{ Amount: { confidence: 0.1 } }, { Amount: { confidence: 0.1 } }],
    };
    const values = { Title: null, Journal: 'Nature', Address: { City: 'Seattle', Zip: '' }, Rows: [{ Amount: '1' }, { Amount: null }] };
    expect(getFieldsBelowThreshold(explainability, 0.8, '', values).map((f) => f.fieldPath)).toEqual([
      'Journal',
      'Address.City',
      'Rows[0].Amount',
    ]);
    // Without the values nothing can be judged, so nothing is excluded.
    expect(getFieldsBelowThreshold(explainability, 0.8)).toHaveLength(6);
  });

  it('reads the values from the section output', () => {
    const section = {
      Output: {
        explainabilityData: { Title: { confidence: 0.0 }, Journal: { confidence: 0.1 } },
        inference_result: { Title: null, Journal: 'Nature' },
      },
    };
    expect(getSectionConfidenceAlerts(section, { hitl: { confidence_threshold: 0.8 } }).map((f) => f.fieldPath)).toEqual(['Journal']);
  });
});
