# decky-claude — rules for anyone (human or agent) changing this repo

## Target user: a layman in Gaming Mode

The person using this plugin has never opened a terminal and only uses Steam's
Gaming Mode / Big Picture UI. Everything must work out of the box:

- No manual steps after installing from a release. If something needs setup,
  the plugin does it (or guides through it in the panel), not the README.
- Never touch user files the plugin didn't create. Anything written outside
  the plugin's own dirs (skill links, `.mcp.json`, CLAUDE.md blocks) must be
  recognisably ours and cleaned up without collateral — e.g. a session in
  `$HOME` links the skill at `~/.claude/skills/`, the user's own skills dir.
- Failures must explain themselves in the panel in plain language, with the
  fix — never a silent no-op or a raw stack trace.
- The panel stays minimal: anything the chat already handles (screenshots,
  the session link) doesn't get panel UI.

## Install & release

- The installed plugin is always the release-zip layout (`.github/workflows/release.yml`
  packaging step) — root-owned copy in `~/homebrew/plugins/decky-claude`.
  Never symlink it to a checkout: Decky chowns/chmods the target.
- Every push to `main` releases; installed copies self-update through Decky's
  own installer. Don't break the version/sha256 contract in `src/update.tsx`.
- The skill is the `vendor/skills` submodule; `skills/steam-debugger` is a
  symlink dereferenced at packaging time.

## Testing

- Hardware-test UI changes in Gaming Mode before calling them done. Steam
  marks D-pad focus with a `.gpfocus` class (not `:focus`), inline styles beat
  its focus CSS, and the QAM only scrolls to focusable elements.
- Frontend-only changes can be loaded by reloading Steam's UI
  (`location.reload()` in SharedJSContext); backend changes need
  `plugin_loader.service` restarted — which kills any session launched from
  the plugin. Call the `session_context` MCP tool to know which you are.
