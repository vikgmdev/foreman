#!/usr/bin/env bash
#
# foreman installer — one line, no dependencies beyond python3 + git or curl.
#
#   curl -fsSL https://raw.githubusercontent.com/vikgmdev/foreman/main/install.sh | bash
#
# What it does (and nothing else):
#   1. Puts foreman in ~/.foreman            (override: FOREMAN_DIR)
#   2. Shims `foreman` into ~/.local/bin     (override: FOREMAN_BIN_DIR)
#   3. Adds that dir to PATH in your shell rc if missing
#
# Re-running updates in place. Uninstall: rm -rf ~/.foreman ~/.local/bin/foreman
# (run `foreman hook uninstall` first if you installed the hook).
set -euo pipefail

REPO_HTTPS="${FOREMAN_REPO:-https://github.com/vikgmdev/foreman.git}"
TARBALL="https://github.com/vikgmdev/foreman/archive/refs/heads/main.tar.gz"
DIR="${FOREMAN_DIR:-$HOME/.foreman}"
BIN_DIR="${FOREMAN_BIN_DIR:-$HOME/.local/bin}"

say()  { printf '\033[1;34m[foreman]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[foreman]\033[0m %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 \
  || fail "python3 (>= 3.9) is required and was not found on PATH."

# Refuse to clobber a directory that isn't ours.
if [ -d "$DIR" ] && [ ! -f "$DIR/foreman.py" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
  fail "$DIR exists and doesn't look like a foreman install — set FOREMAN_DIR elsewhere."
fi

# Fetch or update.
if [ -d "$DIR/.git" ] && command -v git >/dev/null 2>&1; then
  say "updating existing install in $DIR"
  git -C "$DIR" pull --ff-only --quiet
elif command -v git >/dev/null 2>&1; then
  say "cloning into $DIR"
  git clone --depth 1 --quiet "$REPO_HTTPS" "$DIR"
else
  say "git not found — downloading tarball into $DIR"
  mkdir -p "$DIR"
  curl -fsSL "$TARBALL" | tar -xz --strip-components=1 -C "$DIR"
fi

# Shim.
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/foreman" <<EOF
#!/bin/sh
exec python3 "$DIR/foreman.py" "\$@"
EOF
chmod +x "$BIN_DIR/foreman"

# PATH.
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    added=""
    for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
      [ -f "$rc" ] || continue
      if ! grep -q 'foreman installer' "$rc" 2>/dev/null; then
        printf '\nexport PATH="%s:$PATH"  # added by foreman installer\n' "$BIN_DIR" >> "$rc"
        added="$added $rc"
      fi
    done
    [ -n "$added" ] && say "added $BIN_DIR to PATH in:$added (open a new shell)" \
                    || say "add $BIN_DIR to your PATH to use the 'foreman' command"
    ;;
esac

say "installed: $("$BIN_DIR/foreman" version)"
say ""
say "next steps:"
say "  foreman audit --deep      # see where your tokens actually go"
say "  foreman snapshot          # freeze the 'before'"
say "  foreman hook install      # enable automatic context trimming"
say "  foreman compare           # days later: prove the savings"
