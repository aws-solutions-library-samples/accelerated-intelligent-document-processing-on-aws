// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The rendering policy behind `SafeMarkdown`: the sanitize schema and the rule
 * deciding which URLs rendered markdown may load.
 *
 * Kept apart from the component so the schema and the URL rule can be exercised
 * directly by tests, and so the component file stays the only module that
 * imports a markdown renderer (`markdown-surfaces.test.ts` holds that).
 *
 * What rendered markdown may load, by construction:
 *   - an `<img>` whose URL is `data:image/...`, a `blob:` URL of this page's own
 *     origin, a URL on this page's own origin (relative URLs included), or an
 *     S3 URL for one of this deployment's own buckets (the names come from the
 *     settings the app already loads). Any other image is rendered as a link that
 *     shows its URL, so the user can still follow it deliberately;
 *   - nothing through CSS: the `<style>` element is removed together with its
 *     text, and a `style` attribute is kept only when every declaration is an
 *     allowed layout/typography property whose value contains no `(`, `\`, `@`,
 *     quote, `/`, `<` or `>` — so `url(...)`, `image-set(...)`, `expression(...)`,
 *     `@import` and escape sequences cannot be written in one;
 *   - nothing through `srcset`, `<picture>`/`<source>`, `poster`, `background`,
 *     `longdesc` or any other URL-bearing attribute: none is in the schema.
 *
 * Links (`<a href>`) keep their http/https/mailto targets. Following one is a
 * navigation the user chooses, not a load the page performs on its own.
 */

import { defaultSchema } from 'rehype-sanitize';

/** CSS properties a `style` attribute may set. None of them can fetch anything
 *  once the value alphabet below rules out functions. */
const STYLE_PROPERTIES = [
  'background',
  'background-color',
  'border',
  'border-top',
  'border-right',
  'border-bottom',
  'border-left',
  'border-color',
  'border-style',
  'border-width',
  'border-collapse',
  'border-spacing',
  'border-radius',
  'color',
  'font-size',
  'font-style',
  'font-weight',
  'line-height',
  'margin',
  'margin-top',
  'margin-right',
  'margin-bottom',
  'margin-left',
  'padding',
  'padding-top',
  'padding-right',
  'padding-bottom',
  'padding-left',
  'text-align',
  'text-decoration',
  'vertical-align',
  'white-space',
  'word-wrap',
  'word-break',
  'overflow-wrap',
  'table-layout',
  'width',
  'min-width',
  'max-width',
  'height',
];

/**
 * One declaration: an allowed property, then a value drawn from letters, digits,
 * whitespace and `# % . , -`. Anything that could name a resource in CSS needs a
 * character outside that alphabet: `(` for every function (`url`, `image-set`,
 * `src`, `expression`), `\` for an escape, `@` for an at-rule, a quote or `/` for
 * a string or comment.
 */
const STYLE_DECLARATION = `(?:${STYLE_PROPERTIES.join('|')})\\s*:\\s*[a-z0-9#%.,\\s-]+`;

/** A whole `style` attribute value: one or more declarations separated by `;`. */
export const STYLE_ATTRIBUTE_VALUE = new RegExp(`^\\s*${STYLE_DECLARATION}(?:;\\s*${STYLE_DECLARATION})*;?\\s*$`, 'i');

/**
 * Allow-list schema, an extension of `rehype-sanitize`'s `defaultSchema`.
 * Event handlers (`on*`), `<script>`, `<iframe>`, `javascript:` URLs and the like
 * are already excluded by the default and stay excluded.
 */
export const safeSchema = {
  ...defaultSchema,
  // `<style>` is dropped *with its contents*. Without this a removed `<style>`
  // element would leave its CSS text behind as visible paragraph text.
  strip: [...(defaultSchema.strip ?? []), 'style'],
  protocols: {
    ...defaultSchema.protocols,
    href: ['http', 'https', 'mailto'],
    // `data:` and `blob:` are narrowed further by `isAllowedImageUrl`.
    src: ['http', 'https', 'data', 'blob'],
    cite: ['http', 'https'],
  },
  tagNames: [
    // `<picture>`/`<source>` exist only to carry `srcset`, which names further
    // image URLs that no URL transform sees, so they are not rendered.
    ...(defaultSchema.tagNames ?? []).filter((tag) => tag !== 'picture' && tag !== 'source'),
    'details',
    'summary',
    // Custom tag emitted by query_knowledgebase_resolver; mapped to a
    // CustomLink React component in DocumentsQueryLayout.tsx via the
    // `components` prop.
    'documentid',
    // Custom tag emitted by the Quick Start agent to reference a bundled sample
    // document; mapped to a click-to-open component via the `components` prop.
    'sampledoc',
    // Table structure tags used by the rule validation summary.
    'colgroup',
    'col',
  ],
  attributes: {
    // `source` (whose only default attribute is `srcSet`) is not carried over.
    ...Object.fromEntries(Object.entries(defaultSchema.attributes ?? {}).filter(([tag]) => tag !== 'source')),
    p: [...(defaultSchema.attributes?.p ?? []), ['style', /^white-space:\s*pre-line;?$/i]],
    documentid: ['href', 'title', 'className'],
    sampledoc: ['s3key', 'title', 'className'],
    code: [...(defaultSchema.attributes?.code ?? []), 'className'],
    span: [...(defaultSchema.attributes?.span ?? []), 'className', ['style', STYLE_ATTRIBUTE_VALUE]],
    div: [...(defaultSchema.attributes?.div ?? []), 'className'],
    a: [...(defaultSchema.attributes?.a ?? []), 'target', 'rel'],
    // `src` only; `longDesc` (a second URL) is not carried over from the default.
    img: ['src', 'alt', 'width', 'height', 'ariaLabel', 'ariaDescribedBy', 'ariaLabelledBy'],
    // Table layout for the evaluation report and rule validation summary.
    table: [...(defaultSchema.attributes?.table ?? []), ['style', STYLE_ATTRIBUTE_VALUE]],
    tr: [...(defaultSchema.attributes?.tr ?? []), ['style', STYLE_ATTRIBUTE_VALUE]],
    th: [...(defaultSchema.attributes?.th ?? []), ['style', STYLE_ATTRIBUTE_VALUE]],
    td: [...(defaultSchema.attributes?.td ?? []), ['style', STYLE_ATTRIBUTE_VALUE]],
    col: [...(defaultSchema.attributes?.col ?? []), ['style', STYLE_ATTRIBUTE_VALUE]],
    colgroup: defaultSchema.attributes?.['*'] ?? [],
    details: [...(defaultSchema.attributes?.['*'] ?? []), 'open'],
    summary: defaultSchema.attributes?.['*'] ?? [],
  },
} as const;

/** Where the page is served from, and which S3 buckets belong to this deployment. */
export interface ResourceOrigins {
  /** `window.location.origin` of the page doing the rendering. */
  pageOrigin: string;
  /** Names of this deployment's own buckets. Empty means no S3 URL is allowed. */
  buckets: ReadonlySet<string>;
}

/** An AWS region name: `us-west-2`, `us-gov-west-1`, `cn-north-1`, `ap-southeast-3`. */
const REGION = '[a-z]{2}(?:-[a-z]+)+-\\d+';
/**
 * S3 endpoint host names, without the bucket, written as the actual endpoint
 * grammar rather than "anything starting with s3": `s3.amazonaws.com`,
 * `s3.<region>.amazonaws.com`, `s3-<region>.amazonaws.com`,
 * `s3.dualstack.<region>.amazonaws.com`, the `s3-fips` forms, and `.cn`.
 * A looser rule would read `own.s3-other.s3.amazonaws.com` as bucket `own`, when
 * S3 reads it as bucket `own.s3-other`. `amazonaws.com` is the endpoint suffix in
 * both the commercial and GovCloud partitions.
 */
const S3_ENDPOINT_HOST = new RegExp(`^s3(?:-fips)?(?:\\.dualstack)?(?:[.-]${REGION})?\\.amazonaws\\.com(?:\\.cn)?$`);
/** An S3 interface-endpoint host, without the bucket: `vpce-<id>.s3.<region>.vpce.amazonaws.com`. */
const S3_VPCE_HOST = new RegExp(`^vpce-[a-z0-9-]+\\.s3\\.${REGION}\\.vpce\\.amazonaws\\.com(?:\\.cn)?$`);

/** The bucket an https URL addresses, if it is an S3 URL in either addressing style. */
export const s3BucketOf = (url: URL): string | null => {
  if (url.protocol !== 'https:') return null;
  const host = url.hostname.toLowerCase();
  if (S3_ENDPOINT_HOST.test(host)) {
    // Path style: https://s3.<region>.amazonaws.com/<bucket>/<key>
    const first = url.pathname.split('/')[1];
    return first ? decodeURIComponent(first) : null;
  }
  // Virtual-hosted style: https://<bucket>.s3.<region>.amazonaws.com/<key>
  // A bucket name may itself contain dots, so try every split point.
  for (let i = host.indexOf('.'); i > 0; i = host.indexOf('.', i + 1)) {
    const rest = host.slice(i + 1);
    if (S3_ENDPOINT_HOST.test(rest) || S3_VPCE_HOST.test(rest)) return host.slice(0, i);
  }
  return null;
};

/**
 * Whether rendered markdown may load `raw` as an image.
 *
 * A URL that does not parse, or that parses to anything not listed here, is
 * refused: the rule is a list of what is allowed, not of what is known to be bad.
 */
export const isAllowedImageUrl = (raw: string, origins: ResourceOrigins): boolean => {
  const value = raw.trim();
  if (!value || /[\\\s]/.test(value)) return false;
  if (/^data:image\//i.test(value)) return true;
  let url: URL;
  try {
    url = new URL(value, `${origins.pageOrigin}/`);
  } catch {
    return false;
  }
  if (url.protocol === 'blob:') {
    // blob:https://app.example/<uuid> — only this page can mint one of these.
    return value.toLowerCase().startsWith(`blob:${origins.pageOrigin.toLowerCase()}/`);
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') return false;
  if (url.origin === origins.pageOrigin) return true;
  const bucket = s3BucketOf(url);
  return bucket !== null && origins.buckets.has(bucket);
};

/**
 * The settings keys that name this deployment's own buckets. Listed rather than
 * matched by suffix: settings also carry `PublicArtifactsBucket`, a shared public
 * release bucket the deployment does not own, and a future `*Bucket` key should
 * be added here deliberately rather than trusted by its name.
 */
export const DEPLOYMENT_BUCKET_SETTINGS = [
  'InputBucket',
  'OutputBucket',
  'DiscoveryBucket',
  'TestSetBucket',
  'ConfigurationBucket',
  'ReportingBucket',
  'EvaluationBaselineBucket',
] as const;

/** This deployment's bucket names, from the settings document. */
export const bucketsFromSettings = (settings: Record<string, unknown> | undefined | null): Set<string> => {
  const names = new Set<string>();
  for (const key of DEPLOYMENT_BUCKET_SETTINGS) {
    const value = settings?.[key];
    if (typeof value === 'string' && value.trim()) names.add(value.trim().toLowerCase());
  }
  return names;
};

/** The minimal slice of a HAST tree the image rewrite walks. */
interface HastNode {
  type: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
  value?: string;
}

/**
 * A rehype plugin that replaces every `<img>` the policy refuses with a link that
 * shows the image's URL (or plain text when the URL is not http/https), so the
 * image is never requested but the reader can still see and follow it.
 */
export const rehypeRestrictImages = (origins: ResourceOrigins) => () => (tree: HastNode) => {
  const visit = (node: HastNode): void => {
    if (!node.children) return;
    node.children = node.children.map((child) => {
      if (child.type === 'element' && child.tagName === 'img') {
        const src = typeof child.properties?.src === 'string' ? child.properties.src : '';
        if (isAllowedImageUrl(src, origins)) return child;
        const label = src || String(child.properties?.alt ?? 'image');
        const isWebLink = /^https?:\/\//i.test(src.trim());
        if (!isWebLink) return { type: 'element', tagName: 'span', properties: {}, children: [{ type: 'text', value: label }] };
        return {
          type: 'element',
          tagName: 'a',
          properties: { href: src.trim(), target: '_blank', rel: ['noopener', 'noreferrer'] },
          children: [{ type: 'text', value: label }],
        };
      }
      visit(child);
      return child;
    });
  };
  visit(tree);
};
