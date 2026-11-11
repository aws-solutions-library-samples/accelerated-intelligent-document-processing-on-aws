// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Prompt-template comments (`{# ... #}`), removed before a prompt is sent.
 *
 * Mirrors `idp_common/prompt_comments.py` so the prompt preview shows what the
 * backend sends: the configuration editor keeps comments, the request never
 * carries them. Same rules as the Python side:
 * - a comment runs from `{#` to the next `#}` and may span lines;
 * - a line holding only a comment (and whitespace) is removed with its line break;
 * - `{#` with no closing `#}` is left as written;
 * - a template without `{#` is returned unchanged.
 */

// One comment, "tempered" so it always ends at its own first `#}`. COMMENT_LINE
// needs that: with a lazy `[\s\S]*?` body, a line such as `{# a #}Keep{# b #}`
// would fail the line terminator after the first `#}`, and the comment would be
// extended to the next one, removing `Keep` with it.
const ONE_COMMENT = String.raw`\{#(?:(?!#\})[\s\S])*#\}`;
const COMMENT_LINE = new RegExp(`^[ \\t]*(?:${ONE_COMMENT}[ \\t]*)+(?:\\r?\\n|$(?![\\s\\S]))`, 'gm');
const COMMENT = new RegExp(ONE_COMMENT, 'g');

export function stripPromptComments(template: string): string {
  if (typeof template !== 'string' || !template.includes('{#')) return template;
  return template.replace(COMMENT_LINE, '').replace(COMMENT, '');
}

/** Same key rule as `is_prompt_key`: names containing "prompt", except `*_arn`. */
export function isPromptKey(key: string): boolean {
  const lowered = key.toLowerCase();
  return lowered.includes('prompt') && !lowered.endsWith('_arn');
}

/**
 * A nested object with comments removed from every prompt value. Sharing is
 * structural: a branch with nothing to strip is returned as the same object, so
 * a configuration without comments costs one walk and no allocation (the
 * Prompt Preview runs this on every edit of the form).
 */
export function stripPromptCommentsInConfig<T>(node: T): T {
  if (Array.isArray(node)) {
    let changed = false;
    const out = node.map((item) => {
      const next = stripPromptCommentsInConfig(item);
      if (next !== item) changed = true;
      return next;
    });
    return (changed ? out : node) as unknown as T;
  }
  if (node && typeof node === 'object') {
    let out: Record<string, unknown> | null = null;
    const entries = Object.entries(node as Record<string, unknown>);
    for (const [key, value] of entries) {
      const next = isPromptKey(key) && typeof value === 'string' ? stripPromptComments(value) : stripPromptCommentsInConfig(value);
      if (next !== value) {
        if (!out) out = { ...(node as Record<string, unknown>) };
        out[key] = next;
      }
    }
    return (out ?? node) as T;
  }
  return node;
}
