# blender_mcp_server.py
from mcp.server.fastmcp import FastMCP, Context
import argparse
import socket
import json
import logging
import tempfile
import threading
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any
import os
import sys
import base64

from .addon_manager import (
    handshake_addon,
    format_handshake_log,
    run_cli as run_addon_cli,
    EXPECTED_ADDON_PROTOCOL_VERSION,
    check_addon_status_on_startup,
)
from . import blender_scripts, context_log
from .safe_mode import safe_mode_enabled, validate_code, SandboxViolation, SAFE_MODE_ENV
from .openai_apps import (
    APP_MIME_TYPE,
    VIEWPORT_STATE_META,
    VIEWPORT_TITLE,
    VIEWPORT_URI,
    client_extensions,
    is_app_only,
    supports_apps,
    viewport_html,
    viewport_icon,
    viewport_store,
)
from mcp.types import CallToolResult, ImageContent, ResourceLink, TextContent, ToolAnnotations
from urllib.parse import quote, unquote

# Configure logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("BlenderMCPServer")

# Default configuration
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9876


def parse_connection_args(argv):
    """Parse --host/--port out of argv, ignoring anything else.

    parse_known_args is deliberate: MCP clients sometimes append their own
    arguments to the server command, and an unrecognised one must not abort
    startup. Unknown args are logged rather than dropped silently, so a typo
    like --prot does not masquerade as "connected to the default port".
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--token-file", default=None)
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        logger.warning(f"Ignoring unrecognized command-line arguments: {unknown}")
    return args.host, args.port


TOKEN_FILE_ENV = "BLENDER_MCP_TOKEN_FILE"


def resolve_token(argv) -> str | None:
    """The shared secret the addon asks for, if the user switched one on.

    Read from --token-file, else the BLENDER_MCP_TOKEN_FILE environment
    variable. The token lives in a file rather than on the command line so it
    never shows up in process listings or MCP client configs. No file: no token.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--token-file", default=None)
    args, _ = parser.parse_known_args(argv)
    path = args.token_file or os.getenv(TOKEN_FILE_ENV)
    if not path:
        return None
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError as e:
        logger.warning(f"Could not read token file {path!r}: {e}")
        return None


def resolve_connection(cli_host=None, cli_port=None):
    """Resolve the Blender address: CLI flags > environment > defaults."""
    host = cli_host or os.getenv("BLENDER_HOST", DEFAULT_HOST)

    if cli_port is not None:
        return host, cli_port

    raw_port = os.getenv("BLENDER_PORT")
    if raw_port is None or raw_port == "":
        return host, DEFAULT_PORT
    try:
        return host, int(raw_port)
    except ValueError:
        logger.warning(
            f"BLENDER_PORT={raw_port!r} is not a valid port number; "
            f"falling back to {DEFAULT_PORT}"
        )
        return host, DEFAULT_PORT


# Set from --host/--port in main(); these take precedence over the
# BLENDER_HOST/BLENDER_PORT environment variables.
CLI_HOST = None
CLI_PORT = None
TOKEN = None

_addon_handshake = None
_addon_handshake_checked = False
_addon_handshake_lock = threading.Lock()

@dataclass
class BlenderConnection:
    host: str
    port: int
    sock: socket.socket = None  # Changed from 'socket' to 'sock' to avoid naming conflict
    # Serializes send+receive so two commands can never interleave on one socket.
    # Without this, a second command's response can be read as the first's, and
    # the stream stays desynced until the 180s timeout fires.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def connect(self) -> bool:
        """Connect to the Blender addon socket server"""
        if self.sock:
            return True
            
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            logger.info(f"Connected to Blender at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Blender: {str(e)}")
            self.sock = None
            return False
    
    def disconnect(self):
        """Disconnect from the Blender addon"""
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error(f"Error disconnecting from Blender: {str(e)}")
            finally:
                self.sock = None

    def receive_full_response(self, sock, buffer_size=8192):
        """Receive the complete response, potentially in multiple chunks"""
        chunks = []
        # Use a consistent timeout value that matches the addon's timeout
        sock.settimeout(180.0)  # Match the addon's timeout
        
        try:
            while True:
                try:
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        # If we get an empty chunk, the connection might be closed
                        if not chunks:  # If we haven't received anything yet, this is an error
                            raise Exception("Connection closed before receiving any data")
                        break
                    
                    chunks.append(chunk)
                    
                    # Check if we've received a complete JSON object
                    try:
                        data = b''.join(chunks)
                        json.loads(data.decode('utf-8'))
                        # If we get here, it parsed successfully
                        logger.info(f"Received complete response ({len(data)} bytes)")
                        return data
                    except json.JSONDecodeError:
                        # Incomplete JSON, continue receiving
                        continue
                except socket.timeout:
                    # If we hit a timeout during receiving, break the loop and try to use what we have
                    logger.warning("Socket timeout during chunked receive")
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Socket connection error during receive: {str(e)}")
                    raise  # Re-raise to be handled by the caller
        except socket.timeout:
            logger.warning("Socket timeout during chunked receive")
        except Exception as e:
            logger.error(f"Error during receive: {str(e)}")
            raise
            
        # If we get here, we either timed out or broke out of the loop
        # Try to use what we have
        if chunks:
            data = b''.join(chunks)
            logger.info(f"Returning data after receive completion ({len(data)} bytes)")
            try:
                # Try to parse what we have
                json.loads(data.decode('utf-8'))
                return data
            except json.JSONDecodeError:
                # If we can't parse it, it's incomplete
                raise Exception("Incomplete JSON response received")
        else:
            raise Exception("No data received")

    def send_command(self, command_type: str, params: Dict[str, Any] = None, read_only: bool = False) -> Dict[str, Any]:
        """Send a command to Blender and return the response.

        `read_only` marks an execute_code the server runs only to observe the
        scene, so the Viewport app doesn't treat it as an edit and recapture.
        """
        # Hold the lock across send+receive: the response is matched to the
        # command purely by ordering on the stream, so overlapping calls would
        # hand each other's responses back.
        with self._lock:
            # The Viewport app watches these to recapture once Blender goes quiet.
            viewport_store.command_started()
            try:
                return self._send_command_locked(command_type, params)
            finally:
                viewport_store.command_finished("observe" if read_only else command_type)

    def _send_command_locked(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Blender")

        command = {
            "type": command_type,
            "params": params or {}
        }
        if TOKEN:
            command["token"] = TOKEN

        try:
            # Log the command being sent
            logger.info(f"Sending command: {command_type} with params: {params}")
            
            # Send the command
            self.sock.sendall(json.dumps(command).encode('utf-8'))
            logger.info(f"Command sent, waiting for response...")
            
            # Set a timeout for receiving - use the same timeout as in receive_full_response
            self.sock.settimeout(180.0)  # Match the addon's timeout
            
            # Receive the response using the improved receive_full_response method
            response_data = self.receive_full_response(self.sock)
            logger.info(f"Received {len(response_data)} bytes of data")
            
            response = json.loads(response_data.decode('utf-8'))
            logger.info(f"Response parsed, status: {response.get('status', 'unknown')}")
            
            if response.get("status") == "error":
                logger.error(f"Blender error: {response.get('message')}")
                raise Exception(response.get("message", "Unknown error from Blender"))
            
            return response.get("result", {})
        except socket.timeout:
            logger.error("Socket timeout while waiting for response from Blender")
            # Don't try to reconnect here - let the get_blender_connection handle reconnection
            # Just invalidate the current socket so it will be recreated next time
            self.sock = None
            raise Exception("Timeout waiting for Blender response - try simplifying your request. If Blender is running headless (blender -b), commands never execute; run Blender with a GUI or via 'xvfb-run -a blender' instead")
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
            logger.error(f"Socket connection error: {str(e)}")
            self.sock = None
            raise Exception(f"Connection to Blender lost: {str(e)}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from Blender: {str(e)}")
            # Try to log what was received
            if 'response_data' in locals() and response_data:
                logger.error(f"Raw response (first 200 bytes): {response_data[:200]}")
            raise Exception(f"Invalid response from Blender: {str(e)}")
        except Exception as e:
            logger.error(f"Error communicating with Blender: {str(e)}")
            # Don't try to reconnect here - let the get_blender_connection handle reconnection
            self.sock = None
            raise Exception(f"Communication error with Blender: {str(e)}")

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    # We don't need to create a connection here since we're using the global connection
    # for resources and tools

    try:
        # Just log that we're starting up
        logger.info("BlenderMCP server starting up")

        try:
            status = check_addon_status_on_startup()
            if status.needs_action:
                logger.warning(status.message)
            elif status.message:
                logger.info(status.message)
        except Exception as e:
            logger.debug(f"Addon status check skipped: {e}")

        # Try to connect to Blender on startup to verify it's available
        try:
            # This will initialize the global connection if needed
            blender = get_blender_connection()
            logger.info("Successfully connected to Blender on startup")
            if _addon_handshake and not _addon_handshake.up_to_date:
                logger.warning(format_handshake_log(_addon_handshake))
        except Exception as e:
            logger.warning(f"Could not connect to Blender on startup: {str(e)}")
            logger.warning("Make sure the Blender addon is running before using Blender resources or tools")

        # Return an empty context - we're using the global connection
        yield {}
    finally:
        # Clean up the global connection on shutdown
        global _blender_connection
        if _blender_connection:
            logger.info("Disconnecting from Blender on shutdown")
            _blender_connection.disconnect()
            _blender_connection = None
        logger.info("BlenderMCP server shut down")

# Guidance delivered to clients in the `initialize` response. This is the only
# guidance every client is sure to get: MCP prompts are user-invoked, and the
# model has no way to fetch one. Per-tool details belong in tool descriptions.
# Kept short because instructions are injected into every conversation (see
# #347 on context cost).
SERVER_INSTRUCTIONS = """MCP for Blender drives the user's live Blender. execute_blender_code runs Python there
with the full bpy API, so anything Blender can do, you can do; look shows you the result.

Start with get_addon_status (Blender version, addon match) and get_scene_info.

Scripts run in someone else's Blender:
- Look shader nodes up by type, never by name (names are localized):
  `next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")`.
- Never hardcode enum identifiers; read them, e.g.
  `[i.identifier for i in bpy.types.RenderSettings.bl_rna.properties["file_format"].enum_items]`.
  scene.render.engine under-reports: read the current value, and assign a new one inside
  try/except TypeError, whose message lists the valid engines.
- Material colors go on shader node inputs; material.diffuse_color only affects the viewport.

look is how you see your work; use it as much as you need. Images stay in the conversation, so
a smaller max_size keeps long sessions cheap.

This build is offline: there are no asset libraries or 3D generators. Build with bpy, or
import files the user already has on disk."""

# Create the MCP server with lifespan support
mcp = FastMCP(
    "MCP for Blender",
    lifespan=server_lifespan,
    instructions=SERVER_INSTRUCTIONS,
)

# Resource endpoints

# Global connection for resources (since resources can't access context)
_blender_connection = None

def _maybe_handshake_addon(blender: BlenderConnection) -> None:
    """Run addon version handshake once per process after a live connection."""
    global _addon_handshake, _addon_handshake_checked
    with _addon_handshake_lock:
        if _addon_handshake_checked:
            return
        _addon_handshake_checked = True
    try:
        _addon_handshake = handshake_addon(blender)
        log_line = format_handshake_log(_addon_handshake)
        if _addon_handshake.up_to_date:
            logger.info(log_line)
        else:
            logger.warning(log_line)
    except Exception as e:
        logger.debug(f"Addon handshake skipped: {e}")


def _addon_protocol() -> int | None:
    """Protocol the connected addon reported at handshake, or None if unknown."""
    return _addon_handshake.protocol_version if _addon_handshake else None


def get_blender_connection():
    """Get or create a persistent Blender connection"""
    global _blender_connection

    # Reuse the existing connection. We deliberately do NOT probe it with a
    # command here: that put two commands on the wire for every tool call, and
    # any overlap desynced the response stream until the socket timeout fired.
    # A dead socket is detected by the next real command and reconnected then.
    if _blender_connection is not None and _blender_connection.sock is not None:
        return _blender_connection

    # Create a new connection if needed
    if _blender_connection is None:
        host, port = resolve_connection(CLI_HOST, CLI_PORT)
        _blender_connection = BlenderConnection(host=host, port=port)
        if not _blender_connection.connect():
            logger.error("Failed to connect to Blender")
            _blender_connection = None
            raise Exception("Could not connect to Blender. Make sure the Blender addon is running.")
        logger.info("Created new persistent connection to Blender")
        _maybe_handshake_addon(_blender_connection)

    return _blender_connection


@mcp.tool()
async def get_addon_status(ctx: Context, user_prompt: str = "") -> str:
    """
    Check the connected Blender: its version and whether the addon matches this server.
    Call it once at the start.

    If outdated, tells the user how to update via `mcp-for-blender-lite install-addon`
    (then restart or re-enable the addon in Blender).
    """
    try:
        blender = get_blender_connection()
        global _addon_handshake, _addon_handshake_checked
        with _addon_handshake_lock:
            _addon_handshake_checked = False
        _maybe_handshake_addon(blender)
        result = _addon_handshake
        if result is None:
            return "Could not determine addon status."
        payload = {
            "up_to_date": result.up_to_date,
            "protocol_version": result.protocol_version,
            "expected_protocol_version": EXPECTED_ADDON_PROTOCOL_VERSION,
            "addon_version": result.addon_version,
            "capabilities": result.capabilities,
            "blender_version": result.blender_version,
            "source": result.source,
            "warning": result.warning,
            "update_command": "mcp-for-blender-lite install-addon",
            "after_install": (
                "If the addon file was updated: in Blender, Preferences → Add-ons → "
                "disable/enable 'Interface: Blender MCP', or restart Blender, then Start MCP Server."
            ),
        }
        return json.dumps(payload, indent=2)
    except Exception as e:
        return f"Error checking addon status: {e}"


# Backwards compatibility. The server updates itself through uvx, but the addon
# only changes when the user reinstalls it, so any server must work with any
# addon. Two rules keep that true:
# - Never assume a command or argument exists. A command an addon doesn't know
#   comes back as "Unknown command type", and an argument newer than the addon
#   as "unexpected keyword argument"; missing_feature turns either into one message,
#   which asks for an addon update when the handshake says the addon is behind
#   and for a sidebar checkbox when it isn't.
# - Every observation has a fallback to something older addons have
#   (look -> the native screenshot, get_scene_info -> the addon's own summary).
# tests/test_compat_matrix.py runs the tools against real past addons.

ADDON_UPDATE_HINT = "Update the Blender addon: run `mcp-for-blender-lite install-addon`, then restart Blender."


class AddonTooOld(Exception):
    """The connected addon can't do this; the message says what to update."""


def _addon_outdated() -> bool:
    return _addon_handshake is None or not _addon_handshake.up_to_date


_ADDON_LACKS = ("Unknown command type", "unexpected keyword argument")


def _addon_lacks(e: Exception | str) -> bool:
    """Whether a failure means the addon predates the command or an argument."""
    return any(marker in str(e) for marker in _ADDON_LACKS)


def missing_feature(what: str, sidebar_label: str | None = None) -> str:
    """What to tell the user when the addon doesn't handle a command.

    Integration commands are only registered while their sidebar checkbox is
    ticked, so on an up-to-date addon a missing one means switched off.
    """
    if sidebar_label and not _addon_outdated():
        return (f"{sidebar_label} is switched off. Ask the user to tick it in the MCP for Blender sidebar "
                "in Blender (press N in the 3D Viewport).")
    reply = f"The Blender addon is too old for {what}. {ADDON_UPDATE_HINT}"
    if sidebar_label:
        reply += f" If it is already up to date, tick {sidebar_label} in the MCP for Blender sidebar."
    return reply


def _run_script(script: str, args: dict) -> dict:
    """Run one of blender_scripts' observation scripts and return its result."""
    result = get_blender_connection().send_command(
        "execute_code", {"code": blender_scripts.build(script, args)}, read_only=True)
    # Addons before April 2025 run code but don't return what it prints.
    if not isinstance(result, dict) or "result" not in result:
        raise AddonTooOld(missing_feature("this view"))
    return blender_scripts.parse_result(result["result"])


def _format_scene_summary(data: dict, fields) -> str:
    h = data["header"]
    counts = ", ".join(f"{n} {kind}" for kind, n in sorted(h["object_counts"].items())) or "empty"
    selected = ", ".join(h["selected"]) or "none"
    extra = h.get("selected_count", len(h["selected"])) - len(h["selected"])
    if extra > 0:
        selected += f" +{extra} more"
    lines = [f"Scene '{h['scene']}' | {counts} | active {h['active'] or 'none'} | selected {selected} | mode {h['mode']}"]
    st = h.get("settings")
    if st:
        lines.append(
            f"{st['file']} | engine {st['engine']} | frames {st['frames'][0]}-{st['frames'][1]} "
            f"(now {st['frames'][2]}) at {st['fps']} fps | {st['resolution'][0]}x{st['resolution'][1]} | "
            f"camera {st['camera'] or 'none'} | HDRI {st['world_hdri'] or 'none'} | unit scale {st['unit_scale']}"
        )
    columns = " | ".join(["name", "type", *(f for f in blender_scripts.SCENE_FIELDS if f in fields and f != "settings")])
    lines += ["", f"Showing {data['shown']} of {data['total']} ({columns}):", *data["lines"]]
    if data["shown"] < data["total"]:
        lines.append(f"... {data['total'] - data['shown']} more. Narrow with query= or root=, or raise limit.")
    return "\n".join(lines)


@mcp.tool()
async def get_scene_info(
    ctx: Context,
    user_prompt: str = "",
    query: str | None = None,
    root: str | None = None,
    fields: list[str] | None = None,
    limit: int = 20,
) -> str:
    """
    Facts about the scene as text: what's there, where, how big, and how healthy meshes and rigs
    are. No image; to see the scene, use look.

    One header line (object counts, active, selection, mode), then one line per object with the
    fields you ask for. Top-level objects by default; root="Name" lists that object's hierarchy,
    query="chair" lists every object whose name contains the text. For anything else about an
    object, read it with execute_blender_code.

    Parameters:
    - fields: What to show per object (default: location, size, children, hidden).
      placement: location (world), size (world bounding box), ground (on ground, floating or
        below by), rotation (degrees), scale, parent
      contents: children (count), hidden, details (faces, bones or light power), materials,
        modifiers, animation
      health: topology (quads, tris, ngons, non-manifold and boundary edges, loose verts,
        poles), weights (vertices no deform bone moves, deform bones with no vertex group)
      settings: adds a line with the file, engine, frame range, resolution, camera, HDRI and
        unit scale
    - query: Name filter across all objects.
    - root: Object whose hierarchy to list.
    - limit: Maximum object lines (default 20).
    - user_prompt: The user's own words describing what they want, quoted verbatim.
    """
    fields = list(blender_scripts.SCENE_DEFAULT_FIELDS) if fields is None else list(dict.fromkeys(fields))
    unknown = [f for f in fields if f not in blender_scripts.SCENE_FIELDS]
    if unknown:
        return f"Error: unknown fields {', '.join(unknown)}. Pick from: {', '.join(blender_scripts.SCENE_FIELDS)}"
    try:
        try:
            data = _run_script(blender_scripts.SCENE_SUMMARY,
                               {"query": query, "root": root, "limit": limit, "fields": fields})
        except Exception as e:
            # Very old addons, or a Blender that can't run the script: the
            # addon's own summary still says what's there.
            logger.debug(f"Scene summary script failed, using get_scene_info: {e}")
            result = get_blender_connection().send_command("get_scene_info")
            return json.dumps(result, indent=2)
        if data.get("error"):
            return f"Error: {data['error']}"
        return _format_scene_summary(data, fields)
    except Exception as e:
        logger.error(f"Error getting scene info from Blender: {str(e)}")
        return f"Error getting scene info: {str(e)}"


def _capture_viewport(max_size: int) -> tuple[bytes, dict]:
    """Have the addon render the viewport to a temp file.

    Returns the PNG bytes and what newer addons report about it: the camera it
    was rendered with (`view`, for clicking on objects in the image) and the
    file and scene it shows.
    """
    blender = get_blender_connection()
    temp_path = os.path.join(tempfile.gettempdir(), f"blender_screenshot_{os.getpid()}.png")

    result = blender.send_command("get_viewport_screenshot", {
        "max_size": max_size,
        "filepath": temp_path,
        "format": "png"
    })

    if "error" in result:
        raise Exception(result["error"])

    if not os.path.exists(temp_path):
        raise Exception("Screenshot file was not created")

    with open(temp_path, 'rb') as f:
        image_bytes = f.read()
    os.remove(temp_path)
    return image_bytes, result


def _store_capture(max_size: int, source: str) -> None:
    # Read the version first: an edit that lands mid-capture isn't in the image.
    scene_version = viewport_store.scene_version
    png, info = _capture_viewport(max_size)
    origin = {key: info[key] for key in ("file", "scene", "scene_count") if key in info}
    viewport_store.put(png, source, view=info.get("view"), scene_version=scene_version, origin=origin)


# In MCP Apps hosts the result also shows in the fullscreen Viewport app.
def _viewport_screenshot(ctx: Context, max_size: int = 1000, user_prompt: str = "") -> CallToolResult:
    """look(mode="viewport"): the user's viewport, also shown in the Viewport app."""
    try:
        _store_capture(max_size, "model")
        state, image_bytes = _viewport_snapshot()
        # The state rides in _meta, which only the Viewport app reads, so the
        # model sees exactly the image it always did.
        return CallToolResult(
            content=[_png_content(image_bytes)],
            _meta={VIEWPORT_STATE_META: state},
        )
    except Exception as e:
        logger.error(f"Error capturing screenshot: {str(e)}")
        raise Exception(f"Screenshot failed: {str(e)}")


@mcp.tool()
async def execute_blender_code(ctx: Context, code: str, user_prompt: str = "") -> str:
    """
    Run Python in the user's live Blender (bpy, bmesh, mathutils). Whatever it prints is returned.

    Work in small steps and print what you need to know.

    Parameters:
    - code: The Python code to execute
    - user_prompt: The user's own words describing what they want (kept for compatibility; never stored).
    """
    if safe_mode_enabled():
        try:
            validate_code(code)
        except SandboxViolation as exc:
            logger.warning(f"Safe mode rejected script: {exc}")
            return (
                f"Rejected by safe mode - {exc}\n\n"
                f"{SAFE_MODE_ENV} is enabled: scripts may only import bpy, bmesh, "
                "mathutils, and pure-python stdlib modules. No eval/exec/open, no "
                "os/subprocess/network access, no handlers/timers/drivers, no class "
                "or property registration, and no loading of external .blend "
                "datablocks. Blender operators for rendering, saving, and "
                "import/export ARE allowed. Rewrite the script within these limits; "
                "only the user can disable safe mode."
            )
    try:
        # Get the global connection
        blender = get_blender_connection()
        result = blender.send_command("execute_code", {"code": code})
        return f"Code executed successfully: {result.get('result', '')}"
    except Exception as e:
        logger.error(f"Error executing code: {str(e)}")
        # The addon reports failures as a JSON payload so the traceback survives
        # the socket hop; render it as text rather than echoing the raw blob.
        try:
            detail = json.loads(str(e))
            traceback_text = detail["traceback"]
        except (ValueError, KeyError, TypeError):
            return f"Error executing code: {str(e)}"
        return f"Error executing code: {detail.get('exception_type', 'Error')}: {detail.get('message', '')}\n\n{traceback_text}"


# The model-facing tool surface. Each tool covers a job the model can't do
# with execute_blender_code alone: seeing the scene (look) and reading it
# (get_scene_info).

LOOK_MODES = ("viewport", "camera", "angles", "frames")
LOOK_ANGLES = ("front", "back", "left", "right", "top", "three_quarter")
LOOK_SHADING = ("solid", "material", "rendered", "wireframe", "xray")
# Modes before the shading/stats split, so a model trained on them gets pointed the right way.
LOOK_RETIRED = {
    "topology": 'Use shading="wireframe" to see edges, and get_scene_info(fields=["topology"]) for counts.',
    "rig": 'Use shading="xray" to see bones, and get_scene_info(fields=["weights"]) for weighting.',
    "image": 'Pass image= on its own, e.g. look(image="Render Result").',
}


def _look_caption(info: dict) -> str:
    mode = info["mode"]
    if mode == "image":
        w0, h0 = info["original_size"]
        return f"Image '{info['image']}', {w0}x{h0}, shown at {info['width']}x{info['height']}."
    parts = []
    if mode == "angles":
        parts.append("Tiles left to right, top to bottom: " + ", ".join(info.get("views", [])) + ".")
    if mode == "frames":
        parts.append("Frames left to right, top to bottom: " + ", ".join(map(str, info.get("frames", []))) + ".")
    if mode == "camera":
        parts.append(f"Through camera '{info.get('camera')}'.")
    if mode != "viewport":
        size = " x ".join(f"{v:g}" for v in info.get("size", []))
        parts.append(f"Framed {info.get('targets', 0)} objects, {size} m across, centred at {info.get('center')}.")
    return " ".join(parts)


@mcp.tool(meta={"ui": {"resourceUri": VIEWPORT_URI}})
async def look(
    ctx: Context,
    mode: str | None = None,
    target: list[str] | None = None,
    views: list[str | list[float]] | None = None,
    distance: float | None = None,
    shading: str | None = None,
    frames: list[int] | None = None,
    frame_count: int = 6,
    view: str | list[float] | None = None,
    image: str | None = None,
    max_size: int = 768,
    user_prompt: str = "",
) -> CallToolResult:
    """
    See the scene as one image. For counts, sizes and positions, use get_scene_info.

    Choose where from (mode) and how it's drawn (shading):
    - mode: viewport (what the user sees; default), camera (through the scene camera, at the
      render aspect), angles (the target from several sides, auto-framed; default front, right,
      top, three_quarter), frames (a strip over the animation).
    - shading: solid, material, rendered (EEVEE and Workbench only), wireframe (edges over a
      plain surface), xray (see-through, bones in front). Default: the viewport's.
    - image: Instead of the scene, show this image: "Render Result" after a render, another
      image in the file, or a file path.

    Parameters:
    - target: Object names to frame (children included). Default: every visible object.
    - views: For angles: up to 6 of front, back, left, right, top, three_quarter, or [x, y, z]
      directions from the target towards the eye ([0, -1, 0.2] is front, slightly above).
    - distance: Metres from the target's centre to the eye, for angles and frames views. Default:
      far enough to fit it; closer for detail or to stand inside a room.
    - frames / frame_count: For frames: explicit frame numbers, or how many to sample (2-12).
    - view: For frames: "camera", an angle name or an [x, y, z] direction; default the viewport.
    - max_size: Longest side in pixels (default 768). Images stay in the conversation, so go
      smaller for quick checks and larger only to read fine detail.
    - user_prompt: The user's own words describing what they want, quoted verbatim.

    Every setting changed to take the picture is restored afterwards.
    """
    if mode in LOOK_RETIRED:
        return _app_error(f"There is no {mode} mode. {LOOK_RETIRED[mode]}")
    if mode is not None and mode not in LOOK_MODES:
        return _app_error(f"Unknown mode {mode!r}. Use one of: {', '.join(LOOK_MODES)}")
    if image is not None:
        if mode is not None:
            return _app_error("image= shows an image instead of the scene; leave mode unset.")
        mode = "image"
    mode = mode or "viewport"
    if shading is not None and shading not in LOOK_SHADING:
        return _app_error(f"Unknown shading {shading!r}. Use one of: {', '.join(LOOK_SHADING)}")
    for v in (views or []) + ([view] if view is not None and view != "camera" else []):
        if isinstance(v, str) and v not in LOOK_ANGLES:
            return _app_error(f"Unknown view {v!r}. Use one of {', '.join(LOOK_ANGLES)} or an [x, y, z] direction.")
        if not isinstance(v, str) and (len(v) != 3 or not any(v)):
            return _app_error(f"A view direction is three numbers, not all zero; got {v!r}.")
    args = {"mode": mode, "target": target, "views": views, "distance": distance, "shading": shading,
            "frames": frames, "frame_count": frame_count, "view": view, "image": image,
            "max_size": max(200, min(int(max_size or 768), 2000))}

    def native():
        return _viewport_screenshot(ctx, max_size=max_size, user_prompt=user_prompt)

    def scripted():
        return _look_via_script(args)

    # The plain viewport has a native command; everything else is a script.
    # Each falls back to the other, since old addons may have only one of them.
    first, second = (native, scripted) if mode == "viewport" and not shading else (scripted, native)
    try:
        return first()
    except _LookRefused as e:
        return _app_error(str(e))
    except Exception as e:
        reason = str(e)
    if mode == "image":
        # The viewport is no stand-in for the image that was asked for.
        hint = f" {ADDON_UPDATE_HINT}" if _addon_outdated() and ADDON_UPDATE_HINT not in reason else ""
        return _app_error(f"Couldn't show the image: {reason}{hint}")
    try:
        result = second()
    except Exception as e:
        hint = f" {ADDON_UPDATE_HINT}" if _addon_outdated() and ADDON_UPDATE_HINT not in reason else ""
        return _app_error(f"Couldn't capture the view: {reason}{hint}")
    if second is native:
        note = f"look(mode=\"{mode}\") isn't available here ({reason}), so this is the plain viewport."
        result.content.append(TextContent(type="text", text=note))
    return result


class _LookRefused(Exception):
    """A mistake in the request (a missing object, no camera): report it, don't fall back."""


def _look_via_script(args: dict) -> CallToolResult:
    path = os.path.join(tempfile.gettempdir(), f"blender_look_{os.getpid()}.png")
    info = _run_script(blender_scripts.LOOK, {**args, "filepath": path})
    if info.get("error"):
        raise _LookRefused(info["error"])
    with open(path, "rb") as f:
        png = f.read()
    os.remove(path)
    return CallToolResult(content=[_png_content(png), TextContent(type="text", text=_look_caption(info))])


# Guides (guides/, guides.py) are switched off while evals measure what the model
# does without them. To bring them back, register get_guide and the guide://
# resources here again.


# MCP Apps and OpenAI extensions. Tools marked visibility ["app"] are called by
# the host UI, never by the model, and are hidden from clients without MCP Apps.

_APP_ONLY = {"ui": {"visibility": ["app"]}}
_READ_ONLY = ToolAnnotations(readOnlyHint=True)
_SCENE_ITEM_KINDS = ("object", "material", "collection")


def _scene_items(query: str, limit: int = 30) -> list[dict]:
    blender = get_blender_connection()
    if (_addon_protocol() or 0) >= 12:
        result = blender.send_command("list_scene_items", {"query": query, "limit": limit})
        return result.get("items", []) if isinstance(result, dict) else []
    # Older addons only list the first ten objects, and no materials.
    result = blender.send_command("get_scene_info")
    needle = query.strip().lower()
    return [
        {"kind": "object", "name": o["name"], "detail": f"{o.get('type', '').title()} object"}
        for o in (result.get("objects") or [])
        if needle in o["name"].lower()
    ]


def _scene_item_uri(kind: str, name: str) -> str:
    return f"blender://{kind}/{quote(name, safe='')}"


@mcp.tool(
    title="Mention Blender items",
    annotations=_READ_ONLY,
    meta={"openai/extensions": {"mentions/search": {}}, **_APP_ONLY},
)
async def search_mentions(query: str = "") -> CallToolResult:
    """Search scene objects, materials and collections to @-mention in the composer."""
    try:
        items = _scene_items(query)
    except Exception as e:
        logger.debug(f"Mention search failed: {e}")
        items = []
    links = [
        ResourceLink(
            type="resource_link",
            uri=_scene_item_uri(item["kind"], item["name"]),
            name=item["name"],
            title=item["name"],
            description=item.get("detail"),
            mimeType="application/json",
        ).model_dump(by_alias=True, exclude_none=True, mode="json")
        for item in items
        if item.get("kind") in _SCENE_ITEM_KINDS
    ]
    return CallToolResult(content=[], structuredContent={"items": links})


@mcp.resource("blender://object/{name}", mime_type="application/json")
def object_resource(name: str) -> str:
    """A Blender object's transform, materials and mesh stats."""
    return json.dumps(get_blender_connection().send_command("get_object_info", {"name": unquote(name)}))


def _scene_item_resource(kind: str, name: str) -> str:
    name = unquote(name)
    for item in _scene_items(name, limit=100):
        if item.get("kind") == kind and item.get("name") == name:
            return json.dumps(item)
    raise ValueError(f"No {kind} named {name!r} in the open Blender file")


@mcp.resource("blender://material/{name}", mime_type="application/json")
def material_resource(name: str) -> str:
    """A Blender material and the objects that use it."""
    return _scene_item_resource("material", name)


@mcp.resource("blender://collection/{name}", mime_type="application/json")
def collection_resource(name: str) -> str:
    """A Blender collection and how many objects it holds."""
    return _scene_item_resource("collection", name)


@mcp.resource(
    VIEWPORT_URI,
    name="viewport",
    title=VIEWPORT_TITLE,
    mime_type=APP_MIME_TYPE,
    meta={
        "ui": {"prefersBorder": False},
        # Fullscreen only: every screenshot updates the one live view rather
        # than leaving a card in the thread.
        "openai/ui": {"preferredDisplayMode": "fullscreen", "availableDisplayModes": ["fullscreen"]},
    },
)
def viewport_app() -> str:
    return viewport_html()


def _png_content(png: bytes) -> ImageContent:
    return ImageContent(type="image", data=base64.b64encode(png).decode("ascii"), mimeType="image/png")


def _viewport_snapshot() -> tuple[dict, bytes | None]:
    state, png = viewport_store.snapshot()
    # An addon older than this server can't pick objects, so the app says to
    # update it instead of quietly attaching only the image.
    state["addon_outdated"] = _addon_handshake is not None and not _addon_handshake.up_to_date
    return state, png


def _viewport_result(since: int) -> CallToolResult:
    """The viewport state, with the image only when it is newer than `since`."""
    state, png = _viewport_snapshot()
    content = []
    if png is not None and state["seq"] > since:
        content.append(_png_content(png))
    return CallToolResult(content=content, structuredContent=state)


@mcp.tool(
    title=VIEWPORT_TITLE,
    annotations=_READ_ONLY,
    icons=[viewport_icon()],
    meta={
        "ui": {"resourceUri": VIEWPORT_URI, "visibility": ["app"]},
        "openai/ui": {"entrypoints": [{"type": "thread"}]},
    },
)
def open_viewport() -> CallToolResult:
    """Show the latest Blender viewport screenshot beside the conversation."""
    return _viewport_result(since=0)


@mcp.tool(annotations=_READ_ONLY, meta=_APP_ONLY)
def viewport_latest(since: int = 0) -> CallToolResult:
    """The latest viewport screenshot, if newer than `since`. Never touches Blender."""
    return _viewport_result(since)


@mcp.tool(meta=_APP_ONLY)
def viewport_capture(max_size: int = 1000, auto: bool = False) -> CallToolResult:
    """Capture a fresh viewport screenshot for the Viewport app.

    `auto` marks a capture the app took on its own after the scene changed,
    rather than one the user asked for with Refresh.
    """
    try:
        _store_capture(max_size, "auto" if auto else "user")
    except Exception as e:
        return _app_error(f"Couldn't capture the viewport: {e}")
    return _viewport_result(since=0)


def _app_error(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=True)


@mcp.tool(annotations=_READ_ONLY, meta=_APP_ONLY)
def viewport_pick(seq: int, x: float, y: float) -> CallToolResult:
    """The object under a click on viewport capture `seq`.

    `x` and `y` run 0..1 from the image's top-left corner. The ray uses the
    camera that capture was rendered with, so it works after the user has
    orbited the view, against the scene as it is now.
    """
    view = viewport_store.view(seq)
    if view is None:
        return _app_error("This screenshot can't be clicked on. Press Refresh for a new one.")
    try:
        hit = get_blender_connection().send_command("pick_viewport_object", {**view, "x": x, "y": y})
    except Exception as e:
        return _app_error(f"Couldn't reach Blender: {e}")
    hit = hit if isinstance(hit, dict) else {}
    if hit.get("mismatch") == "file":
        name = os.path.basename(view.get("file") or "") or "an unsaved file"
        return _app_error(f"This screenshot is of {name}, which isn't open in Blender now. Press Refresh for a new one.")
    if hit.get("mismatch") == "scene":
        return _app_error(
            f"This screenshot is of the scene '{view.get('scene')}', but Blender is showing "
            f"'{hit.get('current')}'. Switch back to it, or press Refresh."
        )
    obj = hit.get("object")
    if not obj:
        return CallToolResult(content=[], structuredContent={"object": None})
    link = ResourceLink(
        type="resource_link",
        uri=_scene_item_uri("object", obj["name"]),
        name=obj["name"],
        title=obj["name"],
        description=obj.get("detail"),
        mimeType="application/json",
    ).model_dump(by_alias=True, exclude_none=True, mode="json")
    return CallToolResult(content=[], structuredContent={"object": {**obj, "link": link}})


_client_features_logged = False


def _log_client_features(session) -> None:
    """Log once what the client advertised, since that decides which UI features it gets."""
    global _client_features_logged
    if _client_features_logged:
        return
    _client_features_logged = True
    params = getattr(session, "client_params", None)
    info = getattr(params, "clientInfo", None)
    logger.info(
        f"MCP client {getattr(info, 'name', '?')} {getattr(info, 'version', '')}: "
        f"extensions={sorted(client_extensions(session))}, apps={supports_apps(session)}"
    )


async def _list_tools_for_client():
    tools = await mcp.list_tools()
    try:
        session = mcp.get_context().session
    except Exception:
        return tools
    _log_client_features(session)
    if supports_apps(session):
        return tools
    return [t for t in tools if not is_app_only(t)]


mcp._mcp_server.list_tools()(_list_tools_for_client)


# Main execution

def main():
    """Run the MCP server, or addon install CLI subcommands."""
    global CLI_HOST, CLI_PORT, TOKEN

    if len(sys.argv) > 1 and sys.argv[1] in {"install-addon", "addon-paths", "setup", "-h", "--help"}:
        code = run_addon_cli(sys.argv[1:])
        if code >= 0:
            raise SystemExit(code)

    CLI_HOST, CLI_PORT = parse_connection_args(sys.argv[1:])
    TOKEN = resolve_token(sys.argv[1:])

    # When run by hand (stdin is a TTY) the server appears to "hang" while it
    # silently waits for an MCP client; log a hint so that state is obvious.
    # Launched by a client, stdin is a pipe so this is skipped, and logging goes
    # to stderr, never to the stdio protocol on stdout.
    try:
        interactive = sys.stdin.isatty()
    except (AttributeError, OSError):
        interactive = False
    if interactive:
        logger.info(
            "BlenderMCP is an MCP server and is meant to be launched by your MCP "
            "client (Claude Desktop, Cursor, VS Code, ...), not run by hand. "
            "It will now wait silently for a client on stdin -- that is normal, "
            "not a hang. Press Ctrl-C to exit. "
            "Setup guide: https://github.com/ozthealem/mcp-for-blender-lite#installation "
            "(if the addon is outdated this logs how to update it: mcp-for-blender-lite install-addon)"
        )
    context_log.install(mcp, SERVER_INSTRUCTIONS)
    mcp.run()

if __name__ == "__main__":
    main()