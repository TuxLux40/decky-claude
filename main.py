import asyncio
import base64
import glob
import logging
import os
import re

logger = logging.getLogger("decky-claude")

_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_URL_PATTERN = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9_-]+")

# xdotool key name → ydotool KEY_ name
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

    # ── screen capture state ───────────────────────────────────────────────────
    _auto_task: asyncio.Task | None = None
    _auto_active: bool = False
    _auto_interval: int = 10
    _last_thumb_b64: str | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────────

    async def _main(self):
        logger.info("decky-claude loaded")
        self._log_lines = []

    async def _unload(self):
        await self.stop_auto_capture(self)
        await self.stop_session(self)

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

        env = {**os.environ, **self._display_env()}
        try:
            self._process = await asyncio.create_subprocess_exec(
                claude_bin, "--rc",
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

    # ── screenshot API ─────────────────────────────────────────────────────────

    async def capture_screenshot(self):
        """Take a screenshot, save to working_dir/screen.png, return thumbnail."""
        out_path = os.path.join(self._working_dir, "screen.png")
        result = await self._take_screenshot(out_path)
        if result["success"]:
            self._last_thumb_b64 = await self._make_thumb(out_path)
        return {**result, "thumbnail": self._last_thumb_b64}

    async def start_auto_capture(self, interval: int = 10):
        await self.stop_auto_capture(self)
        self._auto_interval = interval
        self._auto_active = True
        self._auto_task = asyncio.ensure_future(self._capture_loop())
        return {"success": True}

    async def stop_auto_capture(self):
        self._auto_active = False
        if self._auto_task:
            self._auto_task.cancel()
            try:
                await self._auto_task
            except asyncio.CancelledError:
                pass
            self._auto_task = None
        return {"success": True}

    async def get_screen_state(self):
        return {
            "auto_active": self._auto_active,
            "interval": self._auto_interval,
            "thumbnail": self._last_thumb_b64,
        }

    # ── input API ──────────────────────────────────────────────────────────────

    async def send_key(self, key: str):
        """Press a key. key = xdotool name: 'escape', 'Return', 'space', etc."""
        env = self._display_env()
        r = await self._run_cmd(["xdotool", "key", "--clearmodifiers", key], env)
        if r["success"]:
            return r
        ydokey = _KEY_MAP.get(key, f"KEY_{key.upper()}")
        return await self._run_cmd(["ydotool", "key", ydokey], env)

    async def send_text(self, text: str):
        """Type a string into the focused window."""
        env = self._display_env()
        r = await self._run_cmd(["xdotool", "type", "--clearmodifiers", "--", text], env)
        if r["success"]:
            return r
        return await self._run_cmd(["ydotool", "type", "--", text], env)

    async def send_mouse_click(self, button: int = 1):
        """Click a mouse button (1=left, 2=middle, 3=right)."""
        env = self._display_env()
        r = await self._run_cmd(["xdotool", "click", str(button)], env)
        if r["success"]:
            return r
        # ydotool button codes: left=0xC0, right=0xC1, middle=0xC2
        btn_code = {1: "0xC0", 3: "0xC1", 2: "0xC2"}.get(button, "0xC0")
        return await self._run_cmd(["ydotool", "click", btn_code], env)

    # ── private helpers ────────────────────────────────────────────────────────

    def _display_env(self) -> dict:
        env: dict[str, str] = {}
        env["XDG_RUNTIME_DIR"] = "/run/user/1000"
        for wd in ["wayland-0", "wayland-1", "wayland-2"]:
            if os.path.exists(f"/run/user/1000/{wd}"):
                env["WAYLAND_DISPLAY"] = wd
                break
        for xd in [":0", ":1"]:
            env.setdefault("DISPLAY", xd)
        return env

    async def _take_screenshot(self, out_path: str) -> dict:
        env = {**os.environ, **self._display_env()}
        for cmd in [
            ["grim", "-s", "1", out_path],
            ["scrot", out_path],
            ["import", "-window", "root", out_path],
        ]:
            r = await self._run_cmd(cmd, env)
            if r["success"] and os.path.exists(out_path):
                return {"success": True, "path": out_path}
        return {"success": False, "error": "No screenshot tool available (grim/scrot/import)"}

    async def _make_thumb(self, src: str) -> str | None:
        """Return base64 of a ~30% scaled thumbnail, falling back to the original."""
        thumb = src.replace(".png", "_thumb.png")
        env = {**os.environ, **self._display_env()}
        # Try grim at 30% scale, then ImageMagick, then raw full image
        for cmd in [
            ["grim", "-s", "0.3", thumb],
            ["convert", "-resize", "30%", src, thumb],
        ]:
            r = await self._run_cmd(cmd, env)
            if r["success"] and os.path.exists(thumb):
                src = thumb
                break
        try:
            with open(src, "rb") as f:
                raw = f.read(512 * 1024)  # cap at 512 KB
            return base64.b64encode(raw).decode()
        except OSError:
            return None

    async def _capture_loop(self):
        while self._auto_active:
            out_path = os.path.join(self._working_dir, "screen_latest.png")
            result = await self._take_screenshot(out_path)
            if result["success"]:
                self._last_thumb_b64 = await self._make_thumb(out_path)
            await asyncio.sleep(self._auto_interval)

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
        candidates = [
            "/home/deck/.local/share/pnpm/claude",
            "/home/deck/.local/bin/claude",
            "/usr/local/bin/claude",
            "/usr/bin/claude",
        ] + glob.glob("/home/deck/.nvm/versions/node/*/bin/claude")
        for path in candidates:
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
