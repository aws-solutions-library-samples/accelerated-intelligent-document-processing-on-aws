// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import Alert from '@cloudscape-design/components/alert';
import { importMessages } from '@cloudscape-design/components/i18n';
import frMessages from '@cloudscape-design/components/i18n/messages/all.fr';
import Pagination from '@cloudscape-design/components/pagination';
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EMPTY_CLOUDSCAPE_MESSAGES, useCloudscapeMessages, type CloudscapeMessages } from '../cloudscape';
import { LOCALE_STORAGE_KEY, type Locale } from '../locales';
import { LocaleProvider, useLocale } from '../LocaleProvider';

vi.mock('@cloudscape-design/components/i18n', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@cloudscape-design/components/i18n')>();
  return { ...actual, importMessages: vi.fn(actual.importMessages) };
});

const EN_FR: readonly Locale[] = ['en', 'fr-CA'];

// Exact values from node_modules/@cloudscape-design/components/i18n/messages/all.fr.json.
// Alert resolves its dismiss label through the deprecated `dismissAriaLabel` key first, which
// carries the ASCII apostrophe; `i18nStrings.dismissAriaLabel` (typographic apostrophe) is
// only reached when that key is absent.
const FR_DISMISS_ALERT = "Ignorer l'alerte";
const FR_PREVIOUS_PAGE = 'Page précédente';
const FR_NEXT_PAGE = 'Page suivante';

/** Built-ins the app never supplies: Alert's dismiss label and Pagination's arrow labels. */
const Builtins = (): React.JSX.Element => (
  <div>
    <Alert dismissible type="info">
      hello
    </Alert>
    <Pagination currentPageIndex={1} pagesCount={3} />
  </div>
);

const Switcher = (): React.JSX.Element => {
  const { locale, setLocale } = useLocale();
  return (
    <div>
      <span data-testid="locale">{locale}</span>
      <button type="button" onClick={() => setLocale('en')}>
        choose en
      </button>
      <button type="button" onClick={() => setLocale('fr-CA')}>
        choose fr-CA
      </button>
    </div>
  );
};

/**
 * React's `useId` values and Cloudscape's performance marks (a counter plus a timestamp) differ
 * between any two renders of the same tree; nothing else may.
 */
const withoutPerRenderIds = (html: string): string =>
  html.replace(/:r[0-9a-z]+:/g, ':r:').replace(/data-analytics-performance-mark="[^"]*"/g, 'data-analytics-performance-mark=""');

const setBrowserLanguages = (languages: readonly string[]): void => {
  vi.spyOn(navigator, 'languages', 'get').mockReturnValue(languages);
};

const frenchDismissButton = (): HTMLElement | null => screen.queryByRole('button', { name: FR_DISMISS_ALERT });

describe('Cloudscape built-in messages', () => {
  beforeEach(() => {
    vi.mocked(importMessages).mockReset();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    document.documentElement.lang = '';
  });

  it('under en, imports no bundle and renders built-ins identically to no provider at all, apart from per-render ids', () => {
    setBrowserLanguages(['en-US']);
    const bare = render(<Builtins />);
    const bareHtml = bare.container.innerHTML;
    bare.unmount();
    expect(bareHtml).toContain('hello');
    expect(bareHtml).not.toContain(FR_DISMISS_ALERT);

    const wrapped = render(
      <LocaleProvider>
        <Builtins />
      </LocaleProvider>,
    );
    expect(withoutPerRenderIds(wrapped.container.innerHTML)).toBe(withoutPerRenderIds(bareHtml));
    expect(importMessages).not.toHaveBeenCalled();
  });

  it('under fr-CA, imports the fr-CA bundle lazily and built-ins then render in French', async () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'fr-CA');
    setBrowserLanguages(['en-US']);
    render(
      <LocaleProvider enabledLocales={EN_FR}>
        <Builtins />
      </LocaleProvider>,
    );
    expect(importMessages).toHaveBeenCalledTimes(1);
    expect(importMessages).toHaveBeenCalledWith('fr-CA');
    // Children render at once, with the English built-ins, while the bundle loads.
    expect(screen.getByText('hello')).toBeInTheDocument();
    expect(frenchDismissButton()).toBeNull();

    expect(await screen.findByRole('button', { name: FR_DISMISS_ALERT })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: FR_PREVIOUS_PAGE })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: FR_NEXT_PAGE })).toBeInTheDocument();
  });

  it('logs a bundle that fails to load once per locale and keeps rendering the English built-ins', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.mocked(importMessages)
      .mockRejectedValueOnce(new Error('chunk unreachable'))
      .mockRejectedValueOnce(new Error('chunk unreachable again'));
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'fr-CA');
    setBrowserLanguages(['en-US']);
    render(
      <LocaleProvider enabledLocales={EN_FR}>
        <Switcher />
        <Builtins />
      </LocaleProvider>,
    );
    expect(screen.getByText('hello')).toBeInTheDocument();
    await waitFor(() => expect(warn).toHaveBeenCalledTimes(1));
    expect(String(warn.mock.calls[0][0])).toContain('fr-CA');
    expect(frenchDismissButton()).toBeNull();

    // A second failure for the same locale is not reported again.
    fireEvent.click(screen.getByRole('button', { name: 'choose en' }));
    fireEvent.click(screen.getByRole('button', { name: 'choose fr-CA' }));
    await waitFor(() => expect(importMessages).toHaveBeenCalledTimes(2));
    await act(async () => {
      await Promise.resolve();
    });
    expect(warn).toHaveBeenCalledTimes(1);
    expect(error).not.toHaveBeenCalled();
    expect(screen.getByText('hello')).toBeInTheDocument();
    expect(frenchDismissButton()).toBeNull();
  });

  // Tested on the hook: Cloudscape keys its lookups by the current locale, so a stale French
  // table handed to the provider under `en` would leave the DOM unchanged and hide the defect.
  it('ignores a bundle that resolves after the locale switched back to en', async () => {
    let resolveFr: (messages: CloudscapeMessages) => void = () => {};
    const pending = new Promise<CloudscapeMessages>((resolve) => {
      resolveFr = resolve;
    });
    vi.mocked(importMessages).mockReturnValueOnce(pending);
    const { result, rerender } = renderHook(({ locale }: { locale: Locale }) => useCloudscapeMessages(locale), {
      initialProps: { locale: 'fr-CA' as Locale },
    });
    expect(importMessages).toHaveBeenCalledWith('fr-CA');
    expect(result.current).toBe(EMPTY_CLOUDSCAPE_MESSAGES);

    rerender({ locale: 'en' });
    await act(async () => {
      resolveFr([frMessages]);
      await pending;
    });
    expect(result.current).toBe(EMPTY_CLOUDSCAPE_MESSAGES);

    // Back to fr-CA: the stale bundle was never stored, so the table is fetched again.
    rerender({ locale: 'fr-CA' });
    expect(result.current).toBe(EMPTY_CLOUDSCAPE_MESSAGES);
    expect(importMessages).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(result.current).toEqual([frMessages]));
  });

  it('switching from en to fr-CA loads the bundle, and back to en drops it at once', async () => {
    setBrowserLanguages(['en-US']);
    render(
      <LocaleProvider enabledLocales={EN_FR}>
        <Switcher />
        <Builtins />
      </LocaleProvider>,
    );
    expect(importMessages).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'choose fr-CA' }));
    expect(importMessages).toHaveBeenCalledWith('fr-CA');
    expect(await screen.findByRole('button', { name: FR_DISMISS_ALERT })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'choose en' }));
    expect(frenchDismissButton()).toBeNull();
  });

  it('hands en the frozen empty table even after another locale loaded, and the loaded table back on return', async () => {
    expect(EMPTY_CLOUDSCAPE_MESSAGES).toEqual([]);
    expect(Object.isFrozen(EMPTY_CLOUDSCAPE_MESSAGES)).toBe(true);

    const { result, rerender } = renderHook(({ locale }: { locale: Locale }) => useCloudscapeMessages(locale), {
      initialProps: { locale: 'fr-CA' as Locale },
    });
    await waitFor(() => expect(result.current).toEqual([frMessages]));

    rerender({ locale: 'en' });
    expect(result.current).toBe(EMPTY_CLOUDSCAPE_MESSAGES);

    rerender({ locale: 'fr-CA' });
    expect(result.current).toEqual([frMessages]);
  });
});
