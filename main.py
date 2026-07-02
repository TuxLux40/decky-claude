import asyncio
import base64
import glob
import json
import logging
import os
import re

logger = logging.getLogger("decky-claude")

_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_URL_PATTERN = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9_-]+")

# Sentinel used to mark the block we inject into CLAUDE.md
_MD_START = "<!-- decky-claude-start -->"
_MD_END = "<!-- decky-claude-end -->"

# Directories scanned for the user's steam-debugger skill
_SKILL_BASES = [
    os.path.expanduser("~/.claude/skills"),
    "/home/deck/.claude/skills",
]


def _claude_md_block(skill_name: str | None) -> str:
    skill_section = ""
    if skill_name:
        skill_section = f"""
## Steam debugger skill — load it first

The user's `{skill_name}` skill is installed for this session
(`.claude/skills/{skill_name}`). BLOCKING REQUIREMENT: invoke the
`{skill_name}` skill via the Skill tool at the start of the session, before
doing any Steam or game debugging work, and follow its instructions.
"""
    return f"""\
{_MD_START}
# Steam Deck Gaming Mode — decky-claude session

You are running via the decky-claude Decky Loader plugin on a Steam Deck.
The user is in Gaming Mode and is messaging you from the Claude Android app.
{skill_section}
## MCP tools you have

- **screenshot** — Capture the current display (game, menu, error dialog).
  Returns a PNG image so you can see exactly what the user sees.
- **send_key** — Send a key press to the focused window
  (e.g. `escape`, `Return`, `space`, `Tab`, `F1`, `ctrl+c`).
- **type_text** — Type a string into the focused window.
- **mouse_move_click** — Move to (x, y) pixel coordinates and click.
  Steam Deck native resolution is 1280×800.

## Behaviour rules

1. **For any question about the game** (puzzles, mechanics, what's on screen,
   crashes, launch failures): call `screenshot` first, then answer based on
   what you see. Never guess — look first.
2. **For debugging**: take a screenshot to see the current visual state, read
   relevant log files (e.g. `~/.steam/logs/`, `~/.local/share/Steam/logs/`,
   Proton logs), and use the input tools to navigate dialogs or menus as needed.
3. **After sending input**: take another screenshot to confirm the result.
4. You have both the terminal view (files, logs, commands) and the visual view
   (screenshots). Use both together.
{_MD_END}
"""

_KEY_MAP: dict[str, str] = {
    "escape": "KEY_ESC",
    "Return": "KEY_ENTER",
    "space": "KEY_SPACE",
    "Tab": "KEY_TAB",
    "F1": "KEY_F1",
    "F2": "KEY_F2",
    "F3": "KEY_F3",
    "F4": "KEY_F4",
    "F5": "KEY_F5",
    "F12": "KEY_F12",
}


class Plugin:
    # ── session state ──────────────────────────────────────────────────────────
    _process: asyncio.subprocess.Process | None = None
    _session_url: str | None = None
    _status: str = "stopped"
    _working_dir: str = "/home/deck"
    _error_msg: str | None = None
    _log_lines: list[str] = []
    _skill_name: str | None = None
    _skill_link_created: str | None = None

    # ── screen state ───────────────────────────────────────────────────────────
    _last_thumb_b64: str | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────────

    async def _main(self):
        logger.info("decky-claude loaded")
        self._log_lines = []

    async def _unload(self):
        await self.stop_session()

    # ── session API ────────────────────────────────────────────────────────────

    async def start_session(self, working_dir: str = "/home/deck"):
        if self._process and self._process.returncode is None:
            return {"success": False, "error": "Session already running"}

        self._working_dir = working_dir
        self._session_url = None
        self._error_msg = None
        self._status = "starting"
        self._log_lines = []

        claude_bin = await self._find_claude()
        if not claude_bin:
            self._status = "error"
            self._error_msg = (
                "claude not found. Install via: npm install -g @anthropic-ai/claude-code"
            )
            return {"success": False, "error": self._error_msg}

        mcp_config = self._setup_working_dir(working_dir)

        env = {**os.environ, **self._display_env()}
        try:
            self._process = await asyncio.create_subprocess_exec(
                claude_bin, "--rc", "--mcp-config", mcp_config,
                cwd=working_dir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except Exception as exc:
            self._status = "error"
            self._error_msg = str(exc)
            return {"success": False, "error": self._error_msg}

        asyncio.ensure_future(self._drain_output())

        for _ in range(40):
            if self._session_url:
                self._status = "running"
                return {"success": True, "url": self._session_url}
            if self._process.returncode is not None:
                self._status = "error"
                self._error_msg = "Process exited before providing a session URL"
                return {"success": False, "error": self._error_msg}
            await asyncio.sleep(0.5)

        if self._process.returncode is None:
            self._status = "running"
            return {"success": True, "url": self._session_url}

        self._status = "error"
        self._error_msg = "Timed out waiting for session URL"
        return {"success": False, "error": self._error_msg}

    async def stop_session(self):
        if self._process:
            try:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    self._process.kill()
            except ProcessLookupError:
                pass
            self._process = None

        self._cleanup_working_dir(self._working_dir)
        self._session_url = None
        self._status = "stopped"
        self._error_msg = None
        return {"success": True}

    async def get_status(self):
        if self._process and self._process.returncode is not None:
            self._process = None
            self._session_url = None
            if self._status not in ("stopped", "error"):
                self._status = "stopped"
        return {
            "status": self._status,
            "url": self._session_url,
            "working_dir": self._working_dir,
            "error": self._error_msg,
            "skill": self._skill_name,
        }

    async def get_log(self):
        return {"lines": self._log_lines[-30:]}

    async def list_dirs(self):
        base = "/home/deck"
        dirs = [base]
        try:
            for entry in sorted(os.listdir(base)):
                path = os.path.join(base, entry)
                if os.path.isdir(path) and not entry.startswith("."):
                    dirs.append(path)
        except OSError:
            pass
        return {"dirs": dirs}

    # ── screenshot API (for panel preview only) ────────────────────────────────

    async def capture_screenshot(self):
        """Manual capture for the panel thumbnail — Claude uses the MCP tool instead."""
        out_path = "/tmp/decky-claude-preview.png"
        result = await self._take_screenshot(out_path)
        if result["success"]:
            self._last_thumb_b64 = await self._make_thumb(out_path)
        return {**result, "thumbnail": self._last_thumb_b64}

    async def get_screen_state(self):
        return {"thumbnail": self._last_thumb_b64}

    # ── input API (manual controls in the panel) ───────────────────────────────

    async def send_key(self, key: str):
        env = self._display_env()
        r = await self._run_cmd(["xdotool", "key", "--clearmodifiers", key], env)
        if r["success"]:
            return r
        ydokey = _KEY_MAP.get(key, f"KEY_{key.upper()}")
        return await self._run_cmd(["ydotool", "key", ydokey], env)

    async def send_text(self, text: str):
        env = self._display_env()
        r = await self._run_cmd(
            ["xdotool", "type", "--clearmodifiers", "--delay", "20", "--", text], env
        )
        if r["success"]:
            return r
        return await self._run_cmd(["ydotool", "type", "--", text], env)

    async def send_mouse_click(self, button: int = 1):
        env = self._display_env()
        r = await self._run_cmd(["xdotool", "click", str(button)], env)
        if r["success"]:
            return r
        btn_code = {1: "0xC0", 3: "0xC1", 2: "0xC2"}.get(button, "0xC0")
        return await self._run_cmd(["ydotool", "click", btn_code], env)

    # ── working-dir setup / teardown ───────────────────────────────────────────

    def _setup_working_dir(self, working_dir: str) -> str:
        """Write .mcp.json, link the steam-debugger skill, inject our CLAUDE.md
        block. Returns the path of the MCP config to pass via --mcp-config."""
        claude_dir = os.path.join(working_dir, ".claude")
        os.makedirs(claude_dir, exist_ok=True)

        # MCP server config — points to mcp_server.py next to this file.
        # Written to .mcp.json (project scope) and also passed explicitly via
        # --mcp-config so no trust prompt can block the headless session.
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        mcp_server = os.path.join(plugin_dir, "mcp_server.py")
        mcp_config_path = os.path.join(working_dir, ".mcp.json")

        existing_mcp: dict = {}
        if os.path.exists(mcp_config_path):
            try:
                with open(mcp_config_path) as f:
                    existing_mcp = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

        servers = existing_mcp.setdefault("mcpServers", {})
        servers["steamdeck"] = {
            "command": "python3",
            "args": [mcp_server],
        }
        with open(mcp_config_path, "w") as f:
            json.dump(existing_mcp, f, indent=2)

        self._setup_skill(working_dir)

        # CLAUDE.md — append our block (idempotent)
        md_path = os.path.join(working_dir, "CLAUDE.md")
        existing_md = ""
        if os.path.exists(md_path):
            with open(md_path) as f:
                existing_md = f.read()

        if _MD_START not in existing_md:
            with open(md_path, "a") as f:
                if existing_md and not existing_md.endswith("\n"):
                    f.write("\n")
                f.write("\n" + _claude_md_block(self._skill_name))

        return mcp_config_path

    # ── steam-debugger skill autoload ──────────────────────────────────────────

    def _find_steam_debugger_skill(self) -> str | None:
        """Locate the user's steam-debugger skill under ~/.claude/skills."""
        seen: set[str] = set()
        for base in _SKILL_BASES:
            base = os.path.realpath(base)
            if base in seen or not os.path.isdir(base):
                continue
            seen.add(base)
            try:
                entries = sorted(os.listdir(base))
            except OSError:
                continue
            for entry in entries:
                norm = entry.lower().replace("-", "").replace("_", "")
                if "steam" in norm and "debug" in norm:
                    path = os.path.join(base, entry)
                    if os.path.isfile(os.path.join(path, "SKILL.md")):
                        return path
        return None

    def _setup_skill(self, working_dir: str) -> None:
        """Symlink the steam-debugger skill into the session's .claude/skills
        so Claude discovers it regardless of working directory."""
        self._skill_name = None
        self._skill_link_created = None

        src = self._find_steam_debugger_skill()
        if not src:
            logger.warning("steam-debugger skill not found in %s", _SKILL_BASES)
            return

        name = os.path.basename(src)
        dest = os.path.join(working_dir, ".claude", "skills", name)
        if os.path.realpath(dest) == os.path.realpath(src):
            self._skill_name = name
            if os.path.islink(dest):
                # Symlink from a previous session — track it for cleanup
                self._skill_link_created = dest
            # else: working dir already contains the real skill (e.g. /home/deck)
            return

        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if not os.path.exists(dest):
                os.symlink(src, dest)
                self._skill_link_created = dest
            self._skill_name = name
            logger.info("steam-debugger skill linked: %s -> %s", dest, src)
        except OSError as exc:
            logger.error("failed to link skill %s: %s", src, exc)

    def _cleanup_working_dir(self, working_dir: str) -> None:
        """Remove the steamdeck MCP entry, skill symlink and CLAUDE.md block."""
        if not working_dir:
            return

        # Clean .mcp.json
        mcp_config_path = os.path.join(working_dir, ".mcp.json")
        if os.path.exists(mcp_config_path):
            try:
                with open(mcp_config_path) as f:
                    cfg = json.load(f)
                cfg.get("mcpServers", {}).pop("steamdeck", None)
                if cfg.get("mcpServers"):
                    with open(mcp_config_path, "w") as f:
                        json.dump(cfg, f, indent=2)
                else:
                    os.unlink(mcp_config_path)
            except (json.JSONDecodeError, OSError):
                pass

        # Remove the skill symlink we created (never the user's real skill)
        if self._skill_link_created and os.path.islink(self._skill_link_created):
            try:
                os.unlink(self._skill_link_created)
                skills_dir = os.path.dirname(self._skill_link_created)
                if not os.listdir(skills_dir):
                    os.rmdir(skills_dir)
            except OSError:
                pass
        self._skill_link_created = None
        self._skill_name = None

        # Clean CLAUDE.md block
        md_path = os.path.join(working_dir, "CLAUDE.md")
        if os.path.exists(md_path):
            try:
                with open(md_path) as f:
                    content = f.read()
                start = content.find("\n" + _MD_START)
                if start == -1:
                    start = content.find(_MD_START)
                end = content.find(_MD_END)
                if start != -1 and end != -1:
                    cleaned = content[:start] + content[end + len(_MD_END):]
                    cleaned = cleaned.rstrip() + "\n" if cleaned.strip() else ""
                    if cleaned.strip():
                        with open(md_path, "w") as f:
                            f.write(cleaned)
                    else:
                        os.unlink(md_path)
            except OSError:
                pass

    # ── private helpers ────────────────────────────────────────────────────────

    def _display_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        env["XDG_RUNTIME_DIR"] = "/run/user/1000"
        for wd in ["wayland-0", "wayland-1", "wayland-2"]:
            if os.path.exists(f"/run/user/1000/{wd}"):
                env["WAYLAND_DISPLAY"] = wd
                break
        env.setdefault("DISPLAY", ":0")
        return env

    async def _take_screenshot(self, out_path: str) -> dict:
        env = {**os.environ, **self._display_env()}
        for cmd in [
            ["grim", out_path],
            ["scrot", out_path],
            ["import", "-window", "root", out_path],
        ]:
            r = await self._run_cmd(cmd, env)
            if r["success"] and os.path.exists(out_path):
                return {"success": True, "path": out_path}
        return {"success": False, "error": "No screenshot tool available (grim/scrot/import)"}

    async def _make_thumb(self, src: str) -> str | None:
        thumb = src.replace(".png", "_thumb.png")
        env = {**os.environ, **self._display_env()}
        for cmd in [["grim", "-s", "0.3", thumb], ["convert", "-resize", "30%", src, thumb]]:
            r = await self._run_cmd(cmd, env)
            if r["success"] and os.path.exists(thumb):
                src = thumb
                break
        try:
            with open(src, "rb") as f:
                return base64.b64encode(f.read(512 * 1024)).decode()
        except OSError:
            return None

    async def _run_cmd(self, cmd: list, env: dict | None = None) -> dict:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **(env or {})},
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=8)
            if proc.returncode == 0:
                return {"success": True}
            return {"success": False, "error": stderr.decode(errors="replace").strip()}
        except FileNotFoundError:
            return {"success": False, "error": f"{cmd[0]} not found"}
        except asyncio.TimeoutError:
            return {"success": False, "error": "command timed out"}

    async def _find_claude(self) -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "which", "claude",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            if proc.returncode == 0:
                path = out.decode().strip()
                if path:
                    return path
        except OSError:
            pass
        for path in [
            "/home/deck/.local/share/pnpm/claude",
            "/home/deck/.local/bin/claude",
            "/usr/local/bin/claude",
            "/usr/bin/claude",
        ] + glob.glob("/home/deck/.nvm/versions/node/*/bin/claude"):
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        return None

    async def _drain_output(self):
        try:
            while True:
                raw = await self._process.stdout.readline()
                if not raw:
                    break
                line = _ANSI_ESCAPE.sub("", raw.decode("utf-8", errors="replace")).strip()
                if not line:
                    continue
                logger.info("claude: %s", line)
                self._log_lines.append(line)
                match = _URL_PATTERN.search(line)
                if match and not self._session_url:
                    self._session_url = match.group(0)
                    if self._status == "starting":
                        self._status = "running"
                lower = line.lower()
                if any(kw in lower for kw in ("not logged in", "api key", "auth", "error")):
                    if self._status == "starting":
                        self._error_msg = line
        except Exception as exc:
            logger.error("drain_output error: %s", exc)
        finally:
            if self._status == "running":
                self._status = "stopped"
