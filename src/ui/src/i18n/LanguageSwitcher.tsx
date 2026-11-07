// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import type { TopNavigationProps } from '@cloudscape-design/components';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';
import { LOCALE_DISPLAY_NAMES } from './locales';
import { useLocale } from './LocaleProvider';

/**
 * The switcher's own strings, declared the way every UI message is: next to the code that
 * renders them, with an explicit stable id (dotted, by area), the English inline as
 * `defaultMessage`, and a `description` for the translator. `npm run i18n:extract` collects
 * them into `src/i18n/en.json`. The generic names each message's ICU arguments (react-intl 12
 * types `formatMessage` values from it), so a call site that forgets `{language}` fails `tsc`.
 */
export const messages = defineMessages<{
  label: NoMessageValues;
  ariaLabel: { readonly language: string };
}>({
  label: {
    id: 'i18n.switcher.label',
    defaultMessage: 'Language',
    description:
      'Tooltip of the language menu button in the top navigation. The button itself shows the current language by its own name (English, Français (Canada)).',
  },
  ariaLabel: {
    id: 'i18n.switcher.ariaLabel',
    defaultMessage: 'Language, currently {language}',
    description: 'Screen-reader label of the language menu button. {language} is the current language by its own name, e.g. "English".',
  },
});

/**
 * The language menu of the top navigation: one item per enabled locale, each labelled in its
 * own language and the current one checked (Cloudscape's checkbox item, so the mark sits where
 * a user looks while choosing, not only in the trigger's text); choosing one calls `setLocale`.
 * Returns `null` while fewer than two locales are enabled, so the caller adds nothing to its
 * utilities and the English shell is unchanged.
 */
export const useLanguageSwitcherUtility = (): TopNavigationProps.Utility | null => {
  const intl = useIntl();
  const { locale, enabledLocales, setLocale } = useLocale();
  if (enabledLocales.length < 2) return null;
  const current = LOCALE_DISPLAY_NAMES[locale];
  return {
    type: 'menu-dropdown',
    iconName: 'globe',
    text: current,
    title: intl.formatMessage(messages.label),
    ariaLabel: intl.formatMessage(messages.ariaLabel, { language: current }),
    items: enabledLocales.map((candidate) => ({
      id: candidate,
      text: LOCALE_DISPLAY_NAMES[candidate],
      itemType: 'checkbox' as const,
      checked: candidate === locale,
    })),
    onItemClick: ({ detail }) => setLocale(detail.id),
  };
};
