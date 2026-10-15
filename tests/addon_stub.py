"""Load the root addon.py with just enough of bpy stubbed out to import it.

Shared by tests that exercise addon code without a real Blender.
"""
import importlib.util
import sys
import types

from conftest import ROOT_ADDON as ADDON


def load_addon(monkeypatch, scene, selected_objects=()):
    bpy = types.ModuleType("bpy")
    bpy.context = types.SimpleNamespace(
        scene=scene,
        selected_objects=list(selected_objects),
        view_layer=types.SimpleNamespace(update=lambda: None),
    )
    bpy.ops = types.SimpleNamespace(
        import_scene=types.SimpleNamespace(gltf=lambda **_kwargs: None)
    )
    bpy.types = types.SimpleNamespace(
        AddonPreferences=object,
        Operator=object,
        Panel=object,
        Scene=type("Scene", (), {}),
    )

    props = types.ModuleType("bpy.props")
    for name in ("BoolProperty", "EnumProperty", "FloatProperty", "IntProperty", "StringProperty"):
        setattr(props, name, lambda **_kwargs: None)
    bpy.props = props

    handlers = types.ModuleType("bpy.app.handlers")
    handlers.persistent = lambda fn: fn
    handlers.undo_post = []
    handlers.redo_post = []
    handlers.depsgraph_update_post = []
    handlers.load_post = []

    app = types.ModuleType("bpy.app")
    app.version = (5, 2, 2)
    app.version_string = "5.2.2"
    app.background = False
    app.handlers = handlers
    app.timers = types.SimpleNamespace(
        is_registered=lambda *_a, **_k: False,
        register=lambda *_a, **_k: None,
        unregister=lambda *_a, **_k: None,
    )
    bpy.app = app

    monkeypatch.setitem(sys.modules, "bpy", bpy)
    monkeypatch.setitem(sys.modules, "bpy.props", props)
    monkeypatch.setitem(sys.modules, "bpy.app", app)
    monkeypatch.setitem(sys.modules, "bpy.app.handlers", handlers)
    monkeypatch.setitem(sys.modules, "mathutils", types.ModuleType("mathutils"))

    spec = importlib.util.spec_from_file_location("blender_mcp_addon_test", ADDON)
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    return addon


def scene():
    return types.SimpleNamespace()
