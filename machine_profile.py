#!/usr/bin/env python3
"""Describe the machine this plugin is running on, for the session's CLAUDE.md.

Without this, a session starts from the bundled skill's assumptions — Steam
Deck, SteamOS, immutable rootfs, a `deck` user — and those are wrong on every
non-Deck install. Acting on them wastes turns at best (paths that don't exist)
and gives dangerous advice at worst (`steamos-readonly disable` on a normal
distro). So the profile leads with what the hardware actually is.

Stdlib only, like the rest of the plugin. Every probe fails soft: a machine
missing lspci or dmi still produces a useful profile.

Run it directly to see what a session would be told:

    python3 machine_profile.py
"""
import glob
import json
import os
import pwd
import re
import subprocess
import sys

# Steam ships these DMI product names; nothing else identifies a Deck reliably.
_DECK_PRODUCTS = {"jupiter": "Steam Deck (LCD)", "galileo": "Steam Deck (OLED)"}

# Relative to the desktop user's home, which is NOT necessarily $HOME: the
# Decky backend inherits HOME=/root from the root-launched loader service, so
# expanduser("~") would look for Steam in root's home and find nothing.
_STEAM_ROOTS = [
    ".local/share/Steam",
    ".steam/steam",
    ".var/app/com.valvesoftware.Steam/data/Steam",
]


def _read(path: str, limit: int = 4096) -> str:
    try:
        with open(os.path.expanduser(path), "r", errors="replace") as f:
            return f.read(limit).strip()
    except OSError:
        return ""


def _cmd(argv: list[str], timeout: float = 5) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _os_release() -> dict[str, str]:
    out = {}
    for line in _read("/etc/os-release").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v.strip().strip('"')
    return out


def hardware() -> dict:
    product = _read("/sys/class/dmi/id/product_name")
    deck = _DECK_PRODUCTS.get(product.strip().lower())

    cpu = ""
    for line in _read("/proc/cpuinfo", 8192).splitlines():
        if line.startswith("model name"):
            cpu = line.partition(":")[2].strip()
            break

    ram_gb = None
    for line in _read("/proc/meminfo", 512).splitlines():
        if line.startswith("MemTotal"):
            kb = re.search(r"(\d+)", line)
            if kb:
                ram_gb = round(int(kb.group(1)) / 1048576)
            break

    gpus = []
    for line in _cmd(["lspci", "-nn"]).splitlines():
        if re.search(r"\b(VGA|3D controller|Display controller)\b", line):
            # Strip the bus address and class prefix; keep the device name.
            gpus.append(re.sub(r"^\S+\s+[^:]+:\s*", "", line).strip())
    if not gpus:
        for card in sorted(glob.glob("/sys/class/drm/card?/device/uevent")):
            driver = re.search(r"DRIVER=(\S+)", _read(card))
            if driver:
                gpus.append(f"{driver.group(1)} (from drm uevent)")

    return {
        "is_steam_deck": bool(deck),
        "model": deck or f"{_read('/sys/class/dmi/id/sys_vendor')} {product}".strip()
        or "unknown",
        "cpu": cpu,
        "ram_gb": ram_gb,
        "gpus": gpus,
    }


def operating_system() -> dict:
    rel = _os_release()
    # SteamOS's rootfs is read-only and needs steamos-readonly to change; on any
    # normal distro that command does not exist and the advice is actively wrong.
    root_opts = ""
    for line in _read("/proc/mounts", 65536).splitlines():
        parts = line.split()
        if len(parts) > 3 and parts[1] == "/":
            root_opts = parts[3]
            break
    return {
        "distro": rel.get("PRETTY_NAME") or rel.get("NAME") or "unknown",
        "id": rel.get("ID", ""),
        "is_steamos": rel.get("ID", "").startswith("steamos"),
        "kernel": os.uname().release,
        "rootfs_readonly": root_opts.startswith("ro"),
    }


def session(home: str, uid: int) -> dict:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    gamescope = next(
        (s for s in ("gamescope-0", "gamescope-1")
         if os.path.exists(os.path.join(runtime, s))),
        None,
    )
    return {
        # Screen capture and input only work the Gaming Mode way; the panel says
        # so too, but a session should not have to discover that by failing.
        "gaming_mode": bool(gamescope),
        "gamescope_socket": gamescope,
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
        "user": _user_name(uid),
        "home": home,
    }


def _vdf_pairs(text: str) -> list[tuple[str, str]]:
    """All "key" "value" pairs in a VDF/ACF file, in order. Good enough for the
    flat lookups here — no nesting is needed."""
    return re.findall(r'"([^"]+)"\s+"([^"]*)"', text)


def steam(home: str) -> dict:
    root = next(
        (os.path.join(home, p) for p in _STEAM_ROOTS
         if os.path.isdir(os.path.join(home, p))),
        None,
    )
    if not root:
        return {"installed": False}

    libraries = []
    for _, value in _vdf_pairs(_read(os.path.join(root, "steamapps", "libraryfolders.vdf"), 65536)):
        if value.startswith("/") and os.path.isdir(value) and value not in libraries:
            libraries.append(value)
    if root not in libraries:
        libraries.insert(0, root)

    games = []
    for lib in libraries:
        for acf in glob.glob(os.path.join(lib, "steamapps", "appmanifest_*.acf")):
            fields = dict(_vdf_pairs(_read(acf, 8192)))
            if not fields.get("name"):
                continue
            try:
                size_gb = round(int(fields.get("SizeOnDisk", 0)) / 1073741824, 1)
            except ValueError:
                size_gb = 0.0
            games.append({
                "appid": fields.get("appid", ""),
                "name": fields["name"],
                "size_gb": size_gb,
                "library": lib,
            })
    games.sort(key=lambda g: g["size_gb"], reverse=True)

    free = {}
    for lib in libraries:
        try:
            st = os.statvfs(lib)
            free[lib] = round(st.f_bavail * st.f_frsize / 1073741824, 1)
        except OSError:
            pass

    compat = sorted(
        os.path.basename(p)
        for p in glob.glob(os.path.join(root, "compatibilitytools.d", "*"))
        if os.path.isdir(p)
    )
    compat += sorted(
        os.path.basename(p)
        for lib in libraries
        for p in glob.glob(os.path.join(lib, "steamapps", "common", "Proton*"))
        if os.path.isdir(p)
    )

    shortcuts = len(glob.glob(os.path.join(root, "userdata", "*", "config", "shortcuts.vdf")))

    return {
        "installed": True,
        "root": root,
        "libraries": libraries,
        "free_gb_by_library": free,
        "game_count": len(games),
        "games": games,
        "compat_tools": compat,
        "has_non_steam_shortcuts": bool(shortcuts),
    }


def decky(home: str) -> dict:
    plugins = sorted(
        os.path.basename(p)
        for p in glob.glob(os.path.join(home, "homebrew", "plugins", "*"))
        if os.path.isdir(p)
    )
    return {"plugins": plugins}


def _user_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def collect(home: str | None = None, uid: int | None = None) -> dict:
    """Profile the machine. `home`/`uid` identify the *desktop* user; the
    caller knows who that is (main.py resolves it), and it is not always the
    user this process runs as, nor whoever $HOME points at."""
    uid = os.getuid() if uid is None else uid
    if home is None:
        try:
            home = pwd.getpwuid(uid).pw_dir
        except KeyError:
            home = os.path.expanduser("~")
    return {
        "hardware": hardware(),
        "os": operating_system(),
        "session": session(home, uid),
        "steam": steam(home),
        "decky": decky(home),
    }


def render(profile: dict, max_games: int = 15) -> str:
    hw, os_, se, st = (profile[k] for k in ("hardware", "os", "session", "steam"))
    lines = ["## This machine", ""]

    if hw["is_steam_deck"]:
        lines.append(f"- **{hw['model']}** — the bundled skill's Deck assumptions hold.")
    else:
        lines.append(
            f"- **{hw['model']} — NOT a Steam Deck.** Ignore any Deck-specific "
            "assumption in the steam-debugger skill: there is no `deck` user, "
            "no `steamos-readonly`, and hardware quirks documented for the Deck "
            "do not apply."
        )
    specs = [s for s in (hw["cpu"], f"{hw['ram_gb']} GB RAM" if hw["ram_gb"] else "") if s]
    if specs:
        lines.append(f"- {' · '.join(specs)}")
    for gpu in hw["gpus"]:
        lines.append(f"- GPU: {gpu}")

    lines += [
        f"- {os_['distro']}, kernel {os_['kernel']}, rootfs "
        + ("read-only (SteamOS-style)" if os_["rootfs_readonly"] else "writable"),
        f"- Running as `{se['user']}`, home `{se['home']}`",
    ]
    if se["gaming_mode"]:
        lines.append(
            f"- At session start: Gaming Mode (gamescope, `{se['gamescope_socket']}`) — "
            "screenshots and input worked then. The user can switch modes at any "
            "time; call `session_context` for the live state."
        )
    else:
        lines.append(
            f"- At session start: **not in Gaming Mode** (desktop session"
            f"{': ' + se['desktop'] if se['desktop'] else ''}). `screenshot` and "
            "input refuse outside Gaming Mode (capture goes through gamescope). "
            "The user may switch later — call `session_context` for the live state."
        )

    if st.get("installed"):
        lines += ["", "### Steam", "", f"- Install root: `{st['root']}`"]
        for lib in st["libraries"]:
            free = st["free_gb_by_library"].get(lib)
            lines.append(f"- Library `{lib}`" + (f" — {free} GB free" if free is not None else ""))
        if st["compat_tools"]:
            lines.append(f"- Compatibility tools: {', '.join(sorted(set(st['compat_tools'])))}")
        if st["has_non_steam_shortcuts"]:
            lines.append("- Has non-Steam shortcuts (games added manually).")
        if st["games"]:
            lines += ["", f"### Installed games ({st['game_count']}, largest first)", ""]
            for g in st["games"][:max_games]:
                lines.append(f"- {g['name']} (appid {g['appid']}, {g['size_gb']} GB)")
            if st["game_count"] > max_games:
                lines.append(f"- … and {st['game_count'] - max_games} more")
    else:
        lines += ["", "- Steam install not found in the usual locations."]

    plugins = profile["decky"]["plugins"]
    if plugins:
        lines += [
            "",
            "### Decky plugins installed",
            "",
            ", ".join(plugins),
            "",
            "Another plugin is a plausible cause of Steam UI misbehaviour — "
            "consider them when the UI itself is the problem.",
        ]
    return "\n".join(lines)


if __name__ == "__main__":
    data = collect()
    if "--json" in sys.argv:
        print(json.dumps(data, indent=2))
    else:
        print(render(data))
