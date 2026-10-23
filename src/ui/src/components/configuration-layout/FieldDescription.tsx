// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * FieldDescription — renders a config-schema `description` as inline markdown.
 *
 * The schema descriptions in `patterns/unified/template.yaml` are written with
 * markdown emphasis and code spans (`**Cost.**`, `` `granularity: packet` ``)
 * because they are also read as source. The config form passed them to
 * Cloudscape's `FormField description` slot as a plain string, so a user saw the
 * literal asterisks and backticks — which is worse than no markup, because it
 * reads as a typo in the product rather than as formatting.
 *
 * Why this wraps SafeMarkdown rather than parsing anything itself: the UI
 * already has one sanitising markdown renderer, and adding a second parser for
 * help text would be a second thing to keep safe. Descriptions are
 * repository-authored rather than user-supplied, so the sanitiser is not
 * load-bearing here — but it costs nothing and a schema description that ever
 * becomes editable (a feature-pack-supplied field, say) is then already safe.
 *
 * ⚠️ The components map is the point, not an afterthought. ReactMarkdown emits
 * block elements, and a `<p>` inside Cloudscape's description slot adds vertical
 * margin that breaks the field's spacing — one paragraph per description, and
 * several for a description containing a blank line. Every block element is
 * therefore rendered as an inline `<span>`, so the result occupies exactly the
 * space a plain string would. Lists are the one case that keeps its own
 * structure, because a description using one genuinely wants the line breaks.
 */

import React from 'react';
import type { Components } from 'react-markdown';

import SafeMarkdown from '../common/SafeMarkdown';

/**
 * Block elements rendered inline. `p` is the one that matters — ReactMarkdown
 * wraps every description in one — but headings and blockquotes are mapped too
 * so a description that picks one up by accident degrades to text rather than
 * to a 2em heading in a help slot.
 */
const INLINE_COMPONENTS: Components = {
  p: ({ children }) => <span className="config-field-description-block">{children}</span>,
  h1: ({ children }) => <span className="config-field-description-block">{children}</span>,
  h2: ({ children }) => <span className="config-field-description-block">{children}</span>,
  h3: ({ children }) => <span className="config-field-description-block">{children}</span>,
  h4: ({ children }) => <span className="config-field-description-block">{children}</span>,
  h5: ({ children }) => <span className="config-field-description-block">{children}</span>,
  h6: ({ children }) => <span className="config-field-description-block">{children}</span>,
  blockquote: ({ children }) => <span className="config-field-description-block">{children}</span>,
  // `pre` would render a code BLOCK; a description's backticks are spans, and a
  // fenced block in a help slot is never what was meant.
  pre: ({ children }) => <span className="config-field-description-block">{children}</span>,
  code: ({ children }) => <code className="config-field-description-code">{children}</code>,
  // A description's links are documentation pointers, so they open away from
  // the form rather than navigating it.
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
};

/**
 * Markdown emphasis and code markers, for the one slot that cannot take JSX.
 *
 * Cloudscape's `ExpandableSection.headerDescription` is typed `string`, so a
 * top-level section's own description cannot be rendered as markup there. Rather
 * than leave the asterisks and backticks visible in that one place — which is
 * the defect this module exists to remove — they are stripped, so the text reads
 * as prose. Section descriptions are a sentence or two by convention, so losing
 * emphasis there costs nothing.
 *
 * Deliberately narrow: emphasis, code spans and link syntax. It is not a
 * markdown parser and must not grow into one — anything needing real rendering
 * belongs in a slot that accepts a node.
 */
export function stripInlineMarkdown(text: string): string {
  return text
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1') // [label](url) -> label
    .replace(/(\*\*|__)(.*?)\1/g, '$2') // bold
    .replace(/(^|[\s(])[*_]([^*_]+)[*_](?=[\s).,;:!?]|$)/g, '$1$2') // italics
    .replace(/`([^`]+)`/g, '$1'); // code spans
}

/**
 * ⚠️ **Angle-bracket tokens have to be escaped before the markdown renderer
 * sees them, or they are deleted.** `SafeMarkdown` runs `rehype-raw` ahead of
 * `rehype-sanitize`, so `<class-confidence>` is parsed as an unknown HTML
 * element and then removed — tag and all. These descriptions are full of such
 * tokens, because the prompts they describe are full of them, and the result is
 * that the word the sentence exists to explain is the one that disappears:
 * "Keep the <class-confidence> wrapper" rendered as "Keep the  wrapper", and
 * `<<CACHEPOINT>>` rendered as `<>`. Measured, not inferred — the cases are in
 * `FieldDescription.test.tsx`.
 *
 * A schema description is prose, never HTML, so escaping is lossless here as
 * well as safer. `&lt;`/`&gt;` entities are passed through by markdown and
 * render as the characters; **inside a code span they would not be**, so code
 * spans are split out and left exactly as written. That is the only reason this
 * is not a bare `String.replace`.
 */
export function escapeAngleBrackets(text: string): string {
  return text
    .split(/(`+[^`]*`+)/g)
    .map((part, i) => (i % 2 === 1 ? part : part.replace(/</g, '&lt;').replace(/>/g, '&gt;')))
    .join('');
}

export interface FieldDescriptionProps {
  children?: string;
}

/**
 * Renders a schema description. Returns null for an absent or blank one so a
 * caller can pass it straight into a `description` slot without a guard.
 */
const FieldDescription: React.FC<FieldDescriptionProps> = ({ children }) => {
  if (!children || !children.trim()) return null;
  return (
    <span className="config-field-description">
      <SafeMarkdown components={INLINE_COMPONENTS}>{escapeAngleBrackets(children)}</SafeMarkdown>
    </span>
  );
};

export default FieldDescription;
