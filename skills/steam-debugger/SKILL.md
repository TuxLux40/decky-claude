---
name: steam-debugger
description: >-
  Debug Steam games and the Steam client on a Steam Deck: crashes, games that
  won't launch, black screens, Proton/compatibility issues, performance
  problems, and controller/input trouble. Use at the start of any
  Steam/game-debugging session and whenever the user reports a game problem.
---

# Steam Debugger

You are debugging on a Steam Deck (SteamOS, Arch-based, immutable rootfs) in
Gaming Mode. You have MCP tools (`screenshot`, `send_key`, `type_text`,
`mouse_move_click`) plus normal shell access as the `deck` user.

## Workflow

1. **Look first.** Take a `screenshot` before anything else. Crash dialogs,
   black screens, and error overlays tell you more than logs alone.
2. **Identify the game and its state:**
   - Running processes: `pgrep -af "reaper|proton|wine|pressure-vessel"`
   - Steam's registry of the last game: `grep -i "RunningAppID" ~/.steam/registry.vdf`
   - App IDs map to install dirs via `~/.steam/steam/steamapps/appmanifest_*.acf`
     (`name` and `installdir` fields).
3. **Read the logs (newest first):**
   - Steam client: `~/.steam/steam/logs/` (esp. `console-linux.txt`,
     `compat_log.txt`, `content_log.txt`)
   - Per-game Proton: `~/.steam/steam/steamapps/compatdata/<appid>/pfx/` and
     `/tmp/proton_$USER/` if present
   - Full Proton log: only exists if launched with `PROTON_LOG=1` →
     `~/steam-<appid>.log`
   - System: `journalctl --user -n 200 --no-pager`, `dmesg | tail -50`
     (OOM kills, GPU resets)
4. **Form a hypothesis before changing anything.** State it to the user.
5. **Apply the least invasive fix first** (launch options → Proton version →
   shader-cache/prefix clear → config edits). One change at a time.
6. **Verify visually.** Relaunch and `screenshot` to confirm the result.

## Common fixes

- **Won't launch / instant exit:** set launch options `PROTON_LOG=1 %command%`,
  reproduce, read `~/steam-<appid>.log`. Look for missing DLLs, vcrun, or
  `wine: Unhandled page fault`.
- **Wrong/old Proton:** try Proton Experimental or the latest GE-Proton
  (in `~/.steam/root/compatibilitytools.d/`). Set per-game in
  Properties → Compatibility.
- **Corrupt shader cache:** delete
  `~/.steam/steam/steamapps/shadercache/<appid>/`.
- **Corrupt prefix:** back up then remove
  `~/.steam/steam/steamapps/compatdata/<appid>/` — Steam recreates it.
  Warn the user first: this resets non-cloud save data stored in the prefix.
- **Performance:** check thermals/throttling
  (`sensors`, `cat /sys/class/drm/card*/device/gpu_busy_percent`), suggest a
  frame cap or TDP limit via the Quick Access performance menu.
- **Out of disk:** `df -h /home` — shader caches and compatdata eat space.

## Rules

- Never modify the read-only rootfs or suggest `steamos-readonly disable`
  unless the user explicitly insists after being warned.
- Back up any file before editing it (`cp x x.bak`).
- Ask before deleting prefixes or save-adjacent data.
- Prefer per-game settings over global ones.
