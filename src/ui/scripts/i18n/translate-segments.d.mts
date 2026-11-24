// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Types of ./translate-segments.mjs for the TypeScript callers (the Vitest suite).
import type { MessageFormatElement } from '@formatjs/icu-messageformat-parser';

/** One sentence of a hoisted message, as the HTML a translation engine receives. */
export interface Sentence {
  html: string;
  /** False for a sentence with no text outside its protected spans (such as `#`); it is kept as it is. */
  translate: boolean;
}

/** The sentences of one English message. Throws on malformed ICU, or a plural or select inside a tag. */
export declare const splitMessage: (english: string) => Sentence[];

/** One message rebuilt from the engine's answers (one per sentence, in order) and checked against its English. */
export declare const joinMessage: (
  id: string,
  english: string,
  answers: string[],
  locale: string,
) => { id: string; message: string } | { id: string; errors: string[] };

/** Canadian French typography for one rebuilt sentence; changes its literals in place. */
export declare const canadianFrenchTypography: (sentence: MessageFormatElement[]) => void;

/** Typography per locale; a locale with no entry keeps the engine's text. */
export declare const TYPOGRAPHY: Readonly<Record<string, (sentence: MessageFormatElement[]) => void>>;

export interface PendingEntry {
  id: string;
  english: string;
  /** `sourceHash(english)`, what the written entry records. */
  sourceHash: string;
  /** True when the catalog has an entry whose English changed; false when it has none. */
  stale: boolean;
  sentences: Sentence[];
}

export interface TranslationPlan {
  locale: string;
  sourceKeys: number;
  pending: PendingEntry[];
  /** Entries left for a later run by `limit`. */
  deferred: number;
  /** Entries that cannot be split for an engine, with the reason; they need a hand translation. */
  unsplittable: { id: string; reason: string }[];
  /** What `coverage.mjs` reports and this tool leaves alone. */
  invalid: string[];
  problems: string[];
  dead: string[];
}

/** The entries of `<locale>.json` that need a translation (missing or stale), with the sentences to send. */
export declare const planTranslation: (
  enJson: Record<string, unknown>,
  localeJson: Record<string, unknown>,
  locale: string,
  options?: { limit?: number | null },
) => TranslationPlan;
