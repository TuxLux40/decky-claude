import {
  ButtonItem,
  DropdownItem,
  PanelSection,
  PanelSectionRow,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin } from "@decky/api";
import { useEffect, useState } from "react";
import { FaTerminal } from "react-icons/fa";

// ── backend callables ──────────────────────────────────────────────────────────

const startSession = callable<
  [string],
  { success: boolean; url?: string; error?: string }
>("start_session");

const stopSession = callable<[], { success: boolean }>("stop_session");

const getStatus = callable<
  [],
  { status: string; url?: string; working_dir: string; error?: string }
>("get_status");

const listDirs = callable<[], { dirs: string[] }>("list_dirs");

// ── types ──────────────────────────────────────────────────────────────────────

type SessionStatus = "stopped" | "starting" | "running" | "error";

const STATUS_COLOR: Record<SessionStatus, string> = {
  stopped: "#888",
  starting: "#f0a500",
  running: "#4caf50",
  error: "#f44336",
};

// ── component ──────────────────────────────────────────────────────────────────

function Content() {
  const [status, setStatus] = useState<SessionStatus>("stopped");
  const [sessionUrl, setSessionUrl] = useState<string | null>(null);
  const [workingDir, setWorkingDir] = useState("/home/deck");
  const [dirs, setDirs] = useState<string[]>(["/home/deck"]);
  const [loading, setLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [urlCopied, setUrlCopied] = useState(false);

  // Initial data load
  useEffect(() => {
    listDirs().then((r) => setDirs(r.dirs));
    syncStatus();
  }, []);

  // Poll status every 3 s
  useEffect(() => {
    const id = setInterval(syncStatus, 3000);
    return () => clearInterval(id);
  }, []);

  async function syncStatus() {
    const r = await getStatus();
    setStatus(r.status as SessionStatus);
    setSessionUrl(r.url ?? null);
    if (r.error) setErrorMsg(r.error);
  }

  async function handleStart() {
    setLoading(true);
    setErrorMsg(null);
    try {
      const r = await startSession(workingDir);
      if (r.success) {
        setStatus("running");
        setSessionUrl(r.url ?? null);
      } else {
        setStatus("error");
        setErrorMsg(r.error ?? "Failed to start session");
      }
    } finally {
      setLoading(false);
    }
  }

  async function handleStop() {
    setLoading(true);
    try {
      await stopSession();
      setStatus("stopped");
      setSessionUrl(null);
      setErrorMsg(null);
    } finally {
      setLoading(false);
    }
  }

  function copyUrl() {
    if (!sessionUrl) return;
    // Steam Deck Gaming Mode doesn't have navigator.clipboard — use execCommand
    const el = document.createElement("textarea");
    el.value = sessionUrl;
    document.body.appendChild(el);
    el.select();
    document.execCommand("copy");
    document.body.removeChild(el);
    setUrlCopied(true);
    setTimeout(() => setUrlCopied(false), 2000);
  }

  const isRunning = status === "running" || status === "starting";
  const statusColor = STATUS_COLOR[status] ?? "#888";
  const statusLabel =
    status === "starting"
      ? "Starting…"
      : status.charAt(0).toUpperCase() + status.slice(1);

  const dirOptions = dirs.map((d) => ({ data: d, label: d }));

  return (
    <>
      {/* ── Status ── */}
      <PanelSection title="Claude Code Remote">
        <PanelSectionRow>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <div
              style={{
                width: 10,
                height: 10,
                borderRadius: "50%",
                background: statusColor,
                flexShrink: 0,
              }}
            />
            <span style={{ color: statusColor, fontWeight: 600 }}>
              {statusLabel}
            </span>
          </div>
        </PanelSectionRow>

        {/* ── Session URL ── */}
        {sessionUrl && (
          <>
            <PanelSectionRow>
              <div
                style={{
                  fontSize: 11,
                  wordBreak: "break-all",
                  color: "#5ba3f5",
                  lineHeight: 1.4,
                  background: "rgba(91,163,245,0.08)",
                  borderRadius: 6,
                  padding: "6px 8px",
                }}
              >
                {sessionUrl}
              </div>
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={copyUrl}>
                {urlCopied ? "Copied!" : "Copy Session URL"}
              </ButtonItem>
            </PanelSectionRow>
            <PanelSectionRow>
              <div style={{ fontSize: 11, color: "#aaa", lineHeight: 1.5 }}>
                Open the Claude app on Android → Code tab → connect to this
                session
              </div>
            </PanelSectionRow>
          </>
        )}

        {/* Waiting indicator */}
        {status === "starting" && !sessionUrl && (
          <PanelSectionRow>
            <div style={{ fontSize: 11, color: "#f0a500" }}>
              Waiting for session URL…
            </div>
          </PanelSectionRow>
        )}

        {/* Error */}
        {errorMsg && (
          <PanelSectionRow>
            <div
              style={{
                fontSize: 11,
                color: "#f44336",
                wordBreak: "break-all",
                background: "rgba(244,67,54,0.08)",
                borderRadius: 6,
                padding: "6px 8px",
              }}
            >
              {errorMsg}
            </div>
          </PanelSectionRow>
        )}
      </PanelSection>

      {/* ── Session controls ── */}
      <PanelSection title="Session">
        {!isRunning && (
          <PanelSectionRow>
            <DropdownItem
              label="Working Directory"
              description={workingDir}
              rgOptions={dirOptions}
              selectedOption={workingDir}
              onChange={(opt) => setWorkingDir(opt.data)}
            />
          </PanelSectionRow>
        )}

        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={isRunning ? handleStop : handleStart}
            disabled={loading}
          >
            {loading
              ? isRunning
                ? "Stopping…"
                : "Starting…"
              : isRunning
              ? "Stop Session"
              : "Start Remote Session"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      {/* ── Setup hint ── */}
      {status === "error" &&
        errorMsg?.toLowerCase().includes("not found") && (
          <PanelSection title="Setup">
            <PanelSectionRow>
              <div style={{ fontSize: 11, color: "#aaa", lineHeight: 1.6 }}>
                Claude Code is not installed. In Desktop Mode, open a terminal
                and run:
                {"\n\n"}
                <code
                  style={{
                    background: "rgba(255,255,255,0.08)",
                    borderRadius: 4,
                    padding: "2px 6px",
                    display: "block",
                    marginTop: 4,
                  }}
                >
                  npm install -g @anthropic-ai/claude-code
                </code>
              </div>
            </PanelSectionRow>
          </PanelSection>
        )}
    </>
  );
}

// ── plugin entry ───────────────────────────────────────────────────────────────

export default definePlugin(() => ({
  title: <div className={staticClasses.Title}>Claude Code</div>,
  icon: <FaTerminal />,
  content: <Content />,
  onDismount() {},
}));
