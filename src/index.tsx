import {
  ButtonItem,
  DropdownItem,
  PanelSection,
  PanelSectionRow,
  staticClasses,
  TextField,
  ToggleField,
} from "@decky/ui";
import { callable, definePlugin } from "@decky/api";
import { useEffect, useState } from "react";
import { FaTerminal } from "react-icons/fa";
import qrcode from "qrcode-generator";

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

const getAuth = callable<
  [],
  { logged_in: boolean; email?: string; org?: string; plan?: string; error?: string }
>("get_auth");
const startLogin = callable<[], { success: boolean; url?: string; error?: string }>("start_login");
const submitLoginCode = callable<
  [string],
  { success: boolean; email?: string; error?: string }
>("submit_login_code");
const cancelLogin = callable<[], { success: boolean }>("cancel_login");

const listSessions = callable<
  [],
  { sessions: MachineSession[]; error?: string }
>("list_sessions");

// ── types ──────────────────────────────────────────────────────────────────────

type SessionStatus = "stopped" | "starting" | "running" | "error";

interface MachineSession {
  id: string;
  short_id: string;
  cwd: string;
  label: string;
  mtime: number;
  live: boolean;
  current: boolean;
}

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

function relativeTime(epochSeconds: number): string {
  const mins = Math.max(0, Math.floor(Date.now() / 1000 - epochSeconds) / 60);
  if (mins < 1) return "just now";
  if (mins < 60) return `${Math.floor(mins)}m ago`;
  if (mins < 60 * 24) return `${Math.floor(mins / 60)}h ago`;
  return `${Math.floor(mins / 60 / 24)}d ago`;
}

/** QR as a single scaled bitmap.
 *
 *  An OAuth URL is ~450 chars, which needs a 73x73 matrix — rendering that as
 *  a div grid would be 5000+ DOM nodes for CEF to lay out. createDataURL emits
 *  one GIF instead; upscaling it with pixelated rendering keeps the module
 *  edges hard, which is what the phone camera needs.
 */
function QrCode({ text, size = 260 }: { text: string; size?: number }) {
  // Type 0 auto-picks the smallest version that fits; "L" adds the least
  // redundancy, so the matrix stays as small (and each module as large) as
  // possible for a long URL.
  const qr = qrcode(0, "L");
  qr.addData(text);
  qr.make();
  const src = qr.createDataURL(8, 1);

  return (
    <div style={{ background: "#fff", padding: 10, borderRadius: 6, margin: "0 auto" }}>
      <img
        src={src}
        width={size}
        height={size}
        style={{ display: "block", imageRendering: "pixelated" }}
        alt="Sign-in QR code"
      />
    </div>
  );
}

// ── component ──────────────────────────────────────────────────────────────────

function Content() {
  // session
  const [status, setStatus] = useState<SessionStatus>("stopped");
  const [sessionUrl, setSessionUrl] = useState<string | null>(null);
  const [workingDir, setWorkingDir] = useState("");
  const [dirs, setDirs] = useState<string[]>([]);
  const [sessionLoading, setSessionLoading] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [urlCopied, setUrlCopied] = useState(false);
  const [skill, setSkill] = useState<string | null>(null);

  // auth
  const [loggedIn, setLoggedIn] = useState<boolean | null>(null);
  const [account, setAccount] = useState<string | null>(null);
  const [plan, setPlan] = useState<string | null>(null);
  const [loginUrl, setLoginUrl] = useState<string | null>(null);
  const [loginBusy, setLoginBusy] = useState(false);
  const [loginCode, setLoginCode] = useState("");
  const [loginError, setLoginError] = useState<string | null>(null);

  // other sessions on this machine
  const [machineSessions, setMachineSessions] = useState<MachineSession[]>([]);

  // screen
  const [thumbnail, setThumbnail] = useState<string | null>(null);
  const [captureLoading, setCaptureLoading] = useState(false);
  const [captureError, setCaptureError] = useState<string | null>(null);

  // input
  const [typeText, setTypeText] = useState("");
  const [inputFeedback, setInputFeedback] = useState<{ msg: string; ok: boolean } | null>(null);

  // ── init ──
  useEffect(() => {
    listDirs().then((r) => {
      setDirs(r.dirs);
      // The backend resolves the real home; adopt its first entry as default
      // rather than assuming /home/deck on the frontend.
      setWorkingDir((cur) => cur || r.dirs[0] || "");
    });
    syncStatus();
    syncAuth();
    syncMachineSessions();
    getScreenState().then((r) => { if (r.thumbnail) setThumbnail(r.thumbnail); });
  }, []);

  useEffect(() => {
    const id = setInterval(syncStatus, 3000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const id = setInterval(syncMachineSessions, 10000);
    return () => clearInterval(id);
  }, []);

  async function syncAuth() {
    const r = await getAuth();
    setLoggedIn(r.logged_in);
    setAccount(r.email ?? null);
    setPlan(r.plan ?? null);
    if (r.logged_in) {
      setLoginUrl(null);
      setLoginError(null);
    }
  }

  async function syncMachineSessions() {
    const r = await listSessions();
    setMachineSessions(r.sessions ?? []);
  }

  async function handleStartLogin() {
    setLoginBusy(true);
    setLoginError(null);
    const r = await startLogin();
    setLoginBusy(false);
    if (r.success && r.url) setLoginUrl(r.url);
    else setLoginError(r.error ?? "Could not start login");
  }

  async function handleSubmitCode() {
    if (!loginCode.trim()) return;
    setLoginBusy(true);
    setLoginError(null);
    const r = await submitLoginCode(loginCode);
    setLoginBusy(false);
    setLoginCode("");
    if (r.success) {
      setLoginUrl(null);
      await syncAuth();
    } else {
      setLoginError(r.error ?? "Login failed");
    }
  }

  async function handleCancelLogin() {
    await cancelLogin();
    setLoginUrl(null);
    setLoginCode("");
    setLoginError(null);
  }

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
      {/* ── Sign in ────────────────────────────────────────────────────── */}
      {loggedIn === false && (
        <PanelSection title="Sign in to Claude Code">
          {!loginUrl && (
            <>
              <PanelSectionRow>
                <div style={{ fontSize: 11, color: "#f0a500", lineHeight: 1.4 }}>
                  Not signed in — a session can't start until you do.
                </div>
              </PanelSectionRow>
              <PanelSectionRow>
                <ButtonItem layout="below" disabled={loginBusy} onClick={handleStartLogin}>
                  {loginBusy ? "Starting…" : "Sign in"}
                </ButtonItem>
              </PanelSectionRow>
            </>
          )}

          {loginUrl && (
            <>
              <PanelSectionRow>
                <div style={{ fontSize: 11, color: "#aaa", lineHeight: 1.4 }}>
                  Scan with your phone, approve the login, then paste the code below.
                </div>
              </PanelSectionRow>
              <PanelSectionRow>
                <QrCode text={loginUrl} />
              </PanelSectionRow>
              <PanelSectionRow>
                <TextField
                  label="Code from browser"
                  value={loginCode}
                  onChange={(e) => setLoginCode(e.target.value)}
                />
              </PanelSectionRow>
              <PanelSectionRow>
                <ButtonItem
                  layout="below"
                  disabled={loginBusy || !loginCode.trim()}
                  onClick={handleSubmitCode}
                >
                  {loginBusy ? "Signing in…" : "Submit code"}
                </ButtonItem>
              </PanelSectionRow>
              <PanelSectionRow>
                <ButtonItem layout="below" onClick={handleCancelLogin}>
                  Cancel
                </ButtonItem>
              </PanelSectionRow>
            </>
          )}

          {loginError && (
            <PanelSectionRow>
              <div style={{ fontSize: 11, color: "#f44336", wordBreak: "break-word" }}>
                {loginError}
              </div>
            </PanelSectionRow>
          )}
        </PanelSection>
      )}

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

        {loggedIn && account && (
          <PanelSectionRow>
            <div style={{ fontSize: 11, color: "#888" }}>
              {account}{plan ? ` · ${plan}` : ""}
            </div>
          </PanelSectionRow>
        )}

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

      {/* ── Sessions on this machine ───────────────────────────────────── */}
      {machineSessions.length > 0 && (
        <PanelSection title="Sessions on this device">
          {machineSessions.map((s) => (
            <PanelSectionRow key={s.id}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, width: "100%" }}>
                <div
                  style={{
                    width: 8, height: 8, borderRadius: "50%", flexShrink: 0,
                    background: s.live ? "#4caf50" : "#555",
                    boxShadow: s.live ? "0 0 6px #4caf50" : "none",
                  }}
                />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{
                    fontSize: 12, fontWeight: s.current ? 700 : 500,
                    color: s.current ? "#5ba3f5" : "#ddd",
                    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                  }}>
                    {s.label}{s.current ? " (this panel)" : ""}
                  </div>
                  <div style={{
                    fontSize: 10, color: "#777",
                    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                  }}>
                    {s.live ? "running" : relativeTime(s.mtime)} · {s.short_id}
                  </div>
                </div>
              </div>
            </PanelSectionRow>
          ))}
          <PanelSectionRow>
            <div style={{ fontSize: 10, color: "#666", lineHeight: 1.4 }}>
              Green means a claude process is live in that directory. Open it from
              the Claude app — remote control is always on for new sessions.
            </div>
          </PanelSectionRow>
        </PanelSection>
      )}

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
