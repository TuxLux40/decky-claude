# decky-claude

A Decky Loader plugin that bridges Claude Code's remote session with Steam Deck Gaming Mode, so you can ask Claude for help while you play — and Claude can see and interact with exactly what you see.

---

## Goals & Intentions

### 1. In-game assistance (the player perspective)

The primary use case is staying in Gaming Mode and asking Claude a question about the game you're playing — a puzzle solution, a hidden mechanic, what a UI element means — without ever switching to Desktop Mode or picking up a laptop.

**Intended flow:**
1. Open the Decky Quick Access menu (⋯) while in-game.
2. Tap **Start Remote Session** → a URL appears.
3. Open the Claude app on Android, connect to the session.
4. Ask your question (e.g. *"how do I solve this puzzle?"*).
5. Claude automatically captures the current screen, analyzes it in context, and replies — seeing exactly what you're seeing, not just a text description.

Claude operates from the **user's perspective**, not the terminal's. It reads the game visually the same way you do.

---

### 2. Autonomous game debugging (see + fix, not just read logs)

The second use case is fixing games that crash or won't launch. Most game debugging advice online is terminal-only: check logs, edit config files, run commands. That misses half the picture.

**Intended flow:**
1. Describe the problem from the Android app (*"Elden Ring crashes at the menu"*).
2. Claude captures a screenshot of the current state (crash dialog, black screen, error overlay).
3. It reads the relevant log files and config from your working directory.
4. If it needs to change a setting, navigate a UI, or confirm something, it uses the Input controls (keystrokes, mouse clicks, typed text) to interact with the game or Steam UI **directly** — the same way a human would.
5. It reports back what it found and what it changed, with the visual context to prove it.

The goal is that Claude can act as a co-pilot who has **both** the terminal view (logs, configs, file system) **and** the visual view (what's actually on screen) — and can operate either one.

---

## Architecture

```
Android Claude app
      │  remote control (claude --rc)
      ▼
Steam Deck — Gaming Mode
  ┌─────────────────────────────┐
  │  Decky Quick Access Panel   │
  │  (decky-claude plugin)      │
  │                             │
  │  ┌──────────┐  ┌─────────┐  │
  │  │ main.py  │  │ React   │  │
  │  │ backend  │  │ panel   │  │
  │  └────┬─────┘  └─────────┘  │
  │       │                     │
  │  ┌────▼──────────────────┐  │
  │  │  claude --rc process  │  │
  │  │  (working directory)  │  │
  │  └───────────────────────┘  │
  │                             │
  │  grim  ──►  screen.png      │  ← visual context
  │  xdotool / ydotool          │  ← UI interaction
  └─────────────────────────────┘
```

| Layer | Tool | Purpose |
|---|---|---|
| Remote session | `claude --rc` | Bridges Android app ↔ Deck |
| Screenshot | `grim` → `scrot` → ImageMagick | Captures the visual state |
| Auto-capture | asyncio loop | Keeps `screen_latest.png` fresh |
| Keyboard/mouse | `xdotool` → `ydotool` | Lets Claude navigate UI |
| File access | Working directory | Logs, configs, saves |
| Skill autoload | `~/.claude/skills/steam-debugger` | Loaded into every session |

### Steam-debugger skill autoload

If a skill whose folder name contains *steam* and *debug* (e.g.
`steam-debugger`) exists under `~/.claude/skills/`, the plugin symlinks it
into the session working directory (`.claude/skills/`) and instructs Claude —
via the injected `CLAUDE.md` block — to invoke it at the start of the session,
before any Steam/game debugging work. The panel shows whether the skill was
found. The symlink and all injected config are removed again when the session
stops.

---

## Installation

### Prerequisites (Desktop Mode, one-time setup)

```bash
# Install Claude Code
npm install -g @anthropic-ai/claude-code

# Log in
claude

# Install screenshot tool (grim is usually already on SteamOS)
which grim || sudo pacman -S grim

# Optional: ydotool for native Wayland input (xdotool works for most games)
sudo pacman -S ydotool
sudo ydotoold &   # or add to autostart
```

### Build the plugin

```bash
pnpm install
pnpm build
```

Or push a `v1.x.x` tag to trigger the GitHub Actions release — download `decky-claude.zip` from the release assets.

### Side-load via Decky

Copy the plugin folder (containing `dist/`, `main.py`, `plugin.json`) to:
```
~/homebrew/plugins/decky-claude/
```
Then restart the Decky plugin loader.

---

## Usage

1. **In Gaming Mode** → Quick Access (⋯) → **Claude Code**.
2. Select your project / game directory from the dropdown.
3. Tap **Start Remote Session** → session URL appears.
4. On Android: Claude app → Code tab → paste or tap the URL.
5. Ask Claude anything. Use **Capture Screen Now** to give it the current visual, or enable **Auto-capture** so it always has a live view.
6. Use the **Input** section to have Claude (or yourself) send keystrokes and clicks directly to the game.

---

## Roadmap

- [x] Screenshot on demand (MCP tool — Claude calls it automatically)
- [x] Autonomous UI navigation via keyboard, mouse, text input (MCP tools)
- [x] CLAUDE.md injection — instructs Claude to screenshot before answering game questions
- [x] steam-debugger skill autoload (`~/.claude/skills/steam-debugger`)
- [ ] Game process detection (identify which game is running)
- [ ] Log file watcher (tail Steam / Proton logs into Claude's context)
- [ ] Gamepad input via `ydotool` evdev events
