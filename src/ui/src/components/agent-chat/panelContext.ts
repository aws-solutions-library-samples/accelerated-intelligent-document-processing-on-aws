// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

const OPEN_TAG = '<panel-context>';
const CLOSE_TAG = '</panel-context>';
const BLOCK_SEPARATOR = '\n\n';
const PANEL_CONTEXT_BLOCKS = /<panel-context>[\s\S]*?<\/panel-context>/g;

/**
 * A message for the agent that the user sees only `visible` of: `context` is wrapped in
 * a `<panel-context>` block, which the chat, the session title and the session list
 * leave out. `idp_common.agents.common.chat_text` and the agent chat resolver strip the
 * same blocks on the server.
 */
export const withPanelContext = (visible: string, context: string): string => {
  const block = OPEN_TAG + context + CLOSE_TAG;
  return visible ? visible + BLOCK_SEPARATOR + block : block;
};

/** What the sender of a chat message sees of it: the text without its `<panel-context>` blocks. */
export const visibleChatText = (content: string): string => content.replace(PANEL_CONTEXT_BLOCKS, '').trim();
