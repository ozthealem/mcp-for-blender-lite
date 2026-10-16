#!/bin/sh
# MCP for Blender Lite installer for Linux and macOS.
# Everything it needs sits next to it: the server wheel, the Blender addon zip
# and setup_clients.py. Only pip talks to the internet, once, for "mcp".
# Run:  sh install.sh
set -e

HERE=$(cd "$(dirname "$0")" && pwd)
if [ -z "$MCPBL_HOME" ]; then
  case "$(uname -s)" in
    Darwin) MCPBL_HOME="$HOME/Library/Application Support/mcp-for-blender-lite" ;;
    *)      MCPBL_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/mcp-for-blender-lite" ;;
  esac
fi

echo
echo " MCP for Blender Lite"
echo " ===================="
echo

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo " Python 3.10+ not found. Install it first, e.g.:"
  echo "   Debian/Ubuntu: sudo apt install python3 python3-venv"
  echo "   Fedora:        sudo dnf install python3"
  echo "   macOS:         https://www.python.org/downloads/"
  exit 1
fi

echo " [1/3] Installing the server into: $MCPBL_HOME"
mkdir -p "$MCPBL_HOME"
if ! "$PY" -m venv "$MCPBL_HOME/venv" 2>/dev/null; then
  echo " Could not create a virtual environment. On Debian/Ubuntu: sudo apt install python3-venv"
  exit 1
fi
BIN="$MCPBL_HOME/venv/bin"
[ -d "$BIN" ] || BIN="$MCPBL_HOME/venv/Scripts"    # Git Bash on Windows, for testing
WHEEL=$(ls "$HERE"/mcp_for_blender_lite-*.whl 2>/dev/null | head -n 1)
if [ -z "$WHEEL" ]; then
  echo " The .whl file is missing next to install.sh"; exit 1
fi
"$BIN/python" -m pip install --quiet --disable-pip-version-check --upgrade "$WHEEL"

EXE="$BIN/mcp-for-blender-lite"
[ -e "$EXE.exe" ] && EXE="$EXE.exe"
echo " [2/3] Blender addon"
echo " [3/3] Claude connection"
"$BIN/python" "$HERE/setup_clients.py" "$EXE"

echo
echo " Done. In Blender press N > MCP Lite > Connect, then restart Claude."
echo
