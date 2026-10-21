// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Config-schema descriptions are markdown, and the form used to show the source.
 *
 * The descriptions in `patterns/unified/template.yaml` carry emphasis and code
 * spans (`**Cost.**`, `` `granularity: packet` ``) because they are read as
 * source as well as rendered. Passing them to Cloudscape's `description` slot as
 * a plain string put the literal asterisks and backticks in front of the user,
 * which reads as a typo in the product rather than as formatting.
 *
 * ⚠️ The block-to-inline mapping is the part that matters and the part with no
 * visible failure in a unit test that only checks text. ReactMarkdown wraps
 * every string in a `<p>`, and a `<p>` in a help slot adds vertical margin that
 * breaks the field's spacing — several paragraphs' worth for a description
 * containing a blank line. So the rendered output must contain no block
 * elements at all.
 */

import React from 'react';
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

import FieldDescription, { escapeAngleBrackets, stripInlineMarkdown } from '../FieldDescription';

describe('FieldDescription', () => {
  it('renders bold as an element, not as asterisks', () => {
    const { container } = render(<FieldDescription>{'**Cost.** It is cheap.'}</FieldDescription>);
    expect(container.querySelector('strong')?.textContent).toBe('Cost.');
    expect(container.textContent).not.toContain('**');
  });

  it('renders a code span as code, not as backticks', () => {
    const { container } = render(<FieldDescription>{'Set `granularity: packet`.'}</FieldDescription>);
    expect(container.querySelector('code')?.textContent).toBe('granularity: packet');
    expect(container.textContent).not.toContain('`');
  });

  it('emits no block elements', () => {
    // ⚠️ The spacing guarantee. A <p> here adds margin the field did not
    // allocate, and the symptom is a subtly misaligned form rather than
    // anything a text assertion would catch.
    const { container } = render(<FieldDescription>{'First line.\n\nSecond paragraph with **bold**.'}</FieldDescription>);
    expect(container.querySelector('p')).toBeNull();
    expect(container.querySelector('h1,h2,h3,h4,h5,h6')).toBeNull();
    expect(container.querySelector('pre')).toBeNull();
    // Both paragraphs' text still shows.
    expect(container.textContent).toContain('First line.');
    expect(container.textContent).toContain('Second paragraph');
  });

  it('keeps a list structured, since a description using one wants the breaks', () => {
    const { container } = render(<FieldDescription>{'- one\n- two'}</FieldDescription>);
    expect(container.querySelectorAll('li')).toHaveLength(2);
  });

  it('opens a link away from the form', () => {
    const { container } = render(<FieldDescription>{'see [the docs](https://example.com)'}</FieldDescription>);
    const link = container.querySelector('a');
    expect(link?.getAttribute('target')).toBe('_blank');
    expect(link?.getAttribute('rel')).toContain('noopener');
  });

  it('renders nothing for an absent or blank description', () => {
    expect(render(<FieldDescription />).container.textContent).toBe('');
    expect(render(<FieldDescription>{'   '}</FieldDescription>).container.textContent).toBe('');
  });

  it('keeps the ⚠️ marker the descriptions use for warnings', () => {
    render(<FieldDescription>{'⚠️ **Overrides Section splitting.**'}</FieldDescription>);
    expect(screen.getByText(/Overrides Section splitting/)).toBeTruthy();
  });
});

describe('stripInlineMarkdown', () => {
  // For Cloudscape's ExpandableSection.headerDescription, which is typed
  // `string` and so cannot take a node. Stripping is the honest fallback;
  // leaving the markers visible in one place is the defect being removed.
  it('removes bold markers', () => {
    expect(stripInlineMarkdown('**Granularity** decides')).toBe('Granularity decides');
  });

  it('removes code backticks', () => {
    expect(stripInlineMarkdown('set `granularity`')).toBe('set granularity');
  });

  it('keeps a link label and drops the url', () => {
    expect(stripInlineMarkdown('see [the docs](https://example.com) now')).toBe('see the docs now');
  });

  it('leaves plain prose untouched', () => {
    const plain = 'Splits the file into documents and assigns a class to each.';
    expect(stripInlineMarkdown(plain)).toBe(plain);
  });

  it('does not eat an underscore inside an identifier', () => {
    // `single_pass` and `task_prompt` appear throughout these descriptions, and
    // a naive italics rule turns them into `singlepass`.
    expect(stripInlineMarkdown("'single_pass' reads task_prompt")).toBe("'single_pass' reads task_prompt");
  });

  it('does not eat an asterisk that is not emphasis', () => {
    expect(stripInlineMarkdown('2 * 3 pages')).toBe('2 * 3 pages');
  });
});

describe('angle-bracket tokens survive the renderer', () => {
  /**
   * ⚠️ These are regression cases, not hypotheticals. `SafeMarkdown` runs
   * `rehype-raw` before `rehype-sanitize`, so before `escapeAngleBrackets` the
   * renderer parsed `<class-confidence>` as an unknown HTML element and the
   * sanitiser removed it — deleting exactly the token the sentence is about.
   * Measured on the real component: "Keep the <class-confidence> wrapper"
   * rendered as "Keep the  wrapper" and `<<CACHEPOINT>>` rendered as `<>`.
   *
   * Seven descriptions in `patterns/unified/template.yaml` mention
   * `<<CACHEPOINT>>` and these tokens are the vocabulary of every prompt
   * setting, so this is help text across every config panel rather than one
   * field.
   */
  const TOKENS = [
    'Use <<CACHEPOINT>> to mark the static prefix',
    'Keep the <class-confidence> wrapper',
    'Spliced before <document-ocr-data> if present',
    'Both <page-text> and </page-text> are kept',
  ];

  for (const text of TOKENS) {
    it(`renders ${JSON.stringify(text)} unchanged`, () => {
      const { container } = render(<FieldDescription>{text}</FieldDescription>);
      expect(container.textContent).toBe(text);
    });
  }

  it('leaves a code span exactly as written', () => {
    // Entities are literal inside a code span, so escaping there would show
    // `&lt;` to the user. That is why the escaper splits code spans out.
    const { container } = render(<FieldDescription>{'Write `<<CACHEPOINT>>` here'}</FieldDescription>);
    expect(container.textContent).toBe('Write <<CACHEPOINT>> here');
  });

  it('still renders the markdown around a token', () => {
    const { container } = render(<FieldDescription>{'**Cost.** `<<CACHEPOINT>>` caches the prefix'}</FieldDescription>);
    expect(container.textContent).toBe('Cost. <<CACHEPOINT>> caches the prefix');
    expect(container.querySelector('strong')).not.toBeNull();
  });

  it('escapes outside code spans only', () => {
    expect(escapeAngleBrackets('a <b> `<c>` <d>')).toBe('a &lt;b&gt; `<c>` &lt;d&gt;');
  });

  it('leaves text with no angle brackets untouched', () => {
    expect(escapeAngleBrackets('plain prose')).toBe('plain prose');
  });

  it('keeps the tokens in the string-only path too', () => {
    // `headerDescription` takes a string, so these descriptions reach the user
    // through `stripInlineMarkdown` as well — which must not lose them either.
    expect(stripInlineMarkdown('Use `<<CACHEPOINT>>` to mark the prefix')).toBe('Use <<CACHEPOINT>> to mark the prefix');
    expect(stripInlineMarkdown('Keep the <class-confidence> wrapper')).toBe('Keep the <class-confidence> wrapper');
  });
});
