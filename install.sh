#!/usr/bin/env bash
# Install this pack's skills into a Hermes Agent installation.
#
#   ./install.sh            symlink skills (edits here take effect immediately)
#   ./install.sh --copy     copy instead of symlinking
#   ./install.sh --uninstall
#
# Skills land in $HERMES_HOME/skills/finance/ (default ~/.hermes/skills/finance).

set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
TARGET_DIR="$HERMES_HOME/skills/finance"
MODE="symlink"

for arg in "$@"; do
  case "$arg" in
    --copy)      MODE="copy" ;;
    --uninstall) MODE="uninstall" ;;
    -h|--help)   sed -n '2,9p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *)           echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

SKILLS=(options-chain options-strategy position-sizing trade-journal market-brief)

if [ "$MODE" = "uninstall" ]; then
  for skill in "${SKILLS[@]}"; do
    if [ -e "$TARGET_DIR/$skill" ] || [ -L "$TARGET_DIR/$skill" ]; then
      rm -rf "$TARGET_DIR/$skill"
      echo "removed  $skill"
    fi
  done
  echo
  echo "Done. Your trade journal at $HERMES_HOME/trade_journal.db was left untouched."
  exit 0
fi

mkdir -p "$TARGET_DIR"

for skill in "${SKILLS[@]}"; do
  src="$SOURCE_DIR/skills/finance/$skill"
  dest="$TARGET_DIR/$skill"

  if [ ! -d "$src" ]; then
    echo "missing source for $skill, skipping" >&2
    continue
  fi

  # Refuse to clobber a real directory that isn't ours.
  if [ -d "$dest" ] && [ ! -L "$dest" ] && [ "$MODE" = "symlink" ]; then
    echo "warning: $dest exists as a real directory; leaving it alone" >&2
    continue
  fi

  rm -rf "$dest"
  if [ "$MODE" = "copy" ]; then
    cp -R "$src" "$dest"
    echo "copied   $skill"
  else
    ln -s "$src" "$dest"
    echo "linked   $skill"
  fi
done

chmod +x "$SOURCE_DIR"/skills/finance/*/scripts/*.py 2>/dev/null || true

echo
echo "Installed to $TARGET_DIR"
echo
echo "Next steps:"
echo "  1. Create a watchlist:  echo AAPL >> $HERMES_HOME/watchlist.txt"
echo "  2. Restart Hermes so it picks up the new skills (or run: hermes skills list)"
echo "  3. Try it:              hermes  ->  \"what's the AAPL option chain look like?\""
