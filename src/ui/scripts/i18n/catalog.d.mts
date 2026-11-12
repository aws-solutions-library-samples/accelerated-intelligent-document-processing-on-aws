// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Types of ./catalog.mjs for the TypeScript callers (the Vitest suite).
import type { MessageFormatElement } from '@formatjs/icu-messageformat-parser';

/** Length of the `sourceHash` recorded in a translation entry. */
export declare const SOURCE_HASH_LENGTH: 16;

/** First 16 hex characters of SHA-256 over the NFC-normalised UTF-8 English text. */
export declare const sourceHash: (text: string) => string;

/** Read and parse a catalog file; throws when it is unreadable, malformed, or not a JSON object. */
export declare const readCatalog: (path: string) => Record<string, unknown>;

export interface CompiledCatalog {
  /** Every entry that passed, parsed to the AST react-intl consumes. */
  messages: Record<string, MessageFormatElement[]>;
  /** Empty when every entry passed; each problem names the catalog and the message id. */
  problems: string[];
}

/** Compile `<locale>.json` against `en.json`, judging every entry with `validateMessage`. */
export declare const compileCatalog: (
  enJson: Record<string, unknown>,
  localeJson: Record<string, unknown>,
  locale: string,
) => CompiledCatalog;
