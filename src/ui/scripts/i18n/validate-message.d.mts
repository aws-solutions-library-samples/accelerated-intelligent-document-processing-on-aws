// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Types of ./validate-message.mjs for the TypeScript callers (the Vitest suite).
import type { MessageFormatElement } from '@formatjs/icu-messageformat-parser';

/** Parse one ICU message into react-intl's AST. Throws a `SyntaxError` on malformed ICU. */
export declare const parseMessage: (message: string) => MessageFormatElement[];

export interface MessageVerdict {
  /** True only when `errors` is empty. */
  ok: boolean;
  /** Each error names the message id and says what differs. */
  errors: string[];
}

/** Judge one translated message against its English source: ICU syntax, argument and tag parity. */
export declare const validateMessage: (id: string, sourceMessage: string, translatedMessage: string) => MessageVerdict;

/** The same parity for a translation that is already parsed (a catalog a build step produced as an AST). */
export declare const validateMessageAst: (id: string, sourceMessage: string, translatedAst: MessageFormatElement[]) => MessageVerdict;
