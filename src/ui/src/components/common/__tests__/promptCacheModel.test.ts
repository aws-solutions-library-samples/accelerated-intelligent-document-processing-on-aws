// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';

import { cacheState, describePromptCache, minCacheablePrefixTokens, modelCachesImplicitly, summarizeCacheUsage } from '../promptCacheModel';

const SONNET = 'us.anthropic.claude-sonnet-4-6';
const ASTRA = 'us.openai.gpt-6-astra';

describe('summarizeCacheUsage (mirrors idp_common.bedrock.prompt_cache)', () => {
  it('classifies reads as caching with a read share and sums escalation contexts', () => {
    const s = summarizeCacheUsage(
      {
        [`Extraction/bedrock/${SONNET}`]: { inputTokens: 100, cacheReadInputTokens: 0, cacheWriteInputTokens: 900, requests: 1 },
        'ExtractionEscalation/bedrock/us.anthropic.claude-opus-4-8': {
          inputTokens: 50,
          cacheReadInputTokens: 900,
          cacheWriteInputTokens: 0,
          requests: 1,
        },
        [`Classification/bedrock/${SONNET}`]: { inputTokens: 1, cacheReadInputTokens: 99999 },
        'OCR/textract/analyze_document': { pages: 3 },
      },
      'Extraction',
    );
    expect(s?.state).toBe('caching');
    expect(s?.cache_read_input_tokens).toBe(900);
    expect(s?.cache_write_input_tokens).toBe(900);
    expect(s?.input_tokens).toBe(150);
    expect(s?.requests).toBe(2);
    expect(s?.model_ids).toEqual(['us.anthropic.claude-opus-4-8', SONNET]);
  });

  it('is write-only when writes have no reads, never-cached at zero/zero, and inconclusive without cache units', () => {
    expect(
      summarizeCacheUsage(
        { [`Extraction/bedrock/${SONNET}`]: { inputTokens: 9, cacheReadInputTokens: 0, cacheWriteInputTokens: 1628 } },
        'Extraction',
      )?.state,
    ).toBe('write-only');
    expect(
      summarizeCacheUsage(
        { [`Extraction/bedrock/${SONNET}`]: { inputTokens: 949, cacheReadInputTokens: 0, cacheWriteInputTokens: 0 } },
        'Extraction',
      )?.state,
    ).toBe('never-cached');
    expect(summarizeCacheUsage({ 'OCR/lambda_hook/arn:x': { inputTokens: 500, pages: 2 } }, 'OCR')?.state).toBeUndefined();
    expect(
      summarizeCacheUsage({ [`Summarization/bedrock/${SONNET}`]: { inputTokens: 500, outputTokens: 20 } }, 'Summarization')?.state,
    ).toBe('no-cache-data');
  });

  it('returns null when the phase made no Bedrock call', () => {
    expect(summarizeCacheUsage({ 'OCR/textract/analyze_document': { pages: 3 } }, 'Extraction')).toBeNull();
    expect(summarizeCacheUsage({}, 'Extraction')).toBeNull();
  });

  it('exact matching keeps escalation tokens out of the Extraction row and names the minimum', () => {
    const metering = {
      [`Extraction/bedrock/${SONNET}`]: { inputTokens: 100, cacheReadInputTokens: 0, cacheWriteInputTokens: 900 },
      'ExtractionEscalation/bedrock/us.anthropic.claude-opus-4-8': { inputTokens: 50, cacheReadInputTokens: 900, cacheWriteInputTokens: 0 },
    };
    const exact = summarizeCacheUsage(metering, 'Extraction', { exact: true });
    expect(exact?.state).toBe('write-only');
    expect(exact?.cache_read_input_tokens).toBe(0);
    expect(exact?.min_cacheable_prefix_tokens).toBe(1024);
    expect(minCacheablePrefixTokens('us.anthropic.claude-haiku-4-5-20251001-v1:0')).toBe(4096);
    expect(minCacheablePrefixTokens('us.anthropic.claude-opus-5')).toBe(512);
    expect(minCacheablePrefixTokens('us.amazon.nova-lite-v1:0')).toBeNull();
  });

  it('measured caching beats the disabled flag', () => {
    expect(cacheState(5, 0, true, true)).toBe('caching');
    expect(cacheState(0, 0, true, true)).toBe('disabled');
    expect(cacheState(0, 0, false, false)).toBe('no-cache-data');
  });
});

describe('describePromptCache', () => {
  it('says what the operator should do for each state', () => {
    const base = {
      input_tokens: 949,
      cache_read_input_tokens: 0,
      cache_write_input_tokens: 0,
      requests: 1,
      read_share: 0,
      model_ids: [SONNET],
    };
    const inert = describePromptCache({ ...base, state: 'never-cached', min_cacheable_prefix_tokens: 1024 });
    expect(inert.indicator).toBe('warning');
    expect(inert.headline).toContain('never cached');
    expect(inert.detail).toContain('1,024 tokens');
    expect(inert.detail).toContain('idp-cli config validate');

    const perPhase = describePromptCache({ ...base, state: 'never-cached' }, { phaseOnly: true });
    expect(perPhase.detail).toContain('prompt_cache: off');

    const writeOnly = describePromptCache({ ...base, state: 'write-only', cache_write_input_tokens: 1628 });
    expect(writeOnly.indicator).toBe('warning');
    expect(writeOnly.headline).toContain('1.25×');

    const caching = describePromptCache({ ...base, state: 'caching', cache_read_input_tokens: 800, read_share: 0.8 });
    expect(caching.indicator).toBe('success');
    expect(caching.headline).toContain('80%');

    expect(describePromptCache({ ...base, state: 'disabled' }).indicator).toBe('stopped');
    expect(describePromptCache({ ...base, state: 'no-cache-data' }).indicator).toBe('info');
  });

  it('has a distinct state for a cache point that never reached the model', () => {
    const base = { input_tokens: 949, cache_read_input_tokens: 0, cache_write_input_tokens: 0, read_share: 0, model_ids: [SONNET] };
    const noPoint = describePromptCache({ ...base, state: 'no-cache-point', cache_point_sent: false });
    expect(noPoint.indicator).toBe('info');
    expect(noPoint.headline).toContain('no cache point reached the model');
    // Must not claim the model cannot cache — see the implicit-caching case below.
    expect(noPoint.detail).not.toContain('does not support prompt caching');
  });

  it('recognizes the models that cache without a cachePoint', () => {
    expect(modelCachesImplicitly(ASTRA)).toBe(true);
    expect(modelCachesImplicitly('global.openai.gpt-6-astra')).toBe(true);
    expect(modelCachesImplicitly('openai.gpt-5.4')).toBe(true);
    expect(modelCachesImplicitly('openai.gpt-5.5')).toBe(true);
    expect(modelCachesImplicitly(SONNET)).toBe(false);
    // GPT-5.6 caches only via an explicit breakpoint derived from a <<CACHEPOINT>>
    // marker, so "no marker" is "no caching" — it must not be called implicit.
    expect(modelCachesImplicitly('openai.gpt-5.6-sol')).toBe(false);
    // Grok advertises implicit caching but it was never observed to engage.
    expect(modelCachesImplicitly('us.xai.grok-4.6')).toBe(false);
    expect(modelCachesImplicitly(null)).toBe(false);
  });

  it('does not tell an implicit-caching model it cannot cache', () => {
    // The regression: the detail said "the model does not support prompt caching",
    // false for Astra (it caches implicitly and REJECTS an explicit cache point).
    const base = { input_tokens: 949, cache_read_input_tokens: 0, cache_write_input_tokens: 0, read_share: 0 };
    const astra = describePromptCache({ ...base, state: 'no-cache-point', cache_point_sent: false, model_ids: [ASTRA] });
    expect(astra.indicator).toBe('info');
    expect(astra.headline).toContain('implicit caching');
    expect(astra.detail).toContain('caches implicitly');
    expect(astra.detail).not.toContain('does not support prompt caching');

    // Mixed models in one phase fall back to the generic (still accurate) wording.
    const mixed = describePromptCache({
      ...base,
      state: 'no-cache-point',
      cache_point_sent: false,
      model_ids: [ASTRA, SONNET],
    });
    expect(mixed.detail).not.toContain('caches implicitly');
  });

  it('reports measured reads on an implicit model as plain caching', () => {
    // Verified live on Astra: a repeated prefix billed inputTokens=2 / read=2707.
    const s = describePromptCache({
      state: 'caching',
      input_tokens: 2,
      cache_read_input_tokens: 2707,
      cache_write_input_tokens: 0,
      read_share: 0.999,
      model_ids: [ASTRA],
    });
    expect(s.indicator).toBe('success');
    expect(s.headline).toContain('100%');
  });

  it('only names the extraction knob on Extraction rows', () => {
    const base = { input_tokens: 949, cache_read_input_tokens: 0, cache_write_input_tokens: 0, read_share: 0, model_ids: [SONNET] };
    const classification = describePromptCache({ ...base, state: 'never-cached' }, { phaseOnly: true, context: 'Classification' });
    expect(classification.detail).not.toContain('prompt_cache');
    expect(classification.detail).toContain('no cache point reached the model for this phase');
    const extraction = describePromptCache({ ...base, state: 'never-cached' }, { phaseOnly: true, context: 'Extraction' });
    expect(extraction.detail).toContain('extraction.prompt_cache: off');
    const summarizationWriteOnly = describePromptCache(
      { ...base, state: 'write-only', cache_write_input_tokens: 10 },
      { phaseOnly: true, context: 'Summarization' },
    );
    expect(summarizationWriteOnly.detail).not.toContain('prompt_cache');
  });
});
