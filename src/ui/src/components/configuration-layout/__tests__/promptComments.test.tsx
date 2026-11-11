// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Prompt comments ({# ... #}) in the preview. The rules mirror
 * idp_common/prompt_comments.py; the expectations below are the outputs the
 * Python suite asserts for the same input (tests/unit/test_prompt_comments.py).
 */

import React from 'react';
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import PromptPreview from '../PromptPreview';
import { isPromptKey, stripPromptComments, stripPromptCommentsInConfig } from '../promptComments';

describe('stripPromptComments', () => {
  it('returns a template without comments unchanged', () => {
    const template = 'Classify {DOCUMENT_TEXT}.\n\n  Keep 90% of <<CACHEPOINT>> {x}';
    expect(stripPromptComments(template)).toBe(template);
  });

  it('removes an inline comment', () => {
    expect(stripPromptComments('Classify {# note #}the page')).toBe('Classify the page');
  });

  it('removes a comment line with its line break', () => {
    expect(stripPromptComments('{# must return JSON #}\nClassify.\n  {# indented #}  \nDone')).toBe('Classify.\nDone');
  });

  it('removes a comment that spans lines', () => {
    expect(stripPromptComments('{# Contract:\n  returns {DOCUMENT_TEXT}\n  <<CACHEPOINT>> #}\nBody')).toBe('Body');
  });

  it('removes a trailing comment without a line break', () => {
    expect(stripPromptComments('Body\n{# end #}')).toBe('Body\n');
  });

  it('removes several comments', () => {
    expect(stripPromptComments('a{#1#}b{#2#}c')).toBe('abc');
  });

  it.each([
    ['{# a #}Keep this{# b #}\nBody', 'Keep this\nBody'],
    ['{# note #} Classify the page.\n{# x #}\nBody', ' Classify the page.\nBody'],
    ['Keep {# a #} this #} too\nBody', 'Keep  this #} too\nBody'],
    ['{# a #} {# b #}\nBody', 'Body'],
  ])('never extends a comment past its own close: %j', (template, expected) => {
    expect(stripPromptComments(template)).toBe(expected);
  });

  it('leaves an unterminated opener as written', () => {
    expect(stripPromptComments('Body {# never closed')).toBe('Body {# never closed');
  });

  it('handles Windows line breaks', () => {
    expect(stripPromptComments('{# c #}\r\nBody')).toBe('Body');
  });
});

describe('stripPromptCommentsInConfig', () => {
  it('strips prompt values only and leaves the input untouched', () => {
    const config = {
      ocr: { task_prompt: '{# c #}\nRead {DOCUMENT_IMAGE}' },
      classes: [{ description: 'keeps {# this #}', 'x-aws-idp-class-prompt': 'A{# c #}B' }],
    };
    const before = JSON.stringify(config);
    const stripped = stripPromptCommentsInConfig(config);
    expect(JSON.stringify(config)).toBe(before);
    expect(stripped.ocr.task_prompt).toBe('Read {DOCUMENT_IMAGE}');
    expect(stripped.classes[0].description).toBe('keeps {# this #}');
    expect(stripped.classes[0]['x-aws-idp-class-prompt']).toBe('AB');
  });

  it('returns the same objects when there is nothing to strip', () => {
    const config = { classes: [{ description: 'An invoice' }], extraction: { task_prompt: 'Extract {DOCUMENT_TEXT}' } };
    expect(stripPromptCommentsInConfig(config)).toBe(config);
    const mixed = { a: { task_prompt: '{# c #}\nX' }, b: { task_prompt: 'Y' } };
    const out = stripPromptCommentsInConfig(mixed);
    expect(out).not.toBe(mixed);
    expect(out.b).toBe(mixed.b); // untouched branch shared
    expect(out.a.task_prompt).toBe('X');
  });

  it('uses the same prompt-key rule as the backend', () => {
    expect(isPromptKey('task_prompt_bbox')).toBe(true);
    expect(isPromptKey('x-aws-idp-extraction-task-prompt')).toBe(true);
    expect(isPromptKey('custom_prompt_lambda_arn')).toBe(false);
    expect(isPromptKey('description')).toBe(false);
  });
});

describe('PromptPreview with commented prompts', () => {
  it('shows the prompt as sent, without its comments', async () => {
    render(
      <PromptPreview
        formValues={{
          classes: [{ $id: 'Invoice', type: 'object', description: 'An invoice', properties: {} }],
          extraction: {
            model: 'us.anthropic.claude-sonnet-5',
            system_prompt: '{# SYSTEM-NOTE: never sent #}\nYou are a document extraction expert.',
            task_prompt: '{# TASK-NOTE: never sent #}\nExtract {DOCUMENT_CLASS} from {DOCUMENT_TEXT}',
          },
        }}
      />,
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { expanded: false, name: /Processing Step/i }));
    await user.click(await screen.findByRole('option', { name: /^Extraction$/ }));

    const tab = async (label: RegExp): Promise<string> => {
      await user.click(screen.getByText(label));
      return document.querySelector('.prompt-preview-content')?.textContent ?? '';
    };

    // The rendered prompts are what is sent: no comment text.
    const task = await tab(/^Task Prompt/);
    expect(task).toContain('Extract');
    expect(task).not.toContain('TASK-NOTE');
    const system = await tab(/^System Prompt/);
    expect(system).toContain('You are a document extraction expert.');
    expect(system).not.toContain('SYSTEM-NOTE');
    expect(task + system).not.toContain('{#');

    // The Raw tabs show the template as typed, comments included.
    expect(await tab(/^Raw Task Template$/)).toContain('TASK-NOTE');
    expect(await tab(/^Raw System Template$/)).toContain('SYSTEM-NOTE');
  });
});
