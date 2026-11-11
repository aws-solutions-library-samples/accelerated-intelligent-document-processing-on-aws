// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Markdown and raw HTML reach the page only through `SafeMarkdown`.
 *
 * `SafeMarkdown` is where the sanitize schema and the image rule live, so a module
 * that renders markdown with its own renderer, or writes an HTML string into the
 * DOM, renders that text with neither. The rule is stated about capability rather
 * than spelling: a module may not *import* a markdown/HTML renderer package (by any
 * import form the TypeScript parser recognises: static, re-export, `import()`,
 * `require()`, `import x = require()`), and may not perform a raw-HTML DOM write
 * (`dangerouslySetInnerHTML` as a JSX attribute or an object key, assignment to
 * `innerHTML`/`outerHTML` by property or element access, `insertAdjacentHTML`,
 * `write`/`writeln` on any object (a document is easily aliased), `createContextualFragment`, `srcdoc`/`srcDoc`).
 * Type-only imports are allowed anywhere, since they cannot render.
 *
 * The files allowed either capability are listed in `markdown-surfaces-allowlist.json`
 * with a reason and an expected count of sites each, so a new site inside a listed file
 * fails as surely as one outside it, and a listed file that no longer holds its sites
 * fails as stale. That file is registered in `scripts/tests/gate_exemptions.json`.
 *
 * Scope: application code under `src/ui/src` and every feature UI under
 * `feature-platform/`, read from the filesystem (so an uncommitted file is read too),
 * skipping `node_modules`, build output and test files, which do not ship.
 *
 * It also flags `DOMParser.parseFromString`, `Object.defineProperty`/`Reflect.set` on
 * `innerHTML`/`outerHTML`, computed keys spelled as string literals, and any string
 * or template text naming `text/html` (a `data:` frame, an HTML `Blob`), `setAttributeNS` of `srcdoc`, and `execCommand('insertHTML')`.
 *
 * What it does not see: a renderer reached without an import the parser can read
 * (a global, a computed `import()` specifier, `eval`), a renderer package outside
 * `RENDERER_PACKAGES` whose name does not contain "markdown", a property name or
 * MIME type assembled at run time, or a `data:`/`blob:` frame whose MIME type is a
 * variable, or an `image/svg+xml` document put in a frame (SVG data URLs are used
 * as `<img>` sources, where they cannot run script, so the MIME type alone is not
 * flagged). Those are not spellings anyone writes by accident.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, resolve, sep } from 'node:path';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';

import allowlist from './markdown-surfaces-allowlist.json';

const REPO_ROOT = resolve(__dirname, '..', '..', '..', '..', '..', '..');
const SCAN_ROOTS = [join(REPO_ROOT, 'src', 'ui', 'src'), join(REPO_ROOT, 'feature-platform')];
const SKIP_DIRS = new Set(['node_modules', 'dist', 'build', 'coverage', '.git', '__tests__', '__mocks__']);
const SOURCE_EXT = /\.(?:[cm]?[jt]sx?)$/;
const TEST_FILE = /\.(?:test|spec)\.[cm]?[jt]sx?$/;

/** Packages that turn markdown or an HTML string into DOM. Matched as the package
 *  name or any subpath of it. */
const RENDERER_PACKAGES = [
  'react-markdown',
  'marked',
  'markdown-it',
  'rehype-raw',
  'remark-rehype',
  'remark-html',
  'rehype-stringify',
  'rehype-react',
  'hast-util-raw',
  'hast-util-to-html',
  'hast-util-to-jsx-runtime',
  'mdast-util-to-hast',
  'micromark',
  'markdown-to-jsx',
  'showdown',
  'snarkdown',
  'remarkable',
  'commonmark',
  'html-react-parser',
  // A sanitizer is only imported to render the HTML string it returns.
  'dompurify',
  'isomorphic-dompurify',
  'sanitize-html',
  '@mdx-js/react',
  '@mdx-js/mdx',
];

const HTML_WRITE_PROPERTIES = new Set(['innerHTML', 'outerHTML']);
const HTML_WRITE_METHODS = new Set([
  'insertAdjacentHTML',
  'createContextualFragment',
  'setHTMLUnsafe',
  'parseHTMLUnsafe',
  // DOMParser: its document is inert, but the only reason to parse HTML in the UI is
  // to move the result into the page.
  'parseFromString',
]);
/** Reflective writes: `Object.defineProperty(el, 'innerHTML', ...)`, `Reflect.set(el, 'innerHTML', x)`. */
const REFLECTIVE_WRITERS = new Set(['defineProperty', 'set']);
const DOCUMENT_WRITE_METHODS = new Set(['write', 'writeln']);
const HTML_WRITE_ATTRIBUTES = new Set(['dangerouslySetInnerHTML', 'srcdoc', 'srcDoc']);

type Capability = 'renderer' | 'html-write';
interface Site {
  capability: Capability;
  line: number;
  what: string;
}

const isRendererSpecifier = (specifier: string): boolean =>
  RENDERER_PACKAGES.some((pkg) => specifier === pkg || specifier.startsWith(`${pkg}/`)) ||
  // Any package with "markdown" in its name (`@uiw/react-markdown-preview`, ...), so
  // the list above is a floor, not the whole rule.
  /markdown/i.test(
    specifier
      .split('/')
      .slice(0, specifier.startsWith('@') ? 2 : 1)
      .join('/'),
  ) ||
  specifier === 'html-to-react' ||
  specifier.startsWith('html-to-react/');

const nameOf = (node: ts.Node | undefined): string | undefined => {
  if (!node) return undefined;
  if (ts.isIdentifier(node) || ts.isPrivateIdentifier(node)) return node.text;
  if (ts.isStringLiteralLike(node)) return node.text;
  if (ts.isComputedPropertyName(node)) return nameOf(node.expression);
  if (ts.isJsxNamespacedName(node)) return node.name.text;
  return undefined;
};

/** Every renderer import and raw-HTML write in one source file. */
const findSites = (fileName: string, source: string): Site[] => {
  const sf = ts.createSourceFile(
    fileName,
    source,
    ts.ScriptTarget.Latest,
    true,
    fileName.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const sites: Site[] = [];
  const add = (capability: Capability, node: ts.Node, what: string): void => {
    sites.push({ capability, line: sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1, what });
  };
  const specifierSite = (node: ts.Node, specifier: ts.Expression | undefined, typeOnly: boolean): void => {
    if (!specifier || !ts.isStringLiteralLike(specifier) || typeOnly) return;
    if (isRendererSpecifier(specifier.text)) add('renderer', node, `imports ${specifier.text}`);
  };

  const visit = (node: ts.Node): void => {
    if (ts.isImportDeclaration(node)) {
      const clause = node.importClause;
      const bindings = clause?.namedBindings;
      const allNamedAreTypes =
        !!bindings && ts.isNamedImports(bindings) && bindings.elements.length > 0 && bindings.elements.every((e) => e.isTypeOnly);
      const typeOnly = !!clause && (clause.isTypeOnly || (!clause.name && allNamedAreTypes));
      specifierSite(node, node.moduleSpecifier, typeOnly);
    } else if (ts.isExportDeclaration(node)) {
      specifierSite(node, node.moduleSpecifier, node.isTypeOnly);
    } else if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference)) {
      specifierSite(node, node.moduleReference.expression, node.isTypeOnly);
    } else if (ts.isCallExpression(node)) {
      const callee = node.expression;
      if (callee.kind === ts.SyntaxKind.ImportKeyword || (ts.isIdentifier(callee) && callee.text === 'require')) {
        specifierSite(node, node.arguments[0], false);
      }
      const method = ts.isPropertyAccessExpression(callee)
        ? callee.name.text
        : ts.isElementAccessExpression(callee)
          ? nameOf(callee.argumentExpression)
          : undefined;
      if (method && HTML_WRITE_METHODS.has(method)) add('html-write', node, `calls ${method}`);
      // Any object's write/writeln: a document is easily aliased (`const d = w.document`),
      // and no other write() call exists in UI code to make this noisy.
      if (method && DOCUMENT_WRITE_METHODS.has(method)) add('html-write', node, `calls ${method}`);
      if (method && REFLECTIVE_WRITERS.has(method) && HTML_WRITE_PROPERTIES.has(nameOf(node.arguments[1]) ?? '')) {
        add('html-write', node, `${method} ${nameOf(node.arguments[1])}`);
      }
      if (method === 'setAttributeNS' && HTML_WRITE_ATTRIBUTES.has(nameOf(node.arguments[1]) ?? '')) {
        add('html-write', node, `sets ${nameOf(node.arguments[1])}`);
      }
      if (method === 'execCommand' && /^insertHTML$/i.test(nameOf(node.arguments[0]) ?? ''))
        add('html-write', node, 'execCommand insertHTML');
      if (method === 'setAttribute' && HTML_WRITE_ATTRIBUTES.has(nameOf(node.arguments[0]) ?? '')) {
        add('html-write', node, `sets ${nameOf(node.arguments[0])}`);
      }
    } else if (
      ts.isBinaryExpression(node) &&
      node.operatorToken.kind >= ts.SyntaxKind.FirstAssignment &&
      node.operatorToken.kind <= ts.SyntaxKind.LastAssignment
    ) {
      const left = node.left;
      const prop = ts.isPropertyAccessExpression(left)
        ? left.name.text
        : ts.isElementAccessExpression(left)
          ? nameOf(left.argumentExpression)
          : undefined;
      if (prop && (HTML_WRITE_PROPERTIES.has(prop) || HTML_WRITE_ATTRIBUTES.has(prop))) add('html-write', node, `assigns ${prop}`);
    } else if (ts.isJsxAttribute(node)) {
      const attr = nameOf(node.name);
      if (attr && HTML_WRITE_ATTRIBUTES.has(attr)) add('html-write', node, `JSX ${attr}`);
    } else if (
      (ts.isStringLiteralLike(node) || ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node)) &&
      /text\/html/i.test(node.text)
    ) {
      // An HTML document built from a string: a `data:text/html,...` frame or a
      // `Blob` of type text/html. FileViewer builds its type from a variable, so
      // its data: URL is covered by its DOMPurify import instead.
      add('html-write', node, 'builds text/html content');
    } else if (ts.isPropertyAssignment(node) || ts.isShorthandPropertyAssignment(node)) {
      const key = nameOf(node.name);
      if (key && (HTML_WRITE_ATTRIBUTES.has(key) || HTML_WRITE_PROPERTIES.has(key))) add('html-write', node, `object key ${key}`);
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return sites;
};

const walk = (dir: string, out: string[]): void => {
  let names: string[];
  try {
    names = readdirSync(dir);
  } catch {
    return;
  }
  for (const name of names) {
    if (SKIP_DIRS.has(name)) continue;
    const full = join(dir, name);
    const stat = statSync(full);
    if (stat.isDirectory()) walk(full, out);
    else if (SOURCE_EXT.test(name) && !TEST_FILE.test(name) && !name.endsWith('.d.ts')) out.push(full);
  }
};

const toRepoPath = (full: string): string => relative(REPO_ROOT, full).split(sep).join('/');

const scanTree = (): { files: string[]; found: Map<string, Site[]> } => {
  const files: string[] = [];
  for (const root of SCAN_ROOTS) walk(root, files);
  const found = new Map<string, Site[]>();
  for (const file of files) {
    const sites = findSites(file, readFileSync(file, 'utf8'));
    if (sites.length) found.set(toRepoPath(file), sites);
  }
  return { files, found };
};

interface AllowEntry {
  capability: Capability;
  sites: number;
  reason: string;
}
const ALLOWED = allowlist.entries as Record<string, AllowEntry>;

describe('markdown and raw HTML reach the page only through SafeMarkdown', () => {
  const { files, found } = scanTree();

  it('reads the trees it claims to scan', () => {
    // An empty or mis-rooted walk would pass every assertion below.
    expect(files.length).toBeGreaterThan(200);
    expect(files.some((f) => toRepoPath(f) === 'src/ui/src/components/common/SafeMarkdown.tsx')).toBe(true);
    expect(files.some((f) => toRepoPath(f).startsWith('feature-platform/'))).toBe(true);
  });

  it('finds no renderer import or HTML write outside the allowlist', () => {
    const unregistered = [...found.entries()]
      .filter(([path]) => !(path in ALLOWED))
      .map(([path, sites]) => sites.map((s) => `${path}:${s.line} ${s.what}`))
      .flat();
    expect(
      unregistered,
      'render markdown with SafeMarkdown (src/ui/src/components/common/SafeMarkdown.tsx), or ' +
        'register the file in markdown-surfaces-allowlist.json with its reason',
    ).toEqual([]);
  });

  it('holds each allowlisted file to its capability and its pinned count of sites', () => {
    // A listed file holding a different number of sites fails: a new site inside it
    // is not covered by the reason written for the old ones, and one that holds none
    // is a stale entry that would pre-approve whatever next lands in that file.
    const mismatched: string[] = [];
    for (const [path, entry] of Object.entries(ALLOWED)) {
      expect(entry.reason.length, `${path} needs a reason`).toBeGreaterThan(20);
      const sites = found.get(path) ?? [];
      const wrongKind = sites.filter((s) => s.capability !== entry.capability);
      if (sites.length !== entry.sites || wrongKind.length) {
        mismatched.push(
          `${path}: expected count ${entry.sites} ${entry.capability}, found ${sites.map((s) => `${s.capability}@${s.line}`).join(', ') || 'none'}`,
        );
      }
    }
    expect(mismatched).toEqual([]);
  });
});

describe('the scanner recognises every form it claims to', () => {
  const cases: Array<[string, string, Capability | null]> = [
    ['static import', `import ReactMarkdown from 'react-markdown';`, 'renderer'],
    ['named import', `import { marked } from 'marked';`, 'renderer'],
    ['subpath import', `import x from 'markdown-it/lib/renderer';`, 'renderer'],
    ['side-effect import', `import 'rehype-raw';`, 'renderer'],
    ['re-export', `export { default } from 'react-markdown';`, 'renderer'],
    ['star re-export', `export * from 'marked';`, 'renderer'],
    ['dynamic import', `const m = await import('react-markdown');`, 'renderer'],
    ['require', `const m = require('markdown-it');`, 'renderer'],
    ['import equals', `import m = require('marked');`, 'renderer'],
    ['type-only import', `import type { Components } from 'react-markdown';`, null],
    ['inline type-only names', `import { type Components } from 'react-markdown';`, null],
    ['mixed type and value', `import Md, { type Components } from 'react-markdown';`, 'renderer'],
    ['markdown-named look-alike', `import x from 'react-markdown-editor';`, 'renderer'],
    ['unrelated package', `import x from 'react-mark-highlight';`, null],
    ['comment mention', `// import ReactMarkdown from 'react-markdown'`, null],
    ['JSX dangerouslySetInnerHTML', `const a = <div dangerouslySetInnerHTML={{ __html: x }} />;`, 'html-write'],
    ['spread object key', `const p = { dangerouslySetInnerHTML: { __html: x } };`, 'html-write'],
    ['quoted object key', `const p = { 'dangerouslySetInnerHTML': { __html: x } };`, 'html-write'],
    ['innerHTML assignment', `el.innerHTML = x;`, 'html-write'],
    ['innerHTML append', `el.innerHTML += x;`, 'html-write'],
    ['element-access innerHTML', `el['innerHTML'] = x;`, 'html-write'],
    ['outerHTML', `el.outerHTML = x;`, 'html-write'],
    ['Object.assign innerHTML', `Object.assign(el, { innerHTML: x });`, 'html-write'],
    ['setHTMLUnsafe', `el.setHTMLUnsafe(x);`, 'html-write'],
    ['DOMParser', `const d = new DOMParser().parseFromString(x, mime);`, 'html-write'],
    ['data:text/html frame', 'const f = <iframe src={`data:text/html,${x}`} />;', 'html-write'],
    ['HTML Blob', `const b = new Blob([x], { type: 'text/html' });`, 'html-write'],
    ['setAttributeNS srcdoc', `frame.setAttributeNS(null, 'srcdoc', x);`, 'html-write'],
    ['execCommand insertHTML', `document.execCommand('insertHTML', false, x);`, 'html-write'],
    ['computed dangerouslySetInnerHTML key', `const p = { ['dangerouslySetInnerHTML']: { __html: x } };`, 'html-write'],
    ['defineProperty innerHTML', `Object.defineProperty(el, 'innerHTML', { value: x });`, 'html-write'],
    ['Reflect.set innerHTML', `Reflect.set(el, 'innerHTML', x);`, 'html-write'],
    ['markdown-named package', `import Preview from '@uiw/react-markdown-preview';`, 'renderer'],
    ['html-to-react', `import { Parser } from 'html-to-react';`, 'renderer'],
    ['sanitizer import', `import DOMPurify from 'dompurify';`, 'renderer'],
    ['innerHTML read', `const h = el.innerHTML;`, null],
    ['insertAdjacentHTML', `el.insertAdjacentHTML('beforeend', x);`, 'html-write'],
    ['createContextualFragment', `document.createRange().createContextualFragment(x);`, 'html-write'],
    ['document.write', `w.document.write(x);`, 'html-write'],
    ['bare document.writeln', `document.writeln(x);`, 'html-write'],
    ['aliased document.write', `const d = w.document; d.write(x);`, 'html-write'],
    ['srcdoc JSX', `const f = <iframe srcDoc={x} />;`, 'html-write'],
    ['srcdoc property', `frame.srcdoc = x;`, 'html-write'],
    ['srcdoc setAttribute', `frame.setAttribute('srcdoc', x);`, 'html-write'],
  ];
  it.each(cases)('%s', (_name, code, expected) => {
    const sites = findSites('probe.tsx', code);
    if (expected === null) expect(sites).toEqual([]);
    else expect(sites.map((s) => s.capability)).toEqual([expected]);
  });
});
