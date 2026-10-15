"""The optional access token: off by default, and when on, every command needs it."""
import types

from addon_stub import load_addon, scene
from blender_mcp import server


def test_no_token_file_means_no_token(monkeypatch):
    monkeypatch.delenv(server.TOKEN_FILE_ENV, raising=False)
    assert server.resolve_token([]) is None


def test_token_is_read_from_the_file_not_the_command_line(tmp_path, monkeypatch):
    monkeypatch.delenv(server.TOKEN_FILE_ENV, raising=False)
    path = tmp_path / "token.txt"
    path.write_text("abc123\n", encoding="utf-8")
    assert server.resolve_token(["--token-file", str(path)]) == "abc123"
    monkeypatch.setenv(server.TOKEN_FILE_ENV, str(path))
    assert server.resolve_token([]) == "abc123"


def test_a_missing_token_file_does_not_stop_the_server(tmp_path, monkeypatch):
    monkeypatch.delenv(server.TOKEN_FILE_ENV, raising=False)
    assert server.resolve_token(["--token-file", str(tmp_path / "nope.txt")]) is None


def test_token_file_flag_is_not_reported_as_unknown(caplog):
    server.parse_connection_args(["--token-file", "x"])
    assert "unrecognized" not in caplog.text


def _addon_with_prefs(monkeypatch, use_token, token):
    addon = load_addon(monkeypatch, scene())
    prefs = types.SimpleNamespace(use_token=use_token, token=token)
    monkeypatch.setattr(addon, "get_blendermcp_addon_preferences", lambda context=None: prefs)
    return addon


def test_addon_accepts_everything_when_the_token_is_off(monkeypatch):
    addon = _addon_with_prefs(monkeypatch, False, "secret")
    assert addon.token_matches({"type": "ping"})


def test_addon_refuses_commands_without_the_right_token(monkeypatch):
    addon = _addon_with_prefs(monkeypatch, True, "secret")
    assert not addon.token_matches({"type": "ping"})
    assert not addon.token_matches({"type": "ping", "token": "wrong"})
    assert addon.token_matches({"type": "ping", "token": "secret"})
    reply = addon.BlenderMCPServer().execute_command({"type": "ping"})
    assert reply["status"] == "error" and "token" in reply["message"]


def test_addon_listens_on_loopback_only(monkeypatch):
    addon = load_addon(monkeypatch, scene())
    assert addon.BlenderMCPServer(port=9999).host == "127.0.0.1"
