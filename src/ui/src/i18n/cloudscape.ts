// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useEffect, useState } from 'react';
import { importMessages, type I18nProviderProps } from '@cloudscape-design/components/i18n';
import { ConsoleLogger } from 'aws-amplify/utils';
import { DEFAULT_LOCALE, type Locale } from './locales';

const logger = new ConsoleLogger('CloudscapeMessages');

export type CloudscapeMessages = ReadonlyArray<I18nProviderProps.Messages>;

/**
 * No messages: every Cloudscape built-in string keeps the value the component ships
 * with, and every `i18nStrings` prop the app leaves undefined stays undefined. This is
 * what `en` always gets. Cloudscape's own `all.en` bundle is never loaded: it would fill
 * props that are undefined today (alert dismiss labels, pagination labels, ...) and change
 * English DOM and aria text.
 */
export const EMPTY_CLOUDSCAPE_MESSAGES: CloudscapeMessages = Object.freeze([]);

/** Locales whose bundle failed to load, so the failure is reported once. */
const failed = new Set<string>();

interface LoadedMessages {
  locale: Locale;
  messages: CloudscapeMessages;
}

/**
 * The Cloudscape messages for `locale`, loaded lazily so the French bundle ships as its
 * own async chunk that English users never download. Children render at once with
 * `EMPTY_CLOUDSCAPE_MESSAGES` while a bundle loads.
 *
 * `importMessages` walks the tag down to its language (`fr-CA` → `fr`), so Cloudscape's
 * `fr` bundle needs no mapping. `en-XA` (the pseudo-locale, built only on flagged builds)
 * has no Cloudscape bundle, so the same walk lands on `en` and the pseudo-locale shows
 * Cloudscape's English built-ins; acceptable for a test build.
 *
 * A rejected import (the chunk unreachable, say) is logged once per locale and the
 * English built-ins stay: a missing bundle costs labels, never the screen.
 */
export const useCloudscapeMessages = (locale: Locale): CloudscapeMessages => {
  const [loaded, setLoaded] = useState<LoadedMessages | null>(null);

  useEffect(() => {
    if (locale === DEFAULT_LOCALE) return undefined;
    // Cancelled when the locale changes before the bundle arrives, so a slow `fr-CA`
    // resolution cannot overwrite a later choice.
    let current = true;
    importMessages(locale).then(
      (messages) => {
        if (current) setLoaded({ locale, messages });
      },
      (error: unknown) => {
        if (failed.has(locale)) return;
        failed.add(locale);
        logger.warn(`Cloudscape messages for ${locale} did not load; its English built-ins stay`, error);
      },
    );
    return () => {
      current = false;
    };
  }, [locale]);

  // The state remembers which locale it holds, so the render right after a switch (before
  // the effect runs) already returns the right table: `en` never sees another locale's
  // messages, and switching back to a loaded locale needs no reload to be correct.
  return loaded !== null && loaded.locale === locale ? loaded.messages : EMPTY_CLOUDSCAPE_MESSAGES;
};
