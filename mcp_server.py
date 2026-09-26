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
        "name": "session_context",
        "description": (
            "Live, read-only check of where this session is running. Returns "
            "JSON: (1) display mode — 'gaming' (a gamescope process is running; "
            "screenshot and input tools work) or 'desktop' (they do not; use "
            "steam_ui_eval/steam_snippet), with the evidence; (2) whether this "
            "claude process descends from Decky's PluginLoader — if so, "
            "restarting plugin_loader.service kills this session instantly with "
            "no auto-resume. Both can change mid-session, so call this before "
            "screenshot/send_key/type_text/mouse_move_click and before "
            "restarting plugin_loader.service, instead of assuming."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "screenshot",
        "description": (
            "Capture the current screen (game, UI, error dialog, desktop). "
            "Call this at the start of any request that involves the game or "
            "visual state before answering. Returns a PNG image. Gaming Mode "
            "only — check session_context if unsure."
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


def _register_snippet_tool() -> None:
    """Declared after SNIPPETS exists so the catalog is in the description —
    the model should be able to pick a snippet without a second round trip."""
    TOOLS.append({
        "name": "steam_snippet",
        "description": (
            "Run a curated, pre-verified SteamClient query for a common Steam "
            "problem. Prefer this over hand-writing steam_ui_eval JavaScript: "
            "several namespaces (Downloads above all) expose no getters, only "
            "RegisterFor* callbacks, so the obvious expression returns nothing. "
            "These snippets already handle that and return structured JSON.\n\n"
            "Snippets marked [ACTION] change client state; everything else is "
            "read-only.\n\n" + _snippet_catalog()
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": list(SNIPPETS)},
            },
            "required": ["name"],
        },
    })

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


# ── session context (live "where am I running" probe) ──────────────────────────

# Process names (/proc/<pid>/comm, truncated to 15 chars by the kernel) that
# mean a Gaming Mode compositor is up. Newer gamescope-session builds run the
# Wayland-only binary as gamescope-wl.
_GAMESCOPE_COMMS = {"gamescope", "gamescope-wl"}
# Desktop compositors / shells, reported only as evidence for Desktop Mode.
_DESKTOP_COMMS = {
    "kwin_wayland": "KDE Plasma (Wayland)", "kwin_x11": "KDE Plasma (X11)",
    "plasmashell": "KDE Plasma", "gnome-shell": "GNOME", "Hyprland": "Hyprland",
    "sway": "sway", "labwc": "labwc", "weston": "weston", "cosmic-comp": "COSMIC",
}
# Decky Loader's process (a PyInstaller binary). Plugin backends, and every
# claude they spawn, descend from it.
_LOADER_COMMS = {"PluginLoader", "Decky Loader"}
_LOADER_UNIT = "plugin_loader.service"


def _proc_stat(pid: int) -> tuple[str, int] | None:
    """(comm, ppid) for a pid, or None if it is gone / unreadable."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read().decode(errors="replace")
    except OSError:
        return None
    # comm is parenthesised and may itself contain spaces or ')'.
    lpar, rpar = raw.find("("), raw.rfind(")")
    if lpar < 0 or rpar < 0:
        return None
    rest = raw[rpar + 2:].split()
    try:
        return raw[lpar + 1:rpar], int(rest[1])
    except (IndexError, ValueError):
        return None


def _proc_uid(pid: int) -> int | None:
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return None


def _proc_unit(pid: int) -> str | None:
    """The systemd unit a pid's cgroup belongs to (last *.service / *.scope)."""
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            path = f.read().strip().splitlines()[-1].split(":", 2)[-1]
    except (OSError, IndexError):
        return None
    units = [p for p in path.split("/") if p.endswith((".service", ".scope"))]
    return units[-1] if units else None


def _find_procs(comms, uid: int | None = None) -> list[dict]:
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        st = _proc_stat(pid)
        if not st or st[0] not in comms:
            continue
        if uid is not None and _proc_uid(pid) != uid:
            continue
        found.append({"pid": pid, "name": st[0]})
    return found


def _ancestors(pid: int, limit: int = 64) -> list[dict]:
    chain = []
    while pid > 0 and len(chain) < limit:
        st = _proc_stat(pid)
        if not st:
            break
        chain.append({"pid": pid, "name": st[0]})
        pid = st[1]
    return chain


def session_context() -> dict:
    """Live answer to "which mode is the machine in, and would restarting
    Decky Loader kill this session?" — both can change mid-session, so this is
    probed on every call rather than baked into the prompt."""
    uid = os.getuid()
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    sockets = sorted(
        s for s in os.listdir(runtime)
        if s.startswith(("gamescope-", "wayland-")) and not s.endswith(".lock")
    ) if os.path.isdir(runtime) else []

    gamescope = _find_procs(_GAMESCOPE_COMMS, uid)
    desktop = _find_procs(_DESKTOP_COMMS, uid)
    # A gamescope socket can outlive its compositor, so the process is the
    # deciding signal; the socket is corroborating evidence only.
    gaming = bool(gamescope)
    display = {
        "mode": "gaming" if gaming else "desktop",
        "gamescope_processes": gamescope,
        "desktop_processes": [
            {**p, "desktop": _DESKTOP_COMMS[p["name"]]} for p in desktop
        ],
        "runtime_sockets": sockets,
        "screenshot_and_input_tools_available": "yes" if gaming else "no",
        "consequence": (
            "screenshot/send_key/type_text/mouse_move_click work (gamescope is running)."
            if gaming else
            "screenshot/send_key/type_text/mouse_move_click will NOT work — "
            "no gamescope process, this is Desktop Mode. Use steam_ui_eval / "
            "steam_snippet instead, or ask the user to switch to Gaming Mode."
        ),
    }

    # The MCP server is spawned by claude, so our own ancestry is claude's
    # ancestry plus one hop.
    chain = _ancestors(os.getpid())
    loader = next((p for p in chain if p["name"] in _LOADER_COMMS), None)
    claude = next((p for p in chain[1:] if p["name"] == "claude"), None)
    unit = _proc_unit(claude["pid"] if claude else os.getpid())
    inside = bool(loader) or unit == _LOADER_UNIT
    origin = {
        "inside_plugin": inside,
        "claude_pid": claude["pid"] if claude else None,
        "loader_pid": loader["pid"] if loader else None,
        "systemd_unit": unit,
        "ancestor_chain": " <- ".join(f"{p['name']}({p['pid']})" for p in chain),
        "restarting_plugin_loader_kills_this_session": "yes" if inside else "no",
        "consequence": (
            "This session was launched by the decky-claude plugin. Restarting "
            "plugin_loader.service (or killing PluginLoader) kills this session "
            "instantly and it does NOT auto-resume — warn the user and make it "
            "the very last step, or have them run it themselves."
            if inside else
            "This session is standalone (not under PluginLoader). Restarting "
            "plugin_loader.service is safe for this session; only the plugin's "
            "own sessions and the MCP tools' CEF connection are briefly affected."
        ),
    }
    return {"display": display, "session_origin": origin}


def _handle_session_context() -> list[dict]:
    return [{"type": "text", "text": json.dumps(session_context(), indent=2, ensure_ascii=False)}]


_DESKTOP_HINT = (
    " Call session_context to check the live display mode — screen capture and "
    "input only work in Gaming Mode (gamescope); in Desktop Mode use "
    "steam_ui_eval / steam_snippet instead."
)


def _handle_screenshot() -> list[dict]:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        path = f.name

    failure = _gamescope_screenshot(path)
    if failure:
        if os.path.exists(path):
            os.unlink(path)
        return [{"type": "text", "text": f"Screenshot failed — {failure}.{_DESKTOP_HINT}"}]

    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode()
    os.unlink(path)
    return [{"type": "image", "data": data, "mimeType": "image/png"}]


def _input_hint() -> str:
    """Point a failed input call at session_context when Desktop Mode is the
    likely reason; in Gaming Mode the failure is something else."""
    try:
        gaming = bool(_find_procs(_GAMESCOPE_COMMS, os.getuid()))
    except OSError:
        gaming = True
    return "" if gaming else "." + _DESKTOP_HINT


def _handle_send_key(key: str) -> list[dict]:
    rc, err, tool = _run_first(deck_common.key_commands(key))
    if rc == 0:
        return [{"type": "text", "text": f"Key sent: {key} ({tool})"}]
    return [{"type": "text", "text": f"send_key failed for {key!r}: {err}{_input_hint()}"}]


def _handle_type_text(text: str) -> list[dict]:
    rc, err, tool = _run_first(deck_common.type_commands(text))
    if rc == 0:
        return [{"type": "text", "text": f"Typed: {text!r} ({tool})"}]
    return [{"type": "text", "text": f"type_text failed: {err}{_input_hint()}"}]


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
    return [{"type": "text", "text": f"mouse_move_click failed: {err}{_input_hint()}"}]


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


# ── curated SteamClient snippets ───────────────────────────────────────────────

# Several SteamClient namespaces expose no getters at all — Downloads, for
# instance, is only RegisterFor* callbacks. The registration fires once with the
# current state, so the read is "subscribe, take the first payload, unsubscribe"
# rather than a call. Callbacks vary in arity, hence the (...a) collection.
_JS_PREAMBLE = """
const once = (reg, ms = 2500) => new Promise(res => {
  let h;
  const done = v => { try { h && h.unregister && h.unregister(); } catch (e) {} res(v); };
  h = reg((...a) => done(a.length > 1 ? a : a[0]));
  setTimeout(() => done(null), ms);
});
const gb = n => Math.round((Number(n) || 0) / 1073741824 * 10) / 10;
"""

SNIPPETS: dict[str, dict] = {
    "downloads": {
        "summary": "Download queue: what is downloading, paused, queued or errored, and why it may be stuck.",
        "js": """
const ov = await once(cb => SteamClient.Downloads.RegisterForDownloadOverview(cb));
const di = await once(cb => SteamClient.Downloads.RegisterForDownloadItems(cb));
const items = (Array.isArray(di) ? di[1] : []) || [];
const flat = items.flatMap(g => (g.item_data || []).map(d => ({
  appid: d.appid, active: d.active, paused: d.paused, completed: d.completed,
  queue_index: d.queue_index, update_error: d.update_error || null,
  buildid: d.buildid, target_buildid: d.target_buildid,
  // A build id that already matches the target with an update still pending is
  // the classic "stuck at 100%" / needs-verification shape.
  stalled_reason:
    d.update_error ? 'update_error: ' + d.update_error
    : d.paused ? 'paused'
    : (d.queue_index === -1 && !d.active && !d.completed) ? 'deferred / not queued'
    : (d.active && ov && ov.update_network_bytes_per_second === 0) ? 'active but 0 B/s'
    : null,
})));
return ({
  overview: ov && {
    state: ov.update_state, appid: ov.update_appid,
    net_bytes_per_sec: ov.update_network_bytes_per_second,
    disk_bytes_per_sec: ov.update_disc_bytes_per_second,
    paused_by_user: !!(ov.update_state_flags & 1),
  },
  items: flat,
  stalled: flat.filter(d => d.stalled_reason),
});
""",
    },
    "login": {
        "summary": "Login and connection state: accounts, login stage, whether the client is actually talking to Steam, reconnect throttling.",
        "js": """
const users = await SteamClient.User.GetLoginUsers();
const cm = (typeof App !== 'undefined' && App.m_cm) || {};
const now = Date.now() / 1000;
return ({
  login_state: typeof App !== 'undefined' ? App.m_eLoginState : null,
  services_initialised: typeof App !== 'undefined' ? App.m_bServicesInitialized : null,
  connected_to_steam: typeof cm.BConnectedToServer === 'function' ? cm.BConnectedToServer() : cm.m_bConnected,
  connection_failed: !!cm.m_bConnectionFailed,
  completed_initial_connect: !!cm.m_bCompletedInitialConnect,
  // Non-zero means the client is deliberately waiting before retrying, which
  // looks identical to "offline" in the UI.
  reconnect_throttled_for_sec: cm.m_rtReconnectThrottleExpiration
    ? Math.max(0, Math.round(cm.m_rtReconnectThrottleExpiration - now)) : 0,
  ip_country: await SteamClient.User.GetIPCountry(),
  secure_computer: await SteamClient.Auth.IsSecureComputer(),
  accounts: (users || []).map(u => ({
    account: u.accountName, persona: u.personaName,
    remembered: u.rememberPassword, has_pin: u.hasPin,
  })),
});
""",
    },
    "library": {
        "summary": "Library folders with capacity and free space, plus the largest installed apps per folder — for disk-full and missing-game problems.",
        "js": """
const folders = await SteamClient.InstallFolder.GetInstallFolders();
return ((folders || []).map(f => ({
  index: f.nFolderIndex, path: f.strFolderPath, drive: f.strDriveName,
  mounted: f.bIsMounted, is_default: f.bIsDefaultFolder,
  capacity_gb: gb(f.nCapacity), free_gb: gb(f.nFreeSpace), used_gb: gb(f.nUsedSize),
  shader_cache_gb: gb(f.nShaderSize), staged_gb: gb(f.nStagedSize),
  app_count: (f.vecApps || []).length,
  largest_apps: (f.vecApps || [])
    .slice().sort((a, b) => (b.nUsedSize || 0) - (a.nUsedSize || 0)).slice(0, 8)
    .map(a => ({ appid: a.nAppID, name: a.strAppName, size_gb: gb(a.nUsedSize) })),
}))); 
""",
    },
    "running": {
        "summary": "Apps Steam currently considers running, with their install and update state.",
        "js": """
const ids = Array.from((typeof SteamUIStore !== 'undefined' && SteamUIStore.m_runningAppIDs) || []);
return ({
  running_appids: ids,
  apps: ids.map(id => {
    const o = appStore && appStore.GetAppOverviewByAppID
      ? appStore.GetAppOverviewByAppID(Number(id)) : null;
    return o ? { appid: o.appid, name: o.display_name, state: o.app_type } : { appid: id };
  }),
});
""",
    },
    "client_info": {
        "summary": "Steam client build/branch and OS branch — check before blaming a bug on the user.",
        "js": """
return ({
  os_branch: await SteamClient.Updates.GetCurrentOSBranch(),
  ui_mode: typeof SteamUIStore !== 'undefined' ? SteamUIStore.m_appDetailsDisplayMode : null,
  user_agent: navigator.userAgent,
});
""",
    },
    "refresh_library": {
        "summary": "ACTION: rescan install folders. Fixes a library that lost track of installed games.",
        "action": True,
        "js": """
await SteamClient.InstallFolder.RefreshFolders();
const folders = await SteamClient.InstallFolder.GetInstallFolders();
return ({ refreshed: true, folders: (folders || []).length });
""",
    },
    "check_updates": {
        "summary": "ACTION: ask Steam to check for a client update.",
        "action": True,
        "js": """
await SteamClient.Updates.CheckForUpdates();
return ({ requested: true });
""",
    },
}


def _snippet_catalog() -> str:
    return "\n".join(
        f"- {name}{' [ACTION]' if s.get('action') else ''}: {s['summary']}"
        for name, s in SNIPPETS.items()
    )


def _handle_steam_snippet(name: str) -> list[dict]:
    snippet = SNIPPETS.get(name)
    if not snippet:
        raise _ToolError(
            f"Unknown snippet {name!r}. Available: {', '.join(SNIPPETS)}"
        )
    return _handle_steam_ui_eval(
        f"(async () => {{{_JS_PREAMBLE}{snippet['js']}}})()", "SharedJSContext"
    )


_register_snippet_tool()


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
    if name == "steam_snippet":
        return _handle_steam_snippet(_require_str(name, args, "name"))
    if name == "session_context":
        return _handle_session_context()
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
