// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The sign-in redirect has to hand back the exact address the visitor asked for.
 *
 * Routes.tsx reads it with `URLSearchParams.get`, which decodes the value once. The
 * value used to be interpolated raw, so a document id's `%2F` came back as `/` and
 * `#/documents/<folder>%2F<file>.pdf/edit` reopened as a path with an extra segment
 * that no route matched: a blank page after every reload of a document in a folder.
 * These tests drive the real UnauthRoutes through a router and read the value back
 * the way Routes.tsx does, so they fail on the raw interpolation.
 */

import { render, screen } from '@testing-library/react';
import React from 'react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

vi.mock('@aws-amplify/ui-react', () => ({
  Authenticator: () => null,
}));
vi.mock('aws-amplify/auth', () => ({ signInWithRedirect: vi.fn() }));

import UnauthRoutes, { redirectSearch } from '../UnauthRoutes';
import { LOGIN_PATH, REDIRECT_URL_PARAM } from '../constants';

const LoginProbe = (): React.JSX.Element => {
  const location = useLocation();
  // Exactly what Routes.tsx does with it.
  const redirect = new URLSearchParams(location.search).get(REDIRECT_URL_PARAM);
  return <div data-testid="redirect">{redirect}</div>;
};

const UnauthRoutesAtLocation = (): React.JSX.Element => {
  const location = useLocation();
  return <UnauthRoutes location={location} />;
};

const redirectFor = (entry: string): string | null => {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path={LOGIN_PATH} element={<LoginProbe />} />
        <Route path="*" element={<UnauthRoutesAtLocation />} />
      </Routes>
    </MemoryRouter>,
  );
  return screen.getByTestId('redirect').textContent;
};

describe('sign-in redirect', () => {
  it('returns a document id with an encoded slash unchanged', () => {
    const entry = '/documents/docs-samples%2Flending_package.pdf/edit?section=1';

    expect(redirectFor(entry)).toBe(entry);
  });

  it('keeps every parameter of a query that has more than one', () => {
    const entry = '/test-studio?tab=results&testRunId=Fake-W2-Tax-Forms-20261014-222111';

    expect(redirectFor(entry)).toBe(entry);
  });

  it('encodes the value rather than interpolating it', () => {
    const search = redirectSearch({ pathname: '/documents/a%2Fb.pdf', search: '?x=1&y=2' });

    expect(new URLSearchParams(search).get(REDIRECT_URL_PARAM)).toBe('/documents/a%2Fb.pdf?x=1&y=2');
    expect(new URLSearchParams(search).has('y')).toBe(false);
  });
});
