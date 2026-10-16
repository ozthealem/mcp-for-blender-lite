"""Second half of install.bat / install.sh: put the addon into every Blender found and
register the server with Claude Desktop and Claude Code.

Every file it changes is backed up first (*.bak-mcp-lite). Nothing is sent
anywhere. Usage: python setup_clients.py <path to mcp-for-blender-lite.exe>
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SERVER_NAME = "blender"


def say(msg):
    print("        " + msg)


def backup(path: Path):
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak-mcp-lite"))


def install_addon(exe: str):
    """Use the package's own installer: it finds Blender's user addon folders.

    Portable Blenders keep their addons beside blender.exe and can't be found
    this way, so also say how to install from the zip by hand.
    """
    result = subprocess.run([exe, "install-addon"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    out = (result.stdout + result.stderr).strip().splitlines()
    for line in out[-3:]:
        say(line)
    if result.returncode != 0:
        say("Blender bulunamadi. Eklentiyi elle kurun / Blender not found, install the addon by hand:")
        say("Edit > Preferences > Add-ons > Install from Disk > mcp_for_blender_lite_addon_*.zip")


def claude_desktop_folder() -> Path | None:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) / "Claude" if appdata else None
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Claude"


def claude_desktop(exe: str):
    folder = claude_desktop_folder()
    if folder is None:
        return
    if not folder.exists():
        say("Claude Desktop yok, atlandi / Claude Desktop not installed, skipped.")
        return
    cfg = folder / "claude_desktop_config.json"
    data = {}
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            say(f"{cfg} okunamadi, dokunulmadi / could not be read, left alone.")
            return
    backup(cfg)
    data.setdefault("mcpServers", {})[SERVER_NAME] = {"command": exe}
    cfg.write_text(json.dumps(data, indent=2), encoding="utf-8")
    say(f"Claude Desktop: {cfg}")


def claude_code(exe: str):
    claude = shutil.which("claude")
    if not claude:
        return
    subprocess.run([claude, "mcp", "remove", "--scope", "user", SERVER_NAME], capture_output=True)
    result = subprocess.run([claude, "mcp", "add", "--scope", "user", SERVER_NAME, "--", exe],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    say("Claude Code: " + ("eklendi / added" if result.returncode == 0 else result.stderr.strip()[:200]))


def main():
    if len(sys.argv) != 2 or not Path(sys.argv[1]).exists():
        print("usage: setup_clients.py <mcp-for-blender-lite.exe>")
        return 1
    exe = str(Path(sys.argv[1]).resolve())
    install_addon(exe)
    if os.environ.get("MCPBL_SKIP_CLIENTS") != "1":
        claude_desktop(exe)
        claude_code(exe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
