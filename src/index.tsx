import {
  ButtonItem,
  DropdownItem,
  PanelSection,
  PanelSectionRow,
  staticClasses,
  ToggleField,
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
  { status: string; url?: string; working_dir: string; error?: string; skill?: string | null }
>("get_status");
const listDirs = callable<[], { dirs: string[] }>("list_dirs");

const captureScreenshot = callable<
  [],
  { success: boolean; path?: string; thumbnail?: string; error?: string }
>("capture_screenshot");
const getScreenState = callable<[], { thumbnail: string | null }>("get_screen_state");

const sendKey = callable<[string], { success: boolean; error?: string }>("send_key");
const sendText = callable<[string], { success: boolean; error?: string }>("send_text");
const sendMouseClick = callable<[number], { success: boolean; error?: string }>("send_mouse_click");

// ── types ──────────────────────────────────────────────────────────────────────

type SessionStatus = "stopped" | "starting" | "running" | "error";

const STATUS_COLOR: Record<SessionStatus, string> = {
  stopped: "#888",
  starting: "#f0a500",
  running: "#4caf50",
  error: "#f44336",
};

const QUICK_KEYS = [
  { label: "Esc", key: "escape" },
  { label: "Enter", key: "Return" },
  { label: "Space", key: "space" },
  { label: "Tab", key: "Tab" },
];

// ── component ──────────────────────────────────────────────────────────────────

function Content() {
  // session
  const [status, setStatus] = useState<SessionStatus>("stopped");
  const [sessionUrl, setSessionUrl] = useState<string | null>(null);
  const [workingDir, setWorkingDir] = useState("/home/deck");
  const [dirs, setDirs] = useState<string[]>(["/home/deck"]);
  const [sessionLoading, setSessionLoading] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [urlCopied, setUrlCopied] = useState(false);
  const [skill, setSkill] = useState<string | null>(null);

  // screen
  const [thumbnail, setThumbnail] = useState<string | null>(null);
  const [captureLoading, setCaptureLoading] = useState(false);
  const [captureError, setCaptureError] = useState<string | null>(null);

  // input
  const [typeText, setTypeText] = useState("");
  const [inputFeedback, setInputFeedback] = useState<{ msg: string; ok: boolean } | null>(null);

  // ── init ──
  useEffect(() => {
    listDirs().then((r) => setDirs(r.dirs));
    syncStatus();
    getScreenState().then((r) => { if (r.thumbnail) setThumbnail(r.thumbnail); });
  }, []);

  useEffect(() => {
    const id = setInterval(syncStatus, 3000);
    return () => clearInterval(id);
  }, []);

  async function syncStatus() {
    const r = await getStatus();
    setStatus(r.status as SessionStatus);
    setSessionUrl(r.url ?? null);
    setSkill(r.skill ?? null);
    if (r.error) setSessionError(r.error);
  }

  // ── session ──
  async function handleStartSession() {
    setSessionLoading(true);
    setSessionError(null);
    try {
      const r = await startSession(workingDir);
      if (r.success) {
        setStatus("running");
        setSessionUrl(r.url ?? null);
      } else {
        setStatus("error");
        setSessionError(r.error ?? "Failed to start session");
      }
    } finally {
      setSessionLoading(false);
    }
  }

  async function handleStopSession() {
    setSessionLoading(true);
    try {
      await stopSession();
      setStatus("stopped");
      setSessionUrl(null);
      setSessionError(null);
    } finally {
      setSessionLoading(false);
    }
  }

  function copyUrl() {
    if (!sessionUrl) return;
    const el = document.createElement("textarea");
    el.value = sessionUrl;
    document.body.appendChild(el);
    el.select();
    document.execCommand("copy");
    document.body.removeChild(el);
    setUrlCopied(true);
    setTimeout(() => setUrlCopied(false), 2000);
  }

  // ── screen ──
  async function handleCapture() {
    setCaptureLoading(true);
    setCaptureError(null);
    try {
      const r = await captureScreenshot();
      if (r.success && r.thumbnail) {
        setThumbnail(r.thumbnail);
      } else if (!r.success) {
        setCaptureError(r.error ?? "Capture failed");
      }
    } finally {
      setCaptureLoading(false);
    }
  }

  // ── input ──
  function showFeedback(msg: string, ok: boolean) {
    setInputFeedback({ msg, ok });
    setTimeout(() => setInputFeedback(null), 2000);
  }

  async function handleKey(key: string) {
    const r = await sendKey(key);
    showFeedback(r.success ? `Sent: ${key}` : (r.error ?? "Failed"), r.success);
  }

  async function handleType() {
    if (!typeText) return;
    const r = await sendText(typeText);
    showFeedback(r.success ? "Typed!" : (r.error ?? "Failed"), r.success);
    if (r.success) setTypeText("");
  }

  async function handleClick(button: number) {
    const r = await sendMouseClick(button);
    showFeedback(r.success ? "Clicked" : (r.error ?? "Failed"), r.success);
  }

  const isRunning = status === "running" || status === "starting";
  const statusColor = STATUS_COLOR[status] ?? "#888";
  const statusLabel =
    status === "starting" ? "Starting…" : status.charAt(0).toUpperCase() + status.slice(1);

  return (
    <>
      {/* ── Remote Session ─────────────────────────────────────────────── */}
      <PanelSection title="Claude Code Remote">
        <PanelSectionRow>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <div
              style={{
                width: 10, height: 10, borderRadius: "50%",
                background: statusColor, flexShrink: 0,
                boxShadow: `0 0 6px ${statusColor}`,
              }}
            />
            <span style={{ color: statusColor, fontWeight: 600 }}>{statusLabel}</span>
          </div>
        </PanelSectionRow>

        {isRunning && (
          <PanelSectionRow>
            <div style={{ fontSize: 11, color: skill ? "#4caf50" : "#f0a500" }}>
              {skill
                ? `Skill loaded: ${skill}`
                : "steam-debugger skill not found (~/.claude/skills)"}
            </div>
          </PanelSectionRow>
        )}

        {sessionUrl && (
          <>
            <PanelSectionRow>
              <div style={{
                fontSize: 11, wordBreak: "break-all", color: "#5ba3f5",
                background: "rgba(91,163,245,0.08)", borderRadius: 6,
                padding: "6px 8px", lineHeight: 1.4,
              }}>
                {sessionUrl}
              </div>
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={copyUrl}>
                {urlCopied ? "Copied!" : "Copy Session URL"}
              </ButtonItem>
            </PanelSectionRow>
            <PanelSectionRow>
              <div style={{ fontSize: 11, color: "#aaa" }}>
                Claude app → Code tab → connect. Claude will screenshot automatically when you ask about the game.
              </div>
            </PanelSectionRow>
          </>
        )}

        {status === "starting" && !sessionUrl && (
          <PanelSectionRow>
            <div style={{ fontSize: 11, color: "#f0a500" }}>Waiting for session URL…</div>
          </PanelSectionRow>
        )}

        {sessionError && (
          <PanelSectionRow>
            <div style={{
              fontSize: 11, color: "#f44336", wordBreak: "break-all",
              background: "rgba(244,67,54,0.08)", borderRadius: 6, padding: "6px 8px",
            }}>
              {sessionError}
            </div>
          </PanelSectionRow>
        )}

        {!isRunning && (
          <PanelSectionRow>
            <DropdownItem
              label="Working Directory"
              description={workingDir}
              rgOptions={dirs.map((d) => ({ data: d, label: d }))}
              selectedOption={workingDir}
              onChange={(opt) => setWorkingDir(opt.data)}
            />
          </PanelSectionRow>
        )}

        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={isRunning ? handleStopSession : handleStartSession}
            disabled={sessionLoading}
          >
            {sessionLoading
              ? isRunning ? "Stopping…" : "Starting…"
              : isRunning ? "Stop Session" : "Start Remote Session"}
          </ButtonItem>
        </PanelSectionRow>

        {sessionError?.toLowerCase().includes("not found") && (
          <PanelSectionRow>
            <div style={{ fontSize: 11, color: "#aaa", lineHeight: 1.6 }}>
              In Desktop Mode, run:
              <code style={{
                display: "block", marginTop: 4,
                background: "rgba(255,255,255,0.07)",
                borderRadius: 4, padding: "2px 6px",
              }}>
                npm install -g @anthropic-ai/claude-code
              </code>
            </div>
          </PanelSectionRow>
        )}
      </PanelSection>

      {/* ── Screen Preview ──────────────────────────────────────────────── */}
      <PanelSection title="Screen Preview">
        {thumbnail && (
          <PanelSectionRow>
            <img
              src={`data:image/png;base64,${thumbnail}`}
              style={{
                width: "100%", borderRadius: 6, display: "block",
                border: "1px solid rgba(255,255,255,0.1)",
              }}
              alt="Last capture"
            />
          </PanelSectionRow>
        )}

        <PanelSectionRow>
          <ButtonItem layout="below" onClick={handleCapture} disabled={captureLoading}>
            {captureLoading ? "Capturing…" : "Capture Screen"}
          </ButtonItem>
        </PanelSectionRow>

        {captureError && (
          <PanelSectionRow>
            <div style={{ fontSize: 11, color: "#f44336" }}>{captureError}</div>
          </PanelSectionRow>
        )}

        <PanelSectionRow>
          <div style={{ fontSize: 11, color: "#555" }}>
            Claude captures automatically when you message it. This button is for your own preview.
          </div>
        </PanelSectionRow>
      </PanelSection>

      {/* ── Manual Input ────────────────────────────────────────────────── */}
      <PanelSection title="Manual Input">
        <PanelSectionRow>
          <div style={{
            fontSize: 11, color: "#f0a500",
            background: "rgba(240,165,0,0.08)",
            borderRadius: 6, padding: "5px 8px",
          }}>
            ⚠ Input goes directly to the focused app
          </div>
        </PanelSectionRow>

        <PanelSectionRow>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {QUICK_KEYS.map(({ label, key }) => (
              <button
                key={key}
                onClick={() => handleKey(key)}
                style={{
                  background: "rgba(255,255,255,0.1)",
                  border: "1px solid rgba(255,255,255,0.2)",
                  borderRadius: 6, color: "#fff",
                  padding: "4px 10px", fontSize: 12,
                  cursor: "pointer", flex: "1 1 auto",
                }}
              >
                {label}
              </button>
            ))}
          </div>
        </PanelSectionRow>

        <PanelSectionRow>
          <div style={{ display: "flex", gap: 6 }}>
            {[{ label: "Left Click", btn: 1 }, { label: "Right Click", btn: 3 }].map(
              ({ label, btn }) => (
                <button
                  key={btn}
                  onClick={() => handleClick(btn)}
                  style={{
                    background: "rgba(255,255,255,0.1)",
                    border: "1px solid rgba(255,255,255,0.2)",
                    borderRadius: 6, color: "#fff",
                    padding: "4px 10px", fontSize: 12,
                    cursor: "pointer", flex: 1,
                  }}
                >
                  {label}
                </button>
              )
            )}
          </div>
        </PanelSectionRow>

        <PanelSectionRow>
          <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <input
              type="text"
              value={typeText}
              onChange={(e) => setTypeText(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleType()}
              placeholder="Type text…"
              style={{
                flex: 1,
                background: "rgba(255,255,255,0.07)",
                border: "1px solid rgba(255,255,255,0.2)",
                borderRadius: 6, color: "#fff",
                padding: "5px 8px", fontSize: 12,
              }}
            />
            <button
              onClick={handleType}
              disabled={!typeText}
              style={{
                background: typeText ? "rgba(91,163,245,0.3)" : "rgba(255,255,255,0.05)",
                border: "1px solid rgba(91,163,245,0.4)",
                borderRadius: 6, color: "#fff",
                padding: "5px 12px", fontSize: 12,
                cursor: typeText ? "pointer" : "default",
              }}
            >
              Send
            </button>
          </div>
        </PanelSectionRow>

        {inputFeedback && (
          <PanelSectionRow>
            <div style={{
              fontSize: 11,
              color: inputFeedback.ok ? "#4caf50" : "#f44336",
            }}>
              {inputFeedback.msg}
            </div>
          </PanelSectionRow>
        )}
      </PanelSection>
    </>
  );
}

// ── plugin entry ───────────────────────────────────────────────────────────────

export default definePlugin(() => ({
  name: "Claude Code",
  title: <div className={staticClasses.Title}>Claude Code</div>,
  icon: <FaTerminal />,
  content: <Content />,
  onDismount() {},
}));
