// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// Prompt-cache efficiency read back out of metering (#780 item 2). Mirrors
// idp_common.bedrock.prompt_cache.summarize_cache_usage / describe_cache_state:
// the backend records a per-section summary in the section result.json
// (metadata.prompt_cache); the per-phase view in the document cost table is
// derived here from the document's metering map, which is keyed by phase and
// model only.

export type PromptCacheState = 'caching' | 'write-only' | 'never-cached' | 'disabled' | 'no-cache-point' | 'no-cache-data';

export interface PromptCacheSummary {
  state?: PromptCacheState | string;
  input_tokens?: number;
  cache_read_input_tokens?: number;
  cache_write_input_tokens?: number;
  requests?: number | null;
  read_share?: number | null;
  model_ids?: string[];
  min_cacheable_prefix_tokens?: number | null;
  // Backend-only: whether a cache point reached the model at all.
  cache_point_sent?: boolean | null;
}

export type StatusType = 'success' | 'warning' | 'info' | 'stopped';

const num = (v: unknown): number => {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
};

// Published per-model minimum cacheable prefix; mirrors _MIN_PREFIX_TIERS in
// idp_common/bedrock/prompt_cache.py (first match wins; Nova needs no entry).
const MIN_PREFIX_TIERS: Array<[RegExp, number]> = [
  [/claude-(opus-5|fable-5)/, 512],
  [/claude-opus-4-7/, 2048],
  [/claude-(opus-4-6|opus-4-5|haiku-4-5)/, 4096],
  [/claude-(sonnet-5|sonnet-4|opus-4-8|opus-4-1|opus-4|3-7-sonnet)/, 1024],
];

export function minCacheablePrefixTokens(modelId?: string | null): number | null {
  if (!modelId) return null;
  const hit = MIN_PREFIX_TIERS.find(([re]) => re.test(modelId));
  return hit ? hit[1] : null;
}

export function cacheState(read: number, write: number, hasCacheUnits: boolean, disabled = false): PromptCacheState {
  if (read > 0) return 'caching';
  if (write > 0) return 'write-only';
  if (disabled) return 'disabled';
  if (hasCacheUnits) return 'never-cached';
  return 'no-cache-data';
}

// Sum the Bedrock cache units of every metering key whose context starts with
// `contextPrefix` ("Extraction" also covers ExtractionEscalation), or equals it
// when `exact` is set (the cost table lists each context on its own row, so a
// prefix match would count escalation tokens twice). Returns null when the
// phase made no Bedrock call.
export function summarizeCacheUsage(
  metering: Record<string, unknown>,
  contextPrefix: string,
  opts: { exact?: boolean } = {},
): PromptCacheSummary | null {
  let input = 0;
  let read = 0;
  let write = 0;
  let requests = 0;
  let sawRequests = false;
  let hasCacheUnits = false;
  const models = new Set<string>();
  Object.entries(metering).forEach(([key, units]) => {
    const parts = key.split('/');
    const contextMatches = opts.exact ? parts[0] === contextPrefix : parts[0].startsWith(contextPrefix);
    if (parts.length < 3 || parts[1] !== 'bedrock' || !contextMatches) return;
    if (!units || typeof units !== 'object') return;
    const u = units as Record<string, unknown>;
    models.add(parts.slice(2).join('/'));
    input += num(u.inputTokens);
    if ('cacheReadInputTokens' in u || 'cacheWriteInputTokens' in u) hasCacheUnits = true;
    read += num(u.cacheReadInputTokens);
    write += num(u.cacheWriteInputTokens);
    if ('requests' in u) {
      sawRequests = true;
      requests += num(u.requests);
    }
  });
  if (models.size === 0) return null;
  const denominator = input + read + write;
  const modelIds = Array.from(models).sort();
  const minimum = modelIds.map(minCacheablePrefixTokens).find((v) => v !== null) ?? null;
  return {
    state: cacheState(read, write, hasCacheUnits),
    input_tokens: input,
    cache_read_input_tokens: read,
    cache_write_input_tokens: write,
    requests: sawRequests ? requests : null,
    read_share: denominator ? read / denominator : null,
    model_ids: modelIds,
    min_cacheable_prefix_tokens: minimum,
  };
}

export interface PromptCacheDescription {
  indicator: StatusType;
  headline: string;
  detail: string;
}

// `phaseOnly` marks a per-phase view (no per-class metadata), where zero/zero
// cannot distinguish an inert cache point from one that was never sent.
// `context` is the phase; the extraction.prompt_cache knob is only mentioned
// for Extraction, since it governs nothing else.
export function describePromptCache(
  summary: PromptCacheSummary,
  opts: { phaseOnly?: boolean; context?: string } = {},
): PromptCacheDescription {
  const isExtraction = !opts.context || opts.context.startsWith('Extraction');
  const read = num(summary.cache_read_input_tokens);
  const write = num(summary.cache_write_input_tokens);
  const uncached = num(summary.input_tokens);
  const requests = summary.requests ? ` over ${num(summary.requests).toLocaleString()} request(s)` : '';
  const counts = `${read.toLocaleString()} read · ${write.toLocaleString()} written · ${uncached.toLocaleString()} uncached input tokens${requests}`;
  switch (summary.state) {
    case 'caching': {
      const pct = Math.round(num(summary.read_share) * 100);
      return { indicator: 'success', headline: `Prompt cache: caching (${pct}% of input read from cache)`, detail: counts };
    }
    case 'write-only':
      return {
        indicator: 'warning',
        headline: `Prompt cache: write-only — paid 1.25× to write ${write.toLocaleString()} tokens, no read landed`,
        detail: `${counts}. Expected when a class is processed once per 5-minute TTL${
          isExtraction ? '; a low-volume deployment can set extraction.prompt_cache: off' : ''
        }.`,
      };
    case 'never-cached': {
      const models = (summary.model_ids || []).join(', ') || 'the model';
      const min = summary.min_cacheable_prefix_tokens;
      const floor = min
        ? `${models}'s minimum cacheable prefix of ${num(min).toLocaleString()} tokens`
        : `${models}'s minimum cacheable prefix`;
      const why = opts.phaseOnly
        ? `Either no cache point reached the model for this phase${
            isExtraction ? ' (no <<CACHEPOINT>> marker, extraction.prompt_cache: off, or an unsupported model)' : ''
          } or the prompt prefix is below ${floor}.`
        : `The prompt prefix is probably below ${floor}; run "idp-cli config validate" for the per-class estimate.`;
      return { indicator: 'warning', headline: 'Prompt cache: never cached — the cache point was inert', detail: `${counts}. ${why}` };
    }
    case 'disabled':
      return { indicator: 'stopped', headline: 'Prompt cache: off by configuration (extraction.prompt_cache: off)', detail: counts };
    case 'no-cache-point':
      return {
        indicator: 'info',
        headline: 'Prompt cache: no cache point reached the model',
        detail: `${counts}. The prompt has no <<CACHEPOINT>> marker or the model does not support prompt caching.`,
      };
    default:
      return { indicator: 'info', headline: 'Prompt cache: no cache usage reported by this model or backend', detail: counts };
  }
}
