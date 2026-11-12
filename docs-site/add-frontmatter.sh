#!/bin/bash
# Adds YAML frontmatter to all docs/*.md files that don't already have it.
# Extracts the title from the first markdown heading (# Title).
# This is backward-compatible — GitHub/GitLab markdown renders frontmatter gracefully.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DOCS_DIR="$PROJECT_ROOT/docs"

echo "📝 Adding frontmatter to documentation files..."

count=0
skipped=0

BOM=$'\xef\xbb\xbf'

# True when the file's first non-blank line is "---". A leading byte-order mark,
# leading blank lines and surrounding whitespace are ignored, which is the rule
# scripts/tests/test_docs_license_header.py applies when it reads the same pages
# (strip the BOM, strip leading whitespace, compare the first line to "---").
# Checking only line 1 would prepend a second frontmatter block to a page that
# opens with a blank line.
has_frontmatter() {
    local line first=1
    while IFS= read -r line || [ -n "$line" ]; do
        if [ "$first" = 1 ]; then
            line=${line#"$BOM"}
            first=0
        fi
        # Trim leading and trailing whitespace (including a CR from CRLF).
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        if [ -n "$line" ]; then
            [ "$line" = "---" ]
            return
        fi
    done < "$1"
    return 1
}

for md_file in "$DOCS_DIR"/*.md; do
    filename=$(basename "$md_file")
    
    if has_frontmatter "$md_file"; then
        echo "   ⏭️  $filename (already has frontmatter)"
        skipped=$((skipped + 1))
        continue
    fi
    
    # Extract title from first markdown heading
    title=$(grep -m 1 '^# ' "$md_file" | sed 's/^# //')
    
    if [ -z "$title" ]; then
        # Fallback: use filename without extension
        title=$(basename "$md_file" .md | sed 's/-/ /g' | sed 's/\b\(.\)/\u\1/g')
    fi
    
    # Escape any quotes in the title
    escaped_title=$(echo "$title" | sed "s/'/\\\\'/g")
    
    # Create temp file with frontmatter prepended
    {
        echo "---"
        echo "title: \"$escaped_title\""
        echo "---"
        echo ""
        cat "$md_file"
    } > "$md_file.tmp"
    
    mv "$md_file.tmp" "$md_file"
    echo "   ✅ $filename → \"$title\""
    count=$((count + 1))
done

echo ""
echo "✨ Done! Added frontmatter to $count files, skipped $skipped."
