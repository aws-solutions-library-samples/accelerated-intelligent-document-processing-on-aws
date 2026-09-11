// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The class-config fallback chain, which shipped wrong and silently (#662).
 *
 * A baseline with no config stamp fell through to `'default'`, and `'default'`
 * resolves to the built-in preset — so a set of hand-uploaded ground truth was
 * offered the lending-package classes whatever the deployment ran, with the
 * document's real class shown as "Not defined in this config version". Nothing
 * failed and nothing said so, because the config badge was hidden in exactly
 * that case.
 */

import { describe, expect, it } from 'vitest';

import { describeClassConfigSource, resolveClassConfigVersion } from '../classConfigVersion';

describe('resolveClassConfigVersion', () => {
  it("prefers the baseline's own stamp over everything else", () => {
    // Those labels were produced by that config, so its class list is the one
    // they can legitimately be changed within — even if the deployment moved on.
    expect(resolveClassConfigVersion('bank-statement-nosplit', 'set-config', 'active-config')).toEqual({
      version: 'bank-statement-nosplit',
      source: 'baseline',
    });
  });

  it("falls back to the test set's declared version when the baseline has no stamp", () => {
    expect(resolveClassConfigVersion(undefined, 'set-config', 'active-config')).toEqual({
      version: 'set-config',
      source: 'testSet',
    });
  });

  it('falls back to the ACTIVE config rather than the built-in preset', () => {
    // The bug. Previously this landed on 'default' — the built-in preset — so an
    // uploaded test set was offered lending-package classes on a deployment
    // running something else entirely.
    expect(resolveClassConfigVersion(undefined, undefined, 'my-deployment-config')).toEqual({
      version: 'my-deployment-config',
      source: 'active',
    });
  });

  it('uses the built-in default only when there is no active version at all', () => {
    expect(resolveClassConfigVersion(undefined, undefined, undefined)).toEqual({
      version: 'default',
      source: 'default',
    });
  });

  it('treats a blank stamp as absent, not as a config named ""', () => {
    // An empty config_version on a baseline is missing data. Honouring it would
    // request a nonexistent config and offer no classes at all.
    expect(resolveClassConfigVersion('', '  ', 'active-config')).toEqual({
      version: 'active-config',
      source: 'active',
    });
    expect(resolveClassConfigVersion(null, null, null)).toEqual({ version: 'default', source: 'default' });
  });
});

describe('describeClassConfigSource', () => {
  it('names a fallback as a fallback, so it cannot pass for the config in force', () => {
    // The silence is half the bug: the badge used to be hidden whenever the
    // version was 'default'.
    expect(describeClassConfigSource({ version: 'v2', source: 'baseline' })).toBe('v2');
    expect(describeClassConfigSource({ version: 'v2', source: 'testSet' })).toBe('v2 (test set)');
    expect(describeClassConfigSource({ version: 'v2', source: 'active' })).toBe('v2 (active config)');
    expect(describeClassConfigSource({ version: 'default', source: 'default' })).toBe('built-in default');
  });
});
