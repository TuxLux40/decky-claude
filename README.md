<img src="assets/glyph.png" alt="" width="96" height="96" align="right">

# decky-claude

A [Decky Loader](https://decky.xyz/) plugin that starts a **Claude Code session on your Steam Deck that you control from your phone** — built first and foremost to debug **Steam and the Steam UI** without leaving Gaming Mode.

Tap *Start Remote Session* in the Quick Access menu, open the session URL in the Claude app on your phone, and Claude is on your Deck: it can interrogate the Steam client from the inside, read logs, see your screen, and press buttons — while you watch from the couch.

---

## Scope: Gaming Mode only

This plugin targets **Gaming Mode — a gamescope session** and nothing else.
Screen capture goes through `gamescopectl`, which is the same compositor-side
capture the controller's screenshot button triggers: Steam sets the
`GAMESCOPECTRL_REQUEST_SCREENSHOT` atom and gamescope takes the frame. Calling
it directly just lets the frame land at a path we choose instead of in your
Steam screenshot library.

There is deliberately no desktop fallback. gamescope implements no Wayland
screencopy protocol, so `grim` cannot capture there, and a desktop session
would need a compositor-specific path of its own (KWin, for instance, exposes
capture only over its own D-Bus interface). Supporting both means carrying a
tool per desktop environment for a case this plugin is not for. Run Big Picture
inside a desktop session and the session features still work — `screenshot`
will simply report that it needs Gaming Mode.

## What it does

1. **Phone-controlled Claude Code** — the plugin launches `claude --remote-control` (remote control) in a working directory you pick and shows the `https://claude.ai/code/session_…` URL in the panel. Open it in the Claude app and drive the session from your phone.
2. **Steam UI debugger (the main event)** — Steam's Gaming Mode UI is embedded Chromium (CEF) with its DevTools debugger on `localhost:8080` (Decky itself relies on it). Claude gets `steam_ui_eval`: it runs JavaScript inside Steam over the Chrome DevTools Protocol, inspects the `SteamClient` API, reads real client state (downloads, library, settings, login), and triggers real actions — no pixel-hunting.
3. **Eyes and hands** — `screenshot` returns what's on screen as an image; `send_key` / `type_text` / `mouse_move_click` inject input via `xdotool`/`ydotool`. By default a capture is the game/desktop frame only — the same base-plane-only frame the physical screenshot button captures, with the Steam overlay (Quick Access Menu, notifications) excluded. Pass `include_steam_ui: true` (or flip the panel's toggle) to capture the overlay too.
4. **steam-debugger skill, autoloaded** — a bundled Claude Code skill encodes the debugging workflow (interrogate Steam UI first, log locations, least-invasive-fix rules, safety rails), with per-topic reference files. It is pulled from [TuxLux40/skills](https://github.com/TuxLux40/skills) as a git submodule, so upstream skill updates land here via automated PRs. It is linked into every session and Claude is instructed to load it at session start. A personal copy in `~/.claude/skills/` (any folder named like *steam…debug…*) overrides the bundled one.

In-game help (asking Claude about the game you're playing) works through the same screenshot/input tools, but it's a nice-to-have — the tooling is tuned for Steam debugging.

## How it works

```
Your phone (Claude app)
      │  claude.ai/code session (claude --remote-control)
      ▼
Steam Deck — Gaming Mode
  ┌───────────────────────────────────────────┐
  │ Decky Quick Access panel (React)          │
  │   └─ main.py backend: spawns claude --remote-control, │
  │      links skill, writes .mcp.json        │
  │                                           │
  │ claude --remote-control ──► mcp_server.py (stdio MCP) │
  │                   ├─ steam_ui_eval ───────┼──► Steam CEF debugger :8080
  │                   ├─ screenshot (gamescope) │    (Chrome DevTools Protocol)
  │                   └─ send_key/type/click  │
  │                      (xdotool/ydotool)    │
  └───────────────────────────────────────────┘
```

Everything injected into the working directory (`.mcp.json`, the `CLAUDE.md` block, the skill symlink) is removed again when you stop the session. `mcp_server.py` is stdlib-only Python — no pip dependencies, no daemon, no open ports; it lives only as a child of the Claude session.

## MCP tools

| Tool | Purpose |
|---|---|
| `steam_snippet` | Run a curated, pre-verified SteamClient query (downloads, login, library, running, client info, refresh, updates) |
| `steam_ui_targets` | List Steam's live UI pages (CDP targets) |
| `steam_ui_eval` | Run JavaScript inside the Steam client (`SharedJSContext` = `SteamClient` API) |
| `screenshot` | Capture the display, returned as a PNG image. Game/desktop frame only by default; `include_steam_ui: true` also captures the Steam overlay (QAM, notifications) — unverified on hardware |
| `send_key` | Key press to the focused window |
| `type_text` | Type a string |
| `mouse_move_click` | Move to (x, y) and click (1280×800 native) |
| `session_context` | Live "where am I running" check (read-only, see below) |

### `session_context`: where is this session running?

Two facts change what Claude can safely do, and both can change mid-session:

- **Display mode.** `gaming` if a `gamescope` / `gamescope-wl` process is running for the user, `desktop` otherwise. The process is the deciding signal; the `gamescope-N` socket is only reported as evidence, since a socket can outlive its compositor. `screenshot` and the input tools work only in `gaming`; in Desktop Mode use `steam_ui_eval` / `steam_snippet`.
- **Session origin.** Whether the Claude process descends from Decky's `PluginLoader` (walked via `/proc/<pid>/stat`, with the systemd unit from `/proc/<pid>/cgroup` as a fallback). A session launched from the plugin **dies instantly when `plugin_loader.service` restarts and does not auto-resume**. A standalone terminal session that uses the same MCP server is unaffected.

The tool returns JSON with the evidence (gamescope/desktop PIDs, runtime sockets, the ancestor chain, the systemd unit) plus a one-line consequence for each fact. The injected `CLAUDE.md` block tells Claude to call it before using screenshots or input, and before restarting the loader. The machine profile's mode line is marked as a snapshot from session start. When screenshots or input fail in Desktop Mode, the error message also points to this tool.

## Installation

### Prerequisites (Desktop Mode, one-time)

```bash
# Install and log in to Claude Code
npm install -g @anthropic-ai/claude-code
claude

# gamescopectl ships with gamescope
which gamescopectl

# send_key/type_text/mouse_move_click need xdotool (primary) and ydotool
# (fallback for pure-Wayland sessions with no XWayland). Neither ships by
# default and the plugin backend runs unprivileged, so this can't be done
# for you automatically — run it once yourself:
./scripts/setup-input-tools.sh
```

Decky Loader must be installed — it also keeps Steam's CEF debugger enabled, which `steam_ui_eval` needs.

> Not on SteamOS? That works too. The plugin resolves the desktop user from
> `DECKY_USER_HOME` / `DECKY_USER` (falling back to the account the backend runs
> as), so it does not assume a `deck` user or uid 1000.

### From a release

Download `decky-claude.zip` from the [GitHub releases][releases] (built by CI on every `v*` tag) and extract it to `~/homebrew/plugins/`, then restart Decky Loader:

```bash
systemctl restart plugin_loader
```

This plugin is distributed here rather than through the official Decky store, so it will not appear in the in-app store listing — install it from a release or from source.

[releases]: https://github.com/TuxLux40/decky-claude/releases

### From source

The bundled skill is a git submodule, so clone recursively (or run
`git submodule update --init` in an existing checkout):

```bash
git clone --recursive https://github.com/TuxLux40/decky-claude.git
cd decky-claude
pnpm install
pnpm build
```

Copy the plugin folder (containing `dist/`, `skills/`, `main.py`, `mcp_server.py`, `deck_common.py`, `machine_profile.py`, `plugin.json`, `package.json`) to `~/homebrew/plugins/decky-claude/` and restart Decky Loader. `skills/steam-debugger` is a symlink into the
submodule — copy with `cp -rL` (or equivalent) so the real files land in the
plugin folder.

## Usage

1. In Gaming Mode: open Quick Access (⋯) and pick the **Claude** icon in the
   sidebar — or go through the Decky tab → **Claude Code**. The sidebar icon can
   be turned off with **Settings → Show in Quick Access sidebar** at the bottom
   of the panel (on by default; applies the next time Quick Access opens).
   Decky has no public API for sidebar tabs, so this uses Decky Loader
   internals: if a Decky update breaks them, the toggle shows as unavailable
   and the plugin stays reachable through the Decky tab.
2. Leave **Session** on *New session* and pick a working directory, or choose a
   past session to pick up where it left off — resuming replays the transcript
   in the directory it was recorded in, so the working directory follows from
   the session. Then tap **Start Remote Session** / **Resume Session**.
3. Open the shown URL in the Claude app on your phone.
4. Describe the problem ("downloads are stuck", "Steam won't stay logged in", "X crashes at the menu"). Claude loads the steam-debugger skill, inspects Steam from the inside, reads logs, screenshots the screen, and walks the fix with you.
5. Stop the session from the panel when done — all injected config is cleaned up.

The panel's **Screen Preview** section is for you, not Claude — Claude captures on its own via the `screenshot` MCP tool whenever it needs to see something. The panel's **Capture Screen** button gives you the same view without spinning up a session: useful to sanity-check the capture pipeline, or just to glance at the Deck's screen from the Quick Access menu. It works even with no session running. By default it captures the game/desktop frame only (same as the physical screenshot button); toggle **Include Steam UI** on to capture the Quick Access Menu / overlay instead. The panel also offers manual key/mouse/text input for when you want to poke the Deck yourself.

## Repository layout

| Path | What |
|---|---|
| `main.py` | Decky backend: session lifecycle, working-dir setup/cleanup, panel API |
| `mcp_server.py` | Stdlib-only stdio MCP server: CDP client + screenshot + input tools |
| `machine_profile.py` | Probes hardware/OS/session/Steam into the session's CLAUDE.md; run standalone to inspect |
| `deck_common.py` | Display environment and xdotool/ydotool commands shared by both |
| `src/index.tsx` | Quick Access panel (React, built to `dist/` by rollup) |
| `src/sidebarTab.tsx` | Optional dedicated Quick Access sidebar tab (Decky internals, isolated) |
| `skills/steam-debugger` | Symlink to `vendor/skills/skills/steam-debugger` — the bundled Claude Code skill, autoloaded into sessions (dereferenced into real files at packaging time) |
| `vendor/skills/` | Git submodule: [TuxLux40/skills](https://github.com/TuxLux40/skills), source of truth for the skill |
| `.github/dependabot.yml` | Daily submodule bumps (skill updates) + weekly GitHub Actions bumps |
| `.github/workflows/release.yml` | Builds and packages `decky-claude.zip` on `v*` tags |
| `assets/` | Plugin icon (D-pad + Claude spark) as SVG source and PNG |

## Roadmap

- [x] Phone-controlled `claude --remote-control` sessions from the Quick Access menu
- [x] Steam UI debugging via Chrome DevTools Protocol (`steam_ui_eval`)
- [x] Screenshot + keyboard/mouse/text MCP tools
- [x] Bundled steam-debugger skill, autoloaded (personal copy overrides)
- [x] Resume a past session from the panel, and see what else is running here
- [x] Curated `SteamClient` helper snippets (stuck downloads, login state, library refresh)
- [ ] Log file watcher (tail Steam logs into Claude's context)
- [ ] In-game assistance polish (game detection, per-game context) — later

## License

MIT — see [LICENSE](LICENSE).
