// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Types of the virtual modules `scripts/i18n/vite-plugin-catalogs.mjs` serves. The plugin
 * header documents their contents; this file only lets `tsc --noEmit` see them.
 */
declare module 'virtual:i18n-catalogs' {
  import type { MessageFormatElement } from '@formatjs/icu-messageformat-parser';

  /** Every locale with a `<locale>.json` catalog file in the build, sorted; `en` included. */
  export const availableLocales: readonly string[];
  /** One lazy loader per catalog other than `en`, each importing that locale's compiled chunk. */
  export const loaders: Readonly<Record<string, () => Promise<{ default: Record<string, MessageFormatElement[]> }>>>;
}

declare module 'virtual:i18n-catalog/*' {
  import type { MessageFormatElement } from '@formatjs/icu-messageformat-parser';

  const messages: Record<string, MessageFormatElement[]>;
  export default messages;
}
