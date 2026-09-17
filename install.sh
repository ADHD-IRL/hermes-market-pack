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

# Windows (Git Bash / MSYS / Cygwin) keeps Hermes under %LOCALAPPDATA%\hermes,
# not ~/.hermes. Getting this wrong installs into a directory Hermes never reads.
IS_WINDOWS=0
case "${OSTYPE:-}" in
  msys* | cygwin* | win32) IS_WINDOWS=1 ;;
esac

if [ -z "${HERMES_HOME:-}" ]; then
  if [ "$IS_WINDOWS" -eq 1 ] && [ -n "${LOCALAPPDATA:-}" ]; then
    if command -v cygpath >/dev/null 2>&1; then
      HERMES_HOME="$(cygpath -u "$LOCALAPPDATA")/hermes"
    else
      HERMES_HOME="$HOME/AppData/Local/hermes"
    fi
  else
    HERMES_HOME="$HOME/.hermes"
  fi
fi

TARGET_DIR="$HERMES_HOME/skills/finance"

# Symlinks on Windows need Developer Mode or an elevated shell; Git Bash
# silently copies instead. Default to an honest copy there.
if [ "$IS_WINDOWS" -eq 1 ]; then
  MODE="copy"
else
  MODE="symlink"
fi

for arg in "$@"; do
  case "$arg" in
    --copy)      MODE="copy" ;;
    --uninstall) MODE="uninstall" ;;
    -h|--help)   sed -n '2,9p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *)           echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

SKILLS=(options-chain options-strategy position-sizing trade-journal market-brief trailing-buy)

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
if [ "$MODE" = "copy" ]; then
  echo "(copied, not linked - re-run this script after pulling updates)"
fi
echo
echo "Next steps:"
echo "  1. Create a watchlist:  echo AAPL >> $HERMES_HOME/watchlist.txt"
echo "  2. Restart Hermes so it picks up the new skills (or run: hermes skills list)"
echo "  3. Try it:              hermes  ->  \"what's the AAPL option chain look like?\""
