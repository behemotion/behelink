#!/usr/bin/env bash
# Distribute the /handoff skill folder + HANDOFF.md to all sibling repos
# listed in this repo's HANDOFF.md Repository map.
#
# Auto-detects which agent skill layouts exist in this repo
# (.claude/skills/handoff/, .opencode/skills/handoff/, …) and replicates the
# same set to each sibling, substituting <SELF_SLUG> per repo.
#
# Usage:
#   bash .claude/skills/handoff/scripts/sync.sh
#   (works from anywhere — the script resolves paths from its own location)

set -euo pipefail

# Known agent skill layouts. Add to this list when supporting more runtimes.
KNOWN_LAYOUTS=(
  ".claude/skills/handoff"
  ".opencode/skills/handoff"
)

# --- Locate source repo root ------------------------------------------------
# Script lives at <repo>/<layout>/scripts/sync.sh — walk up until a parent
# contains a known agent dir.
script_dir="$(cd "$(dirname "$0")" && pwd)"
src_repo=""
d="$script_dir"
for _ in 1 2 3 4 5 6; do
  d="$(dirname "$d")"
  for layout in "${KNOWN_LAYOUTS[@]}"; do
    top="${layout%%/*}"
    if [ -d "$d/$top" ] && [ -d "$d/$layout" ]; then
      src_repo="$d"
      break 2
    fi
  done
done
if [ -z "$src_repo" ]; then
  echo "ERROR: could not locate source repo root from $script_dir" >&2
  exit 1
fi

parent_dir="$(dirname "$src_repo")"
self_slug="$(basename "$src_repo")"

# --- Detect which layouts exist in source -----------------------------------
present_layouts=()
for layout in "${KNOWN_LAYOUTS[@]}"; do
  if [ -d "$src_repo/$layout" ]; then
    present_layouts+=("$layout")
  fi
done

# --- Locate canonical HANDOFF.template.md -----------------------------------
template=""
for layout in "${present_layouts[@]}"; do
  if [ -f "$src_repo/$layout/HANDOFF.template.md" ]; then
    template="$src_repo/$layout/HANDOFF.template.md"
    break
  fi
done
if [ -z "$template" ]; then
  echo "ERROR: HANDOFF.template.md not found in any of: ${present_layouts[*]}" >&2
  exit 1
fi

# --- Parse Repository map from source HANDOFF.md ----------------------------
handoff_md="$src_repo/HANDOFF.md"
if [ ! -f "$handoff_md" ]; then
  echo "ERROR: $handoff_md not found. Run /handoff init first." >&2
  exit 1
fi

# Extract slugs from rows whose first cell is wrapped in backticks.
# Filters out the source repo's own slug.
siblings=()
while IFS= read -r slug; do
  [ -n "$slug" ] && siblings+=("$slug")
done < <(
  awk '
    /^## Repository map/ {in_map=1; next}
    in_map && /^## / {in_map=0}
    in_map && /^\|/ {print}
  ' "$handoff_md" \
  | grep -E '^[|][[:space:]]+`[^`]+`' \
  | sed -E 's/^[|][[:space:]]+`([^`]+)`.*/\1/' \
  | awk -v self="$self_slug" '$0 != self {print}'
)

echo "Source repo:    $src_repo  (slug: $self_slug)"
echo "Parent dir:     $parent_dir"
echo "Layouts found:  ${present_layouts[*]}"
echo "Siblings:       ${siblings[*]:-<none>}"
echo

if [ ${#siblings[@]} -eq 0 ]; then
  echo "No siblings in Repository map (single-repo mode). Nothing to distribute."
  echo "Edit HANDOFF.md to add siblings, or use /handoff self."
  exit 0
fi

# --- Distribute -------------------------------------------------------------
synced=0
for slug in "${siblings[@]}"; do
  target="$parent_dir/$slug"
  if [ ! -d "$target" ]; then
    echo "  SKIP $slug (directory not found at $target)"
    continue
  fi

  echo "  → $slug"

  for layout in "${present_layouts[@]}"; do
    mkdir -p "$target/$layout"
    rsync -a --delete "$src_repo/$layout/" "$target/$layout/"
  done

  sed "s|<SELF_SLUG>|$slug|g" "$template" > "$target/HANDOFF.md"
  synced=$((synced + 1))
done

echo
echo "Done. Distributed to $synced sibling repo(s)."
