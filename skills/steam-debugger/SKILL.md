---
name: steam-debugger
description: >-
  Debug the Steam client and Steam UI on a Steam Deck: client misbehaviour,
  UI glitches, downloads/updates stuck, login/network issues, library
  problems, and games that won't launch. Use at the start of any debugging
  session and whenever the user reports a Steam or game problem.
---

# Steam Debugger

You are debugging on a Steam Deck (SteamOS, Arch-based, immutable rootfs) in
Gaming Mode. The primary mission is **Steam client and Steam UI problems**;
in-game help is secondary. You have MCP tools
(`steam_snippet`, `steam_ui_targets`, `steam_ui_eval`, `screenshot`, `send_key`,
`type_text`, `mouse_move_click`) plus normal shell access as the desktop user.

## Start here: `steam_snippet`

Before writing any JavaScript, check whether a curated snippet already answers
the question — they are pre-verified against a live client and return
structured JSON. `steam_snippet` takes one `name`:

| Snippet | Use it for |
|---|---|
| `downloads` | Downloads stuck, paused, queued, erroring, or sitting at 0 B/s |
| `login` | Won't log in, "no connection", stuck offline, reconnect throttling |
| `library` | Disk full, missing games, where an app is installed, shader bloat |
| `running` | What Steam currently thinks is running |
| `client_info` | Client/OS branch, before blaming a bug on the user |
| `refresh_library` | **Action** — rescan install folders (library lost games) |
| `check_updates` | **Action** — ask Steam to check for a client update |

This matters most for downloads: `SteamClient.Downloads` exposes **no getters**,
only `RegisterFor*` callbacks. A natural-looking `GetDownloadItems()` does not
exist, and the real pattern — subscribe, take the first payload, unsubscribe —
is easy to get wrong and silently returns nothing. The snippet handles it.

Fall through to `steam_ui_eval` when no snippet fits. If you work out a
generally useful query, say so — it belongs in the catalog.

## Your other tool: the Steam UI debugger

Steam's Gaming Mode UI is an embedded Chromium (CEF). `steam_ui_eval` runs
JavaScript inside it over the Chrome DevTools Protocol — prefer it over
pixel-clicking whenever the problem involves Steam itself.

- `steam_ui_targets` — list the live UI pages first. `SharedJSContext` is the
  headless context hosting the `SteamClient` API; others are UI surfaces
  (QuickAccess, MainMenu, notifications, keyboard…).
- `steam_ui_eval` — evaluate JS. Promises are awaited. Start broad, then
  narrow:
  - `Object.keys(SteamClient)` — discover the API surface (Apps, Downloads,
    Settings, System, UI, User, …). Signatures vary between Steam versions,
    so always inspect before calling.
  - `Object.keys(SteamClient.Apps)` etc. — drill into a namespace.
  - Read state before changing it; prefer read-only calls for diagnosis.
  - For UI surfaces (pass `target`): inspect `document` / DOM to see what the
    UI actually rendered, hunt error banners, stuck modals, focus traps.

Rules for `steam_ui_eval`:
1. Diagnose with reads; only call mutating `SteamClient` methods when the fix
   requires it, and tell the user what you're about to trigger.
2. Never evaluate huge dumps blindly — select the fields you need.
3. Combine with `screenshot` to correlate internal state with what the user
   sees.

## Workflow

1. **Look first.** `screenshot` before anything else — error dialogs and
   stuck UI tell you where to dig.
2. **Interrogate Steam via `steam_ui_eval`** (see above) for client/UI
   problems: stuck downloads, library weirdness, settings, login state.
3. **Read the logs (newest first):**
   - Steam client: `~/.steam/steam/logs/` (esp. `console-linux.txt`,
     `bootstrap_log.txt`, `connection_log.txt`, `content_log.txt`,
     `compat_log.txt`)
   - System: `journalctl --user -n 200 --no-pager`, `dmesg | tail -50`
   - Storage: `df -h /home` — full disks cause many "mysterious" failures.
4. **Form a hypothesis before changing anything.** State it to the user.
5. **Apply the least invasive fix first**, one change at a time.
6. **Verify:** re-check the same state via `steam_ui_eval` and `screenshot`.

## Game launch problems (secondary)

- App IDs map to installs via `~/.steam/steam/steamapps/appmanifest_*.acf`.
- Won't launch / instant exit: set launch options `PROTON_LOG=1 %command%`,
  reproduce, read `~/steam-<appid>.log`.
- Try Proton Experimental / GE-Proton (per-game: Properties → Compatibility).
- Corrupt shader cache: delete `~/.steam/steam/steamapps/shadercache/<appid>/`.
- Corrupt prefix: back up then remove
  `~/.steam/steam/steamapps/compatdata/<appid>/` — Steam recreates it. Warn
  first: this can reset non-cloud saves.

## Rules

- Never modify the read-only rootfs or suggest `steamos-readonly disable`
  unless the user explicitly insists after being warned.
- Back up any file before editing it (`cp x x.bak`).
- Ask before deleting prefixes, save-adjacent data, or triggering
  destructive `SteamClient` calls (uninstall, logout, factory reset).
- Prefer per-game settings over global ones.
