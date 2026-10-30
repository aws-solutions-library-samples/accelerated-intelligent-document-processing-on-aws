// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { describe, expect, it } from 'vitest';
import { isBdaModeConfig } from '../processing-mode';

describe('isBdaModeConfig', () => {
  it.each([true, 'true', 'True', ' yes ', 'on', '1', 't', 'y', 1])('reads %j as BDA mode', (value) => {
    expect(isBdaModeConfig({ use_bda: value })).toBe(true);
  });

  // "false" is truthy as a string, which is the round-trip shape that a plain
  // truthiness test gets wrong.
  it.each([false, 'false', 'False', 'no', 'off', '0', 0, 'maybe', '', null, undefined])('reads %j as pipeline mode', (value) => {
    expect(isBdaModeConfig({ use_bda: value })).toBe(false);
  });

  it('reads a missing configuration as pipeline mode', () => {
    expect(isBdaModeConfig(null)).toBe(false);
    expect(isBdaModeConfig(undefined)).toBe(false);
    expect(isBdaModeConfig({})).toBe(false);
  });
});
