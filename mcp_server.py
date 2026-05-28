#!/usr/bin/env python3
"""
Minimal stdio MCP server exposing screen capture and input tools for Steam Deck.
No external dependencies — pure Python stdlib + grim/xdotool/ydotool binaries.
"""
import base64
import json
import os
import subprocess
import sys
import tempfile

# ── display environment ────────────────────────────────────────────────────────

def _display_env() -> dict[str, str]:
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = "/run/user/1000"
    for wd in ["wayland-0", "wayland-1", "wayland-2"]:
        if os.path.exists(f"/run/user/1000/{wd}"):
            env.setdefault("WAYLAND_DISPLAY", wd)
            break
    env.setdefault("DISPLAY", ":0")
    return env

def _run(cmd: list[str]) -> tuple[int, str]:
    env = _display_env()
    try:
        r = subprocess.run(
            cmd, env=env, capture_output=True, timeout=10,
            # If running as root, try to run display commands as the deck user
            **({"user": "deck"} if os.getuid() == 0 and cmd[0] in ("grim", "scrot", "import", "xdotool") else {}),
        )
        return r.returncode, r.stderr.decode(errors="replace").strip()
    except TypeError:
        # 'user' kwarg not available on older Python / this platform, retry without
        try:
            r = subprocess.run(cmd, env=env, capture_output=True, timeout=10)
            return r.returncode, r.stderr.decode(errors="replace").strip()
        except FileNotFoundError:
            return 1, f"{cmd[0]} not found"
        except subprocess.TimeoutExpired:
            return 1, "timeout"
    except FileNotFoundError:
        return 1, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 1, "timeout"

# ── tool definitions ───────────────────────────────────────────────────────────

TOOLS = [
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


def _handle_screenshot() -> list[dict]:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        path = f.name

    captured = False
    for cmd in [
        ["grim", path],
        ["scrot", path],
        ["import", "-window", "root", path],
    ]:
        rc, _ = _run(cmd)
        if rc == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
            captured = True
            break

    if not captured:
        if os.path.exists(path):
            os.unlink(path)
        return [{"type": "text", "text": "Screenshot failed — no tool available (grim / scrot / import)."}]

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


def _dispatch(name: str, args: dict) -> list[dict]:
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
