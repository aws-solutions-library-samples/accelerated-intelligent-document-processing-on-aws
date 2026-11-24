// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// Prompt-cache efficiency read back out of metering (#780 item 2). Mirrors
// idp_common.bedrock.prompt_cache.summarize_cache_usage / describe_cache_state:
// the backend records a per-section summary in the section result.json
// (metadata.prompt_cache); the per-phase view in the document cost table is
// derived here from the document's metering map, which is keyed by phase and
// model only. The notes describePromptCache writes are in the UI's language when
// it is given `intl`, and in English without it.
import { createIntl, defineMessages, type IntlShape, type NoMessageValues } from 'react-intl';

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
  // 'opus-5' also matches 'opus-5-5', which is the published answer for Opus 5.5
  // too (512-token minimum, 4 checkpoints) rather than a coincidence.
  // 'haiku-5-5' is its own alternative here and must stay ahead of the 'haiku-4-5'
  // tier: the two Haikus sit at opposite ends of this table (512 vs 4096), so a
  // shared 'haiku-' stem would report an 8x-too-high minimum for Haiku 5.5.
  // 'sonnet-5-5' must likewise stay ahead of the 1024 tier below, and here the
  // shadowing is in the id rather than in a stem: 'claude-sonnet-5' is a substring
  // of 'claude-sonnet-5-5', so without this alternative Sonnet 5.5 reports 1024 —
  // double its real 512 — and the UI warns that a 512-1024 token class will not
  // cache when it does. Mirrors _MIN_PREFIX_TIERS in idp_common/bedrock/prompt_cache.py.
  [/claude-(opus-5|fable-5|haiku-5-5|sonnet-5-5)/, 512],
  [/claude-opus-4-7/, 2048],
  [/claude-(opus-4-6|opus-4-5|haiku-4-5)/, 4096],
  [/claude-(sonnet-5|sonnet-4|opus-4-8|opus-4-1|opus-4|3-7-sonnet)/, 1024],
];

export function minCacheablePrefixTokens(modelId?: string | null): number | null {
  if (!modelId) return null;
  const hit = MIN_PREFIX_TIERS.find(([re]) => re.test(modelId));
  return hit ? hit[1] : null;
}

// Models that cache WITHOUT a Converse cachePoint block, so "no cache point was
// sent" says nothing about whether they cache. Mirrors
// _IMPLICIT_CACHE_BASE_NAMES in idp_common/bedrock/prompt_cache.py:
//   - openai.gpt-6-astra: Converse, implicit (an explicit cachePoint is rejected)
//   - openai.gpt-5.4/5.5: bedrock-mantle, automatic over ~1,024 tokens
// Deliberately absent: openai.gpt-5.6 (Sol/Terra/Luna), whose caching is EXPLICIT —
// the client only sends a breakpoint when a <<CACHEPOINT>> marker is present, so
// for it "no marker" really is "no caching" and the remedy is to add one; and
// xAI Grok, whose advertised implicit caching was never observed to engage.
const IMPLICIT_CACHE_PATTERNS: RegExp[] = [/openai\.gpt-6-astra/, /openai\.gpt-5\.4/, /openai\.gpt-5\.5/];

export function modelCachesImplicitly(modelId?: string | null): boolean {
  if (!modelId) return false;
  return IMPLICIT_CACHE_PATTERNS.some((re) => re.test(modelId));
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

/** The arguments of each cache state's headline, keyed by the state as `PromptCacheState` writes it. */
type StateHeadlineValues = {
  caching: { readonly share: number };
  'write-only': { readonly written: number };
  'never-cached': NoMessageValues;
  disabled: { readonly setting: string };
  'no-cache-point': NoMessageValues;
  'no-cache-data': NoMessageValues;
};

/**
 * The headline of a prompt-cache note, one per cache state. A state the UI does not know
 * reads as `no-cache-data`, and a `no-cache-point` summary whose models all cache
 * implicitly has its own headline (`implicitCachingHeadline`).
 */
const stateHeadlineMessages = defineMessages<StateHeadlineValues>({
  caching: {
    id: 'common.promptCache.headline.caching',
    defaultMessage: 'Prompt cache: caching ({share, number, ::percent} of input read from cache)',
    description:
      "Headline of the prompt-cache note when input was read from the cache, beside a success icon, on a phase's subtotal row in Reports › Estimated Cost on the document page and in the Processing Path of the edit page's Processing Report. {share} is the share of the input read from the cache, as a percentage.",
  },
  'write-only': {
    id: 'common.promptCache.headline.writeOnly',
    defaultMessage: 'Prompt cache: write-only — paid 1.25× to write {written, number} tokens, no read landed',
    description:
      "Headline of the prompt-cache note when tokens were written to the cache and none read from it, beside a warning icon, on a phase's subtotal row in Reports › Estimated Cost on the document page and in the Processing Path of the edit page's Processing Report. {written} is the number of tokens written to the cache.",
  },
  'never-cached': {
    id: 'common.promptCache.headline.neverCached',
    defaultMessage: 'Prompt cache: never cached — the cache point was inert',
    description:
      "Headline of the prompt-cache note when nothing was read from or written to the cache, beside a warning icon, on a phase's subtotal row in Reports › Estimated Cost on the document page and in the Processing Path of the edit page's Processing Report.",
  },
  disabled: {
    id: 'common.promptCache.headline.disabled',
    defaultMessage: 'Prompt cache: off by configuration ({setting})',
    description:
      "Headline of the prompt-cache note when the configuration turned prompt caching off, beside a stopped icon, in the Processing Path of the edit page's Processing Report. {setting} is the configuration setting that does it, extraction.prompt_cache: off, shown as written.",
  },
  'no-cache-point': {
    id: 'common.promptCache.headline.noCachePoint',
    defaultMessage: 'Prompt cache: no cache point reached the model',
    description:
      "Headline of the prompt-cache note when no cache point was sent to the model, beside an info icon, in the Processing Path of the edit page's Processing Report.",
  },
  'no-cache-data': {
    id: 'common.promptCache.headline.noCacheData',
    defaultMessage: 'Prompt cache: no cache usage reported by this model or backend',
    description:
      "Headline of the prompt-cache note when the model or backend reported no cache usage at all, or a cache state the UI does not know, beside an info icon, in the Processing Path of the edit page's Processing Report.",
  },
});

const messages = defineMessages<{
  implicitCachingHeadline: NoMessageValues;
  counts: { readonly read: number; readonly written: number; readonly uncached: number };
  countsOverRequests: { readonly read: number; readonly written: number; readonly uncached: number; readonly requests: number };
  writeOnly: { readonly counts: string };
  writeOnlyExtraction: { readonly counts: string; readonly setting: string };
  neverCached: { readonly counts: string; readonly floor: string; readonly command: string };
  neverCachedPhase: { readonly counts: string; readonly floor: string };
  neverCachedExtractionPhase: { readonly counts: string; readonly floor: string; readonly marker: string; readonly setting: string };
  implicitCaching: { readonly counts: string; readonly marker: string };
  noCachePoint: { readonly counts: string; readonly marker: string };
  floor: { readonly models: string };
  floorWithMinimum: { readonly models: string; readonly min: number };
  theModel: NoMessageValues;
}>({
  implicitCachingHeadline: {
    id: 'common.promptCache.headline.implicitCaching',
    defaultMessage: 'Prompt cache: implicit caching — no cache point needed',
    description:
      "Headline of the prompt-cache note when no cache point was sent to a model that caches without one, beside an info icon, in the Processing Path of the edit page's Processing Report.",
  },
  counts: {
    id: 'common.promptCache.counts',
    defaultMessage: '{read, number} read · {written, number} written · {uncached, number} uncached input tokens',
    description:
      "The token counts of a prompt-cache note, its whole detail or the start of it, on a phase's subtotal row in Reports › Estimated Cost on the document page and in the Processing Path of the edit page's Processing Report. {read} is the number of input tokens read from the cache, {written} the number written to it, and {uncached} the number of input tokens the cache did not cover.",
  },
  countsOverRequests: {
    id: 'common.promptCache.countsOverRequests',
    defaultMessage:
      '{read, number} read · {written, number} written · {uncached, number} uncached input tokens over {requests, plural, one {# request} other {# requests}}',
    description:
      "The token counts of a prompt-cache note when the number of model requests is known, its whole detail or the start of it, on a phase's subtotal row in Reports › Estimated Cost on the document page and in the Processing Path of the edit page's Processing Report. {read} is the number of input tokens read from the cache, {written} the number written to it, {uncached} the number of input tokens the cache did not cover, and {requests} the number of model requests they add up over.",
  },
  writeOnly: {
    id: 'common.promptCache.detail.writeOnly',
    defaultMessage: '{counts}. Expected when a class is processed once per 5-minute TTL.',
    description:
      'Detail of the prompt-cache note when tokens were written to the cache and none read from it, on the subtotal row of a phase other than extraction in Reports › Estimated Cost on the document page. {counts} is the note\'s token counts, such as "0 read · 1,628 written · 9 uncached input tokens". TTL is the cache\'s time to live.',
  },
  writeOnlyExtraction: {
    id: 'common.promptCache.detail.writeOnlyExtraction',
    defaultMessage: '{counts}. Expected when a class is processed once per 5-minute TTL; a low-volume deployment can set {setting}.',
    description:
      "Detail of the prompt-cache note when tokens were written to the cache and none read from it, on an extraction phase's subtotal row in Reports › Estimated Cost on the document page and in the Processing Path of the edit page's Processing Report. {counts} is the note's token counts, such as \"0 read · 1,628 written · 9 uncached input tokens\". {setting} is the configuration setting that turns prompt caching off, extraction.prompt_cache: off, shown as written. TTL is the cache's time to live.",
  },
  neverCached: {
    id: 'common.promptCache.detail.neverCached',
    defaultMessage: '{counts}. The prompt prefix is probably below {floor}; run "{command}" for the per-class estimate.',
    description:
      'Detail of the prompt-cache note when nothing was read from or written to the cache, in the Processing Path of the edit page\'s Processing Report. {counts} is the note\'s token counts, such as "0 read · 0 written · 949 uncached input tokens". {floor} names the model\'s minimum cacheable prefix, such as "us.anthropic.claude-sonnet-4\'s minimum cacheable prefix of 1,024 tokens". {command} is the command-line check that gives that estimate, idp-cli config validate, shown as written.',
  },
  neverCachedPhase: {
    id: 'common.promptCache.detail.neverCachedPhase',
    defaultMessage: '{counts}. Either no cache point reached the model for this phase or the prompt prefix is below {floor}.',
    description:
      'Detail of the prompt-cache note when nothing was read from or written to the cache, on the subtotal row of a phase other than extraction in Reports › Estimated Cost on the document page. {counts} is the note\'s token counts, such as "0 read · 0 written · 949 uncached input tokens". {floor} names the model\'s minimum cacheable prefix, such as "us.anthropic.claude-sonnet-4\'s minimum cacheable prefix of 1,024 tokens".',
  },
  neverCachedExtractionPhase: {
    id: 'common.promptCache.detail.neverCachedExtractionPhase',
    defaultMessage:
      '{counts}. Either no cache point reached the model for this phase (no {marker} marker, {setting}, or an unsupported model) or the prompt prefix is below {floor}.',
    description:
      'Detail of the prompt-cache note when nothing was read from or written to the cache, on an extraction phase\'s subtotal row in Reports › Estimated Cost on the document page. {counts} is the note\'s token counts, such as "0 read · 0 written · 949 uncached input tokens". {marker} is the marker a prompt carries where its cached part ends, <<CACHEPOINT>>, and {setting} the configuration setting that turns prompt caching off, extraction.prompt_cache: off; both are shown as written. {floor} names the model\'s minimum cacheable prefix, such as "us.anthropic.claude-sonnet-4\'s minimum cacheable prefix of 1,024 tokens".',
  },
  implicitCaching: {
    id: 'common.promptCache.detail.implicitCaching',
    defaultMessage:
      '{counts}. This model caches implicitly, so no {marker} marker is required; a prefix seen again within the cache TTL is reported as caching once reads land.',
    description:
      "Detail of the prompt-cache note when no cache point was sent to a model that caches without one, in the Processing Path of the edit page's Processing Report. {counts} is the note's token counts, such as \"0 read · 0 written · 949 uncached input tokens\". {marker} is the marker a prompt carries where its cached part ends, <<CACHEPOINT>>, shown as written. TTL is the cache's time to live.",
  },
  noCachePoint: {
    id: 'common.promptCache.detail.noCachePoint',
    defaultMessage: '{counts}. The prompt has no {marker} marker, or this model is not one the client sends cachePoint blocks to.',
    description:
      'Detail of the prompt-cache note when no cache point was sent to the model, in the Processing Path of the edit page\'s Processing Report. {counts} is the note\'s token counts, such as "0 read · 0 written · 949 uncached input tokens". {marker} is the marker a prompt carries where its cached part ends, <<CACHEPOINT>>, shown as written. cachePoint is the name of the block the Bedrock client adds to a request, kept as written.',
  },
  floor: {
    id: 'common.promptCache.floor',
    defaultMessage: "{models}'s minimum cacheable prefix",
    description:
      'The part of a prompt-cache note that names the smallest prompt prefix the model will cache, when that size is not known; the never-cached details put it after "below" ("…or the prompt prefix is below {floor}." and "The prompt prefix is probably below {floor}; run …"). {models} is the model id, or several separated by commas, or "the model".',
  },
  floorWithMinimum: {
    id: 'common.promptCache.floorWithMinimum',
    defaultMessage: "{models}'s minimum cacheable prefix of {min, number} tokens",
    description:
      'The part of a prompt-cache note that names the smallest prompt prefix the model will cache; the never-cached details put it after "below" ("…or the prompt prefix is below {floor}." and "The prompt prefix is probably below {floor}; run …"). {models} is the model id, or several separated by commas, or "the model". {min} is that size, in tokens.',
  },
  theModel: {
    id: 'common.promptCache.theModel',
    defaultMessage: 'the model',
    description:
      'Stands for the model in a prompt-cache note when the summary names no model id, as in "the model\'s minimum cacheable prefix".',
  },
});

/** Formats the English source text for callers that pass no `intl`. */
const englishIntl = createIntl({ locale: 'en', defaultLocale: 'en' });

// `phaseOnly` marks a per-phase view (no per-class metadata), where zero/zero
// cannot distinguish an inert cache point from one that was never sent.
// `context` is the phase; the extraction.prompt_cache knob is only mentioned
// for Extraction, since it governs nothing else. The note is worded with
// `intl` when given, in English otherwise; the marker, setting and command it
// quotes are arguments, written as they are.
export function describePromptCache(
  summary: PromptCacheSummary,
  opts: { phaseOnly?: boolean; context?: string } = {},
  intl?: IntlShape,
): PromptCacheDescription {
  const fmt = intl ?? englishIntl;
  const isExtraction = !opts.context || opts.context.startsWith('Extraction');
  const read = num(summary.cache_read_input_tokens);
  const write = num(summary.cache_write_input_tokens);
  const uncached = num(summary.input_tokens);
  const counts = summary.requests
    ? fmt.formatMessage(messages.countsOverRequests, { read, written: write, uncached, requests: num(summary.requests) })
    : fmt.formatMessage(messages.counts, { read, written: write, uncached });
  switch (summary.state) {
    case 'caching': {
      // Rounded first, as the percentage always was: `::percent` alone would round the ratio's decimal form.
      const share = Math.round(num(summary.read_share) * 100) / 100;
      return { indicator: 'success', headline: fmt.formatMessage(stateHeadlineMessages.caching, { share }), detail: counts };
    }
    case 'write-only':
      return {
        indicator: 'warning',
        headline: fmt.formatMessage(stateHeadlineMessages['write-only'], { written: write }),
        detail: isExtraction
          ? fmt.formatMessage(messages.writeOnlyExtraction, { counts, setting: 'extraction.prompt_cache: off' })
          : fmt.formatMessage(messages.writeOnly, { counts }),
      };
    case 'never-cached': {
      const models = (summary.model_ids || []).join(', ') || fmt.formatMessage(messages.theModel);
      const min = summary.min_cacheable_prefix_tokens;
      const floor = min
        ? fmt.formatMessage(messages.floorWithMinimum, { models, min: num(min) })
        : fmt.formatMessage(messages.floor, { models });
      let detail: string;
      if (!opts.phaseOnly) {
        detail = fmt.formatMessage(messages.neverCached, { counts, floor, command: 'idp-cli config validate' });
      } else if (isExtraction) {
        detail = fmt.formatMessage(messages.neverCachedExtractionPhase, {
          counts,
          floor,
          marker: '<<CACHEPOINT>>',
          setting: 'extraction.prompt_cache: off',
        });
      } else {
        detail = fmt.formatMessage(messages.neverCachedPhase, { counts, floor });
      }
      return { indicator: 'warning', headline: fmt.formatMessage(stateHeadlineMessages['never-cached']), detail };
    }
    case 'disabled':
      return {
        indicator: 'stopped',
        headline: fmt.formatMessage(stateHeadlineMessages.disabled, { setting: 'extraction.prompt_cache: off' }),
        detail: counts,
      };
    case 'no-cache-point': {
      // "We sent no cachePoint" is NOT "this model cannot cache" — that is false
      // for the implicit-caching models (see modelCachesImplicitly).
      const ids = summary.model_ids ?? [];
      if (ids.length > 0 && ids.every((m) => modelCachesImplicitly(m))) {
        return {
          indicator: 'info',
          headline: fmt.formatMessage(messages.implicitCachingHeadline),
          detail: fmt.formatMessage(messages.implicitCaching, { counts, marker: '<<CACHEPOINT>>' }),
        };
      }
      return {
        indicator: 'info',
        headline: fmt.formatMessage(stateHeadlineMessages['no-cache-point']),
        detail: fmt.formatMessage(messages.noCachePoint, { counts, marker: '<<CACHEPOINT>>' }),
      };
    }
    default:
      return { indicator: 'info', headline: fmt.formatMessage(stateHeadlineMessages['no-cache-data']), detail: counts };
  }
}
