/**
 * Remark plugin that rewrites relative markdown links for the Starlight docs site.
 *
 * The docs in docs/ contain links like `[Classification](./classification.md)` which
 * work when browsing files in the Git repo. On the Starlight site, pages are served
 * at directory-style URLs like `/classification/` rather than `/classification.md`.
 *
 * From a page like `/architecture/`, a link to `./classification.md` would resolve to
 * `/architecture/classification.md` (404). This plugin rewrites it to `../classification/`
 * which correctly resolves to the sibling page.
 *
 * Pages can also be nested one level below docs/ — `docs/extensions/*.md` and
 * `docs/benchmarking/*.md` are symlinked into `src/content/docs/<dir>/` by
 * setup.sh and served at `/<dir>/<slug>/`. From those pages a link like
 * `../quick-start.md` means `docs/quick-start.md`, i.e. the site page `/quick-start/`,
 * NOT a file at the repository root. The plugin therefore resolves every relative
 * `.md` link against the page's own location under `src/content/docs/` and only
 * sends links that escape the docs/ tree to GitHub.
 *
 * Transformations (from a top-level page, e.g. docs/architecture.md):
 *   ./some-doc.md           → ../some-doc/
 *   ./some-doc.md#section   → ../some-doc/#section
 *   some-doc.md             → ../some-doc/
 *   ../README.md            → https://github.com/…/blob/main/README.md
 *
 * Transformations (from a nested page, e.g. docs/extensions/auto-optimizer.md):
 *   ./sibling.md            → ../sibling/                (stays in /extensions/)
 *   ../quick-start.md       → ../../quick-start/
 *   ../../README.md         → https://github.com/…/blob/main/README.md
 */
import { posix as path } from "node:path";
import { visit } from "unist-util-visit";

const GITHUB_REPO =
  "https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws";

const CONTENT_DOCS_MARKER = "/src/content/docs/";

/**
 * Directory of the current page relative to src/content/docs/ ("" for a
 * top-level page, "extensions" for docs/extensions/*.md). Falls back to ""
 * when the file path is unavailable so top-level behaviour is unchanged.
 */
function pageDirWithinDocs(file) {
  const filePath = (file && (file.path || (file.history && file.history[0]))) || "";
  const idx = filePath.lastIndexOf(CONTENT_DOCS_MARKER);
  if (idx === -1) return "";
  const rel = filePath.slice(idx + CONTENT_DOCS_MARKER.length);
  const dir = path.dirname(rel);
  return dir === "." ? "" : dir;
}

export default function remarkRewriteDocsLinks() {
  return (tree, file) => {
    const pageDir = pageDirWithinDocs(file);

    visit(tree, "link", (node) => {
      const url = node.url;

      // Skip absolute URLs, anchors-only, and non-.md links
      if (!url || url.startsWith("http") || url.startsWith("#")) return;
      if (!url.includes(".md")) return;

      const match = url.match(/^([^#]*\.md)(#[a-zA-Z0-9_-]*)?$/);
      if (!match) return;
      const [, mdPath, anchorPart] = match;
      const anchor = anchorPart || "";

      // Resolve the link target relative to docs/ (posix, no leading "./").
      // A leading "../" after normalisation means it escaped the docs/ tree.
      const targetWithinDocs = path.normalize(path.join(pageDir, mdPath));

      if (targetWithinDocs.startsWith("../")) {
        // Links going outside docs/ — point to GitHub repo
        const repoPath = targetWithinDocs.replace(/^\.\.\//, "");
        node.url = `${GITHUB_REPO}/blob/main/${repoPath}`;
        return;
      }

      // Inside docs/: the page is served at a directory-style URL
      // (/<pageDir>/<slug>/), so first step out of the page's own directory
      // ("../"), then out of each nesting level of pageDir, then down to the
      // target's directory-style URL.
      const pageDepth = pageDir === "" ? 0 : pageDir.split("/").length;
      const targetDir = path.dirname(targetWithinDocs);
      const targetSlug = path.basename(targetWithinDocs, ".md").toLowerCase();
      const targetPrefix = targetDir === "." ? "" : `${targetDir.toLowerCase()}/`;
      node.url = `${"../".repeat(pageDepth + 1)}${targetPrefix}${targetSlug}/${anchor}`;
    });
  };
}
