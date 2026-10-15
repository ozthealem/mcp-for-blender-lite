# Code created by Siddharth Ahuja: www.github.com/ahujasid © 2025
# MCP for Blender Lite: offline fork by ozthealem (no telemetry, no cloud services).

import bpy
import mathutils
import json
import threading
import socket
import queue
import time
import traceback
import os
from bpy.props import IntProperty, BoolProperty
import io
import hmac
from contextlib import redirect_stdout

bl_info = {
    "name": "MCP for Blender Lite",
    "author": "Siddharth Ahuja, ozthealem",
    "version": (1, 8, 1),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > MCP Lite",
    "description": "Offline MCP bridge: lets a local MCP client drive Blender",
    "doc_url": "https://github.com/ozthealem/mcp-for-blender-lite",
    "category": "Interface",
}

# Keep in sync with blender_mcp.addon_manager.EXPECTED_ADDON_PROTOCOL_VERSION.
ADDON_PROTOCOL_VERSION = 13

def get_blendermcp_addon_preferences(context=None):
    """Get add-on preferences object if available."""
    if context is None:
        context = bpy.context
    addon = context.preferences.addons.get(__name__)
    return addon.preferences if addon else None

# Object types with no surface for a ray to hit, picked by their origin instead.
_PICK_BY_ORIGIN = {"LIGHT", "CAMERA", "EMPTY", "LIGHT_PROBE", "SPEAKER", "FORCE_FIELD"}
# How close, in image pixels, a click must land to one of those origins.
_PICK_RADIUS_PX = 16


def _object_detail(obj):
    """'Mesh object in collection 'Props'', as mentions and viewport picks show it."""
    detail = f"{obj.type.title()} object"
    if obj.users_collection:
        detail += f" in collection '{obj.users_collection[0].name}'"
    return detail


class BlenderMCPServer:
    def __init__(self, port=9876):
        # Loopback only: the bridge runs arbitrary Python, so it must never
        # be reachable from another machine.
        self.host = '127.0.0.1'
        self.port = port
        self.running = False
        self.socket = None
        self.server_thread = None
        # Commands are pushed here by client threads and drained by a single
        # timer running on Blender's main thread. bpy.app.timers is not
        # thread-safe, so registering a timer per command (the previous
        # approach) could silently drop the callback - on Windows especially -
        # leaving the client blocked in recv() until its socket timeout.
        self.command_queue = queue.Queue()
        # Live client sockets, so stop() can unblock threads parked in recv().
        self._clients = set()
        self._clients_lock = threading.Lock()

    def start(self):
        if bpy.app.background:
            print("BlenderMCP: cannot start server in background mode (blender -b) - commands would never execute\n"
                  "BlenderMCP: run Blender with a GUI, or use a virtual display: xvfb-run -a blender")
            return

        if self.running:
            print("Server is already running")
            return

        self.running = True

        try:
            # Create socket
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            # Backlog of 1 meant a reconnecting client could complete the TCP
            # handshake and then never be accept()ed - a connection that looks
            # established but is never serviced.
            self.socket.listen(5)

            # Start server thread
            self.server_thread = threading.Thread(target=self._server_loop)
            self.server_thread.daemon = True
            self.server_thread.start()

            # start() is called from the operator, i.e. the main thread, so
            # this is the only safe place to touch bpy.app.timers.
            if not bpy.app.timers.is_registered(self._drain_command_queue):
                bpy.app.timers.register(self._drain_command_queue, persistent=True)

            print(f"BlenderMCP server started on {self.host}:{self.port}")
        except Exception as e:
            print(f"Failed to start server: {str(e)}")
            self.stop()

    def stop(self):
        self.running = False

        try:
            if bpy.app.timers.is_registered(self._drain_command_queue):
                bpy.app.timers.unregister(self._drain_command_queue)
        except Exception:
            pass

        # Close socket
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None

        # Shut down live client sockets. Without this, handler threads stay
        # parked in a blocking recv() forever; being daemon threads they then
        # outlive the restart and close connections the new server owns
        # (the WinError 10054 seen after toggling the addon).
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass

        # Drop any commands that will never be serviced now.
        while True:
            try:
                self.command_queue.get_nowait()
            except queue.Empty:
                break

        # Wait for thread to finish
        if self.server_thread:
            try:
                if self.server_thread.is_alive():
                    self.server_thread.join(timeout=1.0)
            except:
                pass
            self.server_thread = None

        print("BlenderMCP server stopped")

    def _server_loop(self):
        """Main server loop in a separate thread"""
        print("Server thread started")
        self.socket.settimeout(1.0)  # Timeout to allow for stopping

        while self.running:
            try:
                # Accept new connection
                try:
                    client, address = self.socket.accept()
                    print(f"Connected to client: {address}")

                    # Handle client in a separate thread
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client,)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                except socket.timeout:
                    # Just check running condition
                    continue
                except Exception as e:
                    print(f"Error accepting connection: {str(e)}")
                    time.sleep(0.5)
            except Exception as e:
                print(f"Error in server loop: {str(e)}")
                if not self.running:
                    break
                time.sleep(0.5)

        print("Server thread stopped")

    def _drain_command_queue(self):
        """Run queued commands on Blender's main thread.

        Registered once by start(); returns the poll interval so Blender keeps
        calling it. All bpy access happens here, on the main thread.
        """
        if not self.running:
            return None

        while True:
            try:
                command, client = self.command_queue.get_nowait()
            except queue.Empty:
                break

            try:
                response = self.execute_command(command)
                response_json = json.dumps(response)
            except Exception as e:
                print(f"Error executing command: {str(e)}")
                traceback.print_exc()
                response_json = json.dumps({"status": "error", "message": str(e)})

            try:
                client.sendall(response_json.encode('utf-8'))
            except Exception:
                print("Failed to send response - client disconnected")

        return 0.05

    def _handle_client(self, client):
        """Handle connected client"""
        print("Client handler started")
        # A finite timeout keeps this loop responsive to self.running instead
        # of parking in recv() forever.
        client.settimeout(1.0)
        with self._clients_lock:
            self._clients.add(client)
        buffer = b''

        try:
            while self.running:
                # Receive data
                try:
                    data = client.recv(8192)
                    if not data:
                        print("Client disconnected")
                        break

                    buffer += data
                    try:
                        # Try to parse command
                        command = json.loads(buffer.decode('utf-8'))
                        buffer = b''

                        # Hand off to the main thread. Never call
                        # bpy.app.timers.register() from here - it is not
                        # thread-safe and the callback can be silently lost.
                        print(f"Queued command: {command.get('type')}")
                        self.command_queue.put((command, client))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        # Incomplete data, wait for more. A multi-byte UTF-8
                        # character can land split across a recv() chunk
                        # boundary, which fails decode() before json.loads()
                        # ever runs - that's incomplete data too, not garbage.
                        pass
                except socket.timeout:
                    # Expected; loop round and re-check self.running.
                    continue
                except Exception as e:
                    print(f"Error receiving data: {str(e)}")
                    break
        except Exception as e:
            print(f"Error in client handler: {str(e)}")
        finally:
            with self._clients_lock:
                self._clients.discard(client)
            try:
                client.close()
            except:
                pass
            print("Client handler stopped")

    def execute_command(self, command):
        """Execute a command in the main Blender thread"""
        if not token_matches(command):
            return {"status": "error", "message": "Missing or wrong access token. "
                    "Pass the addon's token file to the MCP server with --token-file."}
        try:
            return self._execute_command_internal(command)

        except Exception as e:
            print(f"Error executing command: {str(e)}")
            traceback.print_exc()
            return {"status": "error", "message": str(e)}

    def _execute_command_internal(self, command):
        """Internal command execution with proper context"""
        cmd_type = command.get("type")
        params = command.get("params", {})

        # Trivial liveness check. Touches no bpy data, so a successful ping
        # alongside a failing command isolates data access from transport.
        if cmd_type == "ping":
            return {"status": "success", "result": {"pong": True}}

        handlers = {
            "get_scene_info": self.get_scene_info,
            "get_addon_info": self.get_addon_info,
            "get_object_info": self.get_object_info,
            "list_scene_items": self.list_scene_items,
            "get_viewport_screenshot": self.get_viewport_screenshot,
            "pick_viewport_object": self.pick_viewport_object,
            "execute_code": self.execute_code,
            "describe_node_type": self.describe_node_type,
            "bpy_api_lookup": self.bpy_api_lookup,
            "export_scene": self.export_scene,
        }

        handler = handlers.get(cmd_type)
        if handler:
            try:
                print(f"Executing handler for {cmd_type}")
                result = handler(**params)
                print(f"Handler execution complete")
                return {"status": "success", "result": result}
            except Exception as e:
                print(f"Error in handler: {str(e)}")
                traceback.print_exc()
                return {"status": "error", "message": str(e)}
        else:
            return {"status": "error", "message": f"Unknown command type: {cmd_type}"}



    def get_addon_info(self):
        """Version/capability handshake for the MCP server (and install tooling)."""
        return {
            "name": bl_info.get("name", "MCP for Blender"),
            "addon_version": list(bl_info.get("version", (0, 0))),
            "protocol_version": ADDON_PROTOCOL_VERSION,
            "capabilities": sorted([
                "get_scene_info",
                "get_addon_info",
                "get_object_info",
                "list_scene_items",
                "get_viewport_screenshot",
                "pick_viewport_object",
                "execute_code",
                "describe_node_type",
                "bpy_api_lookup",
                "export_scene",
            ]),
            "blender_version": bpy.app.version_string,
        }

    def get_scene_info(self):
        """Get information about the current Blender scene"""
        try:
            print("Getting scene info...")
            # Simplify the scene info to reduce data size
            scene_info = {
                "name": bpy.context.scene.name,
                "object_count": len(bpy.context.scene.objects),
                "objects": [],
                "materials_count": len(bpy.data.materials),
            }

            # Collect minimal object information (limit to first 10 objects)
            for i, obj in enumerate(bpy.context.scene.objects):
                if i >= 10:  # Reduced from 20 to 10
                    break

                obj_info = {
                    "name": obj.name,
                    "type": obj.type,
                    # Only include basic location data
                    "location": [round(float(obj.location.x), 2),
                                round(float(obj.location.y), 2),
                                round(float(obj.location.z), 2)],
                }
                scene_info["objects"].append(obj_info)

            print(f"Scene info collected: {len(scene_info['objects'])} objects")
            return scene_info
        except Exception as e:
            print(f"Error in get_scene_info: {str(e)}")
            traceback.print_exc()
            return {"error": str(e)}

    def list_scene_items(self, query="", limit=30):
        """Objects, materials and collections whose names match `query`.

        Backs composer @-mentions, which call this on every keystroke, so it
        reads names and cheap counts only. Name matches that start with the
        query rank before ones that merely contain it.
        """
        needle = (query or "").strip().lower()
        limit = max(1, min(int(limit or 30), 100))

        material_users = {}
        for obj in bpy.context.scene.objects:
            for slot in getattr(obj, "material_slots", None) or []:
                if slot.material:
                    material_users.setdefault(slot.material.name, []).append(obj.name)

        candidates = []
        for obj in bpy.context.scene.objects:
            candidates.append(("object", obj.name, _object_detail(obj)))
        for mat in bpy.data.materials:
            users = material_users.get(mat.name, [])
            if users:
                shown = ", ".join(users[:3]) + (f" and {len(users) - 3} more" if len(users) > 3 else "")
                detail = f"Material on {shown}"
            else:
                detail = "Material not used by any object in this scene"
            candidates.append(("material", mat.name, detail))
        for coll in bpy.data.collections:
            candidates.append(("collection", coll.name, f"Collection with {len(coll.all_objects)} objects"))

        matches = []
        for order, (kind, name, detail) in enumerate(candidates):
            lowered = name.lower()
            if needle and needle not in lowered:
                continue
            rank = 0 if not needle or lowered.startswith(needle) else 1
            matches.append((rank, order, {"kind": kind, "name": name, "detail": detail}))
        matches.sort(key=lambda m: (m[0], m[1]))
        return {"items": [m[2] for m in matches[:limit]], "total": len(matches)}

    @staticmethod
    def _get_aabb(obj):
        """ Returns the world-space axis-aligned bounding box (AABB) of an object. """
        if obj.type != 'MESH':
            raise TypeError("Object must be a mesh")

        # Get the bounding box corners in local space
        local_bbox_corners = [mathutils.Vector(corner) for corner in obj.bound_box]

        # Convert to world coordinates
        world_bbox_corners = [obj.matrix_world @ corner for corner in local_bbox_corners]

        # Compute axis-aligned min/max coordinates
        min_corner = mathutils.Vector(map(min, zip(*world_bbox_corners)))
        max_corner = mathutils.Vector(map(max, zip(*world_bbox_corners)))

        return [
            [*min_corner], [*max_corner]
        ]

    def get_object_info(self, name):
        """Get detailed information about a specific object"""
        obj = bpy.data.objects.get(name)
        if not obj:
            raise ValueError(f"Object not found: {name}")

        # Basic object info
        obj_info = {
            "name": obj.name,
            "type": obj.type,
            "location": [obj.location.x, obj.location.y, obj.location.z],
            "rotation": [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z],
            "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            "visible": obj.visible_get(),
            "materials": [],
        }

        if obj.type == "MESH":
            bounding_box = self._get_aabb(obj)
            obj_info["world_bounding_box"] = bounding_box

        # Add material slots
        for slot in obj.material_slots:
            if slot.material:
                obj_info["materials"].append(slot.material.name)

        # Add mesh data if applicable
        if obj.type == 'MESH' and obj.data:
            mesh = obj.data
            obj_info["mesh"] = {
                "vertices": len(mesh.vertices),
                "edges": len(mesh.edges),
                "polygons": len(mesh.polygons),
            }

        return obj_info

    def get_viewport_screenshot(self, max_size=800, filepath=None, format="png"):
        """
        Capture a screenshot of the current 3D viewport and save it to the specified path.

        Parameters:
        - max_size: Maximum size in pixels for the largest dimension of the image
        - filepath: Path where to save the screenshot file
        - format: Image format (png, jpg, etc.)

        Returns success/error status
        """
        # screen.screenshot_area captures the OS window framebuffer, which is
        # all-black whenever the Blender window is not composited in the
        # foreground (the normal case when Blender is driven headless-style via
        # MCP). Render the viewport with gpu.types.GPUOffScreen.draw_view3d
        # instead, which is independent of window compositing state, and fall
        # back to the window grab if offscreen rendering is unavailable (e.g. no
        # GPU context). The response reports which path produced the image.
        try:
            if not filepath:
                return {"error": "No filepath provided"}

            area = region = space = None
            for a in bpy.context.screen.areas:
                if a.type == 'VIEW_3D':
                    area = a
                    space = a.spaces.active
                    region = next((r for r in a.regions if r.type == 'WINDOW'), None)
                    break

            if not area or region is None or space is None:
                return {"error": "No 3D viewport found"}

            method = "offscreen"
            view = None
            # Which file and scene this shows, so the app can label it and a
            # click on it isn't cast into a different one.
            origin = {"file": bpy.data.filepath, "scene": bpy.context.scene.name}
            try:
                import gpu
                import numpy as np

                r3d = space.region_3d
                src_w, src_h = region.width, region.height
                if max(src_w, src_h) > max_size:
                    s = max_size / max(src_w, src_h)
                    width, height = max(1, int(src_w * s)), max(1, int(src_h * s))
                else:
                    width, height = src_w, src_h

                offscreen = gpu.types.GPUOffScreen(width, height)
                try:
                    offscreen.draw_view3d(
                        bpy.context.scene, bpy.context.view_layer, space, region,
                        r3d.view_matrix, r3d.window_matrix, do_color_management=True,
                    )
                    buf = offscreen.texture_color.read()
                finally:
                    offscreen.free()

                buf.dimensions = width * height * 4
                pixels = np.asarray(buf, dtype=np.float32) / 255.0  # GPU buffer is 0..255

                image = bpy.data.images.new("mcp_viewport", width, height, alpha=True)
                image.pixels.foreach_set(pixels.ravel())
                image.filepath_raw = filepath
                image.file_format = format.upper()
                image.save()
                bpy.data.images.remove(image)
                # The camera this image was drawn with, so a click on it can
                # be turned back into a ray (pick_viewport_object).
                view = {
                    **origin,
                    "view_matrix": [list(row) for row in r3d.view_matrix],
                    "window_matrix": [list(row) for row in r3d.window_matrix],
                    "width": width,
                    "height": height,
                }

            except Exception as offscreen_err:
                print(f"[BlenderMCP] offscreen capture failed ({offscreen_err}); "
                      "falling back to window grab", flush=True)
                method = "window_grab"
                with bpy.context.temp_override(area=area):
                    bpy.ops.screen.screenshot_area(filepath=filepath)
                img = bpy.data.images.load(filepath)
                width, height = img.size
                if max(width, height) > max_size:
                    s = max_size / max(width, height)
                    width, height = int(width * s), int(height * s)
                    img.scale(width, height)
                    img.file_format = format.upper()
                    img.save()
                bpy.data.images.remove(img)

            result = {
                "success": True,
                "width": width,
                "height": height,
                "filepath": filepath,
                "method": method,
                **origin,
                "scene_count": len(bpy.data.scenes),
            }
            if view:
                result["view"] = view
            return result

        except Exception as e:
            return {"error": str(e)}

    def pick_viewport_object(self, view_matrix, window_matrix, width, height, x, y, file=None, scene=None):
        """The object under a click on a viewport capture.

        The matrices are the ones the capture was drawn with, and x, y run 0..1
        from the image's top-left, so this works after the view has moved.
        Meshes are hit by a ray; lights, cameras and empties have no surface,
        so they're picked when the click lands near their origin on screen.
        `file` and `scene` are where the capture came from: a click is only
        cast into that same scene, never into whatever is open now.
        """
        from mathutils import Matrix, Vector

        if file is not None and file != bpy.data.filepath:
            return {"object": None, "mismatch": "file", "current": bpy.data.filepath}
        if scene is not None and scene != bpy.context.scene.name:
            return {"object": None, "mismatch": "scene", "current": bpy.context.scene.name}
        scene = bpy.context.scene
        projection = Matrix(window_matrix) @ Matrix(view_matrix)
        unproject = projection.inverted()
        ndc_x, ndc_y = 2.0 * float(x) - 1.0, 1.0 - 2.0 * float(y)

        def at_depth(z):
            p = unproject @ Vector((ndc_x, ndc_y, z, 1.0))
            return p.xyz / p.w

        origin = at_depth(-1.0)
        direction = (at_depth(1.0) - origin).normalized()

        picked, picked_depth = None, float("inf")
        hit, location, _normal, _index, hit_obj, _matrix = scene.ray_cast(
            bpy.context.evaluated_depsgraph_get(), origin, direction)
        if hit and hit_obj is not None:
            picked = getattr(hit_obj, "original", hit_obj)
            picked_depth = (location - origin).length

        click_x, click_y = float(x) * width, float(y) * height
        nearest = _PICK_RADIUS_PX
        for obj in scene.objects:
            if obj.type not in _PICK_BY_ORIGIN or not obj.visible_get():
                continue
            p = projection @ obj.matrix_world.translation.to_4d()
            if p.w <= 0:
                continue  # behind the camera
            screen_x = (p.x / p.w + 1.0) / 2.0 * width
            screen_y = (1.0 - p.y / p.w) / 2.0 * height
            distance = ((screen_x - click_x) ** 2 + (screen_y - click_y) ** 2) ** 0.5
            depth = (obj.matrix_world.translation - origin).length
            # An origin in front of the hit surface wins, as it's drawn on top.
            if distance <= nearest and depth < picked_depth:
                picked, nearest = obj, distance

        if picked is None:
            return {"object": None}
        return {"object": {
            "name": picked.name,
            "type": picked.type,
            "detail": _object_detail(picked),
            "location": list(picked.matrix_world.translation),
        }}

    def execute_code(self, code):
        """Execute arbitrary Blender Python code"""
        # This is powerful but potentially dangerous - use with caution
        try:
            # Create a local namespace for execution
            namespace = {"bpy": bpy}

            # Capture stdout during execution, and return it as result
            capture_buffer = io.StringIO()
            with redirect_stdout(capture_buffer):
                exec(code, namespace)

            captured_output = capture_buffer.getvalue()
            return {"executed": True, "result": captured_output}
        except Exception as e:
            # Give the caller the same detail we have: exception type, message,
            # and a full traceback (with line numbers into the submitted code),
            # instead of collapsing everything into one string. Callers that ran
            # a multi-line script otherwise cannot tell which line failed.
            tb = traceback.format_exc()
            raise Exception(
                json.dumps({
                    "exception_type": type(e).__name__,
                    "message": str(e),
                    "traceback": tb,
                })
            )

    # ------------------------------------------------------------------
    # Documentation / introspection helpers.
    #
    # These never touch the current scene or node tree - they exist purely
    # to answer "what does this thing look like" questions (property names,
    # types, enum values, socket order, function/operator signatures) so an
    # LLM can get a structured answer in one call instead of guessing and
    # discovering the shape of things via a chain of failed execute_code
    # attempts.
    # ------------------------------------------------------------------

    @staticmethod
    def _describe_property(prop):
        """Structured description of a single bpy RNA property."""
        entry = {
            "identifier": prop.identifier,
            "name": prop.name,
            "type": prop.type,  # FLOAT, INT, BOOLEAN, STRING, ENUM, POINTER, COLLECTION
            "description": prop.description,
        }
        for attr in ("is_required", "is_readonly", "is_argument_optional", "array_length"):
            value = getattr(prop, attr, None)
            if value is not None:
                entry[attr] = value

        if prop.type == 'ENUM':
            try:
                entry["enum_items"] = [item.identifier for item in prop.enum_items]
            except Exception:
                pass
            try:
                entry["default"] = prop.default
            except Exception:
                pass
        elif prop.type in ('FLOAT', 'INT'):
            try:
                entry["default"] = (
                    list(prop.default_array) if getattr(prop, "array_length", 0) else prop.default
                )
            except Exception:
                pass
            for attr in ("hard_min", "hard_max", "soft_min", "soft_max", "subtype", "unit", "step"):
                value = getattr(prop, attr, None)
                if value is not None:
                    entry[attr] = value
        elif prop.type == 'BOOLEAN':
            try:
                entry["default"] = prop.default
            except Exception:
                pass
        elif prop.type == 'STRING':
            try:
                entry["default"] = prop.default
            except Exception:
                pass
            max_length = getattr(prop, "max_length", None)
            if max_length:
                entry["max_length"] = max_length
        elif prop.type == 'POINTER':
            fixed_type = getattr(prop, "fixed_type", None)
            if fixed_type is not None:
                entry["pointer_type"] = fixed_type.identifier
        elif prop.type == 'COLLECTION':
            fixed_type = getattr(prop, "fixed_type", None)
            if fixed_type is not None:
                entry["collection_type"] = fixed_type.identifier
        return entry

    def describe_node_type(self, bl_idname, property_overrides=None):
        """Describe a node type's properties and socket schema.

        This is the fix for the single most common failure mode: guessing
        socket names/indices and enum values instead of looking them up.
        Since a node's sockets are only known once instantiated (and can
        depend on mode-like properties, e.g. Mix's `data_type`), this
        creates a throwaway node in a scratch node tree, optionally applies
        `property_overrides` first (e.g. {"data_type": "RGBA"}) so the
        caller can see the exact socket layout for the mode they intend to
        use, then reports its properties/inputs/outputs, and finally
        deletes the scratch tree. Nothing in the user's actual scene is
        touched.
        """
        node_cls = getattr(bpy.types, bl_idname, None)
        if node_cls is None or not (isinstance(node_cls, type) and issubclass(node_cls, bpy.types.Node)):
            candidates = [
                name for name in dir(bpy.types)
                if "Node" in name and bl_idname.lower() in name.lower()
            ]
            return {
                "error": f"Unknown node type: {bl_idname}",
                "did_you_mean": sorted(candidates)[:15],
            }

        tree_type_candidates = [
            "ShaderNodeTree", "GeometryNodeTree", "CompositorNodeTree", "TextureNodeTree",
        ]
        node = None
        tree = None
        used_tree_type = None
        attempts = []
        for tree_type in tree_type_candidates:
            tmp_tree = None
            try:
                tmp_tree = bpy.data.node_groups.new(name="__mcp_introspect_tmp__", type=tree_type)
                node = tmp_tree.nodes.new(type=bl_idname)
                tree = tmp_tree
                used_tree_type = tree_type
                break
            except Exception as e:
                attempts.append(f"{tree_type}: {e}")
                if tmp_tree is not None:
                    try:
                        bpy.data.node_groups.remove(tmp_tree)
                    except Exception:
                        pass

        if node is None:
            return {
                "error": f"Could not instantiate node '{bl_idname}' in any node tree type",
                "attempts": attempts,
            }

        try:
            warnings = []
            if property_overrides:
                for key, value in property_overrides.items():
                    try:
                        setattr(node, key, value)
                    except Exception as e:
                        warnings.append(f"Could not set property '{key}' = {value!r}: {e}")

            base_props = set(bpy.types.Node.bl_rna.properties.keys())
            properties = [
                self._describe_property(prop)
                for prop in node.bl_rna.properties
                if prop.identifier not in base_props
            ]

            def describe_sockets(sockets):
                out = []
                for index, socket in enumerate(sockets):
                    entry = {
                        "index": index,
                        "identifier": socket.identifier,
                        "name": socket.name,
                        "type": socket.type,
                        "is_multi_input": getattr(socket, "is_multi_input", False),
                        "hide_value": getattr(socket, "hide_value", False),
                        "is_linked": socket.is_linked,
                    }
                    if hasattr(socket, "default_value"):
                        try:
                            default_value = socket.default_value
                            if hasattr(default_value, "__len__") and not isinstance(default_value, str):
                                entry["default_value"] = list(default_value)
                            else:
                                entry["default_value"] = default_value
                        except Exception:
                            pass
                    out.append(entry)
                return out

            result = {
                "bl_idname": bl_idname,
                "label": node.bl_label,
                "instantiated_in": used_tree_type,
                "properties": properties,
                "inputs": describe_sockets(node.inputs),
                "outputs": describe_sockets(node.outputs),
                "applied_property_overrides": property_overrides or {},
                "note": (
                    "Sockets reflect the node's current property values (after any "
                    "property_overrides applied above). Enum/mode-like properties "
                    "(e.g. data_type, blend_type) can add, remove or reorder sockets - "
                    "pass the mode you intend to use via property_overrides to see the "
                    "real layout before writing code that indexes these sockets."
                ),
            }
            if warnings:
                result["warnings"] = warnings
            return result
        finally:
            try:
                bpy.data.node_groups.remove(tree)
            except Exception:
                pass

    def bpy_api_lookup(self, query):
        """Structured RNA reference lookup: types, properties, functions, operators.

        Accepts things like:
          - "ShaderNodeTexSky" or "bpy.types.ShaderNodeTexSky"       -> full type schema
          - "ShaderNodeTexSky.sky_type"                              -> one property, with enum items
          - "Object.ray_cast"                                        -> one method's parameters/returns
          - "bpy.ops.mesh.primitive_cube_add"                        -> operator parameters
        This replaces scraping `help()` text: every answer is structured
        JSON with real type names, enum identifiers, and required/optional
        flags, not something that has to be re-parsed out of a text blob.
        """
        query = (query or "").strip()
        if not query:
            return {"error": "Empty query"}

        q = query[4:] if query.startswith("bpy.") else query

        # bpy.ops.<category>.<operator_name>
        if q.startswith("ops."):
            op_parts = q[len("ops."):].split(".")
            op_parts = [p.split("(")[0] for p in op_parts if p]
            if len(op_parts) < 2:
                return {"error": f"Incomplete operator path: bpy.{q}. Expected bpy.ops.<category>.<name>"}
            category, op_name = op_parts[0], op_parts[1]
            op_group = getattr(bpy.ops, category, None)
            op = getattr(op_group, op_name, None) if op_group is not None else None
            if op is None:
                return {"error": f"Unknown operator: bpy.ops.{category}.{op_name}"}
            try:
                rna = op.get_rna_type()
            except Exception as e:
                return {"error": f"Could not introspect operator bpy.ops.{category}.{op_name}: {e}"}
            parameters = [
                self._describe_property(prop)
                for prop in rna.properties
                if prop.identifier != "rna_type"
            ]
            return {
                "kind": "operator",
                "idname": f"bpy.ops.{category}.{op_name}",
                "label": rna.name,
                "description": rna.description,
                "parameters": parameters,
            }

        parts = [p for p in q.split(".") if p and p != "types"]
        if not parts:
            return {"error": "Empty query"}

        type_name = parts[0]
        node_cls = getattr(bpy.types, type_name, None)
        if node_cls is None:
            matches = sorted(
                name for name in dir(bpy.types)
                if type_name.lower() in name.lower()
            )
            return {
                "error": f"Unknown type: {type_name}",
                "did_you_mean": matches[:15],
            }

        if len(parts) == 1:
            properties = [
                self._describe_property(prop)
                for prop in node_cls.bl_rna.properties
                if prop.identifier != "rna_type"
            ]
            functions = []
            for func in node_cls.bl_rna.functions:
                functions.append({
                    "identifier": func.identifier,
                    "description": func.description,
                    "parameters": [
                        self._describe_property(p) for p in func.parameters if not p.is_output
                    ],
                    "returns": [
                        self._describe_property(p) for p in func.parameters if p.is_output
                    ],
                })
            return {
                "kind": "type",
                "bl_idname": type_name,
                "description": node_cls.bl_rna.description,
                "properties": properties,
                "functions": functions,
            }

        # Type.member - could be a property or a function/method
        member_name = parts[1]
        prop = node_cls.bl_rna.properties.get(member_name)
        if prop is not None:
            entry = self._describe_property(prop)
            entry["kind"] = "property"
            entry["owner_type"] = type_name
            return entry

        func = node_cls.bl_rna.functions.get(member_name)
        if func is not None:
            return {
                "kind": "function",
                "owner_type": type_name,
                "identifier": func.identifier,
                "description": func.description,
                "parameters": [self._describe_property(p) for p in func.parameters if not p.is_output],
                "returns": [self._describe_property(p) for p in func.parameters if p.is_output],
            }

        available = sorted(
            list(node_cls.bl_rna.properties.keys()) + list(node_cls.bl_rna.functions.keys())
        )
        return {
            "error": f"'{type_name}' has no property or function named '{member_name}'",
            "did_you_mean": [name for name in available if member_name.lower() in name.lower()][:15],
        }

    def export_scene(self, filepath, format="glb", object_names=None, selection_only=False, apply_modifiers=True):
        """Export the whole scene, the current selection, or the named objects to a GLB or FBX file.

        Named objects are exported together with their children. GLB carries PBR
        materials, emission, skins, shape keys and animation; FBX is the fallback for
        tools that need Unity's built-in importer. apply_modifiers=False keeps rigs
        and shape keys intact. The file is written where the caller asked, so other
        applications (game engines, viewers) can pick it up without going through
        execute_code.
        """
        if not filepath:
            return {"error": "filepath is required"}
        fmt = (format or "glb").lower()
        if fmt not in ("glb", "fbx"):
            return {"error": f"format must be glb or fbx, got '{format}'"}

        names = [n for n in (object_names or []) if n]
        use_selection = False
        exported = []
        if names:
            missing = [n for n in names if bpy.data.objects.get(n) is None]
            if missing:
                return {"error": "Objects not found in Blender: " + ", ".join(missing)}
            bpy.ops.object.select_all(action='DESELECT')
            for n in names:
                obj = bpy.data.objects[n]
                for o in [obj, *obj.children_recursive]:
                    o.select_set(True)
                    if o.name not in exported:
                        exported.append(o.name)
            bpy.context.view_layer.objects.active = bpy.data.objects[names[0]]
            use_selection = True
        elif selection_only:
            if not bpy.context.selected_objects:
                return {"error": "Nothing is selected in Blender and no object_names were given"}
            exported = [o.name for o in bpy.context.selected_objects]
            use_selection = True
        else:
            exported = [o.name for o in bpy.context.scene.objects]

        try:
            if bpy.context.object and getattr(bpy.context.object, "mode", 'OBJECT') != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
        except Exception:
            pass

        directory = os.path.dirname(filepath)
        if directory:
            os.makedirs(directory, exist_ok=True)

        if fmt == "glb":
            bpy.ops.export_scene.gltf(
                filepath=filepath, export_format='GLB', use_selection=use_selection,
                use_active_scene=True, export_apply=apply_modifiers,
                export_animations=True, export_skins=True, export_morph=True, export_yup=True)
        else:
            bpy.ops.export_scene.fbx(
                filepath=filepath, use_selection=use_selection, apply_unit_scale=True,
                bake_space_transform=apply_modifiers, use_mesh_modifiers=apply_modifiers,
                path_mode='COPY', embed_textures=True)

        return {
            "path": filepath,
            "bytes": os.path.getsize(filepath),
            "selection_only": use_selection,
            "exported": exported,
        }

#region Access token

# Optional shared secret. Off by default: anything on this machine that can
# reach 127.0.0.1 could otherwise drive Blender. When on, every command must
# carry the token, which the MCP server reads from TOKEN_FILE_NAME (pass it
# with --token-file). The token never goes on a command line.
TOKEN_FILE_NAME = "mcp_lite_token.txt"


def token_file_path():
    return os.path.join(bpy.utils.user_resource('CONFIG'), TOKEN_FILE_NAME)


def required_token():
    """The token commands must carry, or None when the check is off."""
    prefs = get_blendermcp_addon_preferences()
    if prefs is None or not prefs.use_token or not prefs.token:
        return None
    return prefs.token


def token_matches(command):
    expected = required_token()
    if expected is None:
        return True
    given = command.get("token")
    return isinstance(given, str) and hmac.compare_digest(given, expected)

#endregion


class BLENDERMCP_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    use_token: BoolProperty(
        name="Require Token",
        description="Only accept commands that carry the access token. Off by default",
        default=False,
    )
    token: bpy.props.StringProperty(
        name="Token",
        subtype="PASSWORD",
        description="Shared secret the MCP server sends with every command",
        default="",
    )

    def draw(self, context):
        layout = self.layout
        layout.label(text="This build is offline: no telemetry, no asset sites, no cloud generators.",
                     icon='LOCKED')
        layout.label(text="The server listens on 127.0.0.1 only and starts when you press Connect.",
                     icon='BLANK1')
        draw_token_box(layout, self)


def draw_token_box(layout, prefs):
    box = layout.box()
    box.prop(prefs, "use_token")
    if prefs.use_token:
        col = box.column(align=True)
        if prefs.token:
            col.label(text="Token file for the MCP server (--token-file):")
            col.label(text=token_file_path())
        else:
            col.label(text="No token yet.", icon='ERROR')
        col.operator("blendermcp.new_token", text="New Token", icon='KEY_HLT')


# Blender UI Panel
class BLENDERMCP_PT_Panel(bpy.types.Panel):
    bl_label = "MCP for Blender Lite"
    bl_idname = "BLENDERMCP_PT_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'MCP Lite'

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        prefs = get_blendermcp_addon_preferences(context)

        box = layout.box()
        col = box.column()
        if scene.blendermcp_server_running:
            server = getattr(bpy.types, "blendermcp_server", None)
            running_port = getattr(server, "port", scene.blendermcp_port)
            col.label(text=f"Listening on 127.0.0.1:{running_port}", icon='CHECKMARK')
            col.operator("blendermcp.stop_server", text="Disconnect", icon='X')
        else:
            col.label(text="Not connected", icon='RADIOBUT_OFF')
            col.prop(scene, "blendermcp_port")
            col.operator("blendermcp.start_server", text="Connect", icon='PLAY')

        if prefs:
            draw_token_box(layout, prefs)
        layout.label(text=f"Addon {'.'.join(map(str, bl_info['version']))} · offline")


class BLENDERMCP_OT_NewToken(bpy.types.Operator):
    bl_idname = "blendermcp.new_token"
    bl_label = "New Token"
    bl_description = "Make a new random token and write it to the token file. The old token stops working"

    def execute(self, context):
        import secrets
        prefs = get_blendermcp_addon_preferences(context)
        if prefs is None:
            self.report({'ERROR'}, "Addon preferences are not available")
            return {'CANCELLED'}
        token = secrets.token_hex(32)
        path = token_file_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(token)
        except OSError as e:
            self.report({'ERROR'}, f"Could not write the token file: {e}")
            return {'CANCELLED'}
        prefs.token = token
        self.report({'INFO'}, f"New token written to {path}")
        return {'FINISHED'}


# Operator to start the server
class BLENDERMCP_OT_StartServer(bpy.types.Operator):
    bl_idname = "blendermcp.start_server"
    bl_label = "Connect"
    bl_description = "Start listening for the MCP server on 127.0.0.1"

    def execute(self, context):
        scene = context.scene

        # Create a new server instance
        if not hasattr(bpy.types, "blendermcp_server") or not bpy.types.blendermcp_server:
            bpy.types.blendermcp_server = BlenderMCPServer(port=scene.blendermcp_port)

        # Start the server
        bpy.types.blendermcp_server.start()
        scene.blendermcp_server_running = bpy.types.blendermcp_server.running

        return {'FINISHED'}


# Operator to stop the server
class BLENDERMCP_OT_StopServer(bpy.types.Operator):
    bl_idname = "blendermcp.stop_server"
    bl_label = "Disconnect"
    bl_description = "Stop listening for the MCP server"

    def execute(self, context):
        scene = context.scene

        # Stop the server if it exists
        if hasattr(bpy.types, "blendermcp_server") and bpy.types.blendermcp_server:
            bpy.types.blendermcp_server.stop()
            del bpy.types.blendermcp_server

        scene.blendermcp_server_running = False

        return {'FINISHED'}


CLASSES = (
    BLENDERMCP_AddonPreferences,
    BLENDERMCP_PT_Panel,
    BLENDERMCP_OT_NewToken,
    BLENDERMCP_OT_StartServer,
    BLENDERMCP_OT_StopServer,
)


# Registration functions
def register():
    bpy.types.Scene.blendermcp_port = IntProperty(
        name="Port",
        description="Port for the MCP for Blender server",
        default=9876,
        min=1024,
        max=65535
    )

    bpy.types.Scene.blendermcp_server_running = bpy.props.BoolProperty(
        name="Server Running",
        default=False
    )

    for cls in CLASSES:
        bpy.utils.register_class(cls)

    # No auto-start: the socket opens only when the user presses Connect.
    print("BlenderMCP Lite addon registered")


def unregister():
    # Stop the server if it's running
    if hasattr(bpy.types, "blendermcp_server") and bpy.types.blendermcp_server:
        bpy.types.blendermcp_server.stop()
        del bpy.types.blendermcp_server

    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)

    del bpy.types.Scene.blendermcp_port
    del bpy.types.Scene.blendermcp_server_running

    print("BlenderMCP Lite addon unregistered")


if __name__ == "__main__":
    register()
