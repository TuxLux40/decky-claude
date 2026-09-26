// Self-update: the plugin isn't in the Decky store, so Decky never offers
// updates for it. The backend asks GitHub for the latest release; installing
// goes through Decky's own installer (utilities/install_plugin — the same call
// the store makes), which shows Decky's confirmation prompt, verifies the
// sha256 and reloads the plugin.

import { ButtonItem, PanelSection, PanelSectionRow, ToggleField } from "@decky/ui";
import { callable } from "@decky/api";
import { useEffect, useState } from "react";

// Must equal plugin.json "name": Decky locates the installed copy by it.
const PLUGIN_NAME = "decky-claude";

// decky-loader frontend/src/plugin.ts: enum InstallType { INSTALL, REINSTALL,
// UPDATE, DOWNGRADE, OVERWRITE } — mirrored by backend browser.py.
const INSTALL_TYPE_UPDATE = 2;

const AUTO_FIRST_DELAY_MS = 30_000; // let Steam finish starting first
const AUTO_INTERVAL_MS = 30 * 60_000; // backend caches for 6h, so this is cheap

export interface UpdateInfo {
  current: string;
  latest: string | null;
  update_available: boolean;
  artifact: string | null;
  hash: string | null;
  release_url: string | null;
  checked_at: number;
  error: string | null;
  auto_update: boolean;
  session_active: boolean;
}

const getUpdateInfo = callable<[boolean], UpdateInfo>("get_update_info");
const setAutoUpdate = callable<
  [boolean],
  { success: boolean; auto_update?: boolean; error?: string }
>("set_auto_update");

interface DeckyBackendGlobal {
  call<Args extends unknown[] = unknown[], Ret = unknown>(route: string, ...args: Args): Promise<Ret>;
}

/** Ask Decky to install the release. Resolves once Decky shows its prompt. */
async function requestInstall(info: UpdateInfo): Promise<void> {
  const backend = (window as unknown as { DeckyBackend?: DeckyBackendGlobal }).DeckyBackend;
  if (!backend) throw new Error("Decky loader API not available");
  if (!info.artifact || !info.hash || !info.latest) throw new Error("No installable release");
  // decky-loader backend/decky_loader/utilities.py install_plugin(
  //   artifact, name, version, hash, install_type)
  await backend.call<[string, string, string, string, number]>(
    "utilities/install_plugin",
    info.artifact,
    PLUGIN_NAME,
    info.latest,
    info.hash,
    INSTALL_TYPE_UPDATE,
  );
}

// Only prompt once per version per Steam session: if the user cancels Decky's
// prompt we don't nag again until the next release (or a manual Install).
let promptedVersion: string | null = null;

async function autoUpdateTick() {
  try {
    const info = await getUpdateInfo(false);
    if (
      info.auto_update &&
      info.update_available &&
      !info.session_active &&
      info.latest &&
      promptedVersion !== info.latest
    ) {
      promptedVersion = info.latest;
      await requestInstall(info);
    }
  } catch (e) {
    console.warn("[decky-claude] auto-update check failed", e);
  }
}

/** Background auto-update loop. Returns a cleanup for onDismount. */
export function startAutoUpdate(): () => void {
  let interval: ReturnType<typeof setInterval> | undefined;
  const first = setTimeout(() => {
    autoUpdateTick();
    interval = setInterval(autoUpdateTick, AUTO_INTERVAL_MS);
  }, AUTO_FIRST_DELAY_MS);
  return () => {
    clearTimeout(first);
    if (interval) clearInterval(interval);
  };
}

export function UpdateSection() {
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [checking, setChecking] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  async function refresh(force: boolean) {
    setChecking(true);
    try {
      setInfo(await getUpdateInfo(force));
    } catch (e) {
      setMsg(`Update check failed: ${String(e)}`);
    } finally {
      setChecking(false);
    }
  }

  useEffect(() => {
    refresh(false);
  }, []);

  async function handleInstall() {
    if (!info) return;
    setMsg(null);
    try {
      promptedVersion = info.latest;
      await requestInstall(info);
    } catch (e) {
      setMsg(String(e));
    }
  }

  async function handleToggle(enabled: boolean) {
    setInfo((cur) => (cur ? { ...cur, auto_update: enabled } : cur));
    const r = await setAutoUpdate(enabled).catch((e) => ({ success: false, error: String(e) }));
    if (!r.success) {
      setMsg(`Could not save setting: ${r.error ?? "unknown error"}`);
      setInfo((cur) => (cur ? { ...cur, auto_update: !enabled } : cur));
    } else if (enabled && info?.update_available && !info.session_active) {
      handleInstall();
    }
  }

  const status = !info
    ? "Checking…"
    : info.update_available
      ? `Update available: v${info.latest}`
      : info.error
        ? info.error
        : info.latest
          ? "Up to date"
          : "No release found";

  return (
    <PanelSection title="Plugin Updates">
      <PanelSectionRow>
        <div style={{ fontSize: 12, lineHeight: 1.5 }}>
          <div>Installed: v{info?.current ?? "…"}</div>
          <div style={{ color: info?.update_available ? "#4caf50" : info?.error ? "#f0a500" : "#aaa" }}>
            {status}
          </div>
          {info?.update_available && info.session_active && (
            <div style={{ fontSize: 11, color: "#f0a500" }}>
              Updating reloads the plugin and ends the running session.
            </div>
          )}
        </div>
      </PanelSectionRow>

      {info?.update_available && (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={handleInstall}>
            Install v{info.latest}
          </ButtonItem>
        </PanelSectionRow>
      )}

      <PanelSectionRow>
        <ToggleField
          label="Auto-update"
          description="Install new releases automatically (Decky asks to confirm)"
          checked={info?.auto_update ?? true}
          disabled={!info}
          onChange={handleToggle}
        />
      </PanelSectionRow>

      <PanelSectionRow>
        <ButtonItem layout="below" disabled={checking} onClick={() => refresh(true)}>
          {checking ? "Checking…" : "Check for updates"}
        </ButtonItem>
      </PanelSectionRow>

      {msg && (
        <PanelSectionRow>
          <div style={{ fontSize: 11, color: "#f44336" }}>{msg}</div>
        </PanelSectionRow>
      )}
    </PanelSection>
  );
}
