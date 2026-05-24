import {
  ButtonItem,
  DropdownItem,
  PanelSection,
  PanelSectionRow,
  SliderField,
  staticClasses,
  ToggleField,
} from "@decky/ui";
import { callable, definePlugin } from "@decky/api";
import { useEffect, useState } from "react";
import { FaTerminal } from "react-icons/fa";

// ── backend callables ──────────────────────────────────────────────────────────

const startSession = callable<[string], { success: boolean; url?: string; error?: string }>("start_session");
const stopSession = callable<[], { success: boolean }>("stop_session");
const getStatus = callable<[], { status: string; url?: string; working_dir: string; error?: string }>("get_status");
const listDirs = callable<[], { dirs: string[] }>("list_dirs");

const captureScreenshot = callable<[], { success: boolean; path?: string; thumbnail?: string; error?: string }>("capture_screenshot");
const startAutoCapture = callable<[number], { success: boolean }>("start_auto_capture");
const stopAutoCapture = callable<[], { success: boolean }>("stop_auto_capture");
const getScreenState = callable<[], { auto_active: boolean; interval: number; thumbnail: string | null }>("get_screen_state");

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

const INTERVAL_OPTIONS = [
  { data: 5, label: "5 s" },
  { data: 10, label: "10 s" },
  { data: 30, label: "30 s" },
  { data: 60, label: "60 s" },
];

// ── sub-components ─────────────────────────────────────────────────────────────

function StatusDot({ color }: { color: string }) {
  return (
    <div
      style={{
        width: 10,
        height: 10,
        borderRadius: "50%",
        background: color,
        flexShrink: 0,
        boxShadow: `0 0 6px ${color}`,
      }}
    />
  );
}

// ── main component ─────────────────────────────────────────────────────────────

function Content() {
  // session
  const [status, setStatus] = useState<SessionStatus>("stopped");
  const [sessionUrl, setSessionUrl] = useState<string | null>(null);
  const [workingDir, setWorkingDir] = useState("/home/deck");
  const [dirs, setDirs] = useState<string[]>(["/home/deck"]);
  const [sessionLoading, setSessionLoading] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [urlCopied, setUrlCopied] = useState(false);

  // screen
  const [thumbnail, setThumbnail] = useState<string | null>(null);
  const [autoCapture, setAutoCapture] = useState(false);
  const [captureInterval, setCaptureInterval] = useState(10);
  const [captureLoading, setCaptureLoading] = useState(false);
  const [captureError, setCaptureError] = useState<string | null>(null);

  // input
  const [typeText, setTypeText] = useState("");
  const [inputFeedback, setInputFeedback] = useState<{ msg: string; ok: boolean } | null>(null);

  // ── init ──
  useEffect(() => {
    listDirs().then((r) => setDirs(r.dirs));
    syncStatus();
    getScreenState().then((r) => {
      setAutoCapture(r.auto_active);
      setCaptureInterval(r.interval);
      if (r.thumbnail) setThumbnail(r.thumbnail);
    });
  }, []);

  // ── status poll ──
  useEffect(() => {
    const id = setInterval(syncStatus, 3000);
    return () => clearInterval(id);
  }, []);

  async function syncStatus() {
    const r = await getStatus();
    setStatus(r.status as SessionStatus);
    setSessionUrl(r.url ?? null);
    if (r.error) setSessionError(r.error);
  }

  // ── session handlers ──
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

  // ── screen handlers ──
  async function handleCapture() {
    setCaptureLoading(true);
    setCaptureError(null);
    try {
      const r = await captureScreenshot();
      if (r.success) {
        if (r.thumbnail) setThumbnail(r.thumbnail);
      } else {
        setCaptureError(r.error ?? "Capture failed");
      }
    } finally {
      setCaptureLoading(false);
    }
  }

  async function handleAutoToggle(enabled: boolean) {
    setAutoCapture(enabled);
    if (enabled) {
      await startAutoCapture(captureInterval);
    } else {
      await stopAutoCapture();
    }
    // Refresh thumbnail state
    getScreenState().then((r) => {
      if (r.thumbnail) setThumbnail(r.thumbnail);
    });
  }

  async function handleIntervalChange(val: { data: number }) {
    setCaptureInterval(val.data);
    if (autoCapture) {
      await startAutoCapture(val.data);
    }
  }

  // ── input handlers ──
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
      {/* ── Remote Session ── */}
      <PanelSection title="Claude Code Remote">
        <PanelSectionRow>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <StatusDot color={statusColor} />
            <span style={{ color: statusColor, fontWeight: 600 }}>{statusLabel}</span>
          </div>
        </PanelSectionRow>

        {sessionUrl && (
          <>
            <PanelSectionRow>
              <div
                style={{
                  fontSize: 11,
                  wordBreak: "break-all",
                  color: "#5ba3f5",
                  background: "rgba(91,163,245,0.08)",
                  borderRadius: 6,
                  padding: "6px 8px",
                  lineHeight: 1.4,
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
              <div style={{ fontSize: 11, color: "#aaa" }}>
                Open Claude app → Code tab → connect
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
              <code
                style={{
                  display: "block",
                  marginTop: 4,
                  background: "rgba(255,255,255,0.07)",
                  borderRadius: 4,
                  padding: "2px 6px",
                }}
              >
                npm install -g @anthropic-ai/claude-code
              </code>
            </div>
          </PanelSectionRow>
        )}
      </PanelSection>

      {/* ── Screen Capture ── */}
      <PanelSection title="Screen">
        {thumbnail && (
          <PanelSectionRow>
            <img
              src={`data:image/png;base64,${thumbnail}`}
              style={{
                width: "100%",
                borderRadius: 6,
                border: "1px solid rgba(255,255,255,0.1)",
                display: "block",
              }}
              alt="Last capture"
            />
          </PanelSectionRow>
        )}

        <PanelSectionRow>
          <ButtonItem layout="below" onClick={handleCapture} disabled={captureLoading}>
            {captureLoading ? "Capturing…" : "Capture Screen Now"}
          </ButtonItem>
        </PanelSectionRow>

        {captureError && (
          <PanelSectionRow>
            <div style={{ fontSize: 11, color: "#f44336" }}>{captureError}</div>
          </PanelSectionRow>
        )}

        <PanelSectionRow>
          <ToggleField
            label="Auto-capture"
            description={`Saves screen_latest.png every ${captureInterval} s`}
            checked={autoCapture}
            onChange={handleAutoToggle}
          />
        </PanelSectionRow>

        {autoCapture && (
          <PanelSectionRow>
            <DropdownItem
              label="Interval"
              rgOptions={INTERVAL_OPTIONS}
              selectedOption={captureInterval}
              onChange={handleIntervalChange}
            />
          </PanelSectionRow>
        )}

        <PanelSectionRow>
          <div style={{ fontSize: 11, color: "#666" }}>
            Screenshots saved to your working directory — Claude can read them directly
          </div>
        </PanelSectionRow>
      </PanelSection>

      {/* ── Input ── */}
      <PanelSection title="Input">
        <PanelSectionRow>
          <div
            style={{
              fontSize: 11,
              color: "#f0a500",
              background: "rgba(240,165,0,0.08)",
              borderRadius: 6,
              padding: "5px 8px",
            }}
          >
            ⚠ Input goes directly to the focused app
          </div>
        </PanelSectionRow>

        {/* quick keys */}
        <PanelSectionRow>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {QUICK_KEYS.map(({ label, key }) => (
              <button
                key={key}
                onClick={() => handleKey(key)}
                style={{
                  background: "rgba(255,255,255,0.1)",
                  border: "1px solid rgba(255,255,255,0.2)",
                  borderRadius: 6,
                  color: "#fff",
                  padding: "4px 10px",
                  fontSize: 12,
                  cursor: "pointer",
                  flex: "1 1 auto",
                }}
              >
                {label}
              </button>
            ))}
          </div>
        </PanelSectionRow>

        {/* mouse buttons */}
        <PanelSectionRow>
          <div style={{ display: "flex", gap: 6 }}>
            {[
              { label: "Left Click", btn: 1 },
              { label: "Right Click", btn: 3 },
            ].map(({ label, btn }) => (
              <button
                key={btn}
                onClick={() => handleClick(btn)}
                style={{
                  background: "rgba(255,255,255,0.1)",
                  border: "1px solid rgba(255,255,255,0.2)",
                  borderRadius: 6,
                  color: "#fff",
                  padding: "4px 10px",
                  fontSize: 12,
                  cursor: "pointer",
                  flex: 1,
                }}
              >
                {label}
              </button>
            ))}
          </div>
        </PanelSectionRow>

        {/* text input */}
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
                borderRadius: 6,
                color: "#fff",
                padding: "5px 8px",
                fontSize: 12,
              }}
            />
            <button
              onClick={handleType}
              disabled={!typeText}
              style={{
                background: typeText ? "rgba(91,163,245,0.3)" : "rgba(255,255,255,0.05)",
                border: "1px solid rgba(91,163,245,0.4)",
                borderRadius: 6,
                color: "#fff",
                padding: "5px 12px",
                fontSize: 12,
                cursor: typeText ? "pointer" : "default",
              }}
            >
              Send
            </button>
          </div>
        </PanelSectionRow>

        {inputFeedback && (
          <PanelSectionRow>
            <div
              style={{
                fontSize: 11,
                color: inputFeedback.ok ? "#4caf50" : "#f44336",
                padding: "2px 0",
              }}
            >
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
  title: <div className={staticClasses.Title}>Claude Code</div>,
  icon: <FaTerminal />,
  content: <Content />,
  onDismount() {},
}));
