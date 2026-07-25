#!/usr/bin/env python3
"""
Minimal stdio MCP server exposing screen capture and input tools for Steam Deck.
No external dependencies — pure Python stdlib + gamescopectl/xdotool/ydotool.
"""
import base64
import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request

# Spawned by `claude` as a bare script, so the plugin directory is normally
# sys.path[0] already — pin it anyway so the sibling module resolves no matter
# how the interpreter was invoked.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import deck_common  # noqa: E402  (needs the sys.path anchor above)

# Steam's CEF remote debugger (Decky Loader keeps it enabled)
CEF_HOST = os.environ.get("DECKY_CLAUDE_CEF_HOST", "127.0.0.1")
CEF_PORT = int(os.environ.get("DECKY_CLAUDE_CEF_PORT", "8080"))

# ── display environment ────────────────────────────────────────────────────────

def _display_env() -> dict[str, str]:
    return deck_common.apply_display_env(dict(os.environ))

def _run(cmd: list[str], timeout: float = 10) -> tuple[int, str]:
    env = _display_env()
    try:
        r = subprocess.run(cmd, env=env, capture_output=True, timeout=timeout)
        return r.returncode, r.stderr.decode(errors="replace").strip()
    except FileNotFoundError:
        return 1, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 1, "timeout"

# ── Chrome DevTools Protocol client (for Steam's CEF UI) ──────────────────────

class _WebSocket:
    """Minimal RFC 6455 client — just enough for a CDP request/response."""

    def __init__(self, url: str, timeout: float = 10.0):
        m = re.match(r"ws://([^:/]+):(\d+)(/.*)", url)
        if not m:
            raise ValueError(f"unsupported ws url: {url}")
        host, port, path = m.group(1), int(m.group(2)), m.group(3)
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake: connection closed")
            response += chunk
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"handshake rejected: {response[:120]!r}")

    def _read_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("connection closed mid-frame")
            buf += chunk
        return buf

    def send_text(self, text: str) -> None:
        payload = text.encode()
        mask = os.urandom(4)
        header = bytearray([0x81])  # FIN + text
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv_message(self) -> str:
        """Read one complete (possibly fragmented) text message."""
        parts: list[bytes] = []
        while True:
            b1, b2 = self._read_exact(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read_exact(8))[0]
            if b2 & 0x80:  # masked server frame — not expected, but handle it
                mask = self._read_exact(4)
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read_exact(n)))
            else:
                data = self._read_exact(n)
            if opcode == 0x8:  # close
                raise ConnectionError("server closed connection")
            if opcode == 0x9:  # ping → pong
                self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if opcode in (0x1, 0x0):
                parts.append(data)
                if fin:
                    return b"".join(parts).decode(errors="replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _cef_targets() -> list[dict]:
    with urllib.request.urlopen(f"http://{CEF_HOST}:{CEF_PORT}/json", timeout=5) as r:
        return json.loads(r.read().decode())


def _cef_eval(expression: str, target: str = "SharedJSContext", timeout: float = 15.0) -> dict:
    targets = _cef_targets()
    needle = target.lower()
    match = next(
        (
            t for t in targets
            if needle in (t.get("title") or "").lower()
            or needle in (t.get("url") or "").lower()
        ),
        None,
    )
    if not match:
        titles = [t.get("title") or t.get("url") for t in targets]
        raise LookupError(f"no CEF target matching {target!r}; available: {titles}")

    ws = _WebSocket(match["webSocketDebuggerUrl"], timeout=timeout)
    try:
        ws.send_text(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        }))
        while True:
            msg = json.loads(ws.recv_message())
            if msg.get("id") == 1:
                return msg
    finally:
        ws.close()

# ── tool definitions ───────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "steam_ui_targets",
        "description": (
            "List the Steam client's UI pages (Chrome DevTools Protocol targets "
            "on the CEF debugger, port 8080). Use this to see which contexts "
            "exist before steam_ui_eval. 'SharedJSContext' hosts the SteamClient "
            "API; other targets are UI surfaces like QuickAccess or MainMenu."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "steam_ui_eval",
        "description": (
            "Evaluate JavaScript inside the Steam client UI via the Chrome "
            "DevTools Protocol — the primary tool for debugging Steam itself. "
            "Runs in 'SharedJSContext' by default, where the SteamClient API "
            "lives: explore with Object.keys(SteamClient), inspect apps, "
            "settings, downloads, or trigger real Steam actions instead of "
            "clicking pixels. Set target to another page title (from "
            "steam_ui_targets) to inspect that page's DOM/state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "JavaScript expression (promises are awaited)",
                },
                "target": {
                    "type": "string",
                    "description": "Substring of the target page title (default: SharedJSContext)",
                    "default": "SharedJSContext",
                },
            },
            "required": ["expression"],
        },
    },
    {
        "name": "screenshot",
        "description": (
            "Capture the current screen (game, UI, error dialog, desktop). "
            "Call this at the start of any request that involves the game or "
            "visual state before answering. Returns a PNG image."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "send_key",
        "description": (
            "Send a key press to the focused application. "
            "key = xdotool key name, e.g. 'escape', 'Return', 'space', "
            "'Tab', 'F1'–'F12', 'ctrl+c', 'alt+F4'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "xdotool key name"}
            },
            "required": ["key"],
        },
    },
    {
        "name": "type_text",
        "description": "Type a string of text into the focused application.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "mouse_move_click",
        "description": (
            "Move the mouse to absolute screen coordinates and click. "
            "Use after a screenshot to click specific UI elements. "
            "Steam Deck native resolution is 1280×800."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X coordinate in pixels"},
                "y": {"type": "integer", "description": "Y coordinate in pixels"},
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "middle"],
                    "default": "left",
                },
            },
            "required": ["x", "y"],
        },
    },
]

# ── tool handlers ──────────────────────────────────────────────────────────────


def _run_first(cmds: list[list[str]]) -> tuple[int, str, str]:
    """Run each command until one succeeds; returns (rc, stderr, tool name)."""
    rc, err, tool = 1, "no command to run", ""
    for cmd in cmds:
        tool = cmd[0]
        rc, err = _run(cmd)
        if rc == 0:
            break
    return rc, err, tool


def _gamescope_screenshot(path: str) -> str | None:
    """Capture via gamescope, or an error string explaining why not.

    This plugin targets Gaming Mode only, so capture goes through gamescope and
    nothing else. That is not one option among several: it is the same
    compositor-side capture the controller's screenshot button triggers (Steam
    sets GAMESCOPECTRL_REQUEST_SCREENSHOT, gamescope does the work), just
    addressed directly so the frame lands at a path we choose instead of in the
    user's Steam screenshot library. gamescope implements no Wayland screencopy
    protocol, so grim and friends cannot capture here regardless.
    """
    env = _display_env()
    if not env.get("GAMESCOPE_WAYLAND_DISPLAY"):
        return "not running under gamescope — screen capture needs Gaming Mode"

    # Outside a live gamescope, gamescopectl still exits 0 after failing to
    # connect, so the file itself is the only trustworthy success signal.
    rc, err = _run(["gamescopectl", "screenshot", path], timeout=15)
    if rc != 0:
        return err or "gamescopectl failed"
    # gamescope writes the file from its own render thread, so it can land
    # slightly after the command returns.
    for _ in range(20):
        if os.path.exists(path) and os.path.getsize(path):
            return None
        time.sleep(0.1)
    return "gamescope produced no screenshot"


def _handle_screenshot() -> list[dict]:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        path = f.name

    failure = _gamescope_screenshot(path)
    if failure:
        if os.path.exists(path):
            os.unlink(path)
        return [{"type": "text", "text": f"Screenshot failed — {failure}."}]

    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode()
    os.unlink(path)
    return [{"type": "image", "data": data, "mimeType": "image/png"}]


def _handle_send_key(key: str) -> list[dict]:
    rc, err, tool = _run_first(deck_common.key_commands(key))
    if rc == 0:
        return [{"type": "text", "text": f"Key sent: {key} ({tool})"}]
    return [{"type": "text", "text": f"send_key failed for {key!r}: {err}"}]


def _handle_type_text(text: str) -> list[dict]:
    rc, err, tool = _run_first(deck_common.type_commands(text))
    if rc == 0:
        return [{"type": "text", "text": f"Typed: {text!r} ({tool})"}]
    return [{"type": "text", "text": f"type_text failed: {err}"}]


def _handle_mouse_move_click(x: int, y: int, button: str = "left") -> list[dict]:
    button = deck_common.normalize_button(button)
    # Both lists are ordered xdotool-then-ydotool, so each pair is one backend:
    # a move that lands with the other backend's click would move twice.
    moves = deck_common.mouse_move_commands(x, y)
    clicks = deck_common.click_commands(button)
    err = "no input tool available"
    for move, click in zip(moves, clicks):
        rc, err = _run(move)
        if rc != 0:
            continue
        rc, err = _run(click)
        if rc == 0:
            return [{"type": "text", "text": (
                f"Moved to ({x},{y}) and {button}-clicked ({move[0]})"
            )}]
    return [{"type": "text", "text": f"mouse_move_click failed: {err}"}]


_MAX_EVAL_OUTPUT = 20000


def _handle_steam_ui_targets() -> list[dict]:
    try:
        targets = _cef_targets()
    except (urllib.error.URLError, OSError) as exc:
        return [{"type": "text", "text": (
            f"Steam CEF debugger not reachable on {CEF_HOST}:{CEF_PORT} ({exc}). "
            "It should be enabled whenever Decky Loader is running."
        )}]
    summary = [
        {"title": t.get("title"), "type": t.get("type"), "url": t.get("url")}
        for t in targets
    ]
    return [{"type": "text", "text": json.dumps(summary, indent=2)}]


def _handle_steam_ui_eval(expression: str, target: str) -> list[dict]:
    try:
        msg = _cef_eval(expression, target)
    except (urllib.error.URLError, OSError, ConnectionError) as exc:
        return [{"type": "text", "text": (
            f"Steam CEF debugger not reachable on {CEF_HOST}:{CEF_PORT} ({exc}). "
            "It should be enabled whenever Decky Loader is running."
        )}]
    except LookupError as exc:
        return [{"type": "text", "text": str(exc)}]

    result = msg.get("result", {})
    if "exceptionDetails" in result:
        detail = result["exceptionDetails"]
        text = detail.get("exception", {}).get("description") or json.dumps(detail)
        return [{"type": "text", "text": f"JS exception: {text[:_MAX_EVAL_OUTPUT]}"}]

    value = result.get("result", {})
    out = json.dumps(value.get("value"), indent=2, default=str) \
        if "value" in value else json.dumps(value, indent=2)
    if len(out) > _MAX_EVAL_OUTPUT:
        out = out[:_MAX_EVAL_OUTPUT] + "\n… (truncated — narrow your expression)"
    return [{"type": "text", "text": out}]


class _ToolError(Exception):
    """Unknown tool or unusable arguments — the caller's mistake, reported back
    as an error result so the model can correct itself and retry."""


def _require_str(tool: str, args: dict, name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value:
        raise _ToolError(
            f"{tool}: argument {name!r} must be a non-empty string (got {value!r})"
        )
    return value


def _require_int(tool: str, args: dict, name: str) -> int:
    value = args.get(name)
    # bool is an int subclass and never a sensible coordinate.
    if not isinstance(value, bool):
        try:
            return int(value)  # models routinely send "640" rather than 640
        except (TypeError, ValueError):
            pass
    raise _ToolError(f"{tool}: argument {name!r} must be an integer (got {value!r})")


def _dispatch(name: str, args: dict) -> list[dict]:
    if name == "steam_ui_targets":
        return _handle_steam_ui_targets()
    if name == "steam_ui_eval":
        target = args.get("target") or "SharedJSContext"
        if not isinstance(target, str):
            raise _ToolError(f"{name}: argument 'target' must be a string (got {target!r})")
        return _handle_steam_ui_eval(_require_str(name, args, "expression"), target)
    if name == "screenshot":
        return _handle_screenshot()
    if name == "send_key":
        return _handle_send_key(_require_str(name, args, "key"))
    if name == "type_text":
        return _handle_type_text(_require_str(name, args, "text"))
    if name == "mouse_move_click":
        return _handle_mouse_move_click(
            _require_int(name, args, "x"),
            _require_int(name, args, "y"),
            args.get("button", "left"),
        )
    raise _ToolError(f"Unknown tool: {name!r}")

# ── JSON-RPC / MCP transport ───────────────────────────────────────────────────

def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _handle(msg: dict) -> None:
    method = msg.get("method", "")
    msg_id = msg.get("id")

    if method == "initialize":
        _send({
            "jsonrpc": "2.0", "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "decky-claude", "version": "1.0.0"},
                "capabilities": {"tools": {}},
            },
        })
    elif method in ("notifications/initialized",):
        pass  # no response for notifications
    elif method == "tools/list":
        _send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = msg.get("params", {})
        name = params.get("name", "")
        args = params.get("arguments") or {}
        is_error = False
        if not isinstance(args, dict):
            content = [{"type": "text", "text": f"{name}: 'arguments' must be an object"}]
            is_error = True
        else:
            try:
                content = _dispatch(name, args)
            except _ToolError as exc:
                content = [{"type": "text", "text": str(exc)}]
                is_error = True
            except Exception as exc:
                # A handler that raises still has to answer: the client blocks
                # on this id until the server exits, so an unhandled exception
                # here hangs the whole session rather than failing one tool.
                sys.stderr.write(f"mcp_server: {name} raised\n{traceback.format_exc()}")
                content = [{"type": "text", "text": (
                    f"{name} failed: {type(exc).__name__}: {exc}"
                )}]
                is_error = True
        _send({
            "jsonrpc": "2.0", "id": msg_id,
            "result": {"content": content, "isError": is_error},
        })
    elif msg_id is not None:
        _send({
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": f"Unknown method: {method}"},
        })


def main() -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue  # a line that will not parse carries no id to answer on
        try:
            _handle(msg)
        except Exception as exc:
            sys.stderr.write(f"mcp_server error: {traceback.format_exc()}")
            # Anything with an id is a request the client is still waiting on.
            msg_id = msg.get("id") if isinstance(msg, dict) else None
            if msg_id is not None:
                _send({
                    "jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32603, "message": f"Internal error: {exc}"},
                })


if __name__ == "__main__":
    main()
