import asyncio
import glob
import logging
import os
import re

logger = logging.getLogger("decky-claude")

# Strip ANSI escape codes from terminal output
_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
# Claude Code remote session URL pattern
_URL_PATTERN = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9_-]+")


class Plugin:
    _process: asyncio.subprocess.Process | None = None
    _session_url: str | None = None
    _status: str = "stopped"          # stopped | starting | running | error
    _working_dir: str = "/home/deck"
    _error_msg: str | None = None
    _log_lines: list[str] = []

    # ------------------------------------------------------------------ lifecycle

    async def _main(self):
        logger.info("decky-claude loaded")
        self._log_lines = []

    async def _unload(self):
        await self.stop_session(self)

    # ------------------------------------------------------------------ public API

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

        env = {**os.environ}
        # Ensure nvm / npm global bin dirs are on PATH
        deck_home = "/home/deck"
        extra_paths = [
            f"{deck_home}/.nvm/versions/node/$(ls {deck_home}/.nvm/versions/node 2>/dev/null | tail -1)/bin",
            f"{deck_home}/.local/share/pnpm",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
        ]
        env["PATH"] = ":".join(extra_paths) + ":" + env.get("PATH", "")

        try:
            self._process = await asyncio.create_subprocess_exec(
                claude_bin,
                "--rc",
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

        # Wait up to 20 s for the URL to appear
        for _ in range(40):
            if self._session_url:
                self._status = "running"
                return {"success": True, "url": self._session_url}
            if self._process.returncode is not None:
                self._status = "error"
                self._error_msg = "Process exited before providing a session URL"
                return {"success": False, "error": self._error_msg}
            await asyncio.sleep(0.5)

        # Process still alive but no URL yet — still consider it running
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
        # Reconcile if the process died on its own
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

    # ------------------------------------------------------------------ helpers

    async def _find_claude(self) -> str | None:
        # 1. Shell `which`
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

        # 2. Common static and nvm paths
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

                # Surface auth / error hints
                lower = line.lower()
                if any(kw in lower for kw in ("not logged in", "api key", "auth", "error")):
                    if self._status == "starting":
                        self._error_msg = line
        except Exception as exc:
            logger.error("drain_output error: %s", exc)
        finally:
            if self._status == "running":
                self._status = "stopped"
