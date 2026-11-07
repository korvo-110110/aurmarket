#!/usr/bin/env bash
# aurmarket installer
#
#   ./install.sh               install for the current user (~/.local)
#   sudo ./install.sh --system install for all users (/usr/local)
#   ./install.sh --uninstall   remove it again (add --system if you installed system-wide)
#
# Run it from inside the aurmarket project folder.

set -euo pipefail

APP="aurmarket"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="user"
ACTION="install"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}

for arg in "$@"; do
  case "$arg" in
    --system)    MODE="system" ;;
    --user)      MODE="user" ;;
    --uninstall) ACTION="uninstall" ;;
    -h|--help)   usage ;;
    *) die "Unknown option: $arg (try --help)" ;;
  esac
done

if [ "$MODE" = "system" ]; then
  PREFIX="/usr/local"
  LIB_DIR="$PREFIX/share/$APP"
  BIN_DIR="$PREFIX/bin"
  SUDO=""
  [ "$(id -u)" -ne 0 ] && SUDO="sudo"
else
  LIB_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/$APP"
  BIN_DIR="$HOME/.local/bin"
  SUDO=""
fi
LAUNCHER="$BIN_DIR/$APP"

# ---------------------------------------------------------------- uninstall
if [ "$ACTION" = "uninstall" ]; then
  say "Removing $APP ($MODE install)"
  $SUDO rm -f "$LAUNCHER"
  $SUDO rm -rf "$LIB_DIR"
  say "Done. Your config file (if any) was left untouched."
  exit 0
fi

# ------------------------------------------------------------- find the app
ENTRY_FILE=""
ENTRY_MODULE=""
for candidate in aurmarket.py main.py __main__.py; do
  if [ -f "$SRC_DIR/$candidate" ]; then ENTRY_FILE="$candidate"; break; fi
done
if [ -z "$ENTRY_FILE" ] && [ -f "$SRC_DIR/$APP/__main__.py" ]; then
  ENTRY_MODULE="$APP"
fi
[ -n "$ENTRY_FILE$ENTRY_MODULE" ] || die "Couldn't find the program's entry point (aurmarket.py, main.py, or aurmarket/__main__.py) in $SRC_DIR"

# ------------------------------------------------------------- dependencies
missing=()
command -v python3 >/dev/null 2>&1 || missing+=("python")
python3 -c "import yaml" >/dev/null 2>&1 || missing+=("python-yaml")
command -v git >/dev/null 2>&1 || missing+=("git")

if [ "${#missing[@]}" -gt 0 ]; then
  warn "Missing dependencies: ${missing[*]}"
  if command -v pacman >/dev/null 2>&1; then
    read -r -p "Install them with pacman now? [Y/n] " reply
    case "${reply:-Y}" in
      [Yy]*) sudo pacman -S --needed "${missing[@]}" ;;
      *)     die "Please install: ${missing[*]}" ;;
    esac
  else
    die "Please install: ${missing[*]}"
  fi
fi

# ------------------------------------------------------------------ install
say "Installing $APP to $LIB_DIR"
$SUDO rm -rf "$LIB_DIR"
$SUDO mkdir -p "$LIB_DIR" "$BIN_DIR"
tar -C "$SRC_DIR" \
    --exclude=.git --exclude=__pycache__ --exclude='*.pyc' \
    --exclude=install.sh --exclude=.venv \
    -cf - . | $SUDO tar -C "$LIB_DIR" -xf -

say "Creating launcher $LAUNCHER"
tmp="$(mktemp)"
if [ -n "$ENTRY_FILE" ]; then
  cat > "$tmp" <<EOF
#!/usr/bin/env bash
exec python3 "$LIB_DIR/$ENTRY_FILE" "\$@"
EOF
else
  cat > "$tmp" <<EOF
#!/usr/bin/env bash
PYTHONPATH="$LIB_DIR\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m $ENTRY_MODULE "\$@"
EOF
fi
$SUDO install -m 755 "$tmp" "$LAUNCHER"
rm -f "$tmp"

# --------------------------------------------------------------------- done
say "Installed. Run:  $APP"
if [ "$MODE" = "user" ]; then
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not in your PATH. Add this to your shell config:"
       echo "     export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
  esac
fi
say "Create the config with:  $APP config"
