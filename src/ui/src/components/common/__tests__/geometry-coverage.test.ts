// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Which fields have a bounding box, and why the rest cannot.
 *
 * Requested as [#1327]: the viewer offered a locate control on some rows and
 * nothing on the others, with no statement of the rule and no count, so a reader
 * could not tell whether the missing boxes were a limitation of their
 * configuration or a fault.
 *
 * The shape here is the one a real run produces. In a ten-field form section from
 * a packet processed under the default `ocr_only` mode, six fields carried a value
 * and a box and four carried neither — so "has a box" and "has a value" coincided
 * exactly. The fixture reproduces that split rather than the document, whose text
 * does not belong in this repository.
 */

import { describe, expect, it } from 'vitest';

import { classifyFieldGeometry, describeFieldGeometry, isAbsentValue, summariseGeometryCoverage } from '../geometry-coverage';

/** A located field: confidence and a geometry entry. */
const located = (confidence = 0.9) => ({
  confidence,
  geometry: [{ boundingBox: { left: 0.1, top: 0.2, width: 0.3, height: 0.04 }, page: 1 }],
});

/** A reported field with no box. */
const unlocated = (confidence = 0.9) => ({ confidence });

describe('isAbsentValue', () => {
  it('treats the empty forms as absent', () => {
    expect(isAbsentValue(null)).toBe(true);
    expect(isAbsentValue(undefined)).toBe(true);
    expect(isAbsentValue('')).toBe(true);
    expect(isAbsentValue('   ')).toBe(true);
    expect(isAbsentValue([])).toBe(true);
    expect(isAbsentValue({})).toBe(true);
  });

  it("treats the pipeline's stringified None as absent", () => {
    // The pipeline emits Python's str(None) rather than JSON null, so the field
    // arrives as this four-character string and renders as text. A reader seeing
    // "None" with no box needs to be told the model returned nothing, not that
    // the value could not be found on the page.
    expect(isAbsentValue('None')).toBe(true);
    expect(isAbsentValue('none')).toBe(true);
    expect(isAbsentValue('null')).toBe(true);
  });

  it('does not guess at sentinels with no producer', () => {
    // A single hyphen is an ordinary extracted cell value and "NA" is a real
    // value in several document vocabularies. Treating them as absent would make
    // the row claim nothing was extracted while displaying the value that was.
    expect(isAbsentValue('-')).toBe(false);
    expect(isAbsentValue('--')).toBe(false);
    expect(isAbsentValue('NA')).toBe(false);
    expect(isAbsentValue('N/A')).toBe(false);
  });

  it('does not treat a real value as absent, including falsy ones', () => {
    // Zero and false are extracted values. Reporting them as "nothing was
    // extracted" would explain away a box that is genuinely missing.
    expect(isAbsentValue(0)).toBe(false);
    expect(isAbsentValue(false)).toBe(false);
    expect(isAbsentValue('0')).toBe(false);
    expect(isAbsentValue('887')).toBe(false);
    expect(isAbsentValue('Above identified via arrest report.')).toBe(false);
  });

  it('does not treat a value that merely contains a sentinel as absent', () => {
    // The list is matched whole, so a sentence mentioning one is still a value.
    expect(isAbsentValue('None of the above')).toBe(false);
    expect(isAbsentValue('NA 12345')).toBe(false);
  });
});

describe('classifyFieldGeometry', () => {
  it('reports a field with a box as located, whatever its value', () => {
    expect(classifyFieldGeometry('887', true)).toBe('located');
    // Geometry wins: if the viewer has a box it can show it, and arguing with
    // that from the value would hide a working control.
    expect(classifyFieldGeometry('None', true)).toBe('located');
  });

  it('separates "nothing was extracted" from "could not be found"', () => {
    // The two have different remedies — one is an extraction result to look at,
    // the other is a value to check against the document — so they must not
    // share a message.
    expect(classifyFieldGeometry('None', false)).toBe('no-value');
    expect(classifyFieldGeometry('', false)).toBe('no-value');
    expect(classifyFieldGeometry('24 AUG 11', false)).toBe('unmatched');
  });
});

describe('classifyFieldGeometry when nothing in the section has geometry', () => {
  it('reports the shared cause for a field that does have a value', () => {
    // Nothing in the section is located, so this field's own value explains
    // nothing and the cause is the same on every row.
    expect(classifyFieldGeometry('24 AUG 11', false, false)).toBe('none-produced');
  });

  it('still prefers the absent value, which is the truthful reason', () => {
    // Measured on a misclassified packet section: all thirteen fields came back
    // 'None' with confidence 0.0 and no boxes, so the section-wide check fired
    // first and every row read "no bounding boxes in this document" — blaming
    // the configuration, while sibling sections of the same document had boxes.
    expect(classifyFieldGeometry('', false, false)).toBe('no-value');
    expect(classifyFieldGeometry('None', false, false)).toBe('no-value');
  });

  it('still reports a field that does have a box', () => {
    // Belt and braces: a located field is located whatever the document-level
    // flag says, so a wrong flag cannot hide a working control.
    expect(classifyFieldGeometry('x', true, false)).toBe('located');
  });
});

describe('classifyFieldGeometry when nothing was recorded for the field', () => {
  it('does not claim a match was attempted', () => {
    // Measured on a real packet section: ten fields displayed, three with
    // entries. Reporting the other seven as unmatched sent the reader looking
    // for values on the page when nothing had been recorded about them.
    expect(classifyFieldGeometry('27 Sep 2011', false, true, false)).toBe('no-data');
  });

  it('still prefers the absent-value reason, which is the more useful one', () => {
    // Both hold for a field with no value and no entry; "nothing was extracted"
    // is what the reader can act on.
    expect(classifyFieldGeometry('None', false, true, false)).toBe('no-value');
  });

  it('keeps unmatched for a field that does have an entry', () => {
    // An entry and no box is the case where matching ran and found nothing, and
    // checking the value against the document is then the right advice.
    expect(classifyFieldGeometry('27 Sep 2011', false, true, true)).toBe('unmatched');
  });
});

describe('describeFieldGeometry', () => {
  it('never attributes a section-level outcome to the document or the configuration', () => {
    // A packet section can come back with no boxes while its siblings have them
    // under the same configuration, so neither can be claimed from here.
    const all = (['located', 'no-value', 'unmatched', 'none-produced', 'no-data'] as const).map(describeFieldGeometry);
    all.forEach((text) => {
      expect(text).not.toMatch(/this document/i);
      expect(text).not.toMatch(/geometry\.mode/i);
    });
  });

  it('names the cause rather than restating the absence', () => {
    expect(describeFieldGeometry('no-value')).toMatch(/No value extracted, so no bounding box/);
    expect(describeFieldGeometry('unmatched')).toMatch(/Value not found in the page text/);
    expect(describeFieldGeometry('none-produced')).toMatch(/No field in this section has a bounding box/);
    expect(describeFieldGeometry('no-data')).toMatch(/No bounding box recorded/);
  });

  it('gives every state a sentence', () => {
    // A state with no wording renders an empty tooltip, which reads as a bug in
    // the viewer rather than an explanation.
    (['located', 'no-value', 'unmatched', 'none-produced', 'no-data'] as const).forEach((state) => {
      expect(describeFieldGeometry(state).length).toBeGreaterThan(10);
    });
  });
});

describe('summariseGeometryCoverage', () => {
  it('counts the fields displayed, not the entries recorded', () => {
    // The case that exposed this: one form section of a packet displays ten
    // fields and carries explainability entries for three, so counting entries
    // put "3 of 3" above ten rows. Every displayed field gets a row and a
    // control whether or not anything was recorded about it.
    const values = {
      FormId: '1046835',
      FormType: 'NOTIFICATION',
      SubmitterName: 'Ruth Webster',
      SubmitterId: 'None',
      SubmissionDate: '27 Sep 2011',
      Department: 'Chicago Police Department',
      ProcessedBy: 'None',
      ProcessingDate: 'None',
      ApprovalStatus: 'None',
      Comments: 'Completing the report.',
    };
    const explainability = [{ FormId: located(1), FormType: located(1), SubmitterName: located(0.99) }];

    expect(summariseGeometryCoverage(values, explainability)).toEqual({ located: 3, total: 10 });
  });

  it('counts the six-of-ten split where every field has an entry', () => {
    const values = Object.fromEntries(
      [
        'FormId',
        'FormType',
        'SubmitterName',
        'SubmitterId',
        'SubmissionDate',
        'Department',
        'ProcessedBy',
        'ProcessingDate',
        'ApprovalStatus',
        'Comments',
      ].map((k) => [k, 'v']),
    );
    const explainability = [
      {
        FormId: located(),
        FormType: located(),
        SubmitterName: unlocated(),
        SubmitterId: located(),
        SubmissionDate: located(),
        Department: located(),
        ProcessedBy: unlocated(),
        ProcessingDate: unlocated(),
        ApprovalStatus: unlocated(),
        Comments: located(),
      },
    ];
    expect(summariseGeometryCoverage(values, explainability)).toEqual({ located: 6, total: 10 });
  });

  it('walks nested objects and arrays so a packet is not counted as one field', () => {
    const values = { Payer: { Name: 'a', Address: 'b' }, LineItems: [{ Amount: 1 }, { Amount: 2 }], Total: 3 };
    const explainability = [
      {
        Payer: { Name: located(), Address: unlocated() },
        LineItems: [{ Amount: located() }, { Amount: located() }],
        Total: located(),
      },
    ];
    expect(summariseGeometryCoverage(values, explainability)).toEqual({ located: 4, total: 5 });
  });

  it('counts only geometry the viewer can actually draw', () => {
    // One definition of "has a box", shared with the per-field control: the
    // renderer builds a highlight only from an entry carrying both boundingBox
    // and page. Counting an entry missing either would report a field as
    // locatable whose row offers no way to locate it.
    expect(summariseGeometryCoverage({ Field: 'v' }, [{ Field: { confidence: 0.5, geometry: [] } }])).toEqual({ located: 0, total: 1 });
    expect(summariseGeometryCoverage({ Field: 'v' }, [{ Field: { confidence: 0.5, geometry: [{ page: 1 }] } }])).toEqual({
      located: 0,
      total: 1,
    });
    expect(summariseGeometryCoverage({ Field: 'v' }, [{ Field: { confidence: 0.5, geometry: [{ boundingBox: { left: 0 } }] } }])).toEqual({
      located: 0,
      total: 1,
    });
  });

  it('counts the entry the renderer draws, not any entry that happens to be valid', () => {
    // The renderer takes `geometry[0]` and the overlay draws that. Counting with
    // `some()` would mark this field located while its row offers no control,
    // which is the caption and the rows disagreeing — the state this count
    // exists to prevent.
    const firstUnusable = [{ Field: { confidence: 0.9, geometry: [{ page: 1 }, { boundingBox: { left: 0 }, page: 1 }] } }];
    expect(summariseGeometryCoverage({ Field: 'v' }, firstUnusable)).toEqual({ located: 0, total: 1 });
  });

  it('reports the fields with none located rather than nothing at all', () => {
    // total 0 means "no fields displayed", which the caller uses to omit the
    // line entirely. A section with fields but no geometry is a different
    // statement and must still be counted.
    expect(summariseGeometryCoverage({ A: 'v', B: 'v' }, undefined)).toEqual({ located: 0, total: 2 });
    expect(summariseGeometryCoverage({ A: 'v', B: 'v' }, [])).toEqual({ located: 0, total: 2 });
    expect(summariseGeometryCoverage({}, [{}])).toEqual({ located: 0, total: 0 });
    expect(summariseGeometryCoverage(undefined, [{}])).toEqual({ located: 0, total: 0 });
  });

  it('counts an absent value as a displayed field, since it has a row', () => {
    // A field the model returned nothing for is still shown, still gets a
    // control, and still belongs in the denominator.
    expect(summariseGeometryCoverage({ A: 'None', B: '', C: null }, [{}])).toEqual({ located: 0, total: 3 });
  });
});
