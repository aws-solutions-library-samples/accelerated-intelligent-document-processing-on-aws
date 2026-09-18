// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Tests for the UI's pricing lookup (src/ui/src/components/document-panel/pricing.ts).
//
// This function is a port of the backend's `_get_unit_cost`
// (lib/idp_common_pkg/idp_common/reporting/save_reporting_data.py). It was a
// SECOND, independent implementation of the same substring-matching bug that
// GitHub issue #926 reported against the Python side: it matched the pricing key
// and the unit name case-insensitively and BIDIRECTIONALLY, so
// `cacheReadInputTokens` bound to a related model's `inputTokens` price. Fixing
// only the Python left the number the user actually reads in the UI wrong, and
// nothing tested this file at all — hence these tests.
//
// The expected values below are transcribed by hand from config_library/pricing.yaml
// and are never computed from each other, so a bad multiplier in the shipped table
// fails here rather than being reproduced by the test.
import { describe, it, expect } from 'vitest';

import { lookupUnitPrice } from '../pricing';
import type { PricingLookup } from '../pricing';

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

  it('returns 0 for a unit the matched entry does not list', () => {
    // Every Bedrock call meters totalTokens and requests and neither is charged.
    // The entry exists, so this is not a pricing gap — 0, not null.
    expect(lookupUnitPrice(PRICING, 'bedrock/eu.amazon.nova-2-lite-v1:0', 'totalTokens')).toBe(0);
    expect(lookupUnitPrice(PRICING, 'bedrock/eu.amazon.nova-2-lite-v1:0', 'requests')).toBe(0);
    // ...and specifically NOT the sibling unit's rate, which is what the old
    // unit-level substring rule returned for cacheWriteInputTokens.
    expect(lookupUnitPrice(PRICING, 'textract/detect_document_text', 'inputTokens')).toBe(0);
  });

  it('returns null, not 0, when no entry matches at all', () => {
    // null renders as 'None'/'N/A'. A silent 0 would be indistinguishable from
    // something genuinely free and would quietly understate the document's cost.
    expect(lookupUnitPrice(PRICING, 'bedrock/not.in.the.table', 'inputTokens')).toBeNull();
    expect(lookupUnitPrice(PRICING, 'sagemaker/endpoint', 'seconds')).toBeNull();
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
    // The hook also meters a request count that these rows do not price: 0, since
    // the entry exists, rather than null.
    expect(lookupUnitPrice(PRICING, 'lambda_hook/GENAIIDP-mistral-ocr-hook', 'requests')).toBe(0);
    // A hook nobody has priced is genuinely unpriced.
    expect(lookupUnitPrice(PRICING, 'lambda_hook/GENAIIDP-some-other-hook', 'pages')).toBeNull();
  });

  it('is case-sensitive on the unit name', () => {
    // The old implementation lowercased both sides. Unit names come from the
    // Bedrock/Textract responses verbatim, so a case-insensitive match buys
    // nothing and was half of how cacheReadInputTokens went astray.
    expect(lookupUnitPrice(PRICING, 'bedrock/eu.amazon.nova-2-lite-v1:0', 'inputtokens')).toBe(0);
    expect(lookupUnitPrice(PRICING, 'bedrock/eu.amazon.nova-2-lite-v1:0', 'INPUTTOKENS')).toBe(0);
  });

  it('handles an empty pricing table without throwing', () => {
    expect(lookupUnitPrice({}, 'bedrock/eu.amazon.nova-2-lite-v1:0', 'inputTokens')).toBeNull();
  });
});
