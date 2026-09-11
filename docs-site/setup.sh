#!/bin/bash
# Setup script for GenAIIDP Starlight documentation site
# Creates symlinks from existing docs/ and images/ into the Starlight content structure
# This avoids any content duplication — docs live in their original location

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONTENT_DOCS="$SCRIPT_DIR/src/content/docs"
CONTENT_ROOT="$SCRIPT_DIR/src/content"

echo "📁 Setting up GenAIIDP documentation site..."
echo "   Project root: $PROJECT_ROOT"
echo "   Content docs: $CONTENT_DOCS"

# Ensure content/docs directory exists
mkdir -p "$CONTENT_DOCS"

# Step 1: Symlink all .md files from docs/ into src/content/docs/
# Path: docs-site/src/content/docs/ → 4 levels up to project root
echo ""
echo "🔗 Creating symlinks for documentation files..."
count=0
for md_file in "$PROJECT_ROOT"/docs/*.md; do
    filename=$(basename "$md_file")
    # Skip README.md — we have our own index.mdx landing page
    if [ "$filename" = "README.md" ]; then
        continue
    fi
    target="$CONTENT_DOCS/$filename"
    # Remove existing symlink if present
    [ -L "$target" ] && rm "$target"
    ln -s "../../../../docs/$filename" "$target"
    count=$((count + 1))
done
echo "   ✅ Linked $count documentation files"

# Step 1b: Symlink per-extension docs from docs/extensions/ into
# src/content/docs/extensions/ so they publish under the "extensions/<slug>"
# route (matching the docsUrl slug each OSS feature declares in feature.yaml).
if [ -d "$PROJECT_ROOT/docs/extensions" ]; then
    echo ""
    echo "🔗 Creating symlinks for extension docs..."
    mkdir -p "$CONTENT_DOCS/extensions"
    ext_count=0
    for md_file in "$PROJECT_ROOT"/docs/extensions/*.md; do
        [ -e "$md_file" ] || continue
        filename=$(basename "$md_file")
        target="$CONTENT_DOCS/extensions/$filename"
        # Path: docs-site/src/content/docs/extensions/ → 5 levels up to project root
        [ -L "$target" ] && rm "$target"
        ln -s "../../../../../docs/extensions/$filename" "$target"
        ext_count=$((ext_count + 1))
    done
    echo "   ✅ Linked $ext_count extension docs"
fi

# Step 1c: Symlink the docs/benchmarking/ folder (guide + config-guidance + the
# per-release audit trail under releases/) into src/content/docs/benchmarking/.
if [ -d "$PROJECT_ROOT/docs/benchmarking" ]; then
    echo ""
    echo "🔗 Creating symlinks for benchmarking docs..."
    mkdir -p "$CONTENT_DOCS/benchmarking/releases"
    bench_count=0
    for md_file in "$PROJECT_ROOT"/docs/benchmarking/*.md; do
        [ -e "$md_file" ] || continue
        filename=$(basename "$md_file")
        target="$CONTENT_DOCS/benchmarking/$filename"
        # Path: docs-site/src/content/docs/benchmarking/ → 5 levels up to project root
        [ -L "$target" ] && rm "$target"
        ln -s "../../../../../docs/benchmarking/$filename" "$target"
        bench_count=$((bench_count + 1))
    done
    for md_file in "$PROJECT_ROOT"/docs/benchmarking/releases/*.md; do
        [ -e "$md_file" ] || continue
        filename=$(basename "$md_file")
        target="$CONTENT_DOCS/benchmarking/releases/$filename"
        # Path: docs-site/src/content/docs/benchmarking/releases/ → 6 levels up
        [ -L "$target" ] && rm "$target"
        ln -s "../../../../../../docs/benchmarking/releases/$filename" "$target"
        bench_count=$((bench_count + 1))
    done
    echo "   ✅ Linked $bench_count benchmarking docs"
fi

# Step 1d: Symlink the docs/release-validation/ folder (index + the per-release
# validation record) into src/content/docs/release-validation/.
if [ -d "$PROJECT_ROOT/docs/release-validation" ]; then
    echo ""
    echo "🔗 Creating symlinks for release-validation docs..."
    mkdir -p "$CONTENT_DOCS/release-validation"
    relval_count=0
    for md_file in "$PROJECT_ROOT"/docs/release-validation/*.md; do
        [ -e "$md_file" ] || continue
        filename=$(basename "$md_file")
        target="$CONTENT_DOCS/release-validation/$filename"
        # Path: docs-site/src/content/docs/release-validation/ → 5 levels up
        [ -L "$target" ] && rm "$target"
        ln -s "../../../../../docs/release-validation/$filename" "$target"
        relval_count=$((relval_count + 1))
    done
    echo "   ✅ Linked $relval_count release-validation docs"
fi

# Step 2: Symlink images/ into src/content/images/ (for ../images/ relative paths in docs)
# Path: docs-site/src/content/ → 3 levels up to project root
echo ""
echo "🖼️  Setting up image symlinks..."
[ -L "$CONTENT_ROOT/images" ] && rm "$CONTENT_ROOT/images"
ln -s "../../../images" "$CONTENT_ROOT/images"
echo "   ✅ Linked images directory for relative paths"

# Also put images in public/ for absolute path references
# Path: docs-site/public/ → 2 levels up to project root
mkdir -p "$SCRIPT_DIR/public"
[ -L "$SCRIPT_DIR/public/images" ] && rm "$SCRIPT_DIR/public/images"
ln -s "../../images" "$SCRIPT_DIR/public/images"
echo "   ✅ Linked images directory for public serving"

echo ""
echo "✨ Setup complete! Run 'npm install && npm run dev' to start the dev server."
