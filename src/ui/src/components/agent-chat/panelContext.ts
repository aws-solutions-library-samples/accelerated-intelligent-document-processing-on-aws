// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

const OPEN_TAG = '<panel-context>';
const CLOSE_TAG = '</panel-context>';
const BLOCK_SEPARATOR = '\n\n';
const PANEL_CONTEXT_BLOCKS = /<panel-context>[\s\S]*?<\/panel-context>/g;
const PANEL_CONTEXT_MARKERS = /<\/?panel-context>/g;

/**
 * What the user typed, as it is sent: without `<panel-context>` markers, which only the
 * panel writes. A marker left in would hide the user's own words from their transcript,
 * because the chat leaves out everything between a pair of them.
 */
export const typedText = (typed: string): string => typed.replace(PANEL_CONTEXT_MARKERS, '');

/**
 * A message for the agent that the user sees only `visible` of: `context` is wrapped in
 * a `<panel-context>` block, which the chat, the session title and the session list
 * leave out. `idp_common.agents.common.chat_text` and the agent chat resolver strip the
 * same blocks on the server. The markers are removed from both parts first: `context`
 * carries names Discovery took from the user's documents, and a marker inside one
 * would close the block early, while one in `visible` would hide what the user typed.
 */
export const withPanelContext = (visible: string, context: string): string => {
  const shown = typedText(visible);
  const block = OPEN_TAG + context.replace(PANEL_CONTEXT_MARKERS, '') + CLOSE_TAG;
  return shown ? shown + BLOCK_SEPARATOR + block : block;
};

/** What the sender of a chat message sees of it: the text without its `<panel-context>` blocks. */
export const visibleChatText = (content: string): string => content.replace(PANEL_CONTEXT_BLOCKS, '').trim();

const CLASS_NAME_INVALID_CHARS = /[^a-zA-Z0-9_-]/g;
const HYPHEN_RUN = /-+/g;
const EDGE_HYPHENS = /^-+|-+$/g;

/** Longest document type name a `<panel-context>` block carries: the backend's `MAX_DISCOVERED_CLASS_NAME_CHARS`. */
export const MAX_CONTEXT_CLASS_NAME_CHARS = 64;

/**
 * The document type names Discovery reported, as the class names a `<panel-context>` block
 * may carry: letters, digits, `-` and `_` only, the character set of a class id
 * (`idp_common.config.class_names`), at most {@link MAX_CONTEXT_CLASS_NAME_CHARS} long. Any
 * other character becomes a hyphen, and a name with nothing left is dropped. Discovery's
 * job record already holds names of this form; the panel applies the rule again because
 * the names come from the user's documents and the block goes to the Quick Start agent.
 */
export const contextClassNames = (names: ReadonlyArray<unknown>): string[] =>
  names
    .filter((name): name is string => typeof name === 'string')
    .map((name) =>
      name
        .replace(CLASS_NAME_INVALID_CHARS, '-')
        .replace(HYPHEN_RUN, '-')
        .replace(EDGE_HYPHENS, '')
        .slice(0, MAX_CONTEXT_CLASS_NAME_CHARS)
        .replace(EDGE_HYPHENS, ''),
    )
    .filter((name) => name.length > 0);
