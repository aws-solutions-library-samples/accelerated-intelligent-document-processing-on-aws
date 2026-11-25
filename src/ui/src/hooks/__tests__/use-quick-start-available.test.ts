// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import useQuickStartAvailable from '../use-quick-start-available';

const { role } = vi.hoisted(() => ({ role: { isAdmin: true } }));
vi.mock('../use-user-role', () => ({ default: () => role }));

const available = (flag: string, isAdmin: boolean) => {
  vi.stubEnv('VITE_ENABLE_QUICK_START_WIDGET', flag);
  role.isAdmin = isAdmin;
  return renderHook(() => useQuickStartAvailable()).result.current;
};

describe('useQuickStartAvailable', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    role.isAdmin = true;
  });

  it('offers Quick Start to an Admin when the stack leaves it on', () => {
    expect(available('true', true)).toBe(true);
  });

  it('offers it to nobody else, since only an Admin may create a profile', () => {
    expect(available('true', false)).toBe(false);
  });

  it('offers it to nobody when the stack turns it off', () => {
    expect(available('false', true)).toBe(false);
  });
});
