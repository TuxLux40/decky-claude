import asyncio
import base64
import fcntl
import glob
import json
import logging
import os
import pty
import pwd
import re
import sys
import termios
import uuid
from collections import OrderedDict

# Decky imports this file by path, so the plugin directory is not guaranteed to
# be on sys.path — anchor it so the sibling module shared with mcp_server.py
# resolves either way.
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

import deck_common  # noqa: E402  (needs the sys.path anchor above)
import machine_profile  # noqa: E402

logger = logging.getLogger("decky-claude")


def _become_controlling_tty():
    """preexec_fn for pty-backed subprocesses: fd 0 is already dup'd to the
    pty slave by the time this runs, but an inherited fd is never
    auto-adopted as the controlling terminal on Linux — only setsid() +
    TIOCSCTTY does that. Without this, whether `sudo`/`pkexec` inside the
    spawned `claude` find a controlling terminal is down to accidental
    leftover session state, not this fd wiring.
    """
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)

_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_URL_PATTERN = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9_-]+")
# `claude auth login` prints its OAuth URL wrapped in an OSC-8 hyperlink, so
# stop at ESC/BEL/] rather than whitespace.
_OAUTH_URL = re.compile(r"https://[^\s\x1b\x07\]]*/oauth/[^\s\x1b\x07\]]+")
# Claude 2.1+ blocks `--resume` of a large/old transcript on an interactive
# picker ("Resume from summary?"). A remote-control session has no one at the
# keyboard, so that prompt is why Resume Session looked like a fresh start.
_RESUME_PROMPT = re.compile(
    r"Resume from summary \(recommended\)|Resuming the full session will consume",
    re.I,
)

# Sentinel used to mark the block we inject into CLAUDE.md
_MD_START = "<!-- decky-claude-start -->"
_MD_END = "<!-- decky-claude-end -->"


def _resolve_user() -> tuple[str, int]:
    """Home directory and uid of the desktop user owning the Steam session.

    Hardcoding /home/deck only works on SteamOS. Decky exports DECKY_USER /
    DECKY_USER_HOME, and the backend normally already runs as the desktop user,
    so prefer those signals and treat "deck" as a last-resort guess.
    """
    home = os.environ.get("DECKY_USER_HOME")
    user = os.environ.get("DECKY_USER")

    if user:
        try:
            entry = pwd.getpwnam(user)
            return home or entry.pw_dir, entry.pw_uid
        except KeyError:
            pass
    if home:
        try:
            return home, pwd.getpwnam(os.path.basename(home)).pw_uid
        except KeyError:
            return home, os.getuid()

    # Normal case: the plugin backend runs as the desktop user already.
    uid = os.getuid()
    if uid != 0:
        try:
            entry = pwd.getpwuid(uid)
            return entry.pw_dir, uid
        except KeyError:
            pass
    try:
        entry = pwd.getpwnam("deck")
        return entry.pw_dir, entry.pw_uid
    except KeyError:
        return os.path.expanduser("~"), uid


_USER_HOME, _USER_UID = _resolve_user()

# Directories scanned for the steam-debugger skill. A personal copy in
# ~/.claude/skills overrides the one bundled with the plugin.
_SKILL_BASES = [
    os.path.join(_USER_HOME, ".claude", "skills"),
    os.path.join(_PLUGIN_DIR, "skills"),
]


def _claude_md_block(skill_name: str | None, machine_md: str = "") -> str:
    skill_section = ""
    if skill_name:
        skill_section = f"""
## Steam debugger skill — load it first

The user's `{skill_name}` skill is installed for this session
(`.claude/skills/{skill_name}`). BLOCKING REQUIREMENT: invoke the
`{skill_name}` skill via the Skill tool at the start of the session, before
doing any Steam or game debugging work, and follow its instructions.
"""
    machine_section = f"\n{machine_md}\n" if machine_md else ""
    return f"""\
{_MD_START}
# decky-claude session

You are running via the decky-claude Decky Loader plugin, launched from the
Steam Quick Access menu. The user is messaging you from the Claude app, most
likely on their phone, and may not be able to read long output comfortably.
{machine_section}{skill_section}
## MCP tools you have

- **steam_snippet** — Curated, pre-verified SteamClient queries for the
  common problems: `downloads`, `login`, `library`, `running`, `client_info`,
  plus the `refresh_library` and `check_updates` actions. Try this before
  hand-writing JS: several namespaces (Downloads especially) expose no getters
  at all, only RegisterFor* callbacks, so the obvious expression returns
  nothing.
- **steam_ui_targets** / **steam_ui_eval** — The general debugging tools.
  Steam's UI is embedded Chromium; `steam_ui_eval` runs JavaScript inside it
  via the Chrome DevTools Protocol. `SharedJSContext` hosts the `SteamClient`
  API — inspect Steam's real state and trigger real actions instead of
  clicking pixels.
- **screenshot** — Capture the current display (Steam UI, game, error dialog).
  Returns a PNG image so you can see exactly what the user sees.
- **send_key** — Send a key press to the focused window
  (e.g. `escape`, `Return`, `space`, `Tab`, `F1`, `ctrl+c`).
- **type_text** — Type a string into the focused window.
- **mouse_move_click** — Move to (x, y) pixel coordinates and click.
  Steam Deck native resolution is 1280×800.

## Behaviour rules

1. **The primary mission is debugging Steam and the Steam UI.** For those
   problems, prefer `steam_ui_eval` (structured, reliable) over pixel input;
   use `screenshot` to correlate with what the user sees.
2. **Look before answering**: for anything about the current visual state
   (errors, dialogs, games), call `screenshot` first. Never guess — look.
3. **Read the logs**: `~/.steam/steam/logs/`, `journalctl --user`, Proton
   logs — combine the terminal view with the visual view.
4. **After sending input or triggering an action**: verify via another
   screenshot or `steam_ui_eval` read.
{_MD_END}
"""


class Plugin:
    # ── session state ──────────────────────────────────────────────────────────
    _process: asyncio.subprocess.Process | None = None
    _pty_master_fd: int | None = None
    _session_url: str | None = None
    _status: str = "stopped"
    _working_dir: str = _USER_HOME
    _resume_id: str = ""
    _session_id: str = ""
    _error_msg: str | None = None
    _log_lines: list[str] = []
    _skill_name: str | None = None
    _skill_link_created: str | None = None

    # ── login flow state ───────────────────────────────────────────────────────
    _login_proc: asyncio.subprocess.Process | None = None
    _login_fd: int | None = None
    _login_url: str | None = None

    # ── screen state ───────────────────────────────────────────────────────────
    _last_thumb_b64: str | None = None

    @staticmethod
    def _machine_md() -> str:
        """Machine description for the session's CLAUDE.md.

        Regenerated per session rather than cached: it costs ~50ms of file
        reads, and the parts most worth knowing (free space, installed games,
        whether Gaming Mode is active) are exactly the parts that go stale.
        Never fatal — a session without a profile is merely less informed.
        """
        try:
            return machine_profile.render(machine_profile.collect(_USER_HOME, _USER_UID))
        except Exception:
            logger.exception("could not build machine profile")
            return ""

    # ── lifecycle ──────────────────────────────────────────────────────────────

    async def _main(self):
        logger.info("decky-claude loaded")
        self._log_lines = []

    async def _unload(self):
        await self.cancel_login()
        await self.stop_session()

    # ── session API ────────────────────────────────────────────────────────────

    async def start_session(self, working_dir: str = "", resume_id: str = ""):
        if self._process and self._process.returncode is None:
            return {"success": False, "error": "Session already running"}

        # A transcript only replays in the directory it was recorded in, so the
        # session's own cwd wins over whatever the panel had selected.
        if resume_id:
            recorded = self._session_dir(resume_id)
            if not recorded:
                return {"success": False, "error": "That session no longer exists"}
            working_dir = recorded

        working_dir = working_dir or _USER_HOME
        self._working_dir = working_dir
        self._resume_id = resume_id
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

        try:
            mcp_config = self._setup_working_dir(working_dir)
        except OSError as exc:
            logger.error("could not prepare %s: %s", working_dir, exc)
            self._status = "error"
            self._error_msg = (
                f"Could not prepare {working_dir}: {exc}. "
                "Pick a directory you can write to."
            )
            return {"success": False, "error": self._error_msg}
        self._trust_working_dir(working_dir)

        # claude falls back to non-interactive "-p" behaviour (which then
        # demands a prompt) whenever stdout isn't a TTY, so a plain pipe
        # can't be used here — give it a pty to keep it in interactive
        # remote-control mode while we still capture its output.
        # stdin has to be the pty too: the TUI only renders when all three
        # streams are a terminal, and with stdin on /dev/null it prints
        # nothing at all, so the session URL never appears.
        master_fd, slave_fd = pty.openpty()

        argv = [claude_bin, "--remote-control", "--mcp-config", mcp_config]
        if resume_id:
            argv += ["--resume", resume_id]
            self._session_id = resume_id
        else:
            # Pin the id up front instead of hunting for the transcript claude
            # picked, so the panel can point at its own session in the list.
            self._session_id = str(uuid.uuid4())
            argv += ["--session-id", self._session_id]

        env = self._display_env()
        try:
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=working_dir,
                env=env,
                stdout=slave_fd,
                stderr=slave_fd,
                stdin=slave_fd,
                preexec_fn=_become_controlling_tty,
            )
        except Exception as exc:
            os.close(slave_fd)
            os.close(master_fd)
            self._status = "error"
            self._error_msg = str(exc)
            return {"success": False, "error": self._error_msg}
        os.close(slave_fd)

        self._pty_master_fd = master_fd

        asyncio.ensure_future(self._drain_output())

        for _ in range(40):
            if self._session_url:
                self._status = "running"
                self._error_msg = None
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

        if self._pty_master_fd is not None:
            try:
                os.close(self._pty_master_fd)
            except OSError:
                pass
            self._pty_master_fd = None

        self._cleanup_working_dir(self._working_dir)
        self._session_url = None
        self._status = "stopped"
        self._error_msg = None
        self._resume_id = ""
        self._session_id = ""
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
            "resume_id": self._resume_id,
            "error": self._error_msg,
            "skill": self._skill_name,
        }

    async def get_log(self):
        return {"lines": self._log_lines[-30:]}

    # ── authentication ─────────────────────────────────────────────────────────

    async def get_auth(self):
        """Login state straight from the CLI, so it can't drift from reality."""
        claude_bin = await self._find_claude()
        if not claude_bin:
            return {"logged_in": False, "error": "claude not found"}
        try:
            proc = await asyncio.create_subprocess_exec(
                claude_bin, "auth", "status", "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._display_env(),
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            data = json.loads(out.decode(errors="replace"))
        except (OSError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            return {"logged_in": False, "error": str(exc)}
        return {
            "logged_in": bool(data.get("loggedIn")),
            "email": data.get("email"),
            "org": data.get("orgName"),
            "plan": data.get("subscriptionType"),
            "method": data.get("authMethod"),
        }

    async def start_login(self):
        """Begin `claude auth login` and hand back the OAuth URL to show as a QR.

        The CLI renders the URL as an OSC-8 hyperlink and then blocks on a
        "Paste code here" prompt, so it needs a pty and the process has to stay
        alive until submit_login_code() feeds the code back.
        """
        if self._login_fd is not None:
            if self._login_url:
                return {"success": True, "url": self._login_url}
            await self.cancel_login()

        claude_bin = await self._find_claude()
        if not claude_bin:
            return {"success": False, "error": "claude not found"}

        master_fd, slave_fd = pty.openpty()
        try:
            self._login_proc = await asyncio.create_subprocess_exec(
                claude_bin, "auth", "login",
                cwd=_USER_HOME,
                env=self._display_env(),
                stdout=slave_fd,
                stderr=slave_fd,
                stdin=slave_fd,
                preexec_fn=_become_controlling_tty,
            )
        except Exception as exc:
            os.close(slave_fd)
            os.close(master_fd)
            return {"success": False, "error": str(exc)}
        os.close(slave_fd)
        self._login_fd = master_fd
        self._login_url = None

        loop = asyncio.get_event_loop()
        buf = b""
        deadline = loop.time() + 30
        while loop.time() < deadline:
            try:
                chunk = await asyncio.wait_for(
                    loop.run_in_executor(None, os.read, master_fd, 4096), timeout=5
                )
            except (asyncio.TimeoutError, OSError):
                break
            if not chunk:
                break
            buf += chunk
            match = _OAUTH_URL.search(buf.decode("utf-8", errors="replace"))
            if match:
                self._login_url = match.group(0)
                return {"success": True, "url": self._login_url}

        await self.cancel_login()
        return {"success": False, "error": "Timed out waiting for the login URL"}

    async def submit_login_code(self, code: str):
        """Feed the pasted OAuth code back to the waiting login process."""
        if self._login_fd is None:
            return {"success": False, "error": "No login in progress"}
        try:
            os.write(self._login_fd, (code.strip() + "\n").encode())
        except OSError as exc:
            return {"success": False, "error": str(exc)}

        try:
            await asyncio.wait_for(self._login_proc.wait(), timeout=60)
        except (asyncio.TimeoutError, AttributeError):
            pass
        await self.cancel_login()

        auth = await self.get_auth()
        if auth.get("logged_in"):
            return {"success": True, **auth}
        return {"success": False, "error": auth.get("error") or "Login did not complete"}

    async def cancel_login(self):
        if self._login_proc and self._login_proc.returncode is None:
            try:
                self._login_proc.kill()
            except ProcessLookupError:
                pass
        self._login_proc = None
        if self._login_fd is not None:
            try:
                os.close(self._login_fd)
            except OSError:
                pass
            self._login_fd = None
        self._login_url = None
        return {"success": True}

    # ── other sessions on this machine ─────────────────────────────────────────

    async def list_sessions(self, limit: int = 3):
        """Recent Claude Code sessions belonging to this user.

        Transcripts live in ~/.claude/projects/<encoded-cwd>/<session>.jsonl.
        The encoded directory name is lossy (slashes and dashes collapse), so
        read the real cwd out of the transcript instead.
        """
        root = os.path.join(_USER_HOME, ".claude", "projects")
        live = self._live_session_ids()
        entries = []
        try:
            for project in os.listdir(root):
                pdir = os.path.join(root, project)
                if not os.path.isdir(pdir):
                    continue
                for name in os.listdir(pdir):
                    if not name.endswith(".jsonl"):
                        continue
                    path = os.path.join(pdir, name)
                    try:
                        mtime = os.path.getmtime(path)
                    except OSError:
                        continue
                    entries.append((mtime, path, name[: -len(".jsonl")]))
        except OSError as exc:
            return {"sessions": [], "error": str(exc)}

        entries.sort(reverse=True)
        sessions = []
        for mtime, path, session_id in entries[:limit]:
            cwd = self._session_cwd(path) or self._decode_project_dir(
                os.path.basename(os.path.dirname(path))
            )
            sessions.append({
                "id": session_id,
                "short_id": session_id[:8],
                "cwd": cwd,
                "label": os.path.basename(cwd.rstrip("/")) or cwd,
                "preview": self._session_preview(path, mtime),
                "mtime": int(mtime),
                "live": session_id in live,
                "current": session_id == self._session_id and self._status == "running",
            })
        return {"sessions": sessions}

    # Keyed by transcript path, so it would otherwise grow with every session
    # this machine has ever recorded. The panel only ever renders one page of
    # list_sessions, so a few pages' worth of history is ample.
    _PREVIEW_CACHE_MAX = 64
    _preview_cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()

    @classmethod
    def _session_preview(cls, path: str, mtime: float) -> str:
        """First thing the user asked in that session — the only practical way
        to tell two sessions in the same directory apart when picking one to
        resume. Cached on mtime because the panel polls this list.
        """
        cached = cls._preview_cache.get(path)
        if cached and cached[0] == mtime:
            cls._preview_cache.move_to_end(path)
            return cached[1]

        preview = ""
        try:
            with open(path, "r", errors="replace") as f:
                for _ in range(400):
                    line = f.readline()
                    if not line:
                        break
                    if '"user"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("type") != "user":
                        continue
                    content = (record.get("message") or {}).get("content")
                    if isinstance(content, list):
                        content = next(
                            (b.get("text") for b in content
                             if isinstance(b, dict) and b.get("type") == "text"),
                            None,
                        )
                    if not isinstance(content, str):
                        continue
                    text = " ".join(content.split())
                    # Skip the synthetic openers (command stdout, hook output,
                    # caveats) that would otherwise become every session's title.
                    if not text or text.startswith("<") or text.startswith("Caveat:"):
                        continue
                    preview = text[:70]
                    break
        except OSError:
            pass

        cls._preview_cache[path] = (mtime, preview)
        cls._preview_cache.move_to_end(path)
        while len(cls._preview_cache) > cls._PREVIEW_CACHE_MAX:
            cls._preview_cache.popitem(last=False)
        return preview

    def _session_dir(self, session_id: str) -> str | None:
        """Directory a past session was recorded in, or None if it's gone.

        `claude --resume <id>` only finds a transcript when it is started in the
        same cwd, because that is what selects the ~/.claude/projects bucket.
        """
        root = os.path.join(_USER_HOME, ".claude", "projects")
        try:
            projects = os.listdir(root)
        except OSError:
            return None
        for project in projects:
            path = os.path.join(root, project, f"{session_id}.jsonl")
            if os.path.isfile(path):
                return self._session_cwd(path) or self._decode_project_dir(project)
        return None

    @staticmethod
    def _decode_project_dir(name: str) -> str:
        """Best-effort cwd from the encoded project directory name.

        Claude encodes the cwd by replacing "/" with "-", which is lossy for
        paths that already contain dashes. Only used when the transcript itself
        carries no cwd, so an approximate path beats showing nothing.
        """
        candidate = "/" + name.lstrip("-").replace("-", "/")
        if os.path.isdir(candidate):
            return candidate
        return candidate.rstrip("/") or "/"

    @staticmethod
    def _session_cwd(path: str) -> str | None:
        """cwd from the first transcript lines that carry it (usually line ~3)."""
        try:
            with open(path, "r", errors="replace") as f:
                for _ in range(12):
                    line = f.readline()
                    if not line:
                        break
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict) and record.get("cwd"):
                        return record["cwd"]
        except OSError:
            pass
        return None

    @staticmethod
    def _live_session_ids() -> set:
        """Session ids of running ``claude`` processes owned by this user.

        Live used to be keyed by cwd, which made every transcript in
        /home/oliver un-resumable whenever any Claude Code was open there —
        the default working directory, so Resume Session had nothing to pick.
        ``--session-id`` / ``--resume`` on the argv is the actual occupancy.
        """
        uid = os.getuid()
        ids: set[str] = set()
        flags = {b"--resume", b"-r", b"--session-id"}
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            base = f"/proc/{pid}"
            try:
                if os.stat(base).st_uid != uid:
                    continue
                with open(f"{base}/cmdline", "rb") as f:
                    argv = f.read().split(b"\0")
            except OSError:
                continue
            # Exact match: this plugin's own backend is titled "decky-claude",
            # which would otherwise match a substring test against its own cwd.
            if not argv or os.path.basename(argv[0] or b"") != b"claude":
                continue
            for i, arg in enumerate(argv):
                if arg in flags and i + 1 < len(argv):
                    sid = argv[i + 1].decode("utf-8", errors="replace").strip()
                    if sid and not sid.startswith("-"):
                        ids.add(sid)
        return ids

    async def list_dirs(self):
        base = _USER_HOME
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
        return await self._run_first(deck_common.key_commands(key))

    async def send_text(self, text: str):
        return await self._run_first(deck_common.type_commands(text))

    async def send_mouse_click(self, button: int = 1):
        return await self._run_first(deck_common.click_commands(button))

    # ── working-dir setup / teardown ───────────────────────────────────────────

    def _trust_working_dir(self, working_dir: str) -> None:
        """Pre-answer the two startup prompts that would otherwise block.

        A remote-control session has no local TTY, so anything claude asks
        before printing its URL is unanswerable and the session just hangs:

        1. The workspace trust dialog ("Yes, I trust this folder").
        2. The MCP approval prompt, because we write a .mcp.json declaring the
           `steamdeck` server. Listing it in enabledMcpjsonServers is what
           choosing "Use this and all future MCP servers in this project"
           records.

        Uses the resolved home rather than expanduser("~"): the backend
        inherits HOME=/root from the root-launched loader service, which would
        write these flags to /root/.claude.json while the claude we spawn reads
        the desktop user's copy — leaving the session stuck with no output.
        """
        config_path = os.path.join(_USER_HOME, ".claude.json")
        try:
            with open(config_path) as f:
                config = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            # Not fatal on its own, but it is the likeliest reason a session
            # starts and then sits there printing nothing, so say so loudly.
            logger.error(
                "cannot pre-approve trust/MCP prompts, %s unreadable: %s", config_path, exc
            )
            return

        projects = config.setdefault("projects", {})
        real_dir = os.path.realpath(working_dir)
        entry = projects.setdefault(real_dir, {})

        enabled = entry.setdefault("enabledMcpjsonServers", [])
        mcp_approved = "steamdeck" in enabled
        if entry.get("hasTrustDialogAccepted") and mcp_approved:
            return
        entry["hasTrustDialogAccepted"] = True
        if not mcp_approved:
            enabled.append("steamdeck")

        tmp_path = config_path + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(config, f, indent=2)
            os.replace(tmp_path, config_path)
        except OSError as exc:
            logger.error("could not write %s: %s", config_path, exc)
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def _setup_working_dir(self, working_dir: str) -> str:
        """Write .mcp.json, link the steam-debugger skill, inject our CLAUDE.md
        block. Returns the path of the MCP config to pass via --mcp-config.

        Raises OSError if the working directory cannot be prepared. Every write
        here is load-bearing — without them the session comes up with no tools
        and no idea it is on a Steam Deck — so the caller aborts the launch
        rather than starting a session that silently cannot do its job.
        """
        claude_dir = os.path.join(working_dir, ".claude")
        try:
            os.makedirs(claude_dir, exist_ok=True)
        except OSError as exc:
            logger.error("cannot create %s: %s", claude_dir, exc)
            raise

        # MCP server config — points to mcp_server.py next to this file.
        # Written to .mcp.json (project scope) and also passed explicitly via
        # --mcp-config so no trust prompt can block the headless session.
        mcp_server = os.path.join(_PLUGIN_DIR, "mcp_server.py")
        mcp_config_path = os.path.join(working_dir, ".mcp.json")

        existing_mcp: dict = {}
        if os.path.exists(mcp_config_path):
            # A malformed or unreadable file is not fatal: our own entry is
            # what matters and it gets rewritten from scratch below.
            try:
                with open(mcp_config_path) as f:
                    existing_mcp = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("ignoring unreadable %s: %s", mcp_config_path, exc)

        servers = existing_mcp.setdefault("mcpServers", {})
        servers["steamdeck"] = {
            "command": "python3",
            "args": [mcp_server],
        }
        try:
            with open(mcp_config_path, "w") as f:
                json.dump(existing_mcp, f, indent=2)
        except OSError as exc:
            logger.error("cannot write %s: %s", mcp_config_path, exc)
            raise

        self._setup_skill(working_dir)

        # CLAUDE.md — append our block (idempotent)
        md_path = os.path.join(working_dir, "CLAUDE.md")
        existing_md = ""
        try:
            if os.path.exists(md_path):
                with open(md_path) as f:
                    existing_md = f.read()

            if _MD_START not in existing_md:
                with open(md_path, "a") as f:
                    if existing_md and not existing_md.endswith("\n"):
                        f.write("\n")
                    f.write("\n" + _claude_md_block(self._skill_name, self._machine_md()))
        except OSError as exc:
            logger.error("cannot update %s: %s", md_path, exc)
            raise

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
        """Complete environment for every child process we spawn.

        Returns a full environment rather than an overlay, because two of the
        things it has to do are *remove* inherited variables.

        1. plugin_loader.service runs as root, so children inherit HOME=/root
           even though Decky drops the plugin to the desktop user's uid.
           `claude` then looks for credentials under /root, finds none, and
           reports itself logged out — which also makes `--remote-control` exit
           before printing a session URL.
        2. Decky Loader is a PyInstaller bundle whose bootloader points
           LD_LIBRARY_PATH at its extraction dir (/tmp/_MEIxxxxxx). That dir
           ships an older libreadline.so.8, so any child that shells out dies
           with "sh: symbol lookup error: undefined symbol:
           rl_trim_arg_from_keyseq". claude shells out internally, so this
           killed every session before it could print a URL.
        """
        env = dict(os.environ)

        # Drop the bundle's loader paths so children link system libraries.
        orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
        env.pop("_PYI_APPLICATION_HOME_DIR", None)

        env["HOME"] = _USER_HOME
        try:
            env["USER"] = env["LOGNAME"] = pwd.getpwuid(_USER_UID).pw_name
        except KeyError:
            pass

        # Skip the stale-resume confirmation dialog. Thresholds are the age
        # (minutes) and token count at which `claude --resume` asks "summary
        # or full session?" — a question nobody can answer from the QAM.
        # The PTY still auto-confirms if a future CLI ignores these.
        env.setdefault("CLAUDE_CODE_RESUME_THRESHOLD_MINUTES", "999999")
        env.setdefault("CLAUDE_CODE_RESUME_TOKEN_THRESHOLD", "999999999")

        # Compositor variables are resolved in deck_common so this process and
        # mcp_server.py cannot disagree about which session they are driving.
        return deck_common.apply_display_env(env, _USER_UID)

    async def _take_screenshot(self, out_path: str) -> dict:
        env = self._display_env()
        # This plugin targets Gaming Mode only, so capture goes through
        # gamescope and nothing else. That is not a fallback among options: it
        # is the same compositor-side capture the controller's screenshot
        # button triggers (Steam sets GAMESCOPECTRL_REQUEST_SCREENSHOT and
        # gamescope does the work), just addressed directly so the frame lands
        # at a path we choose instead of in the user's Steam screenshot
        # library. gamescope implements no Wayland screencopy protocol, so
        # grim and friends cannot capture here regardless.
        if not env.get("GAMESCOPE_WAYLAND_DISPLAY"):
            return {
                "success": False,
                "error": "Not running under gamescope — screen capture needs Gaming Mode",
            }

        # Outside a live gamescope, gamescopectl still exits 0 after failing to
        # connect, so the file itself is the only trustworthy success signal.
        r = await self._run_cmd(
            ["gamescopectl", "screenshot", out_path], env, timeout=15
        )
        if r["success"]:
            # gamescope writes the file from its own render thread, so it can
            # land slightly after the command returns.
            for _ in range(20):
                if os.path.exists(out_path) and os.path.getsize(out_path):
                    return {"success": True, "path": out_path}
                await asyncio.sleep(0.1)
        return {
            "success": False,
            "error": r.get("error") or "gamescope produced no screenshot",
        }

    async def _make_thumb(self, src: str) -> str | None:
        thumb = src.replace(".png", "_thumb.png")
        # Downscale the frame we already have. (grim -s used to be tried first,
        # but it re-captures rather than resizes and cannot run under gamescope
        # anyway.) Falls through to the full-size image if convert is absent.
        r = await self._run_cmd(["convert", "-resize", "30%", src, thumb])
        if r["success"] and os.path.exists(thumb):
            src = thumb
        try:
            with open(src, "rb") as f:
                return base64.b64encode(f.read(512 * 1024)).decode()
        except OSError:
            return None

    async def _run_first(self, cmds: list[list[str]]) -> dict:
        """Run the xdotool/ydotool alternatives until one succeeds, reporting
        the last failure if none do."""
        result = {"success": False, "error": "no command to run"}
        for cmd in cmds:
            result = await self._run_cmd(cmd)
            if result["success"]:
                return result
        return result

    async def _run_cmd(self, cmd: list, env: dict | None = None, timeout: float = 8) -> dict:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env={**self._display_env(), **(env or {})},
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            if proc.returncode == 0:
                return {"success": True}
            return {"success": False, "error": stderr.decode(errors="replace").strip()}
        except FileNotFoundError:
            return {"success": False, "error": f"{cmd[0]} not found"}
        except asyncio.TimeoutError:
            # wait_for only cancels the wait, so kill the child as well —
            # a GUI tool like spectacle would otherwise linger forever.
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            return {"success": False, "error": "command timed out"}

    async def _find_claude(self) -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "which", "claude",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._display_env(),
            )
            out, _ = await proc.communicate()
            if proc.returncode == 0:
                path = out.decode().strip()
                if path:
                    return path
        except OSError:
            pass
        for path in [
            os.path.join(_USER_HOME, ".local/share/pnpm/claude"),
            os.path.join(_USER_HOME, ".local/bin/claude"),
            os.path.join(_USER_HOME, ".bun/bin/claude"),
            "/usr/local/bin/claude",
            "/usr/bin/claude",
        ] + glob.glob(os.path.join(_USER_HOME, ".nvm/versions/node/*/bin/claude")):
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        return None

    def _adopt_session_url(self, text: str) -> None:
        """Pull a remote-control URL out of CLI output if we don't have one yet.

        The TUI wraps with ``\\r`` and often omits a trailing newline, so this
        has to search the full decoded buffer, not only newline-split lines.
        Finding a URL means the session started — any earlier banner line that
        looked like an error (MCP auth notices contain the substring "auth")
        was not fatal and must not stay stuck in the QAM.
        """
        if self._session_url:
            return
        match = _URL_PATTERN.search(text)
        if not match:
            return
        self._session_url = match.group(0)
        if self._status == "starting":
            self._status = "running"
        self._error_msg = None

    async def _drain_output(self):
        loop = asyncio.get_event_loop()
        fd = self._pty_master_fd
        buf = b""
        answered_resume_prompt = False
        try:
            while True:
                try:
                    chunk = await loop.run_in_executor(None, os.read, fd, 4096)
                except OSError:
                    # EIO on a pty means the slave side closed — process exited.
                    break
                if not chunk:
                    break
                buf += chunk
                # Search the whole buffer so a URL sitting after a carriage
                # return (no newline yet) is still picked up.
                decoded = _ANSI_ESCAPE.sub("", buf.decode("utf-8", errors="replace"))
                self._adopt_session_url(decoded)
                if (
                    not answered_resume_prompt
                    and self._resume_id
                    and _RESUME_PROMPT.search(decoded)
                ):
                    # Option 2 = "Resume full session as-is". Enter alone would
                    # take the recommended summary, which drops the transcript
                    # the user asked to pick up.
                    try:
                        os.write(fd, b"2\r")
                        answered_resume_prompt = True
                        logger.info("auto-confirmed resume prompt (full session)")
                    except OSError:
                        pass
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = _ANSI_ESCAPE.sub("", raw.decode("utf-8", errors="replace")).strip()
                    if not line:
                        continue
                    logger.info("claude: %s", line)
                    self._log_lines.append(line)
                    self._adopt_session_url(line)
                    # Real login failures make `--remote-control` exit, which
                    # start_session already reports. Do not treat "auth" or
                    # "error" as fatal: the startup banner says "MCP server
                    # needs authentication" and that was showing as a red
                    # error in the QAM even after the session URL arrived.
        except Exception as exc:
            logger.error("drain_output error: %s", exc)
        finally:
            if self._status == "running":
                self._status = "stopped"

    # ── settings API ───────────────────────────────────────────────────────────

    async def get_sidebar_tab(self):
        """Whether the dedicated Quick Access sidebar tab is shown (default on)."""
        return {"enabled": bool(_load_settings().get("sidebar_tab", True))}

    async def set_sidebar_tab(self, enabled: bool):
        settings = _load_settings()
        settings["sidebar_tab"] = bool(enabled)
        _save_settings(settings)
        return {"enabled": settings["sidebar_tab"]}


# ── persisted settings ──────────────────────────────────────────────────────────

# Decky exports DECKY_SETTINGS_DIR (~/homebrew/settings/<plugin>), which
# survives plugin updates; the fallback only matters outside the loader.
_SETTINGS_PATH = os.path.join(
    os.environ.get("DECKY_SETTINGS_DIR")
    or os.path.join(_USER_HOME, ".config", "decky-claude"),
    "settings.json",
)


def _load_settings() -> dict:
    try:
        with open(_SETTINGS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("could not read %s: %s", _SETTINGS_PATH, exc)
        return {}


def _save_settings(settings: dict) -> None:
    os.makedirs(os.path.dirname(_SETTINGS_PATH), exist_ok=True)
    tmp_path = _SETTINGS_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp_path, _SETTINGS_PATH)
