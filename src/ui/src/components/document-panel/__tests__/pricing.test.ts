// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Tests for the UI's pricing lookup (src/ui/src/components/document-panel/pricing.ts).
//
// Two regressions shaped this file and they pull in opposite directions.
//
// The first (#926) was a substring match: the lookup matched the pricing key and
// the unit name case-insensitively and BIDIRECTIONALLY, so `cacheReadInputTokens`
// bound to a related model's `inputTokens` price and cache reads were displayed at
// up to ~8x their real rate. Fixing only the Python left the number the user reads
// wrong, and nothing tested this file at all.
//
// The second (#1406) was the opposite error on the same line: a unit the matched
// entry does not list resolved to `0`. The backend stopped answering that in
// #1212/#1206 — it returns a NULL cost and warns, so an unpriced unit is visible
// instead of silently free — and this file did not move, so a row recorded as
// "unpriced, we do not know" was displayed as a confident $0.00 contribution.
// The fix is NOT a second copy of the backend's rule: the verdict now arrives on
// the `getPricing` response, and the assertions below are about the panel reading
// it rather than about what it says.
//
// Hence the shape of the fixtures. `DECLARED` is transcribed by hand from
// `idp_common.metering_units`, never derived from it, and several tests pass NO
// declarations at all — because the property that keeps the panel honest is that
// it knows nothing it was not told. The rates are likewise transcribed from
// config_library/pricing.yaml and are never computed from each other, so a bad
// multiplier in the shipped table fails here rather than being reproduced.
import { describe, it, expect } from 'vitest';

import { lookupUnitPrice } from '../pricing';
import type { NonChargeableUnits, PricingLookup } from '../pricing';

// The EU Nova 2 Lite BASE-tier row only, plus Textract and both shipped
// Lambda-hook rows. Rates are the real shipped ones.
//
// The base-tier key is a strict PREFIX of the service-tier ids
// `…-v1:0:flex` and `…-v1:0:priority`, which is the exact shape that let the old
// lookup bind one model to another. The tier rows are deliberately ABSENT from
// this fixture even though PR #952 adds them to config_library/pricing.yaml,
// because including them is what made the headline assertion below vacuous: with
// a `:flex` row present the old implementation's exact branch matched on its first
// attempt, the substring fallback never ran, and the old code passed this test
// unchanged. Leaving only the base row is what forces a lookup for a tier id
// through the suffix walk, where the old rule went wrong.
const PRICING: PricingLookup = {
  'bedrock/eu.amazon.nova-2-lite-v1:0': {
    inputTokens: 3.9e-7,
    outputTokens: 3.27e-6,
    cacheReadInputTokens: 9.75e-8,
    cacheWriteInputTokens: 3.9e-7,
  },
  'textract/detect_document_text': {
    pages: 0.0015,
  },
  'lambda_hook/GENAIIDP-mistral-ocr-hook': {
    pages: 0.004,
  },
  'lambda_hook/GENAIIDP-cohere-parse-hook': {
    pages: 0.0015,
  },
};

// What the `getPricing` resolver sends for these four keys, transcribed by hand
// from the declarations in `idp_common.metering_units`: `totalTokens` and
// `requests` are not billing dimensions of Bedrock or of a Lambda hook, on the
// authority of AWS's published price list. Note what is NOT here — Textract
// declares nothing, so every unit its entry omits is unpriced.
const DECLARED: NonChargeableUnits = {
  'bedrock/eu.amazon.nova-2-lite-v1:0': ['requests', 'totalTokens'],
  'lambda_hook/GENAIIDP-mistral-ocr-hook': ['requests', 'totalTokens'],
  'lambda_hook/GENAIIDP-cohere-parse-hook': ['requests', 'totalTokens'],
};

describe('lookupUnitPrice', () => {
  it('prices a cache read at the cache-read rate, not at a sibling model’s input rate', () => {
    // The measured regression, in both of the hops that produced it.
    //
    // Hop one, across ROWS: an unpriced service tier must not borrow the base
    // tier's rate. The old rule accepted `…-v1:0` as a match for
    // `…-v1:0:flex` because one string contains the other, and then took
    // 'inputtokens' from inside 'cachereadinputtokens' — two wrong hops
    // compounding to 3.9e-7 against the tier's real 4.88e-8, a factor of 7.99.
    // The correct answer is null: nothing in this table prices that tier, and a
    // related row's rate is never substituted.
    expect(lookupUnitPrice(PRICING, 'bedrock/eu.amazon.nova-2-lite-v1:0:flex', 'cacheReadInputTokens')).toBeNull();
    expect(lookupUnitPrice(PRICING, 'bedrock/eu.amazon.nova-2-lite-v1:0:flex', 'cacheReadInputTokens')).not.toBe(3.9e-7);

    // Hop two, within a row: when a row IS matched — here through the suffix walk,
    // stripping the 'Extraction/' context component, so the exact branch does not
    // short-circuit it — the cache-read unit must resolve to its own rate and not
    // to 'inputTokens'. The old unit-level rule returned 3.9e-7 here too, 4.0x the
    // correct 9.75e-8, because 'cachereadinputtokens' contains 'inputtokens'.
    expect(lookupUnitPrice(PRICING, 'Extraction/bedrock/eu.amazon.nova-2-lite-v1:0', 'cacheReadInputTokens')).toBe(9.75e-8);
    expect(lookupUnitPrice(PRICING, 'Extraction/bedrock/eu.amazon.nova-2-lite-v1:0', 'cacheReadInputTokens')).not.toBe(3.9e-7);
  });

  it('does not bind a longer model id to a shorter pricing key', () => {
    expect(lookupUnitPrice(PRICING, 'bedrock/eu.amazon.nova-2-lite-v1:0:priority', 'inputTokens')).toBeNull();
  });

  it('does not bind a shorter model id to a longer pricing key', () => {
    expect(lookupUnitPrice(PRICING, 'bedrock/eu.amazon.nova-2', 'inputTokens')).toBeNull();
  });

  it('prices every token unit of a matched entry from that entry alone', () => {
    const key = 'bedrock/eu.amazon.nova-2-lite-v1:0';
    expect(lookupUnitPrice(PRICING, key, 'inputTokens')).toBe(3.9e-7);
    expect(lookupUnitPrice(PRICING, key, 'outputTokens')).toBe(3.27e-6);
    expect(lookupUnitPrice(PRICING, key, 'cacheReadInputTokens')).toBe(9.75e-8);
    expect(lookupUnitPrice(PRICING, key, 'cacheWriteInputTokens')).toBe(3.9e-7);
  });

  it('returns 0 for a unit the matched entry omits and the backend declares free', () => {
    // Every Bedrock call meters totalTokens and requests and neither is charged.
    // The entry exists and the declaration says so, so this is $0.00 and not a
    // pricing gap. The declaration is the backend's; the panel only reads it.
    const context = { count: 1200, nonChargeableUnits: DECLARED };
    expect(lookupUnitPrice(PRICING, 'bedrock/eu.amazon.nova-2-lite-v1:0', 'totalTokens', context)).toBe(0);
    expect(lookupUnitPrice(PRICING, 'bedrock/eu.amazon.nova-2-lite-v1:0', 'requests', context)).toBe(0);
    // The suffix walk and the declaration lookup have to agree on which key
    // matched: a metering key carries a leading phase, and the declaration is
    // keyed on the PRICING key the walk arrived at, not on the key passed in.
    expect(lookupUnitPrice(PRICING, 'Extraction/bedrock/eu.amazon.nova-2-lite-v1:0', 'totalTokens', context)).toBe(0);
  });

  it('returns null for a unit the matched entry omits with no declaration for it', () => {
    // THE #1406 ASSERTION. Textract prices pages and declares nothing else, so a
    // token count metered against it is unpriced — which is what the reporting
    // table records for that row, as a SQL NULL with a warning. A `0` here is a
    // concrete claim the backend does not make, and it understates.
    const context = { count: 50_000, nonChargeableUnits: DECLARED };
    expect(lookupUnitPrice(PRICING, 'textract/detect_document_text', 'inputTokens', context)).toBeNull();
    expect(lookupUnitPrice(PRICING, 'textract/detect_document_text', 'cacheReadInputTokens', context)).toBeNull();
    // A cache unit on a Bedrock entry that does price the other three token
    // units is the #1206 case: whether a cache read costs anything is a property
    // of the model, so an entry omitting the rate is a gap and not a free unit.
    expect(
      lookupUnitPrice({ 'bedrock/some.model-v1:0': { inputTokens: 1e-7 } }, 'bedrock/some.model-v1:0', 'cacheReadInputTokens', context),
    ).toBeNull();
  });

  it('knows nothing it was not told: no declarations means no free units', () => {
    // The property that stops this file becoming a fourth implementation of the
    // priced/unpriced decision. `totalTokens` IS declared free by the backend —
    // and the panel must still answer null for it when the declaration did not
    // arrive, because the alternative is a hardcoded copy of the register here
    // that can disagree with it. An older deployment's resolver and a failed
    // pricing fetch both land in this state, and both show the gap.
    expect(lookupUnitPrice(PRICING, 'bedrock/eu.amazon.nova-2-lite-v1:0', 'totalTokens', { count: 1200 })).toBeNull();
    expect(lookupUnitPrice(PRICING, 'bedrock/eu.amazon.nova-2-lite-v1:0', 'requests', { count: 1 })).toBeNull();
    expect(
      lookupUnitPrice(PRICING, 'bedrock/eu.amazon.nova-2-lite-v1:0', 'totalTokens', { count: 1200, nonChargeableUnits: {} }),
    ).toBeNull();
  });

  it('defaults to the strict answer when the caller passes no context at all', () => {
    // Both options default to the answer that shows a gap: an omitted count is
    // read as a real one, and absent declarations declare nothing. A caller that
    // forgets them gets 'unpriced', never 'free' — the convenient default is the
    // bug this file had, so the direction of these defaults is the fix.
    expect(lookupUnitPrice(PRICING, 'bedrock/eu.amazon.nova-2-lite-v1:0', 'totalTokens')).toBeNull();
    expect(lookupUnitPrice(PRICING, 'lambda_hook/GENAIIDP-mistral-ocr-hook', 'requests')).toBeNull();
  });

  it('returns 0 for a unit metered as zero, declared or not', () => {
    // Arithmetic rather than a judgement: nothing metered costs nothing at any
    // rate, so there is no spend for a $0.00 to hide and nothing to investigate.
    // This is what keeps the shipped per-page OCR hooks quiet — they report
    // inputTokens/outputTokens/totalTokens as 0 against a pages-only entry.
    expect(lookupUnitPrice(PRICING, 'lambda_hook/GENAIIDP-mistral-ocr-hook', 'inputTokens', { count: 0 })).toBe(0);
    expect(lookupUnitPrice(PRICING, 'textract/detect_document_text', 'outputTokens', { count: 0 })).toBe(0);
    // ...and the same unit with a real count is unpriced, which is the whole
    // reason the count is passed in rather than assumed.
    expect(lookupUnitPrice(PRICING, 'lambda_hook/GENAIIDP-mistral-ocr-hook', 'inputTokens', { count: 1 })).toBeNull();
  });

  it('returns null, not 0, when no entry matches at all', () => {
    // null renders as 'None'/'N/A'. A silent 0 would be indistinguishable from
    // something genuinely free and would quietly understate the document's cost.
    expect(lookupUnitPrice(PRICING, 'bedrock/not.in.the.table', 'inputTokens')).toBeNull();
    expect(lookupUnitPrice(PRICING, 'sagemaker/endpoint', 'seconds')).toBeNull();
    // Not even with a zero count or a declaration naming the unit: a declaration
    // is about a unit of an entry that exists, and no entry here does.
    expect(lookupUnitPrice(PRICING, 'bedrock/not.in.the.table', 'totalTokens', { count: 0, nonChargeableUnits: DECLARED })).toBeNull();
  });

  it('strips leading context components, longest suffix first', () => {
    expect(lookupUnitPrice(PRICING, 'OCR/textract/detect_document_text', 'pages')).toBe(0.0015);
    expect(lookupUnitPrice(PRICING, 'textract/detect_document_text', 'pages')).toBe(0.0015);
    expect(lookupUnitPrice(PRICING, 'Classification/bedrock/eu.amazon.nova-2-lite-v1:0', 'inputTokens')).toBe(3.9e-7);
  });

  it('prices both shipped Lambda-hook rows from their `lambda_hook/<name>` key', () => {
    // The metering key the backend emits is `{context}/lambda_hook/{functionName}`
    // — the bare function name, not the configured ARN, because an ARN delimits
    // the name with ':' which this suffix walk cannot split. These two rows are
    // the pricing regression PR #952 had to restore; the UI has to resolve them
    // too or the panel shows 'None' for a page rate the backend charges.
    expect(lookupUnitPrice(PRICING, 'lambda_hook/GENAIIDP-mistral-ocr-hook', 'pages')).toBe(0.004);
    expect(lookupUnitPrice(PRICING, 'OCR/lambda_hook/GENAIIDP-cohere-parse-hook', 'pages')).toBe(0.0015);
    // The hook also meters a request count that these rows do not price. It is
    // $0.00 because the backend declares it — a count BedrockClient injects for
    // shape parity, which no third party bills — not because the entry omits it.
    expect(lookupUnitPrice(PRICING, 'lambda_hook/GENAIIDP-mistral-ocr-hook', 'requests', { count: 1, nonChargeableUnits: DECLARED })).toBe(
      0,
    );
    // A hook nobody has priced is genuinely unpriced.
    expect(lookupUnitPrice(PRICING, 'lambda_hook/GENAIIDP-some-other-hook', 'pages')).toBeNull();
  });

  it('is case-sensitive on the unit name', () => {
    // The old implementation lowercased both sides. Unit names come from the
    // Bedrock/Textract responses verbatim, so a case-insensitive match buys
    // nothing and was half of how cacheReadInputTokens went astray. A mis-cased
    // name is a unit the entry does not list and nothing declares: unpriced, and
    // visible as such, rather than a silent $0.00 beside the real row.
    const context = { count: 1200, nonChargeableUnits: DECLARED };
    expect(lookupUnitPrice(PRICING, 'bedrock/eu.amazon.nova-2-lite-v1:0', 'inputtokens', context)).toBeNull();
    expect(lookupUnitPrice(PRICING, 'bedrock/eu.amazon.nova-2-lite-v1:0', 'INPUTTOKENS', context)).toBeNull();
    // Including on the declared units, which are matched exactly too.
    expect(lookupUnitPrice(PRICING, 'bedrock/eu.amazon.nova-2-lite-v1:0', 'totaltokens', context)).toBeNull();
  });

  it('ignores a declarations payload that is not a list of unit names', () => {
    // It arrives as parsed JSON from the network, so a malformed member must not
    // throw and must not be read as a free unit.
    const malformed = { 'bedrock/eu.amazon.nova-2-lite-v1:0': 'totalTokens' } as unknown as NonChargeableUnits;
    expect(
      lookupUnitPrice(PRICING, 'bedrock/eu.amazon.nova-2-lite-v1:0', 'totalTokens', { count: 9, nonChargeableUnits: malformed }),
    ).toBeNull();
  });

  it('handles an empty pricing table without throwing', () => {
    expect(lookupUnitPrice({}, 'bedrock/eu.amazon.nova-2-lite-v1:0', 'inputTokens')).toBeNull();
  });
});
