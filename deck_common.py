"""Behaviour shared by the Decky backend (main.py) and the MCP server
(mcp_server.py).

Both processes drive the same Gaming Mode session, so the display environment
they hand to child processes and the xdotool/ydotool commands they run have to
agree. Both once carried their own copy and the copies drifted: the ydotool key
map listed F1-F12 on one side and four keys on the other, so `send_key("F5")`
behaved differently depending on whether the panel or Claude sent it.

Stdlib only, and importable as a plain sibling module: mcp_server.py is spawned
by `claude` as a bare `python3` child with no virtualenv.
"""

import os

# ── display environment ────────────────────────────────────────────────────────


def apply_display_env(env: dict[str, str], uid: int | None = None) -> dict[str, str]:
    """Point `env` at the compositor of the running session (mutates and returns it).

    `uid` owns the XDG runtime dir; uid 1000 is the SteamOS default but not
    universal, so callers pass the user they resolved instead of hardcoding it.
    """
    runtime_dir = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid() if uid is None else uid}"
    env["XDG_RUNTIME_DIR"] = runtime_dir

    # Gaming Mode is a gamescope session: its socket is gamescope-N, which a
    # wayland-N probe never matches. gamescopectl reads
    # GAMESCOPE_WAYLAND_DISPLAY to reach the control protocol.
    gamescope_socket = next(
        (s for s in ("gamescope-0", "gamescope-1")
         if os.path.exists(os.path.join(runtime_dir, s))),
        None,
    )
    if gamescope_socket:
        env.setdefault("GAMESCOPE_WAYLAND_DISPLAY", gamescope_socket)
        env["WAYLAND_DISPLAY"] = gamescope_socket
    else:
        for wd in ("wayland-0", "wayland-1", "wayland-2"):
            if os.path.exists(os.path.join(runtime_dir, wd)):
                env.setdefault("WAYLAND_DISPLAY", wd)
                break
    env.setdefault("DISPLAY", ":0")
    return env


# ── input ──────────────────────────────────────────────────────────────────────

# xdotool speaks X keysym names, ydotool speaks linux/input-event-codes.h. Only
# the names that do not simply uppercase into a KEY_* constant need an entry —
# F1-F12, letters and digits already round-trip through the default below,
# which is why this table is short. Lookup is case-insensitive so both "escape"
# and xdotool's own "Escape" resolve.
KEY_MAP: dict[str, str] = {
    "escape": "KEY_ESC",
    "esc": "KEY_ESC",
    "return": "KEY_ENTER",
    "kp_enter": "KEY_KPENTER",
    "backspace": "KEY_BACKSPACE",
    "prior": "KEY_PAGEUP",
    "page_up": "KEY_PAGEUP",
    "next": "KEY_PAGEDOWN",
    "page_down": "KEY_PAGEDOWN",
    "print": "KEY_SYSRQ",
    "menu": "KEY_COMPOSE",
    "control_l": "KEY_LEFTCTRL",
    "control_r": "KEY_RIGHTCTRL",
    "alt_l": "KEY_LEFTALT",
    "alt_r": "KEY_RIGHTALT",
    "shift_l": "KEY_LEFTSHIFT",
    "shift_r": "KEY_RIGHTSHIFT",
    "super_l": "KEY_LEFTMETA",
    "super_r": "KEY_RIGHTMETA",
}

# xdotool numbers buttons the X way; ydotool wants the raw BTN_* codes with the
# 0x40 "click" bit set.
_XDOTOOL_BUTTONS = {"left": "1", "middle": "2", "right": "3"}
_YDOTOOL_BUTTONS = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}
_BUTTON_NAMES = {1: "left", 2: "middle", 3: "right"}


def ydotool_key(key: str) -> str:
    return KEY_MAP.get(key.lower(), f"KEY_{key.upper()}")


def normalize_button(button: str | int) -> str:
    """Accept either the panel's X button number or the MCP tool's name."""
    if isinstance(button, int):
        return _BUTTON_NAMES.get(button, "left")
    return button if button in _XDOTOOL_BUTTONS else "left"


def key_commands(key: str) -> list[list[str]]:
    """xdotool first, ydotool second — try each until one exits 0.

    ydotool only works with a running ydotoold and access to /dev/uinput, so it
    is the fallback for when there is no X server (or XWayland) to talk to.

    ydotool's `key` subcommand takes `<code>:<pressed>` pairs, not a bare key
    name — a bare name is a "non-interpretable value" that ydotool silently
    treats as a no-op delay while still exiting 0, so this must send an
    explicit press (":1") then release (":0") or nothing happens at all.
    """
    code = ydotool_key(key)
    return [
        ["xdotool", "key", "--clearmodifiers", "--", key],
        ["ydotool", "key", "--", f"{code}:1", f"{code}:0"],
    ]


def type_commands(text: str) -> list[list[str]]:
    return [
        ["xdotool", "type", "--clearmodifiers", "--delay", "20", "--", text],
        ["ydotool", "type", "--", text],
    ]


def mouse_move_commands(x: int, y: int) -> list[list[str]]:
    return [
        ["xdotool", "mousemove", "--sync", "--", str(x), str(y)],
        ["ydotool", "mousemove", "--", str(x), str(y)],
    ]


def click_commands(button: str | int = "left") -> list[list[str]]:
    name = normalize_button(button)
    return [
        ["xdotool", "click", _XDOTOOL_BUTTONS[name]],
        ["ydotool", "click", _YDOTOOL_BUTTONS[name]],
    ]
