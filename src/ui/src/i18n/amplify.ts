// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { translations } from '@aws-amplify/ui-react';
import { I18n } from 'aws-amplify/utils';
import type { Locale } from './locales';

let registered = false;

/**
 * Give Amplify UI (the sign-in form) its built-in French dictionary. Idempotent: the
 * dictionary is registered once per page load however many times this runs.
 *
 * `I18n.get` tries the full tag first and then its language subtag, so `fr-CA` picks up
 * this `fr` dictionary. PR 8 adds a `fr-CA` override dictionary here (Québec terms such as
 * "courriel") through `I18n.putVocabulariesForLanguage('fr-CA', ...)`; it layers on top of
 * `fr` by the same lookup. No English dictionary is registered: under `en`, `I18n.get`
 * finds nothing and Amplify's own English defaults render, exactly as before i18n.
 */
export const registerAmplifyVocabularies = (): void => {
  if (registered) return;
  I18n.putVocabulariesForLanguage('fr', translations.fr);
  registered = true;
};

/**
 * Point Amplify UI at the resolved locale. Must run before the Authenticator first
 * renders, and again on every switch: once any vocabulary exists, `I18n.get` defaults
 * its language to `navigator.language` on first use, so without this a French browser
 * would get a French sign-in form while the app resolved `en` (decision D19 of #1770).
 *
 * `setLanguage` triggers no re-render by itself. The provider's state change re-renders
 * the subtree, which re-runs the Authenticator's `translate()`; a switcher never sits on
 * the sign-in page in this series, so no `key={locale}` remount is needed.
 */
export const applyAmplifyLanguage = (locale: Locale): void => {
  I18n.setLanguage(locale);
};
