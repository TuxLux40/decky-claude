#!/usr/bin/env python3
"""
Minimal stdio MCP server exposing screen capture and input tools for Steam Deck.
No external dependencies — pure Python stdlib + grim/xdotool/ydotool binaries.
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
import urllib.error
import urllib.request

# Steam's CEF remote debugger (Decky Loader keeps it enabled)
CEF_HOST = os.environ.get("DECKY_CLAUDE_CEF_HOST", "127.0.0.1")
CEF_PORT = int(os.environ.get("DECKY_CLAUDE_CEF_PORT", "8080"))

# ── display environment ────────────────────────────────────────────────────────

def _display_env() -> dict[str, str]:
    env = dict(os.environ)
    # uid 1000 is the SteamOS default but not universal — derive the runtime
    # dir from the environment or the current user rather than hardcoding it.
    runtime_dir = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    env["XDG_RUNTIME_DIR"] = runtime_dir
    for wd in ["wayland-0", "wayland-1", "wayland-2"]:
        if os.path.exists(os.path.join(runtime_dir, wd)):
            env.setdefault("WAYLAND_DISPLAY", wd)
            break
    env.setdefault("DISPLAY", ":0")
    # Needed by the desktop-session screenshot fallbacks (spectacle talks to
    # the compositor over the session bus, not over a Wayland protocol).
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")
    # Gaming Mode's socket is gamescope-N, which the wayland-N probe above
    # never matches; gamescopectl reads this to find the control protocol.
    for gs in ("gamescope-0", "gamescope-1"):
        if os.path.exists(os.path.join(runtime_dir, gs)):
            env.setdefault("GAMESCOPE_WAYLAND_DISPLAY", gs)
            env.setdefault("WAYLAND_DISPLAY", gs)
            break
    return env

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

_KEY_MAP = {
    "escape": "KEY_ESC",
    "Return": "KEY_ENTER",
    "space": "KEY_SPACE",
    "Tab": "KEY_TAB",
}


def SCREENSHOT_COMMANDS(path: str, env: dict[str, str]) -> list[tuple[list[str], float]]:
    """Capture commands to try, in order, with a per-command timeout.

    Gaming Mode comes first, because that is what this plugin is for. Neither
    gamescope nor KWin implements a Wayland screencopy protocol, so grim works
    in neither of them and each session needs its own native path: gamescopectl
    speaks gamescope's control protocol, spectacle drives KWin's D-Bus
    interface (a Qt app, hence the much longer leash). grim still covers plain
    wlroots compositors, and scrot/import cover X11.
    """
    commands = []
    # Only when a gamescope socket was actually found: outside Gaming Mode
    # gamescopectl still exits 0 after failing to connect, so an ungated attempt
    # would just burn the file-existence check on every desktop capture.
    if env.get("GAMESCOPE_WAYLAND_DISPLAY"):
        commands.append((["gamescopectl", "screenshot", path], 15))
    return commands + [
        (["grim", path], 10),
        (["spectacle", "-f", "-b", "-n", "-o", path], 25),
        (["scrot", "-o", path], 10),
        (["import", "-window", "root", path], 10),
    ]


SCREENSHOT_UNAVAILABLE = (
    "Screenshot failed — no working capture tool "
    "(tried gamescopectl / grim / spectacle / scrot / import)."
)


def _handle_screenshot() -> list[dict]:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        path = f.name

    captured = False
    for cmd, timeout in SCREENSHOT_COMMANDS(path, _display_env()):
        rc, _ = _run(cmd, timeout=timeout)
        if rc == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
            captured = True
            break

    if not captured:
        if os.path.exists(path):
            os.unlink(path)
        return [{"type": "text", "text": SCREENSHOT_UNAVAILABLE}]

    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode()
    os.unlink(path)
    return [{"type": "image", "data": data, "mimeType": "image/png"}]


def _handle_send_key(key: str) -> list[dict]:
    rc, err = _run(["xdotool", "key", "--clearmodifiers", key])
    if rc == 0:
        return [{"type": "text", "text": f"Key sent: {key}"}]
    ydokey = _KEY_MAP.get(key, f"KEY_{key.upper()}")
    rc, err = _run(["ydotool", "key", ydokey])
    if rc == 0:
        return [{"type": "text", "text": f"Key sent: {key} (ydotool)"}]
    return [{"type": "text", "text": f"send_key failed for {key!r}: {err}"}]


def _handle_type_text(text: str) -> list[dict]:
    rc, err = _run(["xdotool", "type", "--clearmodifiers", "--delay", "20", "--", text])
    if rc == 0:
        return [{"type": "text", "text": f"Typed: {text!r}"}]
    rc, err = _run(["ydotool", "type", "--", text])
    if rc == 0:
        return [{"type": "text", "text": f"Typed: {text!r} (ydotool)"}]
    return [{"type": "text", "text": f"type_text failed: {err}"}]


def _handle_mouse_move_click(x: int, y: int, button: str = "left") -> list[dict]:
    btn_num = {"left": "1", "middle": "2", "right": "3"}.get(button, "1")
    rc, err = _run(["xdotool", "mousemove", "--sync", str(x), str(y)])
    if rc == 0:
        rc2, err2 = _run(["xdotool", "click", btn_num])
        if rc2 == 0:
            return [{"type": "text", "text": f"Moved to ({x},{y}) and {button}-clicked"}]

    # ydotool fallback
    _run(["ydotool", "mousemove", "--", str(x), str(y)])
    btn_code = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}.get(button, "0xC0")
    rc, err = _run(["ydotool", "click", btn_code])
    if rc == 0:
        return [{"type": "text", "text": f"Moved to ({x},{y}) and {button}-clicked (ydotool)"}]
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


def _dispatch(name: str, args: dict) -> list[dict]:
    if name == "steam_ui_targets":
        return _handle_steam_ui_targets()
    if name == "steam_ui_eval":
        return _handle_steam_ui_eval(
            args.get("expression", ""), args.get("target", "SharedJSContext")
        )
    if name == "screenshot":
        return _handle_screenshot()
    if name == "send_key":
        return _handle_send_key(args.get("key", ""))
    if name == "type_text":
        return _handle_type_text(args.get("text", ""))
    if name == "mouse_move_click":
        return _handle_mouse_move_click(args["x"], args["y"], args.get("button", "left"))
    return [{"type": "text", "text": f"Unknown tool: {name}"}]

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
        content = _dispatch(params.get("name", ""), params.get("arguments", {}))
        _send({"jsonrpc": "2.0", "id": msg_id, "result": {"content": content, "isError": False}})
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
            _handle(json.loads(raw))
        except json.JSONDecodeError:
            pass
        except Exception as exc:
            sys.stderr.write(f"mcp_server error: {exc}\n")


if __name__ == "__main__":
    main()
