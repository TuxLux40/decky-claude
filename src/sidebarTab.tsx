// Dedicated Quick Access sidebar tab for the plugin panel.
//
// STABILITY RISK: Decky has no public API for sidebar tabs; this wraps the
// loader's internal TabsHook.render (decky-loader frontend/src/tabs-hook.tsx,
// reached via window.DeckyPluginLoader.tabsHook) and may break on a Decky update.
//
// Why wrap render() instead of calling tabsHook.add()/removeById(): Decky's
// render only pushes its tabs when the number of `decky`-marked entries in
// Steam's (memoised, reused) tab array differs from its own list length, and
// it never removes stale ones. Adding or removing a tab after the QAM has
// rendered once therefore re-pushes every Decky tab into the same array,
// duplicating them. Here Decky's own list is never touched: our entry is
// stripped before Decky's render runs (so its count guard stays consistent)
// and appended afterwards, which makes add/remove/reload idempotent.
import { ErrorBoundary, staticClasses, ToggleField } from "@decky/ui";
import { callable } from "@decky/api";
import { ReactNode, useEffect, useState } from "react";
import { SiClaude } from "react-icons/si";

const getSidebarTab = callable<[], { enabled: boolean }>("get_sidebar_tab");
const setSidebarTab = callable<[boolean], { enabled: boolean }>("set_sidebar_tab");

// Arbitrary key, clear of Steam's QuickAccessTab ids and Decky's own (999).
const TAB_KEY = 0x434c4144;
// Survives a plugin reload (new bundle, same window) so the previous
// instance can be torn down before a new one installs.
const GLOBAL_KEY = Symbol.for("decky-claude.sidebar-tab.v1");
const LOG = "[decky-claude sidebar-tab]";

type RenderedTab = { key?: unknown; [k: string]: unknown };
type RenderFn = (this: unknown, tabs: RenderedTab[], visible: boolean) => unknown;
type TabsHook = { render: RenderFn };

// ── tiny observable store (QAM visibility + toggle state) ────────────────────

interface State {
  available: boolean; // internals found and patched
  enabled: boolean; // user setting
  visible: boolean; // QAM currently open
}

let state: State = { available: false, enabled: false, visible: false };
const listeners = new Set<() => void>();

function update(patch: Partial<State>) {
  const next = { ...state, ...patch };
  if (next.available === state.available && next.enabled === state.enabled && next.visible === state.visible) {
    return;
  }
  state = next;
  // Deferred: update() runs inside Steam's QAM render, and setState on other
  // components mid-render makes React warn.
  queueMicrotask(() => listeners.forEach((l) => l()));
}

function useStore(): State {
  const [s, setS] = useState(state);
  useEffect(() => {
    const l = () => setS(state);
    listeners.add(l);
    l();
    return () => {
      listeners.delete(l);
    };
  }, []);
  return s;
}

// Decky only mounts plugin content while the QAM is open; do the same so the
// panel's polling does not run in the background.
function VisibleGate({ children }: { children: ReactNode }) {
  const { visible } = useStore();
  return <>{visible ? children : null}</>;
}

// ── patch lifecycle ──────────────────────────────────────────────────────────

function findHook(): TabsHook | null {
  const w = window as any;
  const hook = w.DeckyPluginLoader?.tabsHook ?? w.__TABS_HOOK_INSTANCE;
  return hook && typeof hook.render === "function" ? (hook as TabsHook) : null;
}

function install(content: ReactNode): (() => void) | null {
  const hook = findHook();
  if (!hook) {
    console.warn(LOG, "Decky TabsHook not found; sidebar tab unavailable");
    return null;
  }

  const hadOwn = Object.prototype.hasOwnProperty.call(hook, "render");
  const previous = hook.render;
  let active = true;

  const tab: RenderedTab = {
    key: TAB_KEY,
    title: null,
    tab: <SiClaude />,
    panel: (
      <ErrorBoundary>
        <VisibleGate>
          <div className={staticClasses.Title} style={{ paddingTop: 3, boxShadow: "unset" }}>
            Claude Code
          </div>
          <div style={{ paddingTop: 16 }}>{content}</div>
        </VisibleGate>
      </ErrorBoundary>
    ),
  };

  const strip = (tabs: RenderedTab[]) => {
    for (let i = tabs.length - 1; i >= 0; i--) {
      if (tabs[i]?.key === TAB_KEY) tabs.splice(i, 1);
    }
  };

  const wrapper: RenderFn = function (this: unknown, tabs, visible) {
    if (!active || !Array.isArray(tabs) || Object.isFrozen(tabs)) {
      return previous.call(this, tabs, visible);
    }
    // Steam renders the QAM without an error boundary above this hook, so a
    // throw of ours must never escape: fall back to Decky's plain render.
    try {
      strip(tabs);
    } catch (e) {
      console.error(LOG, "strip failed", e);
    }
    const result = previous.call(this, tabs, visible);
    try {
      update({ visible });
      if (state.enabled) tabs.push(tab);
    } catch (e) {
      console.error(LOG, "append failed", e);
    }
    return result;
  };

  try {
    Object.defineProperty(hook, "render", { configurable: true, writable: true, value: wrapper });
  } catch (e) {
    console.warn(LOG, "could not patch TabsHook.render", e);
    return null;
  }

  return () => {
    active = false; // if someone wrapped on top of us, we become a pass-through
    if (hook.render === wrapper) {
      if (hadOwn) hook.render = previous;
      else delete (hook as Partial<TabsHook>).render;
    }
  };
}

// ── public API ───────────────────────────────────────────────────────────────

/** Patch the QAM and load the persisted toggle. Call once on plugin load. */
export function initSidebarTab(content: ReactNode) {
  const w = window as any;
  try {
    w[GLOBAL_KEY]?.();
  } catch (e) {
    console.error(LOG, "previous instance cleanup failed", e);
  }
  let uninstall: (() => void) | null = null;
  try {
    uninstall = install(content);
  } catch (e) {
    console.error(LOG, "install failed", e);
  }
  const dispose = () => {
    uninstall?.();
    uninstall = null;
    if (w[GLOBAL_KEY] === dispose) delete w[GLOBAL_KEY];
    update({ available: false, enabled: false, visible: false });
  };
  w[GLOBAL_KEY] = dispose;
  update({ available: uninstall !== null });
  if (!uninstall) return;

  getSidebarTab()
    .then((r) => update({ enabled: r.enabled !== false }))
    .catch((e) => {
      console.error(LOG, "could not read setting, defaulting to on", e);
      update({ enabled: true });
    });
}

/** Remove the patch and our tab. Call from onDismount. */
export function disposeSidebarTab() {
  (window as any)[GLOBAL_KEY]?.();
}

/** "Show in Quick Access sidebar" toggle for the panel. */
export function SidebarTabToggle() {
  const { available, enabled } = useStore();
  const [error, setError] = useState<string | null>(null);

  async function onChange(on: boolean) {
    setError(null);
    update({ enabled: on });
    try {
      await setSidebarTab(on);
    } catch (e) {
      console.error(LOG, "could not save setting", e);
      setError("Could not save setting");
    }
  }

  return (
    <>
      <ToggleField
        label="Show in Quick Access sidebar"
        description={
          available
            ? "Adds a Claude Code icon to the Quick Access sidebar. Applies the next time the menu opens."
            : "Unavailable with this Decky Loader version."
        }
        checked={available && enabled}
        disabled={!available}
        onChange={onChange}
      />
      {error && <div style={{ fontSize: 11, color: "#f44336" }}>{error}</div>}
    </>
  );
}
