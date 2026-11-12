// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * SafeMarkdown — a drop-in replacement for ReactMarkdown + rehype-raw that
 * sanitizes rendered HTML against an allow-list schema.
 *
 * Why: the backend deliberately emits rich markdown containing a small set
 * of HTML constructs (`<details>/<summary>`, custom `<documentid>` anchors,
 * `<br>`, styled `<p>` for whitespace preservation, tables). Some of that
 * markdown embeds document-derived content (OCR text, KB snippets, model
 * answers), and that content can itself contain HTML. Pairing `rehype-raw`
 * with `rehype-sanitize` keeps the backend's HTML working while removing
 * event handlers, `<script>`, `<iframe>`, `javascript:` URLs and every other
 * element or attribute the schema does not list.
 *
 * The schema, and the rule for which URLs rendered markdown may load, live in
 * `safe-markdown-policy.ts`. In short: images load only from this page's own
 * origin, `data:`/`blob:` URLs and this deployment's own S3 buckets (any other
 * image is shown as a link carrying its URL); `<style>` is removed with its
 * contents; and `style` attributes are limited to layout and typography
 * declarations that cannot reference a URL.
 */

import React, { useContext, useMemo } from 'react';
import ReactMarkdown, { defaultUrlTransform, type Components, type UrlTransform } from 'react-markdown';
import rehypeRaw from 'rehype-raw';
import rehypeSanitize from 'rehype-sanitize';
import remarkGfm from 'remark-gfm';
import type { PluggableList } from 'unified';
import { SettingsContext } from '../../contexts/settings';
import { bucketsFromSettings, isAllowedImageUrl, rehypeRestrictImages, safeSchema, type ResourceOrigins } from './safe-markdown-policy';

/**
 * Applied by react-markdown to every URL-bearing property after the rehype
 * plugins have run. An image source is kept only if the policy allows it (the
 * rewrite plugin has normally already turned a refused image into a link, so
 * this is the second of two checks); every other URL property goes through
 * react-markdown's default protocol filter.
 */
const makeUrlTransform =
  (origins: ResourceOrigins): UrlTransform =>
  (url, key, node) => {
    if (key === 'src') return node.tagName === 'img' && isAllowedImageUrl(url, origins) ? url : undefined;
    return defaultUrlTransform(url);
  };

export interface SafeMarkdownProps {
  children: string;
  /**
   * Optional custom components mapping, forwarded to ReactMarkdown's
   * `components` prop. Use this to intercept `documentid` etc.
   */
  components?: Components;
  /**
   * Optional additional remark plugins. remark-gfm is always enabled.
   */
  extraRemarkPlugins?: PluggableList;
  /**
   * Optional additional rehype plugins. rehype-raw (parse) + rehype-sanitize
   * (sanitize) are always enabled; anything provided here runs after them.
   */
  extraRehypePlugins?: PluggableList;
  className?: string;
  /**
   * Parse raw HTML embedded in the markdown (sanitized against the schema).
   * Default true. Pass false for text that has no reason to carry HTML, so any
   * HTML in it is dropped rather than rendered.
   */
  rawHtml?: boolean;
}

/**
 * Renders `children` as markdown, parsing any embedded raw HTML via
 * rehype-raw and then sanitizing the result against `safeSchema`.
 *
 * Intended to replace bare `<ReactMarkdown remarkPlugins={[remarkGfm]}
 * rehypePlugins={[rehypeRaw]}>` usages across the UI.
 */
const SafeMarkdown: React.FC<SafeMarkdownProps> = ({
  children,
  components,
  extraRemarkPlugins = [],
  extraRehypePlugins = [],
  className,
  rawHtml = true,
}) => {
  const content = children ?? '';
  // Read without the throwing hook: SafeMarkdown is also handed to feature
  // bundles, and outside the provider the answer is simply "no buckets".
  const settings = useContext(SettingsContext)?.settings;
  const origins = useMemo<ResourceOrigins>(
    () => ({ pageOrigin: window.location.origin, buckets: bucketsFromSettings(settings) }),
    [settings],
  );
  const urlTransform = useMemo(() => makeUrlTransform(origins), [origins]);
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, ...extraRemarkPlugins]}
      // IMPORTANT: rehype-raw MUST run before rehype-sanitize so the raw
      // HTML nodes are materialized as HAST elements before we sanitize. The
      // image rewrite runs last so nothing added after sanitizing escapes it.
      rehypePlugins={[...(rawHtml ? [rehypeRaw] : []), [rehypeSanitize, safeSchema], ...extraRehypePlugins, rehypeRestrictImages(origins)]}
      urlTransform={urlTransform}
      components={components}
      {...(className ? ({ className } as Record<string, unknown>) : {})}
    >
      {content}
    </ReactMarkdown>
  );
};

export default SafeMarkdown;
