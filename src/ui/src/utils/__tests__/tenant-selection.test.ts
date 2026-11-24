// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import {
  TENANT_HINT_KEY,
  clearTenantHint,
  needsTenantChoice,
  readTenantHint,
  tenantToAutoSelect,
  writeTenantHint,
} from '../tenant-selection';

const memoryStorage = (): Storage => {
  const data = new Map<string, string>();
  return {
    get length() {
      return data.size;
    },
    clear: () => data.clear(),
    getItem: (k) => data.get(k) ?? null,
    key: (i) => Array.from(data.keys())[i] ?? null,
    removeItem: (k) => void data.delete(k),
    setItem: (k, v) => void data.set(k, v),
  };
};

describe('needsTenantChoice', () => {
  it('is true only for the no-tenant marker with two or more tenants on offer', () => {
    expect(needsTenantChoice(['!no-tenant-granted'], ['acme', 'globex'])).toBe(true);
  });

  it.each([
    ['one tenant on offer', ['!no-tenant-granted'], ['acme']],
    ['nothing on offer', ['!no-tenant-granted'], []],
    ['a tenant already active', ['acme-*'], ['acme', 'globex']],
    ['no scope at all (an Admin, or an unrestricted user)', null, ['acme', 'globex']],
    ['the marker among other values', ['!no-tenant-granted', 'acme-*'], ['acme', 'globex']],
  ])('is false for %s', (_name, scope, eligible) => {
    expect(needsTenantChoice(scope as string[] | null, eligible as string[])).toBe(false);
  });
});

describe('needsTenantChoice with fields a caller does not supply', () => {
  it('is false rather than throwing', () => {
    expect(needsTenantChoice(undefined, undefined)).toBe(false);
    expect(needsTenantChoice(null, null)).toBe(false);
    expect(needsTenantChoice(['!no-tenant-granted'], undefined)).toBe(false);
  });
});

describe('the remembered choice', () => {
  it('is returned to the user who made it', () => {
    const storage = memoryStorage();
    writeTenantHint('sub-1', 'globex', storage);
    expect(readTenantHint('sub-1', storage)).toBe('globex');
  });

  it("is never returned to another user's session", () => {
    const storage = memoryStorage();
    writeTenantHint('sub-1', 'globex', storage);
    expect(readTenantHint('sub-2', storage)).toBeNull();
  });

  it.each([
    ['not JSON', 'globex'],
    ['no tenant', JSON.stringify({ sub: 'sub-1' })],
    ['an empty tenant', JSON.stringify({ sub: 'sub-1', tenantId: '' })],
    ['a non-string tenant', JSON.stringify({ sub: 'sub-1', tenantId: 7 })],
  ])('is ignored when stored as %s', (_name, raw) => {
    const storage = memoryStorage();
    storage.setItem(TENANT_HINT_KEY, raw);
    expect(readTenantHint('sub-1', storage)).toBeNull();
  });

  it('is ignored with no subject or no storage', () => {
    const storage = memoryStorage();
    writeTenantHint('sub-1', 'globex', storage);
    expect(readTenantHint(null, storage)).toBeNull();
    expect(readTenantHint('sub-1', null)).toBeNull();
    writeTenantHint(null, 'globex', storage);
    expect(storage.length).toBe(1);
  });

  it('is cleared on request', () => {
    const storage = memoryStorage();
    writeTenantHint('sub-1', 'globex', storage);
    clearTenantHint(storage);
    expect(readTenantHint('sub-1', storage)).toBeNull();
  });

  it('survives storage that throws', () => {
    const broken = {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      },
      removeItem: () => {
        throw new Error('blocked');
      },
    } as unknown as Storage;
    expect(() => writeTenantHint('sub-1', 'acme', broken)).not.toThrow();
    expect(readTenantHint('sub-1', broken)).toBeNull();
    expect(() => clearTenantHint(broken)).not.toThrow();
  });
});

describe('tenantToAutoSelect', () => {
  it('applies a remembered tenant only while it is still on offer', () => {
    expect(tenantToAutoSelect('acme', ['acme', 'globex'])).toBe('acme');
    expect(tenantToAutoSelect('initech', ['acme', 'globex'])).toBeNull();
    expect(tenantToAutoSelect(null, ['acme', 'globex'])).toBeNull();
  });
});
