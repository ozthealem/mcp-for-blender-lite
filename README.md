# MCP for Blender Lite

An offline fork of [MCP for Blender](https://github.com/ahujasid/blender-mcp) by
Siddharth Ahuja. It lets a local MCP client (Claude Desktop, Claude Code, LM Studio
and others) drive a running Blender through Python, and see the result.

**Fork notice.** This is a modified version of the original project, maintained by
[ozthealem](https://github.com/ozthealem). It is not affiliated with the original
author or with the Blender Foundation. The original MIT license and copyright are
kept in [LICENSE](LICENSE).

## What is different

Removed, so nothing leaves your machine:

- Telemetry, trajectory recording, the manual-edit recorder and the consent prompt
- Premium (license server) and all 3D generators: Hyper3D Rodin, Hunyuan3D, Tripo
- Asset sites: Poly Haven, Sketchfab, Poly Pizza
- The addon's self-updater (it downloaded code from GitHub) and the `update` command
- Auto-start: the addon listens only after you press **Connect**
- The `setup` command and web installers (they fetched the upstream package from PyPI)
- Terms and Conditions, Discord and Codex plugin files

Added:

- The addon listens on `127.0.0.1` only
- An optional access token, off by default (see below)

Kept: `execute_blender_code`, `look` (viewport, camera, angles, frames, images),
`get_scene_info`, `get_addon_status`, the viewport app for MCP Apps clients,
node and API lookups, scene export, and safe mode.

## One-click install (Windows)

1. Install [Python](https://www.python.org/downloads/) 3.10+ (tick "Add python.exe to PATH").
2. From [Releases](https://github.com/ozthealem/mcp-for-blender-lite/releases) download
   `mcp_for_blender_lite_windows_<version>.zip`, unzip it and double-click `install.bat`.

It installs the server into `%LOCALAPPDATA%\mcp-for-blender-lite`, copies the addon into
the Blender it finds, and registers the server with Claude Desktop and Claude Code (each
config file is backed up first). A portable Blender is not found automatically: install
the addon zip from the same folder with Edit > Preferences > Add-ons > Install from Disk.
Then enable **MCP for Blender Lite**, press N > **MCP Lite** > **Connect**, and restart Claude.

## Install script (Linux, macOS)

Download `mcp_for_blender_lite_linux_macos_<version>.tar.gz` from
[Releases](https://github.com/ozthealem/mcp-for-blender-lite/releases), then:

```bash
tar xzf mcp_for_blender_lite_linux_macos_*.tar.gz
sh mcp_for_blender_lite_linux_macos_*/install.sh
```

It needs Python 3.10+ with venv (Debian/Ubuntu: `sudo apt install python3 python3-venv`).
The server goes to `~/.local/share/mcp-for-blender-lite` (macOS: `~/Library/Application Support/`),
the addon into Blender's user addons folder, and the server is registered with Claude
Desktop and Claude Code. A Blender from a tarball or Flatpak may not be found: install the
addon zip by hand as above.

## Install from source

You need Python 3.10 or newer and Blender 4.2 or newer.

1. Clone this repository and install it into a virtual environment:

   ```bash
   git clone https://github.com/ozthealem/mcp-for-blender-lite.git
   cd mcp-for-blender-lite
   python -m venv .venv
   .venv/Scripts/python -m pip install -e .
   ```

   (On macOS and Linux the interpreter is `.venv/bin/python`.)

2. Install the Blender addon. In Blender: Edit > Preferences > Add-ons >
   Install from Disk, pick `addon.py` from this folder, and enable
   **Interface: MCP for Blender Lite**.

3. Point your MCP client at the server. For Claude Desktop, add to
   `claude_desktop_config.json` (use your own path):

   ```json
   {
     "mcpServers": {
       "blender": {
         "command": "D:/path/to/mcp-for-blender-lite/.venv/Scripts/mcp-for-blender-lite.exe"
       }
     }
   }
   ```

   For Claude Code:

   ```bash
   claude mcp add blender -- D:/path/to/mcp-for-blender-lite/.venv/Scripts/mcp-for-blender-lite.exe
   ```

4. In Blender press N, open the **MCP Lite** tab and press **Connect**.

## Access token (optional)

Anything on your machine that can reach `127.0.0.1` could otherwise send Python
to Blender. To require a token:

1. In the **MCP Lite** panel tick **Require Token** and press **New Token**.
   The token is written to a file in Blender's config folder; the panel shows
   the path.
2. Start the server with that file:

   ```json
   "args": ["--token-file", "C:/path/shown/in/the/panel/mcp_lite_token.txt"]
   ```

   or set `BLENDER_MCP_TOKEN_FILE` to the path.

The token is never put on a command line or in the client config, only its path.

## Settings

| Variable / flag | Default | Description |
|---|---|---|
| `--port`, `BLENDER_PORT` | `9876` | Port of the Blender addon |
| `--host`, `BLENDER_HOST` | `localhost` | Where the addon runs. The addon itself only listens on 127.0.0.1 |
| `--token-file`, `BLENDER_MCP_TOKEN_FILE` | none | Token file, when the addon requires one |
| `BLENDER_MCP_SAFE_MODE` | off | `1` checks every script first and blocks file, process and network access |
| `BLENDER_MCP_CONTEXT_LOG` | off | `1` writes tool names and sizes (never content) to a local log file |

Several Blender instances can run side by side: give each its own port in the
panel and its own client entry with `--port`.

## Development

```bash
.venv/Scripts/python -m pip install -e . pytest
.venv/Scripts/python -m pytest
```

`src/blender_mcp/bundled/addon.py` must stay identical to `addon.py`; the
`install-addon` command copies it into Blender's addons folder.

## License

MIT. Original work copyright (c) 2025 Siddharth Ahuja; see [LICENSE](LICENSE).
